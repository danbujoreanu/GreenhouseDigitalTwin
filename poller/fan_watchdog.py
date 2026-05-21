#!/usr/bin/env python3
"""
fan_watchdog.py — Evening fan check-in at 22:00

The AC1100 WittSwitch state is not exposed in the Ecowitt cloud API,
so we cannot directly query whether the fan is on or off.

Instead: query InfluxDB for current RH and LVPD. If RH is still HIGH
at 22:00 (suggesting fan may have been running all day), send a Pushover
check-in so Dan can verify the fan isn't running unnecessarily overnight.

Cron (NUC): 0 22 * * * python3 ~/gardening/poller/fan_watchdog.py >> ~/gardening/logs/fan_watchdog.log 2>&1

Why 22:00? Fan should not run overnight in an Irish GH (cold air in =
frost risk). If RH is >80% at 22:00, either the fan is legitimately
needed (rare) or it's stuck on.
"""

import os
import json
import urllib.request
import urllib.parse
import pathlib
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _load_env():
    env_path = pathlib.Path.home() / "gardening" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


_load_env()

INFLUX_URL   = os.environ.get("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN", "")
INFLUX_ORG   = os.environ.get("INFLUX_ORG", "maynooth")
INFLUX_DB    = os.environ.get("INFLUX_DATABASE", "greenhouse")
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_GH_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER_KEY", "")

# Thresholds
RH_ALERT_THRESHOLD  = 80.0   # % — if RH still above this at 22:00, flag it
RH_NORMAL_THRESHOLD = 70.0   # % — below this at night is fine, no alert


def flux_query(query: str) -> str:
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
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
    lines = [l.strip() for l in csv_text.strip().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    if len(lines) < 2:
        return []
    headers = [h.strip() for h in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        vals = [v.strip() for v in line.split(",")]
        row = dict(zip(headers, vals))
        if row.get("_value") == "_value" or row.get("result") == "result":
            continue
        rows.append(row)
    return rows


def send_pushover(title: str, message: str) -> bool:
    data = urllib.parse.urlencode({
        "token": PUSHOVER_TOKEN,
        "user":  PUSHOVER_USER,
        "title": title,
        "message": message,
    }).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=data, method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read()).get("status") == 1


def main():
    if not INFLUX_TOKEN or not PUSHOVER_TOKEN:
        log.error("Missing INFLUX_TOKEN or PUSHOVER_GH_TOKEN — aborting")
        return

    # Query last 30 min of canopy data
    q = f'''
from(bucket:"{INFLUX_DB}")
  |> range(start: -30m)
  |> filter(fn:(r) => r._measurement == "greenhouse_canopy")
  |> filter(fn:(r) => r._field == "humidity_pct" or r._field == "temperature_c" or r._field == "lvpd_kpa" or r._field == "lvpd_zone")
  |> last()
'''
    rows = parse_csv(flux_query(q))
    canopy = {}
    for row in rows:
        canopy[row.get("_field", "")] = row.get("_value", "?")

    try:
        rh   = float(canopy.get("humidity_pct", "0"))
        temp = float(canopy.get("temperature_c", "0"))
        lvpd = float(canopy.get("lvpd_kpa", "0"))
        zone = canopy.get("lvpd_zone", "?")
    except (ValueError, TypeError):
        log.warning("Could not parse canopy data — skipping alert")
        return

    log.info("22:00 check — RH %.0f%%  Temp %.1f°C  LVPD %.2f kPa (%s)", rh, temp, lvpd, zone)

    if rh < RH_NORMAL_THRESHOLD:
        log.info("RH %.0f%% — within normal overnight range. No alert.", rh)
        return

    # RH is elevated at 22:00 — send check-in
    zone_icons = {"optimal": "🟢", "low_risk": "🔵", "high_risk": "🟡", "critical": "🔴"}
    icon = zone_icons.get(zone, "⚪")

    if rh >= RH_ALERT_THRESHOLD:
        severity = "⚠️ HIGH"
        advice   = "Fan may still be running. If frost expected overnight, verify fan is off."
    else:
        severity = "ℹ️ Elevated"
        advice   = "Slightly humid for overnight — monitor."

    message = (
        f"{severity} RH at 22:00\n"
        f"🌡 {temp:.1f}°C  💧 RH {rh:.0f}%  LVPD {lvpd:.2f} kPa {icon}\n"
        f"{advice}"
    )

    ok = send_pushover("🌱 GH Night Check — Fan?", message)
    log.info("Pushover sent: %s", ok)


if __name__ == "__main__":
    main()
