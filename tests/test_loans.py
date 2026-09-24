"""Loans, including the four instruments that most reliably break a schedule."""
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from taxengine.loans import (
    PMI,
    FixedRate,
    Lien,
    LienStack,
    Loan,
    PaymentRule,
    Phase,
    adjustable_rate_mortgage,
    fixed_rate_mortgage,
    heloc,
    interest_only_then_amortizing,
)
from taxengine.money import Money, rate

principal = st.integers(min_value=10_000_00, max_value=5_000_000_00)
annual = st.sampled_from(["0.02", "0.0325", "0.045", "0.065", "0.07875", "0.095", "0.12"])
terms = st.sampled_from([10, 15, 20, 30, 40])


@given(principal, annual, terms)
@settings(max_examples=120, deadline=None)
def test_every_amortizing_loan_retires_to_zero(cents, r, years):
    """The regression test for edge case #1.

    A payment rounded to nearest leaves a stub balance at maturity - a $400k
    30-year note at 6.5% still owed $2.61 in month 360. Servicers round the
    payment up. This asserts it for every combination.
    """
    loan = fixed_rate_mortgage("t", Money(cents), date(2020, 1, 1), rate(r), years)
    schedule = loan.schedule()
    assert schedule[-1].ending_balance.is_zero()
    assert Money.sum(p.principal for p in schedule) == Money(cents)


@given(principal, annual)
@settings(max_examples=80, deadline=None)
def test_interest_only_defers_every_dollar_of_principal(cents, r):
    loan = interest_only_then_amortizing("io", Money(cents), date(2018, 1, 1), rate(r), 10, 30)
    schedule = loan.schedule()
    assert schedule[119].ending_balance == Money(cents), "IO period repaid principal"
    assert schedule[-1].ending_balance.is_zero()


def test_interest_only_conversion_is_a_payment_shock():
    loan = interest_only_then_amortizing(
        "io", Money.from_dollars("750000"), date(2017, 7, 1), rate("0.0425"), 10, 30
    )
    s = loan.schedule()
    assert s[120].payment > s[119].payment
    jump = s[120].payment.ratio_to(s[119].payment)
    assert jump > Decimal("1.5"), "conversion should roughly double the payment"
    assert s[120].on == date(2027, 7, 1)


def test_arm_respects_all_three_caps():
    """2/2/5 means three different reference points: the first adjustment is
    capped against the initial rate, later ones against the previous rate, and
    everything against initial + lifetime."""
    spike = [Decimal("0.015")] * 60 + [Decimal("0.25")] * 300  # absurd index
    loan = adjustable_rate_mortgage(
        "arm", Money.from_dollars("600000"), date(2021, 4, 1),
        rate("0.0325"), rate("0.0275"), spike,
    )
    resets = loan.resets()
    assert resets[0][2] == Decimal("0.0325") + Decimal("0.02"), "initial cap"
    assert resets[1][2] == resets[0][2] + Decimal("0.02"), "periodic cap"
    ceiling = Decimal("0.0325") + Decimal("0.05")
    assert all(to <= ceiling for _, _, to in resets), "lifetime cap"
    assert loan.schedule()[-1].ending_balance.is_zero()


def test_arm_floor_holds_when_the_index_collapses():
    crash = [Decimal("0.03")] * 60 + [Decimal("0.0")] * 300
    loan = adjustable_rate_mortgage(
        "arm", Money.from_dollars("400000"), date(2020, 1, 1),
        rate("0.045"), rate("0.0275"), crash,
    )
    assert all(to >= Decimal("0.0275") for _, _, to in loan.resets()), "floor is the margin"


def test_heloc_starts_empty_and_tracks_draws():
    line = heloc(
        "heloc", Money.from_dollars("200000"), date(2022, 1, 1), FixedRate(rate("0.085")),
        draws=[(0, Money.from_dollars("80000")), (18, Money.from_dollars("45000"))],
    )
    s = line.schedule()
    assert line.original_principal.is_zero(), "a line of credit has no opening principal"
    assert s[0].starting_balance == Money.from_dollars("80000")
    assert s[18].starting_balance > s[17].ending_balance, "second draw increased the balance"


def test_heloc_respects_its_credit_limit():
    line = heloc(
        "heloc", Money.from_dollars("100000"), date(2022, 1, 1), FixedRate(rate("0.08")),
        draws=[(0, Money.from_dollars("250000"))],
    )
    assert line.schedule()[0].starting_balance <= Money.from_dollars("100000")


def test_heloc_interest_is_only_deductible_to_the_traced_extent():
    """Temp. Reg. 1.163-8T: interest follows the use of the proceeds, not the
    collateral. A HELOC on a rental spent on a boat is not rental interest."""
    line = heloc(
        "heloc", Money.from_dollars("200000"), date(2022, 1, 1), FixedRate(rate("0.085")),
        draws=[(0, Money.from_dollars("100000"))],
        interest_deductible_fraction=rate("0.60"),
    )
    total = line.interest_in_year(2023)
    assert line.deductible_interest_in_year(2023) == total.apply_rate(rate("0.60"))
    assert line.deductible_interest_in_year(2023) < total


