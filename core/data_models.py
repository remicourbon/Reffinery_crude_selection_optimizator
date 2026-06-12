"""Data models and YAML loaders for crude-delivery-optimizer.

All static data lives in YAML files under data/. This module loads them into
frozen dataclasses and validates aggressively at load time: a broken YAML file
must be diagnosable in seconds (file + key in every error message), never at
solve time.

Design choices (see README):
- Yields use a fixed 6-cut schema plus an explicit `loss` cut, so the
  mass-balance check is exact (sum == 1.0), not approximate.
- Sulfur is carried at crude level (info) and on the diesel cut only
  (the single input the LP sulfur constraint needs).
- Routes are keyed by the alphabetically sorted port pair; lookups are
  normalised, so each route is entered once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Optional

import yaml

CUTS = ("lpg", "naphtha", "gasoline", "kero", "diesel", "vgo", "residue")
ALL_CUTS = CUTS + ("loss",)
YIELD_TOLERANCE = 1e-6
BENCHMARKS = ("brent", "wti")
CONVERSION_UNITS = ("fcc", "hcu")


class DataValidationError(Exception):
    """Raised when a data file fails validation."""


def _fail(file: str, key: str, msg: str) -> None:
    raise DataValidationError(f"[{file}] '{key}': {msg}")


def route_key(port_a: str, port_b: str) -> str:
    """Canonical, order-independent key for a port pair."""
    return "-".join(sorted((port_a, port_b)))


# --------------------------------------------------------------------------
# Dataclasses
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Crude:
    key: str
    name: str
    api: float
    sulfur_pct: float
    benchmark: str
    diff_usd_bbl: float
    fob_port: str
    yields: Mapping[str, float]
    diesel_sulfur_pct: float

    def __post_init__(self):
        f = "crudes.yaml"
        if not 10.0 <= self.api <= 60.0:
            _fail(f, self.key, f"api={self.api} outside sane range [10, 60]")
        if not 0.0 <= self.sulfur_pct < 6.0:
            _fail(f, self.key, f"sulfur_pct={self.sulfur_pct} outside [0, 6)")
        if self.benchmark not in BENCHMARKS:
            _fail(f, self.key, f"benchmark='{self.benchmark}' not in {BENCHMARKS}")
        missing = [c for c in ALL_CUTS if c not in self.yields]
        if missing:
            _fail(f, self.key, f"missing yield cuts: {missing}")
        unknown = [c for c in self.yields if c not in ALL_CUTS]
        if unknown:
            _fail(f, self.key, f"unknown yield cuts: {unknown}")
        negative = {c: v for c, v in self.yields.items() if v < 0}
        if negative:
            _fail(f, self.key, f"negative yields: {negative}")
        total = sum(self.yields.values())
        if abs(total - 1.0) > YIELD_TOLERANCE:
            _fail(f, self.key, f"yields sum to {total:.6f}, expected 1.0")
        if not 0.0 <= self.diesel_sulfur_pct < 6.0:
            _fail(f, self.key,
                  f"diesel_sulfur_pct={self.diesel_sulfur_pct} outside [0, 6)")
        # Freeze the mapping so the dataclass is deeply immutable.
        object.__setattr__(self, "yields", MappingProxyType(dict(self.yields)))


@dataclass(frozen=True)
class RefineryConfig:
    key: str                      # e.g. "fcc", "hydroskimming"
    refinery_key: str
    cdu_capacity_kbd: float
    conversion_unit: Optional[str]   # "fcc" | "hcu" | None
    conversion_capacity_kbd: float
    coker_capacity_kbd: float
    diesel_sulfur_spec_pct: float
    reformer_capacity_kbd: float = 0.0   # naphtha -> gasoline; 0 = no reformer

    def __post_init__(self):
        f = "refineries.yaml"
        ident = f"{self.refinery_key}.configs.{self.key}"
        if self.cdu_capacity_kbd <= 0:
            _fail(f, ident, "cdu_capacity_kbd must be > 0")
        if self.conversion_unit is not None and \
                self.conversion_unit not in CONVERSION_UNITS:
            _fail(f, ident,
                  f"conversion_unit='{self.conversion_unit}' "
                  f"not in {CONVERSION_UNITS} (or null)")
        if self.conversion_unit is None and self.conversion_capacity_kbd != 0:
            _fail(f, ident,
                  "conversion_capacity_kbd must be 0 when conversion_unit is null")
        if self.conversion_unit is not None and self.conversion_capacity_kbd <= 0:
            _fail(f, ident,
                  "conversion_capacity_kbd must be > 0 when a conversion unit is set")
        if self.coker_capacity_kbd < 0:
            _fail(f, ident, "coker_capacity_kbd must be >= 0")
        if self.reformer_capacity_kbd < 0:
            _fail(f, ident, "reformer_capacity_kbd must be >= 0")
        if not 0.0 < self.diesel_sulfur_spec_pct < 6.0:
            _fail(f, ident,
                  f"diesel_sulfur_spec_pct={self.diesel_sulfur_spec_pct} "
                  "outside (0, 6)")


@dataclass(frozen=True)
class Refinery:
    key: str
    name: str
    port: str
    max_vessel: str
    product_market: str
    configs: Mapping[str, RefineryConfig]

    def __post_init__(self):
        f = "refineries.yaml"
        if not self.configs:
            _fail(f, self.key, "needs at least one config")
        object.__setattr__(self, "configs", MappingProxyType(dict(self.configs)))


@dataclass(frozen=True)
class ConversionUnit:
    """A secondary unit: turns 1 bbl of `feed` into the `outputs` slate."""
    key: str                       # "fcc" | "hcu" | "coker"
    feed: str                      # the cut this unit consumes
    outputs: Mapping[str, float]   # yields over ALL_CUTS, sum == 1.0

    def __post_init__(self):
        f = "conversion_units.yaml"
        if self.feed not in CUTS:
            _fail(f, self.key, f"feed='{self.feed}' is not a known cut")
        missing = [c for c in ALL_CUTS if c not in self.outputs]
        if missing:
            _fail(f, self.key, f"missing output cuts: {missing}")
        unknown = [c for c in self.outputs if c not in ALL_CUTS]
        if unknown:
            _fail(f, self.key, f"unknown output cuts: {unknown}")
        negative = {c: v for c, v in self.outputs.items() if v < 0}
        if negative:
            _fail(f, self.key, f"negative outputs: {negative}")
        total = sum(self.outputs.values())
        if abs(total - 1.0) > YIELD_TOLERANCE:
            _fail(f, self.key, f"outputs sum to {total:.6f}, expected 1.0")
        if self.outputs.get(self.feed, 0.0) >= 1.0:
            _fail(f, self.key, "unit cannot yield >= 100% of its own feed")
        object.__setattr__(self, "outputs",
                           MappingProxyType(dict(self.outputs)))


@dataclass(frozen=True)
class ProductMarket:
    """Regional product price set: constant cracks over a benchmark curve."""
    key: str
    benchmark: str
    cracks: Mapping[str, float]   # $/bbl differential per cut

    def __post_init__(self):
        f = "product_markets.yaml"
        if self.benchmark not in BENCHMARKS:
            _fail(f, self.key, f"benchmark='{self.benchmark}' not in {BENCHMARKS}")
        missing = [c for c in CUTS if c not in self.cracks]
        if missing:
            _fail(f, self.key, f"missing cracks for cuts: {missing}")
        unknown = [c for c in self.cracks if c not in CUTS]
        if unknown:
            _fail(f, self.key, f"unknown cuts in cracks: {unknown}")
        object.__setattr__(self, "cracks", MappingProxyType(dict(self.cracks)))


@dataclass(frozen=True)
class Route:
    key: str
    distance_nm: float
    ws_flat_rate: float

    def __post_init__(self):
        f = "routes.yaml"
        if self.key != route_key(*self.key.split("-", 1)):
            _fail(f, self.key, "key must be 'portA-portB' sorted alphabetically")
        if self.distance_nm <= 0:
            _fail(f, self.key, "distance_nm must be > 0")
        if self.ws_flat_rate <= 0:
            _fail(f, self.key, "ws_flat_rate must be > 0")


@dataclass(frozen=True)
class Vessel:
    key: str
    cargo_kbbl: float
    speed_knots: float
    typical_ws_pct: float
    port_days: float

    def __post_init__(self):
        f = "vessels.yaml"
        if self.cargo_kbbl <= 0:
            _fail(f, self.key, "cargo_kbbl must be > 0")
        if self.speed_knots <= 0:
            _fail(f, self.key, "speed_knots must be > 0")
        if not 0 < self.typical_ws_pct <= 500:
            _fail(f, self.key, f"typical_ws_pct={self.typical_ws_pct} outside (0, 500]")
        if self.port_days < 0:
            _fail(f, self.key, "port_days must be >= 0")


# --------------------------------------------------------------------------
# Dataset container with referential integrity
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Dataset:
    crudes: Mapping[str, Crude]
    refineries: Mapping[str, Refinery]
    routes: Mapping[str, Route]
    vessels: Mapping[str, Vessel]
    conversion_units: Mapping[str, ConversionUnit]
    product_markets: Mapping[str, ProductMarket]

    def route_between(self, port_a: str, port_b: str) -> Route:
        key = route_key(port_a, port_b)
        if key not in self.routes:
            raise DataValidationError(
                f"[routes.yaml] no route between '{port_a}' and '{port_b}' "
                f"(expected key '{key}')")
        return self.routes[key]

    def validate_referential_integrity(self) -> None:
        """Cross-file checks. Called by load_dataset; idempotent."""
        for r in self.refineries.values():
            if r.max_vessel not in self.vessels:
                _fail("refineries.yaml", r.key,
                      f"max_vessel='{r.max_vessel}' not found in vessels.yaml")
            if r.product_market not in self.product_markets:
                _fail("refineries.yaml", r.key,
                      f"product_market='{r.product_market}' "
                      "not found in product_markets.yaml")
            for cfg in r.configs.values():
                ident = f"{r.key}.configs.{cfg.key}"
                if cfg.conversion_unit is not None and \
                        cfg.conversion_unit not in self.conversion_units:
                    _fail("refineries.yaml", ident,
                          f"conversion_unit='{cfg.conversion_unit}' "
                          "not found in conversion_units.yaml")
                if cfg.coker_capacity_kbd > 0 and \
                        "coker" not in self.conversion_units:
                    _fail("refineries.yaml", ident,
                          "has coker capacity but no 'coker' entry "
                          "in conversion_units.yaml")
                if cfg.reformer_capacity_kbd > 0 and \
                        "reformer" not in self.conversion_units:
                    _fail("refineries.yaml", ident,
                          "has reformer capacity but no 'reformer' entry "
                          "in conversion_units.yaml")
        # The most likely data error in this project: a missing route for a
        # (crude, refinery) pair. Fail loudly, listing every gap at once.
        gaps = []
        for c in self.crudes.values():
            for r in self.refineries.values():
                if route_key(c.fob_port, r.port) not in self.routes:
                    gaps.append(f"{c.key} ({c.fob_port}) -> {r.key} ({r.port}): "
                                f"add key '{route_key(c.fob_port, r.port)}'")
        if gaps:
            raise DataValidationError(
                "[routes.yaml] missing routes:\n  " + "\n  ".join(gaps))


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict:
    try:
        with open(path) as fh:
            content = yaml.safe_load(fh)
    except FileNotFoundError:
        raise DataValidationError(f"[{path.name}] file not found at {path}")
    except yaml.YAMLError as exc:
        raise DataValidationError(f"[{path.name}] invalid YAML: {exc}")
    if not isinstance(content, dict) or not content:
        raise DataValidationError(f"[{path.name}] must be a non-empty mapping")
    return content


def _build(cls, file: str, key: str, payload: dict, **extra):
    """Instantiate a dataclass from a YAML payload with friendly errors."""
    if not isinstance(payload, dict):
        _fail(file, key, "entry must be a mapping")
    try:
        return cls(key=key, **payload, **extra)
    except TypeError as exc:
        # Wrong/missing field names land here -- point at the file and key.
        _fail(file, key, f"bad or missing field ({exc})")


def load_crudes(path: Path) -> dict[str, Crude]:
    return {k: _build(Crude, path.name, k, v)
            for k, v in _read_yaml(path).items()}


def load_refineries(path: Path) -> dict[str, Refinery]:
    out = {}
    for rkey, payload in _read_yaml(path).items():
        if not isinstance(payload, dict) or "configs" not in payload:
            _fail(path.name, rkey, "entry must be a mapping with a 'configs' block")
        raw_configs = payload.pop("configs")
        if not isinstance(raw_configs, dict) or not raw_configs:
            _fail(path.name, rkey, "'configs' must be a non-empty mapping")
        configs = {ckey: _build(RefineryConfig, path.name, ckey, cval,
                                refinery_key=rkey)
                   for ckey, cval in raw_configs.items()}
        out[rkey] = _build(Refinery, path.name, rkey, payload, configs=configs)
    return out


def load_routes(path: Path) -> dict[str, Route]:
    return {k: _build(Route, path.name, k, v)
            for k, v in _read_yaml(path).items()}


def load_vessels(path: Path) -> dict[str, Vessel]:
    return {k: _build(Vessel, path.name, k, v)
            for k, v in _read_yaml(path).items()}


def load_conversion_units(path: Path) -> dict[str, ConversionUnit]:
    return {k: _build(ConversionUnit, path.name, k, v)
            for k, v in _read_yaml(path).items()}


def load_product_markets(path: Path) -> dict[str, ProductMarket]:
    return {k: _build(ProductMarket, path.name, k, v)
            for k, v in _read_yaml(path).items()}


def load_dataset(data_dir: str | Path) -> Dataset:
    """Load and fully validate the whole data directory. The single entry
    point the rest of the codebase should use."""
    d = Path(data_dir)
    ds = Dataset(
        crudes=MappingProxyType(load_crudes(d / "crudes.yaml")),
        refineries=MappingProxyType(load_refineries(d / "refineries.yaml")),
        routes=MappingProxyType(load_routes(d / "routes.yaml")),
        vessels=MappingProxyType(load_vessels(d / "vessels.yaml")),
        conversion_units=MappingProxyType(
            load_conversion_units(d / "conversion_units.yaml")),
        product_markets=MappingProxyType(
            load_product_markets(d / "product_markets.yaml")),
    )
    ds.validate_referential_integrity()
    return ds

