"""Tests for core.data_models.

Strategy: load the real data/ directory as the happy path (it doubles as an
integration test of the shipped data), then copy it to tmp_path and corrupt
one thing at a time to verify each validation fails loudly and points at the
right file and key.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from core.data_models import (
    DataValidationError,
    load_dataset,
    route_key,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def data_copy(tmp_path):
    """A writable copy of the real data directory."""
    dst = tmp_path / "data"
    shutil.copytree(DATA_DIR, dst)
    return dst


def _mutate(data_dir: Path, filename: str, mutator):
    """Load a YAML file, apply mutator(dict) in place, write it back."""
    path = data_dir / filename
    content = yaml.safe_load(path.read_text())
    mutator(content)
    path.write_text(yaml.safe_dump(content))


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------

def test_real_data_loads_cleanly():
    ds = load_dataset(DATA_DIR)
    assert "bonny_light" in ds.crudes
    assert "dangote" in ds.refineries
    assert ds.vessels["vlcc"].cargo_kbbl == 2000


def test_yields_sum_to_one_for_all_shipped_crudes():
    ds = load_dataset(DATA_DIR)
    for crude in ds.crudes.values():
        assert abs(sum(crude.yields.values()) - 1.0) < 1e-6, crude.key


def test_route_lookup_is_order_independent():
    ds = load_dataset(DATA_DIR)
    a = ds.route_between("bonny", "lekki")
    b = ds.route_between("lekki", "bonny")
    assert a is b
    assert route_key("lekki", "bonny") == "bonny-lekki"


def test_dataclasses_are_immutable():
    ds = load_dataset(DATA_DIR)
    crude = ds.crudes["bonny_light"]
    with pytest.raises(Exception):
        crude.api = 99
    with pytest.raises(TypeError):
        crude.yields["diesel"] = 0.99


# --------------------------------------------------------------------------
# Validation failures -- each must raise and name the culprit
# --------------------------------------------------------------------------

def test_broken_yield_sum_fails(data_copy):
    _mutate(data_copy, "crudes.yaml",
            lambda d: d["bonny_light"]["yields"].__setitem__("diesel", 0.50))
    with pytest.raises(DataValidationError, match=r"bonny_light.*sum"):
        load_dataset(data_copy)


def test_negative_yield_fails(data_copy):
    def mut(d):
        d["bonny_light"]["yields"]["lpg"] = -0.03
        d["bonny_light"]["yields"]["naphtha"] = 0.28  # keep sum at 1.0
    _mutate(data_copy, "crudes.yaml", mut)
    with pytest.raises(DataValidationError, match=r"bonny_light.*negative"):
        load_dataset(data_copy)


def test_missing_cut_fails(data_copy):
    def mut(d):
        y = d["bonny_light"]["yields"]
        y["residue"] += y.pop("vgo")  # keep sum at 1.0, drop a required cut
    _mutate(data_copy, "crudes.yaml", mut)
    with pytest.raises(DataValidationError, match=r"bonny_light.*missing.*vgo"):
        load_dataset(data_copy)


def test_insane_api_fails(data_copy):
    _mutate(data_copy, "crudes.yaml",
            lambda d: d["bonny_light"].__setitem__("api", 95.0))
    with pytest.raises(DataValidationError, match=r"bonny_light.*api"):
        load_dataset(data_copy)


def test_unknown_benchmark_fails(data_copy):
    _mutate(data_copy, "crudes.yaml",
            lambda d: d["bonny_light"].__setitem__("benchmark", "dubai"))
    with pytest.raises(DataValidationError, match=r"bonny_light.*benchmark"):
        load_dataset(data_copy)


def test_missing_route_fails_and_lists_the_gap(data_copy):
    _mutate(data_copy, "routes.yaml", lambda d: d.pop("bonny-lekki"))
    with pytest.raises(DataValidationError,
                       match=r"missing routes(.|\n)*bonny_light.*dangote"):
        load_dataset(data_copy)


def test_unsorted_route_key_fails(data_copy):
    def mut(d):
        d["lekki-bonny"] = d.pop("bonny-lekki")
    _mutate(data_copy, "routes.yaml", mut)
    with pytest.raises(DataValidationError, match=r"lekki-bonny.*sorted"):
        load_dataset(data_copy)


def test_unknown_max_vessel_fails(data_copy):
    _mutate(data_copy, "refineries.yaml",
            lambda d: d["dangote"].__setitem__("max_vessel", "panamax"))
    with pytest.raises(DataValidationError, match=r"dangote.*panamax"):
        load_dataset(data_copy)


def test_null_conversion_with_capacity_fails(data_copy):
    def mut(d):
        cfg = d["rotterdam"]["configs"]["hydroskimming"]
        cfg["conversion_capacity_kbd"] = 60  # capacity without a unit
    _mutate(data_copy, "refineries.yaml", mut)
    with pytest.raises(DataValidationError, match=r"hydroskimming"):
        load_dataset(data_copy)


def test_misspelled_field_points_at_file_and_key(data_copy):
    def mut(d):
        d["bonny_light"]["apii"] = d["bonny_light"].pop("api")
    _mutate(data_copy, "crudes.yaml", mut)
    with pytest.raises(DataValidationError, match=r"crudes\.yaml.*bonny_light"):
        load_dataset(data_copy)
