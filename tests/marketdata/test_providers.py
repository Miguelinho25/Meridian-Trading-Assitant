"""Providers must be interchangeable, and replay must not leak the future."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from nemonis_marketdata.provider import MarketDataProvider
from nemonis_marketdata.providers import FileProvider, ReplayProvider, SyntheticProvider
from nemonis_marketdata.types import MarketDataError
from nemonis_schemas.enums import Timeframe

START = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


class TestSyntheticProvider:
    def test_satisfies_the_interface(self) -> None:
        assert isinstance(SyntheticProvider(seed=1, anchor=START), MarketDataProvider)

    def test_marked_synthetic(self) -> None:
        assert SyntheticProvider(seed=1, anchor=START).is_synthetic is True

    def test_window_is_half_open(self) -> None:
        provider = SyntheticProvider(seed=1, anchor=START)
        bars = provider.candles("EURUSD", Timeframe.H1, START, START + timedelta(hours=10))
        assert all(START <= b.open_time < START + timedelta(hours=10) for b in bars)

    def test_same_bar_is_identical_across_different_windows(self) -> None:
        """A provider that regenerates slightly different data per call would
        silently break replay determinism."""
        provider = SyntheticProvider(seed=1, anchor=START)
        wide = provider.candles("EURUSD", Timeframe.H1, START, START + timedelta(hours=50))
        narrow = provider.candles(
            "EURUSD", Timeframe.H1, START + timedelta(hours=10), START + timedelta(hours=20)
        )
        overlap = {b.open_time: b for b in wide}
        assert all(overlap[b.open_time] == b for b in narrow)

    def test_empty_window_returns_nothing(self) -> None:
        provider = SyntheticProvider(seed=1, anchor=START)
        assert provider.candles("EURUSD", Timeframe.H1, START, START) == []

    def test_quote_available(self) -> None:
        provider = SyntheticProvider(seed=1, anchor=START)
        quote = provider.latest_quote("EURUSD", now=START + timedelta(hours=5))
        assert quote is not None
        assert quote.ask > quote.bid


class TestReplayDoesNotLeakTheFuture:
    """The provider is a second line of defence behind the event loop's own guard."""

    @pytest.fixture
    def provider(self) -> ReplayProvider:
        return ReplayProvider.from_generator(
            ["EURUSD"], seed=9, start=START, bars=200, timeframe=Timeframe.H1
        )

    def test_nothing_visible_before_playback_starts(self, provider: ReplayProvider) -> None:
        assert provider.candles("EURUSD", Timeframe.H1, START, START + timedelta(days=10)) == []

    def test_only_bars_up_to_the_position_are_returned(self, provider: ReplayProvider) -> None:
        provider.seek(START + timedelta(hours=20))
        bars = provider.candles("EURUSD", Timeframe.H1, START, START + timedelta(days=10))
        assert bars
        assert max(b.open_time for b in bars) <= START + timedelta(hours=20)

    def test_requesting_beyond_the_position_reveals_nothing(self, provider: ReplayProvider) -> None:
        provider.seek(START + timedelta(hours=20))
        future = provider.candles(
            "EURUSD",
            Timeframe.H1,
            START + timedelta(hours=21),
            START + timedelta(days=5),
        )
        assert future == []

    def test_quote_never_comes_from_the_future(self, provider: ReplayProvider) -> None:
        provider.seek(START + timedelta(hours=20))
        quote = provider.latest_quote("EURUSD", now=START + timedelta(days=5))
        assert quote is not None
        assert quote.source_time <= START + timedelta(hours=20)

    def test_rewinding_is_refused(self, provider: ReplayProvider) -> None:
        provider.seek(START + timedelta(hours=20))
        with pytest.raises(MarketDataError, match="Refusing to rewind"):
            provider.seek(START + timedelta(hours=10))

    def test_advance_moves_forward_by_one_bar(self, provider: ReplayProvider) -> None:
        provider.advance()
        first = provider.position
        provider.advance()
        assert first is not None
        assert provider.position == first + timedelta(hours=1)

    def test_wrong_timeframe_refused_rather_than_resampled(self, provider: ReplayProvider) -> None:
        """Resampling would change the fill model silently."""
        provider.seek(START + timedelta(hours=20))
        with pytest.raises(MarketDataError, match="Resampling"):
            provider.candles("EURUSD", Timeframe.M15, START, START + timedelta(hours=10))

    def test_replay_is_deterministic(self) -> None:
        a = ReplayProvider.from_generator(["EURUSD"], seed=4, start=START, bars=100)
        b = ReplayProvider.from_generator(["EURUSD"], seed=4, start=START, bars=100)
        a.seek(START + timedelta(hours=50))
        b.seek(START + timedelta(hours=50))
        window = (START, START + timedelta(hours=50))
        assert a.candles("EURUSD", Timeframe.H1, *window) == b.candles(
            "EURUSD", Timeframe.H1, *window
        )


