# crude-delivery-optimizer

A physical crude oil delivery decision tool. Given a refinery, a **target
arrival date** and a cargo volume, it prices every candidate crude at its own
tenor (FOB at departure + freight + financing + insurance + losses), then a
linear program allocates the cargo to maximise refining margin (total product
worth minus delivered cost) against product prices **at arrival**.

The question it answers, concretely: *I run Dangote -- should I buy US crude
spot today and wait 26 days at sea, or buy Nigerian crude forward and have it
delivered in 5?* The honest comparison requires both options to land on the
same date, each leg priced at its own point on the forward curve. That
principle is the core of the tool.

## Architecture

```
data/        YAML only -- assays, refineries, routes, vessels, conversion
             units, product markets. Validated aggressively at load time.
core/
  data_models.py   frozen dataclasses + loaders + referential integrity
  refinery.py      GPW, material balance, sulfur pool, upgrading (uplift)
  freight.py       Worldscale -> $/bbl, voyage time, dead freight, vessel rule
  market.py        futures curves (yfinance or parametric fallback), FOB and
                   product prices at any tenor
  lp_model.py      the basket LP (PuLP) -- full formulation in the docstring
  decision.py      orchestrator: arrival date -> departure tenors -> LP
tests/       79 unit tests; hand-computed reference numbers in comments
app/         Streamlit, 4 pages (Markets / Freight / Refinery / Simulator)
             -- no business logic in the UI
```

Run: `python run.py` (installs deps on first run, fixes sys.path, launches
Streamlit). Or manually:
`pip install -r requirements.txt && python -m pytest && python -m streamlit run app/app.py`

## Pricing vocabulary (used consistently across all pages)

- **FOB Spot** -- today's price of the crude (benchmark front + differential).
  The tangible screen price, tenor 0.
- **FOB at departure** -- the price actually paid: the crude priced at its
  departure tenor (arrival date minus voyage days). This is the real cash
  outlay and is shown on Markets and Simulator.
- **Structure cost** = FOB at departure - FOB Spot. What the forward curve
  charges (or pays) for buying at the departure tenor instead of today.
  In **contango** the structure of a longer voyage is *lower* (it loads at a
  tenor closer to spot), so the curve favours distance; in **backwardation**
  it is *higher*, penalising distance. The Freight page isolates this in the
  CIF waterfall (which starts at FOB Spot) and in a dedicated structure table.
- **Financing** -- ACT/360 interest on the FOB value over the voyage, a
  *separate* mechanism from structure: structure is the curve effect,
  financing is the cost of capital tied up at sea.
- **CIF** = FOB at departure + freight + financing + insurance + losses.
- **Total product worth** = straight-run GPW + upgrading gain the refinery's
  units extract. Net margin is computed against this (not bare GPW), so a
  conversion refinery's economics are stated correctly.

## The LP in one paragraph

Decision variables: volumes purchased per crude, plus two upgrading volumes
(conversion unit, coker). Objective: straight-run margin per crude plus
upgrading uplift. Constraints: CDU capacity, optional fixed cargo volume,
upgrading bounded by both feed availability and unit capacity, and a sulfur
spec on the straight-run diesel pool **linearised** as
`sum x_i * y_diesel_i * (S_i - S_max) <= 0` (multiply the pool-average ratio
by its positive denominator). Without constraints the LP would buy 100% of
the best crude -- all the intelligence is in the constraints. Shadow prices
are exported: the sulfur dual is the refinery's own sweet/sour premium, the
conversion-capacity dual the marginal value of upgrading room.

## Simplifications (assumed, deliberate)

Data walls are real: Platts/Argus assessments and Worldscale flat-rate tables
are paid. This project replaces them with editable defaults and states it.

- **Grade differentials are static** ($/bbl vs Brent or WTI futures),
  editable; real differentials move daily (Platts/Argus).
- **Product cracks are constant in time**: product forwards inherit the
  benchmark's structure. Ignores product seasonality and crack shocks.
- **Sulfur spec = max sulfur of the straight-run diesel pool the refinery's
  hydrotreater can process** -- not the 10-50 ppm finished-product spec,
  which is met downstream by hydrotreating (not modelled, nor is its
  hydrogen cost; the HCU uplift is therefore generous).
- **Conversion-unit diesel is assumed hydrotreated separately**: the sulfur
  constraint applies to straight-run diesel only.
- **Upgrading is sequential and acyclic** (conversion, then coker; the
  coker's VGO make is not recycled).
- **Freight per crude is quoted at the full cargo volume**; after the LP
  splits the basket, parcels would re-price slightly. Iterating freight x LP
  is out of scope. Multi-voyage cargoes too.
- **The negotiated WS% applies to all vessel classes** when overridden.
- **Cargo-vs-rate convention**: kb/d unit capacities are scaled pro-rata to
  the cargo (processing at full CDU rate).
- Vessel choice is deterministic (cheapest feasible, dead freight included)
  and happens **before** the LP, keeping the problem linear rather than MILP.
- Yields, assay sulfur, distances, WS flat rates, cracks and vessel
  parameters are **approximate and illustrative** -- marked as such in the
  YAML files. Replace with sourced values (public assays: Equinor, ExxonMobil,
  BP; distances: searoutes-type calculators) before relying on outputs.

## Sources

- Crude assays: simplified to a 6-cut schema from public assay libraries.
- Futures: yfinance (Brent BZ, WTI CL) with a parametric fallback
  (spot + constant $/month slope).
- Everything Platts/Argus/Worldscale-priced: editable defaults, flagged.

This is a decision-support and learning project, not a trading system.

## Refineries

Four archetypes. Three are fixed (loaded from YAML); **Marseille is a live
sandbox** whose units are configured from the app, not the data files.

- **Dangote** (Lekki, VLCC port) -- large FCC, West-African product market.
- **Rotterdam** -- hydroskimming and FCC configs; structurally weak margins
  under current prices (realistic for NW Europe).
- **Singapore** -- hydrocracking, and FCC+coker; Asian product market.
- **Marseille** (Fos-sur-Mer, Mediterranean market) -- a sandbox: toggle the
  conversion unit (FCC or HCU) and the coker on/off, set their capacities and
  the straight-run diesel sulfur limit, all live. Built by passing a
  `config_override` to `decision.evaluate`, so the UI constructs a
  `RefineryConfig` on the fly while the data files stay untouched.

Mediterranean crudes were added for Marseille: **Saharan Blend** (Algeria,
light sweet) and **CPC Blend** (Black Sea, light, mildly sour).
