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

from core.data_models import CUTS, ALL_CUTS, RefineryConfig, load_dataset
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
page = st.sidebar.radio("Page", ["Simulator", "Markets", "Freight",
                                 "Refinery", "Reference data"])

st.sidebar.header("Refinery")
refinery_key = st.sidebar.selectbox(
    "Refinery", list(ds.refineries), format_func=lambda k: ds.refineries[k].name)
refinery = ds.refineries[refinery_key]

# Any refinery flagged `sandbox: true` in refineries.yaml is a live sandbox:
# its units are built from widgets, not read from a fixed YAML config.
is_sandbox = refinery.sandbox
config_override = None

if is_sandbox:
    base = refinery.configs["default"]
    st.sidebar.caption("Sandbox: toggle units, set capacities and the "
                       "straight-run sulfur limit. Changes are live.")
    cdu_cap = st.sidebar.slider("CDU capacity (kb/d)", 50, 700,
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

    reformer_on = st.sidebar.checkbox("Reformer (naphtha -> gasoline)",
                                      value=True)
    reformer_cap = float(st.sidebar.slider(
        "Reformer capacity (kb/d)", 10, 150, 40, step=5)) \
        if reformer_on else 0.0

    sulfur_spec = st.sidebar.slider(
        "Straight-run diesel sulfur limit (%)", 0.05, 2.50,
        float(base.diesel_sulfur_spec_pct), step=0.05,
        help="Max sulfur of the straight-run diesel pool the hydrotreater "
             "can process. Lower = stricter = fewer sour crudes allowed.")

    config_override = RefineryConfig(
        key="sandbox", refinery_key=refinery_key, cdu_capacity_kbd=float(cdu_cap),
        conversion_unit=conv_unit, conversion_capacity_kbd=conv_cap,
        coker_capacity_kbd=coker_cap, diesel_sulfur_spec_pct=sulfur_spec,
        reformer_capacity_kbd=reformer_cap)
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
ws_user = st.sidebar.slider("Negotiated Worldscale (%)", 30, 400, 100,
                            help="Applied to all vessel classes; 100 keeps "
                                 "each class's typical rate.")
ws_pct = None if ws_user == 100 else float(ws_user)

st.sidebar.header("Curves")
use_live = st.sidebar.checkbox("Fetch live futures (yfinance)", value=False)
DEFAULT_SPOTS = {"brent": 83.0, "wti": 81.0}
DEFAULT_SLOPES = {"brent": -0.50, "wti": -0.70}
if use_live:
    st.sidebar.caption("Live Brent/WTI contracts; manual inputs hidden. "
                       "Falls back to defaults if the fetch fails.")
    spots, slopes = DEFAULT_SPOTS, DEFAULT_SLOPES
else:
    c1, c2 = st.sidebar.columns(2)
    spots = {"brent": c1.number_input("Brent spot", value=83.0, step=0.5),
             "wti": c2.number_input("WTI spot", value=81.0, step=0.5)}
    slopes = {"brent": c1.number_input("Brent $/mo", value=-0.50, step=0.10,
                                       help=">0 contango, <0 backwardation"),
              "wti": c2.number_input("WTI $/mo", value=-0.70, step=0.10)}
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
    """Per-crude option table, best net margin first.

    Prix maintenant is the crude's FOB spot price today:
    benchmark front month + static crude differential.

    Structure is the forward curve effect:
    Structure = FOB at departure - Prix maintenant.

    Therefore:
    Prix maintenant + Structure = FOB at departure.

    Net margin is computed on TOTAL PRODUCT WORTH (straight-run GPW + the
    upgrading gain this refinery's units would extract from that crude), not
    on bare GPW -- otherwise a conversion refinery's economics look wrong.
    Total product worth per crude is obtained by running apply_upgrading on a
    single-crude basket at the SAME scaled config the LP used.
    """
    rows = []
    for key, o in decision.options.items():
        crude = ds.crudes[key]

        # Price today: benchmark front month + crude differential.
        prix_maintenant = curves[crude.benchmark].front + crude.diff_usd_bbl

        # Forward curve effect between today and the crude's departure date.
        structure = o.fob_usd_bbl - prix_maintenant

        single = {key: decision.volume_kbbl}
        rep = apply_upgrading(single, ds.crudes, decision.scaled_config,
                              ds.conversion_units, prices_arrival)
        tpw = rep.total_value_kusd_day / decision.volume_kbbl  # $/bbl

        rows.append({
            "Crude": crude.name,
            "Vessel": o.freight.vessel_key,
            "Departure": o.departure_date,
            "Voyage (d)": round(o.voyage_days, 1),
            "Prix maintenant": round(prix_maintenant, 2),
            "Structure": round(structure, 2),
            "Freight": round(o.freight.freight_usd_bbl, 2),
            "Financing": round(o.financing_usd_bbl, 2),
            "Ins.+loss": round(o.insurance_usd_bbl + o.losses_usd_bbl, 2),
            "CIF $/bbl": round(o.cif_usd_bbl, 2),
            "Total product worth": round(tpw, 2),
            "Net margin $/bbl": round(tpw - o.cif_usd_bbl, 2),
        })

    return pd.DataFrame(rows).sort_values("Net margin $/bbl", ascending=False)


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
    "lpg": "#9ac1d9", "naphtha": "#6fa8c7", "gasoline": "#4a7fb0",
    "kero": "#5a8a6b", "diesel": "#3f6f4f", "vgo": "#e0a458",
    "residue": "#9b6a9e", "loss": "#cccccc",
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
    components = ["FOB Spot", "Structure", "Freight",
                  "Financing", "Ins.+loss"]
    color_of = {"FOB Spot": COST_COLORS["FOB"],
                "Structure": COST_COLORS["Structure"],
                "Freight": COST_COLORS["Freight"],
                "Financing": COST_COLORS["Financing"],
                "Ins.+loss": COST_COLORS["Ins.+loss"]}
    fig = go.Figure()
    for comp in components:
        fig.add_bar(name=comp, x=df["Crude"], y=df[comp],
                    marker_color=color_of[comp],
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
    spot = row["FOB Spot"]
    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=["absolute"] + ["relative"] * 4 + ["total"],
        x=["FOB Spot", "Structure", "Freight", "Financing",
           "Ins.+loss", "CIF"],
        y=[spot, row["Structure"], row["Freight"],
           row["Financing"], row["Ins.+loss"], 0.0],
        text=[f"{v:.2f}" for v in
              [spot, row["Structure"], row["Freight"],
               row["Financing"], row["Ins.+loss"], row["CIF"]]],
        textposition="outside",
        connector=dict(line=dict(color="#bbbbbb")),
        increasing=dict(marker=dict(color="#b0656a")),
        decreasing=dict(marker=dict(color="#5a8a6b")),
        totals=dict(marker=dict(color="#3b6ea5"))))
    fig.update_layout(height=420, yaxis_title="$/bbl", **PLOT_LAYOUT)
    return fig


