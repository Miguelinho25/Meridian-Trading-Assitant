"""Feature pipeline: leakage safety, warm-up honesty, and immutability (ADR-0006)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from nemonis_features import (
    FEATURES,
    MAX_LOOKBACK,
    FeatureStoreError,
    InMemoryFeatureStore,
    build_series,
    compute_row,
    get_feature,
    required_lookback,
)
from nemonis_marketdata import BarView, SyntheticGenerator
from nemonis_schemas.enums import Timeframe

START = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)


@pytest.fixture
def bars():
    return SyntheticGenerator("EURUSD", seed=1234).generate_list(START, 300)


class TestLookbackDeclarations:
    def test_every_feature_declares_a_positive_lookback(self) -> None:
        for definition in FEATURES:
            assert definition.lookback >= 1, definition.name

    def test_warmup_is_derived_not_hardcoded(self) -> None:
        assert max(f.lookback for f in FEATURES) == MAX_LOOKBACK

    def test_required_lookback_for_a_subset(self) -> None:
        assert required_lookback(["return_1", "sma_50"]) == get_feature("sma_50").lookback

    def test_unknown_feature_raises(self) -> None:
        with pytest.raises(Exception, match="Unknown feature"):
            get_feature("does_not_exist")


class TestWarmupHonesty:
    """A feature computed over too little data must be None, never a number."""

    def test_insufficient_history_yields_none(self, bars) -> None:
        row = compute_row(BarView(bars, 0), computed_at=NOW)
        assert row.values["sma_50"] is None
        assert row.values["volatility_50"] is None

    def test_zero_lookback_features_available_immediately(self, bars) -> None:
        row = compute_row(BarView(bars, 0), computed_at=NOW)
        assert row.values["hour_of_day"] is not None
        assert row.values["session_liquidity"] is not None

    def test_all_features_warm_after_max_lookback(self, bars) -> None:
        row = compute_row(BarView(bars, MAX_LOOKBACK), computed_at=NOW)
        assert row.is_warm, [k for k, v in row.values.items() if v is None]

    def test_not_warm_before_max_lookback(self, bars) -> None:
        assert not compute_row(BarView(bars, MAX_LOOKBACK - 2), computed_at=NOW).is_warm


class TestLeakageSafety:
    def test_row_depends_only_on_past_bars(self, bars) -> None:
        """Mutating the future must not change a row computed in the past.

        The strongest available check: compute at index i, replace everything
        after i with garbage, recompute, and require an identical result.
        """
        index = 100
        original = compute_row(BarView(bars, index), computed_at=NOW)

        tampered = list(bars)
        replacement = SyntheticGenerator("EURUSD", seed=999).generate_list(
            START + timedelta(hours=index + 1), len(bars) - index - 1
        )
        tampered[index + 1 :] = replacement

        recomputed = compute_row(BarView(tampered, index), computed_at=NOW)
        assert recomputed.values == original.values
        assert recomputed.source_bar_hash == original.source_bar_hash

    def test_altering_the_past_does_change_the_row(self, bars) -> None:
        """The converse — otherwise the test above proves nothing."""
        index = 100
        original = compute_row(BarView(bars, index), computed_at=NOW)

        tampered = list(bars)
        tampered[index - 5] = SyntheticGenerator("GBPJPY", seed=5).generate_list(
            bars[index - 5].open_time, 1
        )[0]
        recomputed = compute_row(BarView(tampered, index), computed_at=NOW)
        assert recomputed.source_bar_hash != original.source_bar_hash

    def test_decision_time_is_the_bar_open(self, bars) -> None:
        row = compute_row(BarView(bars, 60), computed_at=NOW)
        assert row.decision_time == bars[60].open_time

    def test_build_series_walks_index_by_index(self, bars) -> None:
        """Each row must see only its own history, not the whole frame."""
        rows = build_series(bars[:80], computed_at=NOW)
        assert len(rows) == 80
        assert rows[0].values["sma_50"] is None
        assert rows[60].values["sma_50"] is not None


class TestImmutability:
    def test_identical_rewrite_is_idempotent(self, bars) -> None:
        store = InMemoryFeatureStore()
        row = compute_row(BarView(bars, 60), computed_at=NOW)
        store.put(row)
        store.put(row)
        assert len(store) == 1

    def test_conflicting_overwrite_is_refused(self, bars) -> None:
        """The scenario ADR-0006 exists to prevent: a recomputed feature
        silently rewriting history with future-informed values."""
        store = InMemoryFeatureStore()
        store.put(compute_row(BarView(bars, 60), computed_at=NOW))

        tampered = list(bars)
        tampered[59] = SyntheticGenerator("GBPJPY", seed=3).generate_list(bars[59].open_time, 1)[0]
        conflicting = compute_row(BarView(tampered, 60), computed_at=NOW)

        with pytest.raises(FeatureStoreError, match="immutable"):
            store.put(conflicting)

    def test_new_version_coexists_with_old(self, bars) -> None:
        """A correction is a new version, and old rows survive for reproducibility."""
        store = InMemoryFeatureStore()
        view = BarView(bars, 60)
        store.put(compute_row(view, computed_at=NOW, feature_version="1.0.0"))
        store.put(compute_row(view, computed_at=NOW, feature_version="1.1.0"))
        assert len(store) == 2
        assert store.versions == {"1.0.0", "1.1.0"}


class TestPointInTimeReads:
    def test_as_of_never_returns_a_future_row(self, bars) -> None:
        store = InMemoryFeatureStore()
        build_series(bars[:100], computed_at=NOW, store=store)

        moment = bars[50].open_time
        row = store.as_of("EURUSD", Timeframe.H1, moment)
        assert row is not None
        assert row.decision_time <= moment

    def test_as_of_picks_the_latest_qualifying_row(self, bars) -> None:
        store = InMemoryFeatureStore()
        build_series(bars[:100], computed_at=NOW, store=store)

        moment = bars[50].open_time + timedelta(minutes=30)
        row = store.as_of("EURUSD", Timeframe.H1, moment)
        assert row is not None
        assert row.decision_time == bars[50].open_time

    def test_as_of_before_any_data_returns_none(self, bars) -> None:
        store = InMemoryFeatureStore()
        build_series(bars[:100], computed_at=NOW, store=store)
        assert store.as_of("EURUSD", Timeframe.H1, START - timedelta(days=1)) is None

    def test_range_is_half_open_and_ordered(self, bars) -> None:
        store = InMemoryFeatureStore()
        build_series(bars[:100], computed_at=NOW, store=store)

        rows = store.range("EURUSD", Timeframe.H1, bars[10].open_time, bars[20].open_time)
        assert [r.decision_time for r in rows] == [b.open_time for b in bars[10:20]]

    def test_warm_only_filter_excludes_warmup(self, bars) -> None:
        store = InMemoryFeatureStore()
        build_series(bars[:120], computed_at=NOW, store=store)
        warm = store.range("EURUSD", Timeframe.H1, START, bars[119].open_time, warm_only=True)
        assert all(r.is_warm for r in warm)
        assert len(warm) < 120


class TestDeterminism:
    def test_same_bars_produce_the_same_rows(self, bars) -> None:
        a = compute_row(BarView(bars, 80), computed_at=NOW)
        b = compute_row(BarView(bars, 80), computed_at=NOW)
        assert a.values == b.values
        assert a.source_bar_hash == b.source_bar_hash


class TestDecimalDiscipline:
    def test_feature_values_are_decimal(self, bars) -> None:
        row = compute_row(BarView(bars, MAX_LOOKBACK + 5), computed_at=NOW)
        for name, value in row.values.items():
            assert value is None or isinstance(value, Decimal), name
