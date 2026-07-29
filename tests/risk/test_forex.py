"""Forex arithmetic. Invariants I3 (floor) and I4 (Decimal) live or die here."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from nemonis_marketdata.instruments import get_spec
from nemonis_risk.forex import (
    ConversionRoute,
    ForexError,
    calculate_position_size,
    convert_quote_to_account,
    loss_at_stop,
    margin_required,
    pip_value_per_lot,
    stop_distance_pips,
)
from nemonis_schemas.enums import Direction, RejectionCode

pytestmark = pytest.mark.risk

EURUSD = get_spec("EURUSD")
USDJPY = get_spec("USDJPY")
GBPJPY = get_spec("GBPJPY")
EURGBP = get_spec("EURGBP")

RATES = {
    "EURUSD": Decimal("1.08500"),
    "GBPUSD": Decimal("1.26500"),
    "USDJPY": Decimal("157.200"),
    "USDCHF": Decimal("0.89500"),
    "USDCAD": Decimal("1.37500"),
    "EURGBP": Decimal("0.85500"),
    "GBPJPY": Decimal("199.000"),
}


class TestPipSize:
    def test_jpy_pairs_use_two_decimal_pips(self) -> None:
        assert USDJPY.pip_size == Decimal("0.01")
        assert GBPJPY.pip_size == Decimal("0.01")

    def test_non_jpy_pairs_use_four_decimal_pips(self) -> None:
        assert EURUSD.pip_size == Decimal("0.0001")
        assert EURGBP.pip_size == Decimal("0.0001")


class TestPipValue:
    def test_usd_quoted_pair_in_usd_account(self) -> None:
        """EURUSD, 1 standard lot: 0.0001 × 100,000 = $10 per pip."""
        assert pip_value_per_lot(EURUSD, Decimal(1)) == Decimal("10.0000")

    def test_jpy_quoted_pair_converted_to_usd(self) -> None:
        """USDJPY: 0.01 × 100,000 = ¥1,000 per pip, ÷157.2 ≈ $6.36."""
        rate = Decimal(1) / RATES["USDJPY"]
        value = pip_value_per_lot(USDJPY, rate)
        assert Decimal("6.3") < value < Decimal("6.4")

    def test_non_positive_rate_refused(self) -> None:
        with pytest.raises(ForexError) as exc:
            pip_value_per_lot(EURUSD, Decimal(0))
        assert exc.value.code is RejectionCode.FX_CONVERSION_UNAVAILABLE


class TestConversionRoutes:
    """All four documented routes, plus the refusal."""

    def test_identity(self) -> None:
        result = convert_quote_to_account("USD", "USD", RATES)
        assert result.route is ConversionRoute.IDENTITY
        assert result.rate == Decimal(1)

    def test_direct(self) -> None:
        result = convert_quote_to_account("EUR", "USD", RATES)
        assert result.route is ConversionRoute.DIRECT
        assert result.rate == RATES["EURUSD"]

    def test_inverse(self) -> None:
        """Account USD, quote JPY: use 1/USDJPY."""
        result = convert_quote_to_account("JPY", "USD", RATES)
        assert result.route is ConversionRoute.INVERSE
        assert result.rate == Decimal(1) / RATES["USDJPY"]

    def test_inverse_is_preferred_over_triangulation(self) -> None:
        """JPY -> GBP looks like a cross, but GBPJPY is quoted, so the inverse
        route is available and must be taken — fewer hops, less error."""
        result = convert_quote_to_account("JPY", "GBP", RATES)
        assert result.route is ConversionRoute.INVERSE
        assert result.rate == Decimal(1) / RATES["GBPJPY"]

    def test_triangulated(self) -> None:
        """CHF -> CAD has neither direct nor inverse; go via USD."""
        result = convert_quote_to_account("CHF", "CAD", RATES)
        assert result.route is ConversionRoute.TRIANGULATED
        assert len(result.via) == 2
        # 1 CHF = 1/0.895 USD; 1 USD = 1.375 CAD
        expected = (Decimal(1) / RATES["USDCHF"]) * RATES["USDCAD"]
        assert abs(result.rate - expected) < Decimal("0.0000001")

    def test_missing_rate_is_refused_never_assumed(self) -> None:
        """An assumed rate silently produces a wrong position size."""
        with pytest.raises(ForexError, match="Refusing to assume") as exc:
            convert_quote_to_account("JPY", "NOK", {})
        assert exc.value.code is RejectionCode.FX_CONVERSION_UNAVAILABLE

    def test_zero_rate_treated_as_missing(self) -> None:
        with pytest.raises(ForexError):
            convert_quote_to_account("EUR", "USD", {"EURUSD": Decimal(0)})

    def test_route_records_pairs_consulted(self) -> None:
        result = convert_quote_to_account("CHF", "CAD", RATES)
        assert "USDCHF" in result.via
        assert "USDCAD" in result.via


class TestStopDistanceValidation:
    def test_long_stop_below_entry_is_valid(self) -> None:
        pips = stop_distance_pips(EURUSD, Decimal("1.0850"), Decimal("1.0820"), Direction.LONG)
        assert pips == Decimal(30)

    def test_short_stop_above_entry_is_valid(self) -> None:
        pips = stop_distance_pips(EURUSD, Decimal("1.0850"), Decimal("1.0880"), Direction.SHORT)
        assert pips == Decimal(30)

    def test_long_stop_above_entry_is_a_sign_error_not_a_wide_stop(self) -> None:
        """abs() alone would produce a plausible size for a nonsensical trade."""
        with pytest.raises(ForexError, match="wrong side") as exc:
            stop_distance_pips(EURUSD, Decimal("1.0850"), Decimal("1.0880"), Direction.LONG)
        assert exc.value.code is RejectionCode.STOP_DISTANCE_INVALID

    def test_short_stop_below_entry_refused(self) -> None:
        with pytest.raises(ForexError, match="wrong side"):
            stop_distance_pips(EURUSD, Decimal("1.0850"), Decimal("1.0820"), Direction.SHORT)

    def test_zero_distance_refused(self) -> None:
        with pytest.raises(ForexError, match="undefined"):
            stop_distance_pips(EURUSD, Decimal("1.0850"), Decimal("1.0850"), Direction.LONG)

    def test_jpy_pip_scaling(self) -> None:
        """30 pips on USDJPY is 0.30, not 0.0030."""
        pips = stop_distance_pips(USDJPY, Decimal("157.20"), Decimal("156.90"), Direction.LONG)
        assert pips == Decimal(30)


class TestPositionSizing:
    def _size(self, **kw):
        base = {
            "spec": EURUSD,
            "equity": Decimal("100000"),
            "risk_pct": Decimal("0.5"),
            "entry": Decimal("1.08500"),
            "stop": Decimal("1.08200"),
            "direction": Direction.LONG,
            "account_ccy": "USD",
            "rates": RATES,
        }
        return calculate_position_size(**{**base, **kw})

    def test_textbook_case(self) -> None:
        """$100k, 0.5% = $500 risk, 30-pip stop, $10/pip/lot -> 1.66 lots."""
        result = self._size()
        assert result.risk_amount_account_ccy == Decimal("500.00")
        assert result.stop_pips == Decimal(30)
        assert result.lots == Decimal("1.66")  # 1.6667 floored to 0.01 step

    def test_realised_risk_never_exceeds_authorised(self) -> None:
        result = self._size()
        assert result.realised_risk_account_ccy <= result.risk_amount_account_ccy

    def test_flooring_reduces_risk_it_never_rounds_up(self) -> None:
        result = self._size()
        assert result.lots * Decimal(30) * Decimal(10) <= Decimal("500.00")

    def test_jpy_pair_sizing(self) -> None:
        result = self._size(
            spec=USDJPY, entry=Decimal("157.20"), stop=Decimal("156.90"), direction=Direction.LONG
        )
        assert result.lots > 0
        assert result.realised_risk_account_ccy <= result.risk_amount_account_ccy

    def test_cross_pair_with_non_usd_account(self) -> None:
        result = self._size(
            spec=GBPJPY,
            entry=Decimal("199.00"),
            stop=Decimal("198.50"),
            account_ccy="GBP",
        )
        assert result.conversion.route is ConversionRoute.INVERSE
        assert result.realised_risk_account_ccy <= result.risk_amount_account_ccy

    def test_wider_stop_gives_smaller_size(self) -> None:
        tight = self._size(stop=Decimal("1.08400"))
        wide = self._size(stop=Decimal("1.07500"))
        assert wide.lots < tight.lots

    def test_size_below_minimum_lot_is_refused(self) -> None:
        """A tiny account with a wide stop cannot trade — say so, do not round up."""
        with pytest.raises(ForexError, match="below the") as exc:
            self._size(equity=Decimal("500"), risk_pct=Decimal("0.1"), stop=Decimal("1.00000"))
        assert exc.value.code is RejectionCode.SIZE_BELOW_MINIMUM_LOT

    def test_size_is_capped_at_max_lot(self) -> None:
        result = self._size(equity=Decimal("100000000"), risk_pct=Decimal("1.0"))
        assert result.lots <= EURUSD.max_lot

    def test_non_positive_equity_refused(self) -> None:
        with pytest.raises(ForexError) as exc:
            self._size(equity=Decimal(0))
        assert exc.value.code is RejectionCode.ACCOUNT_STATE_AMBIGUOUS

    def test_missing_conversion_rate_refuses_the_trade(self) -> None:
        with pytest.raises(ForexError) as exc:
            self._size(account_ccy="NOK", rates={})
        assert exc.value.code is RejectionCode.FX_CONVERSION_UNAVAILABLE

    def test_lots_are_a_multiple_of_lot_step(self) -> None:
        result = self._size()
        assert result.lots % EURUSD.lot_step == 0


class TestSizingProperties:
    """Invariant I3 as a property, across the input space."""

    @settings(max_examples=250, deadline=None)
    @given(
        equity=st.decimals(min_value=10_000, max_value=1_000_000, places=2),
        risk_pct=st.decimals(min_value=Decimal("0.05"), max_value=Decimal("1.0"), places=2),
        stop_pips=st.integers(min_value=5, max_value=400),
    )
    def test_realised_risk_never_exceeds_intended(
        self, equity: Decimal, risk_pct: Decimal, stop_pips: int
    ) -> None:
        entry = Decimal("1.08500")
        stop = entry - (Decimal(stop_pips) * EURUSD.pip_size)
        try:
            result = calculate_position_size(
                spec=EURUSD,
                equity=equity,
                risk_pct=risk_pct,
                entry=entry,
                stop=stop,
                direction=Direction.LONG,
                account_ccy="USD",
                rates=RATES,
            )
        except ForexError:
            return  # Refusing is always acceptable; over-risking never is.
        assert result.realised_risk_account_ccy <= result.risk_amount_account_ccy
        assert result.realised_risk_pct <= risk_pct

    @settings(max_examples=150, deadline=None)
    @given(
        risk_a=st.decimals(min_value=Decimal("0.10"), max_value=Decimal("0.50"), places=2),
        risk_b=st.decimals(min_value=Decimal("0.51"), max_value=Decimal("1.00"), places=2),
    )
    def test_more_risk_never_gives_a_smaller_size(self, risk_a: Decimal, risk_b: Decimal) -> None:
        """Monotonicity: sizing must not be erratic in the risk input."""
        common = {
            "spec": EURUSD,
            "equity": Decimal("100000"),
            "entry": Decimal("1.08500"),
            "stop": Decimal("1.08200"),
            "direction": Direction.LONG,
            "account_ccy": "USD",
            "rates": RATES,
        }
        small = calculate_position_size(risk_pct=risk_a, **common)
        large = calculate_position_size(risk_pct=risk_b, **common)
        assert large.lots >= small.lots


class TestLossAtStop:
    def test_matches_the_authorised_risk(self) -> None:
        loss = loss_at_stop(
            spec=EURUSD,
            lots=Decimal("1.66"),
            stop_pips=Decimal(30),
            pip_value_per_lot=Decimal(10),
        )
        assert loss == Decimal("498.00")


class TestMargin:
    def test_uses_base_currency_notional(self) -> None:
        """Notional is base-denominated; converting from quote would be wrong
        by the exchange rate."""
        margin = margin_required(
            spec=EURUSD,
            lots=Decimal(1),
            price=Decimal("1.085"),
            fx_base_to_account=RATES["EURUSD"],
        )
        # 100,000 EUR × 1.085 USD/EUR × 3.33% ≈ $3,613
        assert Decimal("3600") < margin < Decimal("3630")


class TestNoFloatsLeak:
    def test_every_returned_quantity_is_decimal(self) -> None:
        result = calculate_position_size(
            spec=EURUSD,
            equity=Decimal("100000"),
            risk_pct=Decimal("0.5"),
            entry=Decimal("1.08500"),
            stop=Decimal("1.08200"),
            direction=Direction.LONG,
            account_ccy="USD",
            rates=RATES,
        )
        for value in (
            result.lots,
            result.risk_amount_account_ccy,
            result.realised_risk_account_ccy,
            result.realised_risk_pct,
            result.stop_pips,
            result.pip_value_per_lot,
            result.conversion.rate,
        ):
            assert isinstance(value, Decimal)
