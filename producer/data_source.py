"""Windowed reader for the London smart meter dataset.

The block CSVs each span the full time range for a subset of households, so
serving one timestamp requires touching every file. Loading the whole dataset
(~10 GB CSV) into a small Fargate task is not an option. Instead we load a
sliding window of WINDOW_DAYS at a time: read all files, keep only rows in
[cursor, cursor + window), replay them, then refill.

At 5,000 households x 48 readings/day, a 7-day window is ~1.7M rows —
comfortable in 2 GB of memory. The refill re-reads the source files, which is
wasteful for CSV; converting the source to parquet (partitioned by month)
makes refills near-instant and is the recommended prod layout.

Expected long-format columns (Kaggle London set):
    LCLid, tstp, energy(kWh/hh)   -- names configurable below.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger(__name__)

# Column names in the source files -> canonical names.
COLUMN_MAP = {
    "LCLid": "household_id",
    "tstp": "reading_timestamp",
    "energy(kWh/hh)": "consumption_kwh",
    # already-canonical names pass through
    "household_id": "household_id",
    "reading_timestamp": "reading_timestamp",
    "consumption_kwh": "consumption_kwh",
}


def _list_source_files(source_uri: str) -> list[str]:
    """Return the list of data files (csv/parquet) under a local path or s3:// prefix."""
    if source_uri.startswith("s3://"):
        import boto3

        bucket, _, prefix = source_uri.removeprefix("s3://").partition("/")
        s3 = boto3.client("s3")
        paginator = s3.get_paginator("list_objects_v2")
        keys = [
            obj["Key"]
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
            for obj in page.get("Contents", [])
            if obj["Key"].endswith((".csv", ".parquet"))
        ]
        return [f"s3://{bucket}/{k}" for k in sorted(keys)]

    from pathlib import Path

    p = Path(source_uri)
    if p.is_file():
        return [str(p)]
    return sorted(
        str(f) for f in p.rglob("*") if f.suffix in (".csv", ".parquet")
    )


def _read_file(path: str) -> pd.DataFrame:
    if path.startswith("s3://"):
        import boto3

        bucket, _, key = path.removeprefix("s3://").partition("/")
        body = boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
        buf = io.BytesIO(body)
        df = pd.read_parquet(buf) if path.endswith(".parquet") else pd.read_csv(buf)
    else:
        df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    return df.rename(columns={c: COLUMN_MAP[c] for c in df.columns if c in COLUMN_MAP})


class WindowedReplaySource:
    """Yields (timestamp, rows) waves in timestamp order, refilling as needed."""

    def __init__(
        self,
        source_uri: str,
        window_days: int,
        household_limit: int | None = None,
    ):
        self._files = _list_source_files(source_uri)
        if not self._files:
            raise RuntimeError(f"No csv/parquet files found under {source_uri}")
        log.info("Found %d source files under %s", len(self._files), source_uri)
        self._window = timedelta(days=window_days)
        self._household_limit = household_limit
        self._households: list[str] | None = None  # frozen on first load

    def _load_window(self, start: datetime) -> pd.DataFrame:
        end = start + self._window
        frames = []
        for f in self._files:
            df = _read_file(f)
            missing = {"household_id", "reading_timestamp", "consumption_kwh"} - set(df.columns)
            if missing:
                log.warning("Skipping %s — missing columns %s", f, missing)
                continue
            ts = pd.to_datetime(df["reading_timestamp"], utc=True, errors="coerce")
            df = df.assign(reading_timestamp=ts)
            df = df[(df["reading_timestamp"] >= start) & (df["reading_timestamp"] < end)]
            if not df.empty:
                frames.append(df[["household_id", "reading_timestamp", "consumption_kwh"]])

        if not frames:
            return pd.DataFrame(
                columns=["household_id", "reading_timestamp", "consumption_kwh"]
            )

        window = pd.concat(frames, ignore_index=True)
        window = window.dropna(subset=["household_id", "reading_timestamp"])
        window["consumption_kwh"] = pd.to_numeric(
            window["consumption_kwh"], errors="coerce"
        )

        if self._household_limit:
            if self._households is None:
                self._households = sorted(window["household_id"].unique())[
                    : self._household_limit
                ]
                log.info(
                    "HOUSEHOLD_LIMIT=%d -> replaying %s",
                    self._household_limit,
                    self._households,
                )
            window = window[window["household_id"].isin(self._households)]

        return window.sort_values(["reading_timestamp", "household_id"])

    def waves(self, resume_after: datetime):
        """Generator of (wave_timestamp, DataFrame) strictly after resume_after."""
        cursor = resume_after
        while True:
            window = self._load_window(cursor + timedelta(seconds=1))
            if window.empty:
                # Probe one more window ahead before declaring the dataset done.
                probe = self._load_window(cursor + self._window)
                if probe.empty:
                    log.info("No readings after %s — replay complete.", cursor)
                    return
                window = probe
                cursor = cursor + self._window

            for ts, rows in window.groupby("reading_timestamp", sort=True):
                yield ts.to_pydatetime().astimezone(timezone.utc), rows
                cursor = ts.to_pydatetime()
