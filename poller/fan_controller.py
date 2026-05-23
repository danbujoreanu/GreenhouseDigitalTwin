#!/usr/bin/env python3
"""
fan_controller.py — LVPD-first fan control for ProFan via AC1100 WittSwitch
Maynooth Homestead Digital Twin — 2026-05-23

Runs every 10 minutes via cron (07:00–21:00). Queries InfluxDB for current
canopy conditions, computes the target fan state using LVPD-first logic,
and sends ON/OFF commands to the AC1100 via the GW3000 local IoT API.

Only sends a command when the desired state differs from actual state —
avoids unnecessary RF cycles that wear the relay.

Cron (NUC):
  */10 7-21 * * * python3 ~/gardening/poller/fan_controller.py >> ~/gardening/logs/fan_controller.log 2>&1

Force OFF at night is handled by cron (hours 7-21 only) PLUS an explicit
night guard in the script for 21:00 run safety.

───────────────────────────────────────────────────────────────────────────────
SCENARIO MAP
───────────────────────────────────────────────────────────────────────────────

Scenario                  Condition                       Action
─────────────────────────────────────────────────────────────────────────────
1. High humidity (LVPD)   LVPD < 0.4 kPa (TOO_HUMID)     FAN ON  — primary trigger
2. High RH fallback       RH > 78%                        FAN ON  — if LVPD sensor glitch
3. Heat circulation       GH temp > 28°C                  FAN ON  — prevent stratification
4. Optimal conditions     LVPD 0.4–1.2 kPa, RH < 78%     FAN OFF — no intervention needed
5. Frost guard            GH temp < 14°C                  FAN OFF — no cold air on capsicums
6. Night guard            hour < 7 or hour >= 21          FAN OFF — frost risk, cold air
7. Heat crisis            GH temp > 32°C                  FAN ON  + Pushover: switch to Speed 2
8. Sensor stale           InfluxDB data > 15 min old      FAN OFF — fail safe, no blind actuation

See Greenhouse/GH_FAN_CONTROL_SCENARIOS.md for full scenario analysis.
"""

import os
import json
import time
import logging
import pathlib
import urllib.request
import urllib.parse
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger(__name__)


# ── Env loader ────────────────────────────────────────────────────────────────

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

# ── Config ────────────────────────────────────────────────────────────────────

INFLUX_URL   = os.environ.get("INFLUX_URL",      "http://localhost:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN",    "")
INFLUX_ORG   = os.environ.get("INFLUX_ORG",      "maynooth")
INFLUX_DB    = os.environ.get("INFLUX_DATABASE", "greenhouse")

GW3000_IP        = os.environ.get("GW3000_IOT_IP",    "192.168.68.107")
AC1100_DEVICE_ID = int(os.environ.get("AC1100_DEVICE_ID", "12592"))
AC1100_MODEL     = 2

PUSHOVER_TOKEN = os.environ.get("PUSHOVER_GH_TOKEN", "")
PUSHOVER_USER  = os.environ.get("PUSHOVER_USER_KEY",  "")

# ── Decision thresholds ───────────────────────────────────────────────────────

LVPD_TOO_HUMID_KPA  = 0.4    # below this → TOO_HUMID, fan on
RH_HIGH_PCT         = 78.0   # RH fallback trigger (in case LVPD sensor glitches)
TEMP_HEAT_CIRC_C    = 28.0   # above this → heat circulation, fan on
TEMP_HEAT_CRISIS_C  = 32.0   # above this → fan on + Pushover Speed 2 alert
TEMP_FROST_GUARD_C  = 14.0   # below this → frost guard, fan off
NIGHT_START_HOUR    = 21     # fan forced off at/after this hour (local)
NIGHT_END_HOUR      = 7      # fan allowed back on at/after this hour (local)
DATA_STALE_SECS     = 900    # 15 min — if InfluxDB data older than this, fail safe OFF


# ── InfluxDB (Flux HTTP — read only) ─────────────────────────────────────────

def flux_query(query: str) -> str:
    req = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
        data=query.encode(),
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Content-Type":  "application/vnd.flux",
            "Accept":        "application/csv",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def parse_csv(csv_text: str) -> list[dict]:
    """Parse InfluxDB Flux CSV with CRLF safety (see TOKEN_EFFICIENCY gotcha #13)."""
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


