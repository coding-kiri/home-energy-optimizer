"""Smoke test without MQTT or AWS: source windowing, checkpoint, payload, resume."""

import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from checkpoint import FileCheckpoint, parse_ts
from data_source import WindowedReplaySource
from replay import build_payload

DATA = Path("sample_data")
DATA.mkdir(exist_ok=True)


def make_sample():
    """Two 'blocks' in Kaggle long format, 3 households, 2 days half-hourly."""
    ts = pd.date_range("2012-01-01", periods=96, freq="30min", tz="UTC")
    rows_a, rows_b = [], []
    for i, t in enumerate(ts):
        stamp = t.strftime("%Y-%m-%d %H:%M:%S.0000000")
        rows_a.append(("MAC000001", stamp, round(0.1 + 0.01 * (i % 48), 3)))
        rows_a.append(("MAC000002", stamp, "Null" if i == 10 else 0.2))
        rows_b.append(("MAC000003", stamp, 0.3))
    for name, rows in [("block_0.csv", rows_a), ("block_1.csv", rows_b)]:
        pd.DataFrame(rows, columns=["LCLid", "tstp", "energy(kWh/hh)"]).to_csv(
            DATA / name, index=False
        )


def test_waves_ordered_and_complete():
    src = WindowedReplaySource(str(DATA), window_days=1)
    start = datetime(2011, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    waves = list(src.waves(start))
    assert len(waves) == 96, f"expected 96 waves, got {len(waves)}"
    assert all(len(rows) == 3 for _, rows in waves), "each wave: 3 households"
    stamps = [ts for ts, _ in waves]
    assert stamps == sorted(stamps), "waves must be time-ordered"
    # window boundary: wave 49 (day 2) must still appear despite window_days=1
    assert stamps[48].day == 2


def test_household_limit():
    src = WindowedReplaySource(str(DATA), window_days=2, household_limit=2)
    _, rows = next(src.waves(datetime(2011, 12, 31, tzinfo=timezone.utc)))
    assert sorted(rows["household_id"].unique()) == ["MAC000001", "MAC000002"]


def test_null_consumption_becomes_json_null():
    src = WindowedReplaySource(str(DATA), window_days=1)
    waves = list(src.waves(datetime(2011, 12, 31, tzinfo=timezone.utc)))
    _, rows = waves[10]  # the wave where MAC000002 has "Null"
    row = rows[rows["household_id"] == "MAC000002"].iloc[0]
    payload = build_payload(
        next(rows[rows["household_id"] == "MAC000002"].itertuples(index=False))
    )
    assert math.isnan(row["consumption_kwh"])
    assert payload["consumption_kwh"] is None
    assert payload["reading_timestamp"].endswith("Z")


def test_checkpoint_resume():
    cp = FileCheckpoint("/tmp/test_ckpt.txt")
    Path("/tmp/test_ckpt.txt").unlink(missing_ok=True)
    assert cp.read() is None
    cp.write(parse_ts("2012-01-01T05:00:00Z"))
    assert cp.read() == parse_ts("2012-01-01T05:00:00Z")

    src = WindowedReplaySource(str(DATA), window_days=1)
    first_ts, _ = next(src.waves(cp.read()))
    assert first_ts == parse_ts("2012-01-01T05:30:00Z"), (
        f"resume must start strictly after checkpoint, got {first_ts}"
    )


if __name__ == "__main__":
    make_sample()
    for fn in [
        test_waves_ordered_and_complete,
        test_household_limit,
        test_null_consumption_becomes_json_null,
        test_checkpoint_resume,
    ]:
        fn()
        print(f"PASS {fn.__name__}")
    print("All smoke tests passed.")
