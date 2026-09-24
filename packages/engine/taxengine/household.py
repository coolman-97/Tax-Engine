"""The household tax year - where properties stop being independent.

This module is the reason the project exists. Every spreadsheet an advisor
uses today models one property at a time, and a per-property model cannot see
the three effects that most often change the answer:

1. **Passive losses are a shared pool.** Section 469 suspends rental losses
   and carries them forward. Selling property A in a fully taxable disposition
   releases *A's entire suspended stack at once* - and once released those
   losses are no longer passive, so they shelter ordinary income, wage income,
   and gain on property B. The best year to sell B is often the year you sold
   A. No per-property calculator can see that.

2. **Brackets are shared.** Capital gain stacks on top of ordinary income, so
   the rate on a sale depends on everything else the household did that year.
   Two sales in one year push each other up the table; split across two years
   they may not.

3. **The NIIT threshold is a cliff the household crosses, not the property.**
   $250,000 of MAGI for a joint filer, never indexed. A second sale in the
   same year can drag an otherwise-untaxed slice of the first sale into 3.8%.

Order of operations follows the return itself, because that is the order that
is defensible when a CPA checks it:

    rental results -> passive netting -> 469(g) release on disposition ->
    469(i) allowance -> AGI -> deductions -> stack gain on ordinary ->
    ordinary tax, 25% unrecaptured 1250, 0/15/20 capital gain -> NIIT -> state
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from .character import GainCharacter
from .money import Money, Rate
from .rules.schema import FilingStatus, RuleSet, StateRule

#: Shared empty ledger. A module-level singleton rather than a default-argument
#: call: PassiveActivityLedger is frozen, so one instance is safe to share, and
#: it keeps the default out of the function signature.
EMPTY_LEDGER: PassiveActivityLedger

__all__ = [
    "RentalYear",
    "PassiveActivityLedger",
    "HouseholdYear",
    "TaxYearResult",
    "compute_tax_year",
]


@dataclass(frozen=True)
class RentalYear:
    """One property's operating result for one year, as it lands on Schedule E."""

    activity_id: str
    rents: Money = Money(0)
    operating_expenses: Money = Money(0)
    mortgage_interest: Money = Money(0)
    depreciation: Money = Money(0)
    actively_participates: bool = True
    """Active participation (a lower bar than material participation) is what
    unlocks the $25,000 allowance in 469(i). Using a management company does
    not automatically lose it; giving up all decision rights does."""

    @property
    def net(self) -> Money:
        return self.rents - self.operating_expenses - self.mortgage_interest - self.depreciation

    @property
    def is_loss(self) -> bool:
        return self.net.is_negative()

    @property
    def cash_flow_before_principal(self) -> Money:
        """Depreciation is not cash. This is the number a client recognises."""
        return self.rents - self.operating_expenses - self.mortgage_interest


@dataclass(frozen=True)
class PassiveActivityLedger:
    """Suspended passive losses, per activity, carried forward.

    Kept per activity rather than as one pool because release under 469(g) is
    per activity: selling one property frees that property's stack and nobody
    else's.
    """

    suspended: Mapping[str, Money] = field(default_factory=dict)

    def total(self) -> Money:
        return Money.sum(self.suspended.values())

    def for_activity(self, activity_id: str) -> Money:
        return self.suspended.get(activity_id, Money(0))

    def with_added(self, additions: Mapping[str, Money]) -> PassiveActivityLedger:
        merged = dict(self.suspended)
        for key, amount in additions.items():
            if amount.is_positive():
                merged[key] = merged.get(key, Money(0)) + amount
        return PassiveActivityLedger(merged)

    def released(self, activity_ids: Sequence[str]) -> tuple[PassiveActivityLedger, Money]:
        """Release the stacks for fully-disposed activities.

        Returns the remaining ledger and the freed amount. Freed losses are no
        longer passive - 469(g)(1)(A) treats them as a loss "not from a passive
        activity" - so they are deductible against anything.
        """
        remaining = dict(self.suspended)
        freed = Money(0)
        for activity_id in activity_ids:
            freed = freed + remaining.pop(activity_id, Money(0))
        return PassiveActivityLedger(remaining), freed

    def transferred(self, source: str, destination: str) -> PassiveActivityLedger:
        """Move a suspended stack from a relinquished property to its
        replacement.

        A 1031 exchange is *not* a fully taxable disposition, so 469(g) does
        not fire and the suspended losses are **not** released. They ride along
        with the continuing activity. Getting this backwards is the single most
        expensive modelling error available here: it makes an exchange look
        like it comes with a large free deduction that does not exist.
        """
        remaining = dict(self.suspended)
        carried = remaining.pop(source, Money(0))
        if carried.is_positive():
            remaining[destination] = remaining.get(destination, Money(0)) + carried
        return PassiveActivityLedger(remaining)