def get_canopy() -> dict | None:
    """
    Return latest canopy reading from InfluxDB.
    Returns dict with keys: temp_c, rh_pct, lvpd_kpa, lvpd_zone, age_secs
    Returns None if data is stale or missing.
    """
    q = f'''
from(bucket:"{INFLUX_DB}")
  |> range(start: -20m)
  |> filter(fn:(r) => r._measurement == "greenhouse_canopy")
  |> filter(fn:(r) => r._field == "temperature_c" or r._field == "humidity_pct"
       or r._field == "lvpd_kpa" or r._field == "lvpd_zone")
  |> last()
'''
    try:
        rows = parse_csv(flux_query(q))
    except Exception as e:
        log.warning("InfluxDB query failed: %s", e)
        return None

    canopy = {}
    latest_ts = 0
    for row in rows:
        canopy[row.get("_field", "")] = row.get("_value", "")
        try:
            ts_str = row.get("_time", "")
            if ts_str:
                from datetime import datetime
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                latest_ts = max(latest_ts, int(ts.timestamp()))
        except Exception:
            pass

    if not canopy.get("lvpd_kpa"):
        log.warning("No canopy data in last 20 min")
        return None

    age = int(time.time()) - latest_ts

    try:
        return {
            "temp_c":    float(canopy.get("temperature_c", "0")),
            "rh_pct":    float(canopy.get("humidity_pct",  "0")),
            "lvpd_kpa":  float(canopy.get("lvpd_kpa",      "0")),
            "lvpd_zone": canopy.get("lvpd_zone", "?"),
            "age_secs":  age,
        }
    except (ValueError, TypeError) as e:
        log.warning("Could not parse canopy values: %s", e)
        return None


# ── AC1100 IoT control ────────────────────────────────────────────────────────

def iot_post(payload: dict) -> str:
    """POST to GW3000 local IoT API. Returns response text."""
    req = urllib.request.Request(
        f"http://{GW3000_IP}/parse_quick_cmd_iot",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode()


def read_ac1100() -> dict | None:
    """Read current AC1100 state. Returns dict or None on failure."""
    try:
        resp = iot_post({"command": [{"cmd": "read_device",
                                       "id": AC1100_DEVICE_ID,
                                       "model": AC1100_MODEL}]})
        d = json.loads(resp)["command"][0]
        offline = bool(d.get("warning", 0) & 128)
        return {
            "fan_on":  bool(d.get("ac_status", 0)),
            "power_w": d.get("realtime_power", 0),
            "rssi":    d.get("gw_rssi", 0),
            "offline": offline,
        }
    except Exception as e:
        log.warning("AC1100 read failed: %s", e)
        return None


def fan_on() -> bool:
    """Send quick_run (always-on) command. Returns True on success."""
    try:
        resp = iot_post({"command": [{
            "cmd": "quick_run", "on_type": 0, "off_type": 0,
            "always_on": 1, "on_time": 0, "off_time": 0,
            "val_type": 0, "val": 0,
            "id": AC1100_DEVICE_ID, "model": AC1100_MODEL,
        }]})
        return "200" in resp
    except Exception as e:
        log.error("fan_on command failed: %s", e)
        return False


def fan_off() -> bool:
    """Send quick_stop command. Returns True on success."""
    try:
        resp = iot_post({"command": [{"cmd": "quick_stop",
                                       "id": AC1100_DEVICE_ID,
                                       "model": AC1100_MODEL}]})
        return "200" in resp
    except Exception as e:
        log.error("fan_off command failed: %s", e)
        return False


# ── Pushover ──────────────────────────────────────────────────────────────────

def push(title: str, message: str) -> None:
    if not PUSHOVER_TOKEN or not PUSHOVER_USER:
        return
    data = urllib.parse.urlencode({
        "token": PUSHOVER_TOKEN, "user": PUSHOVER_USER,
        "title": title, "message": message,
    }).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request("https://api.pushover.net/1/messages.json",
                                   data=data, method="POST"), timeout=10
        ) as r:
            json.loads(r.read())
    except Exception as e:
        log.warning("Pushover failed: %s", e)


# ── Decision logic ────────────────────────────────────────────────────────────

