"""Model registry and task routing (model-routing.md §2–3).

No model identifier is hard-coded anywhere else. Every field here is
load-bearing: ``permitted_tasks`` is an allowlist, so a cheap local model can
never be handed a task it will do badly, and an expensive one is never invoked
casually.

Every terminal state of the routing decision is safe. Failure, timeout, invalid
schema, exhausted budget and missing provider all converge on the deterministic
system continuing without AI input. Nothing blocks on a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class Task(StrEnum):
    """Every task a model may be asked to perform.

    A task absent from a model's ``permitted_tasks`` cannot be routed to it,
    whatever the caller passes.
    """

    # Local worker — high volume, shallow.
    TAG_NOTE = "tag_note"
    SUMMARISE_TRADE = "summarise_trade"
    EXTRACT_ENTITIES = "extract_entities"
    GENERATE_LINKS = "generate_links"
    DRAFT_DAILY_SUMMARY = "draft_daily_summary"
    CLASSIFY_CONTEXT = "classify_context"
    EMBED = "embed"

    # Cloud reasoning — low volume, deep.
    CRITIQUE_PROPOSAL = "critique_proposal"
    ANALYSE_TRADE_BATCH = "analyse_trade_batch"
    COMPARE_REGIMES = "compare_regimes"
    ANALYSE_DEGRADATION = "analyse_degradation"
    PROPOSE_HYPOTHESES = "propose_hypotheses"
    REVIEW_WEEKLY = "review_weekly"


class Provider(StrEnum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class CostClass(StrEnum):
    FREE = "free"
    STANDARD = "standard"
    #: Requires explicit per-call confirmation.
    HIGH = "high"


class Privacy(StrEnum):
    #: Never leaves the machine.
    UNRESTRICTED = "unrestricted"
    #: May leave the machine, but only after redaction.
    REDACTED_ONLY = "redacted_only"


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    provider: Provider
    model_id: str
    permitted_tasks: frozenset[Task]
    cost_class: CostClass = CostClass.FREE
    privacy: Privacy = Privacy.UNRESTRICTED
    is_local: bool = True
    timeout_s: int = 120
    retries: int = 2
    max_context: int = 8192
    structured_output: str = "json_mode"
    enabled: bool = True
    fallback: str | None = None
    dimensions: int | None = None
    note: str = ""

    def permits(self, task: Task) -> bool:
        return task in self.permitted_tasks


LOCAL_TASKS: Final[frozenset[Task]] = frozenset(
    {
        Task.TAG_NOTE,
        Task.SUMMARISE_TRADE,
        Task.EXTRACT_ENTITIES,
        Task.GENERATE_LINKS,
        Task.DRAFT_DAILY_SUMMARY,
        Task.CLASSIFY_CONTEXT,
    }
)

CLOUD_TASKS: Final[frozenset[Task]] = frozenset(
    {
        Task.CRITIQUE_PROPOSAL,
        Task.ANALYSE_TRADE_BATCH,
        Task.COMPARE_REGIMES,
        Task.ANALYSE_DEGRADATION,
        Task.PROPOSE_HYPOTHESES,
        Task.REVIEW_WEEKLY,
    }
)


#: The default registry. Cloud entries are present but disabled — enabling them
#: sends data off this machine, which is a deliberate act (security.md §3).
DEFAULT_MODELS: Final[tuple[ModelSpec, ...]] = (
    ModelSpec(
        key="local-worker",
        provider=Provider.OLLAMA,
        model_id="llama3.2:3b",
        permitted_tasks=LOCAL_TASKS,
        cost_class=CostClass.FREE,
        privacy=Privacy.UNRESTRICTED,
        is_local=True,
        timeout_s=120,
        max_context=131072,
        note="3B. Adequate for tagging and summarising; weak at critique.",
    ),
    ModelSpec(
        key="local-embed",
        provider=Provider.OLLAMA,
        model_id="nomic-embed-text",
        permitted_tasks=frozenset({Task.EMBED}),
        cost_class=CostClass.FREE,
        privacy=Privacy.UNRESTRICTED,
        is_local=True,
        dimensions=768,
        timeout_s=60,
    ),
    ModelSpec(
        key="local-worker-large",
        provider=Provider.OLLAMA,
        model_id="qwen3:8b",
        permitted_tasks=LOCAL_TASKS | {Task.CRITIQUE_PROPOSAL},
        is_local=True,
        enabled=False,
        fallback="local-worker",
        note="5.8 GB on an 8 GB machine — see ADR-0003. Enable with more RAM.",
    ),
    ModelSpec(
        key="cloud-reasoning",
        provider=Provider.ANTHROPIC,
        model_id="claude-sonnet-5",
        permitted_tasks=CLOUD_TASKS,
        cost_class=CostClass.STANDARD,
        privacy=Privacy.REDACTED_ONLY,
        is_local=False,
        timeout_s=90,
        retries=1,
        structured_output="json_schema",
        enabled=False,
        fallback="local-worker",
        note="Requires ANTHROPIC_API_KEY. Enabling sends redacted data off-machine.",
    ),
    ModelSpec(
        key="cloud-escalation",
        provider=Provider.ANTHROPIC,
        model_id="claude-opus-4-8",
        permitted_tasks=frozenset(
            {Task.PROPOSE_HYPOTHESES, Task.ANALYSE_DEGRADATION, Task.REVIEW_WEEKLY}
        ),
        cost_class=CostClass.HIGH,
        privacy=Privacy.REDACTED_ONLY,
        is_local=False,
        enabled=False,
        fallback="cloud-reasoning",
        note="High cost — requires explicit per-call confirmation.",
    ),
)


class RoutingError(RuntimeError):
    """A task could not be routed."""


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """Why a task was routed where it was. Recorded for audit."""

    task: Task
    model: ModelSpec | None
    #: Keys considered and rejected, with reasons.
    rejected: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    degraded: bool = False
    reason: str = ""

    @property
    def routed(self) -> bool:
        return self.model is not None


class ModelRegistry:
    """Holds model specs and resolves a task to a model."""

    def __init__(self, models: tuple[ModelSpec, ...] = DEFAULT_MODELS) -> None:
        self._models: dict[str, ModelSpec] = {m.key: m for m in models}

    def get(self, key: str) -> ModelSpec:
        try:
            return self._models[key]
        except KeyError:
            raise RoutingError(f"No model registered as {key!r}") from None

    def all(self) -> tuple[ModelSpec, ...]:
        return tuple(self._models.values())

    def enabled(self) -> tuple[ModelSpec, ...]:
        return tuple(m for m in self._models.values() if m.enabled)

    def route(
        self,
        task: Task,
        *,
        available_keys: frozenset[str] | None = None,
        allow_high_cost: bool = False,
        budget_exhausted: bool = False,
    ) -> RoutingDecision:
        """Resolve a task to a model, or degrade.

        ``available_keys`` restricts to providers that are actually configured —
        a model with no API key is not available regardless of its ``enabled``
        flag, and pretending otherwise would produce a runtime failure instead of
        a clean degradation.
        """
        rejected: list[tuple[str, str]] = []

        candidates = [m for m in self._models.values() if m.permits(task)]
        if not candidates:
            return RoutingDecision(
                task=task,
                model=None,
                degraded=True,
                reason=f"No model permits {task.value}",
            )

        # Prefer local: free, private, and no cost cap to worry about.
        candidates.sort(key=lambda m: (not m.is_local, m.cost_class is CostClass.HIGH))

        for model in candidates:
            if not model.enabled:
                rejected.append((model.key, "disabled"))
                continue
            if available_keys is not None and model.key not in available_keys:
                rejected.append((model.key, "provider not configured"))
                continue
            if model.cost_class is CostClass.HIGH and not allow_high_cost:
                rejected.append((model.key, "high cost requires explicit confirmation"))
                continue
            if not model.is_local and budget_exhausted:
                rejected.append((model.key, "daily cost cap reached"))
                continue
            return RoutingDecision(task=task, model=model, rejected=tuple(rejected))

        return RoutingDecision(
            task=task,
            model=None,
            rejected=tuple(rejected),
            degraded=True,
            reason=(
                f"No available model for {task.value}; the deterministic path "
                f"continues without AI input."
            ),
        )

    def fallback_chain(self, key: str, *, limit: int = 5) -> tuple[ModelSpec, ...]:
        """Walk the fallback chain, stopping on a cycle.

        A misconfigured registry could point two models at each other; that must
        terminate rather than hang.
        """
        chain: list[ModelSpec] = []
        seen: set[str] = set()
        current: str | None = key
        while current and current not in seen and len(chain) < limit:
            seen.add(current)
            try:
                model = self.get(current)
            except RoutingError:
                break
            chain.append(model)
            current = model.fallback
        return tuple(chain)
