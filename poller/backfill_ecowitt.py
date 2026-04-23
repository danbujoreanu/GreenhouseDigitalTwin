#!/usr/bin/env python3
"""
backfill_ecowitt.py — Backfill InfluxDB from Ecowitt cloud history API.

Use this whenever the local Docker stack was offline (laptop closed, restart,
travel). Ecowitt's cloud stores sensor data independently — this script fetches
it and writes it back to InfluxDB with the original timestamps, so 2026 ML
training data has no gaps.

Writes to the same measurements as the live poller:
  greenhouse_canopy  → temperature_c, humidity_pct, lvpd_kpa, lvpd_zone
  soil_moisture      → moisture_pct  (tags: zone=GH4N / GH4S)

Usage:
    # Recover a specific gap (date range)
    python poller/backfill_ecowitt.py --from 2026-05-01 --to 2026-05-02

    # With specific times
    python poller/backfill_ecowitt.py --from 2026-05-01T22:00:00 --to 2026-05-02T08:00:00

    # Preview without writing (dry run)
    python poller/backfill_ecowitt.py --from 2026-05-01 --to 2026-05-02 --dry-run

    # Annotate a data gap (when you know the laptop was off but don't have data)
    python poller/backfill_ecowitt.py --annotate-gap --from 2026-05-01T22:00:00 --to 2026-05-02T06:00:00

Run from the digital_twin directory with the .env file present:
    cd ~/Personal\ Projects/Gardening/Greenhouse/digital_twin
    python poller/backfill_ecowitt.py --from 2026-05-01 --to 2026-05-02
"""

import os
import sys
import math
import argparse
import logging
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gh_backfill")

# ── Config from .env (same as live poller) ────────────────────────────────────
ECOWITT_APP_KEY = os.environ["ECOWITT_APPLICATION_KEY"]
ECOWITT_API_KEY = os.environ["ECOWITT_API_KEY"]
DEVICE_MAC      = os.environ["ECOWITT_DEVICE_MAC"]
INFLUX_URL      = os.getenv("INFLUX_URL",    "http://localhost:8086")
INFLUX_TOKEN    = os.environ["INFLUX_TOKEN"]
INFLUX_ORG      = os.getenv("INFLUX_ORG",    "maynooth_homestead")
INFLUX_BUCKET   = os.getenv("INFLUX_BUCKET", "greenhouse")

ECOWITT_HISTORY_URL = "https://api.ecowitt.net/api/v3/device/history"
MAX_RANGE_DAYS = 30  # Ecowitt API limit per request


# ── PsychrometricEngine (identical to live poller) ────────────────────────────

def saturation_vapor_pressure(T_c: float) -> float:
    """Tetens formula. Returns SVP in kPa."""
    return 0.6108 * math.exp(17.27 * T_c / (T_c + 237.3))


def calc_lvpd(T_air: float, rh: float, leaf_offset: float = 2.0) -> float:
    """Leaf Vapor Pressure Deficit (kPa). Target zone: 0.4–1.2 kPa."""
    T_leaf = T_air - leaf_offset
    svp_leaf = saturation_vapor_pressure(T_leaf)
    svp_air  = saturation_vapor_pressure(T_air)
    actual_vp = svp_air * (rh / 100.0)
    return round(svp_leaf - actual_vp, 4)


def lvpd_zone(lvpd_kpa: float) -> str:
    if lvpd_kpa < 0.0:   return "CONDENSING"
    if lvpd_kpa < 0.4:   return "TOO_HUMID"
    if lvpd_kpa < 0.8:   return "SUBOPTIMAL_LOW"
    if lvpd_kpa <= 1.2:  return "OPTIMAL"
    if lvpd_kpa <= 1.5:  return "SUBOPTIMAL_HIGH"
    return "STRESS"


# ── Ecowitt history API ───────────────────────────────────────────────────────

