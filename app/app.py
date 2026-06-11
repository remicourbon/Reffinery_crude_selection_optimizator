"""crude-delivery-optimizer -- Streamlit UI.

Four pages over the core modules. NO business logic lives here: every number
on screen comes from core/ (covered by the unit tests).
Run with:  streamlit run app/app.py

Page logic:
- The sidebar fixes the state: refinery, config, arrival date, cargo volume,
  negotiated WS%, and the curve source (live yfinance, or manual parametric
  inputs -- shown only when live is off).
- `evaluate()` (core/decision.py) is run once per interaction and shared by
  Markets, Freight and Simulator: it prices every crude at its own departure
  tenor and solves the LP against product prices AT ARRIVAL.
"""

from datetime import date, timedelta
from pathlib import Path

# Self-contained path bootstrap: ensures `core` resolves whether launched via
# `python run.py`, `streamlit run app/app.py`, or from any working directory.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core.data_models import CUTS, RefineryConfig, load_dataset
from core.decision import evaluate
from core.market import benchmark_curves, product_prices
from core.refinery import (apply_upgrading, blend_diesel_sulfur, gpw,
                           utilisation)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

st.set_page_config(page_title="Crude Delivery Optimizer", layout="wide")


@st.cache_resource
def dataset():
    return load_dataset(DATA_DIR)


ds = dataset()

# ---------------------------------------------------------------- sidebar --
st.sidebar.title("Crude Delivery Optimizer")
page = st.sidebar.radio("Page", ["Markets", "Freight", "Refinery", "Simulator"])

st.sidebar.header("Refinery")
refinery_key = st.sidebar.selectbox(
    "Refinery", list(ds.refineries), format_func=lambda k: ds.refineries[k].name)
refinery = ds.refineries[refinery_key]

# Marseille is a live sandbox: its units are built from widgets, not YAML.
SANDBOX_KEY = "marseille"
is_sandbox = refinery_key == SANDBOX_KEY
config_override = None

if is_sandbox:
    base = refinery.configs["default"]
    st.sidebar.caption("Sandbox: toggle units, set capacities and the "
                       "straight-run sulfur limit. Changes are live.")
    cdu_cap = st.sidebar.slider("CDU capacity (kb/d)", 50, 500,
                                int(base.cdu_capacity_kbd), step=10)

    conv_on = st.sidebar.checkbox("Conversion unit", value=True)
    conv_unit, conv_cap = None, 0.0
    if conv_on:
        conv_unit = st.sidebar.selectbox(
            "Conversion type", ["fcc", "hcu"],
            help="FCC maximises gasoline; HCU maximises middle distillates.")
        conv_cap = float(st.sidebar.slider(
            "Conversion capacity (kb/d)", 10, 200,
            int(base.conversion_capacity_kbd), step=5))

    coker_on = st.sidebar.checkbox("Coker", value=False)
    coker_cap = float(st.sidebar.slider(
        "Coker capacity (kb/d)", 10, 150, 40, step=5)) if coker_on else 0.0

    sulfur_spec = st.sidebar.slider(
        "Straight-run diesel sulfur limit (%)", 0.05, 2.50,
        float(base.diesel_sulfur_spec_pct), step=0.05,
        help="Max sulfur of the straight-run diesel pool the hydrotreater "
             "can process. Lower = stricter = fewer sour crudes allowed.")

    config_override = RefineryConfig(
        key="sandbox", refinery_key=SANDBOX_KEY, cdu_capacity_kbd=float(cdu_cap),
        conversion_unit=conv_unit, conversion_capacity_kbd=conv_cap,
        coker_capacity_kbd=coker_cap, diesel_sulfur_spec_pct=sulfur_spec)
    config = config_override
    config_key = "sandbox"
else:
    config_key = st.sidebar.selectbox("Configuration", list(refinery.configs))
    config = refinery.configs[config_key]

st.sidebar.header("Delivery")
arrival = st.sidebar.date_input("Target arrival date",
                                value=date.today() + timedelta(days=30),
                                min_value=date.today())
volume = st.sidebar.number_input("Cargo volume (kbbl)", min_value=50.0,
                                 max_value=2000.0, value=650.0, step=50.0)
