"""Strategy registry and isolation (strategy-platform.md §3.2).

With dozens of plugins running, one misbehaving strategy must not affect anything
else. Four isolation properties:

* **Faults are contained.** An exception is caught, recorded against that
  strategy, and the loop continues. Repeated faults quarantine it.
* **Time is budgeted.** A slow strategy is charged a fault and quarantined on
  repetition.
* **State is private.** Plugins receive a context object, never the engine.
* **Determinism is verified.** Plugins declaring ``deterministic=True`` are
  spot-checked by re-running a bar and comparing.

Faults are first-class research data, not just operational noise. A strategy that
throws only on high-volatility bars is telling you something real about its
assumptions.

**On time budgets.** Enforcement is *after the fact*: elapsed time is measured
and a repeat offender is quarantined. It is deliberately not preemptive.
Interrupting a running plugin needs threads or signals, both of which would make
the engine's output depend on scheduling — and a backtest whose results shift
with CPU load is worthless. A slow strategy is a correctness problem for its
author, not a reason to sacrifice determinism for everyone.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from nemonis_strategy.plugin import (
    LifecycleStatus,
    NoAction,
    Signal,
    StrategyContext,
    StrategyManifest,
    StrategyPlugin,
    StrategyResult,
)


class RegistryError(RuntimeError):
    """A strategy could not be registered."""


@dataclass(slots=True)
class StrategyHealth:
    """Operational record for one registered strategy."""

    faults: int = 0
    timeouts: int = 0
    calls: int = 0
    last_fault: str | None = None
    last_fault_at: datetime | None = None
    total_micros: int = 0

    @property
    def mean_micros(self) -> float:
        return self.total_micros / self.calls if self.calls else 0.0

    @property
    def fault_rate(self) -> float:
        return self.faults / self.calls if self.calls else 0.0


@dataclass(slots=True)
class Registration:
    plugin: StrategyPlugin
    status: LifecycleStatus = LifecycleStatus.REGISTERED
    health: StrategyHealth = field(default_factory=StrategyHealth)
    quarantine_reason: str | None = None

    @property
    def manifest(self) -> StrategyManifest:
        return self.plugin.manifest

    @property
    def is_runnable(self) -> bool:
        return self.status in {
            LifecycleStatus.CANDIDATE,
            LifecycleStatus.PAPER,
            LifecycleStatus.ACTIVE,
        }


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """One strategy's result for one bar, including how it failed if it did."""

    strategy_key: str
    result: StrategyResult
    micros: int
    faulted: bool = False
    fault_detail: str | None = None
    timed_out: bool = False

    @property
    def signal(self) -> Signal | None:
        return self.result if isinstance(self.result, Signal) else None


