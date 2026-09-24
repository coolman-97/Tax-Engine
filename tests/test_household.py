"""The coupling layer. These tests encode the three effects a per-property
model structurally cannot see."""
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from taxengine.character import GainCharacter
from taxengine.household import (
    HouseholdYear,
    PassiveActivityLedger,
    RentalYear,
    compute_tax_year,
)
from taxengine.money import Money
from taxengine.rules import load
from taxengine.rules.schema import FilingStatus as FS

RULES = load(2026)
MFJ = FS.MARRIED_FILING_JOINTLY


def _rental(aid, rents, expenses, interest, depreciation, active=True):
    return RentalYear(aid, Money.from_dollars(rents), Money.from_dollars(expenses),
                      Money.from_dollars(interest), Money.from_dollars(depreciation),
                      actively_participates=active)


# --------------------------------------------------------------------------
# Section 469
# --------------------------------------------------------------------------
def test_passive_losses_are_suspended_not_deducted_at_high_income():
    """A $310k wage earner gets no 469(i) allowance at all, so every dollar of
    rental loss is suspended."""
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money.from_dollars("310000"),
                      rentals=(_rental("a", "68400", "24100", "31900", "21818"),))
    r = compute_tax_year(h, RULES)
    assert r.special_allowance_used.is_zero()
    assert r.passive_loss_suspended_this_year == Money.from_dollars("9418")
    assert r.ledger.for_activity("a") == Money.from_dollars("9418")


def test_the_25k_allowance_applies_below_the_phaseout():
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money.from_dollars("80000"),
                      rentals=(_rental("a", "30000", "12000", "18000", "20000"),))
    r = compute_tax_year(h, RULES)
    assert r.special_allowance_used == Money.from_dollars("20000")
    assert r.passive_loss_suspended_this_year.is_zero()


def test_the_allowance_phases_out_at_50_cents_on_the_dollar():
    """$1 of allowance lost per $2 of MAGI over $100,000, gone at $150,000."""
    pa = RULES.passive_activity
    assert pa.allowance_for(Money.from_dollars("100000"), MFJ) == Money.from_dollars("25000")
    assert pa.allowance_for(Money.from_dollars("120000"), MFJ) == Money.from_dollars("15000")
    assert pa.allowance_for(Money.from_dollars("150000"), MFJ) == Money.from_dollars("0")
    assert pa.allowance_for(Money.from_dollars("400000"), MFJ) == Money.from_dollars("0")


def test_no_allowance_without_active_participation():
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money.from_dollars("80000"),
                      rentals=(_rental("a", "30000", "12000", "18000", "20000", active=False),))
    assert compute_tax_year(h, RULES).special_allowance_used.is_zero()


def test_passive_income_absorbs_passive_loss_before_the_allowance():
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money.from_dollars("500000"),
                      rentals=(_rental("winner", "90000", "20000", "10000", "15000"),
                               _rental("loser", "30000", "15000", "20000", "25000")))
    r = compute_tax_year(h, RULES)
    assert r.passive_loss_allowed == Money.from_dollars("30000")
    assert r.passive_loss_suspended_this_year.is_zero()


def test_full_disposition_releases_the_entire_suspended_stack():
    """IRC 469(g). This is the effect that makes the portfolio a portfolio."""
    opening = PassiveActivityLedger({"a": Money.from_dollars("184000"),
                                     "b": Money.from_dollars("96000")})
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money.from_dollars("310000"),
                      fully_disposed_activities=("a",),
                      gains=GainCharacter(adjusted_net_capital_gain=Money.from_dollars("400000")))
    r = compute_tax_year(h, RULES, opening)
    assert r.passive_loss_released == Money.from_dollars("184000")
    assert r.ledger.for_activity("a").is_zero()
    assert r.ledger.for_activity("b") == Money.from_dollars("96000"), "other stacks untouched"


def test_released_losses_reduce_agi_dollar_for_dollar():
    opening = PassiveActivityLedger({"a": Money.from_dollars("184000")})
    gains = GainCharacter(adjusted_net_capital_gain=Money.from_dollars("400000"))
    base = {"year": 2026, "filing_status": MFJ,
            "wages": Money.from_dollars("310000"), "gains": gains}
    released = compute_tax_year(HouseholdYear(fully_disposed_activities=("a",), **base), RULES, opening)
    withheld = compute_tax_year(HouseholdYear(**base), RULES, PassiveActivityLedger())
    assert withheld.agi - released.agi == Money.from_dollars("184000")
    assert released.total_tax < withheld.total_tax


def test_an_exchange_does_not_release_suspended_losses():
    """An exchange is not a fully taxable disposition. The losses transfer to
    the replacement activity instead of being freed - getting this backwards
    invents a large deduction that does not exist."""
    ledger = PassiveActivityLedger({"relinquished": Money.from_dollars("184000")})
    moved = ledger.transferred("relinquished", "replacement")
    assert moved.for_activity("relinquished").is_zero()
    assert moved.for_activity("replacement") == Money.from_dollars("184000")
    assert moved.total() == ledger.total(), "no losses created or destroyed"


