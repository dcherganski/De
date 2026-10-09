"""Decimal arithmetic for money and quantities. Floats never touch a balance."""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_UP, Decimal, getcontext

getcontext().prec = 34

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("0.0001")
PCT = Decimal("0.01")


def D(value) -> Decimal:
    """Exact conversion: strings and ints as-is, floats through their shortest repr."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round a quantity down to the exchange step, so we never ask for more than we can afford."""
    if step <= 0:
        raise ValueError("step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def money(value: Decimal, places: str = "0.01") -> Decimal:
    """Rounding for display only; stored values keep full precision."""
    return value.quantize(Decimal(places), rounding=ROUND_HALF_EVEN)


def pct_to_fraction(pct: Decimal) -> Decimal:
    return pct * PCT
