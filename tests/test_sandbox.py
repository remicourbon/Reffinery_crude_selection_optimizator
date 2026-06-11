"""Tests for the Marseille sandbox: evaluate() with a live-built config
override must behave exactly like a YAML config, and toggling units must
change the economics in the expected direction."""

from datetime import date, timedelta
from pathlib import Path

import pytest

from core.data_models import RefineryConfig, load_dataset
from core.decision import evaluate
from core.market import benchmark_curves

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ARRIVAL = date(2026, 9, 1)
ANCHOR = date(2026, 6, 11)
SPOTS = {"brent": 80.0, "wti": 76.0}
SLOPES = {"brent": 0.30, "wti": 0.30}


@pytest.fixture(scope="module")
def ds():
    return load_dataset(DATA_DIR)


@pytest.fixture(scope="module")
def curves():
    return benchmark_curves(ANCHOR, SPOTS, SLOPES, use_live=False)


def _cfg(conv=None, conv_cap=0.0, coker_cap=0.0, spec=0.50, cdu=250.0):
    return RefineryConfig(
        key="sandbox", refinery_key="marseille", cdu_capacity_kbd=cdu,
        conversion_unit=conv, conversion_capacity_kbd=conv_cap,
        coker_capacity_kbd=coker_cap, diesel_sulfur_spec_pct=spec)


def test_marseille_and_its_crudes_exist(ds):
    assert "marseille" in ds.refineries
    assert ds.refineries["marseille"].port == "fos"
    assert {"saharan_blend", "cpc_blend"} <= set(ds.crudes)


def test_override_replaces_yaml_config(ds, curves):
    # Marseille's only YAML config is 'default'; an override with a bogus
    # config_key must still work because the override bypasses the lookup.
    cfg = _cfg(conv="fcc", conv_cap=80.0)
    d = evaluate(ds, "marseille", "ignored_label", ARRIVAL, 250.0, curves,
                 config_override=cfg)
    assert d.optimisation.optimal


def test_turning_on_conversion_never_hurts(ds, curves):
    off = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                   config_override=_cfg(conv=None))
    on = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                  config_override=_cfg(conv="fcc", conv_cap=80.0))
    # More processing options can only add value (or leave it unchanged).
    assert on.optimisation.margin_kusd_day >= \
        off.optimisation.margin_kusd_day - 1e-6


def test_stricter_sulfur_limit_cannot_improve_margin(ds, curves):
    loose = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                     config_override=_cfg(conv="fcc", conv_cap=80.0, spec=1.50))
    tight = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                     config_override=_cfg(conv="fcc", conv_cap=80.0, spec=0.20))
    assert tight.optimisation.margin_kusd_day <= \
        loose.optimisation.margin_kusd_day + 1e-6


def test_impossible_sulfur_limit_is_infeasible(ds, curves):
    # No crude has a straight-run diesel pool under 0.02%, and a fixed cargo
    # forces the purchase -> infeasible.
    d = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                 config_override=_cfg(spec=0.02))
    assert d.optimisation.status == "Infeasible"


def test_hcu_favours_distillate_value_over_fcc(ds, curves):
    # On the same crude slate, HCU (diesel/kero rich) should not earn less
    # than FCC given the Med crack stack with kero/diesel the top cracks.
    fcc = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                   config_override=_cfg(conv="fcc", conv_cap=90.0))
    hcu = evaluate(ds, "marseille", "s", ARRIVAL, 250.0, curves,
                   config_override=_cfg(conv="hcu", conv_cap=90.0))
    assert hcu.optimisation.margin_kusd_day >= \
        fcc.optimisation.margin_kusd_day - 1e-6
