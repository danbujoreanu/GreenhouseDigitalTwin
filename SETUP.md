# Digital Twin Stack — Setup Guide

## What this stack does

```
Ecowitt Cloud API (every 5 min)
     ↓
 Python Poller  ← calculates LVPD from WH31 temp + RH
     ↓
  InfluxDB 2.7  ← time-series storage
     ↓
   Grafana       ← dashboards + alerts
```

Works from **anywhere** — polls Ecowitt cloud, no LAN access to greenhouse needed.

---

## Prerequisites

- Docker Desktop installed ✅ (you confirmed this)
- Ecowitt account at app.ecowitt.net ✅
- API key: already in `Gardening/.env` ✅
- 2 things to get from Ecowitt dashboard (see Step 1 below)

---

## Step 1 — Get your Ecowitt credentials

**Application Key** (you need to create this — separate from your API key):
1. Go to [app.ecowitt.net](https://app.ecowitt.net)
2. Account (top right) → **API Keys** → **Create Application Key**
3. Name it: `maynooth-digital-twin`
4. Copy the key (looks like a long hex string)

**GW3000 MAC address** (find in the mobile app):
1. Open Ecowitt app on your phone
2. Tap your GW3000 device → **Settings / About**
3. MAC address shown as `AA:BB:CC:DD:EE:FF`

---

## Step 2 — Configure environment

```bash
cd "Greenhouse/digital_twin"
cp .env.example .env
```

Edit `.env`:
```
ECOWITT_APPLICATION_KEY=your_application_key_here
ECOWITT_DEVICE_MAC=AA:BB:CC:DD:EE:FF
# (all other values already set with sensible defaults)
```

---

## Step 3 — Start the stack

```bash
cd "Greenhouse/digital_twin"
docker compose up -d
```

First run takes ~2 min (pulls images, initialises InfluxDB).

Check logs:
```bash
docker compose logs -f poller   # watch polling + LVPD output
docker compose logs influxdb    # confirm InfluxDB ready
```

---

## Step 4 — View in Grafana

Open: **http://localhost:3000**
Login: `admin` / `maynooth_gh_2026` (or whatever you set in `.env`)

InfluxDB datasource is pre-configured (provisioned automatically).

**Write your first Flux query** to confirm data is arriving:
```flux
from(bucket: "greenhouse")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "greenhouse_canopy")
```

---

## Step 5 — Diagnose sensor channel names (first run)

The poller logs the raw API response keys on every poll:
```
API keys in response: ['outdoor', 'indoor', 'temp_and_humidity_ch1', 'soil_ch1', 'soil_ch2', ...]
```

Ecowitt sensor channel naming varies by firmware version. If WH31 or WH51 data is missing,
check the log output for the actual key names and update `parse_and_build_points()` in `poller/poller.py`.

Common variations:
- WH31 → `indoor` | `temp_and_humidity_ch1` | `temp_and_humidity_ch2`
- WH51 → `soil_ch1` | `soilmoisture_ch1`

---

## Useful commands

```bash
# Stop stack
docker compose down

# Stop and delete all data (fresh start)
docker compose down -v

# Rebuild poller after code changes
docker compose build poller
docker compose up -d poller

# Enter InfluxDB CLI
docker exec -it gh_influxdb influx
```

---

## Phase 2 (June — Mac Mini M5): local push mode

When Mac Mini M5 arrives:
1. Configure GW3000 Customized Upload → Mac Mini local IP (port 8080)
2. Add a Flask ingestion service to docker-compose.yml
3. GW3000 pushes every 60s → Flask → InfluxDB (no cloud polling needed)
4. Same Grafana dashboards — no changes needed

---

## Data schema (InfluxDB measurements)

| Measurement | Tags | Fields |
|-------------|------|--------|
| `outdoor` | sensor=GW3000_builtin, location=garden_outside | temperature_c, humidity_pct |
| `greenhouse_canopy` | sensor=WH31, location=GH_canopy | temperature_c, humidity_pct, lvpd_kpa, lvpd_zone |
| `soil_moisture` | sensor=WH51, zone=GH4N/GH4S | moisture_pct |

---

## LVPD zones

| LVPD (kPa) | Zone | Action |
|------------|------|--------|
| < 0.4 | TOO_HUMID | Open vent — botrytis risk |
| 0.4 – 0.8 | SUBOPTIMAL_LOW | Monitor |
| 0.8 – 1.2 | OPTIMAL | ✅ No action |
| 1.2 – 1.5 | SUBOPTIMAL_HIGH | Consider misting |
| > 1.5 | STRESS | Irrigate immediately |
