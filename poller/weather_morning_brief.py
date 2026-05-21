#!/usr/bin/env python3
"""
weather_morning_brief.py — Daily 07:45 Pushover morning summary

Reads from InfluxDB:
  - outdoor_weather  (today's Open-Meteo forecast)
  - greenhouse_canopy (latest WH31 canopy reading)
  - soil_moisture    (latest WH51 × 2 readings)

Sends a structured Pushover notification to Dan's Gardening app token.

Cron (NUC): 45 7 * * * python3 ~/gardening/poller/weather_morning_brief.py >> ~/gardening/logs/morning_brief.log 2>&1
No Docker container needed — runs directly on NUC host Python 3.
Loads ~/gardening/.env automatically when running via cron.
"""

import os
import json
import urllib.request
import urllib.parse
import pathlib
import logging
from datetime import datetime, timezone, date

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Load .env (for cron — env vars not inherited) ────────────────────────────

def _load_env():
    env_path = pathlib.Path.home() / "gardening" / ".env"
    if not env_path.exists():
        log.warning(".env not found at %s", env_path)
        return
    for line in env_path.read_text().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

_load_env()

# ── Config ───────────────────────────────────────────────────────────────────

INFLUX_URL   = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG   = os.environ.get("INFLUX_ORG", "maynooth")
INFLUX_DB    = os.environ.get("INFLUX_DATABASE", "greenhouse")

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_GH_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER_KEY", "")

# ── InfluxDB helpers ─────────────────────────────────────────────────────────

