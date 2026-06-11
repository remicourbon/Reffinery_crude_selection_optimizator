"""Tests for core.lp_model.

The key economic behaviours are each pinned by a test:
- without binding constraints, the LP buys only the best-margin crude;
- the sulfur constraint forces a blend whose ratio we derive analytically;
- the LP and refinery.apply_upgrading can never disagree on value;
- negative uplift idles the upgrading variable;
- duals (shadow prices) are exposed and carry the right signs.
"""

from pathlib import Path

import pytest

from core.data_models import RefineryConfig, load_dataset
from core.lp_model import OptimisationError, optimise_basket
from core.refinery import apply_upgrading, blend_diesel_sulfur, gpw

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PRICES = {  # same realistic hierarchy as test_upgrading.py
    "lpg": 45.0, "naphtha": 80.0, "kero": 92.0,
    "diesel": 95.0, "vgo": 62.0, "residue": 48.0,
}


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


@pytest.fixture(scope="module")
def dangote(ds):
    return ds.refineries["dangote"].configs["fcc"]


def margins_cif(ds, **margin_per_crude):
    """Build a CIF map giving each crude an exact net margin in $/bbl."""
    return {k: gpw(ds.crudes[k], PRICES) - m
            for k, m in margin_per_crude.items()}


# --------------------------------------------------------------------------
# Core behaviours
# --------------------------------------------------------------------------

def test_unconstrained_lp_buys_only_the_best_crude(ds, dangote):
    # Two sweet crudes (sulfur slack), no volume target: 100% of the best.
    cif = margins_cif(ds, bonny_light=8.0, wti_midland=5.0)
    res = optimise_basket(ds.crudes, dangote, ds.conversion_units,
                          PRICES, cif)
    assert res.optimal
    assert set(res.basket) == {"bonny_light"}
    assert res.basket["bonny_light"] == pytest.approx(650.0)  # CDU full


def test_cdu_dual_is_the_marginal_barrel_value(ds, dangote):
    cif = margins_cif(ds, bonny_light=8.0, wti_midland=5.0)
    res = optimise_basket(ds.crudes, dangote, ds.conversion_units,
                          PRICES, cif)
    # Relaxing the CDU by 1 kb/d adds one barrel of bonny (8 $/bbl) plus
    # its VGO upgraded through the slack FCC -- so the dual exceeds 8.
    assert res.shadow_prices["cdu"] >= 8.0


def test_sulfur_constraint_forces_the_analytic_blend(ds, dangote):
    # basrah (S 1.30, y_d .22) margin 10 > bonny (S 0.08, y_d .31) margin 5.
    # The LP wants max basrah; sulfur (S_max 0.60) binds:
    #   x_b * .22 * (1.30-.60) = x_o * .31 * (.60-.08)
    #   => x_b = x_o * 0.1612/0.154 ; with x_b + x_o = 100:
    x_o = 100.0 / (1.0 + 0.1612 / 0.154)
    x_b = 100.0 - x_o
    cif = margins_cif(ds, basrah_medium=10.0, bonny_light=5.0)
    res = optimise_basket(ds.crudes, dangote, ds.conversion_units,
                          PRICES, cif, total_volume_kbd=100.0)
    assert res.optimal
    assert res.basket["basrah_medium"] == pytest.approx(x_b, rel=1e-4)
    assert res.basket["bonny_light"] == pytest.approx(x_o, rel=1e-4)
    # The pool sits exactly on the spec...
    assert blend_diesel_sulfur(res.basket, ds.crudes) == \
        pytest.approx(0.60, rel=1e-6)
    # ...and the refinery would pay for sulfur room: non-zero dual.
    assert res.shadow_prices["sulfur"] != 0.0


def test_lp_and_apply_upgrading_agree_on_value(ds, dangote):
    """The invariant promised in refinery.py's docstring: the greedy
    upgrading rule IS the LP optimum, so total values must match."""
    cif = margins_cif(ds, basrah_medium=10.0, bonny_light=5.0,
                      arab_light=7.0)
    res = optimise_basket(ds.crudes, dangote, ds.conversion_units,
                          PRICES, cif, total_volume_kbd=400.0)
    assert res.optimal
    rep = apply_upgrading(res.basket, ds.crudes, dangote,
                          ds.conversion_units, PRICES)
    purchase_cost = sum(res.basket[k] * cif[k] for k in res.basket)
    assert res.margin_kusd_day == pytest.approx(
        rep.total_value_kusd_day - purchase_cost, rel=1e-6)
    assert res.upgraded_conv_kbd == pytest.approx(rep.upgraded_conv_kbd,
                                                  rel=1e-6)


