"""
Open-Meteo Historical Hourly Weather Ingestion

Fetches historical hourly weather for a single GB location (London) from the
Open-Meteo Historical Weather API (ERA5 reanalysis), parses each hour into a
record, and writes CSV to a Unity Catalog Volume.

Usage (spark_python_task):
    openmeteo_ingest.py <catalog> <schema> [ingest_offset_days]

The ingest date is computed as today(UTC) minus ingest_offset_days, mirroring the
ENTSO-E ingestion so weather and prices land on the same delivery dates. Unlike a
per-timestamp API, the archive endpoint returns the whole UTC day (24 hourly values
as parallel arrays) in a single call.

Open-Meteo requires no API key for non-commercial use, so no secret is needed.
Note: ERA5 has a ~5 day delay, so very recent dates may return null values.
"""

import io
import sys
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
from pyspark.dbutils import DBUtils
from pyspark.sql import SparkSession

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OPENMETEO_API_URL = "https://archive-api.open-meteo.com/v1/archive"
LOCATION_NAME = "london"
LATITUDE = 51.5074
LONGITUDE = -0.1278

# Hourly variables requested from the archive API (order is irrelevant; each comes
# back as its own parallel array aligned with the "time" array).
HOURLY_VARIABLES = [
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "cloud_cover",
    "wind_speed_10m",
    "weather_code",
]

# WMO weather interpretation codes → (main group, description). Mirrors the grouping
# style of OpenWeather's weather.main/description so downstream stays provider-agnostic.
WMO_CODES: dict[int, tuple[str, str]] = {
    0: ("Clear", "clear sky"),
    1: ("Clouds", "mainly clear"),
    2: ("Clouds", "partly cloudy"),
    3: ("Clouds", "overcast"),
    45: ("Fog", "fog"),
    48: ("Fog", "depositing rime fog"),
    51: ("Drizzle", "light drizzle"),
    53: ("Drizzle", "moderate drizzle"),
    55: ("Drizzle", "dense drizzle"),
    56: ("Drizzle", "light freezing drizzle"),
    57: ("Drizzle", "dense freezing drizzle"),
    61: ("Rain", "slight rain"),
    63: ("Rain", "moderate rain"),
    65: ("Rain", "heavy rain"),
    66: ("Rain", "light freezing rain"),
    67: ("Rain", "heavy freezing rain"),
    71: ("Snow", "slight snow fall"),
    73: ("Snow", "moderate snow fall"),
    75: ("Snow", "heavy snow fall"),
    77: ("Snow", "snow grains"),
    80: ("Rain", "slight rain showers"),
    81: ("Rain", "moderate rain showers"),
    82: ("Rain", "violent rain showers"),
    85: ("Snow", "slight snow showers"),
    86: ("Snow", "heavy snow showers"),
    95: ("Thunderstorm", "thunderstorm"),
    96: ("Thunderstorm", "thunderstorm with slight hail"),
    99: ("Thunderstorm", "thunderstorm with heavy hail"),
}


# ---------------------------------------------------------------------------
# Step 1 — fetch raw JSON (whole UTC day in one call)
# ---------------------------------------------------------------------------


def build_query_params(ingest_date: date) -> dict:
    """Return archive API query parameters for one calendar day of hourly weather.

    timezone=GMT keeps timestamps in UTC; wind_speed_unit=ms and the default
    temperature_unit (celsius) give m/s and °C respectively.
    """
    date_str = ingest_date.isoformat()
    return {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": ",".join(HOURLY_VARIABLES),
        "wind_speed_unit": "ms",
        "timezone": "GMT",
    }


def fetch_weather(params: dict) -> dict:
    """Call the Open-Meteo archive API and return the parsed JSON payload."""
    response = requests.get(OPENMETEO_API_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(f"Open-Meteo API error: {payload.get('reason')}")
    return payload


# ---------------------------------------------------------------------------
# Step 2 — parse
# ---------------------------------------------------------------------------


def parse_weather(payload: dict, fetched_at: datetime) -> list[dict]:
    """Parse an Open-Meteo archive payload into flat hourly weather records.

    The payload carries each variable as a parallel array under "hourly", aligned
    with the "hourly.time" array. Each record represents one hourly observation and
    contains:
        observation_time     – UTC datetime of the observation
        location             – short location identifier (e.g. "london")
        latitude / longitude – grid-cell coordinates the reading was taken for
        temp_celsius         – air temperature at 2 m (°C)
        feels_like_celsius   – apparent temperature (°C)
        humidity_pct         – relative humidity at 2 m (%)
        clouds_pct           – total cloud cover (%)
        wind_speed_ms        – wind speed at 10 m (m/s)
        weather_main         – short weather group derived from the WMO code
        weather_description  – detailed description derived from the WMO code
        fetched_at           – UTC timestamp when the API call was made
    """
    latitude = payload.get("latitude")
    longitude = payload.get("longitude")
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []

    records: list[dict] = []
    for i, time_str in enumerate(times):
        code = hourly.get("weather_code", [None] * len(times))[i]
        main, description = WMO_CODES.get(code, ("Unknown", "unknown"))
        records.append(
            {
                "observation_time": datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc),
                "location": LOCATION_NAME,
                "latitude": latitude,
                "longitude": longitude,
                "temp_celsius": hourly.get("temperature_2m", [None] * len(times))[i],
                "feels_like_celsius": hourly.get("apparent_temperature", [None] * len(times))[i],
                "humidity_pct": hourly.get("relative_humidity_2m", [None] * len(times))[i],
                "clouds_pct": hourly.get("cloud_cover", [None] * len(times))[i],
                "wind_speed_ms": hourly.get("wind_speed_10m", [None] * len(times))[i],
                "weather_main": main,
                "weather_description": description,
                "fetched_at": fetched_at,
            }
        )

    return records


def resolve_ingest_date(offset_days: int) -> date:
    """Return the observation date to fetch: today(UTC) minus offset_days."""
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

    ingest_date = resolve_ingest_date(offset_days)
    date_str = ingest_date.isoformat()
    print(
        f"Ingest date={date_str} (offset_days={offset_days})  "
        f"location={LOCATION_NAME} ({LATITUDE}, {LONGITUDE})"
    )

    fetched_at = datetime.now(tz=timezone.utc)
    payload = fetch_weather(build_query_params(ingest_date))
    records = parse_weather(payload, fetched_at)
    print(f"Parsed {len(records)} hourly weather records.")

    buf = io.StringIO()
    pd.DataFrame(records).to_csv(buf, index=False)

    csv_path = f"/Volumes/{catalog}/{schema}/raw_openmeteo/{LOCATION_NAME}/{date_str}.csv"
    dbutils.fs.put(csv_path, buf.getvalue(), overwrite=True)
    print(f"Written CSV → {csv_path}")


if __name__ == "__main__":
    main()
