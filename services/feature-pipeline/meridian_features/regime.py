"""Market regime classification, v0.

Deliberately a transparent rule-based classifier over declared features, not a
learned model. Three reasons:

1. It is a *baseline*. An HMM or clustering model that cannot beat explicit
   thresholds is not worth its opacity, and without this baseline there is
   nothing to beat.
2. Every classification is explainable, which matters because regime labels feed
   performance attribution — an unexplainable label makes attribution
   unfalsifiable.
3. It is deterministic, so replay stays reproducible.

The interface is what matters long-term: ``RegimeClassifier`` can be replaced by
a learned model without touching callers — see docs/machine-learning.md §3.2.
Every classification carries a version, a confidence, the runner-up and the
feature values behind it, so a later model is directly comparable.

Thresholds are quantile-free constants chosen to be legible, not fitted. Fitting
them on the same data used to evaluate a strategy would be a leak.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from meridian_marketdata.barview import BarView
from meridian_marketdata.sessions import primary_session

from meridian_features.registry import FeatureDef, get_feature

CLASSIFIER_VERSION: Final = "rule-based@0.1.0"


class TrendState(StrEnum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


class VolatilityState(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    """A regime label with everything needed to dispute it."""

    trend: TrendState
    volatility: VolatilityState
    #: Compact label, e.g. "TRENDING/HIGH". The unit used for attribution.
    label: str
    confidence: Decimal
    #: The next most plausible label. Near-equal confidence means the boundary
    #: is arbitrary and attribution against this label is weak.
    alternative: str | None
    explanation: str
    features: dict[str, Decimal | None]
    classifier_version: str
    decision_time: datetime
    #: True when insufficient history forced UNKNOWN. Callers must not treat an
    #: unknown regime as a normal one.
    is_unknown: bool

    @property
    def is_confident(self) -> bool:
        return self.confidence >= Decimal("0.60") and not self.is_unknown


@runtime_checkable
class RegimeClassifier(Protocol):
    """Replaceable by an HMM, clustering or supervised model."""

    version: str

    def classify(self, view: BarView) -> RegimeClassification: ...

    @property
    def required_lookback(self) -> int: ...


# Thresholds. Legible constants, not fitted parameters.
_TREND_STRONG: Final = Decimal("0.35")
_TREND_WEAK: Final = Decimal("0.18")
_VOL_HIGH_RATIO: Final = Decimal("1.60")
_VOL_LOW_RATIO: Final = Decimal("0.65")


class RuleBasedRegimeClassifier:
    """Baseline classifier over trend strength and relative volatility."""

    version = CLASSIFIER_VERSION

    def __init__(self) -> None:
        self._used: tuple[FeatureDef, ...] = (
            get_feature("trend_strength_20"),
            get_feature("volatility_20"),
            get_feature("volatility_50"),
            get_feature("atr_14"),
            get_feature("dist_sma_20"),
        )

    @property
    def required_lookback(self) -> int:
        return max(f.lookback for f in self._used)

    def classify(self, view: BarView) -> RegimeClassification:
        values = {f.name: f.compute(view) for f in self._used}
        moment = view.decision_time

        trend_strength = values["trend_strength_20"]
        vol_short = values["volatility_20"]
        vol_long = values["volatility_50"]

        # Insufficient history is UNKNOWN, never a default label. A wrong regime
        # label silently mis-attributes every trade taken under it.
        if trend_strength is None or vol_short is None or vol_long is None:
            return RegimeClassification(
                trend=TrendState.UNKNOWN,
                volatility=VolatilityState.UNKNOWN,
                label="UNKNOWN",
                confidence=Decimal(0),
                alternative=None,
                explanation=(
                    f"Insufficient history at {moment.isoformat()}: "
                    f"{self.required_lookback} bars required for classification."
                ),
                features=values,
                classifier_version=self.version,
                decision_time=moment,
                is_unknown=True,
            )

        trend, trend_conf = self._trend(trend_strength)
        volatility, vol_conf, ratio = self._volatility(vol_short, vol_long)

        label = f"{trend.value}/{volatility.value}"
        # The minimum, not the mean. The label is a conjunction — "TRENDING/HIGH"
        # is only as trustworthy as its weaker half. Averaging would let a
        # confident volatility reading disguise a coin-flip trend, and the
        # resulting label would be used for attribution as though it were solid.
        confidence = min(trend_conf, vol_conf)

        alternative = self._alternative(trend, volatility, trend_strength, ratio)

        explanation = (
            f"Trend strength {trend_strength:.3f} → {trend.value} "
            f"(thresholds: >{_TREND_STRONG} trending, <{_TREND_WEAK} ranging). "
            f"Short/long volatility ratio {ratio:.2f} → {volatility.value} "
            f"(>{_VOL_HIGH_RATIO} high, <{_VOL_LOW_RATIO} low). "
            f"Session: {primary_session(moment).value}."
        )

        return RegimeClassification(
            trend=trend,
            volatility=volatility,
            label=label,
            confidence=confidence,
            alternative=alternative,
            explanation=explanation,
            features=values,
            classifier_version=self.version,
            decision_time=moment,
            is_unknown=False,
        )

    @staticmethod
    def _trend(strength: Decimal) -> tuple[TrendState, Decimal]:
        """Confidence scales with distance from the nearest boundary.

        A value sitting on a threshold is a coin flip and must report as such,
        rather than claiming the label it happened to land on.
        """
        if strength >= _TREND_STRONG:
            margin = min((strength - _TREND_STRONG) / _TREND_STRONG, Decimal(1))
            return TrendState.TRENDING, Decimal("0.55") + margin * Decimal("0.45")
        if strength <= _TREND_WEAK:
            margin = min((_TREND_WEAK - strength) / _TREND_WEAK, Decimal(1))
            return TrendState.RANGING, Decimal("0.55") + margin * Decimal("0.45")
        # Between thresholds: genuinely ambiguous.
        span = _TREND_STRONG - _TREND_WEAK
        position = (strength - _TREND_WEAK) / span
        state = TrendState.TRENDING if position > Decimal("0.5") else TrendState.RANGING
        return state, Decimal("0.35")

    @staticmethod
    def _volatility(short: Decimal, long: Decimal) -> tuple[VolatilityState, Decimal, Decimal]:
        if long == 0:
            return VolatilityState.UNKNOWN, Decimal(0), Decimal(0)
        ratio = short / long
        if ratio >= _VOL_HIGH_RATIO:
            margin = min((ratio - _VOL_HIGH_RATIO) / _VOL_HIGH_RATIO, Decimal(1))
            return VolatilityState.HIGH, Decimal("0.55") + margin * Decimal("0.45"), ratio
        if ratio <= _VOL_LOW_RATIO:
            margin = min((_VOL_LOW_RATIO - ratio) / _VOL_LOW_RATIO, Decimal(1))
            return VolatilityState.LOW, Decimal("0.55") + margin * Decimal("0.45"), ratio
        return VolatilityState.NORMAL, Decimal("0.70"), ratio

    @staticmethod
    def _alternative(
        trend: TrendState, volatility: VolatilityState, strength: Decimal, ratio: Decimal
    ) -> str | None:
        """The runner-up label when the classification sits near a boundary."""
        near_trend_edge = _TREND_WEAK < strength < _TREND_STRONG
        near_vol_edge = _VOL_LOW_RATIO * Decimal(
            "0.85"
        ) < ratio < _VOL_LOW_RATIO or _VOL_HIGH_RATIO < ratio < _VOL_HIGH_RATIO * Decimal("1.15")
        if near_trend_edge:
            other_trend = (
                TrendState.RANGING if trend is TrendState.TRENDING else TrendState.TRENDING
            )
            return f"{other_trend.value}/{volatility.value}"
        if near_vol_edge:
            other_vol = (
                VolatilityState.NORMAL
                if volatility is not VolatilityState.NORMAL
                else VolatilityState.HIGH
            )
            return f"{trend.value}/{other_vol.value}"
        return None


DEFAULT_CLASSIFIER: Final = RuleBasedRegimeClassifier()
