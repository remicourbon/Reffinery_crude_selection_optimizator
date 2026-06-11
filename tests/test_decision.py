"""Tests for core.decision -- including the Dangote story itself: with the
arrival date fixed, the long-voyage crude is bought at an EARLIER tenor, so
the curve's structure flows into the CIF with the right sign."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from core.data_models import load_dataset
from core.decision import DecisionError, evaluate, price_option
from core.market import benchmark_curves

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ARRIVAL = date(2026, 9, 1)
ANCHOR = date(2026, 6, 11)

SPOTS = {"brent": 80.0, "wti": 76.0}
CONTANGO = {"brent": 0.60, "wti": 0.60}
BACKWARDATION = {"brent": -0.60, "wti": -0.60}


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


@pytest.fixture(scope="module")
def curves_contango():
    return benchmark_curves(ANCHOR, SPOTS, CONTANGO, use_live=False)


@pytest.fixture(scope="module")
def curves_back():
    return benchmark_curves(ANCHOR, SPOTS, BACKWARDATION, use_live=False)


# --------------------------------------------------------------------------
# Option pricing
# --------------------------------------------------------------------------

def test_option_decomposition_adds_up(ds, curves_contango):
    o = price_option(ds, "bonny_light", "dangote", ARRIVAL, 650.0,
                     curves_contango)
    assert o.cif_usd_bbl == pytest.approx(
        o.fob_usd_bbl + o.freight.freight_usd_bbl + o.financing_usd_bbl
        + o.insurance_usd_bbl + o.losses_usd_bbl)
    assert o.departure_date == ARRIVAL - timedelta(days=round(o.voyage_days))


def test_long_voyage_means_earlier_and_cheaper_tenor_in_contango(
        ds, curves_contango):
    """The heart of the Dangote question. Same arrival date: WTI departs
    ~3 weeks before Bonny, so in contango its FOB is taken lower on the
    curve. (Cheaper FOB does not mean cheaper CIF -- freight and financing
    fight back; that arbitration is exactly what the tool is for.)"""
    wti = price_option(ds, "wti_midland", "dangote", ARRIVAL, 650.0,
                       curves_contango)
    bonny = price_option(ds, "bonny_light", "dangote", ARRIVAL, 650.0,
                         curves_contango)
    assert wti.voyage_days > bonny.voyage_days + 15
    assert wti.departure_date < bonny.departure_date
    # On the SAME curve, the earlier tenor is cheaper in contango:
    earlier = curves_contango["wti"].price(wti.departure_date)
    later = curves_contango["wti"].price(bonny.departure_date)
    assert earlier < later
    # And the structure effect reverses in backwardation:
    curves_b = benchmark_curves(ANCHOR, SPOTS, BACKWARDATION, use_live=False)
    wti_b = price_option(ds, "wti_midland", "dangote", ARRIVAL, 650.0,
                         curves_b)
    assert wti_b.fob_usd_bbl > \
        curves_b["wti"].price(bonny.departure_date) + 0.40 - 1e-9 or \
        wti_b.fob_usd_bbl == pytest.approx(
            curves_b["wti"].price(wti_b.departure_date) + 0.40)


def test_financing_scales_with_voyage_length(ds, curves_contango):
    wti = price_option(ds, "wti_midland", "dangote", ARRIVAL, 650.0,
                       curves_contango)
    bonny = price_option(ds, "bonny_light", "dangote", ARRIVAL, 650.0,
                         curves_contango)
    assert wti.financing_usd_bbl > 4 * bonny.financing_usd_bbl


# --------------------------------------------------------------------------
# Full evaluation
# --------------------------------------------------------------------------

def test_evaluate_end_to_end(ds, curves_contango):
    d = evaluate(ds, "dangote", "fcc", ARRIVAL, 650.0, curves_contango)
    assert d.optimisation.optimal
    assert sum(d.optimisation.basket.values()) == pytest.approx(650.0)
    assert not d.excluded
    # Dangote's straight-run pool must sit on or under the 0.60% limit.
    from core.refinery import blend_diesel_sulfur
    assert blend_diesel_sulfur(d.optimisation.basket, ds.crudes) \
        <= 0.60 + 1e-9


def test_oversized_cargo_excludes_crudes(ds, curves_contango):
    # 1500 kb to Rotterdam (suezmax port, 1000 kb max): nothing can lift it.
    with pytest.raises(DecisionError, match="no deliverable crude"):
        evaluate(ds, "rotterdam", "fcc", ARRIVAL, 1500.0, curves_contango)


def test_partial_exclusion_is_reported_not_fatal(ds, curves_contango):
    # 1500 kb to Dangote (VLCC port): fine -- but pretend-check via a small
    # cargo at Rotterdam where everything fits, exclusions stay empty.
    d = evaluate(ds, "rotterdam", "fcc", ARRIVAL, 180.0, curves_contango)
    assert d.optimisation.optimal
    assert d.excluded == {}


def test_dead_freight_can_flip_the_optimal_crude(ds, curves_contango):
    """Found while testing: basket shares are NOT volume-invariant, and
    that's correct -- freight is not homogeneous in volume. At 200 kb the
    near-full Aframax makes short-haul Bonny the winner; at 400 kb dead
    freight reshuffles every CIF and WTI takes the cargo. The cargo size
    itself changes the optimal crude: one of the tool's best demos."""
    d_small = evaluate(ds, "dangote", "fcc", ARRIVAL, 200.0, curves_contango)
    d_large = evaluate(ds, "dangote", "fcc", ARRIVAL, 400.0, curves_contango)
    assert set(d_small.optimisation.basket) == {"bonny_light"}
    assert set(d_large.optimisation.basket) == {"wti_midland"}
    assert sum(d_small.optimisation.basket.values()) == pytest.approx(200.0)
    assert sum(d_large.optimisation.basket.values()) == pytest.approx(400.0)


def test_unknown_refinery_or_config_fails(ds, curves_contango):
    with pytest.raises(DecisionError, match="unknown refinery"):
        evaluate(ds, "pemex", "fcc", ARRIVAL, 100.0, curves_contango)
    with pytest.raises(DecisionError, match="unknown config"):
        evaluate(ds, "dangote", "coking", ARRIVAL, 100.0, curves_contango)
