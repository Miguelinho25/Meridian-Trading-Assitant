"""Strategy plugins: manifest, isolation and registry (ADR-0007)."""

from __future__ import annotations

from meridian_strategy.plugin import (
    LifecycleStatus,
    NoAction,
    Signal,
    StrategyContext,
    StrategyManifest,
    StrategyPlugin,
    StrategyResult,
)
from meridian_strategy.registry import (
    GenerationOutcome,
    Registration,
    RegistryError,
    StrategyHealth,
    StrategyRegistry,
    signals_from,
)

__all__ = [
    "GenerationOutcome",
    "LifecycleStatus",
    "NoAction",
    "Registration",
    "RegistryError",
    "Signal",
    "StrategyContext",
    "StrategyHealth",
    "StrategyManifest",
    "StrategyPlugin",
    "StrategyRegistry",
    "StrategyResult",
    "signals_from",
]