def compute_desired_state(canopy: dict) -> tuple[bool, str]:
    """
    Return (should_be_on: bool, reason: str).

    Priority order (first matching rule wins):
      1. Night guard   → OFF
      2. Frost guard   → OFF
      3. Data stale    → OFF (fail safe)
      4. Heat crisis   → ON  (+ Pushover)
      5. TOO_HUMID     → ON  (LVPD < 0.4 kPa — primary trigger)
      6. High RH       → ON  (fallback)
      7. Heat circ     → ON  (temp > 28°C)
      8. Default       → OFF
    """
    hour = datetime.now().hour   # local time

    # 1. Night guard
    if hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR:
        return False, f"night_guard (hour={hour})"

    # 2. Frost guard
    if canopy["temp_c"] < TEMP_FROST_GUARD_C:
        return False, f"frost_guard (temp={canopy['temp_c']:.1f}°C < {TEMP_FROST_GUARD_C}°C)"

    # 3. Stale data — fail safe
    if canopy["age_secs"] > DATA_STALE_SECS:
        return False, f"stale_data (age={canopy['age_secs']}s > {DATA_STALE_SECS}s)"

    # 4. Heat crisis
    if canopy["temp_c"] > TEMP_HEAT_CRISIS_C:
        return True, f"heat_crisis (temp={canopy['temp_c']:.1f}°C > {TEMP_HEAT_CRISIS_C}°C)"

    # 5. LVPD TOO_HUMID — primary trigger
    if canopy["lvpd_kpa"] < LVPD_TOO_HUMID_KPA:
        return True, f"lvpd_too_humid (lvpd={canopy['lvpd_kpa']:.3f} kPa < {LVPD_TOO_HUMID_KPA})"

    # 6. High RH fallback
    if canopy["rh_pct"] > RH_HIGH_PCT:
        return True, f"high_rh (rh={canopy['rh_pct']:.0f}% > {RH_HIGH_PCT}%)"

    # 7. Heat circulation
    if canopy["temp_c"] > TEMP_HEAT_CIRC_C:
        return True, f"heat_circ (temp={canopy['temp_c']:.1f}°C > {TEMP_HEAT_CIRC_C}°C)"

    # 8. Default — conditions acceptable
    return False, (
        f"optimal (lvpd={canopy['lvpd_kpa']:.3f} kPa  "
        f"rh={canopy['rh_pct']:.0f}%  "
        f"temp={canopy['temp_c']:.1f}°C)"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not INFLUX_TOKEN:
        log.error("INFLUX_TOKEN not set — aborting")
        return

    # 1. Read canopy conditions
    canopy = get_canopy()
    if canopy is None:
        log.warning("No canopy data — sending OFF as fail safe")
        fan_off()
        return

    log.info(
        "Canopy  → %.1f°C  RH %.0f%%  LVPD %.3f kPa  [%s]  (age %ds)",
        canopy["temp_c"], canopy["rh_pct"], canopy["lvpd_kpa"],
        canopy["lvpd_zone"], canopy["age_secs"]
    )

    # 2. Read current fan state
    current = read_ac1100()
    if current is None:
        log.warning("AC1100 unreachable — skipping cycle (no blind commands)")
        return

    log.info(
        "AC1100  → fan=%s  power=%dW  rssi=%ddBm  offline=%s",
        "ON" if current["fan_on"] else "OFF",
        current["power_w"], current["rssi"], current["offline"]
    )

    # 3. Compute desired state
    desired_on, reason = compute_desired_state(canopy)
    log.info("Decision → %s  [%s]", "ON" if desired_on else "OFF", reason)

    # 4. Act only on state change
    if desired_on == current["fan_on"]:
        log.info("No change needed — current state matches desired")
        return

    action_label = "ON" if desired_on else "OFF"
    log.info("Sending fan %s ...", action_label)

    ok = fan_on() if desired_on else fan_off()

    if not ok:
        log.error("Command failed — will retry next cycle")
        return

    # 5. Verify state change after 4s
    time.sleep(4)
    verify = read_ac1100()
    if verify and verify["fan_on"] == desired_on:
        log.info("Verified: fan is now %s", action_label)
    else:
        log.warning("State verify failed — RF may be slow; will re-check next cycle")

    # 6. Pushover notifications
    zone_icons = {
        "TOO_HUMID": "💧", "SUBOPTIMAL_LOW": "🔵",
        "OPTIMAL": "🟢", "SUBOPTIMAL_HIGH": "🟡", "STRESS": "🔴",
    }
    icon = zone_icons.get(canopy["lvpd_zone"], "⚪")

    if desired_on and "heat_crisis" in reason:
        push(
            "🔥 GH Heat Crisis — Speed 2?",
            f"Temp {canopy['temp_c']:.1f}°C  RH {canopy['rh_pct']:.0f}%  LVPD {canopy['lvpd_kpa']:.2f} kPa {icon}\n"
            f"Fan ON. Consider switching ProFan to Speed 2 manually."
        )
    elif desired_on and not current["fan_on"]:
        # Fan turned ON — log only (no push for routine activations)
        log.info("Fan activated: %s", reason)
    elif not desired_on and current["fan_on"]:
        # Fan turned OFF after running — push summary if it was running for the session
        push(
            "🌿 GH Fan OFF",
            f"Conditions now acceptable.\n"
            f"🌡 {canopy['temp_c']:.1f}°C  💧 RH {canopy['rh_pct']:.0f}%  "
            f"LVPD {canopy['lvpd_kpa']:.2f} kPa {icon}\n"
            f"Reason: {reason}"
        )


if __name__ == "__main__":
    main()