def fetch_ecowitt_history(start_dt: datetime, end_dt: datetime) -> dict:
    """
    Query Ecowitt history API for sensor data in the given range.
    Returns the 'data' dict from the API response.
    Max range: 30 days per call. Call multiple times for longer gaps.
    """
    params = {
        "application_key":       ECOWITT_APP_KEY,
        "api_key":               ECOWITT_API_KEY,
        "mac":                   DEVICE_MAC,
        "start_date":            start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date":              end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "call_back":             "all",
        "temp_unitid":           1,   # Celsius
        "pressure_unitid":       3,   # hPa
        "wind_speed_unitid":     6,   # m/s
        "rainfall_unitid":       12,  # mm
        "solar_irradiance_unitid": 16,
        "cycle_type":            "5min",   # 5-minute resolution — matches live poller cadence
    }

    log.info("Fetching Ecowitt history: %s → %s",
             start_dt.strftime("%Y-%m-%d %H:%M"), end_dt.strftime("%Y-%m-%d %H:%M"))

    try:
        resp = requests.get(ECOWITT_HISTORY_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Ecowitt history API request failed: %s", e)
        sys.exit(1)

    body = resp.json()
    if body.get("code") != 0:
        log.error("Ecowitt API returned error %s: %s", body.get("code"), body.get("msg"))
        log.info("Full response: %s", body)
        sys.exit(1)

    return body.get("data", {})


# ── Parse history response → InfluxDB Points ─────────────────────────────────

def parse_history_to_points(data: dict) -> list[Point]:
    """
    Parse Ecowitt history response into InfluxDB Points.

    Ecowitt history format:
      data["temp_and_humidity_ch1"]["temperature"]["list"] = {"1714473600": "22.3", ...}
      data["soil_ch1"]["soilmoisture"]["list"] = {"1714473600": "45", ...}
    """
    points = []

    # ── Canopy temperature + RH (WH31) ────────────────────────────────────────
    # Try both channel names the API uses
    canopy_raw = None
    for key in ("temp_and_humidity_ch1", "indoor", "wh31_ch1"):
        if key in data and data[key]:
            canopy_raw = data[key]
            log.info("Found canopy data under key: %s", key)
            break

    if canopy_raw is None:
        log.warning("No canopy data (WH31) found in response. Keys: %s", list(data.keys()))
    else:
        temp_list = canopy_raw.get("temperature", {}).get("list", {})
        rh_list   = canopy_raw.get("humidity",    {}).get("list", {})

        common_ts = set(temp_list.keys()) & set(rh_list.keys())
        log.info("Canopy timestamps: %d", len(common_ts))

        for ts_str in sorted(common_ts):
            try:
                temp = float(temp_list[ts_str])
                rh   = float(rh_list[ts_str])
            except (ValueError, TypeError):
                continue

            lv   = calc_lvpd(temp, rh)
            zone = lvpd_zone(lv)
            ts_s = int(ts_str)

            p = (
                Point("greenhouse_canopy")
                .tag("sensor",   "WH31")
                .tag("location", "GH_canopy")
                .tag("source",   "backfill")          # distinguishable from live data
                .field("temperature_c", temp)
                .field("humidity_pct",  rh)
                .field("lvpd_kpa",      lv)
                .field("lvpd_zone",     zone)
                .time(ts_s, "s")
            )
            points.append(p)

    # ── Soil moisture (WH51 ch1=GH4N, ch2=GH4S) ──────────────────────────────
    soil_channels = {
        "soil_ch1": "GH4N",
        "soil_ch2": "GH4S",
    }

    for api_key, zone_label in soil_channels.items():
        if api_key not in data or not data[api_key]:
            log.warning("No soil data for %s (%s)", api_key, zone_label)
            continue

        soil_list = data[api_key].get("soilmoisture", {}).get("list", {})
        if not soil_list:
            # Some firmware returns it differently
            soil_list = data[api_key].get("soil_moisture", {}).get("list", {})

        log.info("Soil %s timestamps: %d", zone_label, len(soil_list))

        for ts_str, val in sorted(soil_list.items()):
            try:
                moisture = float(val)
            except (ValueError, TypeError):
                continue

            p = (
                Point("soil_moisture")
                .tag("sensor", "WH51")
                .tag("zone",   zone_label)
                .tag("source", "backfill")
                .field("moisture_pct", moisture)
                .time(int(ts_str), "s")
            )
            points.append(p)

    return points


# ── Write to InfluxDB ─────────────────────────────────────────────────────────

def write_points(points: list[Point]) -> None:
    """Write points to InfluxDB. Idempotent — re-running won't duplicate data."""
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info("✅ Wrote %d points to InfluxDB (%s / %s)", len(points), INFLUX_URL, INFLUX_BUCKET)
    except Exception as e:
        log.error("InfluxDB write failed: %s", e)
        sys.exit(1)
    finally:
        client.close()


# ── Gap annotation ────────────────────────────────────────────────────────────

def annotate_gap(start_dt: datetime, end_dt: datetime) -> None:
    """
    Write a single annotation point to InfluxDB marking a known data gap.
    This prevents ML models from learning false correlations during the offline period.
    """
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        p = (
            Point("data_gap")
            .tag("reason", "laptop_offline")
            .field("start_ts", int(start_dt.timestamp()))
            .field("end_ts",   int(end_dt.timestamp()))
            .field("duration_hours", (end_dt - start_dt).total_seconds() / 3600)
            .field("notes", f"Gap: {start_dt.isoformat()} → {end_dt.isoformat()}")
            .time(int(start_dt.timestamp()), "s")
        )
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=[p])
        log.info("✅ Gap annotated in InfluxDB: %s → %s", start_dt.isoformat(), end_dt.isoformat())
    finally:
        client.close()


