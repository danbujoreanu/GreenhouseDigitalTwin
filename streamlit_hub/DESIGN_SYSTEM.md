# Maynooth Homestead — Streamlit Design System

*Applied: April 2026. Theme: Forest Dark — a monitoring dashboard aesthetic inspired by terminal/ops tooling.*

---

## Colour Tokens

| Token | Hex | Role |
|-------|-----|------|
| `primaryColor` | `#52b788` | Forest green — CTAs, active states, accent lines |
| `backgroundColor` | `#0d1f16` | Main content background |
| `secondaryBackgroundColor` | `#122b1e` | Sidebar, card backgrounds |
| `textColor` | `#e0ede7` | Body text — warm off-white |
| Border | `#1d3829` | Subtle dividers and card borders |
| Metric label | `#7ab89a` | Muted green for metadata labels |
| Danger | `#f87171` | Error / critical alerts |
| Warning | `#fbbf60` | Orange — elevated risk |
| Info | `#7dd3fc` | Sky blue — informational |

---

## Theme Base File

**Location:** `streamlit_hub/.streamlit/config.toml`

```toml
[theme]
base            = "dark"
primaryColor    = "#52b788"
backgroundColor = "#0d1f16"
secondaryBackgroundColor = "#122b1e"
textColor       = "#e0ede7"
font            = "sans serif"
```

**Rule:** Always set `base = "dark"` before adding custom CSS. Never fight Streamlit's default light theme with dark `background` overrides in CSS — it creates mixed-contrast issues. Let the config handle the base, CSS handles refinements only.

---

## Component Patterns

### Status Pills

```html
<span class="status-pill pill-ok">🟢 Sensors live</span>
<span class="status-pill pill-warn">🟡 Fan: standby</span>
<span class="status-pill pill-bad">🔴 Botrytis risk</span>
<span class="status-pill pill-info">🔵 RAG ready</span>
```

| Class | Use case |
|-------|----------|
| `pill-ok` | System healthy, within target |
| `pill-warn` | Elevated, needs monitoring |
| `pill-bad` | Risk active, action needed |
| `pill-info` | Informational, neutral |

### Info Banner

```html
<div class="info-banner">
  📚 <strong>Title</strong> · Supporting text here
</div>
```
Use for: page-level context, RAG corpus status, one-line summaries.

### LVPD Zone Banner (dynamic colour injection)
```python
st.markdown(f"""
<div style='background:{zone_color}18; border-left: 4px solid {zone_color};
     padding: 14px 22px; border-radius: 10px; margin: 16px 0;'>
  <strong style='color:{zone_color}'>{zone_label}</strong>
  → <span style='color:#d1fae5'>{zone_action}</span>
</div>
""", unsafe_allow_html=True)
```
The `18` suffix on `zone_color` makes the hex colour 9% opacity (alpha in hex: 00=0%, 18≈10%, 40=25%, 80=50%, FF=100%).

---

## Layout Rules

- **Sidebar width:** default Streamlit (~240px) — avoid custom widths, breaks on small screens
- **Metric row:** use `st.columns(5)` for 5 sensors — never more than 5 metrics in a row
- **Chart height:** 280px for gauges, 600px for Gantt/timeline, auto for bar/line
- **Page titles:** `st.title()` only — don't duplicate with `st.header()`
- **Dividers:** `st.divider()` between major sections, never `st.markdown("---")`

---

## Plotly Dark Theme Defaults

All Plotly charts should include:
```python
fig.update_layout(
    paper_bgcolor="rgba(0,0,0,0)",   # transparent — picks up Streamlit bg
    plot_bgcolor="rgba(0,0,0,0)",
    font_color="#e0ede7",
    height=280,
    margin=dict(t=40, b=10)
)
```

**Timeline chart gotcha:** `px.timeline` uses ms-epoch integers on x-axis. Never pass `x=timestamp.isoformat()` to `add_vline` with `annotation_text` — use epoch ms + separate `add_annotation`.

---

## Alternative Templates (for future pages)

| Template style | Use when | Key config |
|----------------|----------|------------|
| **Forest Dark** (current) | Ops/monitoring dashboards | `backgroundColor = "#0d1f16"`, green accents |
| **Deep Navy** | Financial dashboards, portfolios | `backgroundColor = "#0a0f1e"`, `primaryColor = "#60a5fa"` (blue) |
| **Warm Earth** | Research/scientific reporting | `backgroundColor = "#1a1209"`, `primaryColor = "#d97706"` (amber) |
| **Minimal Light** | Public-facing / portfolio sharing | `base = "light"`, `primaryColor = "#2d6a4f"`, standard Streamlit |

To switch: update `config.toml` and adjust `info-banner` background opacity in CSS.

---

## Streamlit CSS Specifics (tested April 2026)

| Target | Selector |
|--------|----------|
| Metric card container | `[data-testid="metric-container"]` |
| Metric label | `[data-testid="stMetricLabel"]` |
| Metric value | `[data-testid="stMetricValue"]` |
| Metric delta | `[data-testid="stMetricDelta"]` |
| Sidebar | `[data-testid="stSidebar"]` |
| Sidebar nav (hide) | `[data-testid="stSidebarNav"] { display: none; }` |
| DataFrame | `[data-testid="stDataFrame"]` |
| Text input | `[data-testid="stTextInput"] input` |

**Caution:** Streamlit updates these selectors between versions. If CSS stops working after a `pip upgrade streamlit`, re-inspect with browser DevTools.
