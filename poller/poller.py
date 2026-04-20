#!/usr/bin/env python3
"""
Ecowitt Cloud API → InfluxDB Poller
Maynooth Homestead Digital Twin — Phase 1 (Cloud polling)

Polls Ecowitt Cloud API every N seconds, calculates LVPD,
and writes all sensor data to InfluxDB 2.x.

Sensors expected:
  - WH31: greenhouse canopy temp + RH
  - WH51 × 2: soil moisture (GH4N = ch1, GH4S = ch2)
  - GW3000: outdoor temp + RH (reference only)
"""

import os
import time
import math
import logging
import requests
from datetime import datetime, timezone
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gh_poller")

# ── Config from environment ───────────────────────────────────────────────────
ECOWITT_APP_KEY = os.environ["ECOWITT_APPLICATION_KEY"]
ECOWITT_API_KEY = os.environ["ECOWITT_API_KEY"]
DEVICE_MAC      = os.environ["ECOWITT_DEVICE_MAC"]
INFLUX_URL      = os.environ["INFLUX_URL"]
INFLUX_TOKEN    = os.environ["INFLUX_TOKEN"]
INFLUX_ORG      = os.environ["INFLUX_ORG"]
INFLUX_BUCKET   = os.environ["INFLUX_BUCKET"]
POLL_INTERVAL   = int(os.environ.get("POLL_INTERVAL_SECONDS", 300))

ECOWITT_BASE = "https://api.ecowitt.net/api/v3"


# ── PsychrometricEngine ───────────────────────────────────────────────────────

def saturation_vapor_pressure(T_c: float) -> float:
    """Tetens formula. Returns SVP in kPa."""
    return 0.6108 * math.exp(17.27 * T_c / (T_c + 237.3))


def calc_lvpd(T_air: float, rh: float, leaf_offset: float = 2.0) -> float:
    """
    Leaf Vapor Pressure Deficit (kPa).
    Tleaf = Tair - leaf_offset (standard GH approximation: 2°C).
    Deficit = SVP(Tleaf) - actual VP(Tair, RH).
    Target zone: 0.4 – 1.2 kPa.
    """
    T_leaf = T_air - leaf_offset
    svp_leaf = saturation_vapor_pressure(T_leaf)
    svp_air  = saturation_vapor_pressure(T_air)
    actual_vp = svp_air * (rh / 100.0)
    return round(svp_leaf - actual_vp, 4)


def lvpd_zone(lvpd_kpa: float) -> str:
    if lvpd_kpa < 0.4:   return "TOO_HUMID"
    if lvpd_kpa < 0.8:   return "SUBOPTIMAL_LOW"
    if lvpd_kpa <= 1.2:  return "OPTIMAL"
    if lvpd_kpa <= 1.5:  return "SUBOPTIMAL_HIGH"
    return "STRESS"


# ── Ecowitt API ───────────────────────────────────────────────────────────────

def fetch_device_data() -> dict | None:
    """
    Fetch real-time sensor data from Ecowitt Cloud API.
    Returns parsed data dict or None on failure.

    API docs: https://doc.ecowitt.net/web/#/apiv3en
    """
    params = {
        "application_key": ECOWITT_APP_KEY,
        "api_key":         ECOWITT_API_KEY,
        "mac":             DEVICE_MAC,
        "call_back":       "all",
        "cycle_type":      "auto",
        "temp_unitid":     1,    # Celsius
        "pressure_unitid": 3,    # hPa
        "wind_speed_unitid": 6,  # m/s
        "rainfall_unitid": 12,   # mm
        "solar_irradiance_unitid": 16,
    }
    try:
        r = requests.get(f"{ECOWITT_BASE}/device/real_time", params=params, timeout=15)
        r.raise_for_status()
        body = r.json()

        if body.get("code") != 0:
            log.error("Ecowitt API error %s: %s", body.get("code"), body.get("msg"))
            return None

        return body.get("data", {})

    except requests.RequestException as e:
        log.error("Ecowitt API request failed: %s", e)
        return None


