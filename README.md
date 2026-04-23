# Greenhouse Digital Twin — Maynooth Homestead

> **A low-cost, open-source cyber-physical research platform for precision microclimate management in a Zone 8b glass greenhouse (52°N, Ireland).**

*Vitavia Venus 7500 · 8m² · Maynooth, Co. Kildare · Live since April 2026*

---

## Research Context

This project investigates whether consumer-grade IoT hardware and open-source software can replicate the microclimate precision of commercial controlled-environment agriculture (CEA) at micro-scale and sub-€500 hardware cost.

**Core hypothesis:** LVPD-triggered automation (fan, ventilation, irrigation) combined with a weather-sensor fusion ML model can achieve:
- VPD compliance > 90% of daylight hours
- Zero major fungal (Botrytis cinerea) incidents
- Predictive harvest windows accurate to ±5 days via Growing Degree Day accumulation

**Analogue in energy systems:** The same architecture pattern used in building energy load forecasting (weather features → sensor time-series → demand prediction) is applied here to microclimate and yield prediction. See related project: [Sparc Energy Load Forecasting](https://github.com/danbujoreanu).

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA SOURCES                                                        │
│                                                                      │
│  Ecowitt Cloud API  ──────────────────────┐                          │
│  (WH31 canopy + WH51×2 soil, every 5min) │                          │
│                                           ├──► Python Poller         │
│  Open-Meteo API ─────────────────────────┘    PsychrometricEngine    │
│  (53.38°N 6.59°W — no key required)           LVPD = SVP(T−2°C)     │
│  Temperature · RH · VPD · ET₀ · Precip        − SVP(T)×(RH/100)    │
│  Past 7 days + 7-day forecast                                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                        InfluxDB 2.7
                        org: maynooth
                        bucket: greenhouse
                        measurements:
                          greenhouse_canopy (LVPD, temp, RH)
                          soil_moisture (VWC %, 2 zones)
                               │
              ┌────────────────┴────────────────┐
              │                                 │
        Grafana 10                    Streamlit Hub
        Port 3000                     Port 8501
        5 alert rules:                7 pages:
        · Botrytis risk               · Live Greenhouse
        · Heat stress                 · Production tracking
        · Water stress                · Season Pipeline (Gantt)
        · Soil low (N+S)              · Time & ROI
        → n8n → Pushover alerts       · Finance
                                      · Weather & GH Health ← NEW
                                      · Ask the Garden (RAG)
              │
        Phase 3 (Jul 2026)
        Random Forest model
        Features: LVPD · soil · outdoor_temp · hour · DOY · GDD
        Target: soil_moisture T+60min · harvest_window
              │
        Phase 4 (Aug 2026)
        WFC01 Smart Valve · autonomous irrigation
```

---

## Sensor Stack

| Sensor | Model | Location | Measures | Freq |
|--------|-------|----------|----------|------|
| Hub | GW3000 (ethernet) | Indoor | API gateway · outdoor ref | — |
| Canopy air | WH31 | GH canopy at 1.2m | Temp (°C) · RH (%) | 60s |
| Soil N bed | WH51 | GH4 North bed | Volumetric water content (%) | 60s |
| Soil S bed | WH51 | GH4 South bed | Volumetric water content (%) | 60s |
| Fan automation | AC1100 WittSwitch | GH power socket | 868MHz RF relay | event |
| Smart valve | WFC01 (planned) | GH4 manifold | 2-zone irrigation | Phase 4 |

**RF:** 868 MHz sub-GHz (better canopy penetration than 2.4 GHz)

---

## Weather Integration — Open-Meteo

**Source:** [open-meteo.com](https://open-meteo.com) — free, no API key, 1h resolution  
**Location:** 53.38°N, 6.59°W (Maynooth, Co. Kildare)  
**Variables ingested:**

| Variable | Unit | ML role |
|----------|------|---------|
| `temperature_2m` | °C | GDD accumulation · outdoor baseline |
| `relative_humidity_2m` | % | Outdoor VPD calculation |
| `vapour_pressure_deficit` | kPa | Direct comparison with indoor LVPD |
| `et0_fao_evapotranspiration` | mm/hr | FAO-56 Penman-Monteith reference ET₀ |
| `precipitation` | mm | Outdoor bed irrigation logic |
| `windspeed_10m` | km/h | Ventilation strategy context |

**Coverage:** Past 7 days (historical model analysis) + 7-day forecast, updated every 30 minutes in the Streamlit hub.

**Why it matters for ML:** Outdoor temperature drives the GH thermal envelope (glass conducts). Outdoor VPD is a primary covariate for indoor LVPD prediction, particularly at night when vents are closed. ET₀ provides a calibrated reference for comparing GH transpiration demand. This is the same feature engineering approach used in building energy demand forecasting — exogenous weather features improve model generalisation beyond the training season.

---

## LVPD — The Core Metric

Leaf Vapour Pressure Deficit (Leaf VPD) is the driving force for plant transpiration. Unlike raw relative humidity, it incorporates temperature to give a direct measure of the gradient between leaf and air water vapour content.

```python
def saturation_vapor_pressure(T_c: float) -> float:
    """Tetens formula. Returns kPa."""
    return 0.6108 * math.exp(17.27 * T_c / (T_c + 237.3))

def calc_lvpd(T_air: float, rh: float, leaf_offset: float = 2.0) -> float:
    """
    LVPD = SVP(T_leaf) − SVP(T_air) × (RH/100)
    T_leaf = T_air − 2°C (standard greenhouse canopy approximation)
    Negative = condensation on leaves (Botrytis cinerea activation zone)
    """
    T_leaf = T_air - leaf_offset
    return round(saturation_vapor_pressure(T_leaf) - saturation_vapor_pressure(T_air) * (rh / 100), 4)
```

| Zone | LVPD (kPa) | Condition | Automated response |
|------|------------|-----------|-------------------|
| Condensing 🌫️ | < 0.0 | Dew on leaves | Fan ON via AC1100 |
| Too Humid 💧 | 0.0 – 0.4 | Near-saturation | Fan ON · vent |
| Low ↓ | 0.4 – 0.8 | Suboptimal | Monitor |
| ✅ Optimal | 0.8 – 1.2 | Ideal transpiration | — |
| High ↑ | 1.2 – 1.5 | Elevated stress | Consider misting |
| Stress ☀️ | > 1.5 | Stomata closing | Irrigate |

**Empirical validation (Apr 20–23, 2026):** Analysis of 42 hourly readings showed RH-only alarms (>85% threshold) miss 33% of LVPD risk hours — specifically the evening cool-down (19:00–21:00) and morning warm-up transitions. These are the highest-risk periods for Botrytis spore activation.

Reference: Körner et al. (2008), *Crop Science* — optimal GH VPD 0.5–1.0 kPa for Solanaceae.

---

## Plant Environment Health Score

A composite weekly metric calculated from InfluxDB time-series:

```
PHScore = (hours_LVPD_optimal / total_hours) × 0.60
        + (hours_soil_VWC_≥35% / total_hours) × 0.40
```

LVPD weighted at 60% — disease risk is more acute and irreversible than mild drought stress. Displayed on the Streamlit "Weather & GH Health" page alongside a distribution histogram and zone breakdown.

---

## Growing Degree Days (GDD) Tracker

Harvest window prediction using FAO-56 accumulated heat units:

```
GDD_daily = max(0, (T_max + T_min) / 2 − 10)  [base 10°C — standard Solanaceae]
```

Accumulated from sowing date using Open-Meteo hourly temperature. Progress tracked against variety-specific targets (Johnny's Seeds data + The Market Gardener methodology). 9 varieties tracked live.

**Research interest:** GDD-to-harvest accuracy improves with each season of labelled data (actual harvest date vs GDD prediction). By 2027, the model will have a full growing season to validate against.

---

## ML Roadmap

This project is building toward a full-season predictive model — analogous to energy load demand forecasting where weather is the primary exogenous feature:

| Phase | Target | Status |
|-------|--------|--------|
| **Data collection** | 90+ days of LVPD + soil + weather aligned | 🔵 Building Apr–Jun 2026 |
| **Feature engineering** | GDD · VPD delta (outdoor−indoor) · ET₀ · hour-of-day · DOY | 🔵 Designed |
| **Random Forest (Phase 3)** | Soil moisture T+60min prediction · R² > 0.85 | 🔵 Jul 2026 |
| **Harvest window model** | GDD regression → harvest date ± 5 days | 🔵 Aug 2026 |
| **Autonomous irrigation** | 3-gate logic → WFC01 valve (LVPD + soil + rain gate) | 🔵 Aug 2026 |
| **Full-year model** | 2026 labelled dataset → 2027 predictions | 🔵 Winter 2026/27 |

**Dataset being assembled:** Hourly observations of T_canopy, RH_canopy, LVPD, soil_N, soil_S, outdoor_temp, outdoor_RH, outdoor_VPD, ET₀, GDD_cumulative, precipitation. Harvest events labelled with variety, zone, weight, quality. This is the training set for the 2027 model.

---

## Digital Twin Maturity Ladder

| Level | Capability | Status |
|-------|-----------|--------|
| 1 | Sensors live · Ecowitt Cloud API | ✅ Apr 2026 |
| 2 | Docker stack · LVPD calculated · InfluxDB + Grafana | ✅ Apr 2026 |
| 2.5 | Fan automated via AC1100 (RH trigger) · intel_garden RAG | ✅ Apr 2026 |
| 3 | Weather integration · Plant Health Score · GDD tracker | ✅ Apr 2026 |
| 3.5 | Backfill pipeline · 90-day training dataset complete | 🔵 Jun 2026 |
| 4 | Random Forest · soil prediction · harvest window model | 🔵 Jul 2026 |
| 5 | WFC01 autonomous irrigation · 3-gate logic | 🔵 Aug 2026 |
| 6 | Streamlit served via Tailscale from Mac Mini M5 | 🔵 Jul–Aug 2026 |
| 7 | Full 2026 dataset labelled → 2027 model trained | 2027 |

---

## Plant Knowledge RAG

A Retrieval-Augmented Generation system for agronomic queries, built on the project's own documentation:

- **Stack:** LlamaIndex · ChromaDB · MiniLM-L6-v2 embeddings
- **Corpus:** 7 documents · 73 chunks (GH Climate & VPD · Agronomic Playbook · Crop Targets · Predictive Irrigation · GH Specs · Market Gardener notes · Master Inventory)
- **Query:** `python intel/query_mba.py "your question" garden`
- **Interface:** Streamlit "Ask the Garden" page with 6 example prompts

---

## Stack

| Service | Image / Library | Purpose |
|---------|----------------|---------|
| InfluxDB | `influxdb:2.7-alpine` | Time-series: sensors + harvest events |
| Grafana | `grafana/grafana:latest` | Operational dashboards · 5 alert rules |
| Ecowitt Poller | Python · `influxdb-client` | Cloud API → LVPD → InfluxDB |
| Open-Meteo | REST API (no key) | Outdoor weather · forecast · ET₀ · GDD |
| Streamlit Hub | Python · Plotly | 7-page unified dashboard |
| LlamaIndex + ChromaDB | Python · MiniLM-L6-v2 | Plant knowledge RAG |
| n8n | Docker (Sparc stack) | Alert routing → Pushover |
| scikit-learn | Python | Random Forest (Phase 3, Jul 2026) |

---

## Quick Start

```bash
# 1. Configure environment
cp .env.example .env
# Fill in ECOWITT_APPLICATION_KEY, ECOWITT_API_KEY, ECOWITT_DEVICE_MAC

# 2. Start the Docker stack
docker compose up -d

# 3. Run the Streamlit hub
pip install -r streamlit_hub/requirements.txt
streamlit run streamlit_hub/app.py

# Access:
# Streamlit:  http://localhost:8501
# Grafana:    http://localhost:3000
# InfluxDB:   http://localhost:8086

# 4. (Optional) Recover data gaps
python poller/backfill_ecowitt.py --from 2026-04-18 --to 2026-04-20
```

---

## Harvest Logging

```bash
python log_harvest.py harvest "Sungold F1" --kg 0.35 --zone GH4N --quality 5
python log_harvest.py summary   # → produce value (€) + effective hourly return (€/hr)
```

---

## Dataset

The greenhouse dataset (InfluxDB bucket `greenhouse`) contains:

- **Measurement `greenhouse_canopy`:** fields `temperature_c`, `humidity_pct`, `lvpd_kpa`, `lvpd_zone` — 5-minute resolution from April 2026
- **Measurement `soil_moisture`:** fields `moisture_pct` — tagged by zone (GH4N / GH4S) — 5-minute resolution
- **CSV logs:** `Season/HARVEST_LOG_2026.csv`, `Season/TIME_LOG_2026.csv`, `Season/FINANCE_2026.csv`

Backfill script (`poller/backfill_ecowitt.py`) recovers gaps from Ecowitt Cloud (30-day rolling history available). No gaps in the dataset since Apr 21, 2026.

---

## Project Context

Part of the Maynooth Homestead zero-waste vegetarian food production system. The Digital Twin component is the instrumentation and intelligence layer — sensor data, microclimate management, and production analytics.

**Architecture parallel:** [Sparc Energy Load Forecasting](https://github.com/danbujoreanu) applies the same IoT → time-series → ML pipeline to energy demand prediction in buildings. The feature engineering approach (exogenous weather features, temporal encoding, 80/20 time-series split) is directly transferable.

**Research trajectory:** 2026 season builds the dataset. 2027 season validates the model. Target publications: *Computers and Electronics in Agriculture*, *Biosystems Engineering*.
