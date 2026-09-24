"""Tax rules as versioned data.

Too much of what a firm knows about real estate lives in somebody's head. The
fix is not better documentation, it is making the rules a *data* artifact that
the engine reads, that carries its own citation and effective dates, and that
is versioned so "why is this number different from last quarter" has an
answer other than "someone changed the code".

Every computed result records the ``RuleSet.version`` that produced it, so a
number from March 2026 can be reproduced in March 2027 even after the rules
have moved on.

Bracket tables are stored as ordered (upper bound, rate) pairs with the top
bracket unbounded. Amounts are whole dollars in the source files - tax tables
are published in whole dollars - and are converted to :class:`Money` on load.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..money import Money, Rate

__all__ = [
    "FilingStatus",
    "Bracket",
    "BracketTable",
    "NIITRule",
    "PassiveActivityRule",
    "BonusDepreciationRule",
    "QBIRule",
    "Section121Rule",
    "StateRule",
    "RuleSet",
]


class FilingStatus(str, Enum):
    SINGLE = "single"
    MARRIED_FILING_JOINTLY = "married_filing_jointly"
    MARRIED_FILING_SEPARATELY = "married_filing_separately"
    HEAD_OF_HOUSEHOLD = "head_of_household"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Bracket(_Frozen):
    """One bracket. ``up_to=None`` means the top, unbounded bracket."""

    up_to: int | None = None
    rate: Decimal

    @field_validator("rate")
    @classmethod
    def _sane_rate(cls, v: Decimal) -> Decimal:
        if not (Decimal(0) <= v <= Decimal(1)):
            raise ValueError(f"tax rate {v} outside 0..1")
        return v

    @property
    def ceiling(self) -> Money | None:
        return None if self.up_to is None else Money.from_dollars(self.up_to)


class BracketTable(_Frozen):
    """A progressive rate schedule for one filing status."""

    brackets: tuple[Bracket, ...]

    @field_validator("brackets")
    @classmethod
    def _ordered_and_capped(cls, v: tuple[Bracket, ...]) -> tuple[Bracket, ...]:
        if not v:
            raise ValueError("bracket table is empty")
        if v[-1].up_to is not None:
            raise ValueError("the top bracket must be unbounded (up_to: null)")
        bounds = [b.up_to for b in v[:-1]]
        if any(b is None for b in bounds):
            raise ValueError("only the last bracket may be unbounded")
        if bounds != sorted(bounds):
            raise ValueError("brackets must ascend")
        return v

    def tax_on(self, taxable: Money) -> Money:
        """Tax on ``taxable``, filling each bracket in turn."""
        if not taxable.is_positive():
            return Money(0)
        total = Money(0)
        floor = Money(0)
        for bracket in self.brackets:
            ceiling = bracket.ceiling
            top = taxable if ceiling is None else min(taxable, ceiling)
            slice_ = (top - floor).clamp_at_zero()
            if slice_.is_positive():
                total = total + slice_.apply_rate(bracket.rate)
            if ceiling is None or taxable <= ceiling:
                break
            floor = ceiling
        return total

    def tax_on_stacked(self, base: Money, additional: Money) -> Money:
        """Tax on ``additional`` when it sits on top of ``base``.

        This is how long-term capital gain is actually taxed: it stacks on top
        of ordinary income and is taxed at the rate for the *combined* income,
        not at the rate the gain would face on its own. A client with $180,000
        of ordinary income does not get the 0% capital gains bracket, and an
        engine that computes the gain in isolation will tell them they do.
        """
        return self.tax_on(base + additional) - self.tax_on(base)

    def marginal_rate_at(self, taxable: Money) -> Rate:
        for bracket in self.brackets:
            ceiling = bracket.ceiling
            if ceiling is None or taxable <= ceiling:
                return bracket.rate
        return self.brackets[-1].rate


class NIITRule(_Frozen):
    """Net investment income tax, IRC 1411.

    The thresholds were set by statute in 2013 and are **not indexed for
    inflation**, which is why more households cross them every year. Anyone who
    inflation-adjusts them is wrong, and increasingly wrong over time.
    """

    rate: Decimal
    thresholds: dict[FilingStatus, int]
    citation: str = "IRC 1411"
    inflation_indexed: bool = False

    def tax_on(
        self, net_investment_income: Money, magi: Money, status: FilingStatus
    ) -> Money:
        """3.8% of the *lesser* of net investment income and MAGI over the
        threshold - not of whichever one you looked at first."""
        threshold = Money.from_dollars(self.thresholds[status])
        excess = (magi - threshold).clamp_at_zero()
        base = min(net_investment_income.clamp_at_zero(), excess)
        return base.apply_rate(self.rate)


class PassiveActivityRule(_Frozen):
    """Passive activity loss limitation, IRC 469.

    Rental real estate is passive per se (469(c)(2)), so losses are suspended
    and carried forward rather than deducted, unless the taxpayer qualifies for
    the active-participation allowance or is a real estate professional
    (469(c)(7)). The allowance phases out over a $50,000 band and, like the
    NIIT thresholds, is **not indexed**.
    """

    special_allowance: int
    phaseout_start: dict[FilingStatus, int]
    phaseout_rate: Decimal
    citation: str = "IRC 469(i)"
    inflation_indexed: bool = False

    def allowance_for(self, magi: Money, status: FilingStatus) -> Money:
        cap = Money.from_dollars(self.special_allowance)
        start = Money.from_dollars(self.phaseout_start[status])
        excess = (magi - start).clamp_at_zero()
        if not excess.is_positive():
            return cap
        reduction = excess.apply_rate(self.phaseout_rate)
        return (cap - reduction).clamp_at_zero()


class BonusDepreciationRule(_Frozen):
    """IRC 168(k) bonus, which has an acquisition-date cliff, not just a
    placed-in-service date. Property acquired before the cliff but placed in
    service after it follows the old phase-down schedule."""

    rate_by_year: dict[int, Decimal]
    permanent_rate: Decimal | None = None
    permanent_from_acquisition_date: str | None = None
    citation: str = "IRC 168(k)"

    def rate_for(self, placed_in_service_year: int, acquired_on: str | None) -> Decimal:
        if (
            self.permanent_rate is not None
            and self.permanent_from_acquisition_date is not None
            and acquired_on is not None
            and acquired_on > self.permanent_from_acquisition_date
        ):
            return self.permanent_rate
        return self.rate_by_year.get(placed_in_service_year, Decimal(0))


class QBIRule(_Frozen):
    """IRC 199A qualified business income deduction."""

    rate: Decimal
    threshold: dict[FilingStatus, int]
    safe_harbor: str = "Rev. Proc. 2019-38"
    citation: str = "IRC 199A"


class Section121Rule(_Frozen):
    """Primary residence gain exclusion, IRC 121.

    Two traps live here. First, the exclusion never covers depreciation taken
    after 6 May 1997 - 121(d)(6) - so a converted rental always has a taxable
    unrecaptured 1250 slice no matter how long it was a home. Second,
    121(b)(5) prorates the exclusion by the fraction of the ownership period
    that was *nonqualified use* after 2008.
    """

    exclusion: dict[FilingStatus, int]
    ownership_years: Decimal
    use_years: Decimal
    lookback_years: Decimal
    nonqualified_use_from: int = 2009
    citation: str = "IRC 121"


class StateRule(_Frozen):
    """A state's treatment. The interesting states are the nonconforming ones."""

    code: str
    name: str
    brackets: dict[FilingStatus, BracketTable] = Field(default_factory=dict)
    flat_rate: Decimal | None = None
    taxes_capital_gains_as_ordinary: bool = True
    conforms_to_section_1031: bool = True
    clawback_on_out_of_state_exchange: bool = False
    clawback_form: str | None = None
    conforms_to_bonus_depreciation: bool = False
    additional_surtax: dict | None = None
    citation: str = ""
    notes: str = ""

    def tax_on(self, taxable: Money, status: FilingStatus) -> Money:
        if self.flat_rate is not None:
            return taxable.clamp_at_zero().apply_rate(self.flat_rate)
        table = self.brackets.get(status)
        if table is None:
            return Money(0)
        total = table.tax_on(taxable)
        if self.additional_surtax:
            threshold = Money.from_dollars(self.additional_surtax["threshold"])
            surtax_rate = Decimal(str(self.additional_surtax["rate"]))
            excess = (taxable - threshold).clamp_at_zero()
            total = total + excess.apply_rate(surtax_rate)
        return total


class RuleSet(_Frozen):
    """Everything the engine needs to compute one tax year in one jurisdiction."""

    version: str
    tax_year: int
    source: str
    ordinary: dict[FilingStatus, BracketTable]
    capital_gains: dict[FilingStatus, BracketTable]
    standard_deduction: dict[FilingStatus, int]
    unrecaptured_1250_max_rate: Decimal
    niit: NIITRule
    passive_activity: PassiveActivityRule
    bonus_depreciation: BonusDepreciationRule
    section_121: Section121Rule
    qbi: QBIRule | None = None
    states: dict[str, StateRule] = Field(default_factory=dict)
    verified: dict[str, str] = Field(default_factory=dict)
    """field -> primary source, so docs/ENGINE.md can be generated rather than
    hand-maintained, and anything unverified is visible rather than implied."""

    @property
    def rule_id_prefix(self) -> str:
        return f"fed-{self.tax_year}@{self.version}"
