#!/usr/bin/env python3
"""
fan_watchdog.py — Evening safety check at 22:00

Checks ACTUAL fan state (AC1100 ac_status via GW3000 local IoT API) AND
canopy conditions (InfluxDB). Sends Pushover alert if fan is confirmed ON
at 22:00, or if RH is elevated — fan should not run overnight (frost risk).

The fan_controller.py cron (*/10 7-21) handles daytime on/off logic and
sends a final OFF command at 21:00–21:50. This watchdog is the 22:00 safety
net in case a command was missed or the controller was stopped.

Cron (NUC): 0 22 * * * python3 ~/gardening/poller/fan_watchdog.py >> ~/gardening/logs/fan_watchdog.log 2>&1

Updated 2026-05-23: reads actual AC1100 state via GW3000 local IoT API
instead of using RH as proxy (which was a workaround before the IoT API
was discovered — see ECOWITT_API_EXPLAINED.md §8).
"""

import os
import json
import urllib.request
import urllib.parse
import pathlib
import logging
import time

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
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER_KEY",  "")

GW3000_IP        = os.environ.get("GW3000_IOT_IP",    "192.168.68.107")
AC1100_DEVICE_ID = int(os.environ.get("AC1100_DEVICE_ID", "12592"))

# Thresholds
RH_ALERT_THRESHOLD  = 80.0   # % — elevated RH at 22:00 warrants attention
RH_NORMAL_THRESHOLD = 70.0   # % — below this at night, RH is acceptable


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


def read_ac1100_state() -> dict | None:
    """Read actual fan state from GW3000 local IoT API."""
    payload = json.dumps({"command": [{"cmd": "read_device",
                                        "id": AC1100_DEVICE_ID, "model": 2}]}).encode()
    req = urllib.request.Request(
        f"http://{GW3000_IP}/parse_quick_cmd_iot",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read())["command"][0]
            offline = bool(d.get("warning", 0) & 128)
            return {
                "fan_on":  bool(d.get("ac_status", 0)),
                "power_w": d.get("realtime_power", 0),
                "offline": offline,
            }
    except Exception as e:
        log.warning("AC1100 read failed: %s", e)
        return None


def main():
    if not INFLUX_TOKEN or not PUSHOVER_TOKEN:
        log.error("Missing INFLUX_TOKEN or PUSHOVER_GH_TOKEN — aborting")
        return

    # 1. Read actual fan state
    ac = read_ac1100_state()
    fan_state_str = "UNKNOWN"
    fan_is_on = False
    if ac is not None:
        fan_is_on = ac["fan_on"] and not ac["offline"]
        fan_state_str = ("ON (%dW)" % ac["power_w"]) if fan_is_on else (
            "OFFLINE" if ac["offline"] else "OFF"
        )

    # 2. Read canopy conditions
    q = f'''
from(bucket:"{INFLUX_DB}")
  |> range(start: -30m)
  |> filter(fn:(r) => r._measurement == "greenhouse_canopy")
  |> filter(fn:(r) => r._field == "humidity_pct" or r._field == "temperature_c"
       or r._field == "lvpd_kpa" or r._field == "lvpd_zone")
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

    log.info(
        "22:00 check — Fan: %s | RH %.0f%%  Temp %.1f°C  LVPD %.2f kPa (%s)",
        fan_state_str, rh, temp, lvpd, zone
    )

    zone_icons = {
        "TOO_HUMID": "💧", "SUBOPTIMAL_LOW": "🔵",
        "OPTIMAL": "🟢", "SUBOPTIMAL_HIGH": "🟡", "STRESS": "🔴",
    }
    icon = zone_icons.get(zone, "⚪")

    # 3. Alert logic
    if fan_is_on:
        # Fan confirmed running at 22:00 — always alert
        message = (
            f"⚠️ Fan is ON at 22:00 ({ac['power_w']}W)\n"
            f"🌡 {temp:.1f}°C  💧 RH {rh:.0f}%  LVPD {lvpd:.2f} kPa {icon}\n"
            f"Frost risk overnight — turn off unless RH is critical."
        )
        ok = send_pushover("🌱 GH Night — Fan Still Running!", message)
        log.info("Pushover sent (fan on): %s", ok)

    elif rh >= RH_ALERT_THRESHOLD:
        # Fan is off but RH is high — may need manual intervention
        message = (
            f"💧 High RH at 22:00 (fan is OFF)\n"
            f"🌡 {temp:.1f}°C  💧 RH {rh:.0f}%  LVPD {lvpd:.2f} kPa {icon}\n"
            f"Fan controller handled daytime. Monitor overnight condensation."
        )
        ok = send_pushover("🌱 GH Night Check — High RH", message)
        log.info("Pushover sent (high RH, fan off): %s", ok)

    elif rh >= RH_NORMAL_THRESHOLD:
        log.info("RH %.0f%% elevated but acceptable overnight. Fan %s. No alert.", rh, fan_state_str)

    else:
        log.info("All clear — Fan: %s  RH %.0f%%  LVPD %.2f. No alert.", fan_state_str, rh, lvpd)


if __name__ == "__main__":
    main()
