"""Task routing. Every terminal state must be safe."""

from __future__ import annotations

from dataclasses import replace

import pytest
from nemonis_router.registry import (
    CLOUD_TASKS,
    DEFAULT_MODELS,
    LOCAL_TASKS,
    CostClass,
    ModelRegistry,
    ModelSpec,
    Privacy,
    Provider,
    RoutingError,
    Task,
)


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry()


class TestPermittedTasksIsAnAllowlist:
    def test_the_local_worker_cannot_be_given_deep_analysis(self, registry) -> None:
        """A 3B model handed batch analysis would do it badly and confidently."""
        worker = registry.get("local-worker")
        for task in CLOUD_TASKS:
            assert not worker.permits(task), task.value

    def test_the_local_worker_handles_shallow_tasks(self, registry) -> None:
        worker = registry.get("local-worker")
        for task in LOCAL_TASKS:
            assert worker.permits(task), task.value

    def test_the_embedding_model_only_embeds(self, registry) -> None:
        embed = registry.get("local-embed")
        assert embed.permits(Task.EMBED)
        assert not embed.permits(Task.CRITIQUE_PROPOSAL)
        assert not embed.permits(Task.TAG_NOTE)

    def test_an_unknown_key_raises(self, registry) -> None:
        with pytest.raises(RoutingError, match="No model registered"):
            registry.get("no-such-model")


class TestCloudIsDisabledByDefault:
    def test_no_cloud_model_is_enabled(self, registry) -> None:
        """Enabling one sends data off the machine — a deliberate act."""
        for model in registry.all():
            if not model.is_local:
                assert not model.enabled, model.key

    def test_local_models_are_enabled(self, registry) -> None:
        assert registry.get("local-worker").enabled
        assert registry.get("local-embed").enabled

    def test_the_large_local_model_is_disabled_with_a_reason(self, registry) -> None:
        large = registry.get("local-worker-large")
        assert not large.enabled
        assert "8 GB" in large.note

    def test_cloud_models_are_marked_redacted_only(self, registry) -> None:
        for model in registry.all():
            if not model.is_local:
                assert model.privacy is Privacy.REDACTED_ONLY, model.key

    def test_local_models_are_unrestricted(self, registry) -> None:
        for model in registry.all():
            if model.is_local:
                assert model.privacy is Privacy.UNRESTRICTED, model.key


class TestRouting:
    def test_a_local_task_routes_to_the_local_worker(self, registry) -> None:
        decision = registry.route(Task.TAG_NOTE)
        assert decision.routed
        assert decision.model is not None
        assert decision.model.key == "local-worker"

    def test_local_is_preferred_over_cloud(self, registry) -> None:
        """Free, private, and no cost cap to worry about."""
        enabled = ModelRegistry(
            tuple(m if m.is_local else replace(m, enabled=True) for m in DEFAULT_MODELS)
        )
        decision = enabled.route(Task.CLASSIFY_CONTEXT)
        assert decision.model is not None
        assert decision.model.is_local

    def test_a_cloud_task_degrades_when_cloud_is_disabled(self, registry) -> None:
        """The critical safe state: the deterministic path continues."""
        decision = registry.route(Task.CRITIQUE_PROPOSAL)
        assert not decision.routed
        assert decision.degraded
        assert "deterministic path continues" in decision.reason

    def test_degradation_records_why_each_candidate_was_rejected(self, registry) -> None:
        decision = registry.route(Task.ANALYSE_TRADE_BATCH)
        assert decision.rejected
        assert any("disabled" in reason for _, reason in decision.rejected)

    def test_an_unconfigured_provider_is_not_available(self) -> None:
        """A model with no API key is unavailable regardless of its enabled flag —
        pretending otherwise produces a runtime failure instead of a clean
        degradation."""
        enabled = ModelRegistry(tuple(replace(m, enabled=True) for m in DEFAULT_MODELS))
        decision = enabled.route(Task.CRITIQUE_PROPOSAL, available_keys=frozenset({"local-worker"}))
        assert not decision.routed
        assert any("not configured" in reason for _, reason in decision.rejected)

    def test_high_cost_requires_confirmation(self) -> None:
        enabled = ModelRegistry(tuple(replace(m, enabled=True) for m in DEFAULT_MODELS))
        blocked = enabled.route(
            Task.PROPOSE_HYPOTHESES,
            available_keys=frozenset({"cloud-escalation"}),
            allow_high_cost=False,
        )
        assert not blocked.routed
        assert any("high cost" in reason for _, reason in blocked.rejected)

        allowed = enabled.route(
            Task.PROPOSE_HYPOTHESES,
            available_keys=frozenset({"cloud-escalation"}),
            allow_high_cost=True,
        )
        assert allowed.routed

    def test_an_exhausted_budget_blocks_cloud_but_not_local(self) -> None:
        enabled = ModelRegistry(tuple(replace(m, enabled=True) for m in DEFAULT_MODELS))
        cloud = enabled.route(
            Task.REVIEW_WEEKLY,
            available_keys=frozenset({"cloud-reasoning"}),
            budget_exhausted=True,
        )
        assert not cloud.routed

        local = enabled.route(Task.TAG_NOTE, budget_exhausted=True)
        assert local.routed

    def test_a_task_no_model_permits_degrades(self) -> None:
        minimal = ModelRegistry(
            (
                ModelSpec(
                    key="only-embed",
                    provider=Provider.OLLAMA,
                    model_id="nomic-embed-text",
                    permitted_tasks=frozenset({Task.EMBED}),
                ),
            )
        )
        decision = minimal.route(Task.TAG_NOTE)
        assert not decision.routed
        assert "No model permits" in decision.reason


