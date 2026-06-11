"""Tests for core.freight. Reference numbers are computed by hand in the
comments so every formula has a paper trail."""

from pathlib import Path

import pytest

from core.data_models import load_dataset
from core.freight import (
    FreightInputError,
    best_quote,
    financing_usd_bbl,
    quote,
    tonnes_per_bbl,
    voyage_days,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


def test_tonnes_per_bbl_hand_check():
    # API 42: SG = 141.5/173.5 = 0.8155620; t/bbl = 0.8155620 * 0.158987
    assert tonnes_per_bbl(42.0) == pytest.approx(0.1296637, abs=1e-6)
    # Heavier crude -> heavier barrel
    assert tonnes_per_bbl(27.0) > tonnes_per_bbl(42.0)


def test_voyage_days_hand_check(ds):
    # corpus_christi-lekki: 6050 nm / (12 kn * 24 h) = 21.0069 d + 5 port
    route = ds.route_between("corpus_christi", "lekki")
    days = voyage_days(route, ds.vessels["vlcc"])
    assert days == pytest.approx(6050 / (12 * 24) + 5, abs=1e-4)


def test_financing_act360_hand_check():
    # 70 $/bbl * 5% * 36/360 = 0.35 $/bbl
    assert financing_usd_bbl(70.0, 36.0, 0.05) == pytest.approx(0.35)


def test_quote_full_vlcc_hand_check(ds):
    # flat 14.6 $/t * 60% = 8.76 $/t ; * t/bbl(api 42) = 8.76 * 0.129665
    route = ds.route_between("corpus_christi", "lekki")
    q = quote(route, ds.vessels["vlcc"], volume_kbbl=2000.0, api=42.0)
    assert q.freight_usd_bbl == pytest.approx(8.76 * 0.129665, abs=1e-4)
    assert q.dead_freight is False
    assert q.utilisation == pytest.approx(1.0)


def test_dead_freight_scales_cost(ds):
    # Half-filling the ship doubles the $/bbl.
    route = ds.route_between("corpus_christi", "lekki")
    full = quote(route, ds.vessels["vlcc"], 2000.0, api=42.0)
    half = quote(route, ds.vessels["vlcc"], 1000.0, api=42.0)
    assert half.freight_usd_bbl == pytest.approx(2 * full.freight_usd_bbl)
    assert half.dead_freight is True


def test_ws_override_replaces_typical(ds):
    route = ds.route_between("corpus_christi", "lekki")
    q = quote(route, ds.vessels["vlcc"], 2000.0, api=42.0, ws_pct=45.0)
    assert q.ws_pct == 45.0
    assert q.freight_usd_bbl == pytest.approx(
        14.6 * 0.45 * tonnes_per_bbl(42.0), abs=1e-4)


def test_volume_exceeding_cargo_fails(ds):
    route = ds.route_between("bonny", "lekki")
    with pytest.raises(FreightInputError, match="multi-voyage"):
        quote(route, ds.vessels["aframax"], 800.0, api=35.3)


def test_best_quote_respects_port_limit(ds):
    # Rotterdam is suezmax-limited: a VLCC must never be selected.
    route = ds.route_between("ras_tanura", "rotterdam")
    q = best_quote(route, ds.vessels, max_vessel_key="suezmax",
                   volume_kbbl=900.0, api=33.0)
    assert q.vessel_key == "suezmax"


def test_best_quote_prefers_economies_of_scale(ds):
    # 700 kb to Lekki (VLCC port): aframax exactly full (WS 120) vs
    # suezmax at 70% (WS 95 / 0.7 -> 135.7 eff) vs vlcc at 35% (60 / 0.35
    # -> 171 eff): the full aframax wins despite its higher WS%.
    route = ds.route_between("bonny", "lekki")
    q = best_quote(route, ds.vessels, "vlcc", volume_kbbl=700.0, api=35.3)
    assert q.vessel_key == "aframax"
    # But at 2000 kb only the VLCC can lift it in one go.
    q2 = best_quote(route, ds.vessels, "vlcc", volume_kbbl=2000.0, api=35.3)
    assert q2.vessel_key == "vlcc"


def test_best_quote_no_feasible_vessel_fails(ds):
    route = ds.route_between("bonny", "rotterdam")
    # 1500 kb needs a VLCC, but the port only takes suezmax.
    with pytest.raises(FreightInputError, match="no feasible vessel"):
        best_quote(route, ds.vessels, "suezmax", volume_kbbl=1500.0, api=35.3)
