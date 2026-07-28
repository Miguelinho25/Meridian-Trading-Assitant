"""The generator must be deterministic and structurally honest."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise

import pytest
from meridian_marketdata.instruments import WATCHLIST, get_spec
from meridian_marketdata.sessions import is_weekend, primary_session
from meridian_marketdata.synthetic import SyntheticGenerator
from meridian_schemas.enums import Session, Timeframe

# A Monday, so generation starts in an open market.
START = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


class TestDeterminism:
    """Replay determinism is the property the whole backtest rests on."""

    def test_same_seed_is_byte_identical(self) -> None:
        a = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 200)
        b = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 200)
        assert a == b

    def test_different_seed_differs(self) -> None:
        a = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 100)
        b = SyntheticGenerator("EURUSD", seed=43).generate_list(START, 100)
        assert a != b

    def test_instrument_series_is_independent_of_watchlist_order(self) -> None:
        """Adding a pair must not shift another pair's series.

        Otherwise every committed fixture changes whenever the watchlist is edited.
        """
        first = SyntheticGenerator("EURUSD", seed=7).generate_list(START, 50)
        # Generating another instrument in between must not disturb EURUSD.
        SyntheticGenerator("GBPJPY", seed=7).generate_list(START, 50)
        second = SyntheticGenerator("EURUSD", seed=7).generate_list(START, 50)
        assert first == second

    def test_two_instruments_differ_under_one_seed(self) -> None:
        eur = SyntheticGenerator("EURUSD", seed=7).generate_list(START, 50)
        gbp = SyntheticGenerator("GBPUSD", seed=7).generate_list(START, 50)
        assert [c.bid_close for c in eur] != [c.bid_close for c in gbp]


class TestStructuralValidity:
    @pytest.mark.parametrize("symbol", list(WATCHLIST))
    def test_every_instrument_generates_valid_bars(self, symbol: str) -> None:
        """Candle.__post_init__ rejects crossed quotes and inverted highs."""
        bars = SyntheticGenerator(symbol, seed=11).generate_list(START, 300)
        assert len(bars) == 300

    def test_ask_always_exceeds_bid(self) -> None:
        for bar in SyntheticGenerator("EURUSD", seed=5).generate_list(START, 500):
            assert bar.ask_close > bar.bid_close
            assert bar.ask_low > bar.bid_low

    def test_high_low_bracket_open_close(self) -> None:
        for bar in SyntheticGenerator("GBPJPY", seed=5).generate_list(START, 500):
            assert bar.bid_high >= max(bar.bid_open, bar.bid_close)
            assert bar.bid_low <= min(bar.bid_open, bar.bid_close)

    def test_prices_stay_positive(self) -> None:
        for bar in SyntheticGenerator("EURUSD", seed=99).generate_list(START, 2000):
            assert bar.bid_low > 0


class TestMarketStructure:
    def test_no_bars_generated_during_weekend(self) -> None:
        """A generator that trades through the weekend flatters every gap assumption."""
        bars = SyntheticGenerator("EURUSD", seed=3).generate_list(START, 400)
        assert not any(is_weekend(b.open_time) for b in bars)

    def test_series_spans_a_weekend_gap(self) -> None:
        """400 hourly bars must cross a weekend, proving closure is exercised."""
        bars = SyntheticGenerator("EURUSD", seed=3).generate_list(START, 400)
        gaps = [(b.open_time - a.open_time).total_seconds() / 3600 for a, b in pairwise(bars)]
        assert max(gaps) > 24, "expected a weekend gap of more than a day"

    def test_spread_is_wider_in_thin_liquidity(self) -> None:
        """London spreads should beat the dead zone between New York and Tokyo."""
        bars = SyntheticGenerator("EURUSD", seed=17).generate_list(START, 1500)
        spec = get_spec("EURUSD")

        london = [
            b.spread_close / spec.pip_size
            for b in bars
            if primary_session(b.open_time) is Session.LONDON
        ]
        thin = [
            b.spread_close / spec.pip_size
            for b in bars
            if primary_session(b.open_time) is Session.SYDNEY
        ]
        assert london
        assert thin
        assert sum(london) / len(london) < sum(thin) / len(thin)

    def test_spread_never_collapses_to_zero(self) -> None:
        for bar in SyntheticGenerator("EURUSD", seed=21).generate_list(START, 800):
            assert bar.spread_close > Decimal(0)


class TestTimeframes:
    def test_bar_spacing_matches_timeframe(self) -> None:
        bars = SyntheticGenerator("EURUSD", seed=2, timeframe=Timeframe.M15).generate_list(
            START, 20
        )
        deltas = {(b.open_time - a.open_time).total_seconds() for a, b in pairwise(bars)}
        assert deltas == {15 * 60}

    def test_jpy_pairs_use_three_digits(self) -> None:
        bar = SyntheticGenerator("USDJPY", seed=1).generate_list(START, 1)[0]
        assert bar.bid_close.as_tuple().exponent >= -3
