"""Section 1014 step-up, and why "swap till you drop" is a real strategy.

At death the heir's basis becomes fair market value. Every dollar of deferred
gain from every exchange in the chain, and every dollar of depreciation ever
taken, disappears untaxed. The depreciation clock restarts at the stepped-up
basis, so the heir gets a fresh 27.5 years on a much larger number.

This is why the honest answer to "hold, sell, or exchange" is sometimes
"exchange, and then keep exchanging", and why a model that stops at a 30-year
horizon without modelling the terminal event systematically overstates the
cost of holding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..money import Money
from ..property import Property

__all__ = ["StepUpResult", "step_up_at_death"]


@dataclass(frozen=True)
class StepUpResult:
    on: date
    fair_market_value: Money
    basis_before: Money
    basis_after: Money
    gain_forgiven: Money
    depreciation_forgiven: Money
    deferred_1031_gain_forgiven: Money

    @property
    def tax_never_paid_on(self) -> Money:
        return self.gain_forgiven


def step_up_at_death(
    property_: Property, on: date, fair_market_value: Money
) -> StepUpResult:
    breakdown = property_.basis_on(on.year - 1)
    basis_before = breakdown.adjusted_basis
    deferred = (
        property_.carryover.deferred_gain if property_.carryover else Money(0)
    )
    return StepUpResult(
        on=on,
        fair_market_value=fair_market_value,
        basis_before=basis_before,
        basis_after=fair_market_value,
        gain_forgiven=(fair_market_value - basis_before).clamp_at_zero(),
        depreciation_forgiven=breakdown.accumulated_total,
        deferred_1031_gain_forgiven=deferred,
    )