class TestFallbackChains:
    def test_a_chain_is_walked(self, registry) -> None:
        chain = registry.fallback_chain("cloud-escalation")
        keys = [m.key for m in chain]
        assert keys[0] == "cloud-escalation"
        assert "cloud-reasoning" in keys
        assert keys[-1] == "local-worker"

    def test_a_cycle_terminates(self) -> None:
        """A misconfigured registry must not hang."""
        looped = ModelRegistry(
            (
                ModelSpec(
                    key="a",
                    provider=Provider.OLLAMA,
                    model_id="x",
                    permitted_tasks=frozenset({Task.TAG_NOTE}),
                    fallback="b",
                ),
                ModelSpec(
                    key="b",
                    provider=Provider.OLLAMA,
                    model_id="y",
                    permitted_tasks=frozenset({Task.TAG_NOTE}),
                    fallback="a",
                ),
            )
        )
        assert len(looped.fallback_chain("a")) == 2

    def test_a_dangling_fallback_stops_cleanly(self) -> None:
        dangling = ModelRegistry(
            (
                ModelSpec(
                    key="a",
                    provider=Provider.OLLAMA,
                    model_id="x",
                    permitted_tasks=frozenset({Task.TAG_NOTE}),
                    fallback="missing",
                ),
            )
        )
        assert [m.key for m in dangling.fallback_chain("a")] == ["a"]


class TestNoLLMMode:
    def test_every_task_degrades_cleanly_with_nothing_enabled(self) -> None:
        """NEMONIS_OLLAMA_ENABLED=false with no keys is a supported
        configuration, not a broken one."""
        nothing = ModelRegistry(tuple(replace(m, enabled=False) for m in DEFAULT_MODELS))
        for task in Task:
            decision = nothing.route(task)
            assert not decision.routed
            assert decision.degraded

    def test_no_task_raises_when_unroutable(self, registry) -> None:
        """Routing failure must never interrupt the deterministic pipeline."""
        for task in Task:
            registry.route(task)  # must not raise


class TestRegistryIntegrity:
    def test_every_model_has_a_unique_key(self) -> None:
        keys = [m.key for m in DEFAULT_MODELS]
        assert len(keys) == len(set(keys))

    def test_every_free_model_is_local(self) -> None:
        for model in DEFAULT_MODELS:
            if model.cost_class is CostClass.FREE:
                assert model.is_local, model.key

    def test_no_model_identifier_is_empty(self) -> None:
        for model in DEFAULT_MODELS:
            assert model.model_id
            assert model.permitted_tasks
