"""Feature definitions and the registry.

Every feature declares its **lookback** — the number of bars it needs. Three
things fall out of that declaration, which is why it is mandatory rather than
inferred:

1. ``BarView`` can refuse to compute a feature before enough history exists,
   instead of silently averaging over three bars and returning a plausible number.
2. Purge and embargo lengths for model validation are derived rather than guessed
   ([machine-learning.md §5.1](../../docs/machine-learning.md)).
3. Warm-up length for a backtest is the maximum lookback across active features,
   computed rather than hard-coded.

Features are pure functions of a ``BarView``. They cannot read the clock, perform
I/O, or see beyond the decision index — the view enforces the last one.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final

from nemonis_marketdata.barview import BarView
from nemonis_marketdata.sessions import liquidity_factor, primary_session

#: Bumped when any feature's computation changes. Never recompute stored rows —
#: a correction is a new version (ADR-0006).
FEATURE_VERSION: Final = "1.0.0"

FeatureFn = Callable[[BarView], Decimal | None]


class FeatureError(RuntimeError):
    """A feature could not be computed."""


@dataclass(frozen=True, slots=True)
class FeatureDef:
    name: str
    #: Bars of history required, including the current bar.
    lookback: int
    fn: FeatureFn
    description: str

    def compute(self, view: BarView) -> Decimal | None:
        """Compute, or return None if history is insufficient.

        Returning None rather than raising is deliberate: a warm-up period is a
        normal state, not an error. What must never happen is returning a *number*
        computed from too little data.
        """
        if not view.has_history(self.lookback):
            return None
        return self.fn(view)


# --- Helpers --------------------------------------------------------------


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _stdev(values: list[Decimal]) -> Decimal:
    if len(values) < 2:
        return Decimal(0)
    mu = _mean(values)
    variance = sum(((v - mu) ** 2 for v in values), Decimal(0)) / Decimal(len(values) - 1)
    return Decimal(str(math.sqrt(float(variance))))


def _returns(closes: list[Decimal]) -> list[Decimal]:
    return [(b - a) / a for a, b in pairwise(closes) if a != 0]


# --- Feature implementations ---------------------------------------------
# Each uses only closed history plus the current bar's open, via BarView.


def _sma(period: int) -> FeatureFn:
    def fn(view: BarView) -> Decimal:
        return _mean(view.closes(period))

    return fn


def _return_n(period: int) -> FeatureFn:
    def fn(view: BarView) -> Decimal | None:
        closes = view.closes(period + 1)
        first = closes[0]
        return None if first == 0 else (closes[-1] - first) / first

    return fn


def _volatility(period: int) -> FeatureFn:
    def fn(view: BarView) -> Decimal:
        return _stdev(_returns(view.closes(period + 1)))

    return fn


def _atr(period: int) -> FeatureFn:
    """Average true range, using closed bars only."""

    def fn(view: BarView) -> Decimal:
        ranges: list[Decimal] = []
        for k in range(view.decision_index - period + 1, view.decision_index + 1):
            bar = view.bar(k)
            previous = view.bar(k - 1) if k > 0 else bar
            true_range = max(
                bar.bid_high - bar.bid_low,
                abs(bar.bid_high - previous.bid_close),
                abs(bar.bid_low - previous.bid_close),
            )
            ranges.append(true_range)
        return _mean(ranges)

    return fn


def _trend_strength(period: int) -> FeatureFn:
    """Net move over the period divided by the sum of absolute moves.

    Near 1 means a clean directional run; near 0 means chop. Cheaper and more
    interpretable than ADX, and interpretability matters more than sophistication
    for a baseline that a human has to sanity-check.
    """

    def fn(view: BarView) -> Decimal | None:
        closes = view.closes(period + 1)
        net = abs(closes[-1] - closes[0])
        total = sum((abs(b - a) for a, b in pairwise(closes)), Decimal(0))
        return None if total == 0 else net / total

    return fn


def _distance_from_sma(period: int) -> FeatureFn:
    def fn(view: BarView) -> Decimal | None:
        closes = view.closes(period)
        sma = _mean(closes)
        return None if sma == 0 else (closes[-1] - sma) / sma

    return fn


def _spread_pips(view: BarView) -> Decimal:
    """Current spread in price units. Converted to pips by the caller with the spec."""
    return view.current.ask_open - view.current.bid_open


def _hour_of_day(view: BarView) -> Decimal:
    return Decimal(view.decision_time.hour)


def _day_of_week(view: BarView) -> Decimal:
    return Decimal(view.decision_time.weekday())


def _session_liquidity(view: BarView) -> Decimal:
    return liquidity_factor(view.decision_time)


def _is_london(view: BarView) -> Decimal:
    return Decimal(1) if primary_session(view.decision_time).value == "LONDON" else Decimal(0)


def _range_position(period: int) -> FeatureFn:
    """Where the current price sits in the recent range: 0 = low, 1 = high."""

    def fn(view: BarView) -> Decimal | None:
        window = range(view.decision_index - period + 1, view.decision_index + 1)
        highs = [view.bar(k).bid_high for k in window]
        lows = [view.bar(k).bid_low for k in window]
        top, bottom = max(highs), min(lows)
        if top == bottom:
            return None
        return (view.current.bid_open - bottom) / (top - bottom)

    return fn


# --- Registry -------------------------------------------------------------

FEATURES: Final[tuple[FeatureDef, ...]] = (
    FeatureDef("return_1", 2, _return_n(1), "One-bar return"),
    FeatureDef("return_5", 6, _return_n(5), "Five-bar return"),
    FeatureDef("return_20", 21, _return_n(20), "Twenty-bar return"),
    FeatureDef("sma_10", 10, _sma(10), "Ten-bar simple moving average"),
    FeatureDef("sma_50", 50, _sma(50), "Fifty-bar simple moving average"),
    FeatureDef("dist_sma_20", 20, _distance_from_sma(20), "Relative distance from SMA(20)"),
    FeatureDef("volatility_20", 21, _volatility(20), "Stdev of 20-bar returns"),
    FeatureDef("volatility_50", 51, _volatility(50), "Stdev of 50-bar returns"),
    FeatureDef("atr_14", 15, _atr(14), "Average true range over 14 bars"),
    FeatureDef("trend_strength_20", 21, _trend_strength(20), "Net move / total move over 20 bars"),
    FeatureDef("range_position_20", 20, _range_position(20), "Position within the 20-bar range"),
    FeatureDef("spread_price", 1, _spread_pips, "Current spread in price units"),
    FeatureDef("hour_of_day", 1, _hour_of_day, "UTC hour at the decision instant"),
    FeatureDef("day_of_week", 1, _day_of_week, "Weekday at the decision instant"),
    FeatureDef("session_liquidity", 1, _session_liquidity, "Relative session liquidity"),
    FeatureDef("is_london", 1, _is_london, "Whether London is the primary session"),
)

FEATURES_BY_NAME: Final[dict[str, FeatureDef]] = {f.name: f for f in FEATURES}

#: Bars of warm-up before every feature is computable. Derived, never hard-coded.
MAX_LOOKBACK: Final[int] = max(f.lookback for f in FEATURES)


def get_feature(name: str) -> FeatureDef:
    try:
        return FEATURES_BY_NAME[name]
    except KeyError:
        raise FeatureError(f"Unknown feature {name!r}") from None


def required_lookback(names: list[str] | None = None) -> int:
    """Warm-up needed for a set of features. Used to size purge and embargo."""
    if names is None:
        return MAX_LOOKBACK
    return max(get_feature(n).lookback for n in names)