ws_user = st.sidebar.slider("Negotiated Worldscale (%)", 30, 200, 100,
                            help="Applied to all vessel classes; 100 keeps "
                                 "each class's typical rate.")
ws_pct = None if ws_user == 100 else float(ws_user)

st.sidebar.header("Curves")
use_live = st.sidebar.checkbox("Fetch live futures (yfinance)", value=False)
DEFAULT_SPOTS = {"brent": 80.0, "wti": 76.0}
DEFAULT_SLOPES = {"brent": 0.30, "wti": 0.30}
if use_live:
    st.sidebar.caption("Live Brent/WTI contracts; manual inputs hidden. "
                       "Falls back to defaults if the fetch fails.")
    spots, slopes = DEFAULT_SPOTS, DEFAULT_SLOPES
else:
    c1, c2 = st.sidebar.columns(2)
    spots = {"brent": c1.number_input("Brent spot", value=80.0, step=0.5),
             "wti": c2.number_input("WTI spot", value=76.0, step=0.5)}
    slopes = {"brent": c1.number_input("Brent $/mo", value=0.30, step=0.10,
                                       help=">0 contango, <0 backwardation"),
              "wti": c2.number_input("WTI $/mo", value=0.30, step=0.10)}
curves = benchmark_curves(date.today(), spots, slopes, use_live=use_live)

# One evaluation shared by Markets / Freight / Simulator.
decision, decision_error = None, None
try:
    decision = evaluate(ds, refinery_key, config_key, arrival, volume,
                        curves, ws_pct=ws_pct, config_override=config_override)
except Exception as exc:
    decision_error = str(exc)

prices_arrival = product_prices(ds.product_markets[refinery.product_market],
                                curves, arrival)


def margin_table() -> pd.DataFrame:
    """Per-crude option table, sorted by increasing net margin."""
    rows = []
    for key, o in decision.options.items():
        g = gpw(ds.crudes[key], prices_arrival)
        rows.append({
            "Crude": ds.crudes[key].name,
            "Vessel": o.freight.vessel_key,
            "Departure": o.departure_date,
            "Voyage (d)": round(o.voyage_days, 1),
            "FOB": round(o.fob_usd_bbl, 2),
            "Freight": round(o.freight.freight_usd_bbl, 2),
            "Financing": round(o.financing_usd_bbl, 2),
            "Ins.+loss": round(o.insurance_usd_bbl + o.losses_usd_bbl, 2),
            "CIF $/bbl": round(o.cif_usd_bbl, 2),
            "GPW at arrival": round(g, 2),
            "Net margin $/bbl": round(g - o.cif_usd_bbl, 2),
        })
    return pd.DataFrame(rows).sort_values("Net margin $/bbl")


# ----------------------------------------------------------- visual style --
# One fixed colour per cost component, reused on every chart so the eye
# learns to read the CIF stack at a glance.
COST_COLORS = {
    "FOB": "#3b6ea5",        # blue   -- the underlying barrel
    "Structure": "#e0a458",  # amber  -- what the curve pays for waiting
    "Freight": "#5a8a6b",    # green  -- the voyage
    "Financing": "#9b6a9e",  # purple -- cost of capital in transit
    "Ins.+loss": "#b0656a",  # red    -- leakage
}
CUT_COLORS = {
    "lpg": "#9ac1d9", "naphtha": "#6fa8c7", "kero": "#5a8a6b",
    "diesel": "#3f6f4f", "vgo": "#e0a458", "residue": "#9b6a9e",
    "loss": "#cccccc",
}
PLOT_LAYOUT = dict(
    margin=dict(t=30, b=30, l=10, r=10),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    font=dict(size=13), legend=dict(orientation="h", y=-0.15),
)