def test_real_estate_professional_deducts_losses_currently():
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money.from_dollars("310000"),
                      is_real_estate_professional=True,
                      rentals=(_rental("a", "68400", "24100", "31900", "21818"),))
    r = compute_tax_year(h, RULES)
    assert r.passive_loss_suspended_this_year.is_zero()
    assert r.passive_loss_allowed == Money.from_dollars("9418")
    assert r.niit.is_zero(), "469(c)(7) rentals are outside the NIIT"


@given(st.integers(min_value=0, max_value=500_000_00),
       st.integers(min_value=0, max_value=300_000_00))
@settings(max_examples=120, deadline=None)
def test_passive_losses_are_conserved(loss_cents, wage_cents):
    """Every dollar of loss is either allowed this year or suspended. None
    evaporate, none are counted twice."""
    h = HouseholdYear(year=2026, filing_status=MFJ, wages=Money(wage_cents),
                      rentals=(RentalYear("a", depreciation=Money(loss_cents)),))
    r = compute_tax_year(h, RULES)
    assert r.passive_loss_allowed + r.passive_loss_suspended_this_year == Money(loss_cents)


# --------------------------------------------------------------------------
# Bracket stacking and the NIIT cliff
# --------------------------------------------------------------------------
def test_capital_gain_stacks_on_ordinary_income():
    """The same gain costs different amounts depending on the rest of the
    return. A per-property calculator cannot know this."""
    gain = GainCharacter(adjusted_net_capital_gain=Money.from_dollars("200000"))
    poor = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("40000"), gains=gain), RULES)
    rich = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("700000"), gains=gain), RULES)
    assert rich.capital_gain_tax > poor.capital_gain_tax


def test_unrecaptured_1250_is_capped_at_25_percent_but_can_be_lower():
    """IRC 1(h)(1)(E) is a ceiling, not a flat rate."""
    gain = GainCharacter(unrecaptured_1250=Money.from_dollars("100000"))
    low = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("20000"), gains=gain), RULES)
    high = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("600000"), gains=gain), RULES)
    assert high.unrecaptured_1250_tax == Money.from_dollars("25000"), "hits the 25% cap"
    assert low.unrecaptured_1250_tax < Money.from_dollars("25000"), "below the cap"


def test_1245_recapture_is_ordinary_not_25_percent():
    """A cost seg study that saved tax at 37% gives it back at 37%."""
    both = GainCharacter(section_1245_ordinary=Money.from_dollars("126000"))
    r = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("600000"), gains=both), RULES)
    at_25 = Money.from_dollars("126000").apply_rate(Decimal("0.25"))
    assert r.federal_ordinary_tax - compute_tax_year(HouseholdYear(year=2026,
        filing_status=MFJ, wages=Money.from_dollars("600000")), RULES).federal_ordinary_tax > at_25


def test_niit_thresholds_are_not_indexed():
    """Frozen since 2013 by statute. Projecting them forward with inflation is
    a real modelling bug - the threshold falls in real terms every year."""
    assert load(2046).niit.thresholds[MFJ] == RULES.niit.thresholds[MFJ] == 250000
    assert load(2046).section_121.exclusion[MFJ] == 500000
    assert load(2046).ordinary[MFJ].brackets[3].up_to > RULES.ordinary[MFJ].brackets[3].up_to


def test_niit_is_the_lesser_of_nii_and_magi_excess():
    r = RULES.niit
    # NII is smaller than the excess -> NII binds.
    assert r.tax_on(Money.from_dollars("10000"), Money.from_dollars("400000"), MFJ) \
        == Money.from_dollars("380")
    # Excess is smaller -> excess binds.
    assert r.tax_on(Money.from_dollars("500000"), Money.from_dollars("260000"), MFJ) \
        == Money.from_dollars("380")
    # Below the threshold -> nothing.
    assert r.tax_on(Money.from_dollars("500000"), Money.from_dollars("200000"), MFJ).is_zero()


def test_california_taxes_capital_gain_as_ordinary_income():
    gain = GainCharacter(adjusted_net_capital_gain=Money.from_dollars("500000"))
    ca = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("300000"), gains=gain, state="CA"), RULES)
    tx = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("300000"), gains=gain, state="TX"), RULES)
    assert tx.state_tax.is_zero()
    assert ca.state_tax > Money.from_dollars("50000")


def test_section_1231_lookback_recharacterizes_gain_as_ordinary():
    """IRC 1231(c): five-year lookback. Most models ignore it entirely."""
    gain = GainCharacter(adjusted_net_capital_gain=Money.from_dollars("200000"))
    plain = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("400000"), gains=gain), RULES)
    looked_back = compute_tax_year(HouseholdYear(year=2026, filing_status=MFJ,
        wages=Money.from_dollars("400000"), gains=gain,
        prior_year_1231_losses=Money.from_dollars("60000")), RULES)
    assert looked_back.capital_gain_taxable < plain.capital_gain_taxable
    assert looked_back.total_tax > plain.total_tax, "ordinary rates cost more"
