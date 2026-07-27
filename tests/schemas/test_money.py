"""Decimal discipline — invariants I3 and I4."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from meridian_schemas.money import (
    PrecisionError,
    clamp,
    floor_to_step,
    quantise_money,
    to_decimal,
)


class TestFloatsAreRefused:
    """I4 — a float reaching a sizing calculation is a bug."""

    def test_float_rejected(self) -> None:
        with pytest.raises(PrecisionError, match="binary rounding error"):
            to_decimal(0.1)

    def test_bool_rejected(self) -> None:
        with pytest.raises(PrecisionError):
            to_decimal(True)

    def test_str_accepted_exactly(self) -> None:
        assert to_decimal("0.1") == Decimal("0.1")

    def test_int_accepted(self) -> None:
        assert to_decimal(7) == Decimal(7)

    def test_garbage_rejected(self) -> None:
        with pytest.raises(PrecisionError):
            to_decimal("not a number")


class TestLotQuantisationAlwaysFloors:
    """I3 — realised risk may never exceed intended risk."""

    @pytest.mark.parametrize(
        ("raw", "step", "expected"),
        [
            ("0.19", "0.01", "0.19"),
            ("0.199", "0.01", "0.19"),
            ("0.1999999", "0.01", "0.19"),
            ("0.999", "0.10", "0.90"),
            ("1.00", "0.01", "1.00"),
            ("0.009", "0.01", "0.00"),
        ],
    )
    def test_floors_never_rounds_up(self, raw: str, step: str, expected: str) -> None:
        assert floor_to_step(Decimal(raw), Decimal(step)) == Decimal(expected)

    @given(
        raw=st.decimals(
            min_value=0, max_value=100, places=6, allow_nan=False, allow_infinity=False
        ),
        step=st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("1")]),
    )
    def test_result_never_exceeds_input(self, raw: Decimal, step: Decimal) -> None:
        assert floor_to_step(raw, step) <= raw

    @given(
        raw=st.decimals(
            min_value=0, max_value=100, places=6, allow_nan=False, allow_infinity=False
        ),
        step=st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("1")]),
    )
    def test_result_is_a_multiple_of_step(self, raw: Decimal, step: Decimal) -> None:
        assert floor_to_step(raw, step) % step == 0

    def test_negative_size_refused(self) -> None:
        with pytest.raises(PrecisionError, match="negative"):
            floor_to_step(Decimal("-0.5"), Decimal("0.01"))

    def test_non_positive_step_refused(self) -> None:
        with pytest.raises(PrecisionError, match="positive"):
            floor_to_step(Decimal("1"), Decimal("0"))


class TestQuantisation:
    def test_money_uses_banker_rounding(self) -> None:
        assert quantise_money(Decimal("1.005")) == Decimal("1.00")
        assert quantise_money(Decimal("1.015")) == Decimal("1.02")

    def test_decimal_arithmetic_is_exact(self) -> None:
        """The failure a float would produce here is the reason for I4."""
        total = to_decimal("0.1") + to_decimal("0.2")
        assert total == Decimal("0.3")
        assert str(total) == "0.3"


class TestClamp:
    def test_clamps_both_ends(self) -> None:
        assert clamp(Decimal("5"), Decimal("1"), Decimal("3")) == Decimal("3")
        assert clamp(Decimal("0"), Decimal("1"), Decimal("3")) == Decimal("1")
        assert clamp(Decimal("2"), Decimal("1"), Decimal("3")) == Decimal("2")

    def test_inverted_bounds_refused(self) -> None:
        with pytest.raises(PrecisionError):
            clamp(Decimal("2"), Decimal("3"), Decimal("1"))
