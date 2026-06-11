"""Tests for the upgrading logic in core.refinery.

Prices here are deliberately different from test_refinery.py: a realistic
structure where naphtha/gasoline outprices VGO, so the FCC creates value.
One test then inverts that structure to verify the unit goes idle.
"""

from pathlib import Path

import pytest

from core.data_models import load_dataset
from core.refinery import apply_upgrading, uplift

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PRICES = {  # $/bbl, illustrative but realistically ordered
    "lpg": 45.0, "naphtha": 80.0, "kero": 92.0,
    "diesel": 95.0, "vgo": 62.0, "residue": 48.0,
}


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


def test_fcc_uplift_hand_calculation(ds):
    # out: .05*45 + .55*80 + .20*95 + .15*48 = 2.25 + 44 + 19 + 7.2 = 72.45
    # uplift = 72.45 - 62 (vgo) = 10.45 $/bbl
    assert uplift(ds.conversion_units["fcc"], PRICES) == pytest.approx(10.45)


def test_upgrading_conserves_mass(ds):
    config = ds.refineries["dangote"].configs["fcc"]
    basket = {"bonny_light": 300.0, "arab_light": 200.0}
    res = apply_upgrading(basket, ds.crudes, config, ds.conversion_units, PRICES)
    assert sum(res.balance.values()) == pytest.approx(500.0)


def test_upgrading_is_capacity_limited(ds):
    config = ds.refineries["dangote"].configs["fcc"]
    # 650 kbd of basrah -> VGO = 650 * 0.27 = 175.5 <= 220: feed-limited.
    res = apply_upgrading({"basrah_medium": 650.0}, ds.crudes, config,
                          ds.conversion_units, PRICES)
    assert res.upgraded_conv_kbd == pytest.approx(175.5)
    assert res.balance["vgo"] == pytest.approx(0.0)


def test_upgrading_leaves_excess_feed_unconverted(ds):
    config = ds.refineries["rotterdam"].configs["fcc"]  # conv cap 60 kbd
    # 200 kbd of arab_light -> VGO = 50... use basrah: 200 * .27 = 54 < 60.
    # Force excess: 200 kbd basrah + 100 forties -> 54 + 20 = 74 > 60.
    res = apply_upgrading({"basrah_medium": 200.0, "forties": 100.0},
                          ds.crudes, config, ds.conversion_units, PRICES)
    assert res.upgraded_conv_kbd == pytest.approx(60.0)
    assert res.balance["vgo"] == pytest.approx(14.0)  # sold as-is


def test_negative_uplift_idles_the_unit(ds):
    config = ds.refineries["dangote"].configs["fcc"]
    inverted = dict(PRICES, vgo=90.0, naphtha=60.0)  # VGO outprices naphtha
    assert uplift(ds.conversion_units["fcc"], inverted) < 0
    res = apply_upgrading({"bonny_light": 400.0}, ds.crudes, config,
                          ds.conversion_units, inverted)
    assert res.upgraded_conv_kbd == 0.0
    assert res.upgrading_gain_kusd_day == 0.0
    assert res.total_value_kusd_day == pytest.approx(res.gpw_cdu_kusd_day)


def test_hydroskimmer_has_no_upgrading_but_is_feasible(ds):
    config = ds.refineries["rotterdam"].configs["hydroskimming"]
    res = apply_upgrading({"basrah_medium": 150.0}, ds.crudes, config,
                          ds.conversion_units, PRICES)
    assert res.upgraded_conv_kbd == 0.0
    assert res.upgraded_coker_kbd == 0.0
    # All the VGO and residue are simply sold at their cut prices.
    assert res.balance["vgo"] == pytest.approx(150.0 * 0.27)
    assert res.total_value_kusd_day == pytest.approx(res.gpw_cdu_kusd_day)


def test_config_dependence_fcc_beats_hydroskimming_on_heavy_crude(ds):
    """The whole point of the fix: the same barrel is now worth more in a
    conversion refinery than in a hydroskimmer (under normal prices)."""
    hsk = ds.refineries["rotterdam"].configs["hydroskimming"]
    fcc = ds.refineries["rotterdam"].configs["fcc"]
    basket = {"basrah_medium": 150.0}
    v_hsk = apply_upgrading(basket, ds.crudes, hsk,
                            ds.conversion_units, PRICES).total_value_kusd_day
    v_fcc = apply_upgrading(basket, ds.crudes, fcc,
                            ds.conversion_units, PRICES).total_value_kusd_day
    assert v_fcc > v_hsk
