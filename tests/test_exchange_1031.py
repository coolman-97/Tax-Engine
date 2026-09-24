"""Section 1031. The invariant that has to hold no matter what: every dollar
of realised gain is either recognised now or deferred - never lost, never
double counted."""
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st
from standbehind.dispositions import (
    ExchangeTerms,
    SaleTerms,
    compute_exchange,
    compute_sale,
)
from standbehind.loans import Lien, LienStack, fixed_rate_mortgage
from standbehind.money import Money, rate
from standbehind.property import Property, PropertyKind
from standbehind.rules import load

RULES = load(2026)


def _property(debt="410000", state="CA"):
    liens = ()
    if debt:
        liens = (Lien(1, "first", fixed_rate_mortgage(
            "1st", Money.from_dollars(debt), date(2009, 6, 1), rate("0.055"))),)
    return Property(
        id="p", address="1842 Casitas Ave", kind=PropertyKind.RESIDENTIAL_RENTAL,
        acquired=date(2009, 6, 1), placed_in_service=date(2009, 6, 15),
        purchase_price=Money.from_dollars("585000"),
        land_allocation=Money.from_dollars("205000"),
        capitalized_closing_costs=Money.from_dollars("11800"),
        liens=LienStack(liens), state=state,
    )


prices = st.integers(min_value=600_000_00, max_value=4_000_000_00)
debts = st.integers(min_value=0, max_value=2_000_000_00)


@given(prices, prices, debts)
@settings(max_examples=200, deadline=None)
def test_deferred_plus_recognized_equals_realized(relinquished, replacement, new_debt):
    """The conservation law of section 1031."""
    result = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money(relinquished),
        replacement_price=Money(replacement),
        replacement_debt=Money(new_debt),
    ), RULES)
    assert result.recognized_gain + result.deferred_gain == result.realized_gain


@given(prices, prices, debts)
@settings(max_examples=150, deadline=None)
def test_recognized_gain_never_exceeds_boot_or_realized_gain(rel, rep, debt):
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15), relinquished_price=Money(rel),
        replacement_price=Money(rep), replacement_debt=Money(debt)), RULES)
    assert r.recognized_gain <= r.total_boot
    assert r.recognized_gain <= r.realized_gain
    assert not r.recognized_gain.is_negative()


@given(prices, prices, debts)
@settings(max_examples=150, deadline=None)
def test_character_of_recognized_gain_sums_to_recognized(rel, rep, debt):
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15), relinquished_price=Money(rel),
        replacement_price=Money(rep), replacement_debt=Money(debt)), RULES)
    assert r.character.recognized == r.recognized_gain


def test_a_full_trade_up_defers_everything():
    """Full deferral needs BOTH conditions: all equity reinvested and all debt
    replaced. Note how tight this is - the relinquished equity is $1,080,356
    and the replacement needs $1,100,000 of cash, so the client writes a cheque
    for the $19,644 difference and defers everything. Dial the replacement debt
    up to $720,000 instead and $356 of equity is left over, which is boot, and
    the exchange is no longer fully deferred. There is no rounding slack here.
    """
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        replacement_debt=Money.from_dollars("700000")), RULES)
    assert r.cash_boot.is_zero() and r.mortgage_boot.is_zero()
    assert r.fully_deferred
    assert r.deferred_gain == r.realized_gain


def test_a_sliver_of_unreinvested_equity_is_still_boot():
    """$356 of leftover equity is taxable. Exchanges are all-or-nothing at the
    margin, which is why the replacement has to be sized deliberately."""
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        replacement_debt=Money.from_dollars("720000")), RULES)
    assert r.cash_boot == Money.from_dollars("356.15")
    assert not r.fully_deferred


def test_trading_down_in_debt_creates_taxable_boot_with_no_cash():
    """The one clients never see coming: no cash changes hands, tax is due."""
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1100000"),
        replacement_debt=Money.from_dollars("0")), RULES)
    assert r.cash_boot.is_zero()
    assert r.mortgage_boot.is_positive()
    assert r.recognized_gain.is_positive()


def test_cash_brought_into_the_exchange_offsets_mortgage_boot():
    """Regression for edge case #2 - Reg. 1.1031(b)-1(c).

    Debt relief of $254,443.85 with $19,643.85 of new cash is $234,800 of
    boot, not $254,443.85. Deriving the cash from the deal structure is what
    makes this right without asking the user for it.
    """
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1100000"),
        replacement_debt=Money.from_dollars("0")), RULES)
    assert r.mortgage_boot == Money.from_dollars("234800.00")


def test_recapture_is_recognized_before_capital_gain():
    """Boot pulls the most expensive slice first, not a proportional share."""
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1100000"),
        replacement_debt=Money.from_dollars("0")), RULES)
    assert r.character.unrecaptured_1250.is_positive()
    assert r.character.adjusted_net_capital_gain.is_zero()


def test_replacement_basis_carries_the_deferred_gain_forward():
    """The tax is not forgiven, it is baked into a lower basis."""
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        replacement_debt=Money.from_dollars("720000")), RULES)
    assert r.replacement_basis == Money.from_dollars("1800000") - r.deferred_gain


def test_california_clawback_fires_on_an_out_of_state_replacement():
    r = compute_exchange(_property(state="CA"), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        replacement_debt=Money.from_dollars("720000"),
        replacement_state="TX"), RULES)
    assert r.california_clawback
    assert r.clawback_form == "FTB 3840"
    assert any("3840" in w for w in r.warnings)


def test_no_clawback_when_staying_in_state():
    r = compute_exchange(_property(state="CA"), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        replacement_debt=Money.from_dollars("720000"),
        replacement_state="CA"), RULES)
    assert not r.california_clawback


def test_deadlines_are_45_and_180_days():
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000")), RULES)
    assert (r.identification_deadline - date(2026, 10, 15)).days == 45
    assert (r.closing_deadline - date(2026, 10, 15)).days == 180


def test_missing_the_180_day_deadline_is_flagged_loudly():
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        replacement_on=date(2027, 5, 1)), RULES)
    assert any("180-day" in w for w in r.warnings)


def test_related_party_two_year_rule_is_flagged():
    r = compute_exchange(_property(), ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000"),
        is_related_party=True), RULES)
    assert r.related_party_holding_period_ends == date(2028, 10, 15)
    assert any("1031(f)" in w for w in r.warnings)


def test_exchange_and_sale_realize_the_same_gain():
    """Same property, same price - the gain is identical. Only its treatment
    differs. If these diverge, one of the two is computing basis wrong."""
    p = _property()
    sale = compute_sale(p, SaleTerms(date(2026, 10, 15), Money.from_dollars("1420000")), RULES)
    ex = compute_exchange(p, ExchangeTerms(
        relinquished_on=date(2026, 10, 15),
        relinquished_price=Money.from_dollars("1420000"),
        replacement_price=Money.from_dollars("1800000")), RULES)
    assert sale.total_gain == ex.realized_gain
    assert sale.adjusted_basis == ex.adjusted_basis
