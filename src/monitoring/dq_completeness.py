"""
DQ data-health consolidator (completeness).

Computes per-day completeness for every registered dataset and MERGEs the result into
ONE shared table: `<monitoring_schema>.fact_data_quality`.

Why this lives in the monitoring job (not inside each pipeline):
- A Lakeflow pipeline can only write tables it owns, so per-pipeline DQ facts meant one
  `fact_data_quality_*` table per pipeline — table sprawl, and inconsistent with how
  run-health already consolidates every `*_event_log` into one `dq_expectation_history`.
- This module runs as a plain `spark_python_task`, so it reads each silver table directly
  and writes a single shared fact. Adding a pipeline = add one CompletenessSpec to REGISTRY.

The completeness logic is schema-agnostic: each CompletenessSpec describes its own grain,
period column, and expected count, so datasets with different schemas all map to the same
standard output schema.

Standard output schema (`fact_data_quality`):
    dataset           STRING     short pipeline/domain key (e.g. "entsoe")
    grain             STRING     human label of the entity grain (e.g. "bidding_zone")
    entity_key        STRING     entity value(s) at that grain, joined with " | "
    period_date       DATE       calendar date the readings belong to
    expected_count    BIGINT     expected number of readings for the period
    actual_count      BIGINT     observed number of readings
    null_value_count  BIGINT     rows whose measured value is NULL (NULL if not tracked)
    completeness_pct  DOUBLE     actual / expected * 100, rounded to 2 dp
    last_ingested_at  TIMESTAMP  latest ingestion time in the group (NULL if not tracked)
    computed_at       TIMESTAMP  when this consolidation run wrote the row

Usage (spark_python_task):
    dq_completeness.py <catalog> <silver_schema> <monitoring_schema>
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from functools import reduce

from pyspark.sql import Column, DataFrame, SparkSession
from pyspark.sql import functions as F

ENTITY_KEY_SEPARATOR = " | "

FACT_TABLE = "fact_data_quality"

SRC_VIEW = "_dq_completeness_src"

OUTPUT_COLUMNS = [
    "dataset",
    "grain",
    "entity_key",
    "period_date",
    "expected_count",
    "actual_count",
    "null_value_count",
    "completeness_pct",
    "last_ingested_at",
]


@dataclass(frozen=True)
class CompletenessSpec:
    """Describes how to derive completeness from one dataset's silver readings.

    Args:
        dataset: Short identifier for the pipeline/domain (e.g. "entsoe"). Stored in the
            `dataset` column so multiple datasets share one fact table.
        source_table: Silver table name (within the silver schema) to read readings from.
        entity_cols: Columns that define the completeness grain (e.g. ["bidding_zone"],
            or ["meter_id"] for smart meters). Combined into a single `entity_key`.
        period_col: Timestamp/date column rolled up to a calendar date as `period_date`.
        expected_count: Expected readings per (entity, period). An int for a fixed grain
            (e.g. 24 hourly slots) or a Column expression for variable grains (e.g. a
            DST-aware CASE on `period_date` yielding 23/24/25).
        value_col: Optional measure column; rows where it is NULL are counted into
            `null_value_count`. Left NULL when not provided.
        ingested_at_col: Optional ingestion-time column; its max becomes
            `last_ingested_at`. Left NULL when not provided.
        grain: Optional human label for the grain. Defaults to the joined entity columns.
    """

    dataset: str
    source_table: str
    entity_cols: list[str]
    period_col: str
    expected_count: int | Column
    value_col: str | None = None
    ingested_at_col: str | None = None
    grain: str | None = None

    def __post_init__(self) -> None:
        if not self.entity_cols:
            raise ValueError("CompletenessSpec.entity_cols must not be empty")


# ---------------------------------------------------------------------------
# Dataset registry — add one CompletenessSpec per pipeline. This is the single
# touch-point when onboarding a new pipeline's data-health.
# ---------------------------------------------------------------------------

REGISTRY: list[CompletenessSpec] = [
    CompletenessSpec(
        dataset="entsoe",
        source_table="silver_entsoe_day_ahead_prices",
        entity_cols=["bidding_zone"],
        period_col="delivery_start",
        expected_count=24,
        value_col="price_gbp_per_mwh",
        ingested_at_col="ingested_at",
        grain="bidding_zone",
    ),
    CompletenessSpec(
        dataset="openmeteo",
        source_table="silver_openmeteo_hourly_weather",
        entity_cols=["location"],
        period_col="observation_time",
        expected_count=24,
        value_col="temp_celsius",
        ingested_at_col="ingested_at",
        grain="location",
    ),
]


def build_completeness(readings: DataFrame, spec: CompletenessSpec) -> DataFrame:
    """Build a standard-schema completeness DataFrame from a dataset's silver readings."""
    grain = spec.grain or ", ".join(spec.entity_cols)
    entity_key = F.concat_ws(
        ENTITY_KEY_SEPARATOR, *[F.col(c).cast("string") for c in spec.entity_cols]
    ).alias("entity_key")
    period_date = F.to_date(F.col(spec.period_col)).alias("period_date")

    if spec.value_col is not None:
        null_value_count = (
            F.sum(F.when(F.col(spec.value_col).isNull(), F.lit(1)).otherwise(F.lit(0)))
            .cast("bigint")
            .alias("null_value_count")
        )
    else:
        null_value_count = F.lit(None).cast("bigint").alias("null_value_count")

    if spec.ingested_at_col is not None:
        last_ingested_at = F.max(F.col(spec.ingested_at_col)).alias("last_ingested_at")
    else:
        last_ingested_at = F.lit(None).cast("timestamp").alias("last_ingested_at")

    expected = spec.expected_count
    expected_count = (
        expected if isinstance(expected, Column) else F.lit(expected)
    ).cast("bigint")

    return (
        readings.groupBy(entity_key, period_date)
        .agg(
            F.count(F.lit(1)).cast("bigint").alias("actual_count"),
            null_value_count,
            last_ingested_at,
        )
        .withColumn("dataset", F.lit(spec.dataset))
        .withColumn("grain", F.lit(grain))
        .withColumn("expected_count", expected_count)
        .withColumn(
            "completeness_pct",
            F.round(F.col("actual_count") / F.col("expected_count") * 100, 2),
        )
        .select(*OUTPUT_COLUMNS)
    )


