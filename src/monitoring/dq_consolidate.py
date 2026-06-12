"""
DQ consolidator (run-health).

Discovers every `<name>_event_log` table in the monitoring schema and incrementally
MERGEs expectation metrics into the shared dq_expectation_history table.

Design:
- SINGLE WRITER. Exactly one scheduled job runs this, so concurrent-MERGE conflicts
  on dq_expectation_history cannot occur.
- AUTO-DISCOVERY. Adding a pipeline is zero-touch here: just name its event log
  `<pipeline>_event_log`. pipeline_name is derived by stripping the `_event_log` suffix.
- PER-PIPELINE WATERMARK. Each pipeline only reads event-log rows newer than its own
  last consolidated run, so one pipeline never advances another's incremental cursor.
- IDEMPOTENT. MERGE keys (pipeline_name, update_id, table_name, expectation) collapse
  per-micro-batch flow events into one row per run; re-running with no new events is a
  no-op.

Run-health counts are per pipeline RUN, not per delivery date. Per-date completeness
lives in the shared fact_data_quality table (built by the sibling dq_completeness.py task).

Usage (spark_python_task):
    dq_consolidate.py <catalog> <monitoring_schema>
"""

import sys

from pyspark.sql import SparkSession

EVENT_LOG_SUFFIX = "_event_log"

EXPECTATIONS_SCHEMA = (
    "array<struct<dataset:string, name:string, "
    "passed_records:bigint, failed_records:bigint>>"
)


def _history_table(catalog: str, schema: str) -> str:
    return f"`{catalog}`.`{schema}`.dq_expectation_history"


def create_history_table(spark: SparkSession, catalog: str, schema: str) -> None:
    spark.sql(
        f"""
        CREATE TABLE IF NOT EXISTS {_history_table(catalog, schema)} (
          pipeline_name    STRING,
          update_id        STRING,
          run_timestamp    TIMESTAMP,
          run_date         DATE,
          table_name       STRING,
          expectation      STRING,
          passed_records   BIGINT,
          failed_records   BIGINT,
          fail_pct         DOUBLE
        )
        """
    )


def discover_event_logs(spark: SparkSession, catalog: str, schema: str) -> list[str]:
    rows = spark.sql(
        f"""
        SELECT table_name
        FROM `{catalog}`.information_schema.tables
        WHERE table_schema = '{schema}'
        """
    ).collect()
    return sorted(r.table_name for r in rows if r.table_name.endswith(EVENT_LOG_SUFFIX))


def _union_sql(catalog: str, schema: str, tables: list[str]) -> str:
    parts = []
    for table in tables:
        pipeline_name = table[: -len(EVENT_LOG_SUFFIX)]
        parts.append(
            f"SELECT '{pipeline_name}' AS pipeline_name, origin, timestamp, "
            f"details, event_type FROM `{catalog}`.`{schema}`.`{table}`"
        )
    return "\nUNION ALL\n".join(parts)


def merge_sql(catalog: str, schema: str, tables: list[str]) -> str:
    hist = _history_table(catalog, schema)
    union = _union_sql(catalog, schema, tables)
    return f"""
    MERGE INTO {hist} t
    USING (
      WITH src AS (
        {union}
      ),
      wm AS (
        SELECT pipeline_name, max(run_timestamp) AS watermark
        FROM {hist}
        GROUP BY pipeline_name
      ),
      exploded AS (
        SELECT
          src.pipeline_name,
          src.origin.update_id AS update_id,
          src.timestamp        AS ts,
          explode(from_json(
            src.details:flow_progress:data_quality:expectations,
            '{EXPECTATIONS_SCHEMA}'
          )) AS e
        FROM src
        LEFT JOIN wm ON src.pipeline_name = wm.pipeline_name
        WHERE src.event_type = 'flow_progress'
          AND src.timestamp > coalesce(wm.watermark, TIMESTAMP '1900-01-01')
      )
      SELECT
        pipeline_name,
        update_id,
        max(ts)        AS run_timestamp,
        date(max(ts))  AS run_date,
        e.dataset      AS table_name,
        e.name         AS expectation,
        sum(e.passed_records) AS passed_records,
        sum(e.failed_records) AS failed_records,
        round(sum(e.failed_records) * 100.0 /
          nullif(sum(e.passed_records) + sum(e.failed_records), 0), 2) AS fail_pct
      FROM exploded
      GROUP BY pipeline_name, update_id, e.dataset, e.name
    ) s
    ON  t.pipeline_name = s.pipeline_name
    AND t.update_id     = s.update_id
    AND t.table_name    = s.table_name
    AND t.expectation   = s.expectation
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """


def main() -> None:
    catalog = sys.argv[1] if len(sys.argv) > 1 else "home-energy-optimizer-dev"
    monitoring_schema = sys.argv[2] if len(sys.argv) > 2 else "silver_layer"

    spark = SparkSession.builder.getOrCreate()
    create_history_table(spark, catalog, monitoring_schema)

    tables = discover_event_logs(spark, catalog, monitoring_schema)
    if not tables:
        print(f"No *{EVENT_LOG_SUFFIX} tables in {catalog}.{monitoring_schema}; nothing to do.")
        return

    print(f"Consolidating event logs: {tables}")
    spark.sql(merge_sql(catalog, monitoring_schema, tables))
    print(f"dq_expectation_history updated from {len(tables)} pipeline event log(s).")


if __name__ == "__main__":
    main()
