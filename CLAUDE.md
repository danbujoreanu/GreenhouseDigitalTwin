# digital_twin/ — Module-Specific Claude Instructions
*Auto-loaded when Claude works in this directory. Root Gardening CLAUDE.md applies first.*
*Last updated: 2026-05-21*

---

## What This Module Is

Real-time IoT → data → visualisation stack for the Maynooth greenhouse.

```
Ecowitt sensors → GW3000 gateway → Ecowitt Cloud → poller.py → InfluxDB 2.7
Open-Meteo API → weather_poller.py → InfluxDB 2.7
InfluxDB → Grafana (sparc-grafana, port 3001) → Alert rules → n8n → Pushover
InfluxDB → Streamlit (port 8501) → 7-page dashboard + RAG chat
```

Git repo: `https://github.com/danbujoreanu/GreenhouseDigitalTwin.git` | Branch: `main`
Always use `-C` flag: `git -C "/Users/danalexandrubujoreanu/Personal Projects/Gardening/Greenhouse/digital_twin" <cmd>`

---

## InfluxDB Schema

**Instance:** `gardening-influxdb` (port 8086) | Bucket: `greenhouse` | Org: `maynooth`
**Client:** `influxdb_client_3` (write only — gRPC read broken on 2.7; use Flux HTTP for reads)

### `greenhouse_canopy`
Tags: `sensor=WH31`, `location=GH_canopy`
Fields: `temperature_c` (float), `humidity_pct` (float), `lvpd_kpa` (float), `lvpd_zone` (string)
- `lvpd_zone` values: `optimal` (0.4–1.2 kPa) | `low_risk` (<0.4) | `high_risk` (1.2–2.0) | `critical` (>2.0)
- Source: `poller/poller.py` → every 10 min

### `soil_moisture`
Tags: `sensor=WH51`, `zone=GH4N` or `zone=GH4S`
Fields: `moisture_pct` (float)
- `GH4N` = soil_ch1 (North bed), `GH4S` = soil_ch2 (South bed)
- Source: `poller/poller.py` → every 10 min

### `outdoor`
Tags: `sensor=GW3000_builtin`, `location=garden_outside`
Fields: `temperature_c` (float), `humidity_pct` (float)
- Source: `poller/poller.py` → every 10 min

### `outdoor_weather`
No tags.
Fields: `temp_c`, `rh_pct`, `precip_mm`, `wind_kmh`, `vpd_kpa`, `et0_mm`, `shortwave_wm2` (all float), `weather_code` (int), `lgp_active` (int 0/1)
- Source: `poller/weather_poller.py` → daily 01:00 via cron + `gardening-weather-poller` container
- `lgp_active=1` when temp >10°C (LGP = Length of Growing Period day)
- **Schema note:** `lgp_active` MUST write as int. Mixed int/float caused 422 errors and 15-day data gap (May 4–19 2026). Fixed in weather_poller.py.

### Flux read pattern (always use HTTP, not gRPC)
```python
import requests, os
url = f"{os.getenv('INFLUX_URL', 'http://192.168.68.119:8086')}/api/v2/query?org=maynooth"
r = requests.post(url,
    headers={"Authorization": f"Token {os.getenv('INFLUX_TOKEN')}",
             "Content-Type": "application/vnd.flux", "Accept": "application/csv"},
    data='from(bucket:"greenhouse") |> range(start:-1h) |> filter(fn:(r)=>r._measurement=="greenhouse_canopy") |> last()',
    timeout=15)
```

---

## Sensor IDs

| Sensor | Model | Channel | Measurement | Tag |
|--------|-------|---------|-------------|-----|
| GW3000 | Ecowitt GW3000 WiFi Gateway | built-in outdoor | `outdoor` | `sensor=GW3000_builtin` |
| WH31 | Ecowitt WH31 Temp/Humidity | indoor ch1 (extra sensor appears as `temp_and_humidity_ch1`) | `greenhouse_canopy` | `sensor=WH31` |
| WH51 #1 | Ecowitt WH51 Soil Moisture | soil_ch1 | `soil_moisture` | `zone=GH4N` |
| WH51 #2 | Ecowitt WH51 Soil Moisture | soil_ch2 | `soil_moisture` | `zone=GH4S` |

