"""MQTT publisher.

One persistent connection for the process lifetime, QoS 1, publishes waited
on so the SSM checkpoint is only advanced after the broker has acked the wave.

Cert resolution order:
1. CERT_SECRET_ARN set  -> fetch cert/key/ca JSON from Secrets Manager,
   write to /tmp, connect with mTLS (Fargate + IoT Core path).
2. CERT_PATH/KEY_PATH   -> mTLS with local files (testing against IoT Core
   from a laptop).
3. MQTT_TLS=false       -> plain TCP (local mosquitto in docker-compose).
"""

from __future__ import annotations

import json
import logging
import ssl
import tempfile
from pathlib import Path

import paho.mqtt.client as mqtt

from config import Config

log = logging.getLogger(__name__)

PUBLISH_TIMEOUT_S = 10


def _certs_from_secrets_manager(arn: str, region: str) -> tuple[str, str, str | None]:
    """Secret is JSON: {"certificate_pem": ..., "private_key": ..., "ca_pem": ...}."""
    import boto3

    sm = boto3.client("secretsmanager", region_name=region)
    secret = json.loads(sm.get_secret_value(SecretId=arn)["SecretString"])

    tmp = Path(tempfile.mkdtemp(prefix="iot-certs-"))
    cert = tmp / "cert.pem"
    key = tmp / "key.pem"
    cert.write_text(secret["certificate_pem"])
    key.write_text(secret["private_key"])
    key.chmod(0o600)

    ca = None
    if secret.get("ca_pem"):
        ca_file = tmp / "ca.pem"
        ca_file.write_text(secret["ca_pem"])
        ca = str(ca_file)
    return str(cert), str(key), ca


class MqttPublisher:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.mqtt_client_id,  # IoT policy may pin this — see runbook
            protocol=mqtt.MQTTv311,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        if cfg.mqtt_tls:
            if cfg.cert_secret_arn:
                cert, key, ca = _certs_from_secrets_manager(
                    cfg.cert_secret_arn, cfg.aws_region
                )
            else:
                cert, key, ca = cfg.cert_path, cfg.key_path, cfg.ca_path
            if not (cert and key):
                raise RuntimeError(
                    "MQTT_TLS=true but no certificate configured "
                    "(set CERT_SECRET_ARN or CERT_PATH/KEY_PATH)"
                )
            self._client.tls_set(
                ca_certs=ca,  # None -> system CA store; Amazon ATS root is in it
                certfile=cert,
                keyfile=key,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )

    @staticmethod
    def _on_connect(client, userdata, flags, reason_code, properties):
        log.info("MQTT connected: %s", reason_code)

    @staticmethod
    def _on_disconnect(client, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected: %s (paho will auto-reconnect)", reason_code)

    def connect(self) -> None:
        self._client.connect(self._cfg.mqtt_host, self._cfg.mqtt_port, keepalive=60)
        self._client.loop_start()

    def publish(self, payload: dict) -> None:
        # Trailing \n is deliberate: Firehose concatenates KDS records verbatim into
        # S3 objects with no delimiter. The newline rides through IoT rule -> KDS ->
        # Firehose untouched and makes the raw files valid JSONL for Auto Loader.
        info = self._client.publish(
            self._cfg.mqtt_topic, json.dumps(payload) + "\n", qos=1
        )
        info.wait_for_publish(timeout=PUBLISH_TIMEOUT_S)
        if not info.is_published():
            raise RuntimeError(f"Publish not acked within {PUBLISH_TIMEOUT_S}s")

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
