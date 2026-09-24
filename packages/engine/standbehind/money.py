"""Exact money arithmetic.

The engine's outputs get defended out loud by an advisor with their license
behind them, so "close enough" is not a thing here. Every monetary quantity in
this codebase is an integer number of cents. Floats never touch the tax path.

Two rules make that stick:

1. ``Money`` cannot be constructed from a ``float``. The only ways in are an
   integer count of cents, a string, or a ``Decimal``.
2. Multiplying money by a rate is not an operator. It is
   :meth:`Money.apply_rate`, which *requires* you to have already decided the
   rounding mode. There is no implicit rounding anywhere.

The subtle one is :meth:`Money.allocate`. Splitting $1,000,000.00 into a
72.5/27.5 land/improvement basis split leaves a stray cent under naive
rounding, and a stray cent in the basis compounds into a wrong depreciation
schedule for 27.5 years. ``allocate`` uses the largest-remainder method so the
parts always sum to exactly the whole.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal, localcontext

__all__ = ["Money", "Rate", "rate", "ZERO"]

# A rate is an exact decimal (0.25, 0.038, 0.0363636...), never a float.
Rate = Decimal

_CENTS = Decimal("0.01")
_HUNDRED = Decimal(100)


def rate(value: str | int | Decimal) -> Rate:
    """Build a rate from an exact literal.

    Always pass a string for fractional rates. ``rate(0.1)`` is rejected
    because binary floating point cannot represent 0.1, and a tax engine that
    silently accepts 0.1000000000000000055511151231257827 is a tax engine that
    will eventually be wrong by a dollar in front of a client.
    """
    if isinstance(value, float):  # pragma: no cover - defensive
        raise TypeError(
            "rate() refuses floats; pass a string literal e.g. rate('0.0375')"
        )
    return Decimal(value)


class Money:
    """An exact monetary amount, stored as a signed integer number of cents."""

    __slots__ = ("_cents",)

    def __init__(self, cents: int) -> None:
        if not isinstance(cents, int) or isinstance(cents, bool):
            raise TypeError(
                f"Money() takes an integer number of cents, got {type(cents).__name__}. "
                "Use Money.from_dollars() for a dollar amount."
            )
        self._cents = cents

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_dollars(cls, amount: str | int | Decimal) -> Money:
        """Build from a dollar amount. Rejects floats by design."""
        if isinstance(amount, float):
            raise TypeError(
                "Money.from_dollars() refuses floats. Pass a string "
                f"(e.g. Money.from_dollars('{amount!r}')) or a Decimal, so the "
                "value you meant is the value you get."
            )
        as_decimal = Decimal(amount)
        quantised = (as_decimal * _HUNDRED).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return cls(int(quantised))

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def sum(cls, amounts: Iterable[Money]) -> Money:
        total = 0
        for amount in amounts:
            total += amount._cents
        return cls(total)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    @property
    def cents(self) -> int:
        return self._cents

    @property
    def dollars(self) -> Decimal:
        """Exact decimal dollars. Safe for display and serialisation."""
        return (Decimal(self._cents) / _HUNDRED).quantize(_CENTS)

    def is_zero(self) -> bool:
        return self._cents == 0

    def is_positive(self) -> bool:
        return self._cents > 0

    def is_negative(self) -> bool:
        return self._cents < 0

    # ------------------------------------------------------------------
    # Arithmetic. Money +/- Money and Money * int are exact and closed.
    # ------------------------------------------------------------------
    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self._cents + other._cents)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self._cents - other._cents)

    def __mul__(self, factor: int) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            raise TypeError(
                "Money * x is only defined for whole counts. To apply a rate, "
                "use Money.apply_rate(rate) and name the rounding you want."
            )
        return Money(self._cents * factor)

    __rmul__ = __mul__

    def __neg__(self) -> Money:
        return Money(-self._cents)

    def __abs__(self) -> Money:
        return Money(abs(self._cents))

    def __bool__(self) -> bool:
        return self._cents != 0

    # ------------------------------------------------------------------
    # Rate application - the only place rounding happens.
    # ------------------------------------------------------------------
    def apply_rate(self, r: Rate, rounding: str = ROUND_HALF_UP) -> Money:
        """Multiply by a rate, rounding to the cent exactly once.

        Rounding is explicit and happens here and nowhere else, so a reviewer
        can find every rounding point in the engine by grepping for this name.
        """
        if isinstance(r, float):
            raise TypeError("apply_rate() refuses floats; use rate('0.25')")
        with localcontext() as ctx:
            ctx.prec = 34  # far more than any dollar amount needs
            product = Decimal(self._cents) * Decimal(r)
        return Money(int(product.quantize(Decimal(1), rounding=rounding)))

    def ratio_to(self, other: Money) -> Rate:
        """Exact ratio of two amounts, for use as a rate. Undefined at zero."""
        if other._cents == 0:
            raise ZeroDivisionError("ratio_to() against a zero denominator")
        with localcontext() as ctx:
            ctx.prec = 34
            return Decimal(self._cents) / Decimal(other._cents)

    # ------------------------------------------------------------------
    # Exact splitting
    # ------------------------------------------------------------------
    def allocate(self, weights: Sequence[int | Decimal]) -> list[Money]:
        """Split into parts proportional to ``weights``, losing no cents.

        Largest-remainder method: floor every share, then hand the leftover
        cents out one at a time to whichever parts were shortchanged most.
        ``sum(m.allocate(w)) == m`` holds for every input, which is what makes
        a land/improvement split safe to depreciate off for 27.5 years.
        """
        if not weights:
            raise ValueError("allocate() needs at least one weight")
        decimal_weights = [Decimal(w) for w in weights]
        if any(w < 0 for w in decimal_weights):
            raise ValueError("allocate() weights must be non-negative")
        total_weight = sum(decimal_weights)
        if total_weight == 0:
            raise ValueError("allocate() weights must not sum to zero")

        # Work on the magnitude so negative amounts split symmetrically.
        sign = -1 if self._cents < 0 else 1
        magnitude = abs(self._cents)

        with localcontext() as ctx:
            ctx.prec = 34
            exact = [Decimal(magnitude) * w / total_weight for w in decimal_weights]

        floors = [int(value.to_integral_value(rounding="ROUND_FLOOR")) for value in exact]
        remainder = magnitude - sum(floors)

        # Hand out the remaining cents to the largest fractional parts,
        # breaking ties by position so the result is deterministic.
        order = sorted(
            range(len(exact)),
            key=lambda i: (-(exact[i] - floors[i]), i),
        )
        for i in order[:remainder]:
            floors[i] += 1

        return [Money(sign * part) for part in floors]

    def split_by_rate(self, r: Rate) -> tuple[Money, Money]:
        """Split into (``r`` share, remainder). The two always sum to self."""
        first = self.apply_rate(r)
        return first, self - first

    def prorate(self, numerator: int, denominator: int) -> Money:
        """Scale by an exact integer fraction, e.g. 7.5 months of 12."""
        if denominator == 0:
            raise ZeroDivisionError("prorate() against a zero denominator")
        with localcontext() as ctx:
            ctx.prec = 34
            product = Decimal(self._cents) * Decimal(numerator) / Decimal(denominator)
        return Money(int(product.quantize(Decimal(1), rounding=ROUND_HALF_UP)))

    def clamp_at_zero(self) -> Money:
        """Floor at zero. Used where the code says a quantity cannot go negative."""
        return self if self._cents > 0 else Money(0)

    # ------------------------------------------------------------------
    # Comparison, hashing, display
    # ------------------------------------------------------------------
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Money):
            return self._cents == other._cents
        return NotImplemented

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents < other._cents

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents <= other._cents

    def __gt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents > other._cents

    def __ge__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents >= other._cents

    def __hash__(self) -> int:
        return hash((Money, self._cents))

    def __repr__(self) -> str:
        return f"Money.from_dollars('{self.dollars}')"

    def __str__(self) -> str:
        return format(self)

    def __format__(self, spec: str) -> str:
        """``f"{m}"`` -> ``$1,234.56``; ``f"{m:0}"`` -> ``$1,235`` (whole dollars)."""
        negative = self._cents < 0
        magnitude = abs(self._cents)
        if spec == "0":
            whole = (Decimal(magnitude) / _HUNDRED).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
            body = f"${whole:,}"
        else:
            body = f"${Decimal(magnitude) / _HUNDRED:,.2f}"
        return f"-{body}" if negative else body


ZERO = Money(0)
