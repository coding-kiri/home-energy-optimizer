"""Smart meter replay producer — main loop.

    read checkpoint  ->  for each half-hourly wave after it:
        publish one message per household (QoS 1, acked)
        write the wave timestamp back to the checkpoint
        sleep 1800 / REPLAY_SPEED_FACTOR seconds

The checkpoint advances only after every message in the wave is acked, so a
crash mid-wave replays that wave in full on restart. Downstream this is safe:
Firehose duplicates in raw are bounded to one wave, and the serving layer
upserts on (household, timestamp).
"""

from __future__ import annotations

import logging
import math
import signal
import sys
import time

from checkpoint import build_checkpoint, format_ts, parse_ts
from config import Config
from data_source import WindowedReplaySource
from mqtt_client import MqttPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("replay")

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    log.info("Received signal %s — finishing current wave, then stopping.", signum)
    _shutdown = True


def wave_sleep_seconds(replay_speed_factor: float) -> float:
    return 1800.0 / replay_speed_factor


def build_payload(row) -> dict:
    kwh = row.consumption_kwh
    return {
        "household_id": row.household_id,
        "reading_timestamp": format_ts(row.reading_timestamp.to_pydatetime()),
        "consumption_kwh": None if kwh is None or math.isnan(kwh) else round(float(kwh), 4),
    }


def main() -> int:
    cfg = Config.from_env()
    signal.signal(signal.SIGTERM, _handle_signal)  # Fargate stop sends SIGTERM
    signal.signal(signal.SIGINT, _handle_signal)

    checkpoint = build_checkpoint(
        cfg.checkpoint_param, cfg.checkpoint_file, cfg.aws_region
    )
    resume_after = checkpoint.read()
    if resume_after:
        log.info("Resuming after checkpoint %s", format_ts(resume_after))
    else:
        # Start one second before START_DATE so the wave AT start_date is included.
        resume_after = parse_ts(cfg.start_date)
        log.info("No checkpoint — starting from START_DATE %s", cfg.start_date)
        import datetime

        resume_after -= datetime.timedelta(seconds=1)

    source = WindowedReplaySource(
        cfg.source_uri, cfg.window_days, cfg.household_limit
    )
    publisher = MqttPublisher(cfg)
    publisher.connect()

    sleep_s = wave_sleep_seconds(cfg.replay_speed_factor)
    log.info(
        "Replay speed factor %.1f -> %.2fs between waves",
        cfg.replay_speed_factor,
        sleep_s,
    )

    waves = 0
    messages = 0
    try:
        for wave_ts, rows in source.waves(resume_after):
            published = 0
            for row in rows.itertuples(index=False):
                publisher.publish(build_payload(row))
                published += 1

            checkpoint.write(wave_ts)
            waves += 1
            messages += published
            log.info(
                "Wave %s: %d messages (total %d waves / %d messages)",
                format_ts(wave_ts),
                published,
                waves,
                messages,
            )

            if _shutdown:
                break
            time.sleep(sleep_s)
    finally:
        publisher.close()

    log.info("Stopped cleanly after %d waves / %d messages.", waves, messages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
