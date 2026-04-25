"""
Maynooth Homestead Intelligence Hub
====================================
Unified dashboard: live sensors · production · pipeline · ROI · finance

Run:  streamlit run streamlit_hub/app.py
"""

import os
import math
import csv
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from dotenv import load_dotenv

# ── Path resolution ───────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent        # -> digital_twin/
SEASON = ROOT.parent.parent / "Season"     # -> Gardening/Season/
load_dotenv(ROOT / ".env")

# ── Ecowitt credentials ───────────────────────────────────────────────────────
ECOWITT_APP_KEY = os.getenv("ECOWITT_APPLICATION_KEY", "")
ECOWITT_API_KEY = os.getenv("ECOWITT_API_KEY", "")
DEVICE_MAC      = os.getenv("ECOWITT_DEVICE_MAC", "")

# ── Pushover (user-triggered notifications from Streamlit) ────────────────────
PUSHOVER_GH_TOKEN  = os.getenv("PUSHOVER_GH_TOKEN", "")
PUSHOVER_USER_KEY  = os.getenv("PUSHOVER_USER_KEY", "")
FAN_INSTALLED      = os.getenv("FAN_INSTALLED", "false").lower() == "true"

def send_pushover(message: str, title: str = "🌿 Greenhouse", priority: int = 0) -> bool:
    """Send a Pushover notification via the DT Greenhouse app.
    priority: -1=quiet, 0=normal, 1=high. Returns True on success."""
    if not PUSHOVER_GH_TOKEN or not PUSHOVER_USER_KEY:
        return False
    try:
        r = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": PUSHOVER_GH_TOKEN, "user": PUSHOVER_USER_KEY,
                  "title": title, "message": message, "priority": priority},
            timeout=5
        )
        return r.json().get("status") == 1
    except Exception:
        return False

# ── Open-Meteo: current conditions (shared across pages) ─────────────────────
# WMO weather code → (label, emoji)  — subset covering Irish conditions
_WMO = {
    0: ("Clear sky", "☀️"), 1: ("Mainly clear", "🌤️"), 2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"), 45: ("Fog", "🌫️"), 48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"), 53: ("Drizzle", "🌦️"), 55: ("Heavy drizzle", "🌧️"),
    61: ("Light rain", "🌧️"), 63: ("Rain", "🌧️"), 65: ("Heavy rain", "🌧️"),
    80: ("Rain showers", "🌧️"), 81: ("Showers", "🌧️"), 82: ("Heavy showers", "⛈️"),
    95: ("Thunderstorm", "⛈️"), 96: ("Thunderstorm + hail", "⛈️"),
}

_GH_LAT = os.getenv("GH_LATITUDE", "53.38")
_GH_LON = os.getenv("GH_LONGITUDE", "-6.59")

@st.cache_data(ttl=300)
def fetch_ecowitt():
    """Fetch live sensor data from Ecowitt Cloud API. 5-min cache."""
    if not all([ECOWITT_APP_KEY, ECOWITT_API_KEY, DEVICE_MAC]):
        return None, "Missing credentials in .env"
    try:
        r = requests.get(
            "https://api.ecowitt.net/api/v3/device/real_time",
            params={
                "application_key": ECOWITT_APP_KEY, "api_key": ECOWITT_API_KEY,
                "mac": DEVICE_MAC, "call_back": "all",
                "cycle_type": "auto", "temp_unitid": 1,
            },
            timeout=10
        )
        body = r.json()
        if body.get("code") != 0:
            return None, f"API error {body.get('code')}: {body.get('msg')}"
        return body.get("data", {}), None
    except Exception as e:
        return None, str(e)

@st.cache_data(ttl=1800)
def fetch_current_weather():
    """Fetch current + 7-day hourly from Open-Meteo. No API key. 30-min cache."""
    import urllib.request as _req
    import io as _io
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={_GH_LAT}&longitude={_GH_LON}"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation,"
        "windspeed_10m,vapour_pressure_deficit,et0_fao_evapotranspiration,"
        "shortwave_radiation,weather_code"
        "&current_weather=true"
        "&past_days=7&forecast_days=7&timezone=Europe%2FDublin"
        "&temperature_unit=celsius&windspeed_unit=kmh&precipitation_unit=mm"
    )
    try:
        with _req.urlopen(url, timeout=10) as r:
            import json as _json
            raw = _json.loads(r.read())
        current = raw.get("current_weather", {})
        df = pd.DataFrame(raw["hourly"])
        df["time"] = pd.to_datetime(df["time"])
        df["is_forecast"] = df["time"] > pd.Timestamp.now(tz="Europe/Dublin").tz_localize(None)
        return current, df, None
    except Exception as e:
        return {}, pd.DataFrame(), str(e)

# ── LVPD engine ───────────────────────────────────────────────────────────────
def svp(T): return 0.6108 * math.exp(17.27 * T / (T + 237.3))
def calc_lvpd(T, rh, offset=2.0):
    return round(svp(T - offset) - svp(T) * (rh / 100), 3)

LVPD_ZONES = [
    (-9.9, 0.0,  "Condensing 🌫️",   "#7c3aed",  "Condensation on leaves — botrytis high risk"),
    (0.0,  0.4,  "Too Humid 💧",    "#ef4444",  "Vent — botrytis risk"),
    (0.4,  0.8,  "Low ↓",           "#f97316",  "Monitor"),
    (0.8,  1.2,  "✅ Optimal",       "#22c55e",  "No action"),
    (1.2,  1.5,  "High ↑",          "#f97316",  "Consider misting"),
    (1.5,  9.9,  "Stress ☀️",        "#ef4444",  "Irrigate now"),
]

def lvpd_zone(v):
    for lo, hi, label, color, action in LVPD_ZONES:
        if lo <= v < hi:
            return label, color, action
    return "Unknown", "#6b7280", ""