class TestFileProvider:
    def _write_csv(self, path, *, bid_ask: bool = True) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            if bid_ask:
                writer = csv.writer(handle)
                writer.writerow(
                    [
                        "time",
                        "bid_open",
                        "bid_high",
                        "bid_low",
                        "bid_close",
                        "ask_open",
                        "ask_high",
                        "ask_low",
                        "ask_close",
                        "volume",
                    ]
                )
                for k in range(10):
                    t = (START + timedelta(hours=k)).isoformat()
                    writer.writerow(
                        [
                            t,
                            "1.0850",
                            "1.0860",
                            "1.0840",
                            "1.0855",
                            "1.0851",
                            "1.0861",
                            "1.0841",
                            "1.0856",
                            "100",
                        ]
                    )
            else:
                writer = csv.writer(handle)
                writer.writerow(["time", "open", "high", "low", "close"])
                for k in range(10):
                    t = (START + timedelta(hours=k)).isoformat()
                    writer.writerow([t, "1.0850", "1.0860", "1.0840", "1.0855"])

    def test_loads_bid_ask_csv(self, tmp_path) -> None:
        path = tmp_path / "eurusd.csv"
        self._write_csv(path)
        provider = FileProvider.from_csv(path, instrument="EURUSD", timeframe=Timeframe.H1)
        bars = provider.candles("EURUSD", Timeframe.H1, START, START + timedelta(hours=10))
        assert len(bars) == 10
        assert provider.is_synthetic is False
        assert provider.spread_assumed is False

    def test_mid_only_requires_explicit_spread(self, tmp_path) -> None:
        """Silently inventing a spread would make backtest costs fictional."""
        path = tmp_path / "mid.csv"
        self._write_csv(path, bid_ask=False)
        with pytest.raises(MarketDataError, match="no bid/ask columns"):
            FileProvider.from_csv(path, instrument="EURUSD", timeframe=Timeframe.H1)

    def test_mid_only_with_spread_is_flagged_as_assumed(self, tmp_path) -> None:
        path = tmp_path / "mid.csv"
        self._write_csv(path, bid_ask=False)
        provider = FileProvider.from_csv(
            path, instrument="EURUSD", timeframe=Timeframe.H1, spread_pips=Decimal("1.0")
        )
        assert provider.spread_assumed is True
        bar = provider.candles("EURUSD", Timeframe.H1, START, START + timedelta(hours=1))[0]
        assert bar.ask_close > bar.bid_close

    def test_naive_timestamp_is_rejected(self, tmp_path) -> None:
        """Assuming UTC could shift bars across a daily-reset boundary."""
        path = tmp_path / "naive.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time", "open", "high", "low", "close"])
            writer.writerow(["2026-07-27 00:00:00", "1.08", "1.09", "1.07", "1.085"])
        with pytest.raises(MarketDataError, match="no timezone"):
            FileProvider.from_csv(
                path, instrument="EURUSD", timeframe=Timeframe.H1, spread_pips=Decimal("1.0")
            )

    def test_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(MarketDataError, match="No such data file"):
            FileProvider.from_csv(
                tmp_path / "absent.csv", instrument="EURUSD", timeframe=Timeframe.H1
            )

    def test_error_names_the_offending_line(self, tmp_path) -> None:
        path = tmp_path / "bad.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["time", "open", "high", "low", "close"])
            writer.writerow([(START).isoformat(), "1.08", "1.09", "1.07", "1.085"])
            writer.writerow([(START).isoformat(), "not-a-number", "1.09", "1.07", "1.085"])
        with pytest.raises(MarketDataError, match=r"bad\.csv:3"):
            FileProvider.from_csv(
                path, instrument="EURUSD", timeframe=Timeframe.H1, spread_pips=Decimal("1.0")
            )
