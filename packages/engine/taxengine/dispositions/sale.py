"""An outright sale, and the order the gain gets carved up in.

The sequence below is not stylistic - it is the order Form 4797 and the
Schedule D worksheet apply, and getting it wrong changes the tax bill:

1. **Section 1245 recapture first.** Every dollar of depreciation on the 5-
   and 7-year buckets a cost segregation study carved out comes back as
   *ordinary* income, ahead of everything else, up to the amount of gain. At a
   37% marginal rate this is the most expensive slice and the one most often
   mislabelled as "25% recapture".
2. **Section 1250(a) additional depreciation next, also ordinary.** The excess
   of depreciation actually taken over hypothetical straight line. Zero for a
   building. Substantial for 15-year land improvements, which run on 150%
   declining balance and are bonus-eligible - so a cost-segregated property
   sold a few years after a 100% bonus year converts most of that bucket to
   ordinary income too. This is the slice rental calculators miss entirely.
3. **Unrecaptured section 1250 next.** What is left of the section 1250
   depreciation, taxed at up to 25%. Note "up to": IRC 1(h)(1)(E) caps it at
   25% but a taxpayer in a lower bracket pays the lower rate.
4. **Whatever is left** is section 1231 gain, which nets against 1231 losses
   and, if the net is a gain, is treated as long-term capital gain.

A loss runs the other way: a net section 1231 loss is an *ordinary* loss, fully
deductible, not a capital loss capped at $3,000. That asymmetry is the whole
point of section 1231 and it is worth a lot to a client selling at a loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from ..character import GainCharacter
from ..depreciation import Recapture
from ..money import Money, Rate
from ..property import Property
from ..rules.schema import FilingStatus, RuleSet

__all__ = ["SaleTerms", "SaleResult", "compute_sale"]


@dataclass(frozen=True)
class SaleTerms:
    on: date
    price: Money
    selling_cost_rate: Rate = Decimal("0.06")
    """Commission plus closing costs. Reduces the amount realised, so it
    reduces gain - it is not a separate deduction."""
    explicit_selling_costs: Money | None = None

    def selling_costs(self) -> Money:
        if self.explicit_selling_costs is not None:
            return self.explicit_selling_costs
        return self.price.apply_rate(self.selling_cost_rate)


@dataclass(frozen=True)
class SaleResult:
    """Everything an advisor needs to defend the exit number, line by line."""

    on: date
    gross_price: Money
    selling_costs: Money
    amount_realized: Money
    adjusted_basis: Money
    accumulated_1250: Money
    accumulated_1245: Money
    additional_1250: Money
    total_gain: Money
    character: GainCharacter
    debt_payoff: Money
    lien_clearing_costs: Money
    net_cash_before_tax: Money
    section_121_excluded: Money = Money(0)
    nonqualified_use_fraction: Rate = Decimal(0)
    is_loss: bool = False

    @property
    def net_cash_after_tax(self) -> Money:
        """Filled in by the household layer, which is the only place that can
        know the tax - the rate depends on the rest of the return."""
        return self.net_cash_before_tax


def compute_sale(
    property_: Property,
    terms: SaleTerms,
    rules: RuleSet,
    *,
    filing_status: FilingStatus = FilingStatus.MARRIED_FILING_JOINTLY,
    claim_section_121: bool = False,
) -> SaleResult:
    """Compute a sale, splitting the gain by how it will be taxed."""

    selling_costs = terms.selling_costs()
    amount_realized = terms.price - selling_costs

    # Depreciation is allowed in the year of sale too, prorated by convention,
    # so the schedules are re-cut at the disposition date and the whole basis
    # breakdown is recomputed from them.
    accumulated_1250 = Money(0)
    accumulated_1245 = Money(0)
    additional_1250 = Money(0)
    for _, schedule in property_.schedules():
        cut = schedule.truncate_at_disposition(terms.on)
        amount = cut.accumulated_through(terms.on.year)
        if schedule.recovery_class.recapture is Recapture.SECTION_1245:
            accumulated_1245 = accumulated_1245 + amount
        else:
            accumulated_1250 = accumulated_1250 + amount
            additional_1250 = additional_1250 + cut.additional_depreciation_through(
                terms.on.year
            )
    basis = property_.basis_on(terms.on.year - 1)

    adjusted_basis = (
        basis.original_basis
        + basis.capital_improvements
        - accumulated_1250
        - accumulated_1245
    )
    total_gain = amount_realized - adjusted_basis

    debt_payoff = property_.liens.total_debt(terms.on)
    clearing = property_.liens.clearing_costs(terms.on)
    net_cash = terms.price - selling_costs - debt_payoff - clearing

    # ---- Loss: section 1231 ordinary loss, not a capital loss --------------
    if not total_gain.is_positive():
        return SaleResult(
            on=terms.on, gross_price=terms.price, selling_costs=selling_costs,
            amount_realized=amount_realized, adjusted_basis=adjusted_basis,
            accumulated_1250=accumulated_1250, accumulated_1245=accumulated_1245,
            additional_1250=additional_1250, total_gain=total_gain,
            character=GainCharacter(section_1231_ordinary=total_gain),
            debt_payoff=debt_payoff, lien_clearing_costs=clearing,
            net_cash_before_tax=net_cash, is_loss=True,
        )

    # ---- Gain, carved in statutory order -----------------------------------
    remaining = total_gain

    # 1. Section 1245: all of it, ordinary.
    section_1245 = min(accumulated_1245, remaining)
    remaining = remaining - section_1245

    # 2. Section 1250(a): additional depreciation only, ordinary.
    section_1250_ordinary = min(additional_1250, remaining)
    remaining = remaining - section_1250_ordinary

    # 3. What is left of the 1250 depreciation, at up to 25%.
    unrecaptured_pool = (accumulated_1250 - additional_1250).clamp_at_zero()
    unrecaptured_1250 = min(unrecaptured_pool, remaining)
    remaining = remaining - unrecaptured_1250

    capital_gain = remaining
    excluded = Money(0)
    nonqualified_fraction = Decimal(0)

    # ---- Section 121, if this was once a home ------------------------------
    if claim_section_121 and property_.primary_residence is not None:
        history = property_.primary_residence
        owned_days = (terms.on - property_.acquired).days
        if owned_days > 0:
            nonqualified_years = history.nonqualified_use_years(
                property_.acquired, terms.on
            )
            owned_years = Decimal(owned_days) / Decimal(365)
            nonqualified_fraction = (
                min(Decimal(1), nonqualified_years / owned_years)
                if owned_years > 0
                else Decimal(0)
            )

        # 121(d)(6): depreciation taken after 6 May 1997 is NEVER excludable.
        # The unrecaptured 1250 slice above stays fully taxable no matter how
        # long the property was a principal residence. This is the single most
        # common section 121 error in a converted rental.
        eligible = capital_gain

        # 121(b)(5): prorate away the share of gain attributable to
        # nonqualified use after 2008.
        if nonqualified_fraction > 0:
            eligible = eligible - eligible.apply_rate(nonqualified_fraction)

        cap = Money.from_dollars(rules.section_121.exclusion[filing_status])
        excluded = min(eligible, cap)
        capital_gain = capital_gain - excluded

    character = GainCharacter(
        unrecaptured_1250=unrecaptured_1250,
        section_1245_ordinary=section_1245,
        section_1250_ordinary=section_1250_ordinary,
        adjusted_net_capital_gain=capital_gain,
        section_121_excluded=excluded,
    )

    return SaleResult(
        on=terms.on, gross_price=terms.price, selling_costs=selling_costs,
        amount_realized=amount_realized, adjusted_basis=adjusted_basis,
        accumulated_1250=accumulated_1250, accumulated_1245=accumulated_1245,
        additional_1250=additional_1250, total_gain=total_gain, character=character,
        debt_payoff=debt_payoff, lien_clearing_costs=clearing,
        net_cash_before_tax=net_cash, section_121_excluded=excluded,
        nonqualified_use_fraction=nonqualified_fraction,
    )