def stacked_cif_chart(df: pd.DataFrame) -> go.Figure:
    """All crudes side by side, CIF broken into stacked cost components.
    Lets a trader compare the *shape* of each delivered cost, not just the
    total: a long-haul crude shows a big Freight block and (in contango) a
    negative Structure block; a nearby crude shows the opposite."""
    fig = go.Figure()
    for comp in ["FOB", "Structure", "Freight", "Financing", "Ins.+loss"]:
        fig.add_bar(name=comp, x=df["Crude"], y=df[comp],
                    marker_color=COST_COLORS[comp],
                    text=[f"{v:+.1f}" for v in df[comp]],
                    textposition="inside", insidetextanchor="middle")
    fig.add_scatter(x=df["Crude"], y=df["CIF"], mode="markers+text",
                    name="CIF", marker=dict(color="black", size=9,
                                            symbol="diamond"),
                    text=[f"{v:.1f}" for v in df["CIF"]],
                    textposition="top center")
    fig.update_layout(barmode="relative", height=460,
                      yaxis_title="$/bbl", **PLOT_LAYOUT)
    return fig


def cif_waterfall(row: pd.Series) -> go.Figure:
    comps = ["FOB", "Structure", "Freight", "Financing", "Ins.+loss"]
    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute"] + ["relative"] * 4 + ["total"],
        x=comps + ["CIF"],
        y=[row["FOB"], row["Structure"], row["Freight"],
           row["Financing"], row["Ins.+loss"], 0.0],
        text=[f"{v:.2f}" for v in
              [row["FOB"], row["Structure"], row["Freight"],
               row["Financing"], row["Ins.+loss"], row["CIF"]]],
        textposition="outside",
        connector=dict(line=dict(color="#bbbbbb")),
        increasing=dict(marker=dict(color="#b0656a")),
        decreasing=dict(marker=dict(color="#5a8a6b")),
        totals=dict(marker=dict(color="#3b6ea5"))))
    fig.update_layout(height=420, yaxis_title="$/bbl", **PLOT_LAYOUT)
    return fig


