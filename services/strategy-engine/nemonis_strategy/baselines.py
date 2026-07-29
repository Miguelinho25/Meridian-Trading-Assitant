"""Baseline strategies — **research infrastructure, not profitable systems**.

These exist to exercise the pipeline end to end: features → signal → risk →
fill → attribution. They are two of the most widely published ideas in retail
trading, which is precisely why they are here and precisely why they should not
be expected to make money. Anything this well known is arbitraged.

Their value is as a *control*. When a real strategy is evaluated later, the
question is whether it beats these — and if it cannot beat a 20/50 moving-average
cross after costs, that is worth knowing early and cheaply.

Any performance number they produce is a test fixture, not a finding.
"""

from __future__ import annotations

from decimal import Decimal

from nemonis_schemas.enums import Direction

from nemonis_strategy.plugin import (
    NoAction,
    Signal,
    StrategyContext,
    StrategyManifest,
    StrategyResult,
)


class MovingAverageTrend:
    """Long when the fast average is above the slow one, short when below.

    Stop and target are ATR-scaled so the geometry adapts to volatility rather
    than using fixed pip distances, which would behave completely differently on
    EURUSD and GBPJPY.
    """

    def __init__(
        self,
        *,
        fast: int = 10,
        slow: int = 50,
        stop_atr: Decimal = Decimal("1.5"),
        target_atr: Decimal = Decimal("3.0"),
        version: str = "0.1.0",
    ) -> None:
        self.fast, self.slow = fast, slow
        self.stop_atr, self.target_atr = stop_atr, target_atr
        self.manifest = StrategyManifest(
            id="ma-trend",
            version=version,
            author="HUMAN",
            hypothesis=(
                "BASELINE, NOT A PROFITABLE SYSTEM. Trends persist often enough that "
                "entering in the direction of a fast/slow moving-average cross beats "
                "random entry, before costs. Published everywhere, therefore likely "
                "arbitraged. Present as a control against which real strategies are "
                "measured, not as a candidate for capital."
            ),
            required_features=("sma_10", "sma_50", "atr_14", "trend_strength_20"),
            lookback_bars=51,
            expected_regimes=("TRENDING/NORMAL", "TRENDING/HIGH"),
            max_signals_per_day=2,
            description="Moving-average trend baseline",
        )

    def generate(self, ctx: StrategyContext) -> StrategyResult:
        fast = ctx.feature("sma_10")
        slow = ctx.feature("sma_50")
        atr = ctx.feature("atr_14")
        strength = ctx.feature("trend_strength_20")

        if fast is None or slow is None or atr is None or atr <= 0:
            return NoAction("warm-up: required features unavailable")

        if fast > slow:
            direction = Direction.LONG
            entry = ctx.view.current.ask_open
            stop = entry - atr * self.stop_atr
            target = entry + atr * self.target_atr
        elif fast < slow:
            direction = Direction.SHORT
            entry = ctx.view.current.bid_open
            stop = entry + atr * self.stop_atr
            target = entry - atr * self.target_atr
        else:
            return NoAction("averages equal — no directional view")

        if stop <= 0 or target <= 0:
            return NoAction("degenerate stop or target geometry")

        # Confidence tracks trend cleanliness. Chop is where this idea fails, so
        # the strategy reports lower conviction there rather than pretending.
        confidence = Decimal("0.40")
        if strength is not None:
            confidence = min(Decimal("0.85"), Decimal("0.35") + strength)

        return Signal(
            strategy_id=self.manifest.id,
            strategy_version=self.manifest.version,
            instrument=ctx.instrument,
            direction=direction,
            decision_time=ctx.decision_time,
            entry=entry,
            stop=stop,
            target=target,
            confidence=confidence,
            setup_type="ma-cross",
            invalidation="Averages cross back, or price closes beyond the stop",
            explanation=(
                f"SMA({self.fast})={fast:.5f} vs SMA({self.slow})={slow:.5f}; "
                f"ATR={atr:.5f}; trend strength={strength if strength is not None else 'n/a'}"
            ),
            feature_snapshot=dict(ctx.features),
            regime_label=ctx.regime.label,
        )


class VolatilityBreakout:
    """Enter when price sits at the extreme of its recent range.

    A session/volatility-expansion baseline. Deliberately different in character
    from the trend baseline so the two disagree in different conditions — which
    is what makes them useful for exercising the allocator later.
    """

    def __init__(
        self,
        *,
        threshold: Decimal = Decimal("0.85"),
        stop_atr: Decimal = Decimal("1.2"),
        target_atr: Decimal = Decimal("2.4"),
        version: str = "0.1.0",
    ) -> None:
        self.threshold = threshold
        self.stop_atr, self.target_atr = stop_atr, target_atr
        self.manifest = StrategyManifest(
            id="vol-breakout",
            version=version,
            author="HUMAN",
            hypothesis=(
                "BASELINE, NOT A PROFITABLE SYSTEM. Price closing at the extreme of "
                "its recent range signals volatility expansion that continues briefly. "
                "Included to give the pipeline a second strategy with a different "
                "failure mode from the trend baseline, so multi-strategy behaviour can "
                "be exercised. Not a candidate for capital."
            ),
            required_features=("range_position_20", "atr_14", "volatility_20"),
            lookback_bars=21,
            expected_regimes=("TRENDING/HIGH", "RANGING/HIGH"),
            max_signals_per_day=3,
            description="Volatility-expansion breakout baseline",
        )

    def generate(self, ctx: StrategyContext) -> StrategyResult:
        position = ctx.feature("range_position_20")
        atr = ctx.feature("atr_14")

        if position is None or atr is None or atr <= 0:
            return NoAction("warm-up: required features unavailable")

        if position >= self.threshold:
            direction = Direction.LONG
            entry = ctx.view.current.ask_open
            stop = entry - atr * self.stop_atr
            target = entry + atr * self.target_atr
        elif position <= (Decimal(1) - self.threshold):
            direction = Direction.SHORT
            entry = ctx.view.current.bid_open
            stop = entry + atr * self.stop_atr
            target = entry - atr * self.target_atr
        else:
            return NoAction(f"range position {position:.2f} is mid-range")

        if stop <= 0 or target <= 0:
            return NoAction("degenerate stop or target geometry")

        # Conviction scales with how far into the extreme price has pushed.
        distance = abs(position - Decimal("0.5")) * 2
        confidence = min(Decimal("0.80"), Decimal("0.30") + distance / 2)

        return Signal(
            strategy_id=self.manifest.id,
            strategy_version=self.manifest.version,
            instrument=ctx.instrument,
            direction=direction,
            decision_time=ctx.decision_time,
            entry=entry,
            stop=stop,
            target=target,
            confidence=confidence,
            setup_type="range-breakout",
            invalidation="Price returns inside the range, or the stop is hit",
            explanation=f"Range position {position:.3f}, ATR {atr:.5f}",
            feature_snapshot=dict(ctx.features),
            regime_label=ctx.regime.label,
        )


#: Labelled so no caller can mistake these for validated systems.
BASELINE_STRATEGIES = (MovingAverageTrend, VolatilityBreakout)
