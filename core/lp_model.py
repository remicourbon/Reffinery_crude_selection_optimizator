"""The crude basket LP. The heart of the project.

Mathematical formulation (the blackboard version, to be defended verbatim):

  Decision variables
    x_i >= 0      volume of crude i purchased, kb/d
    u_conv >= 0   volume of feed upgraded through the conversion unit, kb/d
    u_coker >= 0  volume of residue upgraded through the coker, kb/d

  Objective (k$/day, since kb/d x $/bbl)
    max  sum_i x_i * (GPW_CDU_i - CIF_i)
         + u_conv  * uplift_conv
         + u_coker * uplift_coker

    where GPW_CDU_i = sum_c y_ic * p_c (straight-run value, loss at zero)
    and   uplift_u  = (output slate value of unit u) - (price of its feed).

  Constraints                                        name
    sum_i x_i  <= CAP_CDU                            "cdu"
    sum_i x_i   = V_target   (optional)              "total_volume"
    u_conv  <= sum_i x_i * y_i,feed_conv             "conv_feed"
    u_conv  <= CAP_conv                              "conv_capacity"
    u_coker <= sum_i x_i * y_i,res
               + u_conv * y_conv,res                 "coker_feed"
    u_coker <= CAP_coker                             "coker_capacity"
    sum_i x_i * y_i,diesel * (S_i - S_max) <= 0      "sulfur"

Modelling notes (defend these):
- Without constraints the objective is linear in x with constant
  coefficients: the LP would buy 100% of the best crude. All the
  intelligence lives in the constraints.
- The sulfur constraint is the linearised form of the pool-average spec
  (multiply both sides of the ratio by the positive denominator). It applies
  to the STRAIGHT-RUN diesel pool only; conversion-unit diesel is assumed to
  be hydrotreated separately (simplification, see README).
- Upgrading is sequential (conversion first, then coker), matching
  refinery.apply_upgrading: the coker's feed includes the conversion unit's
  residue make, but the coker's VGO make is NOT recycled to the conversion
  unit. Keeps the model acyclic and the story simple.
- Shadow prices (duals) are exported per constraint: the sulfur dual is the
  refinery's own sweet/sour premium; the conv_capacity dual is the marginal
  value of one more barrel of conversion.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional

import pulp

from core.data_models import ConversionUnit, Crude, RefineryConfig
from core.refinery import ProductPrices, gpw, uplift


class OptimisationError(ValueError):
    """Raised on inconsistent optimisation inputs."""


@dataclass(frozen=True)
class OptimisationResult:
    status: str                       # "Optimal", "Infeasible", ...
    basket: Mapping[str, float]       # {crude_key: kb/d} (only x_i > 0)
    upgraded_conv_kbd: float
    upgraded_coker_kbd: float
    margin_kusd_day: float            # objective value
    shadow_prices: Mapping[str, float]   # {constraint_name: dual}

    @property
    def optimal(self) -> bool:
        return self.status == "Optimal"


def optimise_basket(crudes: Mapping[str, Crude],
                    config: RefineryConfig,
                    units: Mapping[str, ConversionUnit],
                    prices: ProductPrices,
                    cif_usd_bbl: Mapping[str, float],
                    total_volume_kbd: Optional[float] = None
                    ) -> OptimisationResult:
    """Solve the basket LP for one refinery configuration.

    `cif_usd_bbl` defines the candidate set: only crudes with a CIF are
    considered (decision.py builds it from freight + market). When
    `total_volume_kbd` is given the basket must total exactly that volume
    (the user's delivery decision); otherwise the CDU capacity is the only
    volume bound.
    """
    unknown = set(cif_usd_bbl) - set(crudes)
    if unknown:
        raise OptimisationError(f"CIF given for unknown crudes: {unknown}")
    candidates = {k: crudes[k] for k in cif_usd_bbl}
    if not candidates:
        raise OptimisationError("no candidate crudes (empty CIF map)")
    if total_volume_kbd is not None and \
            total_volume_kbd > config.cdu_capacity_kbd:
        raise OptimisationError(
            f"total volume {total_volume_kbd} exceeds CDU capacity "
            f"{config.cdu_capacity_kbd}")

    prob = pulp.LpProblem("crude_basket", pulp.LpMaximize)
    x = {k: pulp.LpVariable(f"x_{k}", lowBound=0) for k in candidates}

    # --- objective ---------------------------------------------------------
    objective = pulp.lpSum(
        x[k] * (gpw(c, prices) - cif_usd_bbl[k])
        for k, c in candidates.items())

    conv_unit = units.get(config.conversion_unit) \
        if config.conversion_unit else None
    u_conv = None
    if conv_unit is not None:
        u_conv = pulp.LpVariable("u_conv", lowBound=0)
        objective += u_conv * uplift(conv_unit, prices)

    coker = units.get("coker") if config.coker_capacity_kbd > 0 else None
    if config.coker_capacity_kbd > 0 and coker is None:
        raise OptimisationError("coker capacity set but no 'coker' unit")
    u_coker = None
    if coker is not None:
        u_coker = pulp.LpVariable("u_coker", lowBound=0)
        objective += u_coker * uplift(coker, prices)

    prob += objective

    # --- constraints (named, so duals are addressable) ---------------------
    prob += (pulp.lpSum(x.values()) <= config.cdu_capacity_kbd, "cdu")
    if total_volume_kbd is not None:
        prob += (pulp.lpSum(x.values()) == total_volume_kbd, "total_volume")

    if u_conv is not None:
        feed = conv_unit.feed
        prob += (u_conv <= pulp.lpSum(x[k] * c.yields[feed]
                                      for k, c in candidates.items()),
                 "conv_feed")
        prob += (u_conv <= config.conversion_capacity_kbd, "conv_capacity")

    if u_coker is not None:
        residue_make = pulp.lpSum(x[k] * c.yields["residue"]
                                  for k, c in candidates.items())
        if u_conv is not None:
            residue_make += u_conv * conv_unit.outputs["residue"]
        prob += (u_coker <= residue_make, "coker_feed")
        prob += (u_coker <= config.coker_capacity_kbd, "coker_capacity")

    prob += (pulp.lpSum(
        x[k] * c.yields["diesel"]
        * (c.diesel_sulfur_pct - config.diesel_sulfur_spec_pct)
        for k, c in candidates.items()) <= 0, "sulfur")

    # --- solve --------------------------------------------------------------
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    status = pulp.LpStatus[prob.status]

    if status != "Optimal":
        return OptimisationResult(status, MappingProxyType({}), 0.0, 0.0,
                                  0.0, MappingProxyType({}))

    basket = {k: v.value() for k, v in x.items() if v.value() > 1e-9}
    duals = {name: c.pi for name, c in prob.constraints.items()
             if c.pi is not None}
    return OptimisationResult(
        status=status,
        basket=MappingProxyType(basket),
        upgraded_conv_kbd=u_conv.value() if u_conv is not None else 0.0,
        upgraded_coker_kbd=u_coker.value() if u_coker is not None else 0.0,
        margin_kusd_day=pulp.value(prob.objective),
        shadow_prices=MappingProxyType(duals),
    )