def test_negative_uplift_idles_upgrading_variable(ds, dangote):
    inverted = dict(PRICES, vgo=90.0, naphtha=60.0)
    cif = {"bonny_light": gpw(ds.crudes["bonny_light"], inverted) - 5.0}
    res = optimise_basket(ds.crudes, dangote, ds.conversion_units,
                          inverted, cif, total_volume_kbd=200.0)
    assert res.optimal
    assert res.upgraded_conv_kbd == pytest.approx(0.0, abs=1e-9)


def test_conv_capacity_dual_appears_when_binding(ds):
    # A tight FCC (50 kb/d) behind a 300 kb/d CDU: the sulfur-bound blend
    # makes ~70 kb/d of VGO, so CAPACITY binds (not feed) and its dual is
    # exactly the uplift: one more barrel of FCC room converts slack VGO.
    config = RefineryConfig(
        key="tight_fcc", refinery_key="test", cdu_capacity_kbd=300.0,
        conversion_unit="fcc", conversion_capacity_kbd=50.0,
        coker_capacity_kbd=0.0, diesel_sulfur_spec_pct=0.60)
    cif = margins_cif(ds, basrah_medium=10.0, bonny_light=9.0)
    res = optimise_basket(ds.crudes, config, ds.conversion_units,
                          PRICES, cif, total_volume_kbd=300.0)
    assert res.optimal
    assert res.upgraded_conv_kbd == pytest.approx(50.0)
    # One more barrel of FCC room is worth its uplift: 10.45 $/bbl.
    assert res.shadow_prices["conv_capacity"] == pytest.approx(10.45,
                                                               rel=1e-3)


def test_conv_feed_limited_means_zero_capacity_dual(ds):
    # Mirror case (and the bug this test originally had): on rotterdam-fcc
    # at 200 kb/d, the sulfur-bound blend makes only ~47 kb/d of VGO < 60:
    # FEED binds, so extra capacity is worthless and its dual is zero --
    # the marginal value moved to the feed constraint instead.
    config = ds.refineries["rotterdam"].configs["fcc"]
    cif = margins_cif(ds, basrah_medium=10.0, bonny_light=9.0)
    res = optimise_basket(ds.crudes, config, ds.conversion_units,
                          PRICES, cif, total_volume_kbd=200.0)
    assert res.optimal
    assert res.upgraded_conv_kbd < 60.0
    assert res.shadow_prices["conv_capacity"] == pytest.approx(0.0, abs=1e-9)
    assert res.shadow_prices["conv_feed"] == pytest.approx(10.45, rel=1e-3)


def test_impossible_sulfur_spec_is_infeasible(ds):
    config = RefineryConfig(
        key="strict", refinery_key="test", cdu_capacity_kbd=100.0,
        conversion_unit=None, conversion_capacity_kbd=0.0,
        coker_capacity_kbd=0.0, diesel_sulfur_spec_pct=0.01)
    cif = {"basrah_medium": 50.0}
    res = optimise_basket(ds.crudes, config, ds.conversion_units,
                          PRICES, cif, total_volume_kbd=50.0)
    assert res.status == "Infeasible"
    assert res.basket == {}


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------

def test_unknown_crude_in_cif_fails(ds, dangote):
    with pytest.raises(OptimisationError, match="mystery"):
        optimise_basket(ds.crudes, dangote, ds.conversion_units, PRICES,
                        {"mystery_crude": 60.0})


def test_volume_above_cdu_fails(ds, dangote):
    cif = margins_cif(ds, bonny_light=5.0)
    with pytest.raises(OptimisationError, match="CDU"):
        optimise_basket(ds.crudes, dangote, ds.conversion_units, PRICES,
                        cif, total_volume_kbd=700.0)
