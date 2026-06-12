"""The decision orchestrator: answers the Dangote question.

Given a refinery, a TARGET ARRIVAL DATE and a cargo volume, walk back
through time for every candidate crude:

  departure date  = arrival - voyage days (freight.py)
  FOB             = benchmark curve at DEPARTURE + static diff (market.py)
                    -> buying a far-away crude means buying an earlier tenor:
                       this is where contango/backwardation enters the CIF.
  CIF             = FOB + freight + financing(voyage, ACT/360) + insurance
                    + in-transit losses

All options are then comparable -- same arrival date -- and the LP
(lp_model.py) allocates the cargo against product prices AT ARRIVAL.

Cargo-vs-rate convention (defend this): the LP's capacities are in kb/d but
a cargo is in kbbl. We assume the cargo is processed at full CDU rate, so
unit constraints apply pro-rata over the processing window: capacities are
scaled by volume/CDU before solving. The sulfur constraint is
scale-invariant; shares are unaffected.

Known simplification (README): freight for each crude is quoted at the FULL
cargo volume. After the LP splits the basket, per-crude parcels would
re-price slightly (smaller parcels, more dead freight); iterating
freight x LP is deliberately out of scope.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, timedelta
from types import MappingProxyType
from typing import Mapping, Optional

from core.data_models import Dataset, RefineryConfig
from core.freight import (DEFAULT_FINANCING_RATE, DEFAULT_INSURANCE_PCT,
                          DEFAULT_LOSS_PCT, FreightInputError, FreightQuote,
                          best_quote, financing_usd_bbl)
from core.lp_model import OptimisationResult, optimise_basket
from core.market import Curve, crude_fob, product_prices


class DecisionError(ValueError):
    """Raised when a decision cannot be evaluated at all."""


@dataclass(frozen=True)
class CrudeOption:
    """One purchasable leg, fully priced at its own tenor."""
    crude_key: str
    departure_date: date
    voyage_days: float
    freight: FreightQuote
    fob_usd_bbl: float
    financing_usd_bbl: float
    insurance_usd_bbl: float
    losses_usd_bbl: float
    cif_usd_bbl: float


@dataclass(frozen=True)
class Decision:
    """Everything the Simulator page needs for one (refinery, date, volume)."""
    refinery_key: str
    config_key: str
    arrival_date: date
    volume_kbbl: float
    options: Mapping[str, CrudeOption]      # priced candidates
    excluded: Mapping[str, str]             # crude_key -> reason
    product_prices: Mapping[str, float]     # at arrival, refinery's market
    optimisation: OptimisationResult        # basket in kbbl of the cargo
    scaled_config: RefineryConfig           # capacities scaled to the cargo
                                            # (kbbl) -- the config the LP saw,
                                            # so utilisation() is consistent


def price_option(ds: Dataset, crude_key: str, refinery_key: str,
                 arrival: date, volume_kbbl: float,
                 curves: Mapping[str, Curve],
                 ws_pct: Optional[float] = None,
                 financing_rate: float = DEFAULT_FINANCING_RATE
                 ) -> CrudeOption:
    """Price one crude delivered to one refinery on `arrival`."""
    crude = ds.crudes[crude_key]
    refinery = ds.refineries[refinery_key]
    route = ds.route_between(crude.fob_port, refinery.port)
    fq = best_quote(route, ds.vessels, refinery.max_vessel,
                    volume_kbbl, crude.api, ws_pct)
    departure = arrival - timedelta(days=round(fq.voyage_days))
    fob = crude_fob(crude, curves, departure)
    financing = financing_usd_bbl(fob, fq.voyage_days, financing_rate)
    insurance = fob * DEFAULT_INSURANCE_PCT
    losses = fob * DEFAULT_LOSS_PCT
    return CrudeOption(
        crude_key=crude_key,
        departure_date=departure,
        voyage_days=fq.voyage_days,
        freight=fq,
        fob_usd_bbl=fob,
        financing_usd_bbl=financing,
        insurance_usd_bbl=insurance,
        losses_usd_bbl=losses,
        cif_usd_bbl=fob + fq.freight_usd_bbl + financing + insurance + losses,
    )


def _scaled_config(config: RefineryConfig,
                   volume_kbbl: float) -> RefineryConfig:
    """Scale kb/d unit capacities to the cargo volume (full-CDU-rate
    convention, see module docstring)."""
    scale = volume_kbbl / config.cdu_capacity_kbd
    return dataclasses.replace(
        config,
        cdu_capacity_kbd=volume_kbbl,
        conversion_capacity_kbd=config.conversion_capacity_kbd * scale,
        coker_capacity_kbd=config.coker_capacity_kbd * scale,
        reformer_capacity_kbd=config.reformer_capacity_kbd * scale,
    )


def evaluate(ds: Dataset, refinery_key: str, config_key: str,
             arrival: date, volume_kbbl: float,
             curves: Mapping[str, Curve],
             ws_pct: Optional[float] = None,
             financing_rate: float = DEFAULT_FINANCING_RATE,
             config_override: Optional[RefineryConfig] = None) -> Decision:
    """The full pipeline: price every candidate, then optimise the basket.

    `config_override`, when given, replaces the YAML config entirely -- this
    is how the Marseille sandbox feeds a live-built RefineryConfig (units
    toggled on/off, capacities and sulfur limit set by the user) without
    touching the data files. config_key is then only a label.
    """
    if refinery_key not in ds.refineries:
        raise DecisionError(f"unknown refinery '{refinery_key}'")
    refinery = ds.refineries[refinery_key]
    if volume_kbbl <= 0:
        raise DecisionError("volume_kbbl must be > 0")
    if config_override is not None:
        config = config_override
    else:
        if config_key not in refinery.configs:
            raise DecisionError(
                f"unknown config '{config_key}' for refinery '{refinery_key}'")
        config = refinery.configs[config_key]

    options: dict[str, CrudeOption] = {}
    excluded: dict[str, str] = {}
    for key in ds.crudes:
        try:
            options[key] = price_option(ds, key, refinery_key, arrival,
                                        volume_kbbl, curves, ws_pct,
                                        financing_rate)
        except FreightInputError as exc:
            excluded[key] = str(exc)
    if not options:
        raise DecisionError(
            f"no deliverable crude for {volume_kbbl} kb at "
            f"{refinery_key}: {excluded}")

    prices = product_prices(ds.product_markets[refinery.product_market],
                            curves, arrival)
    cif = {k: o.cif_usd_bbl for k, o in options.items()}
    scaled = _scaled_config(config, volume_kbbl)
    result = optimise_basket(ds.crudes, scaled,
                             ds.conversion_units, prices, cif,
                             total_volume_kbd=volume_kbbl)
    return Decision(
        refinery_key=refinery_key,
        config_key=config_key,
        arrival_date=arrival,
        volume_kbbl=volume_kbbl,
        options=MappingProxyType(options),
        excluded=MappingProxyType(excluded),
        product_prices=MappingProxyType(prices),
        optimisation=result,
        scaled_config=scaled,
    )
