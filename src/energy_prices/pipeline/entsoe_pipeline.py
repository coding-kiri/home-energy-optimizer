"""
ENTSO-E day-ahead prices — Lakeflow Declarative Pipeline (Bronze → Silver + DQ).

Acceptance testing (manual, after first clean run): drop a CSV with bad rows into the
Volume landing path and re-trigger the job. Expect warn counters in the event log /
dq_expectation_history.
"""

import dlt
from pyspark.sql import functions as F
from pyspark.sql.window import Window

# ---------------------------------------------------------------------------
# Pipeline configuration (set in resources/energy_prices/energy_prices.pipeline.yml)
# ---------------------------------------------------------------------------

catalog = spark.conf.get("catalog")
landing_schema = spark.conf.get("landing_schema")
silver_schema = spark.conf.get("silver_schema")

VOLUME_PATH = f"/Volumes/{catalog}/{landing_schema}/raw_entsoe/day_ahead_prices/"

SCHEMA_HINTS = (
    "delivery_start TIMESTAMP, delivery_end TIMESTAMP, "
    "price_gbp_per_mwh DOUBLE, currency STRING, bidding_zone STRING, "
    "fetched_at TIMESTAMP"
)

# ---------------------------------------------------------------------------
# Expectation rule dicts — single source of truth; promote severities after observation
# ---------------------------------------------------------------------------

bronze_entsoe_warn_rules = {
    "no_rescued_data": "_rescued_data IS NULL",
    "valid_delivery_start": "delivery_start IS NOT NULL",
    "valid_bidding_zone": "bidding_zone IS NOT NULL",
}

# Structural rules — promote to @dlt.expect_all_or_drop after observing failure counts.
silver_entsoe_drop_rules = {
    "valid_delivery_start": "delivery_start IS NOT NULL",
    "valid_bidding_zone": "bidding_zone IS NOT NULL",
    "valid_price": "price_gbp_per_mwh IS NOT NULL",
}

silver_entsoe_warn_rules = {
    "non_negative_price": "price_gbp_per_mwh >= 0",
    "plausible_price": "price_gbp_per_mwh <= 500",
}

# All rules start at warn severity. Merge dicts for a single warn decorator;
# split decorators when promoting drop_rules to expect_all_or_drop.
_silver_all_rules = {**silver_entsoe_drop_rules, **silver_entsoe_warn_rules}


# ---------------------------------------------------------------------------
# Bronze — Auto Loader from S3-backed Volume, warn-only expectations
# ---------------------------------------------------------------------------


@dlt.table(name="bronze_entsoe_day_ahead_prices", comment="Raw ENTSO-E day-ahead prices from landing CSV")
@dlt.expect_all(bronze_entsoe_warn_rules)
def bronze_entsoe_day_ahead_prices():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaHints", SCHEMA_HINTS)
        .option("header", "true")
        .option("rescuedDataColumn", "_rescued_data")
        .load(VOLUME_PATH)
    )


# ---------------------------------------------------------------------------
# Silver — deduped hourly slots, ingested_at propagated from fetched_at.
# Batch materialized view (not streaming): row_number dedup is not supported on
# streaming DataFrames, and Bronze is tiny (~24 rows/day) so recompute is trivial.
# ---------------------------------------------------------------------------


@dlt.table(
    name=f"{silver_schema}.silver_entsoe_day_ahead_prices",
    comment="Cleaned ENTSO-E day-ahead prices (one row per bidding zone × delivery period)",
)
@dlt.expect_all(_silver_all_rules)
def silver_entsoe_day_ahead_prices():
    bronze = dlt.read("bronze_entsoe_day_ahead_prices")
    dedup_window = Window.partitionBy("bidding_zone", "delivery_start").orderBy(
        F.col("fetched_at").desc()
    )
    return (
        bronze.withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
        .select(
            "delivery_start",
            "delivery_end",
            "price_gbp_per_mwh",
            "currency",
            "bidding_zone",
            F.col("fetched_at").alias("ingested_at"),
        )
    )
