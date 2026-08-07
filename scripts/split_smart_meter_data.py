"""
Date-shift and split London Smart Meter half-hourly block CSVs.

Reads Kaggle block_*.csv files, shifts timestamps forward by OFFSET_DAYS, and writes:
  - backfill/  → date=/hour= uncompressed JSONL (Firehose raw-zone layout)
  - streaming/ → monthly snappy parquet (replay producer source)

Usage:
    uv run python scripts/split_smart_meter_data.py \\
        --input-dir data/halfhourly_dataset \\
        --output-dir data/halfhourly_split

    uv run python scripts/split_smart_meter_data.py \\
        --input-dir data/halfhourly_dataset \\
        --output-dir data/halfhourly_split \\
        --verify
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# 365 weeks — preserves weekday and half-hour slot under a fixed UTC shift.
OFFSET_DAYS = 2555

BACKFILL_START = pd.Timestamp("2019-08-12 00:00:00", tz="UTC")
STREAMING_START = pd.Timestamp("2020-08-10 00:00:00", tz="UTC")
# Exclusive upper bound used only by --verify (streaming ends ~2021-02-26).
STREAMING_VERIFY_END = pd.Timestamp("2021-03-01 00:00:00", tz="UTC")

PARQUET_SCHEMA = pa.schema(
    [
        ("household_id", pa.string()),
        ("reading_timestamp", pa.timestamp("us", tz="UTC")),
        ("consumption_kwh", pa.float64()),
    ]
)

BACKFILL_JSON_KEYS = ("household_id", "reading_timestamp", "consumption_kwh")
CONSUMPTION_DECIMALS = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Date-shift and split London Smart Meter block CSVs."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="Directory of block_*.csv files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Root directory for backfill/ (JSONL) and streaming/ (parquet) output",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Re-check existing output against acceptance criteria (no write)",
    )
    return parser.parse_args()


def list_block_files(input_dir: Path) -> list[Path]:
    files = sorted(input_dir.glob("block_*.csv"), key=lambda p: int(p.stem.split("_")[1]))
    if not files:
        raise FileNotFoundError(f"No block_*.csv files found in {input_dir}")
    return files


def assert_output_dir_empty(output_dir: Path) -> None:
    """Refuse to write into a non-empty directory — appends would duplicate rows."""
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(
            f"Refusing to run: --output-dir is not empty: {output_dir}\n"
            "Delete it manually before re-running the split."
        )


def transform_block(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce types, drop unexpected columns, rename, and apply the date offset."""
    keep = [col for col in ("LCLid", "tstp", "energy(kWh/hh)") if col in df.columns]
    missing = {"LCLid", "tstp", "energy(kWh/hh)"} - set(keep)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    df = df.loc[:, keep]

    out = pd.DataFrame(
        {
            "household_id": df["LCLid"].astype("string"),
            "reading_timestamp": pd.to_datetime(df["tstp"], utc=True),
            # Literal "Null" (and other non-numeric) becomes NaN; rows are kept.
            "consumption_kwh": pd.to_numeric(df["energy(kWh/hh)"], errors="coerce"),
        }
    )
    out["reading_timestamp"] = out["reading_timestamp"] + pd.Timedelta(days=OFFSET_DAYS)
    return out


