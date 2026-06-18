"""
ENTSO-E Actual Generation per Production Type Ingestion

Downloads the actual-generation XML (document A75) from the ENTSO-E Transparency
Platform, parses each production type's points into long-format records (one row per
settlement period × production type), and writes CSV to a Unity Catalog Volume.

Usage (spark_python_task):
    entsoe_generation_ingest.py <catalog> <schema> [ingest_offset_days]

The ingest date is computed as today(UTC) minus ingest_offset_days so a fixed offset
simulates daily live runs over historical ENTSO-E data, mirroring the prices ingest.

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

# ENTSO-E psrType code → human-readable production type name. Mirrors WMO_CODES in the
# weather ingest so downstream stays provider-agnostic. Unknown codes fall back below.
PSR_TYPES: dict[str, str] = {
    "B01": "Biomass",
    "B02": "Fossil Brown coal/Lignite",
    "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas",
    "B05": "Fossil Hard coal",
    "B06": "Fossil Oil",
    "B07": "Fossil Oil shale",
    "B08": "Fossil Peat",
    "B09": "Geothermal",
    "B10": "Hydro Pumped Storage",
    "B11": "Hydro Run-of-river and poundage",
    "B12": "Hydro Water Reservoir",
    "B13": "Marine",
    "B14": "Nuclear",
    "B15": "Other renewable",
    "B16": "Solar",
    "B17": "Waste",
    "B18": "Wind Offshore",
    "B19": "Wind Onshore",
    "B20": "Other",
    "B25": "Energy storage",
}


# ---------------------------------------------------------------------------
# Step 1 — fetch raw XML
# ---------------------------------------------------------------------------


def build_query_params(token: str, date: datetime) -> dict:
    """Return API query parameters for the actual-generation document (A75).

    processType A16 = Realised. The delivery day runs 23:00 UTC (day-1) → 23:00 UTC (day),
    matching the prices ingest so generation and prices align on the same delivery dates.
    """
    start = (date - timedelta(days=1)).replace(hour=23, minute=0, second=0, microsecond=0)
    end = date.replace(hour=23, minute=0, second=0, microsecond=0)
    return {
        "securityToken": token,
        "documentType": "A75",
        "processType": "A16",
        "in_Domain": BIDDING_ZONE,
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

_NS = {"ns": "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"}


def _parse_resolution(resolution: str) -> timedelta:
    """Convert an ISO 8601 duration string (e.g. PT60M, PT30M, PT15M) to a timedelta."""
    match = re.fullmatch(r"PT(\d+)M", resolution)
    if not match:
        raise ValueError(f"Unsupported resolution: {resolution!r}")
    return timedelta(minutes=int(match.group(1)))


def parse_generation(xml_bytes: bytes, fetched_at: datetime) -> list[dict]:
    """Parse the ENTSO-E GL_MarketDocument (A75) into flat long-format records.

    Each record represents one production type over one settlement period and contains:
        delivery_start       – UTC datetime when the period begins
        delivery_end         – UTC datetime when the period ends
        production_type_code – ENTSO-E psrType code (e.g. "B04")
        production_type      – decoded human-readable name (e.g. "Fossil Gas")
        quantity_mw          – generated power in MW (average over the period)
        bidding_zone         – EIC code of the bidding zone (in_Domain)
        fetched_at           – UTC timestamp when the API call was made

    Generation-only: TimeSeries carrying an outBiddingZone_Domain (storage consumption /
    charging) are skipped. A TimeSeries may hold multiple Period elements with differing
    resolutions; each Period is decoded independently, and position-based gaps are handled
    per Point.
    """
    root = ET.fromstring(xml_bytes)
    records: list[dict] = []

    for ts in root.findall("ns:TimeSeries", _NS):
        # Skip consumption series (storage charging) — keep generation (in_Domain) only.
        if ts.find("ns:outBiddingZone_Domain.mRID", _NS) is not None:
            continue

        bidding_zone = ts.findtext("ns:inBiddingZone_Domain.mRID", namespaces=_NS)
        code = ts.findtext("ns:MktPSRType/ns:psrType", namespaces=_NS)
        production_type = PSR_TYPES.get(code, "Unknown")

        for period in ts.findall("ns:Period", _NS):
            start_str = period.findtext("ns:timeInterval/ns:start", namespaces=_NS)
            resolution_str = period.findtext("ns:resolution", namespaces=_NS)
            period_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            step = _parse_resolution(resolution_str)

            for point in period.findall("ns:Point", _NS):
                position = int(point.findtext("ns:position", namespaces=_NS))
                quantity = float(point.findtext("ns:quantity", namespaces=_NS))
                delivery_start = period_start + (position - 1) * step
                records.append(
                    {
                        "delivery_start": delivery_start,
                        "delivery_end": delivery_start + step,
                        "production_type_code": code,
                        "production_type": production_type,
                        "quantity_mw": quantity,
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
    catalog = sys.argv[1] if len(sys.argv) > 1 else "home-energy-optimizer-dev"
    schema = sys.argv[2] if len(sys.argv) > 2 else "landing_zone"
    offset_days = int(sys.argv[3]) if len(sys.argv) > 3 else 2353

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

    records = parse_generation(xml_bytes, fetched_at)
    print(f"Parsed {len(records)} generation records.")

    buf = io.StringIO()
    pd.DataFrame(records).to_csv(buf, index=False)

    csv_path = f"/Volumes/{catalog}/{schema}/raw_entsoe/actual_generation/{date_str}.csv"
    dbutils.fs.put(csv_path, buf.getvalue(), overwrite=True)
    print(f"Written CSV → {csv_path}")


if __name__ == "__main__":
    main()
