#!/usr/bin/env python3
"""
Open-Meteo → InfluxDB Weather Poller
Maynooth Homestead Digital Twin

Fetches hourly outdoor weather data from Open-Meteo (free, no API key)
and writes it to InfluxDB measurement 'outdoor_weather'.

Idempotent: queries InfluxDB for the latest stored timestamp and only
writes rows newer than that. Safe to run multiple times.

Usage:
  # Normal daily run (fetches last 2 days, deduped):
  python weather_poller.py

  # One-time backfill from season start:
  python weather_poller.py --backfill

  # Backfill from a specific date:
  python weather_poller.py --from-date 2026-04-01

Run via n8n Execute Command node at 01:00 daily.
Requires: influxdb-client, requests, python-dotenv
"""

import os
import sys
import math
import logging
import argparse
import requests
from datetime import datetime, date, timezone, timedelta
from dotenv import load_dotenv
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("weather_poller")

# ── Config ────────────────────────────────────────────────────────────────────
# Load .env from digital_twin root (one level up from poller/)
_ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")
load_dotenv(_ENV_PATH)

INFLUX_URL    = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN  = os.environ["INFLUX_TOKEN"]
INFLUX_ORG    = os.getenv("INFLUX_ORG", "maynooth")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "greenhouse")
GH_LAT        = os.getenv("GH_LATITUDE", "53.38")
GH_LON        = os.getenv("GH_LONGITUDE", "-6.59")

SEASON_START  = date(2026, 4, 1)   # First day of data collection
MEASUREMENT   = "outdoor_weather"

# Open-Meteo free tier: max 92 past days per call
OPEN_METEO_MAX_PAST_DAYS = 92


# ── Open-Meteo fetch ──────────────────────────────────────────────────────────

def fetch_open_meteo(past_days: int, forecast_days: int = 1) -> list[dict]:
    """
    Fetch hourly weather from Open-Meteo.
    Returns list of dicts, one per hour, with parsed fields.
    """
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={GH_LAT}&longitude={GH_LON}"
        f"&hourly=temperature_2m,relative_humidity_2m,precipitation,"
        f"windspeed_10m,vapour_pressure_deficit,"
        f"et0_fao_evapotranspiration,shortwave_radiation,weather_code"
        f"&past_days={past_days}&forecast_days={forecast_days}"
        f"&timezone=UTC"
        f"&temperature_unit=celsius&windspeed_unit=kmh&precipitation_unit=mm"
    )
    log.info(f"Fetching Open-Meteo: past_days={past_days}, forecast_days={forecast_days}")
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()["hourly"]

    rows = []
    times = data["time"]
    for i, ts_str in enumerate(times):
        # Open-Meteo returns "2026-04-01T00:00" in UTC (timezone=UTC)
        dt = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
        # Skip future hours (only store observed data, not forecasts)
        if dt > datetime.now(timezone.utc):
            continue
        rows.append({
            "time":             dt,
            "temp_c":           data["temperature_2m"][i],
            "rh_pct":           data["relative_humidity_2m"][i],
            "precip_mm":        data["precipitation"][i],
            "wind_kmh":         data["windspeed_10m"][i],
            "vpd_kpa":          data["vapour_pressure_deficit"][i],
            "et0_mm":           data["et0_fao_evapotranspiration"][i],
            "shortwave_wm2":    data["shortwave_radiation"][i],
            "weather_code":     data["weather_code"][i],
        })
    log.info(f"Fetched {len(rows)} historical hourly rows from Open-Meteo")
    return rows


# ── LGP helper ───────────────────────────────────────────────────────────────

def is_lgp_day(mean_temp_c: float, et0_mm: float, precip_mm: float) -> bool:
    """
    FAO LGP criterion simplified for tomatoes:
    - Mean daily temp > 10°C (base temp for Solanum lycopersicum)
    - Moisture available (precip >= 0.5 × ET0, or always True inside GH)
    """
    return mean_temp_c > 10.0


# ── InfluxDB helpers ──────────────────────────────────────────────────────────

