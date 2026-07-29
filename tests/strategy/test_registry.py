"""Plugin isolation. One bad strategy must not affect the other forty-nine."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_features import DEFAULT_CLASSIFIER
from nemonis_marketdata import BarView, SyntheticGenerator
from nemonis_schemas.enums import Direction, Session
from nemonis_strategy.plugin import (
    LifecycleStatus,
    NoAction,
    Signal,
    StrategyContext,
    StrategyManifest,
)
from nemonis_strategy.registry import RegistryError, StrategyRegistry, signals_from

START = datetime(2026, 7, 27, tzinfo=UTC)


@pytest.fixture
def bars():
    return SyntheticGenerator("EURUSD", seed=31).generate_list(START, 300)


@pytest.fixture
def ctx(bars) -> StrategyContext:
    view = BarView(bars, 200)
    return StrategyContext(
        view=view,
        features={"return_5": Decimal("0.001"), "atr_14": Decimal("0.0006")},
        regime=DEFAULT_CLASSIFIER.classify(view),
        instrument="EURUSD",
        session=Session.LONDON,
        decision_time=view.decision_time,
    )


def manifest(sid: str = "test", **kw) -> StrategyManifest:
    base = {
        "id": sid,
        "version": "0.1.0",
        "author": "HUMAN",
        "hypothesis": "Testing the registry, not the market.",
        "required_features": ("return_5",),
        "lookback_bars": 20,
    }
    return StrategyManifest(**{**base, **kw})


class GoodStrategy:
    def __init__(self, sid: str = "good", **kw) -> None:
        self.manifest = manifest(sid, **kw)

    def generate(self, ctx: StrategyContext) -> Signal:
        entry = ctx.view.current.ask_open
        return Signal(
            strategy_id=self.manifest.id,
            strategy_version=self.manifest.version,
            instrument=ctx.instrument,
            direction=Direction.LONG,
            decision_time=ctx.decision_time,
            entry=entry,
            stop=entry - Decimal("0.00300"),
            target=entry + Decimal("0.00900"),
            confidence=Decimal("0.70"),
            setup_type="test",
        )


class ExplodingStrategy:
    def __init__(self, sid: str = "boom") -> None:
        self.manifest = manifest(sid)

    def generate(self, ctx: StrategyContext) -> Signal:
        raise RuntimeError("deliberate failure")


class SlowStrategy:
    def __init__(self, sid: str = "slow") -> None:
        self.manifest = manifest(sid)

    def generate(self, ctx: StrategyContext) -> NoAction:
        time.sleep(0.02)
        return NoAction("slow but harmless")


class BadContractStrategy:
    def __init__(self, sid: str = "bad") -> None:
        self.manifest = manifest(sid)

    def generate(self, ctx: StrategyContext):
        return "not a signal"  # type: ignore[return-value]


class NonDeterministicStrategy:
    def __init__(self, sid: str = "random") -> None:
        self.manifest = manifest(sid)
        self._n = 0

    def generate(self, ctx: StrategyContext) -> NoAction:
        self._n += 1
        return NoAction(f"call {self._n}")


class TestManifestContract:
    def test_hypothesis_is_mandatory(self) -> None:
        """'It backtested well' is not a hypothesis."""
        with pytest.raises(ValueError, match="no hypothesis"):
            manifest(hypothesis="   ")

    def test_lookback_must_be_declared(self) -> None:
        with pytest.raises(ValueError, match="lookback"):
            manifest(lookback_bars=0)

    def test_unknown_required_feature_is_refused_at_registration(self) -> None:
        """Surfacing at start-up beats surfacing three hours into a backtest."""
        registry = StrategyRegistry(available_features={"return_5", "atr_14"})
        with pytest.raises(RegistryError, match="features that do not exist"):
            registry.register(GoodStrategy(required_features=("no_such_feature",)))

    def test_duplicate_version_is_refused(self) -> None:
        registry = StrategyRegistry()
        registry.register(GoodStrategy("dup"))
        with pytest.raises(RegistryError, match="already registered"):
            registry.register(GoodStrategy("dup"))


class TestStrategiesCannotSeeTheAccount:
    def test_context_exposes_no_account_state(self, ctx) -> None:
        """The omission that keeps sizing out of strategy logic."""
        fields = set(StrategyContext.__dataclass_fields__)
        for forbidden in ("account", "balance", "equity", "portfolio", "positions"):
            assert forbidden not in fields

    def test_signal_cannot_express_a_lot_size(self) -> None:
        fields = set(Signal.__dataclass_fields__)
        for forbidden in ("lots", "size", "size_lots", "quantity", "volume"):
            assert forbidden not in fields


class TestSignalValidation:
    def test_wrong_side_stop_is_refused(self, ctx) -> None:
        with pytest.raises(ValueError, match="not below entry"):
            Signal(
                strategy_id="x",
                strategy_version="1",
                instrument="EURUSD",
                direction=Direction.LONG,
                decision_time=ctx.decision_time,
                entry=Decimal("1.08"),
                stop=Decimal("1.09"),
                target=None,
                confidence=Decimal("0.5"),
                setup_type="t",
            )

    def test_confidence_outside_unit_interval_is_refused(self, ctx) -> None:
        with pytest.raises(ValueError, match="outside"):
            Signal(
                strategy_id="x",
                strategy_version="1",
                instrument="EURUSD",
                direction=Direction.LONG,
                decision_time=ctx.decision_time,
                entry=Decimal("1.08"),
                stop=Decimal("1.07"),
                target=None,
                confidence=Decimal("1.5"),
                setup_type="t",
            )


class TestFaultContainment:
    def test_an_exception_does_not_escape(self, ctx) -> None:
        registry = StrategyRegistry()
        reg = registry.register(ExplodingStrategy(), status=LifecycleStatus.ACTIVE)
        outcome = registry.generate_one(reg, ctx)
        assert outcome.faulted
        assert isinstance(outcome.result, NoAction)
        assert "deliberate failure" in (outcome.fault_detail or "")

    def test_one_bad_strategy_does_not_stop_the_others(self, ctx) -> None:
        """The property that matters once dozens are running."""
        registry = StrategyRegistry()
        registry.register(ExplodingStrategy("boom"), status=LifecycleStatus.ACTIVE)
        registry.register(GoodStrategy("fine"), status=LifecycleStatus.ACTIVE)

        outcomes = registry.generate_all({"EURUSD": ctx})
        assert len(outcomes) == 2
        assert len(signals_from(outcomes)) == 1

    def test_repeated_faults_quarantine(self, ctx) -> None:
        registry = StrategyRegistry(fault_threshold=3)
        reg = registry.register(ExplodingStrategy(), status=LifecycleStatus.ACTIVE)
        for _ in range(3):
            registry.generate_one(reg, ctx)
        assert reg.status is LifecycleStatus.QUARANTINED
        assert reg.quarantine_reason is not None

    def test_a_quarantined_strategy_stops_running(self, ctx) -> None:
        registry = StrategyRegistry(fault_threshold=2)
        registry.register(ExplodingStrategy("boom"), status=LifecycleStatus.ACTIVE)
        for _ in range(3):
            registry.generate_all({"EURUSD": ctx})
        assert registry.runnable() == ()

    def test_faults_are_recorded_as_research_data(self, ctx) -> None:
        """A strategy that throws only in high volatility is telling you
        something about its assumptions."""
        registry = StrategyRegistry()
        reg = registry.register(ExplodingStrategy(), status=LifecycleStatus.ACTIVE)
        registry.generate_one(reg, ctx)
        assert reg.health.faults == 1
        assert reg.health.last_fault_at == ctx.decision_time
        assert reg.health.fault_rate == 1.0


class TestContractViolation:
    def test_returning_the_wrong_type_is_a_fault(self, ctx) -> None:
        registry = StrategyRegistry()
        reg = registry.register(BadContractStrategy(), status=LifecycleStatus.ACTIVE)
        outcome = registry.generate_one(reg, ctx)
        assert outcome.faulted
        assert "not Signal or NoAction" in (outcome.fault_detail or "")


class TestTimeBudget:
    def test_a_slow_strategy_is_flagged(self, ctx) -> None:
        registry = StrategyRegistry(time_budget_micros=1_000)
        reg = registry.register(SlowStrategy(), status=LifecycleStatus.ACTIVE)
        outcome = registry.generate_one(reg, ctx)
        assert outcome.timed_out
        assert reg.health.timeouts == 1

    def test_repeated_overruns_quarantine(self, ctx) -> None:
        registry = StrategyRegistry(time_budget_micros=1_000, fault_threshold=2)
        reg = registry.register(SlowStrategy(), status=LifecycleStatus.ACTIVE)
        registry.generate_one(reg, ctx)
        registry.generate_one(reg, ctx)
        assert reg.status is LifecycleStatus.QUARANTINED

    def test_a_slow_strategy_still_returns_its_result(self, ctx) -> None:
        """Enforcement is after the fact, not preemptive — interrupting a plugin
        would make output depend on scheduling."""
        registry = StrategyRegistry(time_budget_micros=1_000)
        reg = registry.register(SlowStrategy(), status=LifecycleStatus.ACTIVE)
        outcome = registry.generate_one(reg, ctx)
        assert isinstance(outcome.result, NoAction)
        assert not outcome.faulted


class TestDeterminismVerification:
    def test_a_deterministic_plugin_passes(self, ctx) -> None:
        registry = StrategyRegistry()
        reg = registry.register(GoodStrategy(), status=LifecycleStatus.ACTIVE)
        assert registry.verify_determinism(reg, ctx)

    def test_a_varying_plugin_is_caught(self, ctx) -> None:
        registry = StrategyRegistry()
        reg = registry.register(NonDeterministicStrategy(), status=LifecycleStatus.ACTIVE)
        assert not registry.verify_determinism(reg, ctx)


class TestCapabilityFiltersVersusPriors:
    def test_unsupported_instrument_is_filtered(self, ctx) -> None:
        registry = StrategyRegistry()
        registry.register(
            GoodStrategy(supported_instruments=("GBPJPY",)), status=LifecycleStatus.ACTIVE
        )
        assert registry.generate_all({"EURUSD": ctx}) == ()

    def test_unsupported_session_is_filtered(self, ctx) -> None:
        registry = StrategyRegistry()
        registry.register(
            GoodStrategy(supported_sessions=(Session.TOKYO,)), status=LifecycleStatus.ACTIVE
        )
        assert registry.generate_all({"EURUSD": ctx}) == ()

    def test_expected_regime_is_a_prior_not_a_filter(self, ctx) -> None:
        """Filtering on a belief makes it unfalsifiable and suppresses exactly
        the signals that would show the author was wrong."""
        registry = StrategyRegistry()
        registry.register(
            GoodStrategy(expected_regimes=("TRENDING/HIGH",)), status=LifecycleStatus.ACTIVE
        )
        outcomes = registry.generate_all({"EURUSD": ctx})
        assert len(outcomes) == 1  # ran regardless of the actual regime

    def test_insufficient_history_is_skipped(self, bars) -> None:
        registry = StrategyRegistry()
        registry.register(GoodStrategy(lookback_bars=100), status=LifecycleStatus.ACTIVE)
        view = BarView(bars, 5)
        cold = StrategyContext(
            view=view,
            features={},
            regime=DEFAULT_CLASSIFIER.classify(view),
            instrument="EURUSD",
            session=Session.LONDON,
            decision_time=view.decision_time,
        )
        assert registry.generate_all({"EURUSD": cold}) == ()


class TestLifecycleFunnel:
    def test_registered_strategies_do_not_run(self, ctx) -> None:
        """Hundreds may be registered; only a handful should be active."""
        registry = StrategyRegistry()
        registry.register(GoodStrategy())
        assert registry.runnable() == ()

    def test_promotion_makes_a_strategy_runnable(self, ctx) -> None:
        registry = StrategyRegistry()
        reg = registry.register(GoodStrategy("s"))
        registry.promote(reg.manifest.key, LifecycleStatus.ACTIVE)
        assert len(registry.runnable()) == 1

    def test_retirement_is_terminal(self) -> None:
        registry = StrategyRegistry()
        reg = registry.register(GoodStrategy("s"))
        registry.promote(reg.manifest.key, LifecycleStatus.RETIRED)
        with pytest.raises(RegistryError, match="human decision"):
            registry.promote(reg.manifest.key, LifecycleStatus.ACTIVE)

    def test_a_quarantined_strategy_cannot_be_promoted(self, ctx) -> None:
        registry = StrategyRegistry(fault_threshold=1)
        reg = registry.register(ExplodingStrategy("boom"), status=LifecycleStatus.ACTIVE)
        registry.generate_one(reg, ctx)
        with pytest.raises(RegistryError, match="QUARANTINED"):
            registry.promote(reg.manifest.key, LifecycleStatus.ACTIVE)


class TestDeterministicOrdering:
    def test_outcomes_are_ordered_by_strategy_key(self, ctx) -> None:
        """Replay must produce outcomes in a stable sequence regardless of
        registration order."""
        registry = StrategyRegistry()
        for sid in ("zebra", "alpha", "mike"):
            registry.register(GoodStrategy(sid), status=LifecycleStatus.ACTIVE)
        keys = [o.strategy_key for o in registry.generate_all({"EURUSD": ctx})]
        assert keys == sorted(keys)
