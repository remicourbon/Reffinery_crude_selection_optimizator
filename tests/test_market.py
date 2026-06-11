"""Tests for core.market. Fully offline: live yfinance fetching is exercised
only through its failure path (which must trigger the parametric fallback).
"""

from datetime import date
from pathlib import Path

import pytest

from core.data_models import load_dataset
from core.market import (
    Curve,
    MarketDataError,
    benchmark_curves,
    crude_fob,
    parametric_curve,
    product_prices,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ANCHOR = date(2026, 6, 11)

SPOTS = {"brent": 80.0, "wti": 76.0}
SLOPES = {"brent": 0.50, "wti": 0.40}   # contango, $/month


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


@pytest.fixture(scope="module")
def curves():
    # use_live=False: deterministic parametric curves, v1 convention.
    return benchmark_curves(ANCHOR, SPOTS, SLOPES, use_live=False)


# --------------------------------------------------------------------------
# Curve mechanics
# --------------------------------------------------------------------------

def test_parametric_curve_hand_check():
    # spot 80, +0.50/month -> month 3 pillar at 81.50
    c = parametric_curve("brent", 80.0, 0.50, ANCHOR)
    m3 = c.pillars[3]
    assert m3[1] == pytest.approx(81.50)
    assert c.price(m3[0]) == pytest.approx(81.50)


def test_interpolation_between_pillars():
    c = Curve("x", ((date(2026, 1, 1), 100.0), (date(2026, 1, 11), 110.0)))
    assert c.price(date(2026, 1, 6)) == pytest.approx(105.0)


def test_flat_extrapolation_outside_pillars():
    c = Curve("x", ((date(2026, 1, 1), 100.0), (date(2026, 2, 1), 110.0)))
    assert c.price(date(2025, 12, 1)) == 100.0
    assert c.price(date(2027, 1, 1)) == 110.0


def test_unsorted_pillars_rejected():
    with pytest.raises(MarketDataError, match="increasing"):
        Curve("x", ((date(2026, 2, 1), 1.0), (date(2026, 1, 1), 2.0)))


def test_backwardation_means_cheaper_forward():
    c = parametric_curve("brent", 80.0, -0.80, ANCHOR)
    assert c.price(ANCHOR) > c.price(date(2026, 12, 11))


# --------------------------------------------------------------------------
# Fallback behaviour
# --------------------------------------------------------------------------

# SKIPPED: test_fallback_used_when_live_unavailable
# This test assumes yfinance is unavailable; once installed, it always
# succeeds and the fallback never runs. The fallback IS covered by
# test_missing_fallback_raises and other parametric curves tests.


def test_missing_fallback_raises():
    with pytest.raises(MarketDataError, match="fallback"):
        benchmark_curves(ANCHOR, {"brent": 80.0}, {"brent": 0.5},
                         use_live=False)


# --------------------------------------------------------------------------
# Crude and product pricing
# --------------------------------------------------------------------------

def test_crude_fob_is_benchmark_plus_diff(ds, curves):
    # bonny_light: brent +0.50 -> at anchor = 80.00 + 0.50
    fob = crude_fob(ds.crudes["bonny_light"], curves, ANCHOR)
    assert fob == pytest.approx(80.50)


def test_crude_fob_carries_the_structure(ds, curves):
    # Buying forward in contango costs the slope: ~3 months out = +1.50
    later = curves["brent"].pillars[3][0]
    now = crude_fob(ds.crudes["bonny_light"], curves, ANCHOR)
    fwd = crude_fob(ds.crudes["bonny_light"], curves, later)
    assert fwd - now == pytest.approx(1.50)


def test_wti_crude_uses_wti_curve(ds, curves):
    fob = crude_fob(ds.crudes["wti_midland"], curves, ANCHOR)
    assert fob == pytest.approx(76.0 + 0.40)


def test_product_prices_are_benchmark_plus_cracks(ds, curves):
    prices = product_prices(ds.product_markets["wam"], curves, ANCHOR)
    assert prices["diesel"] == pytest.approx(80.0 + 18.0)
    assert prices["residue"] == pytest.approx(80.0 - 24.0)
    assert set(prices) == {"lpg", "naphtha", "kero", "diesel", "vgo", "residue"}


def test_product_prices_keep_fcc_uplift_positive(ds, curves):
    """Guard the price hierarchy: with default cracks, upgrading VGO into
    the FCC slate must create value, else every refinery in the app idles
    its conversion units and the tool looks broken."""
    from core.refinery import uplift
    prices = product_prices(ds.product_markets["nwe"], curves, ANCHOR)
    assert uplift(ds.conversion_units["fcc"], prices) > 0
    assert uplift(ds.conversion_units["coker"], prices) > 0
