"""Event-driven backtest engine, metrics and bias detection."""

from __future__ import annotations

from meridian_backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    EquityPoint,
)
from meridian_backtest.metrics import (
    SUFFICIENCY_THRESHOLD,
    SUPPRESSION_THRESHOLD,
    BiasFlag,
    Flag,
    Metrics,
    compute_metrics,
    sharpe_ratio,
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
    "compute_metrics",
    "sharpe_ratio",
]
