"""Depreciation must sum to basis, and must match the IRS published tables."""
from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from standbehind.depreciation import (
    FIFTEEN_YEAR,
    FIVE_YEAR,
    NONRESIDENTIAL_REAL,
    RESIDENTIAL_RENTAL,
    SEVEN_YEAR,
    build_schedule,
)
from standbehind.money import Money, rate

basis = st.integers(min_value=1, max_value=50_000_000_00)
months = st.integers(min_value=1, max_value=12)
years = st.integers(min_value=1990, max_value=2035)
classes = st.sampled_from(
    [RESIDENTIAL_RENTAL, NONRESIDENTIAL_REAL, FIVE_YEAR, SEVEN_YEAR, FIFTEEN_YEAR]
)


@given(basis, years, months, classes)
@settings(max_examples=300, deadline=None)
def test_schedule_sums_to_exactly_basis(cents, year, month, cls):
    """No drift, for any basis, any month, any class. This is what makes
    accumulated depreciation - and therefore adjusted basis - exact."""
    b = Money(cents)
    s = build_schedule(b, date(year, month, 1), cls)
    assert s.total == b


@given(basis, years, months, classes)
@settings(max_examples=200, deadline=None)
def test_no_year_is_negative_and_accumulation_is_monotonic(cents, year, month, cls):
    s = build_schedule(Money(cents), date(year, month, 1), cls)
    assert all(not a.is_negative() for _, a in s.by_year)
    previous = Money(0)
    for y, _ in s.by_year:
        current = s.accumulated_through(y)
        assert current >= previous
        previous = current
    assert s.accumulated_through(s.final_year) == s.total


@given(basis, years, months)
@settings(max_examples=150, deadline=None)
def test_accumulated_never_exceeds_depreciable_basis(cents, year, month):
    b = Money(cents)
    s = build_schedule(b, date(year, month, 1), RESIDENTIAL_RENTAL)
    for y, _ in s.by_year:
        assert s.accumulated_through(y) <= b


# --------------------------------------------------------------------------
# Golden values: IRS Pub 946 published percentage tables.
# --------------------------------------------------------------------------
def _percentages(schedule, basis_money, places="0.001"):
    from decimal import Decimal as D
    return [
        (D(a.cents) / D(basis_money.cents) * 100).quantize(D(places))
        for _, a in schedule.by_year
    ]


def _assert_matches_published_table(cls, published, label):
    """Compare against a published IRS table at the precision it is published.

    Pub 946 prints these to two decimal places, and then nudges individual
    years so each column sums to exactly 100.00%. The engine instead computes
    the exact fraction the statute describes, so the two agree to within one
    published unit (0.01) but are not always byte-identical - for example
    7-year property year 6 is exactly 8.925%, which the IRS prints as 8.92
    rather than 8.93 so the column lands on 100.00.

    Asserting "within one published unit, and sums to exactly 100%" is the
    honest test. Asserting byte equality against a rounded presentation would
    be testing the rounding, not the depreciation.
    """
    from decimal import Decimal as D
    b = Money.from_dollars("1000000")
    s = build_schedule(b, date(2021, 3, 1), cls)
    got = _percentages(s, b, "0.001")
    assert len(got) >= len(published), f"{label}: schedule too short"
    for i, expected in enumerate(published):
        diff = abs(got[i] - D(expected))
        assert diff <= D("0.005"), (
            f"{label} year {i + 1}: engine {got[i]} vs published {expected} "
            f"(difference {diff} exceeds half a published unit)"
        )
    # The real invariant is on the money, not on the display percentages:
    # rounding each year to three decimals for comparison introduces its own
    # error, so summing those would test the formatting. The schedule itself
    # must sum to exactly the basis.
    assert s.total == b, f"{label}: schedule sums to {s.total}, not {b}"


def test_matches_irs_table_a6_residential_27_5_year():
    """Pub 946 Table A-6, residential rental, mid-month convention.

    First-year percentages by placed-in-service month, and the 3.636% that
    every full year in the middle of the schedule uses. These are published to
    three decimals and the engine reproduces all twelve exactly.
    """
    expected_first_year = {
        1: "3.485", 2: "3.182", 3: "2.879", 4: "2.576", 5: "2.273", 6: "1.970",
        7: "1.667", 8: "1.364", 9: "1.061", 10: "0.758", 11: "0.455", 12: "0.152",
    }
    b = Money.from_dollars("1000000")
    for month, expected in expected_first_year.items():
        s = build_schedule(b, date(2020, month, 1), RESIDENTIAL_RENTAL)
        assert _percentages(s, b)[0] == Decimal(expected), f"month {month}"

    s = build_schedule(b, date(2020, 1, 1), RESIDENTIAL_RENTAL)
    assert _percentages(s, b)[2] == Decimal("3.636")


def test_matches_irs_table_a1_five_year_200db_half_year():
    """Pub 946 Table A-1, 5-year property. The switch from 200% declining
    balance to straight line is computed, not hardcoded - reproducing this
    column is the evidence the switch point is found correctly."""
    _assert_matches_published_table(
        FIVE_YEAR, ["20.00", "32.00", "19.20", "11.52", "11.52", "5.76"], "5-year"
    )


def test_matches_irs_table_a1_seven_year_200db_half_year():
    """Pub 946 Table A-1, 7-year property."""
    _assert_matches_published_table(
        SEVEN_YEAR,
        ["14.29", "24.49", "17.49", "12.49", "8.93", "8.92", "8.93", "4.46"],
        "7-year",
    )


def test_matches_irs_table_a1_fifteen_year_150db_half_year():
    """Pub 946 Table A-1, 15-year land improvements, 150% declining balance."""
    _assert_matches_published_table(
        FIFTEEN_YEAR,
        ["5.00", "9.50", "8.55", "7.70", "6.93", "6.23", "5.90", "5.90"],
        "15-year",
    )


def test_bonus_depreciation_takes_the_whole_thing_in_year_one():
    b = Money.from_dollars("60000")
    s = build_schedule(b, date(2025, 6, 1), FIVE_YEAR, bonus_rate=rate("1.00"))
    assert s.annual(2025) == b
    assert s.total == b


def test_bonus_never_applies_to_the_building():
    """Section 168(k) needs a recovery period of 20 years or less. The
    building does not qualify; that asymmetry is why cost seg exists."""
    b = Money.from_dollars("500000")
    s = build_schedule(b, date(2025, 6, 1), RESIDENTIAL_RENTAL, bonus_rate=rate("1.00"))
    assert s.annual(2025) < b
    assert not RESIDENTIAL_RENTAL.bonus_eligible


def test_disposition_year_uses_mid_month_proration():
    b = Money.from_dollars("275000")
    s = build_schedule(b, date(2010, 1, 1), RESIDENTIAL_RENTAL)
    full = s.annual(2026)
    # Sold in July: 6.5 months of 12 => 13/24 of a full year.
    sold = s.truncate_at_disposition(date(2026, 7, 20)).annual(2026)
    assert sold == full.prorate(13, 24)