def flux_query(query: str) -> str:
    """Query InfluxDB 2.7 via Flux HTTP endpoint (not gRPC — gRPC broken on 2.7)."""
    url = f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}"
    req = urllib.request.Request(
        url,
        data=query.encode(),
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type": "application/vnd.flux",
            "Accept": "application/csv",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def parse_csv(csv_text: str) -> list[dict]:
    """Parse InfluxDB annotated CSV response into list of row dicts.

    Handles CRLF line endings (InfluxDB 2.7 returns \r\n) and
    repeated table-header rows in multi-series responses.
    """
    lines = [l.strip() for l in csv_text.strip().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if len(lines) < 2:
        return []
    headers = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        vals = [v.strip() for v in line.split(",")]
        row = dict(zip(headers, vals))
        # Skip repeated header rows that appear between multi-series tables
        if row.get("_value") == "_value" or row.get("result") == "result":
            continue
        rows.append(row)
    return rows

# ── Pushover ─────────────────────────────────────────────────────────────────

def send_pushover(title: str, message: str) -> bool:
    data = urllib.parse.urlencode({
        "token":   PUSHOVER_TOKEN,
        "user":    PUSHOVER_USER,
        "title":   title,
        "message": message,
    }).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=data, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    return resp.get("status") == 1

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not INFLUX_TOKEN:
        log.error("INFLUX_TOKEN not set — aborting")
        return
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        log.error("PUSHOVER_GH_TOKEN or PUSHOVER_USER_KEY not set — aborting")
        return

    today = date.today().isoformat()
    log.info("Morning brief for %s", today)

    # ── 1. Today's outdoor weather forecast ──────────────────────────────────
    weather_q = f'''
from(bucket:"{INFLUX_DB}")
  |> range(start: {today}T00:00:00Z)
  |> filter(fn:(r) => r._measurement == "outdoor_weather")
  |> filter(fn:(r) =>
       r._field == "temp_c"     or
       r._field == "precip_mm"  or
       r._field == "wind_kmh"   or
       r._field == "et0_mm"     or
       r._field == "shortwave_wm2")
'''

    # ── 2. Latest greenhouse canopy (last 90 min) ─────────────────────────────
    canopy_q = f'''
from(bucket:"{INFLUX_DB}")
  |> range(start: -90m)
  |> filter(fn:(r) => r._measurement == "greenhouse_canopy")
  |> filter(fn:(r) =>
       r._field == "temperature_c" or
       r._field == "humidity_pct"  or
       r._field == "lvpd_kpa"      or
       r._field == "lvpd_zone")
  |> last()
'''

    # ── 3. Latest soil moisture ───────────────────────────────────────────────
    soil_q = f'''
from(bucket:"{INFLUX_DB}")
  |> range(start: -90m)
  |> filter(fn:(r) => r._measurement == "soil_moisture")
  |> filter(fn:(r) => r._field == "moisture_pct")
  |> last()
'''

    try:
        weather_rows = parse_csv(flux_query(weather_q))
    except Exception as e:
        log.error("InfluxDB weather query failed: %s", e)
        weather_rows = []

    try:
        canopy_rows = parse_csv(flux_query(canopy_q))
    except Exception as e:
        log.error("InfluxDB canopy query failed: %s", e)
        canopy_rows = []

    try:
        soil_rows = parse_csv(flux_query(soil_q))
    except Exception as e:
        log.error("InfluxDB soil query failed: %s", e)
        soil_rows = []

    # ── Process outdoor forecast ──────────────────────────────────────────────
    temps, precips, winds, et0s, solar = [], [], [], [], []
    for row in weather_rows:
        field = row.get("_field", "")
        try:
            val = float(row.get("_value", ""))
        except (ValueError, TypeError):
            continue
        if field == "temp_c":        temps.append(val)
        elif field == "precip_mm":   precips.append(val)
        elif field == "wind_kmh":    winds.append(val)
        elif field == "et0_mm":      et0s.append(val)
        elif field == "shortwave_wm2": solar.append(val)

    temp_min    = min(temps)    if temps   else None
    temp_max    = max(temps)    if temps   else None
    rain_total  = sum(precips)  if precips else 0.0
    wind_max    = max(winds)    if winds   else None
    et0_today   = sum(et0s)     if et0s    else None
    peak_solar  = max(solar)    if solar   else None

    # ── Process canopy ────────────────────────────────────────────────────────
    canopy: dict[str, str] = {}
    for row in canopy_rows:
        canopy[row.get("_field", "")] = row.get("_value", "?")

    # ── Process soil ──────────────────────────────────────────────────────────
    soil: dict[str, float | None] = {}
    for row in soil_rows:
        zone = row.get("zone", "?")
        try:
            soil[zone] = float(row.get("_value", ""))
        except (ValueError, TypeError):
            soil[zone] = None

    # ── Format message ────────────────────────────────────────────────────────
    today_str = datetime.now().strftime("%a %d %b")

    # Weather line
    if temp_min is not None and temp_max is not None:
        weather_line = f"🌡 {temp_min:.0f}–{temp_max:.0f}°C"
    else:
        weather_line = "🌡 No forecast"

    if rain_total > 0.5:
        weather_line += f"  ☔ {rain_total:.1f}mm"
    else:
        weather_line += "  ☀️ Dry"

    if wind_max is not None and wind_max > 25:
        weather_line += f"  💨 {wind_max:.0f}km/h"

    if et0_today is not None:
        weather_line += f"  ET₀ {et0_today:.1f}mm"

    if peak_solar is not None:
        weather_line += f"  ☀️{peak_solar:.0f}W/m²"

    # Greenhouse line
    try:
        gh_temp  = float(canopy.get("temperature_c", ""))
        gh_rh    = float(canopy.get("humidity_pct", ""))
        gh_lvpd  = float(canopy.get("lvpd_kpa", ""))
        gh_zone  = canopy.get("lvpd_zone", "?")
        zone_icons = {"optimal": "🟢", "low_risk": "🔵", "high_risk": "🟡", "critical": "🔴"}
        zone_icon  = zone_icons.get(gh_zone, "⚪")
        gh_line = (
            f"🌿 GH {gh_temp:.1f}°C  RH {gh_rh:.0f}%  "
            f"LVPD {gh_lvpd:.2f} kPa {zone_icon}"
        )
    except (ValueError, TypeError):
        gh_line = "🌿 GH: no sensor data"

    # Soil line
    soil_parts = []
    for zone in ("GH4N", "GH4S"):
        pct = soil.get(zone)
        if pct is not None:
            icon = "🟢" if pct > 50 else ("🟡" if pct > 25 else "🔴")
            soil_parts.append(f"{icon} {zone} {pct:.0f}%")
        else:
            soil_parts.append(f"⚪ {zone} ?")
    soil_line = "🌱 " + "  ".join(soil_parts) if soil_parts else "🌱 Soil: no data"

    message = "\n".join([weather_line, gh_line, soil_line])
    title   = f"🌱 Garden — {today_str}"

    log.info("Sending Pushover:\n%s\n%s", title, message)

    try:
        ok = send_pushover(title, message)
        log.info("Pushover result: %s", "✅ sent" if ok else "❌ failed")
    except Exception as e:
        log.error("Pushover send failed: %s", e)


if __name__ == "__main__":
    main()