def _fact_table(catalog: str, schema: str) -> str:
    return f"`{catalog}`.`{schema}`.{FACT_TABLE}"


def create_fact_table(spark: SparkSession, catalog: str, schema: str) -> None:
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_fact_table(catalog, schema)} (
          dataset           STRING,
          grain             STRING,
          entity_key        STRING,
          period_date       DATE,
          expected_count    BIGINT,
          actual_count      BIGINT,
          null_value_count  BIGINT,
          completeness_pct  DOUBLE,
          last_ingested_at  TIMESTAMP,
          computed_at       TIMESTAMP
        )
        """
    )


def compute_all(spark: SparkSession, catalog: str, silver_schema: str) -> DataFrame | None:
    """Read every registered silver table and union the standard completeness rows."""
    frames: list[DataFrame] = []
    for spec in REGISTRY:
        readings = spark.sql(
            f"SELECT * FROM `{catalog}`.`{silver_schema}`.`{spec.source_table}`"
        )
        frames.append(build_completeness(readings, spec))
    if not frames:
        return None
    unioned = reduce(lambda a, b: a.unionByName(b), frames)
    return unioned.withColumn("computed_at", F.current_timestamp())


def merge_fact(spark: SparkSession, catalog: str, monitoring_schema: str, df: DataFrame) -> None:
    """Idempotent upsert keyed on (dataset, entity_key, period_date)."""
    df.createOrReplaceTempView(SRC_VIEW)
    spark.sql(
        f"""
        MERGE INTO {_fact_table(catalog, monitoring_schema)} t
        USING {SRC_VIEW} s
        ON  t.dataset     = s.dataset
        AND t.entity_key  = s.entity_key
        AND t.period_date = s.period_date
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
        """
    )


def main() -> None:
    catalog = sys.argv[1] if len(sys.argv) > 1 else "home-energy-optimizer-dev"
    silver_schema = sys.argv[2] if len(sys.argv) > 2 else "silver_layer"
    monitoring_schema = sys.argv[3] if len(sys.argv) > 3 else "silver_layer"

    spark = SparkSession.builder.getOrCreate()
    create_fact_table(spark, catalog, monitoring_schema)

    if not REGISTRY:
        print("CompletenessSpec REGISTRY is empty; nothing to do.")
        return

    df = compute_all(spark, catalog, silver_schema)
    merge_fact(spark, catalog, monitoring_schema, df)
    datasets = ", ".join(spec.dataset for spec in REGISTRY)
    print(f"fact_data_quality updated for dataset(s): {datasets}.")


if __name__ == "__main__":
    main()
