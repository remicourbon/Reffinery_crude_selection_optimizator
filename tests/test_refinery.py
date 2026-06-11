"""Tests for core.refinery.

The two non-negotiable invariants are tested against hand-computed numbers:
mass conservation in the material balance, and a GPW recomputed on paper.
"""

import pytest

from core.data_models import load_dataset
from core.refinery import (
    RefineryInputError,
    blend_diesel_sulfur,
    gpw,
    material_balance,
    utilisation,
)

from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PRICES = {  # $/bbl, illustrative
    "lpg": 50.0, "naphtha": 70.0, "kero": 90.0,
    "diesel": 95.0, "vgo": 75.0, "residue": 55.0,
}


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


# --------------------------------------------------------------------------
# GPW
# --------------------------------------------------------------------------

def test_gpw_matches_hand_calculation(ds):
    # bonny_light: .03*50 + .22*70 + .16*90 + .31*95 + .20*75 + .07*55
    #            = 1.5 + 15.4 + 14.4 + 29.45 + 15.0 + 3.85 = 79.60
    # the 1% loss cut is worth zero by design.
    assert gpw(ds.crudes["bonny_light"], PRICES) == pytest.approx(79.60)


def test_gpw_loss_is_worth_zero(ds):
    # A price for "loss" must be ignored even if provided.
    prices = dict(PRICES, loss=1000.0)
    assert gpw(ds.crudes["bonny_light"], prices) == pytest.approx(79.60)


def test_gpw_missing_price_fails(ds):
    prices = dict(PRICES)
    del prices["vgo"]
    with pytest.raises(RefineryInputError, match="vgo"):
        gpw(ds.crudes["bonny_light"], prices)


# --------------------------------------------------------------------------
# Material balance
# --------------------------------------------------------------------------

def test_material_balance_conserves_mass(ds):
    basket = {"bonny_light": 300.0, "basrah_medium": 200.0, "forties": 150.0}
    balance = material_balance(basket, ds.crudes)
    assert sum(balance.values()) == pytest.approx(sum(basket.values()))


def test_material_balance_hand_check(ds):
    # 100 kbd of bonny (vgo .20) + 100 of basrah (vgo .27) -> 47 kbd VGO
    balance = material_balance(
        {"bonny_light": 100.0, "basrah_medium": 100.0}, ds.crudes)
    assert balance["vgo"] == pytest.approx(47.0)


def test_material_balance_rejects_unknown_crude(ds):
    with pytest.raises(RefineryInputError, match="mystery_crude"):
        material_balance({"mystery_crude": 100.0}, ds.crudes)


def test_material_balance_rejects_negative_volume(ds):
    with pytest.raises(RefineryInputError, match="negative"):
        material_balance({"bonny_light": -5.0}, ds.crudes)


# --------------------------------------------------------------------------
# Blend sulfur
# --------------------------------------------------------------------------

def test_blend_sulfur_hand_check(ds):
    # bonny 100 kbd: diesel pool 31 kbd at 0.08% ; basrah 100 kbd: 22 kbd at 1.30%
    # -> (31*0.08 + 22*1.30) / 53 = 31.08 / 53 = 0.58641...%
    s = blend_diesel_sulfur(
        {"bonny_light": 100.0, "basrah_medium": 100.0}, ds.crudes)
    assert s == pytest.approx(31.08 / 53.0)


def test_blend_sulfur_single_crude_is_its_own_sulfur(ds):
    s = blend_diesel_sulfur({"arab_light": 250.0}, ds.crudes)
    assert s == pytest.approx(ds.crudes["arab_light"].diesel_sulfur_pct)


def test_blend_sulfur_empty_pool_is_zero(ds):
    assert blend_diesel_sulfur({}, ds.crudes) == 0.0


# --------------------------------------------------------------------------
# Utilisation / feasibility
# --------------------------------------------------------------------------

def test_utilisation_feasible_clean_basket(ds):
    config = ds.refineries["dangote"].configs["fcc"]
    # 400 kbd of sweet US crude: vgo 68 <= 220, straight-run diesel pool at
    # 0.05% under the 0.60% hydrotreater limit -> fully feasible.
    report = utilisation({"wti_midland": 400.0}, ds.crudes, config)
    assert report.cdu_utilisation == pytest.approx(400 / 650)
    assert report.conversion_used_kbd == pytest.approx(400 * 0.17)
    assert report.diesel_sulfur_ok is True
    assert report.feasible is True


def test_utilisation_cdu_overload_is_infeasible(ds):
    config = ds.refineries["dangote"].configs["fcc"]
    report = utilisation({"wti_midland": 700.0}, ds.crudes, config)
    assert report.cdu_utilisation > 1.0
    assert report.feasible is False


def test_utilisation_no_conversion_unit_reports_none(ds):
    config = ds.refineries["rotterdam"].configs["hydroskimming"]
    report = utilisation({"forties": 100.0}, ds.crudes, config)
    assert report.conversion_utilisation is None
    assert report.coker_utilisation is None
