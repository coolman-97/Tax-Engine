"""Loans, including the ones that break naive amortisation schedules.

Four instruments reliably break a naive amortisation schedule: interest-only
loans, ARMs, HELOCs, and a property carrying four paid-off liens. All four are
here, plus balloons, negative amortisation, prepayment penalties and PMI.

The design that makes them one code path instead of five: **a loan is a
sequence of phases**, and each phase supplies two things - how the rate is
determined this month, and how the payment is determined this month. An
interest-only loan converting to amortising is two phases. An ARM is a fixed
phase followed by adjusting phases. A HELOC is a draw phase followed by a
repayment phase. Nothing special-cases anything.

The lien stack is modelled separately and deliberately keeps zero-balance
liens. A paid-off lien that was never reconveyed still occupies its position,
still has to be cleared at closing, and still pushes any new borrowing further
down the stack. Filtering on ``balance > 0`` is exactly the bug that makes a
system quietly report the wrong lendable equity.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import Enum

from .money import Money, Rate

__all__ = [
    "PaymentRule",
    "Phase",
    "RatePath",
    "FixedRate",
    "IndexedRate",
    "Loan",
    "Period",
    "Lien",
    "LienStack",
    "fixed_rate_mortgage",
    "interest_only_then_amortizing",
    "adjustable_rate_mortgage",
    "heloc",
    "PMI",
]

_MONTHS = 12


class PaymentRule(Enum):
    INTEREST_ONLY = "interest-only"
    AMORTIZE = "amortize"
    """Recast: pay off the current balance over the phase's remaining term."""
    MINIMUM_PERCENT = "minimum-percent"
    """HELOC draw-period minimum, e.g. 1% of balance or interest, whichever is
    greater. Can amortise slower than interest accrues."""
    NONE = "none"
    """Accrue only - used for negative amortisation and deferred periods."""


# ----------------------------------------------------------------------
# Rate paths
# ----------------------------------------------------------------------
class RatePath:
    """Supplies the annual note rate for a given month index."""

    def rate_at(self, month: int) -> Rate:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass(frozen=True)
class FixedRate(RatePath):
    annual: Rate

    def rate_at(self, month: int) -> Rate:
        return self.annual


@dataclass(frozen=True)
class IndexedRate(RatePath):
    """An adjustable rate: index plus margin, bounded by caps and a floor.

    The cap structure is the part that gets mis-implemented. A 5/1 ARM quoted
    "2/2/5" means: the first adjustment may move at most 2 points from the
    initial rate, each later adjustment at most 2 points from the *previous*
    rate, and the rate may never exceed the initial rate plus 5. Those are
    three different reference points, and using the initial rate for all three
    understates the rate an ARM can reach.
    """

    initial: Rate
    margin: Rate
    index_path: Sequence[Rate]
    """Annual index value by month index. Deterministic here; the stochastic
    module supplies simulated paths with the same shape."""

    fixed_months: int = 60
    adjust_every: int = 12
    initial_cap: Rate = Decimal("0.02")
    periodic_cap: Rate = Decimal("0.02")
    lifetime_cap: Rate = Decimal("0.05")
    floor: Rate | None = None

    def rate_at(self, month: int) -> Rate:
        if month < self.fixed_months:
            return self.initial

        ceiling = self.initial + self.lifetime_cap
        floor = self.floor if self.floor is not None else self.margin
        current = self.initial
        previous = self.initial

        adjustment = 0
        m = self.fixed_months
        while m <= month:
            index_value = (
                self.index_path[m] if m < len(self.index_path) else self.index_path[-1]
            )
            target = index_value + self.margin
            cap = self.initial_cap if adjustment == 0 else self.periodic_cap
            low, high = previous - cap, previous + cap
            current = max(low, min(high, target))
            current = max(floor, min(ceiling, current))
            previous = current
            adjustment += 1
            m += self.adjust_every
        return current