# ── Produce value table ───────────────────────────────────────────────────────
# Prices: Irish supermarket / specialty shop, organic where available — April 2026
# Tomato prices are variety-specific; cherry commands 3× the price of plum.
PRICES = {
    # Tomatoes — confirmed Irish market prices
    "San Marzano":            4.00,  # Organic plum/paste. Tesco organic plum ~€4/kg
    "Black Krim":             7.00,  # Heirloom beefsteak. Specialty/farmers market €6–8/kg (rare in Ireland)
    "Marmande":               5.00,  # Ribbed beef tomato. Organic beef ~€4–6/kg
    "Sungold F1":            12.00,  # Premium cherry. Tesco 250g punnet = €3.00 → €12/kg
    "Tigerella":              6.00,  # Striped specialty. Farmers market €5–7/kg
    "Smarald":                4.50,  # Romanian heirloom. Estimated specialty plum level
    "Prima Bella":            5.00,  # Polish heirloom. Specialty ~€5/kg
    # Cucumbers
    "Passandra F1":           5.00,  # Mini cucumber. Organic 3-pack ~€2.50 → ~€5/kg
    # Capsicums / Aubergines
    "Jalapeño Ruben":        14.00,  # Fresh organic jalapeño very rare in IE — specialty €12–16/kg
    "Yolo Wonder":            4.50,  # Red bell pepper. Tesco organic €4–5/kg
    "Pantos":                 5.00,  # Specialty Romanian pepper
    "Tsaksoniki Aubergine":   4.50,  # Organic aubergine. Tesco ~€4–5/kg
    # Legumes
    "Kelvedon Wonder":        6.00,  # Fresh peas. Organic fresh peas are expensive ~€5–7/kg
    "Aquadulce Claudia":      5.00,  # Fresh broad beans. ~€5/kg
    "Dalmaziano":             9.00,  # Organic dry borlotti beans. ~€7–10/kg
    "Cobra":                  6.00,  # Fresh climbing beans. ~€5–7/kg
    # Squash / Courgette
    "Defender F1":            2.50,  # Courgette. Cheap in season ~€2–3/kg
    "Uchiki Kuri":            2.00,  # Winter squash. ~€2/kg
    # Leafy greens
    "Kale":                   4.00,  # Organic kale. Tesco organic bag ~€1.80/200g → ~€9/kg; say €4 loose
    "Spinach Matador":        5.00,  # Organic spinach. Very expensive per kg ~€5–8/kg
    # Herbs (high value/kg — sold in tiny quantities)
    "Basil":                 20.00,  # Tesco fresh basil €1.50/30g → €50/kg; say €20 realistic
    "Dill":                  15.00,  # Fresh dill. Specialty ~€15/kg
    "Parsley":               10.00,  # Fresh Italian parsley ~€10/kg
}

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Maynooth Homestead",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Design system: Dark sidebar · Light content · Forest green accents ──────
# Palette: bg #f9fafb · text #111827 · primary #15803d · border #e5e7eb
# Sidebar: #162d1f (dark) · sidebar text: #c8e6d4
# WCAG AA compliant: primary on white = 5.3:1 ✓ · sidebar text on sidebar bg = 6.1:1 ✓
st.markdown("""
<style>
    /* ── Hide built-in nav ── */
    [data-testid="stSidebarNav"] { display: none; }

    /* ── SIDEBAR — dark forest ── */
    [data-testid="stSidebar"] {
        background: #162d1f !important;
        border-right: 1px solid #1e3d28;
    }
    [data-testid="stSidebar"] p,
    [data-testid="stSidebar"] label,
    [data-testid="stSidebar"] span,
    [data-testid="stSidebar"] div { color: #c8e6d4 !important; }
    [data-testid="stSidebar"] h1,
    [data-testid="stSidebar"] h2,
    [data-testid="stSidebar"] h3,
    [data-testid="stSidebar"] strong { color: #6ee7a0 !important; }
    [data-testid="stSidebar"] hr { border-color: #1e3d28 !important; }
    /* Radio buttons in sidebar */
    [data-testid="stSidebar"] [data-testid="stMarkdown"] { color: #c8e6d4 !important; }

    /* ── MAIN CONTENT — metric cards ── */
    [data-testid="metric-container"] {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 10px;
        padding: 1rem 1.25rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    }
    [data-testid="stMetricLabel"] {
        font-size: 0.72rem !important;
        font-weight: 600 !important;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        color: #6b7280 !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 1.65rem !important;
        font-weight: 700;
        color: #111827 !important;
    }
    [data-testid="stMetricDelta"] { font-size: 0.8rem !important; }

    /* ── STATUS PILLS ── */
    .status-pill {
        display: inline-block;
        padding: 3px 11px;
        border-radius: 14px;
        font-size: 0.76rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        margin: 2px 1px;
    }
    /* Sidebar context — dark background pills */
    .pill-ok   { background: #14532d; color: #86efac; border: 1px solid #166534; }
    .pill-warn { background: #78350f; color: #fde68a; border: 1px solid #92400e; }
    .pill-bad  { background: #7f1d1d; color: #fca5a5; border: 1px solid #991b1b; }
    .pill-info { background: #1e3a5f; color: #93c5fd; border: 1px solid #1d4ed8; }

    /* ── INFO BANNER (light context) ── */
    .info-banner {
        background: #f0fdf4;
        border-left: 4px solid #15803d;
        padding: 12px 18px;
        border-radius: 0 8px 8px 0;
        color: #14532d;
        font-size: 0.88rem;
        margin: 12px 0;
        line-height: 1.55;
    }

    /* ── SCORE CARD (big number) ── */
    .score-card {
        background: #ffffff;
        border: 1px solid #e5e7eb;
        border-radius: 14px;
        padding: 1.5rem 2rem;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }
    .score-number { font-size: 3rem; font-weight: 800; line-height: 1; }
    .score-label  { font-size: 0.8rem; color: #6b7280; text-transform: uppercase;
                    letter-spacing: 0.06em; margin-top: 6px; }

    /* ── BUTTONS ── */
    .stButton > button {
        border: 1px solid #d1d5db;
        border-radius: 8px;
        font-weight: 500;
        color: #374151;
        background: #ffffff;
        transition: all 0.12s ease;
    }
    .stButton > button:hover {
        border-color: #15803d;
        background: #f0fdf4;
        color: #14532d;
    }

    /* ── SIDEBAR BUTTON override ── */
    [data-testid="stSidebar"] .stButton > button {
        background: #1e3d28;
        border-color: #2d6a4f;
        color: #c8e6d4;
    }
    [data-testid="stSidebar"] .stButton > button:hover {
        background: #2d6a4f;
        color: #f0fdf4;
    }

    /* ── DATAFRAMES ── */
    [data-testid="stDataFrame"] {
        border: 1px solid #e5e7eb !important;
        border-radius: 8px;
        overflow: hidden;
    }

    /* ── TEXT INPUT ── */
    [data-testid="stTextInput"] input {
        border: 1px solid #d1d5db !important;
        border-radius: 8px !important;
    }
    [data-testid="stTextInput"] input:focus {
        border-color: #15803d !important;
        box-shadow: 0 0 0 3px rgba(21,128,61,0.12) !important;
    }

    /* ── EXPANDERS ── */
    [data-testid="stExpander"] {
        border: 1px solid #e5e7eb !important;
        border-radius: 8px !important;
    }
</style>
""", unsafe_allow_html=True)

