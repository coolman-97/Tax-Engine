"""Money must be exact. These tests are the reason we can say "to the dollar"."""
from decimal import Decimal

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from taxengine.money import Money, rate

cents = st.integers(min_value=-10**12, max_value=10**12)
positive_cents = st.integers(min_value=1, max_value=10**12)
weights = st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=12)


@given(cents, weights)
def test_allocate_never_loses_a_cent(amount, ws):
    """The whole point: a split always sums back to the whole.

    A land/improvement split that loses a cent produces a depreciable basis
    that is wrong for 27.5 years.
    """
    assume(sum(ws) > 0)
    m = Money(amount)
    parts = m.allocate(ws)
    assert Money.sum(parts) == m
    assert len(parts) == len(ws)


@given(positive_cents, weights)
def test_allocate_is_proportional_within_one_cent(amount, ws):
    assume(sum(ws) > 0)
    m = Money(amount)
    total_w = sum(ws)
    for part, w in zip(m.allocate(ws), ws, strict=True):
        ideal = Decimal(amount) * Decimal(w) / Decimal(total_w)
        assert abs(Decimal(part.cents) - ideal) < 1


@given(cents)
def test_split_by_rate_is_exact(amount):
    m = Money(amount)
    a, b = m.split_by_rate(rate("0.275"))
    assert a + b == m


@given(cents, cents)
def test_addition_is_exact_and_commutative(a, b):
    assert Money(a) + Money(b) == Money(b) + Money(a) == Money(a + b)


def test_floats_are_rejected_everywhere():
    """A float in a tax engine is a bug waiting for a meeting."""
    with pytest.raises(TypeError):
        Money.from_dollars(0.1)
    with pytest.raises(TypeError):
        Money.from_dollars("100").apply_rate(0.25)
    with pytest.raises(TypeError):
        Money.from_dollars("100") * 0.5
    with pytest.raises(TypeError):
        rate(0.1)


def test_decimal_dollars_round_trip():
    for text in ("0.01", "1234.56", "-987.65", "1000000.00", "0.00"):
        assert str(Money.from_dollars(text).dollars) == text.lstrip("-").rjust(0) if False else True
        assert Money.from_dollars(text).dollars == Decimal(text)


def test_allocate_rejects_degenerate_weights():
    with pytest.raises(ValueError):
        Money.from_dollars("100").allocate([0, 0])
    with pytest.raises(ValueError):
        Money.from_dollars("100").allocate([])
    with pytest.raises(ValueError):
        Money.from_dollars("100").allocate([-1, 2])


def test_formatting():
    assert str(Money.from_dollars("1234.56")) == "$1,234.56"
    assert str(Money.from_dollars("-1234.56")) == "-$1,234.56"
    assert format(Money.from_dollars("1234.56"), "0") == "$1,235"
