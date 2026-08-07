"""Checkpoint store for the replay cursor.

The checkpoint holds the last *published* wave timestamp (ISO 8601 UTC).
On restart the producer resumes from the wave after it. Without this, a task
restart would replay from START_DATE and flood the S3 raw zone with
duplicates — the serving layer's primary key protects Postgres/Lakebase,
but nothing protects raw.

Two backends:
- SsmCheckpoint  — production (Fargate). One String parameter.
- FileCheckpoint — local dev. A plain text file.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


def parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def format_ts(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Checkpoint(Protocol):
    def read(self) -> datetime | None: ...
    def write(self, ts: datetime) -> None: ...


class FileCheckpoint:
    def __init__(self, path: str):
        self._path = Path(path)

    def read(self) -> datetime | None:
        if not self._path.exists():
            return None
        raw = self._path.read_text().strip()
        return parse_ts(raw) if raw else None

    def write(self, ts: datetime) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(format_ts(ts))


class SsmCheckpoint:
    def __init__(self, param_name: str, region: str):
        import boto3

        self._param = param_name
        self._ssm = boto3.client("ssm", region_name=region)

    def read(self) -> datetime | None:
        try:
            resp = self._ssm.get_parameter(Name=self._param)
        except self._ssm.exceptions.ParameterNotFound:
            return None
        raw = resp["Parameter"]["Value"].strip()
        return parse_ts(raw) if raw else None

    def write(self, ts: datetime) -> None:
        self._ssm.put_parameter(
            Name=self._param,
            Value=format_ts(ts),
            Type="String",
            Overwrite=True,
        )


def build_checkpoint(param_name: str | None, file_path: str, region: str) -> Checkpoint:
    if param_name:
        return SsmCheckpoint(param_name, region)
    return FileCheckpoint(file_path)
