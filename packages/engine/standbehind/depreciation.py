"""MACRS depreciation, to the cent.

Depreciation is where a real-estate engine quietly goes wrong. It is a long
schedule, so a rounding error in year one is still there in year twenty-seven,
and it does not just affect the deduction - accumulated depreciation is what
drives adjusted basis, which drives gain, which drives unrecaptured section
1250 tax at exit. One stray cent per year for 27.5 years is a wrong number in
the meeting that matters.

Two techniques keep it exact:

**Cumulative differencing (straight line).** Instead of computing each year's
deduction and adding them up - which accumulates rounding drift and leaves the
schedule not quite summing to basis - the engine computes *cumulative*
depreciation through the end of each period and takes differences. The total
is then exactly the depreciable basis by construction, for any basis, any
month, any recovery period.

**Explicit remaining-basis tracking (declining balance).** Declining balance is
inherently sequential, so each year is rounded once and subtracted from a
running remaining basis, with the final year taking whatever is left. The
schedule again sums to exactly basis.

Conventions implemented: mid-month for real property (section 168(d)(2)),
half-year and mid-quarter for personal property (section 168(d)(1) and (3)).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum

from .money import Money, Rate

__all__ = [
    "Convention",
    "Method",
    "Recapture",
    "RecoveryClass",
    "RESIDENTIAL_RENTAL",
    "NONRESIDENTIAL_REAL",
    "ADS_RESIDENTIAL_POST_2017",
    "ADS_RESIDENTIAL_PRE_2018",
    "ADS_NONRESIDENTIAL",
    "FIVE_YEAR",
    "SEVEN_YEAR",
    "FIFTEEN_YEAR",
    "DepreciationSchedule",
    "straight_line_schedule",
    "declining_balance_schedule",
    "build_schedule",
]


class Convention(Enum):
    """When property is treated as placed in service within its first year."""

    MID_MONTH = "mid-month"
    """Real property. Treated as placed in service at the midpoint of the
    month. IRC 168(d)(2)."""

    HALF_YEAR = "half-year"
    """Personal property, default. Half a year regardless of month.
    IRC 168(d)(1)."""

    MID_QUARTER = "mid-quarter"
    """Personal property, forced when more than 40% of the year's personal
    property is placed in service in Q4. IRC 168(d)(3)."""


class Method(Enum):
    STRAIGHT_LINE = "SL"
    DECLINING_200 = "200DB"
    DECLINING_150 = "150DB"


class Recapture(Enum):
    """Which recapture statute a class of property falls under at disposition.

    This is *not* determined by the recovery period, which is the trap. A
    cost segregation study produces 5-, 7- and 15-year buckets, and it is
    tempting to treat all three as section 1245 "personal property". They are
    not:

    - 5- and 7-year buckets (appliances, carpet, cabinetry) really are section
      1245 property. Every dollar of depreciation, bonus included, comes back
      as ordinary income.
    - 15-year land improvements (paving, fencing, landscaping, site utilities)
      are **section 1250 property**. Reg. 1.48-1(c) defines tangible personal
      property as "any tangible property except land and improvements thereto",
      and an apartment building is not an integral part of manufacturing under
      1245(a)(3)(B), so the fallback in 1250(c) applies.

    That distinction is not academic. Section 1250 only recaptures
    *additional* depreciation - the excess of what was actually taken over what
    straight line would have given. For a 27.5-year building depreciated
    straight line, that excess is zero, which is why individuals have no
    ordinary recapture on buildings. But 15-year land improvements use 150%
    declining balance **and are bonus-eligible**, so the excess is large and
    the ordinary recapture is real.

    Form 4797 line 26a spells it out: additional depreciation is the excess of
    actual depreciation "including any special depreciation allowance" over
    straight line. Buildings are not bonus-eligible so they never trip it;
    land improvements and QIP are, and they are not on the line-26 carve-out
    list.
    """

    SECTION_1245 = "1245"
    """All depreciation recaptured as ordinary income, capped at gain."""

    SECTION_1250 = "1250"
    """Only additional depreciation (actual less hypothetical straight line) is
    ordinary; the remainder is unrecaptured 1250 gain at up to 25%."""


@dataclass(frozen=True)
class RecoveryClass:
    """A MACRS property class: how long, what method, which convention."""

    name: str
    years: Decimal
    method: Method
    convention: Convention
    bonus_eligible: bool
    """Section 168(k) bonus applies to property with a recovery period of 20
    years or less. The building itself never qualifies; the 5/7/15-year
    components a cost segregation study carves out of it do. That asymmetry is
    the entire financial point of a cost seg study - and, via the additional
    depreciation rule above, a large part of its cost at exit."""

    recapture: Recapture = Recapture.SECTION_1250
    citation: str = ""

    @property
    def recovery_months(self) -> int:
        return int(self.years * 12)


# Real property - straight line, mid-month, never bonus eligible.
RESIDENTIAL_RENTAL = RecoveryClass(
    "residential rental", Decimal("27.5"), Method.STRAIGHT_LINE,
    Convention.MID_MONTH, False, Recapture.SECTION_1250,
    "IRC 168(b)(3)(B), 168(c), 168(d)(2)(B), 168(e)(2)(A)",
)
NONRESIDENTIAL_REAL = RecoveryClass(
    "nonresidential real", Decimal(39), Method.STRAIGHT_LINE,
    Convention.MID_MONTH, False, Recapture.SECTION_1250,
    "IRC 168(b)(3)(A), 168(c), 168(d)(2)(A), 168(e)(2)(B)",
)
# Alternative Depreciation System - the cost of electing out of the section
# 163(j) interest limitation as a real property trade or business.
ADS_RESIDENTIAL_POST_2017 = RecoveryClass(
    "ADS residential (placed in service after 2017)", Decimal(30),
    Method.STRAIGHT_LINE, Convention.MID_MONTH, False, Recapture.SECTION_1250,
    "IRC 168(g)(2)(C)(iii)",
)
ADS_RESIDENTIAL_PRE_2018 = RecoveryClass(
    "ADS residential (placed in service before 2018)", Decimal(40),
    Method.STRAIGHT_LINE, Convention.MID_MONTH, False, Recapture.SECTION_1250,
    "IRC 168(g)(2)(C)",
)
ADS_NONRESIDENTIAL = RecoveryClass(
    "ADS nonresidential real", Decimal(40), Method.STRAIGHT_LINE,
    Convention.MID_MONTH, False, "IRC 168(g)(2)(C)",
)

# Personal property and land improvements - what a cost seg study finds.
FIVE_YEAR = RecoveryClass(
    "5-year personal property", Decimal(5), Method.DECLINING_200,
    Convention.HALF_YEAR, True, Recapture.SECTION_1245,
    "IRC 168(e)(3)(B); 1245(a)(3)(A)",
)
SEVEN_YEAR = RecoveryClass(
    "7-year personal property", Decimal(7), Method.DECLINING_200,
    Convention.HALF_YEAR, True, Recapture.SECTION_1245,
    "IRC 168(e)(3)(C); 1245(a)(3)(A)",
)
FIFTEEN_YEAR = RecoveryClass(
    "15-year land improvements", Decimal(15), Method.DECLINING_150,
    Convention.HALF_YEAR, True, Recapture.SECTION_1250,
    # NOT 168(e)(3)(E) - that subparagraph covers municipal wastewater plant,
    # telephone distribution, retail motor fuels outlets and QIP, none of which
    # is a parking lot. Ordinary land improvements reach 15 years through the
    # class-life route instead.
    "IRC 168(e)(1) via Rev. Proc. 87-56 asset class 00.3 (class life 20 -> "
    "GDS 15); section 1250(c) per Reg. 1.48-1(c)",
)


@dataclass(frozen=True)
class DepreciationSchedule:
    """A finished schedule: how much is deducted in each calendar year."""

    recovery_class: RecoveryClass
    depreciable_basis: Money
    placed_in_service: date
    by_year: tuple[tuple[int, Money], ...]
    bonus_taken: Money = Money(0)
    straight_line_by_year: tuple[tuple[int, Money], ...] = ()
    """The hypothetical straight-line schedule over the same recovery period
    and convention. Only used to measure "additional depreciation" for section
    1250 property; for property already on straight line it is identical to
    ``by_year`` and the additional depreciation is zero."""

    def annual(self, year: int) -> Money:
        for y, amount in self.by_year:
            if y == year:
                return amount
        return Money(0)

    def accumulated_through(self, year: int) -> Money:
        """Total depreciation allowed or allowable through the end of ``year``.

        "Allowed *or allowable*" is the statutory phrase and it matters: basis
        is reduced by depreciation the taxpayer *could* have taken even if they
        did not take it (IRC 1016(a)(2)). An engine that only counts what was
        claimed will overstate basis and understate gain.
        """
        return Money.sum(amount for y, amount in self.by_year if y <= year)

    @property
    def total(self) -> Money:
        return Money.sum(amount for _, amount in self.by_year) + self.bonus_taken

    @property
    def final_year(self) -> int:
        return max((y for y, _ in self.by_year), default=self.placed_in_service.year)

    def straight_line_through(self, year: int) -> Money:
        """What straight line would have allowed through ``year``."""
        if not self.straight_line_by_year:
            return self.accumulated_through(year)
        return Money.sum(a for y, a in self.straight_line_by_year if y <= year)

    def additional_depreciation_through(self, year: int) -> Money:
        """Section 1250(b)(1) additional depreciation through ``year``.

        The excess of depreciation actually taken - explicitly including any
        special depreciation allowance, per Form 4797 line 26a - over what
        straight line over the same recovery period would have given.

        For a 27.5-year building this is always zero, which is why individuals
        have no ordinary recapture on buildings. For 15-year land improvements
        on 150% declining balance it is real, and if bonus was claimed it is
        very large: at 100% bonus the entire basis is deducted in year one
        while the hypothetical straight-line schedule has only reached 1/30th
        of it, so almost all of it is ordinary income on an early sale.
        """
        if self.recovery_class.recapture is not Recapture.SECTION_1250:
            return Money(0)
        excess = self.accumulated_through(year) - self.straight_line_through(year)
        return excess.clamp_at_zero()

    def exhausted_by(self) -> int:
        """Calendar year the schedule runs out - a real planning trigger. The
        year the deduction stops is often the year the client's tax picture
        changes without anything about the property changing."""
        return self.final_year

    def truncate_at_disposition(self, sold: date) -> DepreciationSchedule:
        """Re-cut the schedule for a sale partway through the recovery period.

        Under the mid-month convention the year of disposition gets
        ``(month - 0.5) / 12`` of a year. Personal property under the half-year
        convention gets half a year in the disposition year.
        """
        kept: list[tuple[int, Money]] = []
        for y, amount in self.by_year:
            if y < sold.year:
                kept.append((y, amount))
            elif y == sold.year:
                if self.recovery_class.convention is Convention.MID_MONTH:
                    # Half-months of ownership this year, out of 24.
                    half_months = (sold.month - 1) * 2 + 1
                    kept.append((y, amount.prorate(half_months, 24)))
                else:
                    kept.append((y, amount.prorate(1, 2)))
        sl_kept: list[tuple[int, Money]] = []
        for y, amount in self.straight_line_by_year:
            if y < sold.year:
                sl_kept.append((y, amount))
            elif y == sold.year:
                if self.recovery_class.convention is Convention.MID_MONTH:
                    half_months = (sold.month - 1) * 2 + 1
                    sl_kept.append((y, amount.prorate(half_months, 24)))
                else:
                    sl_kept.append((y, amount.prorate(1, 2)))
        return DepreciationSchedule(
            self.recovery_class,
            self.depreciable_basis,
            self.placed_in_service,
            tuple(kept),
            self.bonus_taken,
            tuple(sl_kept),
        )


def _first_year_half_months(placed_in_service: date) -> int:
    """Half-months of service in the first calendar year, out of 24.

    Mid-month: placed in service in month M counts ``12 - M + 0.5`` months,
    which is ``(12 - M) * 2 + 1`` half-months. January gives 23/24 of a year
    (3.485% of a 27.5-year basis); December gives 1/24 (0.152%). Those two
    figures are the first and last entries of IRS Pub 946 Table A-6, which is
    the cheapest available check that this is right.
    """
    return (12 - placed_in_service.month) * 2 + 1


def straight_line_schedule(
    depreciable_basis: Money,
    placed_in_service: date,
    recovery_class: RecoveryClass,
) -> tuple[tuple[int, Money], ...]:
    """Straight line over the recovery period, exact to the cent.

    Computed by cumulative differencing: the running total through each
    year-end is rounded once, and each year's deduction is the difference
    between consecutive totals. The schedule therefore sums to exactly
    ``depreciable_basis`` for every input, with no drift and no fudge in the
    final year.
    """
    if depreciable_basis.is_zero():
        return ()
    total_months = recovery_class.recovery_months
    # Work in half-months so the mid-month convention is exact in integers.
    total_half_months = total_months * 2

    if recovery_class.convention is Convention.MID_MONTH:
        first_chunk = _first_year_half_months(placed_in_service)
    elif recovery_class.convention is Convention.HALF_YEAR:
        first_chunk = 12  # half of 24 half-months
    else:  # mid-quarter
        quarter = (placed_in_service.month - 1) // 3
        # Midpoint of the quarter: 4.5, 3.5, 2.5 or 1.5 quarters remain.
        first_chunk = int((4 - quarter) * 6 - 3)

    schedule: list[tuple[int, Money]] = []
    year = placed_in_service.year
    consumed = 0
    previous_cumulative = Money(0)

    while consumed < total_half_months:
        chunk = first_chunk if consumed == 0 else 24
        chunk = min(chunk, total_half_months - consumed)
        consumed += chunk
        cumulative = depreciable_basis.prorate(consumed, total_half_months)
        schedule.append((year, cumulative - previous_cumulative))
        previous_cumulative = cumulative
        year += 1

    return tuple(schedule)


def declining_balance_schedule(
    depreciable_basis: Money,
    placed_in_service: date,
    recovery_class: RecoveryClass,
) -> tuple[tuple[int, Money], ...]:
    """Declining balance with the automatic switch to straight line.

    MACRS uses 200% (or 150%) declining balance and switches to straight line
    in the first year straight line over the *remaining* recovery period gives
    a larger deduction - IRC 168(b)(1)(B). The switch is not optional and not a
    heuristic; it is the point at which the two formulas cross, and computing
    it rather than hardcoding a year is what lets this handle 5-, 7- and
    15-year property with one code path.
    """
    if depreciable_basis.is_zero():
        return ()

    factor = Decimal(2) if recovery_class.method is Method.DECLINING_200 else Decimal("1.5")
    db_rate = factor / recovery_class.years

    if recovery_class.convention is Convention.HALF_YEAR:
        first_fraction = Decimal("0.5")
    else:  # mid-quarter
        quarter = (placed_in_service.month - 1) // 3
        first_fraction = (Decimal(4 - quarter) - Decimal("0.5")) / Decimal(4)

    schedule: list[tuple[int, Money]] = []
    remaining = depreciable_basis
    year = placed_in_service.year
    # The recovery period spans one more calendar year than its length,
    # because the first year is partial.
    total_years = int(recovery_class.years) + 1
    switched = False

    for index in range(total_years):
        if remaining.is_zero():
            break
        fraction = first_fraction if index == 0 else Decimal(1)
        # Years of recovery still to run, counting the current one.
        years_left = recovery_class.years - Decimal(index) + (Decimal(1) - first_fraction)

        if not switched:
            db_amount = remaining.apply_rate(db_rate * fraction)
            sl_amount = (
                remaining.apply_rate(fraction / years_left)
                if years_left > 0
                else remaining
            )
            if sl_amount >= db_amount:
                switched = True
                amount = sl_amount
            else:
                amount = db_amount
        else:
            amount = (
                remaining.apply_rate(fraction / years_left)
                if years_left > 0
                else remaining
            )

        if index == total_years - 1 or amount > remaining:
            amount = remaining  # last year mops up the remainder exactly
        schedule.append((year, amount))
        remaining = remaining - amount
        year += 1

    if remaining.is_positive() and schedule:
        last_year, last_amount = schedule[-1]
        schedule[-1] = (last_year, last_amount + remaining)

    return tuple(schedule)


def build_schedule(
    depreciable_basis: Money,
    placed_in_service: date,
    recovery_class: RecoveryClass,
    *,
    bonus_rate: Rate | None = None,
) -> DepreciationSchedule:
    """Build a full schedule, applying section 168(k) bonus first if eligible.

    Bonus depreciation comes off the top; the remaining basis is then
    depreciated normally over the recovery period. Bonus is claimed entirely in
    the placed-in-service year, which is why a cost segregation study can turn
    a profitable rental into a paper loss - and why that loss then runs
    straight into the section 469 passive activity rules in ``household.py``.
    """
    bonus = Money(0)
    basis = depreciable_basis
    if bonus_rate is not None and recovery_class.bonus_eligible and bonus_rate > 0:
        bonus = depreciable_basis.apply_rate(bonus_rate)
        basis = depreciable_basis - bonus

    if recovery_class.method is Method.STRAIGHT_LINE:
        by_year = straight_line_schedule(basis, placed_in_service, recovery_class)
    else:
        by_year = declining_balance_schedule(basis, placed_in_service, recovery_class)

    if bonus.is_positive():
        first_year = placed_in_service.year
        merged = dict(by_year)
        merged[first_year] = merged.get(first_year, Money(0)) + bonus
        by_year = tuple(sorted(merged.items()))
        bonus = Money(0)  # folded into year one; total stays equal to basis

    # The hypothetical straight-line schedule, for section 1250(b)(1). It runs
    # over the FULL depreciable basis - bonus and all - because 1250(b)(5)(A)
    # compares against straight line "for each taxable year", not against
    # straight line on a basis already reduced by bonus.
    if recovery_class.recapture is Recapture.SECTION_1250 and (
        recovery_class.method is not Method.STRAIGHT_LINE or bonus_rate
    ):
        straight_line = straight_line_schedule(
            depreciable_basis, placed_in_service, recovery_class
        )
    else:
        straight_line = by_year

    return DepreciationSchedule(
        recovery_class, depreciable_basis, placed_in_service, by_year, bonus,
        straight_line,
    )
