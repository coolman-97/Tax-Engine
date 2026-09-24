"""Like-kind exchange under section 1031, including the parts that bite.

An exchange is usually presented to a client as "no tax". That is true only
when every dollar of equity *and* every dollar of debt is replaced. The three
ways it stops being true:

- **Cash boot** - equity that does not go into the replacement property.
- **Mortgage boot** - debt relief that is not matched by new debt or new cash.
  Trading down in leverage is a taxable event even though no cash changed
  hands, and this is the one clients never see coming.
- **Recognised gain is characterised worst-first.** Boot does not pull a
  proportional slice of the gain; it pulls section 1245 recapture first (at
  ordinary rates), then unrecaptured 1250 at 25%, and only then capital gain.
  A small amount of boot on a cost-segregated property can be taxed entirely
  at 37%.

Two more that live here:

- **Suspended passive losses are not released.** An exchange is not a fully
  taxable disposition, so 469(g) does not fire. The losses ride along with the
  continuing activity - see ``household.PassiveActivityLedger.transferred``.
- **California does not let go.** Exchange a California property into an
  out-of-state replacement and the deferred California-source gain follows
  you, tracked on FTB Form 3840, which has to be filed *every year* until the
  gain is finally recognised. Miss it and the FTB may assess the deferred gain
  outright.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ..character import GainCharacter
from ..depreciation import Recapture
from ..money import Money, Rate
from ..property import CarryoverBasis, Property
from ..rules.schema import RuleSet

__all__ = ["ExchangeTerms", "ExchangeResult", "compute_exchange", "IdentificationRule"]

IDENTIFICATION_DAYS = 45
EXCHANGE_DAYS = 180


class IdentificationRule:
    THREE_PROPERTY = "three-property"
    TWO_HUNDRED_PERCENT = "200%"
    NINETY_FIVE_PERCENT = "95%"


@dataclass(frozen=True)
class ExchangeTerms:
    relinquished_on: date
    relinquished_price: Money
    replacement_price: Money
    replacement_on: date | None = None
    selling_cost_rate: Rate = Decimal("0.06")
    explicit_selling_costs: Money | None = None
    replacement_debt: Money = Money(0)
    additional_cash_invested: Money = Money(0)
    """Cash brought in *beyond* what the replacement purchase already requires -
    e.g. paying down the new loan at closing. The cash implied by the deal
    structure is derived, not taken from this field."""
    replacement_state: str | None = None
    replacement_closing_costs: Money = Money(0)
    is_related_party: bool = False
    elect_out_of_1_168i6: bool = False

    def selling_costs(self) -> Money:
        if self.explicit_selling_costs is not None:
            return self.explicit_selling_costs
        return self.relinquished_price.apply_rate(self.selling_cost_rate)

    @property
    def identification_deadline(self) -> date:
        return self.relinquished_on + timedelta(days=IDENTIFICATION_DAYS)

    @property
    def closing_deadline(self) -> date:
        """180 days, or the return due date if earlier. The return-due-date
        trap catches Q4 closings: relinquish in November and the deadline is
        15 April, not the following May, unless the return is extended."""
        return self.relinquished_on + timedelta(days=EXCHANGE_DAYS)


@dataclass(frozen=True)
class ExchangeResult:
    relinquished_on: date
    amount_realized: Money
    adjusted_basis: Money
    realized_gain: Money
    cash_boot: Money
    mortgage_boot: Money
    total_boot: Money
    recognized_gain: Money
    deferred_gain: Money
    character: GainCharacter
    replacement_basis: Money
    carryover: CarryoverBasis
    identification_deadline: date
    closing_deadline: date
    california_clawback: bool = False
    clawback_form: str | None = None
    related_party_holding_period_ends: date | None = None
    warnings: tuple[str, ...] = ()

    @property
    def fully_deferred(self) -> bool:
        return self.recognized_gain.is_zero()


def compute_exchange(
    property_: Property,
    terms: ExchangeTerms,
    rules: RuleSet,
) -> ExchangeResult:
    """Compute an exchange: boot, recognised gain, and the replacement basis."""

    selling_costs = terms.selling_costs()
    amount_realized = terms.relinquished_price - selling_costs

    accumulated_1250 = Money(0)
    accumulated_1245 = Money(0)
    additional_1250 = Money(0)
    for _, schedule in property_.schedules():
        cut = schedule.truncate_at_disposition(terms.relinquished_on)
        amount = cut.accumulated_through(terms.relinquished_on.year)
        if schedule.recovery_class.recapture is Recapture.SECTION_1245:
            accumulated_1245 = accumulated_1245 + amount
        else:
            accumulated_1250 = accumulated_1250 + amount
            additional_1250 = additional_1250 + cut.additional_depreciation_through(
                terms.relinquished_on.year
            )
    basis = property_.basis_on(terms.relinquished_on.year - 1)

    adjusted_basis = (
        basis.original_basis + basis.capital_improvements
        - accumulated_1250 - accumulated_1245
    )
    realized_gain = (amount_realized - adjusted_basis).clamp_at_zero()

    # ---- Boot --------------------------------------------------------------
    old_debt = property_.liens.total_debt(terms.relinquished_on)
    net_equity = amount_realized - old_debt
    cash_needed = (
        terms.replacement_price + terms.replacement_closing_costs - terms.replacement_debt
    )
    cash_boot = (net_equity - cash_needed).clamp_at_zero()

    # Mortgage boot: debt relief not matched by new debt.
    #
    # The offset is the part that is easy to get wrong. Cash the taxpayer
    # brings *into* the exchange is boot given, and it nets against debt relief
    # (Reg. 1.1031(b)-1(c)). That cash is usually not stated anywhere - it is
    # implied by the structure: whenever the replacement costs more than the
    # relinquished equity covers, the shortfall is cash the client must write a
    # cheque for, and it reduces mortgage boot dollar for dollar.
    #
    # Deriving it rather than asking for it is the difference between taxing
    # $254,444 and taxing $234,800 on the same deal. This engine originally
    # only honoured an explicitly-passed figure and therefore overstated boot
    # on every trade-down. See docs/EDGE_CASES.md #2.
    cash_brought_in = (cash_needed - net_equity).clamp_at_zero()
    total_cash_paid = cash_brought_in + terms.additional_cash_invested
    gross_mortgage_boot = (old_debt - terms.replacement_debt).clamp_at_zero()
    mortgage_boot = (gross_mortgage_boot - total_cash_paid).clamp_at_zero()

    total_boot = cash_boot + mortgage_boot
    recognized_gain = min(realized_gain, total_boot)
    deferred_gain = realized_gain - recognized_gain

    # ---- Characterise the recognised slice, worst first --------------------
    remaining = recognized_gain
    recognized_1245 = min(accumulated_1245, remaining)
    remaining = remaining - recognized_1245
    recognized_1250_ordinary = min(additional_1250, remaining)
    remaining = remaining - recognized_1250_ordinary
    unrecaptured_pool = (accumulated_1250 - additional_1250).clamp_at_zero()
    recognized_1250 = min(unrecaptured_pool, remaining)
    remaining = remaining - recognized_1250
    character = GainCharacter(
        unrecaptured_1250=recognized_1250,
        section_1245_ordinary=recognized_1245,
        section_1250_ordinary=recognized_1250_ordinary,
        adjusted_net_capital_gain=remaining,
        deferred_1031=deferred_gain,
    )

    # ---- Replacement basis -------------------------------------------------
    # Substituted basis: the deferred gain is baked into a lower basis, which
    # is how the tax comes back later. IRC 1031(d).
    replacement_total = terms.replacement_price + terms.replacement_closing_costs
    replacement_basis = (replacement_total - deferred_gain).clamp_at_zero()

    # Unrecaptured 1250 that was deferred rides along and will be recaptured
    # on the eventual taxable sale of the replacement.
    deferred_1250 = (
        accumulated_1250 - recognized_1250 - recognized_1250_ordinary
    ).clamp_at_zero()

    carryover = CarryoverBasis(
        exchanged_basis=min(replacement_basis, replacement_total),
        relinquished_placed_in_service=property_.placed_in_service,
        relinquished_accumulated_depreciation=Money(0),
        deferred_gain=deferred_gain,
        relinquished_unrecaptured_1250=deferred_1250,
        relinquished_state=property_.state,
        elect_out_of_1_168i6=terms.elect_out_of_1_168i6,
    )

    # ---- Jurisdiction and statutory traps ----------------------------------
    warnings: list[str] = []
    state_rule = rules.states.get(property_.state or "")
    clawback = False
    clawback_form = None
    if (
        state_rule is not None
        and state_rule.clawback_on_out_of_state_exchange
        and terms.replacement_state
        and terms.replacement_state != property_.state
    ):
        clawback = True
        clawback_form = state_rule.clawback_form
        warnings.append(
            f"{state_rule.name} clawback applies: {clawback_form} must be filed every "
            f"year until the deferred gain of {deferred_gain} is recognised, and "
            f"{state_rule.name} will tax it regardless of where the client lives then."
        )

    related_end = None
    if terms.is_related_party:
        related_end = date(
            terms.relinquished_on.year + 2,
            terms.relinquished_on.month,
            terms.relinquished_on.day,
        )
        warnings.append(
            "Related-party exchange under 1031(f): if either party disposes of "
            f"their property before {related_end.isoformat()}, the deferral is "
            "retroactively disallowed and the gain becomes taxable in that year."
        )

    if mortgage_boot.is_positive():
        warnings.append(
            f"Mortgage boot of {mortgage_boot}: debt dropped from {old_debt} to "
            f"{terms.replacement_debt}, only {total_cash_paid} of which was "
            "replaced with cash. Taxable even though no cash was received at "
            "closing - this is the boot clients never see coming."
        )

    if terms.replacement_on and terms.replacement_on > terms.closing_deadline:
        warnings.append(
            f"Replacement closed {terms.replacement_on.isoformat()}, after the "
            f"180-day deadline of {terms.closing_deadline.isoformat()}. The "
            "exchange fails entirely and the whole gain is taxable."
        )

    return ExchangeResult(
        relinquished_on=terms.relinquished_on,
        amount_realized=amount_realized,
        adjusted_basis=adjusted_basis,
        realized_gain=realized_gain,
        cash_boot=cash_boot,
        mortgage_boot=mortgage_boot,
        total_boot=total_boot,
        recognized_gain=recognized_gain,
        deferred_gain=deferred_gain,
        character=character,
        replacement_basis=replacement_basis,
        carryover=carryover,
        identification_deadline=terms.identification_deadline,
        closing_deadline=terms.closing_deadline,
        california_clawback=clawback,
        clawback_form=clawback_form,
        related_party_holding_period_ends=related_end,
        warnings=tuple(warnings),
    )
