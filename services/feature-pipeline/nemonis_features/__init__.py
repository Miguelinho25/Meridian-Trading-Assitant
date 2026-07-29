"""Leakage-safe feature pipeline and point-in-time feature store."""

from __future__ import annotations

from nemonis_features.regime import (
    CLASSIFIER_VERSION,
    DEFAULT_CLASSIFIER,
    RegimeClassification,
    RegimeClassifier,
    RuleBasedRegimeClassifier,
    TrendState,
    VolatilityState,
)
from nemonis_features.registry import (
    FEATURE_VERSION,
    FEATURES,
    FEATURES_BY_NAME,
    MAX_LOOKBACK,
    FeatureDef,
    FeatureError,
    get_feature,
    required_lookback,
)
from nemonis_features.store import (
    WARMUP_BARS,
    FeatureRow,
    FeatureStoreError,
    InMemoryFeatureStore,
    build_series,
    compute_row,
    hash_source_bars,
)

__all__ = [
    "CLASSIFIER_VERSION",
    "DEFAULT_CLASSIFIER",
    "FEATURES",
    "FEATURES_BY_NAME",
    "FEATURE_VERSION",
    "MAX_LOOKBACK",
    "WARMUP_BARS",
    "FeatureDef",
    "FeatureError",
    "FeatureRow",
    "FeatureStoreError",
    "InMemoryFeatureStore",
    "RegimeClassification",
    "RegimeClassifier",
    "RuleBasedRegimeClassifier",
    "TrendState",
    "VolatilityState",
    "build_series",
    "compute_row",
    "get_feature",
    "hash_source_bars",
    "required_lookback",
]