def test_negative_amortization_is_representable():
    loan = Loan(
        "neg-am", Money.from_dollars("300000"), date(2021, 1, 1), FixedRate(rate("0.09")),
        (Phase(24, PaymentRule.MINIMUM_PERCENT, minimum_percent=Decimal("0.001")),
         Phase(336, PaymentRule.AMORTIZE, amortization_term_months=336)),
    )
    s = loan.schedule()
    # A payment below accrued interest must grow the balance, not silently floor.
    assert any(p.negatively_amortized for p in s[:24]) or s[23].ending_balance >= s[0].starting_balance


def test_balloon_pays_the_whole_balance_at_the_end():
    loan = Loan(
        "balloon", Money.from_dollars("500000"), date(2022, 1, 1), FixedRate(rate("0.07")),
        (Phase(84, PaymentRule.AMORTIZE, amortization_term_months=360, balloon_at_end=True),),
    )
    s = loan.schedule()
    assert s[-1].ending_balance.is_zero()
    assert s[-1].payment > s[-2].payment * 10


def test_pmi_terminates_on_the_scheduled_balance_not_market_value():
    """The Homeowners Protection Act terminates PMI at 78% of ORIGINAL value on
    the amortisation schedule. Appreciation does not cancel PMI automatically."""
    value = Money.from_dollars("500000")
    loan = fixed_rate_mortgage(
        "pmi", Money.from_dollars("475000"), date(2020, 1, 1), rate("0.05"), 30,
        pmi=PMI(annual_rate=rate("0.0085"), original_value=value),
    )
    s = loan.schedule()
    assert s[0].pmi.is_positive()
    threshold = value.apply_rate(rate("0.78"))
    dropped = [p for p in s if p.pmi.is_zero() and p.ending_balance <= threshold]
    assert dropped, "PMI never terminated"
    assert all(p.pmi.is_zero() for p in s if p.ending_balance <= threshold)


def test_prepayment_penalty_applies_only_inside_the_window():
    loan = fixed_rate_mortgage("pp", Money.from_dollars("400000"), date(2023, 1, 1), rate("0.07"))
    loan = Loan(
        loan.name, loan.original_principal, loan.origination, loan.rate_path, loan.phases,
        prepayment_penalty_months=36, prepayment_penalty_rate=Decimal("0.02"),
    )
    inside = loan.payoff_cost(date(2024, 6, 1))
    outside = loan.payoff_cost(date(2027, 6, 1))
    assert inside > loan.balance_on(date(2024, 6, 1))
    assert outside == loan.balance_on(date(2027, 6, 1))


# --------------------------------------------------------------------------
# A property carrying four paid-off liens.
# --------------------------------------------------------------------------
def _four_paid_off():
    first = fixed_rate_mortgage("1st", Money.from_dollars("310000"), date(2018, 5, 1), rate("0.0475"))
    return LienStack((
        Lien(1, "purchase money first", first),
        Lien(2, "paid-off HELOC, never reconveyed", None, release_cost=Money.from_dollars("175")),
        Lien(3, "satisfied mechanics lien", None, release_cost=Money.from_dollars("450")),
        Lien(4, "solar UCC-1, paid", None, release_cost=Money.from_dollars("125")),
        Lien(5, "county abatement lien, paid", None, release_cost=Money.from_dollars("310")),
    ))


def test_paid_off_liens_still_have_to_be_cleared():
    """The bug this guards: filtering the lien stack on `balance > 0` drops
    zero-balance liens, which still cloud title and still cost money to
    release."""
    stack = _four_paid_off()
    when = date(2026, 6, 1)
    assert stack.total_debt(when) > Money(0)
    assert len(stack.encumbrances_to_clear(when)) == 5
    assert stack.clearing_costs(when) == Money.from_dollars("1060")


def test_paid_off_liens_push_new_borrowing_down_the_stack():
    """A paid-off but unreleased second still holds position 2, so new
    borrowing is a sixth lien, priced accordingly - not a second."""
    assert _four_paid_off().next_available_position() == 6


def test_payoff_waterfall_respects_seniority():
    stack = _four_paid_off()
    when = date(2026, 6, 1)
    payments, remainder = stack.payoff_waterfall(Money.from_dollars("100000"), when)
    assert payments[0][0].position == 1
    assert payments[0][1] == Money.from_dollars("100000"), "senior lien takes it all"
    assert all(p.is_zero() for _, p in payments[1:])
    assert remainder.is_zero()


def test_payoff_waterfall_conserves_every_cent():
    stack = _four_paid_off()
    when = date(2026, 6, 1)
    proceeds = Money.from_dollars("500000")
    payments, remainder = stack.payoff_waterfall(proceeds, when)
    assert Money.sum(p for _, p in payments) + remainder == proceeds


def test_lien_stack_rejects_duplicate_positions():
    with pytest.raises(ValueError):
        LienStack((Lien(1, "a"), Lien(1, "b")))
