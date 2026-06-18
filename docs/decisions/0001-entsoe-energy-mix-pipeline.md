# ADR 0001 — ENTSO-E Energy Mix pipeline

Status: Accepted — 2026-06-18
Context owner: Piotr

## Decision

Add a new medallion pipeline that ingests **ENTSO-E Actual Generation per Production
Type** for the GB bidding zone and lands it as a cleaned, long-format Silver table.
It mirrors the existing `energy_prices` and `weather` pipelines so monitoring and
orchestration stay uniform.

## Context

The optimizer already has day-ahead prices and weather. Generation mix (how much each
fuel/technology produced per settlement period) is the missing signal for reasoning
about carbon intensity and when cheap renewable supply is abundant. The data comes from
the same ENTSO-E Transparency Platform API and the same `entsoe-api-token` secret already
used for prices, so the marginal cost of adding it is low.

## Decisions and rationale

Each row below is a branch we resolved during the design interview.

| Branch | Decision | Why |
| --- | --- | --- |
| Data source | Document **A75 — Actual Generation per Production Type**, `processType=A16` (Realised), GB zone `10YGB----------A` | The canonical "energy mix": actual MW per fuel/tech per period. Not forecast (A69), not the consumption-inclusive A73. |
| Table shape | **Long / tidy**: one row per `(delivery_start, production_type)` | No schema change when ENTSO-E adds a fuel type; matches the one-row-per-period style of Silver prices. |
| Code organization | **New domain** `src/energy_mix/` (ingestion + pipeline) | Distinct dataset with its own pipeline, tables, and event log. Mirrors how `weather` is separated from `energy_prices`. |
| Storage direction | **Generation only** — keep `in_Domain` TimeSeries, drop `out_Domain` (storage consumption) series | We want "what was generated". Consumption/charging is out of scope for the mix. |
| Fuel decoding | Keep raw **psrType code + decoded `production_type` name** (e.g. `B04` → "Fossil Gas"); no category column | Mirrors `WMO_CODES` in the weather ingest. Categories (renewable/fossil/...) can be layered in Gold later. |
| Naming | Files `entsoe_generation_ingest.py` / `entsoe_generation_pipeline.py`; tables `bronze_entsoe_actual_generation` & `silver_entsoe_actual_generation`; event log `entsoe_generation_event_log`; reuse the `raw_entsoe` Volume with subfolder `actual_generation/` | Consistent with prices/weather. Distinct event-log name keeps DQ auto-discovery correct. One Volume per provider. |
| Silver scope | Cleaned long table only; no mix-% computed | Keep Silver provider-faithful; defer `share_of_total_pct` to a future Gold layer. |
| Scheduling | Mirror prices: `ingest_offset_days` default `2353`, daily cron `13:30 UTC`, `PAUSED` | Same historical-replay-as-live model; one consistent batch window. |
| Data quality | Warn-first rollout. Bronze warn: `no_rescued_data`, `delivery_start NOT NULL`, `production_type_code NOT NULL`. Silver drop-candidates: `delivery_start` / `production_type_code` / `quantity_mw` NOT NULL. Silver warn: `quantity_mw >= 0`, `quantity_mw <= 50000` | All rules start at warn; promote drop-candidates to `expect_all_or_drop` after observing real failure counts. 50000 MW is a generous GB per-type plausibility ceiling. |

## Consequences

- Monitoring is zero-touch: `dq_consolidate.py` auto-discovers `entsoe_generation_event_log`.
- The A75 XML differs from A44 (prices): different namespace
  (`...451-6:generationloaddocument:3:0`), `quantity` instead of `price.amount`, no
  currency, `MktPSRType/psrType`, and possibly multiple `Period` elements / mixed
  resolutions (PT15M/PT30M/PT60M) per TimeSeries. The generation parser handles these
  generically rather than reusing the prices parser.
- Small helpers (`_parse_resolution`, `resolve_ingest_date`) are duplicated into the new
  ingest, following the existing prices/weather duplication pattern (each
  `spark_python_task` is a standalone file).
