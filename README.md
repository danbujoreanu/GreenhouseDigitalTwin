# Greenhouse Digital Twin — Maynooth Homestead

> A low-cost, open-source Digital Twin for a 8m² glass greenhouse in Zone 8b Ireland.
> Sensor data → LVPD calculation → InfluxDB → Grafana → ML irrigation prediction.

**Live system:** Vitavia Venus 7500 | Maynooth, Co. Kildare | Zone 8b | 52°N

---

## Architecture

```
Ecowitt Cloud API  ──────────────────────────────────────────────┐
(poll every 5 min)                                               │
    ↓                                                            │
Python Poller                                              Phase 1 (now)
  └─ PsychrometricEngine (LVPD calc)                            │
    ↓                                                            │
InfluxDB 2.7  ◄──────────────────────────────────────────────────┘
    ↓
Grafana (dashboards + alerts)
    ↓
Streamlit Hub (harvest log, ROI, RAG interface)    ← Phase 2
    ↓
Random Forest ML model (irrigation prediction)     ← Phase 3 (Jul)
    ↓
WFC01 Smart Valve (autonomous irrigation)          ← Phase 4 (Aug)
```

**Sensor stack:**
- GW3000 hub (ethernet, Ecowitt Cloud API)
- WH31 — canopy air temp + relative humidity
- WH51 × 2 — soil volumetric water content (GH4N + GH4S beds)

---

## Stack

| Service | Image | Purpose |
|---------|-------|---------|
| InfluxDB | `influxdb:2.7-alpine` | Time-series storage for all sensor + harvest data |
| Grafana | `grafana/grafana:latest` | Operational dashboards + alerting |
| Ecowitt Poller | Custom Python | Cloud API → LVPD calc → InfluxDB write |
| Streamlit Hub | Python | Unified dashboard: sensors + production + ROI + RAG |

---

## Quick Start

### Prerequisites
- Docker Desktop installed
- Ecowitt account with GW3000 sensor registered

### 1. Configure environment
```bash
cp .env.example .env
# Edit .env: add ECOWITT_APPLICATION_KEY and ECOWITT_DEVICE_MAC
# (See SETUP.md for how to get these from app.ecowitt.net)
```

### 2. Start the stack
```bash
docker compose up -d
```

### 3. Access dashboards
- **Grafana:** http://localhost:3000 (admin / see .env)
- **InfluxDB:** http://localhost:8086

### 4. Run the Streamlit hub (no Docker needed)
```bash
pip install -r streamlit_hub/requirements.txt
streamlit run streamlit_hub/app.py
```

---

## LVPD Zones

| LVPD (kPa) | Zone | Action |
|------------|------|--------|
| < 0.4 | TOO_HUMID | Open vent — botrytis risk |
| 0.4 – 0.8 | SUBOPTIMAL_LOW | Monitor |
| 0.8 – 1.2 | ✅ OPTIMAL | No action |
| 1.2 – 1.5 | SUBOPTIMAL_HIGH | Consider misting |
| > 1.5 | STRESS | Irrigate |

---

## Harvest Logging
```bash
python log_harvest.py harvest "San Marzano" --kg 0.45 --zone GH2N --quality 5
python log_harvest.py summary   # → produce value + hourly ROI
```

---

## Digital Twin Maturity Ladder

| Level | Description | Status |
|-------|-------------|--------|
| 1 | Sensors live, Ecowitt Cloud | ✅ Apr 2026 |
| 2 | Docker stack + LVPD calculated | 🔵 May 2026 |
| 3 | Historical dataset + irrigation log | 🔵 Jun 2026 |
| 4 | Random Forest model trained | 🔵 Jul 2026 |
| 5 | WFC01 autonomous irrigation | 🔵 Aug 2026 |
| 6 | Fan automated on VPD threshold | 🔵 Aug 2026 |
| 7 | Full predict → act → learn loop | 2027 |

---

## Project context
Part of the Maynooth Homestead zero-waste vegetarian food production system.
Season tracking, sowing schedules, and grow logs managed separately in the project vault.

**Related:** [Sparc Energy Load Forecasting](https://github.com/danbujoreanu) — same IoT + ML architecture pattern at enterprise scale.