# ── Sidebar navigation ────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🌱 Homestead Hub")
    st.caption("Maynooth · Zone 8b · 52°N")
    st.divider()
    page = st.radio(
        "Navigate",
        ["🌡️ Live Greenhouse", "🌱 Production", "📋 Season Pipeline",
         "⏱️ Time & ROI", "💰 Finance", "🌤️ Weather & GH Health",
         "🤖 Ask the Garden", "👁️ Vision & Phenology"],
        label_visibility="collapsed"
    )
    st.divider()
    # Quick status strip
    st.markdown("**System status**")
    st.markdown('<span class="status-pill pill-ok">🟢 Sensors live</span>', unsafe_allow_html=True)
    st.markdown('<span class="status-pill pill-ok">🟢 RAG ready</span>', unsafe_allow_html=True)
    if FAN_INSTALLED:
        st.markdown('<span class="status-pill pill-info">🔵 Fan: AC1100 paired</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="status-pill" style="background:#f3f4f6;color:#6b7280;border:1px solid #e5e7eb">⏳ Fan: not yet installed</span>', unsafe_allow_html=True)
    st.divider()
    st.caption(f"Refreshed: {datetime.now().strftime('%H:%M:%S')}")
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — LIVE GREENHOUSE
# ═══════════════════════════════════════════════════════════════════════════════
if page == "🌡️ Live Greenhouse":
    st.title("🌡️ Live Greenhouse Status")

    # fetch_ecowitt() defined at module level
    data, err = fetch_ecowitt()

    if err:
        st.error(f"⚠️ Ecowitt API: {err}")
        st.info("Make sure ECOWITT_APPLICATION_KEY, ECOWITT_API_KEY, and ECOWITT_DEVICE_MAC are set in .env")
        # Show demo values when API unavailable
        st.markdown("### Demo values (API not connected)")
        data = {}

    def safe(d, *keys):
        for k in keys:
            if not isinstance(d, dict): return None
            d = d.get(k)
        try: return float(d) if d else None
        except: return None

    # Try multiple channel names for WH31
    canopy = (data.get("temp_and_humidity_ch1")
              or data.get("indoor")
              or data.get("temp_and_humidity_ch2", {}))
    gh_temp = safe(canopy, "temperature", "value")
    gh_rh   = safe(canopy, "humidity", "value")
    soil_ch1 = safe(data.get("soil_ch1", {}), "soilmoisture", "value")
    soil_ch2 = safe(data.get("soil_ch2", {}), "soilmoisture", "value")
    out_temp = safe(data.get("outdoor", {}), "temperature", "value")

    # If no live data, show placeholder
    if gh_temp is None: gh_temp, gh_rh = 22.0, 65.0  # demo
    if soil_ch1 is None: soil_ch1 = 42.0
    if soil_ch2 is None: soil_ch2 = 38.0

    lvpd_val = calc_lvpd(gh_temp, gh_rh)
    zone_label, zone_color, zone_action = lvpd_zone(lvpd_val)
    soil_low = soil_ch1 < 35 or soil_ch2 < 35
    # Override action text using combined LVPD + soil context
    if lvpd_val > 1.5:
        zone_action = "Irrigate + ventilate" if soil_low else "Ventilate now — soil OK"

    # ── Top row: key metrics ──────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("🌡️ Canopy Temp", f"{gh_temp:.1f} °C",
              delta=f"{gh_temp - 18:.1f}°C vs 18°C target")
    c2.metric("💧 Humidity", f"{gh_rh:.0f} %")
    c3.metric("🌬️ LVPD", f"{lvpd_val:.3f} kPa", delta=zone_label)
    c4.metric("🌱 Soil GH4N", f"{soil_ch1:.0f} %",
              delta="Low" if soil_ch1 < 30 else "OK")
    c5.metric("🌱 Soil GH4S", f"{soil_ch2:.0f} %",
              delta="Low" if soil_ch2 < 30 else "OK")

    # ── LVPD zone indicator ───────────────────────────────────────────────────
    fan_rh_threshold = 85.0
    fan_status = "ON — humidity trigger" if gh_rh >= fan_rh_threshold else "standby"
    fan_pill_class = "pill-warn" if gh_rh >= fan_rh_threshold else "pill-ok"

    st.markdown(f"""
    <div style='background:{zone_color}18; border-left: 4px solid {zone_color};
         padding: 14px 22px; border-radius: 10px; margin: 16px 0;
         display: flex; align-items: center; gap: 24px;'>
      <div>
        <span style='color:{zone_color}; font-size:1.15em; font-weight:700'>{zone_label}</span>
        &nbsp; → &nbsp;<span style='color:#374151; font-weight:500'>{zone_action}</span>
        &nbsp;&nbsp;<span style='color:#6b7280; font-size:0.85em'>LVPD = {lvpd_val:.3f} kPa</span>
      </div>
      <div style='margin-left:auto'>
        {"<span class='status-pill " + fan_pill_class + "'>🌀 Fan: " + fan_status + "</span>&nbsp;<span class='status-pill pill-info'>📡 AC1100 paired</span>" if FAN_INSTALLED else "<span class='status-pill' style='background:#f3f4f6;color:#6b7280;border:1px solid #e5e7eb'>⏳ Fan: not installed</span>"}
      </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Irrigation recommendation + partner alert ─────────────────────────────
    heat_stress  = lvpd_val > 1.5
    lvpd_optimal = 0.8 <= lvpd_val <= 1.2
    needs_water  = soil_low and lvpd_val > 0.4
    col_irr, col_btn = st.columns([3, 1])
    with col_irr:
        if needs_water and heat_stress:
            st.error("⚠️ **Dry soil + heat stress — irrigate immediately, then ventilate.**")
        elif needs_water:
            st.info("💧 **Irrigation recommended** — soil moisture low")
        elif lvpd_optimal and not soil_low:
            st.success("✅ **Conditions good** — LVPD optimal, soil moisture adequate")
        # else: LVPD zone banner already communicates the risk — no duplicate card
    with col_btn:
        if st.button("📲 Send water reminder", help="Push Pushover alert to phone"):
            beds = []
            if soil_ch1 < 35: beds.append(f"GH4N {soil_ch1:.0f}%")
            if soil_ch2 < 35: beds.append(f"GH4S {soil_ch2:.0f}%")
            msg = (f"💧 Water reminder\nLVPD: {lvpd_val:.3f} kPa | Temp: {gh_temp:.1f}°C\n"
                   f"Dry beds: {', '.join(beds) if beds else 'none'}")
            ok = send_pushover(msg, title="🌿 GH Water Reminder", priority=0)
            st.success("✅ Reminder sent!") if ok else st.warning("⚠️ Pushover not configured")

    # ── Outdoor weather card (Open-Meteo) ────────────────────────────────────
    cur_wx, _wx_df, _wx_err = fetch_current_weather()
    if cur_wx:
        wx_code   = int(cur_wx.get("weathercode", 1))
        wx_label, wx_emoji = _WMO.get(wx_code, ("Unknown", "🌡️"))
        wx_temp   = cur_wx.get("temperature", "—")
        wx_wind   = cur_wx.get("windspeed", "—")
        wx_is_day = cur_wx.get("is_day", 1)
        # Precip in next 3 h from hourly
        rain_soon = ""
        if not _wx_df.empty and "precipitation" in _wx_df.columns:
            now_ts = pd.Timestamp.now(tz="Europe/Dublin").tz_localize(None)
            nxt3 = _wx_df[(_wx_df["time"] >= now_ts) & (_wx_df["time"] < now_ts + pd.Timedelta(hours=3))]
            total_rain = nxt3["precipitation"].sum() if not nxt3.empty else 0
            rain_soon = f"🌧️ {total_rain:.1f} mm rain next 3h" if total_rain > 0.1 else "☂️ No rain next 3h"
        # GH vs outdoor delta
        temp_delta = gh_temp - wx_temp if isinstance(wx_temp, (int, float)) else None
        delta_str = f"+{temp_delta:.1f}°C GH heating" if temp_delta and temp_delta >= 0 else (
                    f"{temp_delta:.1f}°C" if temp_delta else "")
        day_night = "☀️ Day" if wx_is_day else "🌙 Night"
        st.markdown(f"""
        <div style='background:#f8fafc; border:1px solid #e2e8f0; border-radius:10px;
             padding:12px 20px; margin:10px 0; display:flex; align-items:center; gap:20px;
             flex-wrap:wrap;'>
          <div style='font-size:2em; line-height:1'>{wx_emoji}</div>
          <div>
            <div style='font-weight:700; color:#111827; font-size:1em'>Outdoor — Maynooth now</div>
            <div style='color:#374151; font-size:0.95em'>{wx_label} &nbsp;·&nbsp; {day_night}</div>
          </div>
          <div style='display:flex; gap:28px; flex-wrap:wrap; margin-left:8px'>
            <div><span style='color:#6b7280; font-size:0.8em'>TEMP</span><br/>
              <b style='color:#111827'>{wx_temp}°C</b>
              {"&nbsp;<span style='color:#15803d; font-size:0.8em'>" + delta_str + "</span>" if delta_str else ""}
            </div>
            <div><span style='color:#6b7280; font-size:0.8em'>WIND</span><br/>
              <b style='color:#111827'>{wx_wind} km/h</b></div>
            <div><span style='color:#6b7280; font-size:0.8em'>RAIN</span><br/>
              <b style='color:#111827; font-size:0.85em'>{rain_soon}</b></div>
          </div>
        </div>
        """, unsafe_allow_html=True)

    # ── LVPD gauge ───────────────────────────────────────────────────────────
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=lvpd_val,
        title={"text": "LVPD (kPa)", "font": {"size": 18}},
        number={"suffix": " kPa", "font": {"size": 28}},
        gauge={
            "axis": {"range": [-0.5, 2.5], "tickwidth": 1},
            "bar": {"color": zone_color},
            "steps": [
                {"range": [-0.5, 0.0],"color": "#ede9fe"},
                {"range": [0.0, 0.4], "color": "#fecaca"},
                {"range": [0.4, 0.8],"color": "#fed7aa"},
                {"range": [0.8, 1.2],"color": "#bbf7d0"},
                {"range": [1.2, 1.5],"color": "#fed7aa"},
                {"range": [1.5, 2.5],"color": "#fecaca"},
            ],
            "threshold": {"line": {"color": "white", "width": 3}, "value": lvpd_val},
        }
    ))
    fig.update_layout(height=280, margin=dict(t=40, b=10))

    col1, col2 = st.columns([1, 2])
    with col1:
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        st.markdown("#### LVPD Reference")
        for lo, hi, label, color, action in LVPD_ZONES:
            active = "**" if lo <= lvpd_val < hi else ""
            st.markdown(
                f"{'→ ' if active else '&nbsp;&nbsp;'}"
                f"{lo:.1f}–{hi:.1f} kPa &nbsp; "
                f"<span style='color:{color}'>{active}{label}{active}</span>"
                f" — {action}", unsafe_allow_html=True
            )

    # ── Raw API keys (diagnostic) ─────────────────────────────────────────────
    with st.expander("🔧 Raw API response keys (diagnostic)"):
        st.json(list(data.keys()) if data else {})
        st.caption("If WH31/WH51 shows 'missing', check which key your sensor uses above")

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 2 — PRODUCTION
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🌱 Production":
    st.title("🌱 Production Tracking")

    @st.cache_data(ttl=60)
    def load_harvest():
        path = SEASON / "HARVEST_LOG_2026.csv"
        try:
            df = pd.read_csv(path, comment="#")
            df = df[df["variety"].notna() & ~df["variety"].str.startswith("#", na=True)]
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["weight_kg"] = pd.to_numeric(df["weight_kg"], errors="coerce").fillna(0)
            df["value_eur"] = df["variety"].map(PRICES).fillna(3.0) * df["weight_kg"]
            return df
        except FileNotFoundError:
            return pd.DataFrame()

    df = load_harvest()

    # Targets
    TARGETS = {
        "All Tomatoes": 10.0, "Passandra F1": 6.0,
        "Jalapeño Ruben": 2.0, "Broad Beans": 3.0,
    }

    # ── Harvest entry form ────────────────────────────────────────────────────
    with st.expander("➕ Log a harvest", expanded=df.empty):
        with st.form("harvest_form", clear_on_submit=True):
            fc1, fc2, fc3 = st.columns(3)
            variety = fc1.selectbox("Variety", list(PRICES.keys()))
            zone = fc2.selectbox("Zone", ["GH1N","GH1S","GH2N","GH2S","GH3N","GH3S",
                                          "GH4N","GH4S","GH5N","GH5S","GH6N","GH6S",
                                          "Bay3","Bay4","Bay5","Bay6","Bay7"])
            quality = fc3.slider("Quality (1–5)", 1, 5, 4)
            fw1, fw2 = st.columns(2)
            weight_kg = fw1.number_input("Weight (kg)", min_value=0.0, step=0.05, format="%.3f")
            count = fw2.number_input("Count (optional, e.g. chillis)", min_value=0, step=1)
            notes = st.text_input("Notes (optional)")
            submitted = st.form_submit_button("💾 Save harvest")
            if submitted:
                if weight_kg == 0 and count == 0:
                    st.warning("Enter weight or count.")
                else:
                    harvest_path = SEASON / "HARVEST_LOG_2026.csv"
                    row = {
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "variety": variety, "zone": zone,
                        "weight_kg": weight_kg if weight_kg > 0 else "",
                        "count": count if count > 0 else "",
                        "quality": quality, "notes": notes,
                    }
                    file_exists = harvest_path.exists()
                    with open(harvest_path, "a", newline="") as f:
                        w = csv.DictWriter(f, fieldnames=row.keys())
                        if not file_exists:
                            w.writeheader()
                        w.writerow(row)
                    st.success(f"✅ Logged {weight_kg:.3f} kg {variety} from {zone}")
                    st.cache_data.clear()

    if df.empty:
        st.info("📭 No harvest data yet. First tomatoes expected July 2026.")
    else:
        total_kg = df["weight_kg"].sum()
        total_val = df["value_eur"].sum()

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Harvest", f"{total_kg:.2f} kg")
        c2.metric("Estimated Value", f"€{total_val:.2f}")
        c3.metric("Varieties Harvested", df["variety"].nunique())

        # Cumulative harvest over time
        df_sorted = df.sort_values("date")
        df_sorted["cumulative_kg"] = df_sorted["weight_kg"].cumsum()
        fig1 = px.line(df_sorted, x="date", y="cumulative_kg",
                       title="Cumulative Harvest (kg)", markers=True,
                       labels={"cumulative_kg": "kg", "date": ""})
        st.plotly_chart(fig1, use_container_width=True)

        col1, col2 = st.columns(2)
        with col1:
            # By variety
            by_var = df.groupby("variety").agg(kg=("weight_kg", "sum"), value=("value_eur", "sum")).reset_index()
            fig2 = px.bar(by_var.sort_values("kg", ascending=True), x="kg", y="variety",
                          orientation="h", title="Harvest by Variety (kg)", color="kg",
                          color_continuous_scale="Greens")
            st.plotly_chart(fig2, use_container_width=True)
        with col2:
            # Value by variety
            fig3 = px.pie(by_var, values="value", names="variety",
                          title="Estimated Value by Variety (€)")
            st.plotly_chart(fig3, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 3 — SEASON PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "📋 Season Pipeline":
    st.title("📋 Season Pipeline — What's Where")

    pipeline_data = [
        # variety, zone, sow_date, stage, expected_harvest, notes
        ("San Marzano", "GH2N/GH2S", "2026-03-01", "🌿 Growing", "2026-07-15", "7 plants (6 cordon + 1 stake)"),
        ("Black Krim", "GH3N/GH5S", "2026-03-01", "🌿 Growing", "2026-07-20", "5 plants (4 cordon + 1 stake)"),
        ("Marmande", "GH3S/GH5N", "2026-03-01", "🌿 Growing", "2026-07-20", "5 plants (4 cordon + 1 stake)"),
        ("Sungold F1", "GH4N/GH5S", "2026-03-01", "🌿 Growing", "2026-07-10", "4 plants — €12/kg cherry, earliest fruiter"),
        ("Tigerella", "GH4S/GH5N", "2026-03-01", "🌿 Growing", "2026-07-15", "3 plants (2 cordon + 1 stake)"),
        ("Tomato (4 unconfirmed)", "GH various", "2026-04-07", "🌿 Growing", "2026-07-20", "⚠️ Variety unconfirmed — check pot labels May 3 (GARDEN-74)"),
        ("Tomato (outdoor mix)", "Bay 3 E-wall", "2026-03-01", "🌿 Growing", "2026-08-01", "11 plants, sunny east wall — planted May 2026"),
        ("Passandra F1", "GH1N/GH1S", "2026-03-10", "🌿 Growing", "2026-07-01", "3 plants: H1N×1, H1S×2 — training up ridge"),
        ("Jalapeño Ruben", "GH6N", "2026-01-04", "🌸 Establishing", "2026-08-01", "8 plants, 60cm canes"),
        ("Yolo Wonder", "GH6S", "2026-01-04", "🌸 Establishing", "2026-08-15", "3 plants"),
        ("Tsaksoniki Aubergine", "GH6S", "2026-01-04", "🌸 Establishing", "2026-08-15", "3 plants"),
        ("Pantos", "GH6S", "2026-03-09", "🌸 Establishing", "2026-08-20", "12+ plants (lab batch Apr 18 — confirm variety on return)"),
        ("Aquadulce Claudia", "Bay 6", "2026-03-01", "🌿 Growing", "2026-06-20", "8 plants, against trellis"),
        ("Kelvedon Wonder", "Bay 5", "2026-03-18", "🌿 Growing", "2026-06-15", "36 positions"),
        ("Kale (Nero + Amara)", "Lab → Bay 4/5", "2026-03-10", "🪴 Lab ready", "2026-09-01", "46 plants, brassica cage needed first"),
        ("Defender F1 Courgette", "Bay 6 (outdoor)", "NOT SOWN", "⚠️ Sow May 4", "2026-07-20", "URGENT — heat mat on return"),
        ("Uchiki Kuri Squash", "Bay 6 (outdoor)", "NOT SOWN", "⚠️ Sow May 4", "2026-08-15", "URGENT — heat mat on return"),
        ("Dalmaziano Beans", "Bay 3", "NOT SOWN", "⚠️ Sow May 1", "2026-09-01", "Direct sow outdoors"),
    ]

    df_pipe = pd.DataFrame(pipeline_data, columns=["Variety", "Zone", "Sown", "Stage", "Expected Harvest", "Notes"])

    stage_order = {"🌿 Growing": 1, "🌸 Establishing": 2, "🪴 Lab ready": 3,
                   "⚠️ Sow May 4": 4, "⚠️ Sow May 1": 4, "⚠️ Sow soon": 4}
    df_pipe["_order"] = df_pipe["Stage"].map(stage_order).fillna(5)
    df_pipe = df_pipe.sort_values("_order").drop("_order", axis=1)

    # Gantt chart
    gantt_data = []
    today = pd.Timestamp.now().normalize()
    for _, row in df_pipe.iterrows():
        start = pd.to_datetime(row["Sown"], errors="coerce") or today
        end = pd.to_datetime(row["Expected Harvest"], errors="coerce")
        if pd.isna(start): start = today
        if pd.isna(end): end = today + timedelta(days=90)
        gantt_data.append(dict(Task=row["Variety"], Start=start, Finish=end, Stage=row["Stage"]))

    fig = px.timeline(gantt_data, x_start="Start", x_end="Finish", y="Task",
                      color="Stage", title="Crop Timeline 2026",
                      color_discrete_map={
                          "🌿 Growing": "#22c55e", "🌸 Establishing": "#3b82f6",
                          "🪴 Lab ready": "#a855f7", "⚠️ Sow May 4": "#ef4444",
                          "⚠️ Sow May 1": "#ef4444",
                      })
    # px.timeline uses ms-epoch on x-axis; add_vline annotation causes TypeError
    # with string dates on timeline charts. Pass epoch ms + separate annotation.
    today_ms = int(today.timestamp() * 1000)
    fig.add_vline(x=today_ms, line_dash="dash", line_color="#52b788")
    fig.add_annotation(
        x=today_ms, y=1.02, xref="x", yref="paper",
        text="Today", showarrow=False,
        font=dict(color="#52b788", size=12), bgcolor="rgba(0,0,0,0.4)"
    )
    fig.update_layout(height=600, yaxis_autorange="reversed")
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(df_pipe, use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 4 — TIME & ROI
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "⏱️ Time & ROI":
    st.title("⏱️ Time Investment & ROI")

    @st.cache_data(ttl=60)
    def load_time():
        path = SEASON / "TIME_LOG_2026.csv"
        try:
            df = pd.read_csv(path, comment="#")
            df = df[df["category"].notna() & ~df["category"].str.startswith("#", na=True)]
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["duration_min"] = pd.to_numeric(df["duration_min"], errors="coerce").fillna(0)
            return df
        except FileNotFoundError:
            return pd.DataFrame()

    @st.cache_data(ttl=60)
    def load_harvest_for_roi():
        path = SEASON / "HARVEST_LOG_2026.csv"
        try:
            df = pd.read_csv(path, comment="#")
            df = df[df["variety"].notna() & ~df["variety"].str.startswith("#", na=True)]
            df["weight_kg"] = pd.to_numeric(df["weight_kg"], errors="coerce").fillna(0)
            df["value_eur"] = df["variety"].map(PRICES).fillna(3.0) * df["weight_kg"]
            return df["value_eur"].sum()
        except FileNotFoundError:
            return 0.0

    df_time = load_time()
    produce_value = load_harvest_for_roi()
    SEED_SAVING = 55.0  # estimated €/yr from seed saving

    if not df_time.empty:
        total_mins = df_time["duration_min"].sum()
        total_hrs = total_mins / 60
        total_value = produce_value + SEED_SAVING
        hourly_return = total_value / total_hrs if total_hrs > 0 else 0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total Hours", f"{total_hrs:.1f} hrs")
        c2.metric("Produce Value", f"€{produce_value:.2f}")
        c3.metric("+ Seed Savings", f"€{SEED_SAVING:.0f}")
        c4.metric("Effective €/hr", f"€{hourly_return:.2f}",
                  delta="✅ Above target" if hourly_return >= 15 else "⏳ Building")

        col1, col2 = st.columns(2)
        with col1:
            by_cat = df_time.groupby("category")["duration_min"].sum().reset_index()
            by_cat["hours"] = by_cat["duration_min"] / 60
            fig1 = px.pie(by_cat, values="hours", names="category",
                          title="Time by Category (hours)",
                          color_discrete_sequence=px.colors.qualitative.Set3)
            st.plotly_chart(fig1, use_container_width=True)
        with col2:
            # Monthly time bar
            df_time["month"] = df_time["date"].dt.to_period("M").astype(str)
            by_month = df_time.groupby(["month","category"])["duration_min"].sum().reset_index()
            by_month["hours"] = by_month["duration_min"] / 60
            fig2 = px.bar(by_month, x="month", y="hours", color="category",
                          title="Hours by Month", barmode="stack")
            st.plotly_chart(fig2, use_container_width=True)

        # ROI target gauge
        target_return = 15.0
        fig3 = go.Figure(go.Indicator(
            mode="gauge+number+delta",
            value=hourly_return,
            delta={"reference": target_return, "valueformat": ".1f"},
            title={"text": "Effective €/hr (target: €15)"},
            number={"prefix": "€", "suffix": "/hr"},
            gauge={
                "axis": {"range": [0, 30]},
                "bar": {"color": "#22c55e" if hourly_return >= 15 else "#f97316"},
                "steps": [{"range": [0, 15], "color": "#1e293b"}, {"range": [15, 30], "color": "#14532d"}],
                "threshold": {"line": {"color": "#22c55e", "width": 3}, "value": 15},
            }
        ))
        fig3.update_layout(height=250, margin=dict(t=40, b=10))
        st.plotly_chart(fig3, use_container_width=True)
    else:
        st.info("No time data yet. Time log initialises on first entry.")
        st.code("python Greenhouse/digital_twin/log_harvest.py time --category physical_garden --minutes 120 --activity 'Planting'", language="bash")

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 5 — FINANCE
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "💰 Finance":
    st.title("💰 Finance & Budget")

    @st.cache_data(ttl=60)
    def load_finance():
        path = SEASON / "FINANCE_2026.csv"
        try:
            df = pd.read_csv(path, comment="#")
            df = df[df["category"].notna() & ~df["category"].str.startswith("#", na=True)]
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df["amount_eur"] = pd.to_numeric(df["amount_eur"], errors="coerce").fillna(0)
            return df
        except FileNotFoundError:
            return pd.DataFrame()

    df_fin = load_finance()
    BUDGET = 700.0
    STILL_TO_SPEND = 799 + 60 + 30 + 15  # Mac Mini + WFC01 + fan + neem

    confirmed = df_fin[df_fin["amount_eur"] > 0]["amount_eur"].sum() if not df_fin.empty else 0
    projected_total = confirmed + STILL_TO_SPEND

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Confirmed Spend", f"€{confirmed:.2f}")
    c2.metric("Still to Spend", f"€{STILL_TO_SPEND:.0f}")
    c3.metric("Projected Total", f"€{projected_total:.0f}")
    c4.metric("vs Budget (€700)", f"€{projected_total - BUDGET:+.0f}",
              delta="over" if projected_total > BUDGET else "under")

    if not df_fin.empty and confirmed > 0:
        df_known = df_fin[df_fin["amount_eur"] > 0]
        col1, col2 = st.columns(2)
        with col1:
            by_cat = df_known.groupby("category")["amount_eur"].sum().reset_index()
            fig1 = px.pie(by_cat, values="amount_eur", names="category",
                          title="Spend by Category (confirmed)",
                          color_discrete_sequence=px.colors.qualitative.Pastel)
            st.plotly_chart(fig1, use_container_width=True)
        with col2:
            by_supplier = df_known.groupby("supplier")["amount_eur"].sum().reset_index()
            fig2 = px.bar(by_supplier.sort_values("amount_eur"), x="amount_eur", y="supplier",
                          orientation="h", title="Spend by Supplier",
                          labels={"amount_eur": "€", "supplier": ""})
            st.plotly_chart(fig2, use_container_width=True)

        st.dataframe(df_known[["date","category","supplier","item","amount_eur","status"]],
                     use_container_width=True, hide_index=True)

    # Still to buy
    st.subheader("Still to purchase")
    st.dataframe(pd.DataFrame([
        {"Item": "Mac Mini M5 (16GB/256GB)", "Est. €": 799, "When": "Jun 2026", "Linear": "GARDEN-19"},
        {"Item": "WFC01 Smart Valve", "Est. €": 60, "When": "Jun-Jul 2026", "Linear": "GARDEN-24"},
        {"Item": "Neem Oil", "Est. €": 15, "When": "May 3+ return", "Linear": "Return sprint"},
        {"Item": "Calcium Fertiliser (FHF — dispatched Apr 21)", "Est. €": 0, "When": "May 3+ delivery", "Linear": "GARDEN-17"},
        {"Item": "Bamboo canes ×80+", "Est. €": 18, "When": "May 3+ return", "Linear": "Return sprint"},
    ]), hide_index=True, use_container_width=True)

    st.subheader("Recently purchased ✅")
    st.dataframe(pd.DataFrame([
        {"Item": "ProFan 8\" Clip Fan 20W (SKU FAN-OSC-20W)", "Paid €": 37.00, "Date": "22 Apr 2026"},
        {"Item": "Ecowitt AC1100 WittSwitch Smart Plug", "Paid €": 35.99, "Date": "23 Apr 2026"},
        {"Item": "Propagator Large (TI204) + Small (TI205)", "Paid €": 25.00, "Date": "22 Apr 2026"},
        {"Item": "Vitavia Wall Shelves ×2 (Lenehans)", "Paid €": 73.98, "Date": "8 Apr 2026"},
    ]), hide_index=True, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — ASK THE GARDEN (RAG — live Apr 2026)
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🤖 Ask the Garden":
    st.title("🤖 Ask the Garden")
    st.markdown("""
    <div class="info-banner">
    📚 <strong>intel_garden RAG — live</strong> &nbsp;·&nbsp;
    7 docs · 73 chunks · LlamaIndex + ChromaDB + MiniLM-L6-v2 &nbsp;·&nbsp;
    Sources: GH climate & VPD · agronomic playbook · crop targets · predictive irrigation · MASTER_INVENTORY · Jean-Martin Fortier methodology
    </div>
    """, unsafe_allow_html=True)

    # Example question buttons
    st.markdown("**Quick questions:**")
    ex_cols = st.columns(3)
    example_questions = [
        "What LVPD causes botrytis risk in Kildare?",
        "When to stop side-shooting cordon tomatoes?",
        "How often to feed tomatoes with Vinasse?",
        "What soil moisture triggers irrigation?",
        "Best time to harvest Jalapeño Ruben?",
        "How to build the brassica cage?",
    ]
    selected_example = None
    for i, eq in enumerate(example_questions):
        with ex_cols[i % 3]:
            if st.button(eq, use_container_width=True):
                selected_example = eq

    st.divider()
    question = st.text_input(
        "Ask about your crops:",
        value=selected_example or "",
        placeholder="e.g. When to stop side-shooting tomatoes?"
    )

    if question:
        st.caption("⏳ First query loads the model (~30–60 s). Subsequent queries are faster.")
        with st.spinner("Querying intel_garden — loading model on first run…"):
            import subprocess, os as _os
            _python = _os.path.expanduser("~/miniconda3/envs/ml_lab1/bin/python")
            try:
                result = subprocess.run(
                    [_python,
                     "/Users/danalexandrubujoreanu/Personal Projects/Gardening/AI/query_mba.py",
                     question, "garden"],
                    capture_output=True, text=True, timeout=120  # 2 min — model cold-start
                )
                if result.returncode == 0 and result.stdout.strip():
                    st.markdown("---")
                    st.markdown(result.stdout)
                else:
                    st.warning("No answer returned.")
                    if result.stderr:
                        with st.expander("Error details"):
                            st.code(result.stderr)
            except subprocess.TimeoutExpired:
                st.error("⏰ Query timed out (>2 min). The RAG model may still be loading — try again in 30 seconds.")
                st.info("If this keeps happening, restart the ml_lab1 env: `conda activate ml_lab1`")

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 7 — WEATHER & GH HEALTH
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🌤️ Weather & GH Health":
    import math as _math
    import io as _io

    st.title("🌤️ Weather & Greenhouse Health")
    st.caption(
        "Outdoor: Open-Meteo API (53.38°N 6.59°W — Maynooth) · "
        "Indoor: Ecowitt WH31 canopy sensor · "
        "Methodology: Tetens SVP, FAO-56 Penman-Monteith ET₀, GDD base 10°C"
    )

    # ── Live GH readings (reuse module-level fetch) ───────────────────────────
    _ew_data, _ew_err = fetch_ecowitt()
    def _safe(d, *keys):
        for k in keys:
            if not isinstance(d, dict): return None
            d = d.get(k)
        return d.get("value") if isinstance(d, dict) else None
    _d = _ew_data or {}
    def _tof(v):
        try: return float(v)
        except (TypeError, ValueError): return None
    gh_temp = _tof(_safe(_d, "temp_and_humidity_ch1", "temperature") or _safe(_d, "indoor", "temperature"))
    gh_rh   = _tof(_safe(_d, "temp_and_humidity_ch1", "humidity")   or _safe(_d, "indoor", "humidity"))
    if gh_temp is None: gh_temp, gh_rh = 22.0, 65.0  # demo fallback
    lvpd_val = calc_lvpd(gh_temp, gh_rh)

    # ── Helpers ───────────────────────────────────────────────────────────────
    # fetch_current_weather() defined at module level — reused here
    def query_influx_http(flux: str) -> pd.DataFrame:
        """Query InfluxDB via HTTP API — works regardless of Python env."""
        token = os.getenv("INFLUX_TOKEN", "")
        if not token:
            return pd.DataFrame()
        try:
            import urllib.request as _req
            req = _req.Request(
                "http://localhost:8086/api/v2/query?org=maynooth",
                data=flux.encode(),
                headers={
                    "Authorization": f"Token {token}",
                    "Content-Type": "application/vnd.flux",
                    "Accept": "application/csv",
                },
                method="POST",
            )
            with _req.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
            lines = [l for l in raw.splitlines() if l and not l.startswith("#")]
            if len(lines) < 2:
                return pd.DataFrame()
            return pd.read_csv(_io.StringIO("\n".join(lines)))
        except Exception:
            return pd.DataFrame()

    # ── Fetch data ────────────────────────────────────────────────────────────
    _cur_wx, wx, wx_err = fetch_current_weather()  # wx = hourly DataFrame

    # InfluxDB: canopy + soil last 7d
    df_canopy = query_influx_http("""
from(bucket: "greenhouse")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "greenhouse_canopy")
  |> filter(fn: (r) => r._field == "lvpd_kpa" or r._field == "temperature_c" or r._field == "humidity_pct")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