def route_rows(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Split into backfill / streaming; return discarded count."""
    discarded = int((df["reading_timestamp"] < BACKFILL_START).sum())
    backfill = df[
        (df["reading_timestamp"] >= BACKFILL_START)
        & (df["reading_timestamp"] < STREAMING_START)
    ]
    streaming = df[df["reading_timestamp"] >= STREAMING_START]
    return backfill, streaming, discarded


def year_month_key(timestamps: pd.Series) -> pd.Series:
    return timestamps.dt.strftime("%Y-%m")


def date_hour_key(timestamps: pd.Series) -> pd.Series:
    """Partition key matching Firehose date=YYYY-MM-DD/hour=HH layout."""
    return timestamps.dt.strftime("%Y-%m-%d/%H")


def format_timestamp_z(ts: pd.Timestamp) -> str:
    """ISO-8601 UTC with Z suffix, matching the producer payload."""
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_consumption_json(value: object) -> str:
    """JSON number rounded to 4 decimals, or JSON null."""
    if value is None or pd.isna(value):
        return "null"
    return json.dumps(round(float(value), CONSUMPTION_DECIMALS))


def write_backfill_jsonl(df: pd.DataFrame, root: Path) -> int:
    """Append rows to readings.jsonl per shifted event date+hour; return rows written."""
    if df.empty:
        return 0

    # No sorting — Firehose raw files are unordered; one file per hour is appended to.
    keys = date_hour_key(df["reading_timestamp"])
    rows_written = 0

    for key, group in df.groupby(keys, sort=False):
        event_date, hour = str(key).split("/")
        partition_dir = root / "backfill" / f"date={event_date}" / f"hour={hour}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        out_path = partition_dir / "readings.jsonl"

        # LCLid values are alphanumeric — safe to embed without JSON string escaping.
        household = group["household_id"].astype(str)
        timestamp = group["reading_timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        consumption = group["consumption_kwh"].map(format_consumption_json)
        lines = (
            '{"household_id": "'
            + household
            + '", "reading_timestamp": "'
            + timestamp
            + '", "consumption_kwh": '
            + consumption
            + "}"
        )

        with out_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines.tolist()))
            handle.write("\n")
        rows_written += len(lines)

    return rows_written


def write_streaming_parquet(df: pd.DataFrame, root: Path, part_stem: str) -> int:
    """Write one snappy parquet part per year_month; return rows written."""
    if df.empty:
        return 0

    # Sort within this part file only; no cross-part ordering.
    df = df.sort_values("reading_timestamp", kind="mergesort")
    months = year_month_key(df["reading_timestamp"])
    rows_written = 0

    for year_month, group in df.groupby(months, sort=True):
        partition_dir = root / "streaming" / f"year_month={year_month}"
        partition_dir.mkdir(parents=True, exist_ok=True)
        out_path = partition_dir / f"{part_stem}.parquet"
        table = pa.Table.from_pandas(group, schema=PARQUET_SCHEMA, preserve_index=False)
        pq.write_table(table, out_path, compression="snappy")
        rows_written += len(group)

    return rows_written


def run_split(input_dir: Path, output_dir: Path) -> dict[str, int | pd.Timestamp | None]:
    assert_output_dir_empty(output_dir)
    block_files = list_block_files(input_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows_in = 0
    rows_backfill = 0
    rows_streaming = 0
    rows_discarded = 0
    backfill_min: pd.Timestamp | None = None
    backfill_max: pd.Timestamp | None = None
    streaming_min: pd.Timestamp | None = None
    streaming_max: pd.Timestamp | None = None

    for block_path in block_files:
        block_id = int(block_path.stem.split("_")[1])
        part_stem = f"part-{block_id:05d}"
        print(f"Processing {block_path.name} ...", flush=True)

        # Read energy as string so literal "Null" is not lost to mixed-type inference.
        raw = pd.read_csv(block_path, dtype={"energy(kWh/hh)": "string"})
        rows_in += len(raw)
        transformed = transform_block(raw)
        backfill, streaming, discarded = route_rows(transformed)
        rows_discarded += discarded

        if not backfill.empty:
            ts_min = backfill["reading_timestamp"].min()
            ts_max = backfill["reading_timestamp"].max()
            backfill_min = ts_min if backfill_min is None else min(backfill_min, ts_min)
            backfill_max = ts_max if backfill_max is None else max(backfill_max, ts_max)
            rows_backfill += write_backfill_jsonl(backfill, output_dir)

        if not streaming.empty:
            ts_min = streaming["reading_timestamp"].min()
            ts_max = streaming["reading_timestamp"].max()
            streaming_min = ts_min if streaming_min is None else min(streaming_min, ts_min)
            streaming_max = ts_max if streaming_max is None else max(streaming_max, ts_max)
            rows_streaming += write_streaming_parquet(streaming, output_dir, part_stem)

    summary = {
        "rows_in": rows_in,
        "rows_backfill": rows_backfill,
        "rows_streaming": rows_streaming,
        "rows_discarded": rows_discarded,
        "backfill_min": backfill_min,
        "backfill_max": backfill_max,
        "streaming_min": streaming_min,
        "streaming_max": streaming_max,
        "blocks": len(block_files),
    }
    print_summary(summary)
    return summary


def print_summary(summary: dict[str, int | pd.Timestamp | None]) -> None:
    print("\n=== Split summary ===")
    print(f"blocks processed : {summary['blocks']}")
    print(f"rows_in          : {summary['rows_in']}")
    print(f"rows_backfill    : {summary['rows_backfill']}")
    print(f"rows_streaming   : {summary['rows_streaming']}")
    print(f"rows_discarded   : {summary['rows_discarded']}")
    balance = (
        summary["rows_backfill"] + summary["rows_streaming"] + summary["rows_discarded"]
    )
    print(f"rows accounted   : {balance} (match={balance == summary['rows_in']})")
    print(f"backfill range   : {summary['backfill_min']} .. {summary['backfill_max']}")
    print(f"streaming range  : {summary['streaming_min']} .. {summary['streaming_max']}")


def iter_jsonl_files(root: Path) -> list[Path]:
    backfill_dir = root / "backfill"
    if not backfill_dir.exists():
        return []
    return sorted(backfill_dir.glob("date=*/hour=*/readings.jsonl"))


def iter_parquet_files(root: Path) -> list[Path]:
    streaming_dir = root / "streaming"
    if not streaming_dir.exists():
        return []
    return sorted(streaming_dir.glob("year_month=*/part-*.parquet"))


def count_jsonl_rows(files: list[Path]) -> int:
    total = 0
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            total += sum(1 for line in handle if line.strip())
    return total


def count_parquet_rows(files: list[Path]) -> int:
    return sum(pq.read_metadata(path).num_rows for path in files)


def parse_jsonl_timestamp(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def scan_jsonl_timestamp_bounds(
    files: list[Path],
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    ts_min: pd.Timestamp | None = None
    ts_max: pd.Timestamp | None = None
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                ts = parse_jsonl_timestamp(record["reading_timestamp"])
                ts_min = ts if ts_min is None else min(ts_min, ts)
                ts_max = ts if ts_max is None else max(ts_max, ts)
    return ts_min, ts_max


def scan_parquet_timestamp_bounds(
    files: list[Path],
) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    ts_min: pd.Timestamp | None = None
    ts_max: pd.Timestamp | None = None
    for path in files:
        table = pq.read_table(path, columns=["reading_timestamp"], schema=PARQUET_SCHEMA)
        ts = table.column("reading_timestamp").to_pandas()
        if ts.empty:
            continue
        file_min = ts.min()
        file_max = ts.max()
        ts_min = file_min if ts_min is None else min(ts_min, file_min)
        ts_max = file_max if ts_max is None else max(ts_max, file_max)
    return ts_min, ts_max


def count_jsonl_null_consumption(files: list[Path]) -> int:
    nulls = 0
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if "consumption_kwh" not in record:
                    raise AssertionError(f"Omitted consumption_kwh key in {path}")
                if record["consumption_kwh"] == "Null":
                    raise AssertionError(f"String 'Null' found in {path}")
                if record["consumption_kwh"] is None:
                    nulls += 1
    return nulls


def count_parquet_null_consumption(files: list[Path]) -> int:
    nulls = 0
    for path in files:
        table = pq.read_table(path, columns=["consumption_kwh"], schema=PARQUET_SCHEMA)
        nulls += table.column("consumption_kwh").null_count
    return nulls


def count_input_nulls_by_route(input_dir: Path) -> tuple[int, int]:
    """Count literal 'Null' energy strings routed to backfill and streaming."""
    null_backfill = 0
    null_streaming = 0
    for block_path in list_block_files(input_dir):
        raw = pd.read_csv(
            block_path,
            usecols=["tstp", "energy(kWh/hh)"],
            dtype={"energy(kWh/hh)": "string"},
        )
        null_mask = raw["energy(kWh/hh)"] == "Null"
        if not null_mask.any():
            continue
        shifted = pd.to_datetime(raw.loc[null_mask, "tstp"], utc=True) + pd.Timedelta(
            days=OFFSET_DAYS
        )
        null_backfill += int(
            ((shifted >= BACKFILL_START) & (shifted < STREAMING_START)).sum()
        )
        null_streaming += int((shifted >= STREAMING_START).sum())
    return null_backfill, null_streaming


def verify_backfill_partitions(files: list[Path]) -> None:
    for path in files:
        if path.name != "readings.jsonl":
            raise AssertionError(f"Expected readings.jsonl, found {path}")
        hour_dir = path.parent
        date_dir = hour_dir.parent
        event_date = date_dir.name.removeprefix("date=")
        hour = hour_dir.name.removeprefix("hour=")
        names = {p.name for p in hour_dir.iterdir()}
        if names != {"readings.jsonl"}:
            raise AssertionError(
                f"Expected exactly one readings.jsonl in {hour_dir}, found {sorted(names)}"
            )
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                ts = parse_jsonl_timestamp(record["reading_timestamp"])
                row_date = ts.strftime("%Y-%m-%d")
                row_hour = ts.strftime("%H")
                if row_date != event_date or row_hour != hour:
                    raise AssertionError(
                        f"Partition mismatch in {path}:{line_no}: "
                        f"{row_date}/{row_hour} != {event_date}/{hour}"
                    )


def verify_streaming_partitions(files: list[Path]) -> None:
    for path in files:
        year_month = path.parent.name.removeprefix("year_month=")
        table = pq.read_table(path, columns=["reading_timestamp"], schema=PARQUET_SCHEMA)
        ts = table.column("reading_timestamp").to_pandas()
        bad = ts.dt.strftime("%Y-%m") != year_month
        if bad.any():
            raise AssertionError(
                f"Partition mismatch in {path}: found other months besides {year_month}"
            )


def find_row_in_jsonl(
    files: list[Path],
    household_id: str,
    shifted_ts: pd.Timestamp,
) -> bool:
    target = format_timestamp_z(shifted_ts)
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if (
                    record["household_id"] == household_id
                    and record["reading_timestamp"] == target
                ):
                    return True
    return False


def find_row_in_parquet(
    files: list[Path],
    household_id: str,
    shifted_ts: pd.Timestamp,
) -> bool:
    for path in files:
        df = pq.read_table(path, schema=PARQUET_SCHEMA).to_pandas()
        hit = df[
            (df["household_id"] == household_id)
            & (df["reading_timestamp"] == shifted_ts)
        ]
        if not hit.empty:
            return True
    return False


def verify_spot_check(input_dir: Path, output_dir: Path) -> None:
    """Confirm a sampled original tstp shifts by OFFSET_DAYS onto an output row."""
    sample_path = list_block_files(input_dir)[0]
    raw = pd.read_csv(sample_path, nrows=5000, dtype={"energy(kWh/hh)": "string"})
    numeric_mask = pd.to_numeric(raw["energy(kWh/hh)"], errors="coerce").notna()
    sample = raw.loc[numeric_mask].iloc[0]

    original_ts = pd.to_datetime(sample["tstp"], utc=True)
    shifted_ts = original_ts + pd.Timedelta(days=OFFSET_DAYS)
    household_id = str(sample["LCLid"])

    if shifted_ts < BACKFILL_START:
        raise AssertionError("Sampled row fell into discard range; pick another sample")

    if shifted_ts < STREAMING_START:
        part_dir = (
            output_dir
            / "backfill"
            / f"date={shifted_ts.strftime('%Y-%m-%d')}"
            / f"hour={shifted_ts.strftime('%H')}"
        )
        part_file = part_dir / "readings.jsonl"
        part_files = [part_file] if part_file.exists() else []
        found = find_row_in_jsonl(part_files, household_id, shifted_ts)
        dataset = "backfill"
    else:
        part_dir = (
            output_dir / "streaming" / f"year_month={shifted_ts.strftime('%Y-%m')}"
        )
        part_files = sorted(part_dir.glob("part-*.parquet"))
        found = find_row_in_parquet(part_files, household_id, shifted_ts)
        dataset = "streaming"

    if not part_files:
        raise AssertionError(f"Missing partition for spot check: {part_dir}")
    if not found:
        raise AssertionError(
            f"Spot-check row not found: {household_id} @ {shifted_ts} in {dataset}"
        )

    roundtrip = shifted_ts - pd.Timedelta(days=OFFSET_DAYS)
    if roundtrip != original_ts:
        raise AssertionError(f"Roundtrip mismatch: {roundtrip} != {original_ts}")
    if roundtrip.day_name() != original_ts.day_name():
        raise AssertionError("Weekday not preserved after offset")

    print(
        f"Spot check OK: {household_id} {original_ts} -> {shifted_ts} "
        f"({original_ts.day_name()}, {dataset})"
    )


def verify_backfill_json_sample(files: list[Path]) -> None:
    """A sampled backfill line must look like a producer message."""
    sample_line = None
    for path in files:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    sample_line = line
                    break
        if sample_line is not None:
            break

    if sample_line is None:
        raise AssertionError("No backfill JSONL lines found to sample")

    record = json.loads(sample_line)
    if tuple(record.keys()) != BACKFILL_JSON_KEYS:
        raise AssertionError(
            f"Expected keys {BACKFILL_JSON_KEYS}, got {tuple(record.keys())}"
        )
    ts = record["reading_timestamp"]
    if not isinstance(ts, str) or not ts.endswith("Z"):
        raise AssertionError(f"Timestamp must be Z-suffixed ISO-8601 string, got {ts!r}")
    parsed = pd.Timestamp(ts)
    if parsed.tzinfo is None:
        raise AssertionError(f"Timestamp did not parse as timezone-aware: {ts!r}")
    print(f"Backfill JSON sample OK: {sample_line[:120]}...")


def run_verify(input_dir: Path, output_dir: Path) -> None:
    print("=== Verifying output ===")
    block_files = list_block_files(input_dir)
    rows_in = 0
    for block_path in block_files:
        rows_in += sum(1 for _ in open(block_path, newline="")) - 1

    backfill_files = iter_jsonl_files(output_dir)
    streaming_files = iter_parquet_files(output_dir)
    rows_backfill = count_jsonl_rows(backfill_files)
    rows_streaming = count_parquet_rows(streaming_files)

    rows_discarded = 0
    for block_path in block_files:
        raw = pd.read_csv(block_path, usecols=["tstp"])
        shifted = pd.to_datetime(raw["tstp"], utc=True) + pd.Timedelta(days=OFFSET_DAYS)
        rows_discarded += int((shifted < BACKFILL_START).sum())

    print(f"rows_in          : {rows_in}")
    print(f"rows_backfill    : {rows_backfill}")
    print(f"rows_streaming   : {rows_streaming}")
    print(f"rows_discarded   : {rows_discarded}")
    if rows_in != rows_backfill + rows_streaming + rows_discarded:
        raise AssertionError(
            "Row balance failed: "
            f"{rows_in} != {rows_backfill} + {rows_streaming} + {rows_discarded}"
        )
    print("1. Row balance OK")

    if not backfill_files or not streaming_files:
        raise AssertionError("Expected non-empty backfill and streaming outputs")

    bf_min, bf_max = scan_jsonl_timestamp_bounds(backfill_files)
    st_min, st_max = scan_parquet_timestamp_bounds(streaming_files)

    if bf_min is None or bf_max is None or st_min is None or st_max is None:
        raise AssertionError("Could not determine timestamp bounds from output")
    if not (BACKFILL_START <= bf_min and bf_max < STREAMING_START):
        raise AssertionError(f"Backfill range out of bounds: {bf_min} .. {bf_max}")
    if not (STREAMING_START <= st_min and st_max < STREAMING_VERIFY_END):
        raise AssertionError(f"Streaming range out of bounds: {st_min} .. {st_max}")
    print(f"2. Timestamp bounds OK (backfill {bf_min}..{bf_max}, streaming {st_min}..{st_max})")

    verify_spot_check(input_dir, output_dir)
    print("3. Spot check OK")

    verify_backfill_partitions(backfill_files)
    verify_streaming_partitions(streaming_files)
    print("4. Partition membership OK (date=/hour= backfill, year_month= streaming)")

    expected_null_backfill, expected_null_streaming = count_input_nulls_by_route(input_dir)
    null_backfill = count_jsonl_null_consumption(backfill_files)
    null_streaming = count_parquet_null_consumption(streaming_files)
    if null_backfill != expected_null_backfill:
        raise AssertionError(
            f"Backfill null mismatch: output={null_backfill}, input Nulls={expected_null_backfill}"
        )
    if null_streaming != expected_null_streaming:
        raise AssertionError(
            f"Streaming null mismatch: output={null_streaming}, input Nulls={expected_null_streaming}"
        )
    print(
        f"5. Null balance OK (backfill={null_backfill}, streaming={null_streaming}); "
        "JSON null serialization OK"
    )

    verify_backfill_json_sample(backfill_files)
    print("6. Backfill JSON producer-compatible sample OK")
    print("\nAll acceptance checks passed.")


def main() -> int:
    args = parse_args()
    if args.verify:
        run_verify(args.input_dir, args.output_dir)
    else:
        run_split(args.input_dir, args.output_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
