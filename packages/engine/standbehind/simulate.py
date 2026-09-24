"""Simulating a portfolio forward, one year at a time.

The prompt this project answers asks for "buying and selling real estate over
a period of time". This is that module - but the interesting part is not the
loop, it is what the loop is allowed to assume.

The simulation is an **event-sourced ledger**. Each year emits immutable
entries; portfolio state is a fold over those entries. That buys three things
that matter for a number someone defends out loud:

- **Replayability.** The same scenario always produces the same ledger, and
  the ledger hashes, so "is this the number I showed the client in March"
  is answerable.
- **Auditability.** Every figure in the UI traces to a ledger entry, and every
  ledger entry names the rule version that produced it.
- **Diffability.** Two strategies can be compared line by line rather than
  only at the bottom.

Market assumptions live in one place and are explicit, because they are
assumptions - they carry ``Assumed`` provenance and the engine will not let
them masquerade as facts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import Enum

from .character import GainCharacter
from .dispositions.exchange_1031 import ExchangeTerms, compute_exchange
from .dispositions.sale import SaleTerms, compute_sale
from .household import (
    HouseholdYear,
    PassiveActivityLedger,
    RentalYear,
    TaxYearResult,
    compute_tax_year,
)
from .money import Money, Rate
from .property import Property, PropertyKind
from .rules import load as load_rules
from .rules.schema import FilingStatus, RuleSet

__all__ = [
    "MarketAssumptions", "PropertyAssumptions", "ActionKind", "Action",
    "Strategy", "YearLedger", "SimulationResult", "Portfolio", "simulate",
    "compare_strategies",
]


@dataclass(frozen=True)
class MarketAssumptions:
    """Everything the engine does not know and had to assume."""

    appreciation: Rate = Decimal("0.035")
    rent_growth: Rate = Decimal("0.03")
    expense_growth: Rate = Decimal("0.035")
    insurance_growth: Rate = Decimal("0.09")
    """Broken out from expenses on purpose. Insurance on apartment property
    rose about 75% between 2019 and 2024; lumping it into general expense
    inflation materially understates the cost of holding."""
    vacancy: Rate = Decimal("0.05")
    discount_rate: Rate = Decimal("0.06")
    selling_cost_rate: Rate = Decimal("0.06")
    cpi: Rate = Decimal("0.025")


@dataclass(frozen=True)
class PropertyAssumptions:
    """Per-property operating facts as of the simulation start."""

    property_id: str
    market_value: Money
    annual_rents: Money
    annual_operating_expenses: Money
    annual_insurance: Money = Money(0)
    appreciation: Rate | None = None
    capex_reserve_rate: Rate = Decimal("0.05")


class ActionKind(Enum):
    HOLD = "hold"
    SELL = "sell"
    EXCHANGE = "exchange"
    DIE = "die"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    property_id: str
    year: int
    month: int = 6
    replacement_price: Money | None = None
    replacement_debt: Money = Money(0)
    replacement_state: str | None = None
    claim_section_121: bool = False


@dataclass(frozen=True)
class Strategy:
    name: str
    actions: tuple[Action, ...] = ()
    description: str = ""

    def for_year(self, year: int) -> list[Action]:
        return [a for a in self.actions if a.year == year]


@dataclass(frozen=True)
class Portfolio:
    household_name: str
    filing_status: FilingStatus
    properties: tuple[Property, ...]
    assumptions: tuple[PropertyAssumptions, ...]
    wages: Money = Money(0)
    other_ordinary_income: Money = Money(0)
    portfolio_income: Money = Money(0)
    state: str | None = None
    opening_ledger: PassiveActivityLedger = field(default_factory=PassiveActivityLedger)
    is_real_estate_professional: bool = False

    def assumption_for(self, property_id: str) -> PropertyAssumptions:
        for a in self.assumptions:
            if a.property_id == property_id:
                return a
        raise KeyError(f"no assumptions supplied for property {property_id!r}")


@dataclass(frozen=True)
class YearLedger:
    """One simulated year. Immutable; the portfolio is a fold over these."""

    year: int
    property_values: Mapping[str, Money]
    property_debt: Mapping[str, Money]
    rental_cash_flow: Money
    principal_paid: Money
    depreciation: Money
    disposition_proceeds: Money
    tax: TaxYearResult
    events: tuple[str, ...]
    net_worth: Money
    after_tax_cash_flow: Money
    active_properties: tuple[str, ...]

    @property
    def total_value(self) -> Money:
        return Money.sum(self.property_values.values())

    @property
    def total_debt(self) -> Money:
        return Money.sum(self.property_debt.values())

    @property
    def equity(self) -> Money:
        return self.total_value - self.total_debt


@dataclass(frozen=True)
class SimulationResult:
    strategy: str
    years: tuple[YearLedger, ...]
    cash_balance: Money
    ledger_hash: str

    @property
    def terminal_net_worth(self) -> Money:
        return self.years[-1].net_worth if self.years else Money(0)

    @property
    def cumulative_tax(self) -> Money:
        return Money.sum(y.tax.total_tax for y in self.years)

    @property
    def cumulative_after_tax_cash_flow(self) -> Money:
        return Money.sum(y.after_tax_cash_flow for y in self.years)

    def npv(self, discount_rate: Rate, base_year: int | None = None) -> Money:
        """Present value of after-tax cash flow plus terminal net worth.

        Without discounting, "hold forever" wins every comparison by default,
        because a dollar of tax deferred for thirty years looks identical to a
        dollar of tax avoided. It is not.
        """
        if not self.years:
            return Money(0)
        base = base_year or self.years[0].year
        total = Money(0)
        for y in self.years:
            n = y.year - base
            factor = Decimal(1) / ((Decimal(1) + discount_rate) ** n)
            total = total + y.after_tax_cash_flow.apply_rate(factor)
        n = self.years[-1].year - base
        factor = Decimal(1) / ((Decimal(1) + discount_rate) ** n)
        return total + self.terminal_net_worth.apply_rate(factor)

    def all_events(self) -> list[tuple[int, str]]:
        return [(y.year, e) for y in self.years for e in y.events]


def _grow(amount: Money, growth: Rate, years: int) -> Money:
    if years <= 0:
        return amount
    return amount.apply_rate((Decimal(1) + growth) ** years)


def simulate(
    portfolio: Portfolio,
    strategy: Strategy,
    market: MarketAssumptions,
    *,
    start_year: int = 2026,
    horizon: int = 30,
    rules_for: Mapping[int, RuleSet] | None = None,
) -> SimulationResult:
    """Run one strategy forward and return the full ledger."""

    properties: dict[str, Property] = {p.id: p for p in portfolio.properties}
    active: set[str] = set(properties)
    pal = portfolio.opening_ledger
    cash = Money(0)
    years: list[YearLedger] = []
    prior_1231_losses = Money(0)
    deferred_state_gain: dict[str, Money] = {}

    for offset in range(horizon):
        year = start_year + offset
        rules = (
            rules_for[year] if rules_for and year in rules_for
            else load_rules(year, cpi=market.cpi)
        )
        events: list[str] = []
        rentals: list[RentalYear] = []
        values: dict[str, Money] = {}
        debts: dict[str, Money] = {}
        gains = GainCharacter()
        disposed: list[str] = []
        proceeds = Money(0)
        cash_flow = Money(0)
        principal_paid = Money(0)
        depreciation_total = Money(0)
        as_of = date(year, 12, 31)

        actions = {a.property_id: a for a in strategy.for_year(year)}

        for pid in sorted(active):
            prop = properties[pid]
            assumption = portfolio.assumption_for(pid)
            appreciation = assumption.appreciation or market.appreciation

            value = _grow(assumption.market_value, appreciation, offset)
            values[pid] = value
            debts[pid] = prop.liens.total_debt(as_of)

            rents = _grow(assumption.annual_rents, market.rent_growth, offset)
            rents = rents - rents.apply_rate(market.vacancy)
            opex = _grow(assumption.annual_operating_expenses, market.expense_growth, offset)
            insurance = _grow(assumption.annual_insurance, market.insurance_growth, offset)
            capex = rents.apply_rate(assumption.capex_reserve_rate)

            interest = Money.sum(
                lien.loan.deductible_interest_in_year(year)
                for lien in prop.liens.liens if lien.loan is not None
            )
            principal = Money.sum(
                lien.loan.principal_paid_in_year(year)
                for lien in prop.liens.liens if lien.loan is not None
            )
            depreciation = prop.depreciation_in_year(year)

            action = actions.get(pid)
            if action is not None and action.kind in (ActionKind.SELL, ActionKind.EXCHANGE):
                # Operating results are prorated to the month of disposition.
                months = action.month
                rents = rents.prorate(months, 12)
                opex = opex.prorate(months, 12)
                insurance = insurance.prorate(months, 12)
                capex = capex.prorate(months, 12)
                interest = interest.prorate(months, 12)
                principal = principal.prorate(months, 12)

            rentals.append(RentalYear(
                activity_id=pid, rents=rents,
                operating_expenses=opex + insurance + capex,
                mortgage_interest=interest, depreciation=depreciation,
                actively_participates=prop.actively_participates,
            ))
            cash_flow = cash_flow + rents - opex - insurance - capex - interest - principal
            principal_paid = principal_paid + principal
            depreciation_total = depreciation_total + depreciation

            if prop.depreciation_exhausted_in() == year:
                events.append(
                    f"{pid}: depreciation schedule exhausted - the shelter ends "
                    f"and taxable rental income steps up with no change to the property"
                )
            for lien in prop.liens.liens:
                if lien.loan is None:
                    continue
                for reset_date, was, now in lien.loan.resets():
                    if reset_date.year == year:
                        events.append(
                            f"{pid}: rate resets {was * 100:.3f}% -> "
                            f"{now * 100:.3f}% on {reset_date.isoformat()}"
                        )
                for shock_date, before, after in lien.loan.payment_shocks():
                    if shock_date.year == year:
                        jump = after.ratio_to(before) - Decimal(1)
                        events.append(
                            f"{pid}: PAYMENT SHOCK on {shock_date.isoformat()} - "
                            f"{before}/mo becomes {after}/mo (+{jump * 100:.0f}%)"
                        )

        # ---- dispositions -------------------------------------------------
        for pid, action in sorted(actions.items()):
            if pid not in active:
                continue
            prop = properties[pid]
            assumption = portfolio.assumption_for(pid)
            appreciation = assumption.appreciation or market.appreciation
            value = _grow(assumption.market_value, appreciation, offset)
            on = date(year, action.month, 15)

            if action.kind is ActionKind.SELL:
                result = compute_sale(
                    prop, SaleTerms(on, value, market.selling_cost_rate), rules,
                    filing_status=portfolio.filing_status,
                    claim_section_121=action.claim_section_121,
                )
                gains = gains + result.character
                proceeds = proceeds + result.net_cash_before_tax
                disposed.append(pid)
                active.discard(pid)
                events.append(
                    f"{pid}: SOLD for {value}. Gain {result.total_gain} "
                    f"({result.character.unrecaptured_1250} unrecaptured 1250, "
                    f"{result.character.section_1245_ordinary} ordinary 1245). "
                    f"Releases suspended passive losses of {pal.for_activity(pid)}."
                )
                if result.character.section_1231_ordinary.is_negative():
                    prior_1231_losses = prior_1231_losses - result.character.section_1231_ordinary

            elif action.kind is ActionKind.EXCHANGE:
                replacement_price = action.replacement_price or value
                result = compute_exchange(prop, ExchangeTerms(
                    relinquished_on=on, relinquished_price=value,
                    replacement_price=replacement_price,
                    replacement_debt=action.replacement_debt,
                    replacement_state=action.replacement_state,
                    selling_cost_rate=market.selling_cost_rate,
                ), rules)
                gains = gains + result.character
                new_id = f"{pid}->replacement{year}"
                replacement = Property(
                    id=new_id,
                    address=f"replacement for {prop.address}",
                    kind=PropertyKind.RESIDENTIAL_RENTAL,
                    acquired=on, placed_in_service=on,
                    purchase_price=replacement_price,
                    land_allocation=replacement_price.apply_rate(prop.land_ratio),
                    carryover=result.carryover,
                    state=action.replacement_state or prop.state,
                )
                properties[new_id] = replacement
                portfolio = replace(portfolio, assumptions=portfolio.assumptions + (
                    PropertyAssumptions(
                        property_id=new_id, market_value=replacement_price,
                        annual_rents=_grow(assumption.annual_rents, market.rent_growth, offset),
                        annual_operating_expenses=_grow(
                            assumption.annual_operating_expenses, market.expense_growth, offset),
                        annual_insurance=_grow(
                            assumption.annual_insurance, market.insurance_growth, offset),
                        appreciation=assumption.appreciation,
                    ),))
                active.discard(pid)
                active.add(new_id)
                # 469(g) does NOT fire: the losses follow the activity.
                pal = pal.transferred(pid, new_id)
                events.append(
                    f"{pid}: EXCHANGED into {replacement_price} replacement. "
                    f"{result.deferred_gain} deferred, {result.recognized_gain} recognised now. "
                    f"Suspended losses of {pal.for_activity(new_id)} transfer with the activity "
                    f"(they are NOT released - an exchange is not a taxable disposition)."
                )
                for warning in result.warnings:
                    events.append(f"{pid}: {warning}")
                if result.california_clawback:
                    deferred_state_gain[new_id] = result.deferred_gain

        # ---- the household return ----------------------------------------
        household = HouseholdYear(
            year=year, filing_status=portfolio.filing_status,
            wages=_grow(portfolio.wages, market.cpi, offset),
            other_ordinary_income=_grow(portfolio.other_ordinary_income, market.cpi, offset),
            portfolio_income=_grow(portfolio.portfolio_income, market.cpi, offset),
            rentals=tuple(rentals), gains=gains,
            fully_disposed_activities=tuple(disposed),
            is_real_estate_professional=portfolio.is_real_estate_professional,
            state=portfolio.state, prior_year_1231_losses=prior_1231_losses,
        )
        tax = compute_tax_year(household, rules, pal)
        pal = tax.ledger
        prior_1231_losses = Money(0)

        after_tax = cash_flow + proceeds - tax.total_tax
        cash = cash + after_tax
        equity = Money.sum(values.values()) - Money.sum(debts.values())
        net_worth = equity + cash

        years.append(YearLedger(
            year=year, property_values=values, property_debt=debts,
            rental_cash_flow=cash_flow, principal_paid=principal_paid,
            depreciation=depreciation_total, disposition_proceeds=proceeds,
            tax=tax, events=tuple(events), net_worth=net_worth,
            after_tax_cash_flow=after_tax, active_properties=tuple(sorted(active)),
        ))

    return SimulationResult(
        strategy=strategy.name, years=tuple(years), cash_balance=cash,
        ledger_hash=_hash_ledger(years),
    )


def _hash_ledger(years: Sequence[YearLedger]) -> str:
    """Content hash of the whole ledger.

    Replaying a scenario must produce a byte-identical hash. This is what makes
    "is this the number I showed the client in March" answerable, and it runs
    in CI so a refactor cannot silently move a number.
    """
    digest = hashlib.blake2b(digest_size=16)
    for y in years:
        payload = json.dumps({
            "year": y.year,
            "values": {k: v.cents for k, v in sorted(y.property_values.items())},
            "debt": {k: v.cents for k, v in sorted(y.property_debt.items())},
            "cash_flow": y.rental_cash_flow.cents,
            "tax": y.tax.total_tax.cents,
            "net_worth": y.net_worth.cents,
        }, sort_keys=True, separators=(",", ":"))
        digest.update(payload.encode())
    return digest.hexdigest()


def compare_strategies(
    portfolio: Portfolio,
    strategies: Iterable[Strategy],
    market: MarketAssumptions,
    **kwargs,
) -> list[SimulationResult]:
    return [simulate(portfolio, s, market, **kwargs) for s in strategies]
