"""Regime classification must be explainable, deterministic and honestly uncertain."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_features.regime import (
    CLASSIFIER_VERSION,
    RegimeClassifier,
    RuleBasedRegimeClassifier,
    TrendState,
    VolatilityState,
)
from nemonis_marketdata import BarView, SyntheticGenerator

START = datetime(2026, 7, 27, 0, 0, tzinfo=UTC)


@pytest.fixture
def bars():
    return SyntheticGenerator("EURUSD", seed=77).generate_list(START, 400)


@pytest.fixture
def classifier() -> RuleBasedRegimeClassifier:
    return RuleBasedRegimeClassifier()


class TestInterface:
    def test_satisfies_the_protocol(self, classifier) -> None:
        """So an HMM or clustering model can replace it without touching callers."""
        assert isinstance(classifier, RegimeClassifier)

    def test_reports_its_version(self, classifier) -> None:
        assert classifier.version == CLASSIFIER_VERSION

    def test_declares_required_lookback(self, classifier) -> None:
        assert classifier.required_lookback > 0


class TestUnknownIsNotADefault:
    """A wrong regime label silently mis-attributes every trade taken under it."""

    def test_insufficient_history_is_unknown(self, bars, classifier) -> None:
        result = classifier.classify(BarView(bars, 5))
        assert result.is_unknown
        assert result.trend is TrendState.UNKNOWN
        assert result.volatility is VolatilityState.UNKNOWN
        assert result.confidence == Decimal(0)

    def test_unknown_is_never_confident(self, bars, classifier) -> None:
        assert not classifier.classify(BarView(bars, 5)).is_confident

    def test_unknown_explains_why(self, bars, classifier) -> None:
        result = classifier.classify(BarView(bars, 5))
        assert "Insufficient history" in result.explanation
        assert str(classifier.required_lookback) in result.explanation

    def test_classifies_once_warm(self, bars, classifier) -> None:
        result = classifier.classify(BarView(bars, 200))
        assert not result.is_unknown
        assert result.trend is not TrendState.UNKNOWN


class TestExplainability:
    def test_every_classification_carries_an_explanation(self, bars, classifier) -> None:
        result = classifier.classify(BarView(bars, 200))
        assert result.explanation
        # The numbers behind the label must appear, so it can be disputed.
        assert "Trend strength" in result.explanation
        assert "volatility ratio" in result.explanation

    def test_feature_values_are_retained(self, bars, classifier) -> None:
        result = classifier.classify(BarView(bars, 200))
        assert "trend_strength_20" in result.features
        assert "volatility_20" in result.features

    def test_version_is_attached(self, bars, classifier) -> None:
        assert classifier.classify(BarView(bars, 200)).classifier_version == CLASSIFIER_VERSION

    def test_decision_time_is_the_bar_open(self, bars, classifier) -> None:
        result = classifier.classify(BarView(bars, 200))
        assert result.decision_time == bars[200].open_time


class TestConfidenceReflectsAmbiguity:
    def test_confidence_is_bounded(self, bars, classifier) -> None:
        for index in range(60, 400, 17):
            result = classifier.classify(BarView(bars, index))
            assert Decimal(0) <= result.confidence <= Decimal(1)

    def test_ambiguous_trend_reports_low_confidence(self, bars, classifier) -> None:
        """A value sitting between thresholds is a coin flip and must say so."""
        results = [classifier.classify(BarView(bars, i)) for i in range(60, 400)]
        warm = [r for r in results if not r.is_unknown]
        ambiguous = [r for r in warm if r.confidence < Decimal("0.5")]
        assert ambiguous, "expected at least one genuinely ambiguous classification"
        for result in ambiguous:
            assert result.alternative is not None

    def test_near_boundary_offers_an_alternative(self, bars, classifier) -> None:
        warm = [classifier.classify(BarView(bars, i)) for i in range(60, 400)]
        assert any(r.alternative is not None for r in warm if not r.is_unknown)


class TestDeterminism:
    def test_same_input_same_label(self, bars, classifier) -> None:
        a = classifier.classify(BarView(bars, 200))
        b = classifier.classify(BarView(bars, 200))
        assert a.label == b.label
        assert a.confidence == b.confidence
        assert a.features == b.features

    def test_independent_instances_agree(self, bars) -> None:
        a = RuleBasedRegimeClassifier().classify(BarView(bars, 200))
        b = RuleBasedRegimeClassifier().classify(BarView(bars, 200))
        assert a.label == b.label


class TestNoLookAhead:
    def test_future_bars_do_not_change_the_label(self, bars, classifier) -> None:
        index = 200
        original = classifier.classify(BarView(bars, index))

        tampered = list(bars)
        tampered[index + 1 :] = SyntheticGenerator("GBPJPY", seed=1).generate_list(
            bars[index + 1].open_time, len(bars) - index - 1
        )
        recomputed = classifier.classify(BarView(tampered, index))

        assert recomputed.label == original.label
        assert recomputed.confidence == original.confidence


class TestLabelSpace:
    def test_labels_are_well_formed(self, bars, classifier) -> None:
        valid_trends = {s.value for s in TrendState}
        valid_vols = {s.value for s in VolatilityState}
        for index in range(60, 400, 11):
            result = classifier.classify(BarView(bars, index))
            if result.is_unknown:
                assert result.label == "UNKNOWN"
                continue
            trend, _, volatility = result.label.partition("/")
            assert trend in valid_trends
            assert volatility in valid_vols

    def test_more_than_one_regime_occurs_across_a_long_series(self, bars, classifier) -> None:
        """A classifier that only ever emits one label attributes nothing."""
        labels = {classifier.classify(BarView(bars, i)).label for i in range(60, 400)}
        assert len(labels) > 1
