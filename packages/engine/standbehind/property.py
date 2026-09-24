"""A property: how it was acquired, what it is worth, what its basis is.

Basis is the number everything downstream depends on, and it is the number
that is hardest to establish. Purchase price is in a closing file. The
land/improvement split is often in nobody's file at all and has to be asked of
the owner. Capital improvements are on receipts the client may or may not
still have. Depreciation is on a tax return, usually a different one each
year.

So this module does not take "basis" as an input. It takes the components,
each carrying its own provenance, and derives basis - which means the
derivation is inspectable and the engine knows exactly which missing component
is blocking a recommendation.

Three acquisition routes produce three different starting bases, and all three
appear in real portfolios:

- **Purchase** - cost, plus capitalised closing costs. IRC 1012.
- **Inheritance** - fair market value at death. IRC 1014. The depreciation
  clock restarts and all the decedent's accumulated depreciation vanishes,
  which is why "hold until death" is a real strategy rather than a joke.
- **1031 replacement** - carryover basis from the relinquished property, plus
  any excess basis paid in cash. Reg. 1.168(i)-6 keeps the carryover piece on
  the *old* property's remaining schedule rather than starting a fresh 27.5
  years, which is the detail that makes exchange modelling hard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum

from .depreciation import (
    FIFTEEN_YEAR,
    FIVE_YEAR,
    NONRESIDENTIAL_REAL,
    RESIDENTIAL_RENTAL,
    SEVEN_YEAR,
    DepreciationSchedule,
    Recapture,
    RecoveryClass,
    build_schedule,
)
from .loans import LienStack
from .money import Money, Rate

__all__ = [
    "PropertyKind",
    "AcquisitionKind",
    "CostSegregation",
    "CapitalImprovement",
    "CarryoverBasis",
    "PrimaryResidenceHistory",
    "Property",
    "BasisBreakdown",
]


class PropertyKind(Enum):
    RESIDENTIAL_RENTAL = "residential rental"
    NONRESIDENTIAL_REAL = "nonresidential real"

    @property
    def recovery_class(self) -> RecoveryClass:
        return (
            RESIDENTIAL_RENTAL
            if self is PropertyKind.RESIDENTIAL_RENTAL
            else NONRESIDENTIAL_REAL
        )


class AcquisitionKind(Enum):
    PURCHASE = "purchase"
    INHERITANCE = "inheritance"
    EXCHANGE_1031 = "1031 replacement"
    GIFT = "gift"


@dataclass(frozen=True)
class CostSegregation:
    """A cost segregation study, which reclassifies part of a building into
    shorter-lived components.

    The tax win is real and so is the bill at exit: these components recapture
    under section 1245 as **ordinary** income, not at the 25% unrecaptured 1250
    rate. A study that saved tax at 37% gives it back at 37%.
    """

    five_year: Money = Money(0)
    seven_year: Money = Money(0)
    fifteen_year: Money = Money(0)
    study_cost: Money = Money(0)
    performed_on: date | None = None

    @property
    def total(self) -> Money:
        return self.five_year + self.seven_year + self.fifteen_year

    def components(self) -> list[tuple[Money, RecoveryClass]]:
        return [
            (self.five_year, FIVE_YEAR),
            (self.seven_year, SEVEN_YEAR),
            (self.fifteen_year, FIFTEEN_YEAR),
        ]


@dataclass(frozen=True)
class CapitalImprovement:
    """A capital improvement starts its **own** depreciation schedule on its
    own placed-in-service date - it does not join the building's schedule.
    Adding it to basis and depreciating it over the building's remaining life
    is a common shortcut and it is wrong."""

    on: date
    amount: Money
    description: str = ""
    recovery_class: RecoveryClass | None = None


@dataclass(frozen=True)
class CarryoverBasis:
    """Basis and schedule inherited from a relinquished property in a 1031.

    Reg. 1.168(i)-6: the exchanged basis keeps depreciating over the
    relinquished property's *remaining* recovery period using its original
    method, and only the excess basis starts a new 27.5-year clock. The
    taxpayer may elect out and treat the whole thing as new property, which is
    sometimes better and sometimes worse - so both are modelled.
    """

    exchanged_basis: Money
    relinquished_placed_in_service: date
    relinquished_accumulated_depreciation: Money
    deferred_gain: Money
    relinquished_unrecaptured_1250: Money = Money(0)
    relinquished_state: str | None = None
    elect_out_of_1_168i6: bool = False


@dataclass(frozen=True)
class PrimaryResidenceHistory:
    """For a former home converted to a rental - the section 121 case."""

    occupied_from: date
    occupied_until: date

    def nonqualified_use_years(self, owned_from: date, sold: date) -> Decimal:
        """Years of nonqualified use after 2008, per 121(b)(5).

        Periods *after* the last date of use as a principal residence do not
        count as nonqualified use, which is the carve-out that makes
        convert-then-sell work at all. Only non-residence periods that fall
        between 1 Jan 2009 and the last day of residence count.
        """
        start = max(owned_from, date(2009, 1, 1))
        if self.occupied_from <= start:
            return Decimal(0)
        days = (min(self.occupied_from, sold) - start).days
        return max(Decimal(0), Decimal(days) / Decimal(365))


@dataclass(frozen=True)
class BasisBreakdown:
    """Adjusted basis, with the pieces kept apart because the exit tax needs
    them apart."""

    original_basis: Money
    land: Money
    depreciable_real: Money
    depreciable_personal: Money
    capital_improvements: Money
    accumulated_1250: Money
    """Depreciation on section 1250 property, ALL of it. The ordinary slice
    below is carved out of this figure, not added to it."""
    accumulated_1245: Money
    """Depreciation on section 1245 property (5- and 7-year cost seg buckets).
    Every dollar recaptures as ordinary income."""
    additional_1250: Money = Money(0)
    """The part of ``accumulated_1250`` that exceeds hypothetical straight
    line - section 1250(b)(1) additional depreciation, recaptured as ordinary
    income under 1250(a). Zero for a building; substantial for bonused
    15-year land improvements."""

    @property
    def accumulated_total(self) -> Money:
        return self.accumulated_1250 + self.accumulated_1245

    @property
    def unrecaptured_1250_pool(self) -> Money:
        """The slice eligible for the 25% rate, once the ordinary slice is out."""
        return (self.accumulated_1250 - self.additional_1250).clamp_at_zero()

    @property
    def adjusted_basis(self) -> Money:
        return (
            self.original_basis + self.capital_improvements - self.accumulated_total
        )


@dataclass(frozen=True)
class Property:
    """A single property, from acquisition to the day before disposition."""

    id: str
    address: str
    kind: PropertyKind
    acquired: date
    placed_in_service: date
    purchase_price: Money
    land_allocation: Money
    capitalized_closing_costs: Money = Money(0)
    acquisition_kind: AcquisitionKind = AcquisitionKind.PURCHASE
    cost_segregation: CostSegregation | None = None
    capital_improvements: tuple[CapitalImprovement, ...] = ()
    liens: LienStack = field(default_factory=lambda: LienStack(()))
    carryover: CarryoverBasis | None = None
    primary_residence: PrimaryResidenceHistory | None = None
    state: str | None = None
    bonus_rate_at_acquisition: Rate | None = None
    actively_participates: bool = True

    # ------------------------------------------------------------------
    @property
    def original_basis(self) -> Money:
        """Starting basis, which depends on how the property was acquired."""
        if self.carryover is not None:
            # Exchanged basis carries over; anything paid on top is excess basis.
            return self.carryover.exchanged_basis + self.excess_basis
        return self.purchase_price + self.capitalized_closing_costs

    @property
    def excess_basis(self) -> Money:
        if self.carryover is None:
            return Money(0)
        return (
            self.purchase_price
            + self.capitalized_closing_costs
            - self.carryover.exchanged_basis
        ).clamp_at_zero()

    @property
    def depreciable_personal(self) -> Money:
        return self.cost_segregation.total if self.cost_segregation else Money(0)

    @property
    def depreciable_real(self) -> Money:
        """The building: total basis, less land, less anything cost seg carved out."""
        return (
            self.original_basis - self.land_allocation - self.depreciable_personal
        ).clamp_at_zero()

    @property
    def land_ratio(self) -> Rate:
        total = self.purchase_price + self.capitalized_closing_costs
        return self.land_allocation.ratio_to(total) if total.is_positive() else Decimal(0)

    # ------------------------------------------------------------------
    def schedules(self) -> list[tuple[str, DepreciationSchedule]]:
        """Every depreciation schedule this property runs.

        Each schedule carries its own recovery class, and the recovery class
        carries its recapture character, so callers never have to guess which
        statute a bucket falls under.

        Memoised: the set is fixed once the property is constructed, and a
        thirty-year simulation asks for it once per property per year.
        """
        cached = getattr(self, "_schedules_cache", None)
        if cached is not None:
            return cached
        out = self._build_schedules()
        object.__setattr__(self, "_schedules_cache", out)
        return out

    def _build_schedules(self) -> list[tuple[str, DepreciationSchedule]]:
        out: list[tuple[str, DepreciationSchedule]] = []
        recovery = self.kind.recovery_class

        if self.carryover is not None and not self.carryover.elect_out_of_1_168i6:
            # Reg. 1.168(i)-6: the exchanged basis stays on the relinquished
            # property's clock. Model it by placing it in service on the
            # ORIGINAL date, so the remaining life is what is actually left.
            exchanged_depreciable = (
                self.carryover.exchanged_basis
                - self.land_allocation.apply_rate(
                    self.carryover.exchanged_basis.ratio_to(self.original_basis)
                    if self.original_basis.is_positive()
                    else Decimal(0)
                )
            ).clamp_at_zero()
            out.append((
                "exchanged basis (continues relinquished schedule)",
                build_schedule(
                    exchanged_depreciable,
                    self.carryover.relinquished_placed_in_service,
                    recovery,
                ),
            ))
            if self.excess_basis.is_positive():
                out.append((
                    "excess basis (new schedule)",
                    build_schedule(self.excess_basis, self.placed_in_service, recovery),
                ))
        else:
            if self.depreciable_real.is_positive():
                out.append((
                    "building",
                    build_schedule(
                        self.depreciable_real, self.placed_in_service, recovery
                    ),
                ))

        if self.cost_segregation is not None:
            for amount, component_class in self.cost_segregation.components():
                if amount.is_positive():
                    out.append((
                        component_class.name,
                        build_schedule(
                            amount,
                            self.placed_in_service,
                            component_class,
                            bonus_rate=self.bonus_rate_at_acquisition,
                        ),
                    ))

        for improvement in self.capital_improvements:
            cls = improvement.recovery_class or recovery
            out.append((
                f"improvement: {improvement.description or improvement.on.isoformat()}",
                build_schedule(improvement.amount, improvement.on, cls),
            ))

        return out

    # ------------------------------------------------------------------
    def depreciation_in_year(self, year: int) -> Money:
        return Money.sum(s.annual(year) for _, s in self.schedules())

    def basis_on(self, through_year: int) -> BasisBreakdown:
        """Adjusted basis at the end of ``through_year``."""
        accumulated_1250 = Money(0)
        accumulated_1245 = Money(0)
        additional_1250 = Money(0)
        for _, schedule in self.schedules():
            amount = schedule.accumulated_through(through_year)
            if schedule.recovery_class.recapture is Recapture.SECTION_1245:
                accumulated_1245 = accumulated_1245 + amount
            else:
                accumulated_1250 = accumulated_1250 + amount
                additional_1250 = additional_1250 + (
                    schedule.additional_depreciation_through(through_year)
                )

        # Depreciation the relinquished property already took rides along.
        if self.carryover is not None:
            accumulated_1250 = (
                accumulated_1250 + self.carryover.relinquished_accumulated_depreciation
            )

        improvements = Money.sum(i.amount for i in self.capital_improvements)
        return BasisBreakdown(
            original_basis=self.original_basis,
            land=self.land_allocation,
            depreciable_real=self.depreciable_real,
            depreciable_personal=self.depreciable_personal,
            capital_improvements=improvements,
            accumulated_1250=accumulated_1250,
            accumulated_1245=accumulated_1245,
            additional_1250=additional_1250,
        )

    def depreciation_exhausted_in(self) -> int:
        """The year the building's deduction runs out.

        A planning trigger with no external cause: nothing about the property
        changes, but the client's taxable income jumps. Worth telling an
        advisor about several years ahead.
        """
        years = [
            s.exhausted_by()
            for _, s in self.schedules()
            if s.recovery_class.recapture is Recapture.SECTION_1250 and s.by_year
        ]
        return max(years) if years else self.placed_in_service.year

    def equity_on(self, value: Money, when: date) -> Money:
        return value - self.liens.total_debt(when)

    def lendable_equity_on(
        self, value: Money, when: date, max_ltv: Rate = Decimal("0.70")
    ) -> Money:
        return self.liens.lendable_equity(value, when, max_ltv)
