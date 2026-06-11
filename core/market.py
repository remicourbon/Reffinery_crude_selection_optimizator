"""Market data: futures curves, FOB crude prices and product prices at any
tenor.

This module owns the entire time dimension of the model. Everything
downstream (refinery.py, lp_model.py) receives plain prices and never knows
which tenor they were taken at -- decision.py is the only orchestrator of
"price what, at which date".

Conventions (see README):
- Crudes price as: benchmark futures curve + static differential
  (crude.diff_usd_bbl). Real grade differentials are Platts/Argus; ours are
  editable defaults.
- Products price as: benchmark curve + constant regional crack
  (ProductMarket.cracks). Cracks are flat in time; the forward structure of
  products is inherited from the crude benchmark.
- Curve source: live futures via yfinance when available, else a parametric
  curve (spot + constant slope in $/month) -- same fallback convention as
  the v1 projects. Curve construction is injected, so all tests run offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Mapping, Optional

from core.data_models import CUTS, Crude, ProductMarket

DAYS_PER_MONTH = 30.4375
YF_TICKERS = {"brent": "BZ", "wti": "CL"}
MONTH_CODES = "FGHJKMNQUVXZ"   # Jan..Dec futures month codes


class MarketDataError(Exception):
    """Raised when market data cannot be built or priced."""


# --------------------------------------------------------------------------
# Curve
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Curve:
    """A forward curve: sorted (date, $/bbl) pillars with linear
    interpolation between pillars and flat extrapolation outside them."""
    instrument: str
    pillars: tuple[tuple[date, float], ...]

    def __post_init__(self):
        if len(self.pillars) < 1:
            raise MarketDataError(f"{self.instrument}: empty curve")
        dates = [d for d, _ in self.pillars]
        if dates != sorted(dates) or len(set(dates)) != len(dates):
            raise MarketDataError(
                f"{self.instrument}: pillars must be strictly increasing")

    def price(self, on: date) -> float:
        ps = self.pillars
        if on <= ps[0][0]:
            return ps[0][1]
        if on >= ps[-1][0]:
            return ps[-1][1]
        for (d0, p0), (d1, p1) in zip(ps, ps[1:]):
            if d0 <= on <= d1:
                w = (on - d0).days / (d1 - d0).days
                return p0 + w * (p1 - p0)
        raise AssertionError("unreachable")  # pragma: no cover

    def shifted(self, diff_usd_bbl: float, instrument: str) -> "Curve":
        """Same curve, parallel-shifted -- crude diffs and product cracks."""
        return Curve(instrument,
                     tuple((d, p + diff_usd_bbl) for d, p in self.pillars))

    @property
    def front(self) -> float:
        return self.pillars[0][1]


def parametric_curve(instrument: str, spot: float, slope_usd_month: float,
                     anchor: date, months: int = 18) -> Curve:
    """v1-style fallback: spot + constant slope ($/month).

    slope > 0 is contango, slope < 0 backwardation.
    """
    pillars = tuple(
        (anchor + timedelta(days=round(m * DAYS_PER_MONTH)),
         spot + m * slope_usd_month)
        for m in range(months + 1))
    return Curve(instrument, pillars)


# --------------------------------------------------------------------------
# Live curves (yfinance) with parametric fallback
# --------------------------------------------------------------------------

def fetch_benchmark_curve(benchmark: str, anchor: Optional[date] = None,
                          months: int = 12) -> Curve:
    """Build a curve from individual futures contracts via yfinance.

    Raises MarketDataError on any failure (no network, missing contracts,
    too few pillars) -- callers are expected to fall back to
    parametric_curve. Import is local so the dependency stays optional.
    """
    if benchmark not in YF_TICKERS:
        raise MarketDataError(f"no ticker mapping for '{benchmark}'")
    try:
        import yfinance as yf
    except ImportError as exc:
        raise MarketDataError(f"yfinance unavailable: {exc}")

    anchor = anchor or date.today()
    root = YF_TICKERS[benchmark]
    pillars = []
    for m in range(1, months + 1):
        y, mo = divmod(anchor.month - 1 + m, 12)
        year, month = anchor.year + y, mo + 1
        ticker = f"{root}{MONTH_CODES[month - 1]}{str(year)[-2:]}.NYM"
        try:
            hist = yf.Ticker(ticker).history(period="5d")
            if hist.empty:
                continue
            close = float(hist["Close"].iloc[-1])
        except Exception:
            continue
        pillars.append((date(year, month, 1), close))
    if len(pillars) < 3:
        raise MarketDataError(
            f"{benchmark}: only {len(pillars)} contracts fetched; "
            "falling back to parametric curve is expected here")
    return Curve(benchmark, tuple(pillars))


def benchmark_curves(anchor: date,
                     fallback_spots: Mapping[str, float],
                     fallback_slopes: Mapping[str, float],
                     use_live: bool = True) -> dict[str, Curve]:
    """The standard entry point: live curves when possible, v1-style
    parametric otherwise. fallback_spots/slopes must cover both benchmarks
    (they are the app's editable defaults)."""
    out = {}
    for bench in YF_TICKERS:
        curve = None
        if use_live:
            try:
                curve = fetch_benchmark_curve(bench, anchor)
            except MarketDataError:
                curve = None
        if curve is None:
            try:
                curve = parametric_curve(
                    bench, fallback_spots[bench], fallback_slopes[bench],
                    anchor)
            except KeyError as exc:
                raise MarketDataError(
                    f"no live data and no fallback for {exc}")
        out[bench] = curve
    return out


# --------------------------------------------------------------------------
# Crude and product pricing at any tenor
# --------------------------------------------------------------------------

def crude_fob(crude: Crude, curves: Mapping[str, Curve], on: date) -> float:
    """FOB price of a crude for loading on `on`: benchmark + static diff."""
    if crude.benchmark not in curves:
        raise MarketDataError(f"no curve for benchmark '{crude.benchmark}'")
    return curves[crude.benchmark].price(on) + crude.diff_usd_bbl


def product_prices(market: ProductMarket, curves: Mapping[str, Curve],
                   on: date) -> dict[str, float]:
    """All cut prices for a regional market on a given date -- exactly the
    {cut: $/bbl} mapping refinery.py expects."""
    if market.benchmark not in curves:
        raise MarketDataError(f"no curve for benchmark '{market.benchmark}'")
    base = curves[market.benchmark].price(on)
    return {c: base + market.cracks[c] for c in CUTS}