# ----------------------------------------------------------------------
# Phases and loans
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Phase:
    """A stretch of months sharing one payment rule."""

    months: int
    payment_rule: PaymentRule
    amortization_term_months: int | None = None
    """For AMORTIZE: the term the payment is sized against. A 30-year loan with
    a 10-year interest-only period amortises over the remaining 240 months, not
    360 - which is why the payment jumps so hard at conversion."""
    minimum_percent: Rate | None = None
    balloon_at_end: bool = False


@dataclass(frozen=True)
class Period:
    """One month of a loan's life."""

    month: int
    on: date
    starting_balance: Money
    note_rate: Rate
    payment: Money
    interest: Money
    principal: Money
    ending_balance: Money
    pmi: Money = Money(0)
    draw: Money = Money(0)

    @property
    def negatively_amortized(self) -> bool:
        return self.ending_balance > self.starting_balance


@dataclass(frozen=True)
class PMI:
    """Private mortgage insurance, with its automatic termination rule.

    PMI must terminate automatically at 78% of *original* value on the
    amortisation schedule (Homeowners Protection Act). Engines that drop it
    based on current market value get the timing wrong, because appreciation
    does not automatically cancel PMI - the scheduled balance does.
    """

    annual_rate: Rate
    original_value: Money
    auto_terminate_ltv: Rate = Decimal("0.78")

    def monthly_for(self, scheduled_balance: Money) -> Money:
        threshold = self.original_value.apply_rate(self.auto_terminate_ltv)
        if scheduled_balance <= threshold:
            return Money(0)
        return scheduled_balance.apply_rate(self.annual_rate / Decimal(_MONTHS))


