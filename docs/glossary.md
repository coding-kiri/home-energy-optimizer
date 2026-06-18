# Glossary

Domain terms used across the home-energy-optimizer pipelines.

## ENTSO-E / energy market

- **ENTSO-E Transparency Platform** — EU body's public API for electricity market and
  grid data. One API token (`entsoe-api-token` secret) covers prices and generation.
- **Energy mix** — the breakdown of electricity generation by fuel/technology over time.
- **Actual Generation per Production Type (A75)** — ENTSO-E document type giving actual
  generated power (MW) per production type per settlement period. The source for the mix.
- **processType A16 (Realised)** — query parameter marking "actual/realised" data, as
  opposed to forecast process types.
- **psrType** — ENTSO-E code for a production type / fuel (e.g. `B04` Fossil Gas, `B16`
  Solar, `B19` Wind Onshore). We store the raw code plus a decoded `production_type` name.
- **Bidding zone** — market area for which prices/generation are reported. GB is
  `10YGB----------A`.
- **in_Domain / out_Domain** — in A75, generation TimeSeries carry `inBiddingZone_Domain`;
  storage consumption (charging) carries `outBiddingZone_Domain`. We keep `in_Domain` only.
- **Settlement / delivery period** — the time slot a value applies to (`delivery_start` →
  `delivery_end`). GB generation is typically half-hourly.
- **resolution** — ISO-8601 duration of a period (`PT15M`, `PT30M`, `PT60M`). Period
  start plus `(position - 1) * resolution` gives each point's `delivery_start`.
- **MW vs MWh** — generation quantity is power in **MW** (average over the period); prices
  are GBP per **MWh**.

## Pipeline / platform

- **Medallion (Bronze → Silver)** — Bronze holds raw landed data; Silver holds cleaned,
  deduped, typed data. No Gold layer yet.
- **Auto Loader (`cloudFiles`)** — incremental file ingestion into Bronze from a Volume.
- **`_rescued_data`** — Auto Loader column capturing values that didn't fit the schema;
  a non-null value flags a parse/shape problem.
- **Expectation (warn / drop)** — a DLT data-quality rule. Warn records failures without
  removing rows; drop (`expect_all_or_drop`) removes failing rows. Rules start warn-first.
- **Event log** — per-pipeline DLT log table `<pipeline>_event_log`; `dq_consolidate.py`
  auto-discovers these and merges expectation metrics into `dq_expectation_history`.
- **`ingest_offset_days`** — fetch date = today(UTC) − offset; a fixed offset replays
  historical data as if it were arriving live.
- **`ingested_at`** — Silver timestamp propagated from the ingest's `fetched_at`; used as
  the dedup tiebreaker (latest fetch wins).
