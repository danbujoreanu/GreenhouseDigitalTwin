# Greenhouse Digital Twin — Command Runbook

> Every command you'll ever need, with context. Read this when you've forgotten how something works.

---

## Environment Setup (first time only)

```bash
# 1. Clone or navigate to the repo
cd "Personal Projects/Gardening/Greenhouse/digital_twin"

# 2. Create your .env from the example
cp .env.example .env
# Edit .env and fill in (already done — these are your values):
#   ECOWITT_API_KEY=713783f4-645e-4b35-9eed-5a03069ddc32
#   ECOWITT_APPLICATION_KEY=CE4AAC42E8EEED6BAAB6829BCF9DF862
#   ECOWITT_DEVICE_MAC=28:56:2F:6A:27:BF
```

---

## Docker Stack (InfluxDB + Grafana + Poller)

### First-time setup: install Docker Desktop
> InfluxDB and Grafana run inside Docker. Docker must be running before any of these commands.
> Download: https://www.docker.com/products/docker-desktop/
> On Mac: open Docker Desktop app → wait for the whale icon in menu bar to stop animating

```bash
# Verify Docker is running
docker --version
docker compose version
```

### Start the full stack
```bash
cd "Personal Projects/Gardening/Greenhouse/digital_twin"
docker compose up -d

# What this does:
# - Starts InfluxDB (port 8086) — initialises on first run with org/bucket from .env
# - Starts Grafana (port 3000) — auto-provisions InfluxDB datasource
# - Starts Ecowitt poller — polls Ecowitt Cloud API every 5 min, calculates LVPD, writes to InfluxDB
```

### Check it's working
```bash
docker compose ps                  # all 3 services should be "running"
docker compose logs -f poller      # watch live: should see "Canopy → 22.3°C RH 68% LVPD 0.82 kPa"
docker compose logs influxdb       # should end with "Listening on port 8086"
```

### Access dashboards
```
Grafana:   http://localhost:3000   login: admin / maynooth_gh_2026
InfluxDB:  http://localhost:8086   login: admin / maynooth_gh_2026
```

### Stop / restart
```bash
docker compose down              # stop (keeps all data)
docker compose down -v           # stop AND wipe all data (fresh start)
docker compose restart poller    # restart just the poller (e.g. after code change)
docker compose build poller      # rebuild poller image after editing poller.py
docker compose up -d poller      # start just the poller
```

### Update poller code
```bash
# After editing poller/poller.py:
docker compose build poller
docker compose up -d poller
docker compose logs -f poller    # verify new code is running
```

---

## Streamlit Dashboard (no Docker needed)

### Install
```bash
pip install streamlit pandas plotly requests python-dotenv
```

### Run
```bash
cd "Personal Projects/Gardening/Greenhouse/digital_twin"
streamlit run streamlit_hub/app.py
# Opens at http://localhost:8501
```

### Run with InfluxDB connected
```bash
# First start the Docker stack (see above), then:
INFLUX_URL=http://localhost:8086 streamlit run streamlit_hub/app.py
```

---

## Harvest Logging

```bash
cd "Personal Projects/Gardening"

# Log a harvest by weight
python Greenhouse/digital_twin/log_harvest.py harvest "San Marzano" --kg 0.45 --zone GH2N --quality 5

# Log a harvest by count (e.g. cucumbers)
python Greenhouse/digital_twin/log_harvest.py harvest "Passandra F1" --count 3 --zone GH1N

# Log with notes
python Greenhouse/digital_twin/log_harvest.py harvest "Sungold F1" --kg 0.18 --zone GH4N --quality 5 --notes "First harvest of season"

# Also write to InfluxDB (when Docker stack is running)
python Greenhouse/digital_twin/log_harvest.py harvest "Black Krim" --kg 0.6 --zone GH3N --influx

# Season summary: total produce value + hours + effective €/hr
python Greenhouse/digital_twin/log_harvest.py summary

# Import all CSV harvest data into InfluxDB
python Greenhouse/digital_twin/log_harvest.py import-to-influx
```

---

## Time Logging