**WH31 gotcha:** API response channel name varies — `indoor` OR `temp_and_humidity_ch1` depending on registration order. `poller.py` checks both. If canopy data missing, add debug log: `list(data.keys())[:10]` to see actual channel names in response.

**Ecowitt env vars:**
```
ECOWITT_APPLICATION_KEY=<key>   # App-level key
ECOWITT_API_KEY=<key>           # Device-specific key
ECOWITT_DEVICE_MAC=<mac>        # GW3000 MAC address
```

---

## Docker Services

| Container | Image | Port | Restart | Purpose |
|-----------|-------|------|---------|---------|
| `gardening-influxdb` | influxdb:2.7-alpine | 8086 | always | Time-series DB |
| `gardening-poller` | local build | — | always | Ecowitt → InfluxDB (10 min) |
| `gardening-weather-poller` | local build | — | **no** | Open-Meteo → InfluxDB (daily cron) |
| `gardening-streamlit` | local build | 8501 | always | 7-page Streamlit hub |
| `unified-rag-api` | local build | 7862 | always | FastAPI RAG service |
| ~~gardening-grafana~~ | decommissioned | ~~3000~~ | 🔴 | Migrated to sparc-grafana 2026-05-19 |

**NUC paths:**
- Docker compose: `~/gardening/docker-compose.yml`
- Poller: `~/gardening/poller/`
- Streamlit: `~/gardening/streamlit_hub/` (volume-mounted → no rebuild for app.py edits)
- RAG: `~/gardening/rag/`

**Weather poller cron** (daily, NUC crontab):
```
0 1 * * * docker start gardening-weather-poller >> ~/gardening/logs/weather_poller.log
```
Note: `restart: "no"` — intentional. It runs once as a batch job (not a daemon). Cron starts it; it runs to completion and exits.

---

## n8n Workflows

Both workflows live in the **sparc** n8n instance (`sparc-n8n`, port 5678).

| WF | Name | Trigger | Purpose | Status |
|----|------|---------|---------|--------|
| WF2 | Greenhouse — Grafana Alert Relay | Webhook `POST /webhook/gh-alert` | Receives Grafana alerts → formats → Pushover | ✅ Active |
| WF4 | Greenhouse — Daily Evening Summary | Cron 20:00 | Queries InfluxDB → formats daily summary → Pushover | ✅ Active |
| — | Garden — Daily Task Brief (RAG) | Cron 08:00 | POSTs to RAG API (category=season) → Pushover morning brief | ✅ Active (ID: `oxvjEx84Glkz9zY2`) |

**n8n `$env.VAR` access:** Requires `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` in `~/sparc/docker-compose.yml` env block.
Without this, all `$env.PUSHOVER_GH_TOKEN` calls return `access to env vars denied` — silent failure, no Pushover alert.

**Contact point URL (Grafana → n8n):**
- Internal: `http://sparc-n8n:5678/webhook/gh-alert` (both on `sparc` network — preferred)
- External fallback: `http://192.168.68.119:5678/webhook/gh-alert`

---

## Grafana Alert Rules (sparc-grafana, port 3001)

All 5 rules in Folder: `Gardening`, provisioned 2026-05-19.

| UID | Alert | Threshold | Condition |
|-----|-------|-----------|-----------|
| `gh-alert-lvpd-stress` | LVPD stress | lvpd_kpa > 2.0 | 5 min |
| `gh-alert-lvpd-humid` | LVPD low (humid) | lvpd_kpa < 0.2 | 5 min |
| `gh-alert-frost-risk` | Frost risk | temperature_c < 5.0 | 5 min |
| `gh-alert-heat-stress` | Heat stress | temperature_c > 35.0 | 5 min |
| `gh-alert-soil-dry` | Soil dry | moisture_pct < 25.0 | 10 min |

See `Explainers/GRAFANA_EXPLAINED.md` §Alert Rules for full Flux queries and troubleshooting.