@dataclass
class Loan:
    """A loan defined as a rate path plus a sequence of payment phases."""

    name: str
    original_principal: Money
    origination: date
    rate_path: RatePath
    phases: tuple[Phase, ...]
    pmi: PMI | None = None
    prepayment_penalty_months: int = 0
    prepayment_penalty_rate: Rate = Decimal("0")
    credit_limit: Money | None = None
    draws: tuple[tuple[int, Money], ...] = ()
    """(month index, amount) draws against a revolving line."""
    interest_deductible_fraction: Rate = Decimal(1)
    """Tracing rules: HELOC interest is only deductible against the rental to
    the extent the proceeds were used for it. IRC 163(h) and Temp. Reg.
    1.163-8T. Defaults to fully deductible for acquisition debt."""

    def _monthly_rate(self, month: int) -> Rate:
        return self.rate_path.rate_at(month) / Decimal(_MONTHS)

    @staticmethod
    def _level_payment(balance: Money, monthly_rate: Rate, n: int) -> Money:
        """Standard annuity payment, rounded **up** to the cent.

        The rounding direction is not cosmetic. Rounding to nearest leaves a
        stub balance at maturity: a $400,000 30-year note at 6.5% rounded
        half-up still owes $2.61 in month 360, because the payment is $2,528.27
        when retiring the note exactly needs $2,528.2673. Servicers round the
        payment up for precisely this reason, and the loan then retires a hair
        early with a small final payment instead of never quite retiring.

        This engine got that wrong on the first pass; ``test_loans.py``
        asserts every amortising loan reaches a zero balance, which is what
        caught it. See docs/EDGE_CASES.md #1.
        """
        if n <= 0:
            return balance
        if monthly_rate == 0:
            return Money(-(-balance.cents // n))
        growth = (Decimal(1) + monthly_rate) ** n
        factor = monthly_rate * growth / (growth - Decimal(1))
        return balance.apply_rate(factor, rounding=ROUND_CEILING)

    def schedule(self, months: int | None = None) -> list[Period]:
        """Walk the loan month by month.

        Memoised for the full walk. A loan's terms do not change after
        construction, but ``interest_in_year``, ``principal_paid_in_year``,
        ``balance_on``, ``resets`` and ``payment_shocks`` all need the schedule,
        and a thirty-year simulation asks for it thousands of times. Without the
        cache the demo scenario spent 2.6 seconds re-amortising the same four
        loans; with it, 30 milliseconds.
        """
        if months is None:
            cached = getattr(self, "_schedule_cache", None)
            if cached is not None:
                return cached
            built = self._build_schedule(None)
            object.__setattr__(self, "_schedule_cache", built)
            return built
        return self._build_schedule(months)

    def _build_schedule(self, months: int | None) -> list[Period]:
        draws = dict(self.draws)
        balance = self.original_principal
        scheduled_balance = self.original_principal
        periods: list[Period] = []
        month = 0
        limit = months if months is not None else sum(p.months for p in self.phases)

        for phase_index, phase in enumerate(self.phases):
            payment: Money | None = None
            last_rate: Rate | None = None
            is_last_phase = phase_index == len(self.phases) - 1

            for i in range(phase.months):
                if month >= limit:
                    return periods
                if balance.is_zero() and not draws.get(month):
                    month += 1
                    continue

                monthly_rate = self._monthly_rate(month)
                starting = balance

                drawn = draws.get(month, Money(0))
                if drawn.is_positive():
                    if self.credit_limit is not None:
                        headroom = self.credit_limit - balance
                        drawn = min(drawn, headroom.clamp_at_zero())
                    balance = balance + drawn
                    starting = balance

                interest = balance.apply_rate(monthly_rate)

                # Recast the payment whenever the rate moves or a phase starts.
                if phase.payment_rule is PaymentRule.AMORTIZE:
                    if payment is None or monthly_rate != last_rate:
                        remaining = (
                            phase.amortization_term_months
                            if phase.amortization_term_months is not None
                            else phase.months
                        ) - i
                        payment = self._level_payment(balance, monthly_rate, remaining)
                        last_rate = monthly_rate
                    due = payment
                elif phase.payment_rule is PaymentRule.INTEREST_ONLY:
                    due = interest
                elif phase.payment_rule is PaymentRule.MINIMUM_PERCENT:
                    pct = phase.minimum_percent or Decimal("0.01")
                    due = max(balance.apply_rate(pct), interest)
                else:
                    due = Money(0)

                # The final scheduled payment is a payoff payment.
                #
                # Rounding the level payment up is not on its own enough to
                # retire every loan: interest is also rounded to the cent each
                # month, and on small balances those roundings can eat the
                # slack. Hypothesis found a $13,164.73 note at 3.25% that still
                # owed $0.01 in month 360. Servicers send a payoff quote for
                # the last payment rather than the level amount, which is both
                # what really happens and what makes the invariant hold for
                # every input. See docs/EDGE_CASES.md #3.
                is_final_payment = is_last_phase and i == phase.months - 1
                if (phase.balloon_at_end and i == phase.months - 1) or (
                    is_final_payment and phase.payment_rule is PaymentRule.AMORTIZE
                ):
                    due = balance + interest

                principal = due - interest
                if principal > balance:
                    principal = balance
                    due = balance + interest

                ending = balance - principal

                pmi_amount = Money(0)
                if self.pmi is not None:
                    scheduled_balance = min(scheduled_balance, ending)
                    pmi_amount = self.pmi.monthly_for(scheduled_balance)

                on = _add_months(self.origination, month)
                periods.append(
                    Period(month, on, starting, self.rate_path.rate_at(month),
                           due, interest, principal, ending, pmi_amount, drawn)
                )
                balance = ending
                month += 1

        return periods

    # ------------------------------------------------------------------
    def balance_on(self, when: date) -> Money:
        months = _months_between(self.origination, when)
        periods = self.schedule()
        if months <= 0:
            return self.original_principal
        for p in periods:
            if p.month == months - 1:
                return p.ending_balance
        return periods[-1].ending_balance if periods else Money(0)

    def interest_in_year(self, year: int) -> Money:
        return Money.sum(p.interest for p in self.schedule() if p.on.year == year)

    def deductible_interest_in_year(self, year: int) -> Money:
        """Interest allocable to the rental activity under the tracing rules."""
        return self.interest_in_year(year).apply_rate(self.interest_deductible_fraction)

    def principal_paid_in_year(self, year: int) -> Money:
        return Money.sum(p.principal for p in self.schedule() if p.on.year == year)

    def payoff_cost(self, when: date) -> Money:
        """Balance plus any prepayment penalty still in force."""
        balance = self.balance_on(when)
        months = _months_between(self.origination, when)
        if months < self.prepayment_penalty_months and self.prepayment_penalty_rate > 0:
            return balance + balance.apply_rate(self.prepayment_penalty_rate)
        return balance

    def payment_shocks(self, threshold: Rate = Decimal("0.15")) -> list[tuple[date, Money, Money]]:
        """Months where the required payment jumps by more than ``threshold``.

        Catches the interest-only conversion and any ARM recast in one place.
        A client who has paid the same amount for ten years does not
        spontaneously discover that it doubles next July; the software should
        say so years ahead.
        """
        out: list[tuple[date, Money, Money]] = []
        previous: Money | None = None
        for p in self.schedule():
            if (
                previous is not None
                and previous.is_positive()
                and p.payment > previous
                and p.payment.ratio_to(previous) - Decimal(1) > threshold
            ):
                out.append((p.on, previous, p.payment))
            previous = p.payment
        return out

    def resets(self) -> list[tuple[date, Rate, Rate]]:
        """Months where the note rate changes: (date, from, to).

        These are planning triggers. An advisor wants to hear about a reset
        before the client does.
        """
        out: list[tuple[date, Rate, Rate]] = []
        previous: Rate | None = None
        for p in self.schedule():
            if previous is not None and p.note_rate != previous:
                out.append((p.on, previous, p.note_rate))
            previous = p.note_rate
        return out


# ----------------------------------------------------------------------
# Liens
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Lien:
    """A recorded encumbrance. May have a zero balance and still matter."""

    position: int
    description: str
    loan: Loan | None = None
    recorded: date | None = None
    released: bool = False
    """A paid-off lien that was never reconveyed is ``released=False`` with a
    zero balance. It still has to be cleared at closing."""
    release_cost: Money = Money(0)

    def balance_on(self, when: date) -> Money:
        return self.loan.balance_on(when) if self.loan is not None else Money(0)

    def is_encumbering(self, when: date) -> bool:
        """True if this lien must be dealt with at closing - which includes
        zero-balance liens that were never released."""
        return not self.released and (
            self.balance_on(when).is_positive() or self.loan is None or True
        )


@dataclass(frozen=True)
class LienStack:
    """All liens against a property, in seniority order."""

    liens: tuple[Lien, ...]

    def __post_init__(self) -> None:
        positions = [lien.position for lien in self.liens]
        if len(set(positions)) != len(positions):
            raise ValueError(f"duplicate lien positions: {sorted(positions)}")

    def ordered(self) -> tuple[Lien, ...]:
        return tuple(sorted(self.liens, key=lambda lien: lien.position))

    def total_debt(self, when: date) -> Money:
        return Money.sum(lien.balance_on(when) for lien in self.liens)

    def clearing_costs(self, when: date) -> Money:
        """Reconveyance and release fees for liens still on title.

        Small dollars, but a sale cannot close without them, and a zero-balance
        lien is precisely the one a spreadsheet forgets."""
        return Money.sum(
            lien.release_cost for lien in self.liens if not lien.released
        )

    def encumbrances_to_clear(self, when: date) -> tuple[Lien, ...]:
        return tuple(lien for lien in self.ordered() if not lien.released)

    def next_available_position(self) -> int:
        """Where new borrowing would sit.

        A paid-off but unreleased second still holds position 2, so new
        borrowing is a third, not a second - priced accordingly.
        """
        taken = {lien.position for lien in self.liens if not lien.released}
        position = 1
        while position in taken:
            position += 1
        return position

    def payoff_waterfall(
        self, proceeds: Money, when: date
    ) -> tuple[list[tuple[Lien, Money]], Money]:
        """Distribute proceeds by seniority. Returns (payments, remainder)."""
        remaining = proceeds
        payments: list[tuple[Lien, Money]] = []
        for lien in self.ordered():
            owed = lien.loan.payoff_cost(when) if lien.loan is not None else Money(0)
            owed = owed + lien.release_cost if not lien.released else owed
            paid = min(owed, remaining.clamp_at_zero())
            payments.append((lien, paid))
            remaining = remaining - paid
        return payments, remaining

    def lendable_equity(
        self, value: Money, when: date, max_ltv: Rate = Decimal("0.70")
    ) -> Money:
        """Value at ``max_ltv`` less all existing debt.

        The figure a lender would actually advance against, which is the one
        worth planning on - raw equity overstates what a client can reach."""
        return (value.apply_rate(max_ltv) - self.total_debt(when)).clamp_at_zero()


# ----------------------------------------------------------------------
# Constructors for the common shapes
# ----------------------------------------------------------------------
def fixed_rate_mortgage(
    name: str, principal: Money, origination: date, annual_rate: Rate,
    term_years: int = 30, *, pmi: PMI | None = None,
) -> Loan:
    n = term_years * _MONTHS
    return Loan(name, principal, origination, FixedRate(annual_rate),
                (Phase(n, PaymentRule.AMORTIZE, amortization_term_months=n),), pmi=pmi)


def interest_only_then_amortizing(
    name: str, principal: Money, origination: date, annual_rate: Rate,
    io_years: int = 10, term_years: int = 30,
) -> Loan:
    """The payment shock case. Ten years of interest-only on a 30-year note
    leaves 20 years to retire the whole principal, so the payment steps up hard
    on a date the client has usually forgotten about."""
    io = io_years * _MONTHS
    remaining = (term_years - io_years) * _MONTHS
    return Loan(name, principal, origination, FixedRate(annual_rate), (
        Phase(io, PaymentRule.INTEREST_ONLY),
        Phase(remaining, PaymentRule.AMORTIZE, amortization_term_months=remaining),
    ))


def adjustable_rate_mortgage(
    name: str, principal: Money, origination: date, initial_rate: Rate,
    margin: Rate, index_path: Sequence[Rate], *, fixed_years: int = 5,
    term_years: int = 30, initial_cap: Rate = Decimal("0.02"),
    periodic_cap: Rate = Decimal("0.02"), lifetime_cap: Rate = Decimal("0.05"),
) -> Loan:
    n = term_years * _MONTHS
    path = IndexedRate(initial_rate, margin, index_path, fixed_years * _MONTHS, 12,
                       initial_cap, periodic_cap, lifetime_cap)
    return Loan(name, principal, origination, path,
                (Phase(n, PaymentRule.AMORTIZE, amortization_term_months=n),))


def heloc(
    name: str, credit_limit: Money, origination: date, rate_path: RatePath,
    *, draw_years: int = 10, repay_years: int = 20,
    draws: Sequence[tuple[int, Money]] = (),
    minimum_percent: Rate = Decimal("0.01"),
    interest_deductible_fraction: Rate = Decimal(1),
) -> Loan:
    """A revolving line: interest-only-ish draw period, then it amortises.

    Opening balance is zero and grows with draws, which is why a HELOC cannot
    be modelled as a loan with a principal."""
    draw_months = draw_years * _MONTHS
    repay_months = repay_years * _MONTHS
    return Loan(
        name, Money(0), origination, rate_path,
        (Phase(draw_months, PaymentRule.MINIMUM_PERCENT, minimum_percent=minimum_percent),
         Phase(repay_months, PaymentRule.AMORTIZE, amortization_term_months=repay_months)),
        credit_limit=credit_limit, draws=tuple(draws),
        interest_deductible_fraction=interest_deductible_fraction,
    )


# ----------------------------------------------------------------------
def _add_months(start: date, months: int) -> date:
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, [31, 29 if _leap(year) else 28, 31, 30, 31, 30,
                          31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _months_between(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)
