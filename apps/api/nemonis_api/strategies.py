"""The strategy registry.

Read-only, and lifecycle promotion is deliberately absent. Moving a strategy
toward ACTIVE is an evidence decision, not a UI action: it requires validated
out-of-sample performance, and a button that skipped that would be the fastest
route to trading an unvalidated strategy. Promotion arrives with the
evidence-gated allocator (ADR-0007, Milestone 2), which reads the backtest
records rather than a click.

One distinction this module is careful to preserve. ``supported_instruments``
and ``supported_sessions`` are **hard filters** — a strategy simply cannot run
outside them. ``expected_regimes`` is a **prior**: what the author believes,
recorded so the platform can later report whether they were right. Filtering on
it would make the belief unfalsifiable and suppress exactly the signals that
would reveal the author was wrong (strategy-platform.md §6). The API keeps them
in separate fields with that difference stated, so no UI can quietly conflate
them.
"""

from __future__ import annotations

from fastapi import APIRouter
from nemonis_strategy.baselines import MovingAverageTrend, VolatilityBreakout
from nemonis_strategy.plugin import LifecycleStatus
from nemonis_strategy.registry import Registration, StrategyRegistry
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/strategies", tags=["strategies"])


def build_registry() -> StrategyRegistry:
    """The strategies this build ships.

    Baselines only, and they are honest baselines rather than a shipped edge:
    something has to be beaten before a new strategy can claim anything.

    Registered CANDIDATE, not ACTIVE. They were previously ACTIVE, which
    contradicted both their own hypothesis ("not a candidate for capital") and
    the Backtest Lab reporting zero runs qualifying as evidence — the platform
    was simultaneously claiming nothing is validated and that two strategies are
    live. CANDIDATE is still runnable, so backtests are unaffected, and ACTIVE
    now means what it should: promoted on evidence.
    """
    registry = StrategyRegistry()
    for factory in (MovingAverageTrend, VolatilityBreakout):
        registry.register(factory(), status=LifecycleStatus.CANDIDATE)
    return registry


class Health(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: int
    faults: int
    timeouts: int
    fault_rate: str
    mean_micros: str
    last_fault: str | None
    last_fault_at: str | None


class StrategyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    id: str
    version: str
    author: str
    #: Mandatory. A strategy that cannot state what it believes cannot be
    #: evaluated against whether that belief held.
    hypothesis: str
    description: str
    status: str
    is_runnable: bool
    quarantine_reason: str | None
    deterministic: bool
    required_features: list[str]
    lookback_bars: int
    max_signals_per_day: int
    #: Hard filters — the strategy cannot run outside these.
    supported_instruments: list[str] | None
    supported_sessions: list[str] | None
    #: A prior, never a filter. Kept in its own field so no UI can conflate it
    #: with the capability constraints above.
    expected_regimes: list[str] | None
    health: Health


class FunnelStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    count: int
    runnable: bool


class RegistryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategies: list[StrategyOut]
    funnel: list[FunnelStage]
    notice: str


def _to_out(registration: Registration) -> StrategyOut:
    m = registration.manifest
    h = registration.health
    return StrategyOut(
        key=m.key,
        id=m.id,
        version=m.version,
        author=m.author,
        hypothesis=m.hypothesis,
        description=m.description,
        status=registration.status.value,
        is_runnable=registration.is_runnable,
        quarantine_reason=registration.quarantine_reason,
        deterministic=m.deterministic,
        required_features=list(m.required_features),
        lookback_bars=m.lookback_bars,
        max_signals_per_day=m.max_signals_per_day,
        supported_instruments=(
            list(m.supported_instruments) if m.supported_instruments is not None else None
        ),
        supported_sessions=(
            [s.value for s in m.supported_sessions] if m.supported_sessions is not None else None
        ),
        expected_regimes=(list(m.expected_regimes) if m.expected_regimes is not None else None),
        health=Health(
            calls=h.calls,
            faults=h.faults,
            timeouts=h.timeouts,
            fault_rate=f"{h.fault_rate:.4f}",
            mean_micros=f"{h.mean_micros:.1f}",
            last_fault=h.last_fault,
            last_fault_at=h.last_fault_at.isoformat() if h.last_fault_at else None,
        ),
    )


@router.get("", response_model=RegistryOut, summary="Registered strategies")
async def index() -> RegistryOut:
    registry = build_registry()
    registrations = registry.all()

    counts = dict.fromkeys(LifecycleStatus, 0)
    for r in registrations:
        counts[r.status] += 1

    return RegistryOut(
        strategies=[_to_out(r) for r in registrations],
        # Reported in funnel order, including empty stages: a funnel showing only
        # the populated stages hides that nothing has been promoted.
        funnel=[
            FunnelStage(
                status=status.value,
                count=counts[status],
                runnable=status
                in {LifecycleStatus.CANDIDATE, LifecycleStatus.PAPER, LifecycleStatus.ACTIVE},
            )
            for status in (
                LifecycleStatus.REGISTERED,
                LifecycleStatus.CANDIDATE,
                LifecycleStatus.PAPER,
                LifecycleStatus.ACTIVE,
                LifecycleStatus.RETIRED,
                LifecycleStatus.QUARANTINED,
            )
        ],
        notice=(
            "Hundreds may be registered; only a handful should ever be active. "
            "Evidence, not compute, is the binding constraint."
        ),
    )