def get_latest_stored_time(client: InfluxDBClient) -> datetime | None:
    """Query InfluxDB for the most recent timestamp in outdoor_weather."""
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: 2026-01-01T00:00:00Z)
  |> filter(fn: (r) => r._measurement == "{MEASUREMENT}" and r._field == "temp_c")
  |> last()
  |> keep(columns: ["_time"])
'''
    try:
        tables = client.query_api().query(query, org=INFLUX_ORG)
        for table in tables:
            for record in table.records:
                t = record.get_time()
                log.info(f"Latest stored timestamp: {t}")
                return t
    except Exception as e:
        log.warning(f"Could not query latest timestamp: {e}")
    return None


def write_rows(client: InfluxDBClient, rows: list[dict], since: datetime | None):
    """Write rows to InfluxDB, skipping any already stored (dedup by timestamp)."""
    write_api = client.write_api(write_options=SYNCHRONOUS)
    points = []
    skipped = 0

    for row in rows:
        if since and row["time"] <= since:
            skipped += 1
            continue
        # Derive LGP flag (hourly temp > 10°C counts toward LGP)
        lgp_active = 1 if row["temp_c"] is not None and row["temp_c"] > 10.0 else 0

        p = (
            Point(MEASUREMENT)
            .tag("source", "open_meteo")
            .tag("location", "maynooth")
            .field("temp_c",        float(row["temp_c"])        if row["temp_c"]        is not None else 0.0)
            .field("rh_pct",        float(row["rh_pct"])        if row["rh_pct"]        is not None else 0.0)
            .field("precip_mm",     float(row["precip_mm"])     if row["precip_mm"]     is not None else 0.0)
            .field("wind_kmh",      float(row["wind_kmh"])      if row["wind_kmh"]      is not None else 0.0)
            .field("vpd_kpa",       float(row["vpd_kpa"])       if row["vpd_kpa"]       is not None else 0.0)
            .field("et0_mm",        float(row["et0_mm"])        if row["et0_mm"]        is not None else 0.0)
            .field("shortwave_wm2", float(row["shortwave_wm2"]) if row["shortwave_wm2"] is not None else 0.0)
            .field("weather_code",  int(row["weather_code"])    if row["weather_code"]  is not None else 0)
            .field("lgp_active",    lgp_active)
            .time(row["time"], write_precision="s")
        )
        points.append(p)

    if skipped:
        log.info(f"Skipped {skipped} already-stored rows (dedup)")

    if not points:
        log.info("No new rows to write.")
        return 0

    write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
    log.info(f"Wrote {len(points)} rows to InfluxDB [{MEASUREMENT}]")
    return len(points)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Open-Meteo → InfluxDB weather poller")
    parser.add_argument(
        "--backfill", action="store_true",
        help="Backfill from season start (2026-04-01) — use once on first run"
    )
    parser.add_argument(
        "--from-date", type=str, default=None,
        help="Backfill from a specific date (YYYY-MM-DD)"
    )
    args = parser.parse_args()

    # Determine how many past_days to request
    if args.from_date:
        from_dt = date.fromisoformat(args.from_date)
        past_days = (date.today() - from_dt).days + 1
    elif args.backfill:
        past_days = (date.today() - SEASON_START).days + 1
    else:
        past_days = 2  # Normal daily run: overlap by 2 days to catch any gaps

    past_days = min(past_days, OPEN_METEO_MAX_PAST_DAYS)
    log.info(f"Mode: {'backfill' if (args.backfill or args.from_date) else 'daily'} | past_days={past_days}")

    # Connect to InfluxDB
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)

    # Get latest stored timestamp for dedup (skip on backfill to check anyway)
    latest = get_latest_stored_time(client)

    # Fetch from Open-Meteo
    rows = fetch_open_meteo(past_days=past_days)

    # Write (deduped)
    written = write_rows(client, rows, since=latest)

    client.close()
    log.info(f"Done. Written: {written} rows. Latest stored: {latest}")

    # Summary for n8n / cron log
    print(f"weather_poller OK | written={written} | past_days={past_days} | latest_before={latest}")


if __name__ == "__main__":
    main()
