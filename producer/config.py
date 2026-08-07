"""Configuration for the smart meter replay producer.

All settings come from environment variables so the same code runs locally
(docker-compose, plain mosquitto, file checkpoint) and on Fargate
(IoT Core mTLS, SSM checkpoint, certs from Secrets Manager).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Config:
    # --- data source ---
    source_uri: str            # local dir/file or s3://bucket/prefix (csv or parquet)
    start_date: str            # ISO date; first timestamp when no checkpoint exists
    household_limit: int | None
    window_days: int           # how many days of readings to hold in memory at once

    # --- replay clock ---
    replay_speed_factor: float  # 1 = real time; 1800 = one wave per second

    # --- checkpoint ---
    checkpoint_param: str | None  # SSM parameter name; None -> local file
    checkpoint_file: str          # local fallback for dev

    # --- mqtt ---
    mqtt_host: str
    mqtt_port: int
    mqtt_topic: str
    mqtt_client_id: str
    mqtt_tls: bool
    cert_secret_arn: str | None   # Secrets Manager secret with cert/key/ca (prod)
    cert_path: str | None         # file paths for local TLS testing
    key_path: str | None
    ca_path: str | None

    aws_region: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            source_uri=_env("SOURCE_URI", required=True),
            start_date=_env("START_DATE", required=True),
            household_limit=(
                int(v) if (v := _env("HOUSEHOLD_LIMIT")) else None
            ),
            window_days=int(_env("WINDOW_DAYS", "7")),
            replay_speed_factor=float(_env("REPLAY_SPEED_FACTOR", "1")),
            checkpoint_param=_env("CHECKPOINT_PARAM"),
            checkpoint_file=_env("CHECKPOINT_FILE", "/tmp/replay_checkpoint.txt"),
            mqtt_host=_env("MQTT_HOST", required=True),
            mqtt_port=int(_env("MQTT_PORT", "8883")),
            mqtt_topic=_env("MQTT_TOPIC", "smartmeter/readings"),
            mqtt_client_id=_env("MQTT_CLIENT_ID", "smart-meter-replay-producer"),
            mqtt_tls=_env("MQTT_TLS", "true").lower() == "true",
            cert_secret_arn=_env("CERT_SECRET_ARN"),
            cert_path=_env("CERT_PATH"),
            key_path=_env("KEY_PATH"),
            ca_path=_env("CA_PATH"),
            aws_region=_env("AWS_REGION", "eu-central-1"),
        )
