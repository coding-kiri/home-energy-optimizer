import base64
import binascii
import json
import logging
import os
import re
import time
from typing import Any

import boto3
import psycopg2
from psycopg2.extensions import connection as PgConnection
from psycopg2.extras import execute_values

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SECRET_ARN = os.environ["SECRET_ARN"]
PG_TABLE = os.getenv("PG_TABLE", "meter_readings")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", PG_TABLE):
    raise ValueError("PG_TABLE must be an unqualified Postgres identifier")

_secrets_client = boto3.client("secretsmanager")
_secret_response = _secrets_client.get_secret_value(SecretId=SECRET_ARN)
_connection_string = _secret_response.get("SecretString")

if not _connection_string:
    raise ValueError("Lakebase secret must contain a connection string")


def _open_connection() -> PgConnection:
    connection = psycopg2.connect(_connection_string)
    connection.autocommit = False
    return connection


_connection = _open_connection()


def _close_connection(connection: PgConnection) -> None:
    try:
        connection.close()
    except psycopg2.Error:
        logger.warning("Failed to close unusable Postgres connection")


def _rollback_connection(connection: PgConnection) -> None:
    try:
        connection.rollback()
    except psycopg2.Error:
        logger.warning("Failed to roll back Postgres transaction")


def _get_connection() -> PgConnection:
    global _connection

    if _connection.closed:
        _connection = _open_connection()
        return _connection

    try:
        with _connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        _connection.rollback()
    except psycopg2.Error:
        _close_connection(_connection)
        _connection = _open_connection()

    return _connection


def _record_metadata(record: dict[str, Any]) -> tuple[str, str]:
    kinesis_record = record.get("kinesis", {})
    partition_key = str(kinesis_record.get("partitionKey", "unknown"))
    sequence_number = str(kinesis_record.get("sequenceNumber", "unknown"))
    return partition_key, sequence_number


def _decode_record(record: dict[str, Any]) -> tuple[str, str, Any] | None:
    partition_key, sequence_number = _record_metadata(record)

    try:
        encoded_data = record["kinesis"]["data"]
        decoded_data = base64.b64decode(encoded_data, validate=True).decode("utf-8")
        payload = json.loads(decoded_data)
    except (
        KeyError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ):
        logger.warning(
            "Skipping malformed record partition_key=%s sequence_number=%s",
            partition_key,
            sequence_number,
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "Skipping non-object record partition_key=%s sequence_number=%s",
            partition_key,
            sequence_number,
        )
        return None

    required_fields = ("household_id", "reading_timestamp", "consumption_kwh")
    if any(payload.get(field) is None for field in required_fields):
        logger.warning(
            "Skipping record with missing fields partition_key=%s sequence_number=%s",
            partition_key,
            sequence_number,
        )
        return None

    return (
        str(payload["household_id"]),
        str(payload["reading_timestamp"]),
        payload["consumption_kwh"],
    )


def handler(event: dict[str, Any], context: Any) -> dict[str, int]:
    del context

    started_at = time.monotonic()
    records = event.get("Records", [])
    received = len(records)
    inserted = 0
    skipped = 0
    succeeded = False
    connection = None

    try:
        connection = _get_connection()
        rows = []
        for record in records:
            row = _decode_record(record)
            if row is None:
                skipped += 1
                continue
            rows.append(row)

        if rows:
            with connection.cursor() as cursor:
                execute_values(
                    cursor,
                    f"INSERT INTO {PG_TABLE} "
                    "(household_id, reading_timestamp, consumption_kwh) "
                    "VALUES %s ON CONFLICT DO NOTHING",
                    rows,
                    page_size=500,
                )
                pending_inserted = cursor.rowcount
            connection.commit()
            inserted = pending_inserted

        succeeded = True
        return {
            "received": received,
            "inserted": inserted,
            "skipped": skipped,
        }
    except Exception:
        if connection is not None:
            _rollback_connection(connection)
        raise
    finally:
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        logger.info(
            "Batch complete succeeded=%s received=%d inserted=%d skipped=%d elapsed_ms=%d",
            succeeded,
            received,
            inserted,
            skipped,
            elapsed_ms,
        )