def freight_frame() -> pd.DataFrame:
    """Per-crude CIF decomposition for the Freight page and stacked chart.
    FOB here is FOB-at-arrival (the structure-free reference); Structure is
    the curve effect of departing earlier. Their sum is FOB-at-departure."""
    rows = []
    for key, o in decision.options.items():
        crude = ds.crudes[key]
        fob_at_arrival = curves[crude.benchmark].price(arrival) \
            + crude.diff_usd_bbl
        rows.append({
            "key": key, "Crude": crude.name,
            "FOB": fob_at_arrival,
            "Structure": o.fob_usd_bbl - fob_at_arrival,
            "Freight": o.freight.freight_usd_bbl,
            "Financing": o.financing_usd_bbl,
            "Ins.+loss": o.insurance_usd_bbl + o.losses_usd_bbl,
            "CIF": o.cif_usd_bbl,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- markets --
if page == "Markets":
    st.title("Markets")
    st.caption("Each crude's forward curve = its benchmark futures curve + "
               "its static differential (no grade trades on futures; see "
               "README). All product prices are taken AT THE TARGET ARRIVAL "
               f"DATE ({arrival}).")

    st.subheader("Crude forward curves ($/bbl)")
    fig = go.Figure()
    for key, crude in ds.crudes.items():
        bench = curves[crude.benchmark]
        xs = [d for d, _ in bench.pillars]
        ys = [p + crude.diff_usd_bbl for _, p in bench.pillars]
        fig.add_scatter(x=xs, y=ys, mode="lines", name=crude.name)
    fig.update_layout(height=420, yaxis_title="$/bbl", **PLOT_LAYOUT)
    st.plotly_chart(fig, use_container_width=True)

    if decision:
        st.subheader("FOB at each crude's own tenor")
        st.caption("No tenor slider needed: with the arrival date fixed, "
                   "each crude's pricing date IS its departure date "
                   "(arrival - voyage).")
        rows = [{
            "Crude": ds.crudes[k].name,
            "Benchmark": ds.crudes[k].benchmark,
            "Diff ($/bbl)": ds.crudes[k].diff_usd_bbl,
            "Departure": o.departure_date,
            "FOB at departure": round(o.fob_usd_bbl, 2),
        } for k, o in decision.options.items()]
        st.dataframe(pd.DataFrame(rows), hide_index=True)
    elif decision_error:
        st.warning(decision_error)

    st.subheader(f"Product prices at arrival -- "
                 f"{refinery.product_market.upper()}")
    st.dataframe(pd.DataFrame([prices_arrival]).round(2), hide_index=True)

# ---------------------------------------------------------------- freight --
elif page == "Freight":
    st.title("Freight & structure")
    if decision is None:
        st.error(decision_error)
        st.stop()

    # --- structure callout (concept c) ------------------------------------
    bench = ds.refineries[refinery_key].product_market
    bench_name = ds.product_markets[bench].benchmark
    slope = slopes[bench_name]
    if abs(slope) < 1e-6:
        st.info(f"**{bench_name.upper()} curve is flat** "
                f"({slope:+.2f} $/month): the tenor of purchase doesn't "
                "change the FOB. Distance is paid purely through freight and "
                "financing.")
    elif slope > 0:
        st.success(f"**{bench_name.upper()} in contango** "
                   f"({slope:+.2f} $/month): buying earlier (longer voyages) "
                   "is *cheaper* on the curve. The structure **subsidises "
                   "distance** — it shows up as a negative bar below.")
    else:
        st.warning(f"**{bench_name.upper()} in backwardation** "
                   f"({slope:+.2f} $/month): buying earlier is *more "
                   "expensive*. The structure **penalises distance** — long "
                   "voyages pay a premium on the curve.")

    df = freight_frame()
    st.caption("FOB = price at the arrival date (structure-free reference). "
               "Structure = FOB(departure) - FOB(arrival): the curve effect "
               "of departing earlier for a longer voyage.")

    st.subheader("Delivered cost composition, all crudes ($/bbl)")
    st.plotly_chart(stacked_cif_chart(df.sort_values("CIF")),
                    use_container_width=True)

    st.subheader("CIF waterfall")
    chosen = st.selectbox("Crude", df.sort_values("CIF")["Crude"])
    row = df[df["Crude"] == chosen].iloc[0]
    st.plotly_chart(cif_waterfall(row), use_container_width=True)
    st.dataframe(df.drop(columns="key").round(2), hide_index=True)

# --------------------------------------------------------------- refinery --
elif page == "Refinery":
    st.title(f"Refinery -- {refinery.name} [{config_key}]")
    st.caption("Material balance and refining margin. Basket from the LP "
               "(Simulator inputs) or hand-picked.")

    source = st.radio("Basket source", ["Optimal (from Simulator inputs)",
                                        "Manual"], horizontal=True)
    if source.startswith("Optimal"):
        if decision is None or not decision.optimisation.optimal:
            st.error(decision_error or
                     f"Optimisation status: {decision.optimisation.status}")
            st.stop()
        basket = dict(decision.optimisation.basket)   # kbbl of the cargo
        unit_label = "kbbl"
        # The optimal basket is in kbbl of the cargo, so use the SAME scaled
        # config the LP used (capacities scaled to the cargo). Using the raw
        # kb/d config here would report nonsense like "CDU 1000%".
        active_config = decision.scaled_config
    else:
        st.write("Volumes (kb/d):")
        cols = st.columns(len(ds.crudes))
        basket = {}
        for col, (key, crude) in zip(cols, ds.crudes.items()):
            v = col.number_input(crude.name, min_value=0.0,
                                 max_value=float(config.cdu_capacity_kbd),
                                 value=0.0, step=10.0, key=f"b_{key}")
            if v > 0:
                basket[key] = v
        unit_label = "kb/d"
        # Manual volumes are in kb/d, matching the raw config capacities.
        active_config = config
        if not basket:
            st.info("Set at least one crude volume to run the refinery.")
            st.stop()

    rep = apply_upgrading(basket, ds.crudes, active_config,
                          ds.conversion_units, prices_arrival)
    util = utilisation(basket, ds.crudes, active_config)
    total = sum(basket.values())

    st.subheader("Crude slate")
    slate = pd.DataFrame(
        [{"Crude": ds.crudes[k].name, unit_label: round(v, 1),
          "Share %": round(100 * v / total, 1)} for k, v in basket.items()])
    st.dataframe(slate, hide_index=True)

    a, b, c, d = st.columns(4)
    a.metric("GPW", f"{rep.gpw_cdu_kusd_day / total:.2f} $/bbl")
    b.metric("Upgrading gain",
             f"{rep.upgrading_gain_kusd_day / total:.2f} $/bbl")
    c.metric("Total product worth",
             f"{rep.total_value_kusd_day / total:.2f} $/bbl")
    d.metric("Diesel pool sulfur", f"{util.diesel_sulfur_pct:.2f}%",
             delta=f"limit {active_config.diesel_sulfur_spec_pct:.2f}%",
             delta_color="off")

    st.subheader("Unit utilisation")
    st.progress(min(util.cdu_utilisation, 1.0),
                text=f"CDU: {util.cdu_utilisation:.0%}")
    if active_config.conversion_unit:
        u = rep.upgraded_conv_kbd / active_config.conversion_capacity_kbd
        st.progress(min(u, 1.0),
                    text=f"{active_config.conversion_unit.upper()}: {u:.0%} "
                         f"(uplift {rep.conv_uplift_usd_bbl:.2f} $/bbl)")
    if active_config.coker_capacity_kbd > 0:
        u = rep.upgraded_coker_kbd / active_config.coker_capacity_kbd
        st.progress(min(u, 1.0),
                    text=f"Coker: {u:.0%} "
                         f"(uplift {rep.coker_uplift_usd_bbl:.2f} $/bbl)")
    if source == "Manual" and not util.feasible:
        st.error("This basket violates at least one constraint "
                 "(capacity or straight-run sulfur limit).")

    st.subheader("Material balance after upgrading")
    bal = pd.DataFrame({
        unit_label: {c_: round(v, 1) for c_, v in rep.balance.items()},
        "%": {c_: round(100 * v / total, 1) for c_, v in rep.balance.items()},
    })
    left, right = st.columns([2, 1])
    cuts_order = [c for c in CUT_COLORS if c in rep.balance]
    fig = go.Figure(go.Bar(
        x=[c.upper() for c in cuts_order],
        y=[rep.balance[c] for c in cuts_order],
        marker_color=[CUT_COLORS[c] for c in cuts_order],
        text=[f"{rep.balance[c]:.0f}" for c in cuts_order],
        textposition="outside"))
    fig.update_layout(height=340, yaxis_title=unit_label, **PLOT_LAYOUT)
    left.plotly_chart(fig, use_container_width=True)
    right.dataframe(bal)

# -------------------------------------------------------------- simulator --
else:
    st.title("Simulator")
    st.caption(f"Deliver {volume:.0f} kbbl to {refinery.name} on {arrival}: "
               "each crude priced at its own departure tenor, LP allocation "
               "against product prices at arrival.")
    if decision is None:
        st.error(decision_error)
        st.stop()

    st.subheader("Options, sorted by net margin")
    st.dataframe(margin_table(), hide_index=True)
    for key, reason in decision.excluded.items():
        st.warning(f"{ds.crudes[key].name} excluded: {reason}")

    res = decision.optimisation
    if not res.optimal:
        st.error(f"Optimisation status: {res.status}")
        st.stop()

    st.subheader("Optimal cargo")
    a, b, c = st.columns(3)
    a.metric("Cargo margin", f"{res.margin_kusd_day:,.0f} k$")
    b.metric("Margin per bbl", f"{res.margin_kusd_day / volume:.2f} $/bbl")
    c.metric("Pool sulfur",
             f"{blend_diesel_sulfur(res.basket, ds.crudes):.2f}%")
    bk = dict(res.basket)
    fig = go.Figure(go.Bar(
        x=[ds.crudes[k].name for k in bk], y=list(bk.values()),
        marker_color="#3b6ea5",
        text=[f"{v:.0f}" for v in bk.values()], textposition="outside"))
    fig.update_layout(height=340, yaxis_title="kbbl", **PLOT_LAYOUT)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Shadow prices ($/bbl)")
    st.caption("Marginal value of relaxing each constraint: the 'sulfur' "
               "dual is this refinery's own sweet/sour premium; a zero "
               "'conv_capacity' dual means the unit is feed-limited.")
    st.dataframe(pd.DataFrame(
        [{"Constraint": k, "Dual": round(v, 3)}
         for k, v in res.shadow_prices.items()]), hide_index=True)
    