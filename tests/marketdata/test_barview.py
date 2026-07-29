"""BarView must make look-ahead impossible, not merely discouraged."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from nemonis_marketdata.barview import BarView, LookAheadError
from nemonis_marketdata.types import Candle
from nemonis_schemas.enums import Timeframe

START = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


def make_series(n: int) -> list[Candle]:
    """Bars whose close encodes their index, so leaks are identifiable."""
    return [
        Candle(
            instrument="EURUSD",
            timeframe=Timeframe.H1,
            open_time=START + timedelta(hours=k),
            bid_open=Decimal(f"1.{1000 + k:04d}"),
            bid_high=Decimal(f"1.{1010 + k:04d}"),
            bid_low=Decimal(f"1.{990 + k:04d}"),
            bid_close=Decimal(f"1.{1000 + k:04d}"),
            ask_open=Decimal(f"1.{1001 + k:04d}"),
            ask_high=Decimal(f"1.{1011 + k:04d}"),
            ask_low=Decimal(f"1.{991 + k:04d}"),
            ask_close=Decimal(f"1.{1001 + k:04d}"),
        )
        for k in range(n)
    ]


class TestFutureAccessRaises:
    def test_positive_index_beyond_decision_raises(self) -> None:
        view = BarView(make_series(10), decision_index=4)
        with pytest.raises(LookAheadError, match="future data"):
            _ = view[5]

    def test_far_future_index_raises(self) -> None:
        view = BarView(make_series(10), decision_index=4)
        with pytest.raises(LookAheadError):
            _ = view[9]

    def test_lookahead_error_is_an_index_error(self) -> None:
        """So `except IndexError` written for short series still surfaces it."""
        assert issubclass(LookAheadError, IndexError)


class TestNegativeIndexingIsRelativeToDecision:
    """The classic silent leak: bars[-1] meaning the end of the whole series."""

    def test_minus_one_is_the_current_bar_not_the_last(self) -> None:
        bars = make_series(10)
        view = BarView(bars, decision_index=4)
        assert view[-1] is bars[4]
        assert view[-1] is not bars[9]

    def test_minus_two_is_the_previous_bar(self) -> None:
        bars = make_series(10)
        view = BarView(bars, decision_index=4)
        assert view[-2] is bars[3]

    def test_negative_beyond_start_raises_plain_index_error(self) -> None:
        view = BarView(make_series(10), decision_index=2)
        with pytest.raises(IndexError, match="before the start"):
            _ = view[-99]


class TestLengthAndIterationHideTheFuture:
    def test_len_counts_visible_bars_only(self) -> None:
        assert len(BarView(make_series(10), decision_index=4)) == 5

    def test_iteration_stops_at_the_decision_index(self) -> None:
        bars = make_series(10)
        view = BarView(bars, decision_index=4)
        assert [b.open_time for b in view] == [b.open_time for b in bars[:5]]

    def test_slice_cannot_escape_the_boundary(self) -> None:
        bars = make_series(10)
        view = BarView(bars, decision_index=4)
        assert len(view[:]) == 5
        assert len(view[0:100]) == 5

    def test_list_conversion_is_bounded(self) -> None:
        view = BarView(make_series(10), decision_index=3)
        assert len(list(view)) == 4


class TestDecisionPointSemantics:
    def test_current_is_the_decision_bar(self) -> None:
        bars = make_series(10)
        assert BarView(bars, decision_index=6).current is bars[6]

    def test_decision_time_is_the_open_not_the_close(self) -> None:
        """Deciding on bar i's close and filling at it is a fantasy fill."""
        bars = make_series(10)
        view = BarView(bars, decision_index=6)
        assert view.decision_time == bars[6].open_time
        assert view.decision_time != bars[6].close_time

    def test_last_closed_is_the_previous_bar(self) -> None:
        bars = make_series(10)
        assert BarView(bars, decision_index=6).last_closed() is bars[5]

    def test_last_closed_raises_at_index_zero(self) -> None:
        with pytest.raises(LookAheadError, match="No closed bar"):
            BarView(make_series(10), decision_index=0).last_closed()


class TestClosesWindow:
    def test_returns_oldest_first_ending_at_current(self) -> None:
        bars = make_series(10)
        view = BarView(bars, decision_index=5)
        assert view.closes(3) == [bars[3].bid_close, bars[4].bid_close, bars[5].bid_close]

    def test_ask_side_available(self) -> None:
        bars = make_series(10)
        view = BarView(bars, decision_index=5)
        assert view.closes(2, side="ask") == [bars[4].ask_close, bars[5].ask_close]

    def test_insufficient_history_raises_rather_than_truncating(self) -> None:
        """A 20-period average silently computed over 3 bars is a plausible-looking bug."""
        view = BarView(make_series(10), decision_index=2)
        with pytest.raises(LookAheadError, match="only 3 bars are visible"):
            view.closes(20)

    def test_exactly_enough_history_is_allowed(self) -> None:
        view = BarView(make_series(10), decision_index=2)
        assert len(view.closes(3)) == 3

    def test_zero_or_negative_count_rejected(self) -> None:
        view = BarView(make_series(10), decision_index=5)
        with pytest.raises(ValueError, match="must be positive"):
            view.closes(0)


class TestConstruction:
    def test_decision_index_beyond_series_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside a series"):
            BarView(make_series(5), decision_index=5)

    def test_negative_decision_index_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            BarView(make_series(5), decision_index=-1)

    def test_has_history_never_raises(self) -> None:
        view = BarView(make_series(10), decision_index=2)
        assert view.has_history(3) is True
        assert view.has_history(4) is False


class TestAdvancingTheView:
    def test_each_index_sees_exactly_its_own_history(self) -> None:
        """Walking the series must never widen what an earlier index could see."""
        bars = make_series(20)
        for i in range(20):
            view = BarView(bars, decision_index=i)
            assert len(view) == i + 1
            assert view.current is bars[i]
            if i < 19:
                with pytest.raises(LookAheadError):
                    _ = view[i + 1]