def freight_frame() -> pd.DataFrame:
    """Per-crude CIF decomposition, built from FOB SPOT.

    The waterfall starts at FOB Spot (today's price, tenor 0 -- the tangible
    screen price) and adds Structure as a visible bar: Structure =
    FOB(departure) - FOB(spot), the curve effect of buying at the departure
    tenor rather than today. Positive in contango (subsidises distance),
    Negative in backwardation. The columns FOB Spot + Structure reconstruct
    FOB at departure, which is the real price paid and matches the Markets
    page.
    """
    rows = []
    for key, o in decision.options.items():
        crude = ds.crudes[key]
        fob_spot = curves[crude.benchmark].front + crude.diff_usd_bbl
        rows.append({
            "key": key, "Crude": crude.name,
            "FOB Spot": fob_spot,
            "Structure": o.fob_usd_bbl - fob_spot,
            "Freight": o.freight.freight_usd_bbl,
            "Financing": o.financing_usd_bbl,
            "Ins.+loss": o.insurance_usd_bbl + o.losses_usd_bbl,
            "FOB at departure": o.fob_usd_bbl,
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
        st.subheader("FOB Spot per crude")
        st.caption("FOB Spot is each crude's price today (tenor 0): the "
                   "benchmark front month plus the crude's static "
                   "differential.")
        rows = [{
            "Crude": ds.crudes[k].name,
            "Benchmark": ds.crudes[k].benchmark,
            "Diff ($/bbl)": ds.crudes[k].diff_usd_bbl,
            "FOB Spot": round(curves[ds.crudes[k].benchmark].front
                              + ds.crudes[k].diff_usd_bbl, 2),
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
        st.success(
            f"**{bench_name.upper()} is in contango** "
            f"({slope:+.2f} $/month): earlier pricing is cheaper than later pricing. "
            "The structure component rewards crudes priced closer to spot and "
            "penalizes crudes priced further out on the curve.")
    else:
        st.warning(f"**{bench_name.upper()} in backwardation** "
                   f"({slope:+.2f} $/month): earlier pricing is more expensive than later pricing. "
                   "The structure component penalizes crudes priced closer to spot and "
                   "benefits crudes priced further out on the curve.")

    df = freight_frame().sort_values("CIF").reset_index(drop=True)
    st.caption("The waterfall starts at **FOB Spot** (today's price) and adds "
               "**Structure** = FOB(departure) - FOB(spot): what the curve "
               "charges (or pays) for buying at the departure tenor instead "
               "of today. FOB Spot + Structure = FOB at departure (the real "
               "price paid, matching the Markets page).")

    # --- cheapest-crude waterfall, on top --------------------------------
    cheapest = df.iloc[0]
    st.subheader(f"CIF waterfall -- cheapest delivered: {cheapest['Crude']} "
                 f"({cheapest['CIF']:.2f} $/bbl)")
    options = list(df["Crude"])
    chosen = st.selectbox("Crude", options, index=0)
    row = df[df["Crude"] == chosen].iloc[0]
    st.plotly_chart(cif_waterfall(row), use_container_width=True)

    # --- comparisons across all crudes -----------------------------------
    st.subheader("Delivered cost composition, all crudes ($/bbl)")
    st.plotly_chart(stacked_cif_chart(df), use_container_width=True)
    st.dataframe(df.drop(columns="key").round(2), hide_index=True)

    # --- structure cost table --------------------------------------------
    st.subheader("Structure cost")
    st.caption("How the forward curve turns today's spot into the price you "
               "actually pay at departure. Positive structure (contango) "
               "means the curve subsidises the wait; negative "
               "(backwardation) means it penalises it.")
    struct = df[["Crude", "FOB Spot", "FOB at departure", "Structure"]].copy()
    struct.columns = ["Crude", "FOB Spot ($/bbl)", "FOB at departure ($/bbl)",
                      "Structure cost ($/bbl)"]
    st.dataframe(struct.round(2), hide_index=True)

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

    if unit_label == "kbbl":   # Optimal mode: basket is a cargo in kbbl
        nameplate_cdu = config.cdu_capacity_kbd
        feed_days = total / nameplate_cdu
        if feed_days > 15:
            st.warning(f"This cargo is **{feed_days:.1f} days** of feed at "
                       f"{nameplate_cdu:.0f} kb/d. Above ~15 days, storage and "
                       "market exposure get unrealistic for a single purchase.")
        else:
            st.info(f"This cargo is **{feed_days:.1f} days** of feed at "
                    f"{nameplate_cdu:.0f} kb/d nameplate CDU.")
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

    # --- refinery characteristics -----------------------------------------
    st.subheader("Refinery characteristics")
    st.caption("Nameplate capacity (kb/d throughput, independent of cargo "
               "size), utilisation (feed actually upgraded / capacity over "
               "the processing window) and the per-barrel uplift each unit "
               "earns at current prices.")
    char_rows = [{
        "Unit": "CDU", "Active": "yes",
        "Capacity (kb/d)": round(config.cdu_capacity_kbd, 1),
        "Utilisation %": round(100 * util.cdu_utilisation, 1),
        "Uplift $/bbl": "-",
    }]
    if config.conversion_unit:
        cu = config.conversion_unit.upper()
        char_rows.append({
            "Unit": cu, "Active": "yes",
            "Capacity (kb/d)": round(config.conversion_capacity_kbd, 1),
            "Utilisation %": round(
                100 * rep.upgraded_conv_kbd
                / active_config.conversion_capacity_kbd, 1),
            "Uplift $/bbl": round(rep.conv_uplift_usd_bbl, 2),
        })
    else:
        char_rows.append({"Unit": "Conversion (FCC/HCU)", "Active": "no",
                          "Capacity (kb/d)": "-",
                          "Utilisation %": "-", "Uplift $/bbl": "-"})
    if config.coker_capacity_kbd > 0:
        char_rows.append({
            "Unit": "Coker", "Active": "yes",
            "Capacity (kb/d)": round(config.coker_capacity_kbd, 1),
            "Utilisation %": round(
                100 * rep.upgraded_coker_kbd
                / active_config.coker_capacity_kbd, 1),
            "Uplift $/bbl": round(rep.coker_uplift_usd_bbl, 2),
        })
    else:
        char_rows.append({"Unit": "Coker", "Active": "no",
                          "Capacity (kb/d)": "-",
                          "Utilisation %": "-", "Uplift $/bbl": "-"})
    if config.reformer_capacity_kbd > 0:
        char_rows.append({
            "Unit": "Reformer", "Active": "yes",
            "Capacity (kb/d)": round(config.reformer_capacity_kbd, 1),
            "Utilisation %": round(
                100 * rep.upgraded_reformer_kbd
                / active_config.reformer_capacity_kbd, 1),
            "Uplift $/bbl": round(rep.reformer_uplift_usd_bbl, 2),
        })
    else:
        char_rows.append({"Unit": "Reformer", "Active": "no",
                          "Capacity (kb/d)": "-",
                          "Utilisation %": "-", "Uplift $/bbl": "-"})
    char_df = pd.DataFrame(char_rows)
    for col in ["Capacity (kb/d)", "Utilisation %", "Uplift $/bbl"]:
        char_df[col] = char_df[col].astype(str)
    st.dataframe(char_df, hide_index=True)

# ---------------------------------------------------------- reference data --
elif page == "Reference data":
    st.title("Reference data")
    st.caption("All the static assumptions behind the model, straight from "
               "the YAML files. Values are illustrative and approximate "
               "(Platts/Argus/Worldscale are paid) -- see the README for "
               "sources and simplifications. Nothing here is hidden: this is "
               "what every other page computes from.")

    tab_crudes, tab_ref, tab_vessels, tab_routes, tab_units, tab_mkt = st.tabs(
        ["Crudes", "Refineries", "Vessels", "Routes", "Conversion units",
         "Product markets"])

    with tab_crudes:
        st.subheader("Crude assays")
        st.caption("Yields are volume fractions of each cut (sum to 1.0 with "
                   "loss). FOB price = benchmark futures curve + the static "
                   "differential. diesel S% is the sulfur of the diesel cut "
                   "(the only sulfur the LP constrains).")
        rows = []
        for k, c in ds.crudes.items():
            r = {"Crude": c.name, "API": c.api, "Sulfur %": c.sulfur_pct,
                 "Benchmark": c.benchmark, "Diff $/bbl": c.diff_usd_bbl,
                 "FOB port": c.fob_port, "Diesel S%": c.diesel_sulfur_pct}
            for cut in ALL_CUTS:
                r[cut] = round(c.yields[cut], 3)
            rows.append(r)
        st.dataframe(pd.DataFrame(rows), hide_index=True)

    with tab_ref:
        st.subheader("Refineries and configurations")
        st.caption("Each refinery has a discharge port, a max vessel the port "
                   "accepts, a regional product market, and one or more "
                   "configurations. Sandbox refineries show their default "
                   "config here; their units are editable live in the sidebar.")
        rows = []
        for rk, r in ds.refineries.items():
            for ck, cfg in r.configs.items():
                rows.append({
                    "Refinery": r.name, "Port": r.port,
                    "Max vessel": r.max_vessel,
                    "Product market": r.product_market,
                    "Config": ck,
                    "CDU kb/d": cfg.cdu_capacity_kbd,
                    "Conversion": cfg.conversion_unit or "-",
                    "Conv. kb/d": cfg.conversion_capacity_kbd,
                    "Coker kb/d": cfg.coker_capacity_kbd,
                    "Reformer kb/d": cfg.reformer_capacity_kbd,
                    "Diesel S spec %": cfg.diesel_sulfur_spec_pct,
                })
        st.dataframe(pd.DataFrame(rows), hide_index=True)

    with tab_vessels:
        st.subheader("Vessel classes")
        st.caption("Cargo size, service speed, typical negotiated Worldscale "
                   "%, and port days (load + discharge). The model picks the "
                   "cheapest feasible vessel for each route and cargo size.")
        rows = [{
            "Vessel": vk, "Cargo (kbbl)": v.cargo_kbbl,
            "Speed (kn)": v.speed_knots,
            "Typical WS %": v.typical_ws_pct, "Port days": v.port_days,
        } for vk, v in ds.vessels.items()]
        st.dataframe(pd.DataFrame(rows), hide_index=True)

    with tab_routes:
        st.subheader("Routes")
        st.caption("Distance (nautical miles) and Worldscale flat rate ($/t) "
                   "for each port pair. Keys are alphabetically sorted port "
                   "pairs. Voyage time = distance / speed + port days.")
        rows = [{
            "Route": rk, "Distance (nm)": r.distance_nm,
            "WS flat ($/t)": r.ws_flat_rate,
        } for rk, r in sorted(ds.routes.items())]
        st.dataframe(pd.DataFrame(rows), hide_index=True)

    with tab_units:
        st.subheader("Conversion units")
        st.caption("What one barrel of feed becomes when upgraded (volume "
                   "fractions, sum to 1.0). FCC and reformer make gasoline; "
                   "the reformer's feed is naphtha, FCC/HCU's is VGO, the "
                   "coker's is residue.")
        rows = []
        for uk, u in ds.conversion_units.items():
            r = {"Unit": uk, "Feed": u.feed}
            for cut in ALL_CUTS:
                r[cut] = round(u.outputs[cut], 3)
            rows.append(r)
        st.dataframe(pd.DataFrame(rows), hide_index=True)

    with tab_mkt:
        st.subheader("Product markets (cracks vs benchmark, $/bbl)")
        st.caption("Each regional market prices products as benchmark curve + "
                   "a constant crack per cut. Gasoline crack sits well above "
                   "naphtha -- that gap is what the reformer captures.")
        rows = []
        for mk, m in ds.product_markets.items():
            r = {"Market": mk, "Benchmark": m.benchmark}
            for cut in CUTS:
                r[cut] = m.cracks[cut]
            rows.append(r)
        st.dataframe(pd.DataFrame(rows), hide_index=True)

# -------------------------------------------------------------- simulator --
else:
    st.title("Simulator")
    st.caption(f"Deliver {volume:.0f} kbbl to {refinery.name} on {arrival}: "
               "each crude priced at its own departure tenor, LP allocation "
               "against product prices at arrival.")
    if decision is None:
        st.error(
            "No deliverable crude for the selected cargo size and discharge port. "
            "The selected cargo is probably larger than the maximum vessel allowed "
            "at the discharge port. Try reducing the cargo volume.")
        with st.expander("Technical details"):
            st.write(decision_error)
        st.stop()

    st.subheader("Options, best net margin first")
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
    vessels_used = sorted({decision.options[k].freight.vessel_key
                           for k in bk})
    st.caption("Vessel(s): " + ", ".join(
        f"**{ds.crudes[k].name}** -> {decision.options[k].freight.vessel_key} "
        f"({decision.options[k].voyage_days:.0f} d, departs "
        f"{decision.options[k].departure_date})" for k in bk))
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
    
