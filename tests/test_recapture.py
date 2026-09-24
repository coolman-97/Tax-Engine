"""Recapture classification - the rule that separates a real engine from a
calculator.

The trap: a cost segregation study hands you 5-, 7- and 15-year buckets, and
it is natural to treat all three as section 1245 "personal property". The
15-year bucket is land improvements, and Reg. 1.48-1(c) excludes "land and
improvements thereto" from tangible personal property, so it falls to section
1250(c) instead.

That matters because section 1250 only recaptures *additional* depreciation -
the excess over hypothetical straight line. For a building that excess is
zero. For 15-year land improvements on 150% declining balance it is real, and
once bonus depreciation is claimed it is most of the basis.
"""
from datetime import date

from taxengine.depreciation import (
    FIFTEEN_YEAR,
    FIVE_YEAR,
    NONRESIDENTIAL_REAL,
    RESIDENTIAL_RENTAL,
    SEVEN_YEAR,
    Recapture,
    build_schedule,
)
from taxengine.dispositions import SaleTerms, compute_sale
from taxengine.fixtures import build_household
from taxengine.money import Money, rate
from taxengine.rules import load

RULES = load(2026)
M = Money.from_dollars


def test_classification_matches_the_statute_not_the_recovery_period():
    """15 years does not imply 1245. The classification is independent."""
    assert FIVE_YEAR.recapture is Recapture.SECTION_1245
    assert SEVEN_YEAR.recapture is Recapture.SECTION_1245
    assert FIFTEEN_YEAR.recapture is Recapture.SECTION_1250
    assert RESIDENTIAL_RENTAL.recapture is Recapture.SECTION_1250
    assert NONRESIDENTIAL_REAL.recapture is Recapture.SECTION_1250


def test_land_improvements_are_not_cited_to_168e3E():
    """168(e)(3)(E) covers wastewater plant, telephone distribution, retail
    motor fuels outlets and QIP - not parking lots. Land improvements reach
    15 years through the class-life route in 168(e)(1)."""
    assert "168(e)(3)(E)" not in FIFTEEN_YEAR.citation
    assert "87-56" in FIFTEEN_YEAR.citation
    assert "1250(c)" in FIFTEEN_YEAR.citation


def test_a_straight_line_building_has_no_additional_depreciation():
    """Which is exactly why individuals have zero ordinary recapture on a
    building - 1250(b)(1) compares actual against straight line, and for a
    building they are the same schedule."""
    s = build_schedule(M("380000"), date(2009, 6, 15), RESIDENTIAL_RENTAL)
    for year in (2012, 2020, 2026, 2036):
        assert s.additional_depreciation_through(year).is_zero()


def test_150_percent_declining_balance_creates_additional_depreciation():
    """Even with no bonus, 150% DB runs ahead of straight line."""
    s = build_schedule(M("100000"), date(2021, 4, 1), FIFTEEN_YEAR)
    assert s.additional_depreciation_through(2026).is_positive()
    # And it decays back to zero once the schedules converge at the end.
    assert s.additional_depreciation_through(s.final_year).is_zero()


def test_bonus_on_land_improvements_makes_most_of_it_ordinary():
    """The single most commonly missed rule in rental calculators.

    100% bonus deducts the whole basis in year one while the hypothetical
    straight-line schedule has reached only 1/30th of it, so nearly all of it
    is additional depreciation - ordinary income, not 25% gain - on an early
    sale.
    """
    basis = M("47000")
    s = build_schedule(basis, date(2021, 4, 1), FIFTEEN_YEAR, bonus_rate=rate("1.00"))
    five_years_later = s.additional_depreciation_through(2026)
    share = five_years_later.ratio_to(basis)
    assert share > rate("0.60"), f"expected roughly two-thirds, got {share:.0%}"
    # It unwinds as the straight-line schedule catches up.
    assert s.additional_depreciation_through(2036).is_zero()


def test_sale_splits_the_gain_three_ways_not_two():
    household = build_household()
    austin = next(p for p in household.properties if p.id == "austin")
    result = compute_sale(austin, SaleTerms(date(2026, 10, 1), M("742000")), RULES)
    c = result.character

    assert c.section_1245_ordinary == M("79000"), "the 5- and 7-year buckets, entire"
    assert c.section_1250_ordinary.is_positive(), "land improvements, bonus over SL"
    assert c.unrecaptured_1250.is_positive()
    assert c.adjusted_net_capital_gain.is_positive()
    assert c.recognized == result.total_gain, "every dollar is accounted for"

    understatement = c.section_1250_ordinary
    assert understatement > M("30000"), (
        "a model that treats land improvements as 25% property would understate "
        f"ordinary income by {understatement}"
    )


def test_the_25_percent_pool_excludes_both_ordinary_slices():
    """The 25% bucket is what is left after the 1245 bucket and the 1250(a)
    additional-depreciation slice are taken out - never more.

    Compared against the figures the sale itself reports, not against a
    year-end breakdown: the sale date is in October, so a partial year of
    depreciation is allowed in the year of disposition and the pool at closing
    is larger than the pool at the prior year end.
    """
    household = build_household()
    austin = next(p for p in household.properties if p.id == "austin")
    result = compute_sale(austin, SaleTerms(date(2026, 10, 1), M("742000")), RULES)
    pool = result.accumulated_1250 - result.additional_1250
    assert result.character.unrecaptured_1250 <= pool
    assert result.character.section_1250_ordinary <= result.additional_1250
    assert result.character.section_1245_ordinary <= result.accumulated_1245


def test_recapture_is_capped_at_the_gain():
    """Sell for barely more than basis and there is not enough gain to
    recapture everything. The carve stops at the gain, it does not invent it."""
    household = build_household()
    austin = next(p for p in household.properties if p.id == "austin")
    result = compute_sale(austin, SaleTerms(date(2026, 10, 1), M("460000")), RULES)
    assert result.character.recognized == result.total_gain
    assert result.character.adjusted_net_capital_gain.is_zero()
    assert not result.character.unrecaptured_1250.is_negative()
