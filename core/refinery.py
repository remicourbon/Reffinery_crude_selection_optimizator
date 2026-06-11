"""Refinery economics: gross product worth, material balance, blend sulfur.

Design rules (see README):
- GPW is *pure*: sum over cuts of yield x price, with `loss` worth zero.
  Sulfur is handled exclusively by the LP constraint (lp_model.py); putting a
  sulfur penalty in GPW as well would double-count it and corrupt the LP's
  shadow prices.
- Everything here is a pure function over the frozen dataclasses of
  data_models.py and plain dicts. No market access, no time dimension, no UI:
  prices arrive as a simple {cut: usd_per_bbl} mapping, whatever tenor
  market.py priced them at.
- A "basket" is a {crude_key: volume_kbd} mapping -- the LP's decision
  variable, but these functions don't know or care whether the basket came
  from an optimiser or a slider.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from core.data_models import CUTS, ConversionUnit, Crude, RefineryConfig

ProductPrices = Mapping[str, float]   # {cut: $/bbl}, must cover all CUTS
Basket = Mapping[str, float]          # {crude_key: kbd}, volumes >= 0


class RefineryInputError(ValueError):
    """Raised on inconsistent inputs (unknown crude, missing price, ...)."""


def _check_prices(prices: ProductPrices) -> None:
    missing = [c for c in CUTS if c not in prices]
    if missing:
        raise RefineryInputError(f"missing product prices for cuts: {missing}")


def _check_basket(basket: Basket, crudes: Mapping[str, Crude]) -> None:
    unknown = [k for k in basket if k not in crudes]
    if unknown:
        raise RefineryInputError(f"unknown crudes in basket: {unknown}")
    negative = {k: v for k, v in basket.items() if v < 0}
    if negative:
        raise RefineryInputError(f"negative volumes in basket: {negative}")


# --------------------------------------------------------------------------
# Per-crude economics
# --------------------------------------------------------------------------

def gpw(crude: Crude, prices: ProductPrices) -> float:
    """Gross product worth of one barrel of `crude`, $/bbl.

    Pure sum of yield x price over the six product cuts. The `loss` cut is
    valorised at zero -- it is the explicit cost of processing inefficiency.
    """
    _check_prices(prices)
    return sum(crude.yields[c] * prices[c] for c in CUTS)


# --------------------------------------------------------------------------
# Basket-level physics
# --------------------------------------------------------------------------

def material_balance(basket: Basket,
                     crudes: Mapping[str, Crude]) -> dict[str, float]:
    """Volumes of each cut (incl. loss) produced by the basket, in kb/d.

    Invariant (tested): sum of all output cuts == sum of basket volumes.
    """
    _check_basket(basket, crudes)
    balance = {c: 0.0 for c in CUTS + ("loss",)}
    for key, vol in basket.items():
        for cut, y in crudes[key].yields.items():
            balance[cut] += vol * y
    return balance


def blend_diesel_sulfur(basket: Basket,
                        crudes: Mapping[str, Crude]) -> float:
    """Volume-weighted sulfur (%) of the pooled diesel cut.

    This is the *reporting* view of the quantity the LP constrains in its
    linearised form: sum_i x_i * y_diesel_i * (S_i - S_max) <= 0. Here we
    compute the actual ratio for display; returns 0.0 on an empty pool.
    """
    _check_basket(basket, crudes)
    pool = sum(basket[k] * crudes[k].yields["diesel"] for k in basket)
    if pool <= 0:
        return 0.0
    weighted = sum(basket[k] * crudes[k].yields["diesel"]
                   * crudes[k].diesel_sulfur_pct for k in basket)
    return weighted / pool


# --------------------------------------------------------------------------
# Feasibility reporting against a refinery configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class UtilisationReport:
    """How a basket loads each constrained unit of a configuration.

    Utilisation is used/capacity, or None when the unit doesn't exist.
    `feasible` is the headline answer; the rest feeds the UI (gauges) and
    explains *why* when it is False.
    """
    cdu_used_kbd: float
    cdu_utilisation: float
    conversion_used_kbd: float
    conversion_utilisation: float | None
    coker_used_kbd: float
    coker_utilisation: float | None
    diesel_sulfur_pct: float
    diesel_sulfur_ok: bool
    feasible: bool


def utilisation(basket: Basket,
                crudes: Mapping[str, Crude],
                config: RefineryConfig,
                tolerance: float = 1e-9) -> UtilisationReport:
    """Check a basket against a configuration's capacities and sulfur spec.

    Note the convention shared with lp_model.py: the conversion unit is fed
    by the VGO cut, the coker by the residue cut. A unit with zero capacity
    means the cut has nowhere to go for upgrading -- it still leaves the
    refinery (sold as-is at its cut price), so this is *not* an infeasibility,
    just poor economics that GPW already reflects.
    """
    balance = material_balance(basket, crudes)
    cdu_used = sum(basket.values())
    conv_used = balance["vgo"]
    coker_used = balance["residue"]
    sulfur = blend_diesel_sulfur(basket, crudes)

    def util(used: float, cap: float) -> float | None:
        return (used / cap) if cap > 0 else None

    conv_util = util(conv_used, config.conversion_capacity_kbd)
    coker_util = util(coker_used, config.coker_capacity_kbd)

    cdu_ok = cdu_used <= config.cdu_capacity_kbd + tolerance
    conv_ok = (config.conversion_capacity_kbd <= 0
               or conv_used <= config.conversion_capacity_kbd + tolerance)
    coker_ok = (config.coker_capacity_kbd <= 0
                or coker_used <= config.coker_capacity_kbd + tolerance)
    sulfur_ok = sulfur <= config.diesel_sulfur_spec_pct + tolerance

    return UtilisationReport(
        cdu_used_kbd=cdu_used,
        cdu_utilisation=cdu_used / config.cdu_capacity_kbd,
        conversion_used_kbd=conv_used,
        conversion_utilisation=conv_util,
        coker_used_kbd=coker_used,
        coker_utilisation=coker_util,
        diesel_sulfur_pct=sulfur,
        diesel_sulfur_ok=sulfur_ok,
        feasible=cdu_ok and conv_ok and coker_ok and sulfur_ok,
    )


# --------------------------------------------------------------------------
# Upgrading (conversion units)
# --------------------------------------------------------------------------

def uplift(unit: ConversionUnit, prices: ProductPrices) -> float:
    """Value created by upgrading 1 bbl of feed through `unit`, $/bbl.

    = value of the output slate - price of the feed sold as-is.
    Can be negative (e.g. in a weird price scenario where VGO outprices
    naphtha): the refinery would then leave the unit idle, which is exactly
    what apply_upgrading and the LP both do.
    """
    _check_prices(prices)
    out_value = sum(unit.outputs[c] * prices[c] for c in CUTS)
    return out_value - prices[unit.feed]


@dataclass(frozen=True)
class UpgradingResult:
    """Product slate and economics of a basket after upgrading.

    upgraded volumes follow the rule: upgrade min(feed available, capacity)
    if the unit's uplift is positive, else nothing. This greedy rule is
    exactly the LP optimum for the upgrading variables (their only other
    constraints are the bounds themselves), so the Refinery page and the
    optimiser can never disagree.
    """
    balance: Mapping[str, float]          # final slate, kb/d, incl. loss
    upgraded_conv_kbd: float
    upgraded_coker_kbd: float
    conv_uplift_usd_bbl: float            # 0.0 when no unit
    coker_uplift_usd_bbl: float
    gpw_cdu_kusd_day: float               # value of the straight-run slate
    upgrading_gain_kusd_day: float        # extra value from conversion
    total_value_kusd_day: float           # = gpw_cdu + upgrading_gain


def _upgrade_step(balance: dict[str, float], unit: ConversionUnit,
                  capacity_kbd: float, prices: ProductPrices
                  ) -> tuple[float, float]:
    """Apply one unit in place; return (volume upgraded, uplift $/bbl)."""
    lift = uplift(unit, prices)
    feed_available = balance[unit.feed]
    volume = min(feed_available, capacity_kbd) if lift > 0 else 0.0
    if volume > 0:
        balance[unit.feed] -= volume
        for cut, y in unit.outputs.items():
            balance[cut] += volume * y
    return volume, lift


def apply_upgrading(basket: Basket,
                    crudes: Mapping[str, Crude],
                    config: RefineryConfig,
                    units: Mapping[str, ConversionUnit],
                    prices: ProductPrices) -> UpgradingResult:
    """Full refinery pass: CDU material balance, then conversion, then coker.

    Mass is conserved end to end (tested): upgrading only moves volume
    between cuts (some of it into `loss`).
    """
    _check_prices(prices)
    balance = material_balance(basket, crudes)
    gpw_cdu = sum(balance[c] * prices[c] for c in CUTS)

    conv_vol, conv_lift = 0.0, 0.0
    if config.conversion_unit is not None:
        if config.conversion_unit not in units:
            raise RefineryInputError(
                f"unknown conversion unit '{config.conversion_unit}'")
        conv_vol, conv_lift = _upgrade_step(
            balance, units[config.conversion_unit],
            config.conversion_capacity_kbd, prices)

    coker_vol, coker_lift = 0.0, 0.0
    if config.coker_capacity_kbd > 0:
        if "coker" not in units:
            raise RefineryInputError("coker capacity set but no 'coker' unit")
        coker_vol, coker_lift = _upgrade_step(
            balance, units["coker"], config.coker_capacity_kbd, prices)

    gain = conv_vol * max(conv_lift, 0.0) + coker_vol * max(coker_lift, 0.0)
    return UpgradingResult(
        balance=MappingProxyType(balance),
        upgraded_conv_kbd=conv_vol,
        upgraded_coker_kbd=coker_vol,
        conv_uplift_usd_bbl=conv_lift if config.conversion_unit else 0.0,
        coker_uplift_usd_bbl=coker_lift if config.coker_capacity_kbd > 0 else 0.0,
        gpw_cdu_kusd_day=gpw_cdu,
        upgrading_gain_kusd_day=gain,
        total_value_kusd_day=gpw_cdu + gain,
    )
