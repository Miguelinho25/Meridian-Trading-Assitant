"""Event-driven backtest engine, metrics and bias detection."""

from __future__ import annotations

from nemonis_backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    EquityPoint,
)
from nemonis_backtest.metrics import (
    SUFFICIENCY_THRESHOLD,
    SUPPRESSION_THRESHOLD,
    BiasFlag,
    Flag,
    Metrics,
    compute_metrics,
    sharpe_ratio,
)
from nemonis_backtest.validation import (
    MonteCarloResult,
    StressResult,
    StressScenario,
    WalkForwardResult,
    Window,
    monte_carlo,
    stress_test,
    walk_forward,
)

__all__ = [
    "SUFFICIENCY_THRESHOLD",
    "SUPPRESSION_THRESHOLD",
    "BacktestConfig",
    "BacktestEngine",
    "BacktestResult",
    "BiasFlag",
    "EquityPoint",
    "Flag",
    "Metrics",
    "MonteCarloResult",
    "StressResult",
    "StressScenario",
    "WalkForwardResult",
    "Window",
    "compute_metrics",
    "monte_carlo",
    "sharpe_ratio",
    "stress_test",
    "walk_forward",
]
