"""
ENTSO-E actual generation per production type — Lakeflow Declarative Pipeline (Bronze → Silver + DQ).

Acceptance testing (manual, after first clean run): drop a CSV with bad rows into the
Volume landing path and re-trigger the job. Expect warn counters in the event log /
dq_expectation_history.
"""

import dlt
from pyspark.sql import functions as F
from pyspark.sql.window import Window

# ---------------------------------------------------------------------------
# Pipeline configuration (set in resources/energy_mix/energy_mix_pipeline.yml)
# ---------------------------------------------------------------------------

catalog = spark.conf.get("catalog")
landing_schema = spark.conf.get("landing_schema")
silver_schema = spark.conf.get("silver_schema")

VOLUME_PATH = f"/Volumes/{catalog}/{landing_schema}/raw_entsoe/actual_generation/"

SCHEMA_HINTS = (
    "delivery_start TIMESTAMP, delivery_end TIMESTAMP, "
    "production_type_code STRING, production_type STRING, "
    "quantity_mw DOUBLE, bidding_zone STRING, "
    "fetched_at TIMESTAMP"
)

# ---------------------------------------------------------------------------
# Expectation rule dicts — single source of truth; promote severities after observation
# ---------------------------------------------------------------------------

bronze_entsoe_generation_warn_rules = {
    "no_rescued_data": "_rescued_data IS NULL",
    "valid_delivery_start": "delivery_start IS NOT NULL",
    "valid_production_type_code": "production_type_code IS NOT NULL",
}

# Structural rules — promote to @dlt.expect_all_or_drop after observing failure counts.
silver_entsoe_generation_drop_rules = {
    "valid_delivery_start": "delivery_start IS NOT NULL",
    "valid_production_type_code": "production_type_code IS NOT NULL",
    "valid_quantity": "quantity_mw IS NOT NULL",
}

silver_entsoe_generation_warn_rules = {
    "non_negative_quantity": "quantity_mw >= 0",
    "plausible_quantity": "quantity_mw <= 50000",
}

# All rules start at warn severity. Merge dicts for a single warn decorator;
# split decorators when promoting drop_rules to expect_all_or_drop.
_silver_all_rules = {**silver_entsoe_generation_drop_rules, **silver_entsoe_generation_warn_rules}


# ---------------------------------------------------------------------------
# Bronze — Auto Loader from S3-backed Volume, warn-only expectations
# ---------------------------------------------------------------------------


@dlt.table(
    name="bronze_entsoe_actual_generation",
    comment="Raw ENTSO-E actual generation per production type from landing CSV",
)
@dlt.expect_all(bronze_entsoe_generation_warn_rules)
def bronze_entsoe_actual_generation():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaHints", SCHEMA_HINTS)
        .option("header", "true")
        .option("rescuedDataColumn", "_rescued_data")
        .load(VOLUME_PATH)
    )


# ---------------------------------------------------------------------------
# Silver — deduped long-format rows, ingested_at propagated from fetched_at.
# Batch materialized view (not streaming): row_number dedup is not supported on
# streaming DataFrames, and Bronze is small (~hundreds of rows/day) so recompute is trivial.
# ---------------------------------------------------------------------------


@dlt.table(
    name=f"{silver_schema}.silver_entsoe_actual_generation",
    comment="Cleaned ENTSO-E actual generation (one row per bidding zone × production type × period)",
)
@dlt.expect_all(_silver_all_rules)
def silver_entsoe_actual_generation():
    bronze = dlt.read("bronze_entsoe_actual_generation")
    dedup_window = Window.partitionBy(
        "bidding_zone", "production_type_code", "delivery_start"
    ).orderBy(F.col("fetched_at").desc())
    return (
        bronze.withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
        .select(
            "delivery_start",
            "delivery_end",
            "production_type_code",
            "production_type",
            "quantity_mw",
            "bidding_zone",
            F.col("fetched_at").alias("ingested_at"),
        )
    )