@dataclass(frozen=True)
class HouseholdYear:
    """Everything the household did in one tax year."""

    year: int
    filing_status: FilingStatus
    wages: Money = Money(0)
    other_ordinary_income: Money = Money(0)
    portfolio_income: Money = Money(0)
    """Interest, dividends, and other net investment income that is not rental."""
    itemized_deductions: Money = Money(0)
    rentals: tuple[RentalYear, ...] = ()
    gains: GainCharacter = GainCharacter()
    fully_disposed_activities: tuple[str, ...] = ()
    """Activities sold in a fully taxable disposition to an unrelated party
    this year. Exchanges do not belong here."""
    is_real_estate_professional: bool = False
    """IRC 469(c)(7): rentals cease to be passive per se, and the income is
    generally outside the NIIT as well."""
    state: str | None = None
    prior_year_1231_losses: Money = Money(0)
    """Nonrecaptured net section 1231 losses from the previous five years.
    1231(c) recharacterises this year's 1231 gain as ordinary up to this
    amount - a lookback most models ignore entirely."""


@dataclass(frozen=True)
class TaxYearResult:
    """A full year, broken out so every line can be defended separately."""

    year: int
    filing_status: FilingStatus

    net_rental_income: Money
    passive_loss_allowed: Money
    passive_loss_suspended_this_year: Money
    passive_loss_released: Money
    special_allowance_used: Money
    ledger: PassiveActivityLedger

    agi: Money
    magi_for_niit: Money
    magi_for_469: Money
    deductions: Money
    taxable_income: Money

    ordinary_taxable: Money
    unrecaptured_1250_taxable: Money
    capital_gain_taxable: Money

    federal_ordinary_tax: Money
    unrecaptured_1250_tax: Money
    capital_gain_tax: Money
    niit: Money
    state_tax: Money

    rule_version: str

    @property
    def federal_tax(self) -> Money:
        return (
            self.federal_ordinary_tax
            + self.unrecaptured_1250_tax
            + self.capital_gain_tax
            + self.niit
        )

    @property
    def total_tax(self) -> Money:
        return self.federal_tax + self.state_tax

    @property
    def effective_rate(self) -> Rate:
        if not self.agi.is_positive():
            return Decimal(0)
        return self.total_tax.ratio_to(self.agi)


