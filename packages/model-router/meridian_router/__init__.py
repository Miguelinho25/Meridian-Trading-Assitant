"""Model registry, task routing and validated AI critique.

Structurally cannot reach the risk engine — see the import-linter contract in
pyproject.toml. That is what makes "no LLM can influence sizing" a property of
the build rather than a promise.
"""

from __future__ import annotations

from meridian_router.critique import (
    CRITIQUE_SCHEMA_PROMPT,
    AICritique,
    CritiqueRejectedError,
    SimilarCaseRef,
    abstain,
    contains_injection,
    parse_critique,
    wrap_untrusted,
)
from meridian_router.provider import (
    Invocation,
    ModelProvider,
    NullProvider,
    OllamaProvider,
    ProviderHealth,
    ProviderResult,
    prompt_hash,
)
from meridian_router.registry import (
    CLOUD_TASKS,
    DEFAULT_MODELS,
    LOCAL_TASKS,
    CostClass,
    ModelRegistry,
    ModelSpec,
    Privacy,
    Provider,
    RoutingDecision,
    RoutingError,
    Task,
)
from meridian_router.service import (
    CritiqueOutcome,
    CritiqueRequest,
    CritiqueService,
    build_prompt,
)

__all__ = [
    "CLOUD_TASKS",
    "CRITIQUE_SCHEMA_PROMPT",
    "DEFAULT_MODELS",
    "LOCAL_TASKS",
    "AICritique",
    "CostClass",
    "CritiqueOutcome",
    "CritiqueRejectedError",
    "CritiqueRequest",
    "CritiqueService",
    "Invocation",
    "ModelProvider",
    "ModelRegistry",
    "ModelSpec",
    "NullProvider",
    "OllamaProvider",
    "Privacy",
    "Provider",
    "ProviderHealth",
    "ProviderResult",
    "RoutingDecision",
    "RoutingError",
    "SimilarCaseRef",
    "Task",
    "abstain",
    "build_prompt",
    "contains_injection",
    "parse_critique",
    "prompt_hash",
    "wrap_untrusted",
]