""")

    df_soil = query_influx_http("""
from(bucket: "greenhouse")
  |> range(start: -7d)
  |> filter(fn: (r) => r._measurement == "soil_moisture")
  |> filter(fn: (r) => r._field == "moisture_pct")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
""")

    # ── SECTION 1: Plant Health Score ─────────────────────────────────────────
    st.markdown("## 🌿 Plant Environment Health Score")
    st.markdown(
        "*What % of the past 7 days did the greenhouse maintain optimal conditions "
        "for plant transpiration and disease prevention?*"
    )

    has_influx = not df_canopy.empty
    lvpd_score = soil_score = overall_score = None

    if has_influx and "lvpd_kpa" in df_canopy.columns:
        lvpd_vals = pd.to_numeric(df_canopy["lvpd_kpa"], errors="coerce").dropna()
        n_total = len(lvpd_vals)
        n_optimal = ((lvpd_vals >= 0.4) & (lvpd_vals <= 1.2)).sum()
        n_risk    = (lvpd_vals < 0.4).sum()
        n_stress  = (lvpd_vals > 1.5).sum()
        lvpd_score = int(n_optimal / n_total * 100) if n_total > 0 else 0

    if not df_soil.empty and "_value" in df_soil.columns:
        soil_vals = pd.to_numeric(df_soil["_value"], errors="coerce").dropna()
        n_soil_ok = (soil_vals >= 35).sum()
        soil_score = int(n_soil_ok / len(soil_vals) * 100) if len(soil_vals) > 0 else 0

    # Weighted score: LVPD 60%, soil 40%
    if lvpd_score is not None and soil_score is not None:
        overall_score = int(lvpd_score * 0.6 + soil_score * 0.4)
    elif lvpd_score is not None:
        overall_score = lvpd_score

    if overall_score is not None:
        score_color = "#15803d" if overall_score >= 75 else "#d97706" if overall_score >= 50 else "#dc2626"
        grade = "Excellent" if overall_score >= 85 else "Good" if overall_score >= 70 else "Fair" if overall_score >= 50 else "Poor"

        sc1, sc2, sc3, sc4 = st.columns(4)
        with sc1:
            st.markdown(f"""
            <div class="score-card">
              <div class="score-number" style="color:{score_color}">{overall_score}%</div>
              <div class="score-label">Overall health — 7 days</div>
              <div style="font-weight:700; color:{score_color}; margin-top:4px">{grade}</div>
            </div>""", unsafe_allow_html=True)
        with sc2:
            s = lvpd_score if lvpd_score is not None else "—"
            c = "#15803d" if isinstance(s, int) and s >= 70 else "#d97706"
            st.markdown(f"""
            <div class="score-card">
              <div class="score-number" style="color:{c}">{s}{'%' if isinstance(s,int) else ''}</div>
              <div class="score-label">LVPD optimal (0.4–1.2 kPa)</div>
            </div>""", unsafe_allow_html=True)
        with sc3:
            s = soil_score if soil_score is not None else "—"
            c = "#15803d" if isinstance(s, int) and s >= 70 else "#d97706"
            st.markdown(f"""
            <div class="score-card">
              <div class="score-number" style="color:{c}">{s}{'%' if isinstance(s,int) else ''}</div>
              <div class="score-label">Soil moisture adequate (≥35%)</div>
            </div>""", unsafe_allow_html=True)
        with sc4:
            risk_pct = int(n_risk / n_total * 100) if lvpd_score is not None else 0
            st.markdown(f"""
            <div class="score-card">
              <div class="score-number" style="color:#dc2626">{risk_pct}%</div>
              <div class="score-label">Botrytis risk hours (LVPD&lt;0.4)</div>
            </div>""", unsafe_allow_html=True)

        st.markdown("""
        <div class="info-banner">
        <strong>Methodology:</strong> LVPD score = hours in 0.4–1.2 kPa range ÷ total hours.
        Soil score = hours with ≥35% VWC on either bed ÷ total hours.
        Overall = LVPD×0.6 + Soil×0.4 (LVPD weighted higher — disease risk is more acute than drought).
        Literature reference: Körner et al. (2008) — optimal greenhouse VPD 0.5–1.0 kPa for tomatoes.
        </div>
        """, unsafe_allow_html=True)

        if has_influx:
            # LVPD distribution histogram
            fig_hist = go.Figure()
            fig_hist.add_trace(go.Histogram(
                x=lvpd_vals, nbinsx=30,
                marker_color=[
                    "#7c3aed" if v < 0 else "#ef4444" if v < 0.4 else
                    "#f97316" if v < 0.8 else "#22c55e" if v < 1.2 else
                    "#f97316" if v < 1.5 else "#ef4444"
                    for v in sorted(lvpd_vals)
                ],
                name="LVPD hours"
            ))
            fig_hist.add_vrect(x0=0.4, x1=1.2, fillcolor="#22c55e", opacity=0.08,
                               annotation_text="Optimal zone", annotation_position="top left")
            fig_hist.update_layout(
                title="LVPD Distribution — last 7 days (hourly means)",
                xaxis_title="LVPD (kPa)", yaxis_title="Hours",
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font_color="#111827", height=280, margin=dict(t=40,b=20)
            )
            st.plotly_chart(fig_hist, use_container_width=True)
    else:
        st.info("📡 InfluxDB data not available — ensure Docker stack is running (`docker compose up -d`)")

    st.divider()

    # ── SECTION 2: Current conditions — outdoor vs indoor ─────────────────────
    st.markdown("## 🔬 Outdoor vs Greenhouse — Current Conditions")

    now_idx = None
    if not wx.empty:
        now_idx = (wx["time"] - pd.Timestamp.now()).abs().idxmin()
        out_temp  = wx.loc[now_idx, "temperature_2m"]
        out_rh    = wx.loc[now_idx, "relative_humidity_2m"]
        out_vpd   = wx.loc[now_idx, "vapour_pressure_deficit"]  # kPa, no leaf offset
        out_et0   = wx.loc[now_idx, "et0_fao_evapotranspiration"]
        out_wind  = wx.loc[now_idx, "windspeed_10m"]
    else:
        out_temp = out_rh = out_vpd = out_et0 = out_wind = None

    comp_cols = st.columns(2)
    with comp_cols[0]:
        st.markdown("**🌿 Greenhouse (WH31 canopy)**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Temp", f"{gh_temp:.1f}°C" if gh_temp else "—")
        c2.metric("RH", f"{gh_rh:.0f}%" if gh_rh else "—")
        c3.metric("LVPD", f"{lvpd_val:.3f} kPa" if gh_temp else "—")
    with comp_cols[1]:
        st.markdown("**☁️ Maynooth outdoor (Open-Meteo)**")
        c1, c2, c3 = st.columns(3)
        c1.metric("Temp", f"{out_temp:.1f}°C" if out_temp is not None else "—",
                  delta=f"{gh_temp - out_temp:+.1f}°C vs GH" if gh_temp and out_temp is not None else None)
        c2.metric("RH", f"{out_rh:.0f}%" if out_rh is not None else "—")
        c3.metric("Air VPD", f"{out_vpd:.3f} kPa" if out_vpd is not None else "—",
                  help="Open-Meteo VPD is air VPD (no leaf offset). GH LVPD uses 2°C leaf correction.")

    if out_et0 is not None:
        st.caption(
            f"📐 Reference evapotranspiration (ET₀): **{out_et0:.2f} mm/hr** "
            f"(FAO-56 Penman-Monteith) · Outdoor wind: {out_wind:.0f} km/h"
        )

    st.divider()

    # ── SECTION 3: 7-Day Forecast ─────────────────────────────────────────────
    st.markdown("## 📅 7-Day Weather Forecast — Maynooth")
    st.caption("Shaded zone = forecast (right of now). Historical = measured model analysis.")

    if not wx.empty and wx_err is None:
        fig_wx = go.Figure()

        hist = wx[~wx["is_forecast"]]
        fcast = wx[wx["is_forecast"]]
        now_time = pd.Timestamp.now()

        # Temperature traces
        fig_wx.add_trace(go.Scatter(x=hist["time"], y=hist["temperature_2m"],
            name="Temp (actual)", line=dict(color="#374151", width=1.5)))
        fig_wx.add_trace(go.Scatter(x=fcast["time"], y=fcast["temperature_2m"],
            name="Temp (forecast)", line=dict(color="#374151", width=1.5, dash="dot")))

        # Outdoor VPD on secondary axis
        fig_wx.add_trace(go.Scatter(x=hist["time"], y=hist["vapour_pressure_deficit"],
            name="Air VPD (actual)", line=dict(color="#15803d", width=1.5),
            yaxis="y2"))
        fig_wx.add_trace(go.Scatter(x=fcast["time"], y=fcast["vapour_pressure_deficit"],
            name="Air VPD (forecast)", line=dict(color="#15803d", width=1.5, dash="dot"),
            yaxis="y2"))

        # "Now" line
        fig_wx.add_vline(x=int(now_time.timestamp() * 1000),
                         line_dash="dash", line_color="#d97706", line_width=1.5)
        fig_wx.add_annotation(x=int(now_time.timestamp() * 1000), y=1.02,
            xref="x", yref="paper", text="Now", showarrow=False,
            font=dict(color="#d97706", size=11))

        fig_wx.update_layout(
            height=360, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font_color="#111827", margin=dict(t=20, b=30),
            legend=dict(orientation="h", y=-0.15),
            yaxis=dict(title="Temperature (°C)", showgrid=True, gridcolor="#f3f4f6"),
            yaxis2=dict(title="Air VPD (kPa)", overlaying="y", side="right",
                        showgrid=False, range=[0, 2.5]),
            xaxis=dict(showgrid=False),
        )
        st.plotly_chart(fig_wx, use_container_width=True)

        # ET₀ bar chart
        daily_wx = wx.groupby(wx["time"].dt.date).agg(
            et0=("et0_fao_evapotranspiration", "sum"),
            precip=("precipitation", "sum"),
            tmax=("temperature_2m", "max"),
            tmin=("temperature_2m", "min"),
            rad=("shortwave_radiation", "sum"),
        ).reset_index()
        daily_wx.columns = ["date", "ET₀ (mm/d)", "Rain (mm)", "T_max", "T_min", "Rad_Wh"]
        daily_wx["GDD"] = ((daily_wx["T_max"] + daily_wx["T_min"]) / 2 - 10).clip(lower=0)
        # DLI: 1 W/m² ≈ 2.1 μmol/m²/s PAR; hourly data → ×3600s; sum already in Wh/m²
        # DLI (mol/m²/d) = Σ(hourly_W/m²) × 3600 × 2.1 / 1_000_000
        daily_wx["DLI (mol/m²/d)"] = (daily_wx["Rad_Wh"] * 3600 * 2.1 / 1_000_000).round(1)

        col_et, col_tbl = st.columns([2, 1])
        with col_et:
            fig_et = go.Figure()
            fig_et.add_bar(x=daily_wx["date"], y=daily_wx["ET₀ (mm/d)"],
                           name="ET₀ (mm/d)", marker_color="#15803d", opacity=0.8)
            fig_et.add_bar(x=daily_wx["date"], y=daily_wx["Rain (mm)"],
                           name="Rain (mm)", marker_color="#60a5fa", opacity=0.7)
            fig_et.add_scatter(x=daily_wx["date"], y=daily_wx["DLI (mol/m²/d)"],
                               name="DLI (mol/m²/d)", mode="lines+markers",
                               line=dict(color="#f59e0b", width=2),
                               yaxis="y2")
            fig_et.update_layout(
                barmode="overlay", height=280,
                title="Daily ET₀ · Precipitation · DLI (Daily Light Integral)",
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                font_color="#111827", margin=dict(t=40, b=20),
                xaxis=dict(showgrid=False),
                yaxis=dict(title="mm", showgrid=True, gridcolor="#f3f4f6"),
                yaxis2=dict(title="DLI (mol/m²/d)", overlaying="y", side="right",
                            showgrid=False, range=[0, 30]),
                legend=dict(orientation="h", y=-0.25),
            )
            # Tomato optimal DLI band (15–22 mol/m²/d)
            fig_et.add_hrect(y0=15, y1=22, yref="y2", fillcolor="#bbf7d0",
                             opacity=0.15, line_width=0,
                             annotation_text="Tomato optimal DLI (15–22)", annotation_position="top right",
                             annotation_font_size=10, annotation_font_color="#15803d")
            st.plotly_chart(fig_et, use_container_width=True)
            st.caption(
                "DLI = Daily Light Integral (mol/m²/d) — cumulative photosynthetically active radiation. "
                "Irish cloud cover frequently suppresses DLI below the 15 mol/m²/d tomato threshold. "
                "ET₀ = FAO-56 Penman-Monteith reference evapotranspiration."
            )
        with col_tbl:
            st.markdown("**Daily summary**")
            st.dataframe(daily_wx[["date","ET₀ (mm/d)","Rain (mm)","GDD","DLI (mol/m²/d)"]].tail(10),
                         hide_index=True, use_container_width=True)

        # ── VPD Buffer Coefficient ────────────────────────────────────────────
        st.markdown("### 🔬 Greenhouse Buffer Coefficient — Outdoor VPD vs Indoor LVPD")
        st.caption(
            "The delta (Outdoor Air VPD − Indoor LVPD) quantifies the greenhouse glass as a climate buffer. "
            "A stable positive delta during outdoor spikes proves the structure is protecting the crop."
        )
        if not df_canopy.empty and "vapour_pressure_deficit" in wx.columns:
            # Align hourly Open-Meteo with InfluxDB canopy data
            hist_wx = wx[~wx["is_forecast"]].copy()
            hist_wx = hist_wx.set_index("time")[["vapour_pressure_deficit"]].rename(
                columns={"vapour_pressure_deficit": "outdoor_vpd"})
            # df_canopy is already pivot()-ed: lvpd_kpa is a direct column (no _field/_value)
            if "lvpd_kpa" not in df_canopy.columns:
                st.info(f"Buffer coefficient: lvpd_kpa not in InfluxDB columns {list(df_canopy.columns)[:8]}. Check Docker stack.")
                canopy_h = pd.Series(dtype=float)
            else:
                _ct = df_canopy[["_time", "lvpd_kpa"]].copy()
                _ct["_time"] = pd.to_datetime(_ct["_time"], utc=True).dt.tz_localize(None).dt.floor("h")
                canopy_h = _ct.groupby("_time")["lvpd_kpa"].mean().rename("indoor_lvpd")
            if not canopy_h.empty:
                buf = hist_wx.join(canopy_h, how="inner")
                buf["buffer_delta"] = buf["outdoor_vpd"] - buf["indoor_lvpd"]
                fig_buf = go.Figure()
                fig_buf.add_scatter(x=buf.index, y=buf["outdoor_vpd"],
                                    name="Outdoor Air VPD (kPa)", line=dict(color="#ef4444", width=1.5))
                fig_buf.add_scatter(x=buf.index, y=buf["indoor_lvpd"],
                                    name="Indoor LVPD (kPa)", line=dict(color="#22c55e", width=1.5))
                fig_buf.add_scatter(x=buf.index, y=buf["buffer_delta"],
                                    name="Buffer Δ (outdoor − indoor)", line=dict(color="#f59e0b", width=1.5, dash="dot"))
                fig_buf.add_hrect(y0=0, y1=0.4, fillcolor="#bbf7d0", opacity=0.1, line_width=0)
                fig_buf.update_layout(
                    height=300, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                    font_color="#111827", margin=dict(t=20, b=20),
                    yaxis=dict(title="VPD / LVPD (kPa)", showgrid=True, gridcolor="#f3f4f6"),
                    xaxis=dict(showgrid=False),
                    legend=dict(orientation="h", y=-0.25),
                )
                st.plotly_chart(fig_buf, use_container_width=True)
            else:
                st.info("InfluxDB LVPD history needed for buffer coefficient — check Docker stack.")
        else:
            st.info("Buffer coefficient requires both Open-Meteo and InfluxDB data.")

    else:
        st.warning(f"Open-Meteo unavailable: {wx_err}")

    st.divider()

    # ── SECTION 4: GDD tracker ────────────────────────────────────────────────
    st.markdown("## 🌱 Growing Degree Days (GDD) — Crop Progress")
    st.caption(
        "GDD base 10°C (standard for tomatoes, peppers, cucumbers). "
        "Formula: GDD = max(0, (T_max + T_min) / 2 − 10). "
        "Accumulated from sowing date using Open-Meteo hourly data."
    )

    SOW_DATES = {
        "Jalapeño Ruben":     "2026-01-04",
        "Yolo Wonder":        "2026-01-04",
        "Tsaksoniki Aubergine":"2026-01-04",
        "San Marzano":        "2026-03-01",
        "Black Krim":         "2026-03-01",
        "Sungold F1":         "2026-03-01",
        "Marmande":           "2026-03-01",
        "Passandra F1":       "2026-03-10",
        "Pantos":             "2026-03-09",
    }
    # GDD-to-harvest targets (days × avg GDD ≈ threshold)
    GDD_HARVEST = {
        "Jalapeño Ruben": 1200, "Yolo Wonder": 1400, "Tsaksoniki Aubergine": 1300,
        "San Marzano": 1100, "Black Krim": 1000, "Sungold F1": 900,
        "Marmande": 1000, "Passandra F1": 800, "Pantos": 1200,
    }

    if not wx.empty:
        # Build daily GDD from Open-Meteo hourly
        daily_gdd_df = wx.groupby(wx["time"].dt.date).agg(
            tmax=("temperature_2m", "max"), tmin=("temperature_2m", "min")
        ).reset_index()
        daily_gdd_df["gdd_day"] = ((daily_gdd_df["tmax"] + daily_gdd_df["tmin"]) / 2 - 10).clip(lower=0)
        daily_gdd_df["date"] = pd.to_datetime(daily_gdd_df["time"])

        gdd_rows = []
        for variety, sow in SOW_DATES.items():
            sow_dt = pd.to_datetime(sow)
            mask = daily_gdd_df["date"] >= sow_dt
            accum = daily_gdd_df.loc[mask, "gdd_day"].sum()
            target = GDD_HARVEST.get(variety, 1000)
            pct = min(100, int(accum / target * 100))
            days_from_sow = (pd.Timestamp.now() - sow_dt).days
            gdd_rows.append({
                "Variety": variety,
                "Sown": sow,
                "Days": days_from_sow,
                "GDD Accumulated": f"{accum:.0f}",
                "GDD Target": target,
                "Progress": pct,
            })

        gdd_df = pd.DataFrame(gdd_rows).sort_values("Progress", ascending=False)

        # Progress bar chart
        fig_gdd = go.Figure()
        fig_gdd.add_bar(
            x=gdd_df["Progress"], y=gdd_df["Variety"],
            orientation="h",
            marker=dict(
                color=[f"rgba(21,128,61,{0.4 + p/100*0.6})" for p in gdd_df["Progress"]],
                line=dict(color="#15803d", width=1)
            ),
            text=[f"{p}%" for p in gdd_df["Progress"]],
            textposition="outside",
        )
        fig_gdd.add_vline(x=100, line_dash="dash", line_color="#dc2626",
                          annotation_text="Harvest window", annotation_position="top right")
        fig_gdd.update_layout(
            height=350, title="GDD Progress to Harvest (base 10°C)",
            xaxis=dict(title="% of GDD target", range=[0, 115], showgrid=True, gridcolor="#f3f4f6"),
            yaxis=dict(autorange="reversed"),
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font_color="#111827", margin=dict(t=40, b=20, l=160),
        )
        st.plotly_chart(fig_gdd, use_container_width=True)
        st.dataframe(gdd_df[["Variety","Sown","Days","GDD Accumulated","GDD Target","Progress"]],
                     hide_index=True, use_container_width=True)
        st.caption(
            "GDD targets are variety-specific estimates from market gardening literature "
            "(Johnny's Seeds variety data, The Market Gardener). "
            "Greenhouse conditions accelerate progress vs field estimates — treat as directional."
        )

# ═══════════════════════════════════════════════════════════════════════════════
elif page == "👁️ Vision & Phenology":
    import json as _json
    st.title("👁️ Vision & Phenology")
    st.caption(
        "Computer vision layer — YOLOv8 object detection for ripeness tracking and plant phenology. "
        "Phase 3 (Jul 2026). Currently awaiting camera hardware."
    )

    VISION_IMG  = ROOT / "Greenhouse" / "vision" / "latest.jpg"
    VISION_JSON = ROOT / "Greenhouse" / "vision" / "latest.json"

    col_img, col_metrics = st.columns([2, 1])

    with col_img:
        st.markdown("**Latest GH frame**")
        if VISION_IMG.exists():
            st.image(str(VISION_IMG), use_column_width=True)
        else:
            st.markdown("""
            <div style='background:#f3f4f6; border:2px dashed #d1d5db; border-radius:12px;
                 padding:60px; text-align:center; color:#6b7280'>
              <div style='font-size:3em'>📷</div>
              <div style='font-weight:600; margin-top:12px'>Camera not connected</div>
              <div style='font-size:0.85em; margin-top:6px'>
                Place latest.jpg from the GH camera at<br/>
                <code>Greenhouse/vision/latest.jpg</code>
              </div>
            </div>
            """, unsafe_allow_html=True)

    with col_metrics:
        st.markdown("**YOLOv8 detection**")
        if VISION_JSON.exists():
            try:
                det = _json.loads(VISION_JSON.read_text())
                red   = int(det.get("red_tomatoes", 0))
                green = int(det.get("green_tomatoes", 0))
                total = red + green
                ratio = round(red / total * 100, 1) if total > 0 else 0
                st.metric("🔴 Ripe tomatoes", red)
                st.metric("🟢 Unripe tomatoes", green)
                st.metric("Ripeness ratio", f"{ratio}%",
                          delta=f"{ratio - 50:.0f}% vs 50% threshold")
            except Exception as e:
                st.warning(f"Could not parse detection JSON: {e}")
        else:
            st.info("No detection data yet.")
            st.markdown("""
            **Expected JSON format:**
            ```json
            {
              "red_tomatoes": 12,
              "green_tomatoes": 28,
              "timestamp": "2026-07-15T14:30:00"
            }
            ```
            Place at `Greenhouse/vision/latest.json`
            """)

    st.divider()
    st.markdown("### Roadmap")
    st.markdown("""
    | Phase | Feature | Status |
    |-------|---------|--------|
    | 3 (Jul 2026) | USB/IP camera → latest.jpg pipeline | ⏳ Pending hardware |
    | 3 (Jul 2026) | YOLOv8 inference script (local, no cloud) | ⏳ Pending |
    | 3.5 (Aug 2026) | Ripeness ratio → harvest alert via Pushover | ⏳ Pending |
    | 4 (2027) | Phenology tracking — leaf area index, disease detection | 🔵 Research |
    """)
    st.caption(
        "Reference: YOLOv8 (Ultralytics) — runs on Apple Silicon without GPU. "
        "Inference time ~50ms/frame on M4. No cloud dependency."
    )
