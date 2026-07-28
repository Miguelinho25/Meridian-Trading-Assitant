"""Data quality is a fail-closed gate: unknown state must never read as OK."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from meridian_marketdata import Quote, SyntheticGenerator, assess_quote, assess_series, get_spec
from meridian_schemas.enums import DataQualityVerdict, Timeframe

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
SPEC = get_spec("EURUSD")
START = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


def quote(**overrides) -> Quote:
    defaults = {
        "instrument": "EURUSD",
        "bid": Decimal("1.08500"),
        "ask": Decimal("1.08508"),
        "source_time": NOW,
        "arrival_time": NOW,
    }
    return Quote(**{**defaults, **overrides})


class TestFreshQuotePasses:
    def test_clean_quote_is_ok(self) -> None:
        report = assess_quote(quote(), now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.OK
        assert not report.blocks_trading
        assert report.score == Decimal(1)


class TestStaleDataBlocks:
    def test_quote_older_than_limit_is_invalid(self) -> None:
        old = quote(source_time=NOW - timedelta(seconds=120))
        report = assess_quote(old, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.INVALID
        assert report.blocks_trading
        assert any(i.code == "MARKET_DATA_STALE" for i in report.issues)

    def test_quote_at_the_limit_still_passes(self) -> None:
        edge = quote(source_time=NOW - timedelta(seconds=60))
        report = assess_quote(edge, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.OK


class TestMissingDataBlocks:
    def test_absent_quote_is_invalid_not_ok(self) -> None:
        """Absence of evidence is not evidence of quality."""
        report = assess_quote(None, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.INVALID
        assert report.blocks_trading
        assert report.score == Decimal(0)

    def test_empty_series_is_invalid(self) -> None:
        report = assess_series([], spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        assert report.verdict is DataQualityVerdict.INVALID
        assert report.blocks_trading


class TestClockSkewBlocks:
    def test_source_time_in_the_future_is_invalid(self) -> None:
        """Age becomes meaningless, so nothing about the feed can be trusted."""
        future = quote(source_time=NOW + timedelta(minutes=5))
        report = assess_quote(future, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.INVALID
        assert any(i.code == "SOURCE_TIME_IN_FUTURE" for i in report.issues)

    def test_arrival_before_source_is_invalid(self) -> None:
        skewed = quote(source_time=NOW, arrival_time=NOW - timedelta(seconds=30))
        report = assess_quote(skewed, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.INVALID
        assert any(i.code == "ARRIVAL_BEFORE_SOURCE" for i in report.issues)


class TestAbnormalSpreadBlocks:
    def test_wide_spread_is_invalid(self) -> None:
        wide = quote(ask=Decimal("1.08600"))  # 10 pips vs 0.8 typical
        report = assess_quote(wide, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.INVALID
        assert any(i.code == "ABNORMAL_SPREAD" for i in report.issues)

    def test_normal_spread_passes(self) -> None:
        normal = quote(ask=Decimal("1.08512"))  # 1.2 pips
        report = assess_quote(normal, now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.OK

    def test_zero_spread_is_invalid(self) -> None:
        report = assess_quote(quote(ask=Decimal("1.08500")), now=NOW, max_age_seconds=60, spec=SPEC)
        assert report.verdict is DataQualityVerdict.INVALID
        assert any(i.code == "ZERO_OR_CROSSED_SPREAD" for i in report.issues)


class TestSeriesAssessment:
    def test_clean_generated_series_is_ok(self) -> None:
        bars = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 200)
        report = assess_series(bars, spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        assert report.verdict is DataQualityVerdict.OK, [i.detail for i in report.issues]

    def test_weekend_gaps_are_not_counted_as_missing(self) -> None:
        """A generator that skips the weekend must not be reported as gappy."""
        bars = SyntheticGenerator("EURUSD", seed=8).generate_list(START, 400)
        report = assess_series(bars, spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        assert not any(i.code == "MISSING_BARS" and i.blocking for i in report.issues)

    def test_intraday_hole_is_detected(self) -> None:
        bars = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 100)
        holed = bars[:20] + bars[40:]  # drop 20 mid-week bars
        report = assess_series(holed, spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        assert any(i.code == "MISSING_BARS" for i in report.issues)
        assert report.blocks_trading

    def test_duplicate_timestamps_detected(self) -> None:
        bars = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 50)
        report = assess_series([*bars, bars[10]], spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        assert any(i.code == "DUPLICATE_BARS" for i in report.issues)

    def test_out_of_order_bars_detected(self) -> None:
        bars = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 50)
        shuffled = [*bars[:20], *reversed(bars[20:])]
        report = assess_series(shuffled, spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        assert any(i.code == "OUT_OF_ORDER" for i in report.issues)
        assert report.blocks_trading


class TestDegradedStillBlocks:
    def test_degraded_blocks_trading(self) -> None:
        """'Mostly fine' is exactly when a wrong fill slips through unnoticed."""
        bars = SyntheticGenerator("EURUSD", seed=42).generate_list(START, 200)
        holed = bars[:100] + bars[101:]  # one missing bar — below the blocking threshold
        report = assess_series(holed, spec=SPEC, timeframe=Timeframe.H1, now=NOW)
        if report.verdict is DataQualityVerdict.DEGRADED:
            assert report.blocks_trading