```bash
cd "Personal Projects/Gardening"

# Log a Claude session
python Greenhouse/digital_twin/log_harvest.py time --category claude_session --minutes 90 --activity "VPD dashboard build"

# Log physical garden time
python Greenhouse/digital_twin/log_harvest.py time --category physical_garden --minutes 180 --activity "Tomato cordon training + side-shooting"

# Log an infrastructure build
python Greenhouse/digital_twin/log_harvest.py time --category infrastructure --minutes 120 --activity "Brassica cage Bay 4"

# Categories: claude_session | physical_garden | infrastructure | planning | research
```

---

## Git / GitHub

### First push (already done)
```bash
cd "Personal Projects/Gardening/Greenhouse/digital_twin"
git init
git remote add origin https://github.com/danbujoreanu/GreenhouseDigitalTwin.git
git add .
git commit -m "Initial Digital Twin stack: InfluxDB + Grafana + Ecowitt poller + LVPD engine"
git push -u origin main
```

### Day-to-day
```bash
cd "Personal Projects/Gardening/Greenhouse/digital_twin"
git status                          # see what changed
git add poller/poller.py            # stage specific file
git add -A                          # stage everything (check git status first)
git commit -m "Add harvest logging CLI"
git push                            # push to GitHub
```

---

## Plant Knowledge RAG (intel_garden tier)

> Requires the Energy project's LlamaIndex + ChromaDB infrastructure.

```bash
# Query the garden RAG (once populated)
cd ~/building-energy-load-forecast
~/miniconda3/envs/ml_lab1/bin/python \
  "/Users/danalexandrubujoreanu/Personal Projects/Gardening/AI/query_mba.py" \
  "optimal soil temperature jalapeño fruit set" garden

# Ingest new documents
~/miniconda3/envs/ml_lab1/bin/python scripts/intel_ingest.py \
  --dir "/Users/danalexandrubujoreanu/Personal Projects/Gardening/intel/docs/garden" \
  --tier garden
```

---

## Ecowitt API (manual queries)

```bash
# Test your credentials — list all devices
curl "https://api.ecowitt.net/api/v3/user/device/list?\
application_key=CE4AAC42E8EEED6BAAB6829BCF9DF862\
&api_key=713783f4-645e-4b35-9eed-5a03069ddc32" | python3 -m json.tool

# Fetch real-time sensor data
curl "https://api.ecowitt.net/api/v3/device/real_time?\
application_key=CE4AAC42E8EEED6BAAB6829BCF9DF862\
&api_key=713783f4-645e-4b35-9eed-5a03069ddc32\
&mac=28:56:2F:6A:27:BF\
&call_back=all\
&temp_unitid=1" | python3 -m json.tool
```

---

## InfluxDB Queries (Flux language)

> Run these in Grafana → Explore, or in InfluxDB UI → Data Explorer.

```flux
// Last 24 hours of greenhouse canopy data
from(bucket: "greenhouse")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "greenhouse_canopy")

// Current LVPD value
from(bucket: "greenhouse")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == "greenhouse_canopy" and r._field == "lvpd_kpa")
  |> last()

// Soil moisture both zones, last 7 days
from(bucket: "greenhouse")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "soil_moisture")

// Cumulative harvest by variety
from(bucket: "greenhouse")
  |> range(start: -1y)
  |> filter(fn: (r) => r._measurement == "harvest" and r._field == "weight_kg")
  |> group(columns: ["variety"])
  |> sum()
```

---

## Troubleshooting

### Poller says "WH31 canopy data missing"
The Ecowitt API uses different channel names depending on how many sensors are registered.
Run this to see what keys your device returns:
```bash
docker compose logs poller | grep "API keys in response"
```
Common fix: edit `poller/poller.py` → `parse_and_build_points()`, look for `wh31_ch1` and try the channel name shown in the log.

### InfluxDB won't start after `docker compose up -d`
It's probably still initialising (takes 20-30s on first run).
```bash
docker compose logs influxdb | tail -5    # look for "Listening on port 8086"
```

### "Error: INFLUX_TOKEN not set"
You're running log_harvest.py with `--influx` but the Docker stack isn't running.
Either start the stack (`docker compose up -d`) or omit `--influx` to log to CSV only.

### "application_key required" from Ecowitt API
Your .env is missing ECOWITT_APPLICATION_KEY. Check: `cat .env | grep APPLICATION`.