class StrategyRegistry:
    """Holds plugins, enforces isolation, runs them over a bar."""

    def __init__(
        self,
        *,
        available_features: Iterable[str] = (),
        fault_threshold: int = 3,
        time_budget_micros: int = 50_000,
    ) -> None:
        self._registrations: dict[str, Registration] = {}
        self._available_features = set(available_features)
        self.fault_threshold = fault_threshold
        self.time_budget_micros = time_budget_micros

    # -- Registration ------------------------------------------------------

    def register(
        self, plugin: StrategyPlugin, *, status: LifecycleStatus = LifecycleStatus.REGISTERED
    ) -> Registration:
        """Register a plugin, validating its manifest against the platform.

        Validation happens here rather than at first call so a broken manifest
        surfaces at start-up, not three hours into a backtest.
        """
        manifest = plugin.manifest
        key = manifest.key

        if key in self._registrations:
            raise RegistryError(
                f"{key} is already registered. Every change to a strategy is a new "
                f"version — reusing a version would make historical results "
                f"ambiguous about which code produced them."
            )

        if self._available_features:
            unknown = set(manifest.required_features) - self._available_features
            if unknown:
                raise RegistryError(
                    f"{key} requires features that do not exist: {sorted(unknown)}. "
                    f"Register the feature or correct the manifest."
                )

        registration = Registration(plugin=plugin, status=status)
        self._registrations[key] = registration
        return registration

    def get(self, key: str) -> Registration:
        try:
            return self._registrations[key]
        except KeyError:
            raise RegistryError(f"No strategy registered as {key!r}") from None

    def all(self) -> tuple[Registration, ...]:
        return tuple(self._registrations.values())

    def runnable(self) -> tuple[Registration, ...]:
        return tuple(r for r in self._registrations.values() if r.is_runnable)

    def by_status(self, status: LifecycleStatus) -> tuple[Registration, ...]:
        return tuple(r for r in self._registrations.values() if r.status is status)

    # -- Lifecycle ---------------------------------------------------------

    def promote(self, key: str, to: LifecycleStatus) -> Registration:
        """Advance a strategy through the funnel.

        Retirement is terminal here: a retired strategy is never resurrected
        automatically. Bringing one back is a human decision, because the
        conditions that retired it are exactly what an automated system would be
        worst at judging.
        """
        registration = self.get(key)
        if registration.status is LifecycleStatus.RETIRED and to is not LifecycleStatus.RETIRED:
            raise RegistryError(
                f"{key} is RETIRED. Reinstating a retired strategy is a human "
                f"decision — register a new version instead."
            )
        if registration.status is LifecycleStatus.QUARANTINED and to in {
            LifecycleStatus.ACTIVE,
            LifecycleStatus.PAPER,
        }:
            raise RegistryError(
                f"{key} is QUARANTINED after {registration.health.faults} faults "
                f"({registration.quarantine_reason}). Fix and register a new "
                f"version rather than promoting a faulting strategy."
            )
        registration.status = to
        return registration

    def quarantine(self, key: str, reason: str) -> Registration:
        registration = self.get(key)
        registration.status = LifecycleStatus.QUARANTINED
        registration.quarantine_reason = reason
        return registration

    # -- Execution ---------------------------------------------------------

    def generate_one(self, registration: Registration, ctx: StrategyContext) -> GenerationOutcome:
        """Run one plugin with fault containment and timing."""
        key = registration.manifest.key
        health = registration.health
        started = time.perf_counter_ns()

        try:
            # Deliberately typed as `object`: a plugin is arbitrary third-party
            # code and its annotation is not enforced at runtime, so the return
            # value is untrusted until checked below.
            raw: object = registration.plugin.generate(ctx)
        except Exception as exc:
            micros = (time.perf_counter_ns() - started) // 1000
            health.calls += 1
            health.faults += 1
            health.total_micros += micros
            health.last_fault = f"{type(exc).__name__}: {exc}"
            health.last_fault_at = ctx.decision_time

            if health.faults >= self.fault_threshold:
                self.quarantine(key, f"{health.faults} faults; last: {health.last_fault}")

            return GenerationOutcome(
                strategy_key=key,
                result=NoAction(reason=f"faulted: {health.last_fault}"),
                micros=micros,
                faulted=True,
                fault_detail=health.last_fault,
            )

        micros = (time.perf_counter_ns() - started) // 1000
        health.calls += 1
        health.total_micros += micros

        # Without this, a strategy returning a bare float would propagate as a
        # "signal" into the risk engine.
        if not isinstance(raw, Signal | NoAction):
            health.faults += 1
            health.last_fault = f"returned {type(raw).__name__}, not Signal or NoAction"
            if health.faults >= self.fault_threshold:
                self.quarantine(key, health.last_fault)
            return GenerationOutcome(
                strategy_key=key,
                result=NoAction(reason=f"contract violation: {health.last_fault}"),
                micros=micros,
                faulted=True,
                fault_detail=health.last_fault,
            )

        result: StrategyResult = raw
        timed_out = micros > self.time_budget_micros
        if timed_out:
            health.timeouts += 1
            if health.timeouts >= self.fault_threshold:
                self.quarantine(
                    key,
                    f"{health.timeouts} time-budget breaches "
                    f"(budget {self.time_budget_micros}µs, last {micros}µs)",
                )

        return GenerationOutcome(
            strategy_key=key, result=result, micros=micros, timed_out=timed_out
        )

    def generate_all(self, contexts: dict[str, StrategyContext]) -> tuple[GenerationOutcome, ...]:
        """Run every runnable strategy against its instrument's context.

        Ordered by strategy key so a replay produces outcomes in a stable
        sequence regardless of dict insertion order.
        """
        outcomes: list[GenerationOutcome] = []

        for registration in sorted(self.runnable(), key=lambda r: r.manifest.key):
            manifest = registration.manifest
            for instrument in sorted(contexts):
                ctx = contexts[instrument]
                if not manifest.supports_instrument(instrument):
                    continue
                if not manifest.supports_session(ctx.session):
                    continue
                if not ctx.view.has_history(manifest.lookback_bars):
                    continue
                outcomes.append(self.generate_one(registration, ctx))

        return tuple(outcomes)

    def verify_determinism(self, registration: Registration, ctx: StrategyContext) -> bool:
        """Spot-check a plugin claiming determinism by re-running one bar.

        Not a proof — one bar cannot establish it — but it catches the common
        cases: an unseeded RNG, a wall-clock read, or iteration over an unordered
        collection.
        """
        if not registration.manifest.deterministic:
            return True
        first = self.generate_one(registration, ctx)
        second = self.generate_one(registration, ctx)
        return first.result == second.result


def signals_from(outcomes: Sequence[GenerationOutcome]) -> tuple[Signal, ...]:
    """Extract the signals, discarding no-actions and faults."""
    return tuple(o.signal for o in outcomes if o.signal is not None)