---

## Random Forest Irrigation Model (Phase 2 — not yet built)

Designed spec in `Greenhouse/GH_PREDICTIVE_IRRIGATION.md`. Key architecture:

**3-Gate Decision Logic:**
```
Gate 1 — Absolute threshold:   if moisture_pct < 25% → irrigate regardless
Gate 2 — RF prediction:        if P(irrigate|features) > 0.65 → irrigate
Gate 3 — Override:             if outdoor_weather shows rain_prob > 60% → skip
```

**Feature matrix** (from InfluxDB):
- `moisture_pct` (last 24h trend)
- `temp_c`, `lvpd_kpa` (canopy)
- `et0_mm` (evapotranspiration from outdoor_weather)
- `shortwave_wm2` (solar load)
- Day of year, crop zone label

**Training data target:** May → Sep season data. Minimum 60 days. Model retrained annually.
**ADR location:** `Greenhouse/ADR/` (create per ML decision)
**Linear epics:** GARDEN-88 (ML feature matrix), GARDEN-89 (RF model training)

---

## Streamlit Hub (7 pages)

Entry point: `streamlit_hub/app.py` (~2050 lines as of May 2026)
Pages defined by `elif page ==` routing at top of file.

| Page | Key data sources |
|------|----------------|
| 🌡️ Greenhouse Climate | InfluxDB `greenhouse_canopy` + `soil_moisture` |
| 🌿 Plant Health Score | InfluxDB multi-measurement composite |
| 🌦️ Weather & GH Health | Open-Meteo API + `outdoor_weather` + GDD/LGP tracker |
| ⏱️ Time & Outdoor Tracking | `season/TIME_LOG_2026.csv` — log form + pie/bar charts |
| 💰 Business Intelligence | `season/FINANCE_2026.csv` + break-even chart + P&L |
| 🔬 Research & RAG | RAG API (port 7862) |
| 📊 Harvest Log | `biology.db` SQLite (harvest form + variety chart) |

**Editing app.py:** Edit on Mac → `rsync` to NUC → `docker restart gardening-streamlit`. No rebuild needed (volume mount).

**Syntax validation before rsync:**
```bash
python3 -m py_compile "/Users/danalexandrubujoreanu/Personal Projects/Gardening/Greenhouse/digital_twin/streamlit_hub/app.py" && echo "OK"
```

---

## Feedback Loop — How to Verify Changes

```bash
# 1. Check all gardening containers are running
ssh dan@192.168.68.119 "docker ps --filter name=gardening --format 'table {{.Names}}\t{{.Status}}'"

# 2. Check poller wrote data in last 15 min
ssh dan@192.168.68.119 "docker logs gardening-poller --tail 5"

# 3. Check weather poller last run
ssh dan@192.168.68.119 "docker logs gardening-weather-poller --tail 5 2>&1 || echo 'Container not running (expected — batch job)'"

# 4. Verify InfluxDB has recent canopy data
# (query via Flux HTTP — see schema section above)

# 5. RAG health
curl -s http://192.168.68.119:7862/health

# 6. Streamlit health
curl -sf http://192.168.68.119:8501 > /dev/null && echo "Streamlit UP"
```

---

## Key Env Vars (all in `~/gardening/.env` on NUC)

```
INFLUX_URL=http://192.168.68.119:8086
INFLUX_TOKEN=<token>
INFLUX_DATABASE=greenhouse
INFLUX_ORG=maynooth
ECOWITT_APPLICATION_KEY=<key>
ECOWITT_API_KEY=<key>
ECOWITT_DEVICE_MAC=<mac>
GH_LAT=53.38              # Greenhouse latitude (Maynooth)
GH_LON=-6.59              # Greenhouse longitude
GEMINI_API_KEY=<key>      # RAG synthesis
```

Shared API keys also in `~/building-energy-load-forecast/.env` (Mac):
- `LINEAR_API_KEY` — Gardening team: `03df05c2-d8e8-461f-8dd9-d1a1b0014502`
- `INFLUXDB_TOKEN` — same token as `INFLUX_TOKEN` above