def safe_float(d: dict, *keys) -> float | None:
    """Safely traverse nested dict and return float value."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    if d is None:
        return None
    try:
        return float(d)
    except (TypeError, ValueError):
        return None


# ── InfluxDB writer ───────────────────────────────────────────────────────────

def write_to_influx(write_api, points: list[Point]) -> None:
    try:
        write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=points)
        log.info("Wrote %d point(s) to InfluxDB", len(points))
    except Exception as e:
        log.error("InfluxDB write failed: %s", e)


# ── Main polling loop ─────────────────────────────────────────────────────────

def parse_and_build_points(data: dict) -> list[Point]:
    """Parse Ecowitt data dict and build InfluxDB points."""
    points = []
    ts = datetime.now(timezone.utc)

    # ── Outdoor (GW3000 built-in sensor) ─────────────────────────────────────
    outdoor = data.get("outdoor", {})
    out_temp = safe_float(outdoor, "temperature", "value")
    out_rh   = safe_float(outdoor, "humidity", "value")

    if out_temp is not None and out_rh is not None:
        p = (
            Point("outdoor")
            .tag("sensor", "GW3000_builtin")
            .tag("location", "garden_outside")
            .field("temperature_c", out_temp)
            .field("humidity_pct", out_rh)
            .time(ts, WritePrecision.SECONDS)
        )
        points.append(p)

    # ── Indoor / Canopy (WH31) ────────────────────────────────────────────────
    # WH31 appears under "indoor" when it's the first extra sensor
    indoor = data.get("indoor", {})
    # Also check "temp_and_humidity_ch1" which is how extra WH31 sensors appear
    wh31_ch1 = data.get("temp_and_humidity_ch1", indoor)

    gh_temp = safe_float(wh31_ch1, "temperature", "value")
    gh_rh   = safe_float(wh31_ch1, "humidity", "value")

    if gh_temp is not None and gh_rh is not None:
        lvpd_val = calc_lvpd(gh_temp, gh_rh)
        zone = lvpd_zone(lvpd_val)

        p = (
            Point("greenhouse_canopy")
            .tag("sensor", "WH31")
            .tag("location", "GH_canopy")
            .field("temperature_c", gh_temp)
            .field("humidity_pct", gh_rh)
            .field("lvpd_kpa", lvpd_val)
            .field("lvpd_zone", zone)
            .time(ts, WritePrecision.SECONDS)
        )
        points.append(p)
        log.info(
            "Canopy  → %.1f°C  RH %.0f%%  LVPD %.3f kPa  [%s]",
            gh_temp, gh_rh, lvpd_val, zone
        )
    else:
        log.warning("WH31 canopy data missing (check sensor channel name in API response)")

    # ── Soil Moisture (WH51 × 2) ──────────────────────────────────────────────
    soil_channels = {
        "soil_ch1": "GH4N",   # GH4 North bed
        "soil_ch2": "GH4S",   # GH4 South bed
    }
    for api_key, label in soil_channels.items():
        ch_data = data.get(api_key, {})
        moisture = safe_float(ch_data, "soilmoisture", "value")
        # Some firmware versions use "soil_moisture" or "moisture"
        if moisture is None:
            moisture = safe_float(ch_data, "soil_moisture", "value")
        if moisture is None:
            moisture = safe_float(ch_data, "moisture", "value")

        if moisture is not None:
            p = (
                Point("soil_moisture")
                .tag("sensor", "WH51")
                .tag("zone", label)
                .field("moisture_pct", moisture)
                .time(ts, WritePrecision.SECONDS)
            )
            points.append(p)
            log.info("Soil %s → %.0f%%", label, moisture)
        else:
            log.warning("WH51 soil data missing for channel %s", api_key)

    return points


def main():
    log.info("=== Maynooth Greenhouse Poller starting ===")
    log.info("Ecowitt device MAC : %s", DEVICE_MAC)
    log.info("InfluxDB           : %s  bucket=%s", INFLUX_URL, INFLUX_BUCKET)
    log.info("Poll interval      : %ds", POLL_INTERVAL)

    influx_client = InfluxDBClient(
        url=INFLUX_URL,
        token=INFLUX_TOKEN,
        org=INFLUX_ORG,
    )
    write_api = influx_client.write_api(write_options=SYNCHRONOUS)

    # Wait briefly for InfluxDB to be fully ready
    time.sleep(5)

    while True:
        log.info("── Polling Ecowitt Cloud API ──")
        data = fetch_device_data()

        if data:
            log.info(
                "API keys in response: %s",
                list(data.keys())[:10]   # diagnostic — shows actual sensor channels
            )
            points = parse_and_build_points(data)
            if points:
                write_to_influx(write_api, points)
            else:
                log.warning("No valid data points parsed — check sensor channel names above")
        else:
            log.warning("No data from Ecowitt — will retry next cycle")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
