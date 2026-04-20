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
SEASON = ROOT.parent.parent.parent / "Season"  # -> Gardening/Season/
load_dotenv(ROOT / ".env")

# ── Ecowitt credentials ───────────────────────────────────────────────────────
ECOWITT_APP_KEY = os.getenv("ECOWITT_APPLICATION_KEY", "")
ECOWITT_API_KEY = os.getenv("ECOWITT_API_KEY", "")
DEVICE_MAC      = os.getenv("ECOWITT_DEVICE_MAC", "")

# ── LVPD engine ───────────────────────────────────────────────────────────────
def svp(T): return 0.6108 * math.exp(17.27 * T / (T + 237.3))
def calc_lvpd(T, rh, offset=2.0):
    return round(svp(T - offset) - svp(T) * (rh / 100), 3)

LVPD_ZONES = [
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
PRICES = {
    "San Marzano": 3.0, "Black Krim": 4.5, "Marmande": 3.5,
    "Sungold F1": 8.0, "Tigerella": 3.5, "Smarald": 3.5,
    "Passandra F1": 4.0, "Jalapeño Ruben": 12.0, "Yolo Wonder": 5.0,
    "Pantos": 5.0, "Tsaksoniki Aubergine": 4.0, "Kelvedon Wonder": 6.0,
    "Aquadulce Claudia": 5.0, "Defender F1": 3.0, "Uchiki Kuri": 2.5,
    "Kale": 3.0, "Spinach Matador": 4.0, "Dalmaziano": 8.0, "Cobra": 6.0,
}

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Maynooth Homestead",
    page_icon="🌱",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS
st.markdown("""
<style>
    .metric-card { background: #1e293b; border-radius: 8px; padding: 1rem; }
    .zone-badge { padding: 4px 12px; border-radius: 20px; font-weight: 600; display: inline-block; }
    [data-testid="stSidebarNav"] { display: none; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar navigation ────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://img.icons8.com/emoji/48/seedling.png", width=48)
    st.title("Homestead Hub")
    st.caption("Maynooth · Zone 8b · 52°N")
    st.divider()
    page = st.radio(
        "Navigate",
        ["🌡️ Live Greenhouse", "🌱 Production", "📋 Season Pipeline",
         "⏱️ Time & ROI", "💰 Finance", "🤖 Ask the Garden"],
        label_visibility="collapsed"
    )
    st.divider()
    st.caption(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}")
    if st.button("🔄 Refresh"):
        st.cache_data.clear()
        st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 1 — LIVE GREENHOUSE
# ═══════════════════════════════════════════════════════════════════════════════
if page == "🌡️ Live Greenhouse":
    st.title("🌡️ Live Greenhouse Status")

    @st.cache_data(ttl=300)
    def fetch_ecowitt():
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
    st.markdown(f"""
    <div style='background:{zone_color}22; border-left: 4px solid {zone_color};
         padding: 12px 20px; border-radius: 8px; margin: 16px 0;'>
    <strong style='color:{zone_color}; font-size:1.1em'>{zone_label}</strong>
    &nbsp;&nbsp;→&nbsp;&nbsp;{zone_action}
    &nbsp;&nbsp;|&nbsp;&nbsp;LVPD = {lvpd_val:.3f} kPa
    </div>
    """, unsafe_allow_html=True)

    # ── Irrigation recommendation ─────────────────────────────────────────────
    needs_water = (soil_ch1 < 35 or soil_ch2 < 35) and lvpd_val > 0.4
    st.info("💧 **Irrigation recommended** — soil moisture low" if needs_water
            else "✅ **No irrigation needed** — soil moisture adequate")

    # ── LVPD gauge ───────────────────────────────────────────────────────────
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=lvpd_val,
        title={"text": "LVPD (kPa)", "font": {"size": 18}},
        number={"suffix": " kPa", "font": {"size": 28}},
        gauge={
            "axis": {"range": [0, 2.5], "tickwidth": 1},
            "bar": {"color": zone_color},
            "steps": [
                {"range": [0, 0.4],  "color": "#fecaca"},
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

    if df.empty:
        st.info("📭 No harvest data yet. First tomatoes expected July 2026.")
        st.markdown("**Log a harvest:**")
        st.code('python Greenhouse/digital_twin/log_harvest.py harvest "San Marzano" --kg 0.45 --zone GH2N --quality 5', language="bash")
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
        ("San Marzano", "GH2N/GH2S", "2026-03-01", "🌿 Growing", "2026-07-15", "6 cordons"),
        ("Black Krim", "GH3N/GH5S", "2026-03-01", "🌿 Growing", "2026-07-20", "4 cordons"),
        ("Marmande", "GH3S/GH5N", "2026-03-01", "🌿 Growing", "2026-07-20", "4 cordons"),
        ("Sungold F1", "GH4N", "2026-03-01", "🌿 Growing", "2026-07-10", "Earliest fruiter"),
        ("Tigerella", "GH4S", "2026-03-01", "🌿 Growing", "2026-07-15", "2 cordons"),
        ("Passandra F1", "GH1N/GH1S", "2026-03-10", "🌿 Growing", "2026-07-01", "3 plants, training up ridge"),
        ("Jalapeño Ruben", "GH6N", "2026-01-04", "🌸 Establishing", "2026-08-01", "8 plants, 60cm canes"),
        ("Yolo Wonder", "GH6S", "2026-01-04", "🌸 Establishing", "2026-08-15", "3 plants"),
        ("Tsaksoniki Aubergine", "GH6S", "2026-01-04", "🌸 Establishing", "2026-08-15", "3 plants"),
        ("Pantos", "GH6S", "2026-03-09", "🌱 Growing", "2026-08-20", "12 plants total"),
        ("Aquadulce Claudia", "Bay 6", "2026-03-01", "🌿 Growing", "2026-06-20", "8 plants, against trellis"),
        ("Kelvedon Wonder", "Bay 5", "2026-03-18", "🌿 Growing", "2026-06-15", "36 positions"),
        ("Kale (Nero + Amara)", "Lab → Bay 4/5", "2026-03-10", "🪴 Lab ready", "2026-09-01", "46 plants, cage needed"),
        ("Defender F1 Courgette", "Bay 6 (outdoor)", "NOT SOWN", "⚠️ Sow May 4", "2026-07-20", "Urgent — heat mat"),
        ("Uchiki Kuri Squash", "Bay 6 (outdoor)", "NOT SOWN", "⚠️ Sow May 4", "2026-08-15", "Urgent — heat mat"),
        ("Dalmaziano Beans", "Bay 3", "NOT SOWN", "⚠️ Sow May 1", "2026-09-01", "Direct sow outdoors"),
    ]

    df_pipe = pd.DataFrame(pipeline_data, columns=["Variety", "Zone", "Sown", "Stage", "Expected Harvest", "Notes"])

    stage_order = {"🌿 Growing": 1, "🌸 Establishing": 2, "🪴 Lab ready": 3,
                   "⚠️ Sow May 4": 4, "⚠️ Sow May 1": 4, "⚠️ Sow soon": 4}
    df_pipe["_order"] = df_pipe["Stage"].map(stage_order).fillna(5)
    df_pipe = df_pipe.sort_values("_order").drop("_order", axis=1)

    # Gantt chart
    gantt_data = []
    today = pd.Timestamp("2026-04-20")
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
    fig.add_vline(x="2026-04-20", line_dash="dash", line_color="white",
                  annotation_text="Today", annotation_position="top right")
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
        {"Item": "8\" Clip Fan", "Est. €": 30, "When": "May 2026", "Linear": "GARDEN-16"},
        {"Item": "Neem Oil", "Est. €": 15, "When": "May 2026", "Linear": "Return sprint"},
        {"Item": "Calcium Fertiliser (chase FHF)", "Est. €": 0, "When": "May 2026", "Linear": "GARDEN-17"},
    ]), hide_index=True, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE 6 — ASK THE GARDEN (RAG placeholder)
# ═══════════════════════════════════════════════════════════════════════════════
elif page == "🤖 Ask the Garden":
    st.title("🤖 Ask the Garden")
    st.caption("Plant knowledge RAG — powered by Charles Dowding (No Dig), RHS, themarketgardener.com")

    RAG_STATUS = "not_built"  # change to "ready" when intel_garden tier is populated

    if RAG_STATUS == "not_built":
        st.warning("⏳ **Plant knowledge RAG not yet built** — this is GARDEN-3 (due May 31)")
        st.markdown("""
        **When ready, you'll be able to ask:**
        - *"When should I side-dress the San Marzano?"*
        - *"What LVPD is optimal for jalapeño fruit set?"*
        - *"No-dig compost depth for cucumber beds?"*
        - *"Signs of magnesium deficiency in Black Krim?"*

        **To build the intel_garden tier:**
        ```bash
        cd ~/building-energy-load-forecast
        # Ingest Charles Dowding no-dig.com articles + RHS grow guides
        ~/miniconda3/envs/ml_lab1/bin/python scripts/intel_ingest.py \\
          --dir "/Users/danalexandrubujoreanu/Personal Projects/Gardening/intel/docs/garden" \\
          --tier garden
        ```
        """)
    else:
        question = st.text_input("Ask about your crops:", placeholder="e.g. When to stop side-shooting tomatoes?")
        if question:
            import subprocess, sys
            result = subprocess.run(
                [sys.executable,
                 "/Users/danalexandrubujoreanu/Personal Projects/Gardening/AI/query_mba.py",
                 question, "garden"],
                capture_output=True, text=True
            )
            st.markdown(result.stdout)
