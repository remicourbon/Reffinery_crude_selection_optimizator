"""Freight economics: Worldscale-based cost, voyage time, vessel choice.

Design rules (see README):
- Worldscale flat rates are $/tonne; conversion to $/bbl uses the crude's
  own density (from API gravity), not a generic 7.33 bbl/t -- a tonne of
  Basrah is fewer barrels than a tonne of WTI, and freight per barrel
  reflects it.
- Dead freight: a cargo smaller than the vessel still pays for the vessel.
  Cost is charged on max(volume, vessel cargo), which makes under-filling a
  big ship expensive and gives the deterministic vessel-choice rule its
  teeth -- all without any integer variable (the choice happens *before*
  the LP, see lp_model.py).
- Financing accrues ACT/360 on the FOB value during the voyage: capital is
  tied up from loading to discharge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from core.data_models import Route, Vessel

BBL_M3 = 0.158987          # one barrel in cubic metres
DEFAULT_FINANCING_RATE = 0.05
DEFAULT_INSURANCE_PCT = 0.05 / 100   # 0.05% of cargo value
DEFAULT_LOSS_PCT = 0.15 / 100        # 0.15% of volume lost in transit


class FreightInputError(ValueError):
    """Raised on inconsistent freight inputs."""


def tonnes_per_bbl(api: float) -> float:
    """Mass of one barrel, in tonnes, from API gravity."""
    sg = 141.5 / (131.5 + api)
    return sg * BBL_M3


def voyage_days(route: Route, vessel: Vessel) -> float:
    """Sea time at service speed plus total port days (load + discharge)."""
    sea = route.distance_nm / (vessel.speed_knots * 24.0)
    return sea + vessel.port_days


def financing_usd_bbl(fob_usd_bbl: float, days: float,
                      annual_rate: float = DEFAULT_FINANCING_RATE) -> float:
    """Cost of capital tied up during the voyage, ACT/360."""
    if days < 0:
        raise FreightInputError("days must be >= 0")
    return fob_usd_bbl * annual_rate * days / 360.0


@dataclass(frozen=True)
class FreightQuote:
    """Freight economics of one (route, vessel, volume) combination."""
    vessel_key: str
    volume_kbbl: float
    freight_usd_bbl: float       # incl. dead freight if under-filled
    voyage_days: float
    ws_pct: float                # the % actually used (override or typical)
    utilisation: float           # volume / vessel cargo, <= 1
    dead_freight: bool           # True when utilisation < 1


def quote(route: Route, vessel: Vessel, volume_kbbl: float, api: float,
          ws_pct: Optional[float] = None) -> FreightQuote:
    """Freight quote for one vessel on one route.

    $/bbl = flat ($/t) x WS% x t/bbl, scaled up by dead freight when the
    cargo under-fills the vessel: the charterer pays for the ship, not for
    the barrels.
    """
    if volume_kbbl <= 0:
        raise FreightInputError("volume_kbbl must be > 0")
    if volume_kbbl > vessel.cargo_kbbl:
        raise FreightInputError(
            f"volume {volume_kbbl} kb exceeds {vessel.key} cargo "
            f"({vessel.cargo_kbbl} kb); multi-voyage is out of scope")
    pct = vessel.typical_ws_pct if ws_pct is None else ws_pct
    if pct <= 0:
        raise FreightInputError("ws_pct must be > 0")

    base_usd_bbl = route.ws_flat_rate * (pct / 100.0) * tonnes_per_bbl(api)
    utilisation = volume_kbbl / vessel.cargo_kbbl
    dead_freight_factor = 1.0 / utilisation     # pay the whole ship
    return FreightQuote(
        vessel_key=vessel.key,
        volume_kbbl=volume_kbbl,
        freight_usd_bbl=base_usd_bbl * dead_freight_factor,
        voyage_days=voyage_days(route, vessel),
        ws_pct=pct,
        utilisation=utilisation,
        dead_freight=utilisation < 1.0 - 1e-9,
    )


def best_quote(route: Route,
               vessels: Mapping[str, Vessel],
               max_vessel_key: str,
               volume_kbbl: float,
               api: float,
               ws_pct: Optional[float] = None) -> FreightQuote:
    """Deterministic vessel choice: cheapest $/bbl among feasible vessels.

    Feasible = allowed at the discharge port (cargo size <= the port's
    max vessel) AND large enough to carry the volume in one voyage.
    `ws_pct`, when given, is the user's negotiated rate applied to every
    candidate (a simplification: in reality each class negotiates its own).

    This runs *before* the LP so the optimisation stays purely linear --
    letting the LP pick vessels would introduce min-cargo logic and turn
    the problem into a MILP for marginal benefit.
    """
    if max_vessel_key not in vessels:
        raise FreightInputError(f"unknown max_vessel '{max_vessel_key}'")
    port_limit = vessels[max_vessel_key].cargo_kbbl
    candidates = [v for v in vessels.values()
                  if v.cargo_kbbl <= port_limit and v.cargo_kbbl >= volume_kbbl]
    if not candidates:
        raise FreightInputError(
            f"no feasible vessel for {volume_kbbl} kb with port limit "
            f"'{max_vessel_key}' ({port_limit} kb)")
    quotes = [quote(route, v, volume_kbbl, api, ws_pct) for v in candidates]
    return min(quotes, key=lambda q: q.freight_usd_bbl)
