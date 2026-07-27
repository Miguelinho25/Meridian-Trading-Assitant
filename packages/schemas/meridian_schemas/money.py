"""Decimal-safe quantities (risk-engine.md §3.4).

Floats never touch money, prices, sizes or percentages. The one asymmetric rule
lives here: lot quantisation floors, so realised risk can never exceed intended
risk (invariant I3).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Annotated, Any

from meridian_config import limits
from pydantic import BeforeValidator, PlainSerializer


class PrecisionError(ValueError):
    """A quantity could not be represented safely."""


def to_decimal(value: Any) -> Decimal:
    """Coerce to Decimal, refusing floats.

    A float carries binary rounding error before we ever see it — 0.1 + 0.2 is
    already wrong by the time it is passed in, and no downstream quantisation can
    recover the intended value. Rejecting at the boundary is the only reliable
    defence, so this raises rather than converting.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise PrecisionError(f"Refusing to coerce bool {value!r} to Decimal")
    if isinstance(value, float):
        raise PrecisionError(
            f"Refusing to coerce float {value!r} to Decimal — binary rounding error is "
            f"already present. Pass a str or Decimal instead."
        )
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise PrecisionError(f"Not a valid decimal: {value!r}") from exc
    raise PrecisionError(f"Cannot convert {type(value).__name__} to Decimal")


def _quantise(value: Decimal, dp: int, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-dp), rounding=rounding)


def quantise_price(value: Decimal, digits: int | None = None) -> Decimal:
    return _quantise(value, digits if digits is not None else limits.PRICE_DP)


def quantise_money(value: Decimal, dp: int | None = None) -> Decimal:
    return _quantise(value, dp if dp is not None else limits.MONEY_DP)


def quantise_percent(value: Decimal) -> Decimal:
    return _quantise(value, limits.PERCENT_DP)


def quantise_pips(value: Decimal) -> Decimal:
    return _quantise(value, limits.PIP_DP)


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round *down* to a multiple of ``step``.

    Invariant I3. Used for lot sizing, where rounding up would mean risking more
    than the operator authorised. Always floors, never rounds to nearest.
    """
    if step <= 0:
        raise PrecisionError(f"Step must be positive, got {step}")
    if value < 0:
        raise PrecisionError(f"Refusing to floor a negative size: {value}")
    return (value / step).quantize(Decimal(1), rounding=ROUND_DOWN) * step


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if low > high:
        raise PrecisionError(f"Invalid clamp bounds: [{low}, {high}]")
    return max(low, min(value, high))


# --- Pydantic annotated types --------------------------------------------
# Serialised as strings so JSON never sees a float and precision survives the
# round trip through the API and the frontend.

DecimalStr = Annotated[
    Decimal,
    BeforeValidator(to_decimal),
    PlainSerializer(str, return_type=str, when_used="json"),
]

Price = DecimalStr
Money = DecimalStr
Percent = DecimalStr
Lots = DecimalStr
Pips = DecimalStr
