"""
ENTSO-E Day-Ahead Price Ingestion

Downloads the day-ahead price XML from the ENTSO-E Transparency Platform,
parses each hourly Point into records, and writes CSV to a Unity Catalog Volume.

Usage (spark_python_task):
    entsoe_ingest.py <catalog> <schema> [ingest_offset_days]

The ingest date is computed as today(UTC) minus ingest_offset_days so a fixed offset
simulates daily live runs over historical ENTSO-E data.

Secret required:
    scope: energy-optimizer
    key:   entsoe-api-token
"""

import io
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from pyspark.dbutils import DBUtils
from pyspark.sql import SparkSession

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ENTSOE_API_URL = "https://web-api.tp.entsoe.eu/api"
BIDDING_ZONE = "10YGB----------A"
SECRET_SCOPE = "energy-optimizer"
SECRET_KEY = "entsoe-api-token"


# ---------------------------------------------------------------------------
# Step 1 — fetch raw XML
# ---------------------------------------------------------------------------


def build_query_params(token: str, date: datetime) -> dict:
    """Return API query parameters for the day-ahead price document."""
    # ENTSO-E delivery day runs 23:00 UTC (day-1) → 23:00 UTC (day)
    start = (date - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    end = date.replace(hour=23, minute=0, second=0, microsecond=0)
    return {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": BIDDING_ZONE,
        "out_Domain": BIDDING_ZONE,
        "periodStart": start.strftime("%Y%m%d%H%M"),
        "periodEnd": end.strftime("%Y%m%d%H%M"),
    }


def fetch_xml(params: dict) -> bytes:
    """Call the ENTSO-E API and return raw XML bytes."""
    response = requests.get(ENTSOE_API_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.content


# ---------------------------------------------------------------------------
# Step 2 — parse
# ---------------------------------------------------------------------------

_NS = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}


def _parse_resolution(resolution: str) -> timedelta:
    """Convert an ISO 8601 duration string (e.g. PT60M, PT30M) to a timedelta."""
    match = re.fullmatch(r"PT(\d+)M", resolution)
    if not match:
        raise ValueError(f"Unsupported resolution: {resolution!r}")
    return timedelta(minutes=int(match.group(1)))


def parse_prices(xml_bytes: bytes, fetched_at: datetime) -> list[dict]:
    """Parse the ENTSO-E Publication_MarketDocument XML into a flat list of price records.

    Each record represents one settlement period (typically 1 hour) and contains:
        delivery_start   – UTC datetime when the period begins
        delivery_end     – UTC datetime when the period ends
        price_gbp_per_mwh – day-ahead price in GBP/MWh
        currency         – currency code from the document (e.g. "GBP")
        bidding_zone     – EIC code of the bidding zone
        fetched_at       – UTC timestamp when the API call was made

    Points are position-based (1-indexed, offset from Period start), so gaps in
    position numbering (which occasionally appear in real ENTSO-E responses) are
    handled correctly — each Point is computed independently.
    """
    root = ET.fromstring(xml_bytes)
    records: list[dict] = []

    for ts in root.findall("ns:TimeSeries", _NS):
        currency = ts.findtext("ns:currency_Unit.name", namespaces=_NS)
        bidding_zone = ts.findtext("ns:in_Domain.mRID", namespaces=_NS)

        period = ts.find("ns:Period", _NS)
        if period is None:
            continue

        start_str = period.findtext("ns:timeInterval/ns:start", namespaces=_NS)
        resolution_str = period.findtext("ns:resolution", namespaces=_NS)
        period_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        step = _parse_resolution(resolution_str)

        for point in period.findall("ns:Point", _NS):
            position = int(point.findtext("ns:position", namespaces=_NS))
            price = float(point.findtext("ns:price.amount", namespaces=_NS))
            delivery_start = period_start + (position - 1) * step
            records.append(
                {
                    "delivery_start": delivery_start,
                    "delivery_end": delivery_start + step,
                    "price_gbp_per_mwh": price,
                    "currency": currency,
                    "bidding_zone": bidding_zone,
                    "fetched_at": fetched_at,
                }
            )

    return records


def resolve_ingest_date(offset_days: int) -> date:
    """Return the delivery date to fetch: today(UTC) minus offset_days."""
    return datetime.now(tz=timezone.utc).date() - timedelta(days=offset_days)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    catalog = sys.argv[1] if len(sys.argv) > 1 else "home-energy-optimizer"
    schema = sys.argv[2] if len(sys.argv) > 2 else "landing_zone"
    offset_days = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    spark = SparkSession.builder.getOrCreate()
    dbutils = DBUtils(spark)
    token = dbutils.secrets.get(scope=SECRET_SCOPE, key=SECRET_KEY)

    ingest_date = resolve_ingest_date(offset_days)
    date_str = ingest_date.isoformat()
    fetch_datetime = datetime.combine(ingest_date, datetime.min.time())

    params = build_query_params(token, fetch_datetime)
    print(
        f"Ingest date={date_str} (offset_days={offset_days})  "
        f"periodStart={params['periodStart']}  periodEnd={params['periodEnd']}"
    )

    fetched_at = datetime.now(tz=timezone.utc)
    xml_bytes = fetch_xml(params)

    records = parse_prices(xml_bytes, fetched_at)
    print(f"Parsed {len(records)} price records.")

    buf = io.StringIO()
    pd.DataFrame(records).to_csv(buf, index=False)

    csv_path = f"/Volumes/{catalog}/{schema}/raw_entsoe/day_ahead_prices/{date_str}.csv"
    dbutils.fs.put(csv_path, buf.getvalue(), overwrite=True)
    print(f"Written CSV → {csv_path}")


if __name__ == "__main__":
    main()
