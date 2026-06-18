"""
Open-Meteo hourly weather — Lakeflow Declarative Pipeline (Bronze → Silver + DQ).

Mirrors the ENTSO-E pipeline: Auto Loader ingests landing CSV into a Bronze table,
then a batch materialized view dedupes to one row per location × observation hour.
"""

import dlt
from pyspark.sql import functions as F
from pyspark.sql.window import Window

# ---------------------------------------------------------------------------
# Pipeline configuration (set in resources/weather/weather_pipeline.yml)
# ---------------------------------------------------------------------------

catalog = spark.conf.get("catalog")
landing_schema = spark.conf.get("landing_schema")
silver_schema = spark.conf.get("silver_schema")

VOLUME_PATH = f"/Volumes/{catalog}/{landing_schema}/raw_openmeteo/london/"

SCHEMA_HINTS = (
    "observation_time TIMESTAMP, location STRING, latitude DOUBLE, longitude DOUBLE, "
    "temp_celsius DOUBLE, feels_like_celsius DOUBLE, humidity_pct INT, clouds_pct INT, "
    "wind_speed_ms DOUBLE, weather_main STRING, weather_description STRING, "
    "fetched_at TIMESTAMP"
)

# ---------------------------------------------------------------------------
# Expectation rule dicts — single source of truth; promote severities after observation
# ---------------------------------------------------------------------------

bronze_openmeteo_warn_rules = {
    "no_rescued_data": "_rescued_data IS NULL",
    "valid_observation_time": "observation_time IS NOT NULL",
    "valid_location": "location IS NOT NULL",
}

# Structural rules — promote to @dlt.expect_all_or_drop after observing failure counts.
silver_openmeteo_drop_rules = {
    "valid_observation_time": "observation_time IS NOT NULL",
    "valid_location": "location IS NOT NULL",
    "valid_temp": "temp_celsius IS NOT NULL",
}

silver_openmeteo_warn_rules = {
    "plausible_temp": "temp_celsius BETWEEN -60 AND 60",
    "valid_humidity": "humidity_pct BETWEEN 0 AND 100",
    "non_negative_wind": "wind_speed_ms >= 0",
}

# E1 rollout: all rules start at warn severity. Merge dicts for a single warn decorator;
# split decorators when promoting drop_rules to expect_all_or_drop.
_silver_all_rules = {**silver_openmeteo_drop_rules, **silver_openmeteo_warn_rules}


# ---------------------------------------------------------------------------
# Bronze — Auto Loader from S3-backed Volume, warn-only expectations
# ---------------------------------------------------------------------------


@dlt.table(name="bronze_openmeteo_hourly_weather", comment="Raw Open-Meteo hourly weather from landing CSV")
@dlt.expect_all(bronze_openmeteo_warn_rules)
def bronze_openmeteo_hourly_weather():
    return (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "csv")
        .option("cloudFiles.schemaHints", SCHEMA_HINTS)
        .option("header", "true")
        .option("rescuedDataColumn", "_rescued_data")
        .load(VOLUME_PATH)
    )


# ---------------------------------------------------------------------------
# Silver — deduped hourly observations, ingested_at propagated from fetched_at.
# Batch materialized view (not streaming): row_number dedup is not supported on
# streaming DataFrames, and Bronze is tiny (~24 rows/day) so recompute is trivial.
# ---------------------------------------------------------------------------


@dlt.table(
    name=f"{silver_schema}.silver_openmeteo_hourly_weather",
    comment="Cleaned Open-Meteo hourly weather (one row per location × observation hour)",
)
@dlt.expect_all(_silver_all_rules)
def silver_openmeteo_hourly_weather():
    bronze = dlt.read("bronze_openmeteo_hourly_weather")
    dedup_window = Window.partitionBy("location", "observation_time").orderBy(
        F.col("fetched_at").desc()
    )
    return (
        bronze.withColumn("_row_num", F.row_number().over(dedup_window))
        .filter(F.col("_row_num") == 1)
        .drop("_row_num")
        .select(
            "observation_time",
            "location",
            "latitude",
            "longitude",
            "temp_celsius",
            "feels_like_celsius",
            "humidity_pct",
            "clouds_pct",
            "wind_speed_ms",
            "weather_main",
            "weather_description",
            F.col("fetched_at").alias("ingested_at"),
        )
    )