def compute_tax_year(
    household: HouseholdYear,
    rules: RuleSet,
    opening_ledger: PassiveActivityLedger | None = None,
) -> TaxYearResult:
    """Compute one household tax year, to the dollar."""

    opening_ledger = opening_ledger if opening_ledger is not None else EMPTY_LEDGER
    status = household.filing_status
    state_rule: StateRule | None = (
        rules.states.get(household.state) if household.state else None
    )

    # --- 1. Rental results -------------------------------------------------
    passive_income = Money.sum(r.net for r in household.rentals if not r.is_loss)
    current_losses = {r.activity_id: -r.net for r in household.rentals if r.is_loss}
    current_loss_total = Money.sum(current_losses.values())
    net_rental_income = passive_income - current_loss_total

    # --- 2. Release suspended stacks for fully taxable dispositions --------
    ledger, released = opening_ledger.released(household.fully_disposed_activities)

    # --- 3. Section 1231(c) five-year lookback ----------------------------
    gains = household.gains
    if household.prior_year_1231_losses.is_positive():
        gains = gains.with_1231_recharacterized(household.prior_year_1231_losses)

    recognized_gain = gains.recognized
    unrecaptured_1250 = gains.unrecaptured_1250
    capital_gain = gains.adjusted_net_capital_gain

    # --- 4. Passive netting ------------------------------------------------
    # A real estate professional's rentals are not passive at all.
    if household.is_real_estate_professional:
        allowed_loss = current_loss_total + released
        suspended_now = Money(0)
        allowance_used = Money(0)
    else:
        # Released losses are no longer passive: fully deductible, no allowance
        # needed, and they come off before the 469(i) test.
        offsettable = min(current_loss_total, passive_income)
        remaining_loss = current_loss_total - offsettable

        # MAGI for the 469(i) phaseout is AGI computed without passive losses.
        magi_for_469 = (
            household.wages
            + household.other_ordinary_income
            + household.portfolio_income
            + passive_income
            + recognized_gain
            - released
        ).clamp_at_zero()

        any_active = any(
            r.actively_participates for r in household.rentals if r.is_loss
        )
        allowance = (
            rules.passive_activity.allowance_for(magi_for_469, status)
            if any_active
            else Money(0)
        )
        allowance_used = min(remaining_loss, allowance)
        suspended_now = remaining_loss - allowance_used
        allowed_loss = offsettable + allowance_used + released

    if household.is_real_estate_professional:
        magi_for_469 = Money(0)

    # Suspended losses accrue per activity, pro rata to each activity's share
    # of this year's losses.
    additions: dict[str, Money] = {}
    if suspended_now.is_positive() and current_loss_total.is_positive():
        keys = sorted(current_losses)
        shares = suspended_now.allocate([current_losses[k].cents for k in keys])
        additions = dict(zip(keys, shares, strict=True))
    ledger = ledger.with_added(additions)

    # --- 5. AGI ------------------------------------------------------------
    agi = (
        household.wages
        + household.other_ordinary_income
        + household.portfolio_income
        + passive_income
        + recognized_gain
        - allowed_loss
    )

    # --- 6. Deductions and taxable income ---------------------------------
    standard = Money.from_dollars(rules.standard_deduction[status])
    deductions = max(standard, household.itemized_deductions)
    taxable_income = (agi - deductions).clamp_at_zero()

    # --- 7. Stack the gain on top of ordinary income ----------------------
    # Deductions absorb ordinary income first; preferential-rate income sits on
    # top, with unrecaptured 1250 below the 0/15/20 layer.
    preferential = unrecaptured_1250 + capital_gain
    ordinary_taxable = (taxable_income - preferential).clamp_at_zero()
    room = taxable_income - ordinary_taxable
    unrecaptured_taxable = min(unrecaptured_1250, room)
    capital_taxable = room - unrecaptured_taxable

    ordinary_table = rules.ordinary[status]
    capital_table = rules.capital_gains[status]

    federal_ordinary_tax = ordinary_table.tax_on(ordinary_taxable)

    # Unrecaptured 1250 is taxed at ordinary rates but capped at 25%
    # (IRC 1(h)(1)(E)). A taxpayer in the 12% bracket pays 12% on it, not 25%.
    at_ordinary = ordinary_table.tax_on_stacked(ordinary_taxable, unrecaptured_taxable)
    at_cap = unrecaptured_taxable.apply_rate(rules.unrecaptured_1250_max_rate)
    unrecaptured_1250_tax = min(at_ordinary, at_cap)

    capital_gain_tax = capital_table.tax_on_stacked(
        ordinary_taxable + unrecaptured_taxable, capital_taxable
    )

    # --- 8. NIIT -----------------------------------------------------------
    if household.is_real_estate_professional:
        rental_nii = Money(0)
    else:
        rental_nii = (passive_income - allowed_loss).clamp_at_zero()
    net_investment_income = (
        household.portfolio_income + rental_nii + gains.net_investment_income
    )
    magi_for_niit = agi
    niit = rules.niit.tax_on(net_investment_income, magi_for_niit, status)

    # --- 9. State ----------------------------------------------------------
    if state_rule is None:
        state_tax = Money(0)
    elif state_rule.taxes_capital_gains_as_ordinary:
        state_tax = state_rule.tax_on(taxable_income, status)
    else:
        state_tax = state_rule.tax_on(ordinary_taxable, status)

    return TaxYearResult(
        year=household.year,
        filing_status=status,
        net_rental_income=net_rental_income,
        passive_loss_allowed=allowed_loss,
        passive_loss_suspended_this_year=suspended_now,
        passive_loss_released=released,
        special_allowance_used=allowance_used,
        ledger=ledger,
        agi=agi,
        magi_for_niit=magi_for_niit,
        magi_for_469=magi_for_469,
        deductions=deductions,
        taxable_income=taxable_income,
        ordinary_taxable=ordinary_taxable,
        unrecaptured_1250_taxable=unrecaptured_taxable,
        capital_gain_taxable=capital_taxable,
        federal_ordinary_tax=federal_ordinary_tax,
        unrecaptured_1250_tax=unrecaptured_1250_tax,
        capital_gain_tax=capital_gain_tax,
        niit=niit,
        state_tax=state_tax,
        rule_version=rules.version,
    )


EMPTY_LEDGER = PassiveActivityLedger()
