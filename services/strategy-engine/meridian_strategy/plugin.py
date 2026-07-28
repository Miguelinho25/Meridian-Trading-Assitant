"""Strategy plugin contract (strategy-platform.md §3).

A strategy is a **pure function of market state**. It receives a bounded view of
price history, features and regime — and deliberately *not* the account.

That omission is the important one. A strategy that cannot see the balance cannot
size a position, so sizing stays entirely with the risk engine; and it cannot
behave differently between backtest, replay and paper trading, because the input
it would have varied on is not there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from meridian_features.regime import RegimeClassification
from meridian_marketdata.barview import BarView
from meridian_schemas.enums import Direction, Session


class LifecycleStatus(StrEnum):
    """The promotion funnel (strategy-platform.md §2).

    Hundreds may be REGISTERED; only a handful should ever be ACTIVE. Evidence,
    not compute, is the binding constraint.
    """

    REGISTERED = "REGISTERED"
    CANDIDATE = "CANDIDATE"
    PAPER = "PAPER"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    #: Set by the platform after repeated faults. Not a human decision.
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class StrategyManifest:
    """The declarative contract, so the platform can reason without executing."""

    id: str
    version: str
    author: Literal["HUMAN", "AI", "ENSEMBLE"]
    #: What it believes and why. Mandatory — a strategy that cannot state its
    #: belief cannot be evaluated against whether the belief held, and
    #: "it backtested well" is not a hypothesis.
    hypothesis: str
    required_features: tuple[str, ...]
    lookback_bars: int

    #: Capability constraints. These *are* hard filters.
    supported_instruments: tuple[str, ...] | None = None
    supported_sessions: tuple[Session, ...] | None = None

    #: A PRIOR, never a filter (strategy-platform.md §6). Recorded so the system
    #: can later report whether the author was right. Filtering on it would make
    #: the belief unfalsifiable and suppress the very signals that would reveal
    #: the author was wrong.
    expected_regimes: tuple[str, ...] | None = None

    max_signals_per_day: int = 10
    deterministic: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if not self.hypothesis.strip():
            raise ValueError(
                f"Strategy {self.id!r} has no hypothesis. A strategy that cannot "
                f"state what it believes cannot be evaluated against whether that "
                f"belief held."
            )
        if self.lookback_bars < 1:
            raise ValueError(f"Strategy {self.id!r} must declare a lookback of at least 1")

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def supports_instrument(self, instrument: str) -> bool:
        return self.supported_instruments is None or instrument in self.supported_instruments

    def supports_session(self, session: Session) -> bool:
        return self.supported_sessions is None or session in self.supported_sessions


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy may see. Note what is absent: the account."""

    view: BarView
    features: dict[str, Decimal | None]
    regime: RegimeClassification
    instrument: str
    session: Session
    #: Bar open, not close — a strategy deciding at bar i does not know its close.
    decision_time: datetime

    def feature(self, name: str) -> Decimal | None:
        return self.features.get(name)

    def require(self, name: str) -> Decimal:
        """Read a feature that must be present.

        Raises rather than substituting a default: a strategy computing on a
        stand-in value produces a plausible signal from data it never had.
        """
        value = self.features.get(name)
        if value is None:
            raise ValueError(
                f"Feature {name!r} is unavailable at {self.decision_time.isoformat()} "
                f"(warm-up, or not declared in required_features)."
            )
        return value


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's output. Expresses concepts, never a lot size."""

    strategy_id: str
    strategy_version: str
    instrument: str
    direction: Direction
    decision_time: datetime

    entry: Decimal
    stop: Decimal
    target: Decimal | None

    confidence: Decimal
    setup_type: str
    #: What would prove this wrong, in words. Recorded for the journal.
    invalidation: str = ""
    expected_holding_bars: int | None = None
    explanation: str = ""

    #: The exact feature values behind the decision, stored verbatim so the
    #: signal can be re-derived and disputed later.
    feature_snapshot: dict[str, Decimal | None] = field(default_factory=dict)
    regime_label: str = ""

    def __post_init__(self) -> None:
        if not Decimal(0) <= self.confidence <= Decimal(1):
            raise ValueError(f"Confidence {self.confidence} is outside [0, 1]")
        if self.entry <= 0 or self.stop <= 0:
            raise ValueError("Entry and stop must be positive")
        if self.direction is Direction.LONG and self.stop >= self.entry:
            raise ValueError(f"LONG stop {self.stop} is not below entry {self.entry}")
        if self.direction is Direction.SHORT and self.stop <= self.entry:
            raise ValueError(f"SHORT stop {self.stop} is not above entry {self.entry}")


@dataclass(frozen=True, slots=True)
class NoAction:
    """Explicit "no signal". Carries a reason, because *why* a strategy stayed
    out is research data as much as why it entered."""

    reason: str = ""


StrategyResult = Signal | NoAction


@runtime_checkable
class StrategyPlugin(Protocol):
    """The plugin contract."""

    manifest: StrategyManifest

    def generate(self, ctx: StrategyContext) -> StrategyResult: ...
