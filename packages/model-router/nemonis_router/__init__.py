"""Model registry, task routing and validated AI critique.

Structurally cannot reach the risk engine — see the import-linter contract in
pyproject.toml. That is what makes "no LLM can influence sizing" a property of
the build rather than a promise.
"""

from __future__ import annotations

from nemonis_router.critique import (
    CRITIQUE_SCHEMA_PROMPT,
    AICritique,
    CritiqueRejectedError,
    SimilarCaseRef,
    abstain,
    contains_injection,
    parse_critique,
    wrap_untrusted,
)
from nemonis_router.embeddings import (
    CHUNK_VERSION,
    EmbeddingError,
    EmbeddingRecord,
    EmbeddingService,
    InMemoryVectorStore,
    RetrievalFilter,
    RetrievalResult,
    SimilarityMatch,
    VectorStore,
    content_hash,
    cosine_similarity,
)
from nemonis_router.provider import (
    Invocation,
    ModelProvider,
    NullProvider,
    OllamaProvider,
    ProviderHealth,
    ProviderResult,
    prompt_hash,
)
from nemonis_router.registry import (
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
from nemonis_router.service import (
    CritiqueOutcome,
    CritiqueRequest,
    CritiqueService,
    build_prompt,
)

__all__ = [
    "CHUNK_VERSION",
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
    "EmbeddingError",
    "EmbeddingRecord",
    "EmbeddingService",
    "InMemoryVectorStore",
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
    "RetrievalFilter",
    "RetrievalResult",
    "RoutingDecision",
    "RoutingError",
    "SimilarCaseRef",
    "SimilarityMatch",
    "Task",
    "VectorStore",
    "abstain",
    "build_prompt",
    "contains_injection",
    "content_hash",
    "cosine_similarity",
    "parse_critique",
    "prompt_hash",
    "wrap_untrusted",
]