# ── Date parsing ─────────────────────────────────────────────────────────────

def parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date '{s}'. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backfill InfluxDB from Ecowitt cloud history (fills gaps when laptop was offline)"
    )
    parser.add_argument("--from", dest="from_dt", required=True,
                        help="Gap start: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--to", dest="to_dt", required=True,
                        help="Gap end:   YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and parse but do NOT write to InfluxDB")
    parser.add_argument("--annotate-gap", action="store_true",
                        help="Write a gap annotation point only (no data recovery)")
    args = parser.parse_args()

    start_dt = parse_dt(args.from_dt)
    end_dt   = parse_dt(args.to_dt)

    if end_dt <= start_dt:
        log.error("--to must be after --from")
        sys.exit(1)

    gap_days = (end_dt - start_dt).days
    if gap_days > MAX_RANGE_DAYS:
        log.error("Range too large (%d days). Ecowitt API max is %d days. Split into chunks.", gap_days, MAX_RANGE_DAYS)
        sys.exit(1)

    log.info("=== Maynooth Greenhouse — Ecowitt Backfill ===")
    log.info("Gap:      %s → %s", start_dt.isoformat(), end_dt.isoformat())
    log.info("InfluxDB: %s  bucket=%s", INFLUX_URL, INFLUX_BUCKET)

    if args.annotate_gap:
        if args.dry_run:
            log.info("DRY RUN — would annotate gap %s → %s", start_dt.isoformat(), end_dt.isoformat())
        else:
            annotate_gap(start_dt, end_dt)
        return

    # Fetch from Ecowitt
    raw_data = fetch_ecowitt_history(start_dt, end_dt)

    if not raw_data:
        log.warning("Ecowitt returned empty data for this range. Either no sensors recorded, or range is in the future.")
        return

    # Parse into InfluxDB Points
    points = parse_history_to_points(raw_data)

    if not points:
        log.warning("No valid data points parsed. Check sensor key names in API response.")
        log.info("API response keys: %s", list(raw_data.keys()))
        return

    canopy_count = sum(1 for p in points if "greenhouse_canopy" in str(p))
    soil_count   = sum(1 for p in points if "soil_moisture"     in str(p))
    log.info("Parsed: %d canopy points, %d soil points (%d total)", canopy_count, soil_count, len(points))

    if args.dry_run:
        log.info("DRY RUN — sample of what would be written:")
        for p in points[:5]:
            log.info("  %s", p.to_line_protocol())
        log.info("  ... (%d more points)", max(0, len(points) - 5))
        return

    # Write to InfluxDB
    write_points(points)
    log.info("Done. Points tagged source=backfill — distinguishable from live poller data in Grafana.")
    log.info("Tip: Run the gap annotation separately if you want to mark the offline period:")
    log.info("  python poller/backfill_ecowitt.py --annotate-gap --from %s --to %s", args.from_dt, args.to_dt)


if __name__ == "__main__":
    main()
