"""Embeddings and semantic retrieval (the brief's memory layer).

Two rules govern this module.

**Vectors from different embedding versions are never compared.** Re-embedding
with a different model produces a different space; mixing them yields similarity
scores that look plausible and mean nothing. The store refuses the comparison
rather than returning a number.

**Similarity is not evidence.** Retrieval returns cases with their *actual
outcomes* and a sample size, never a bare ranked list. Ten similar trades that
all lost is the useful finding, and a UI showing only "10 similar trades found"
would hide it.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from nemonis_router.provider import ModelProvider
from nemonis_router.registry import ModelSpec

#: Bumped when chunking or normalisation changes. Part of the comparison key,
#: because a different chunking strategy is a different space too.
CHUNK_VERSION = "1.0.0"

#: Minimum cosine similarity for a case to be offered as comparable.
#:
#: Measured, not chosen. Three paraphrase queries against a seven-trade corpus
#: plus unrelated prose, embedded with nomic-embed-text:
#:
#:     same setup, paraphrased   min 0.502   mean 0.608   max 0.730
#:     different setup           min 0.387   mean 0.490   max 0.585
#:     unrelated prose           min 0.293   mean 0.357   max 0.407
#:
#: 0.45 sits in the gap between the worst correct match (0.502) and the best
#: unrelated one (0.407). An earlier value of 0.60 was derived from a single
#: query that nearly duplicated its document (0.83) and filtered out every real
#: paraphrase — a reminder that one measurement is not a distribution.
#:
#: Re-measure if the embedding model changes. This is a property of
#: nomic-embed-text, not a universal constant.
DEFAULT_MIN_RELEVANCE = Decimal("0.45")

#: Whether cosine ranking can be trusted to order *setups*. It cannot.
#:
#: In the same measurement the best wrong-setup match (0.585) outscored the
#: worst correct one (0.502) — a separation of -0.083. The model distinguishes
#: trading text from non-trading text, and little more. Ordering within a
#: retrieved set is therefore not evidence of comparability, which is why
#: metadata filtering is the primary mechanism here and similarity only breaks
#: ties inside an already-relevant population.
SIMILARITY_RANKS_SETUPS_RELIABLY = False


class EmbeddingError(RuntimeError):
    """An embedding operation was refused."""


def content_hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class EmbeddingRecord:
    """One embedded item, with everything needed to know when it is stale."""

    record_id: str
    vector: tuple[float, ...]
    #: Model that produced it. Vectors from different models are incomparable.
    embedding_model: str
    embedding_version: str
    chunk_version: str
    #: Hash of the source text, so a changed source is detectable without
    #: storing the text itself.
    source_hash: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def space(self) -> tuple[str, str, str, int]:
        """The comparison key. Only records sharing this may be compared."""
        return (
            self.embedding_model,
            self.embedding_version,
            self.chunk_version,
            len(self.vector),
        )

    def is_stale_for(self, text: str) -> bool:
        """Whether the source text has changed since this was embedded."""
        return self.source_hash != content_hash(text)


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    """Metadata filters. Applied *before* similarity, not after.

    Filtering after ranking would let a full result set be consumed by
    irrelevant instruments and return nothing usable.
    """

    instrument: str | None = None
    direction: str | None = None
    session: str | None = None
    strategy_id: str | None = None
    strategy_version: str | None = None
    regime_label: str | None = None
    timeframe: str | None = None
    #: "win" | "loss" — filter by realised outcome.
    result: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    risk_profile: str | None = None

    def matches(self, metadata: dict[str, Any]) -> bool:
        simple = {
            "instrument": self.instrument,
            "direction": self.direction,
            "session": self.session,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "regime_label": self.regime_label,
            "timeframe": self.timeframe,
            "result": self.result,
            "risk_profile": self.risk_profile,
        }
        for key, wanted in simple.items():
            if wanted is not None and str(metadata.get(key, "")) != str(wanted):
                return False

        when = metadata.get("closed_at")
        if isinstance(when, datetime):
            if self.date_from is not None and when < self.date_from:
                return False
            if self.date_to is not None and when > self.date_to:
                return False
        return True


@dataclass(frozen=True, slots=True)
class SimilarityMatch:
    record_id: str
    relevance: Decimal
    metadata: dict[str, Any]

    @property
    def outcome_r(self) -> Decimal | None:
        value = self.metadata.get("outcome_r")
        return Decimal(str(value)) if value is not None else None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Matches plus the context needed to read them honestly."""

    matches: tuple[SimilarityMatch, ...]
    #: Records that passed the metadata filter — the population searched.
    candidates_searched: int
    space: tuple[str, str, str, int] | None = None
    #: Whether a metadata filter narrowed the population. When False the set was
    #: selected by cosine similarity alone, which measurement shows cannot
    #: reliably tell one setup from another.
    narrowed_by_metadata: bool = False

    @property
    def sample_size(self) -> int:
        return len(self.matches)

    @property
    def outcomes(self) -> tuple[Decimal, ...]:
        return tuple(m.outcome_r for m in self.matches if m.outcome_r is not None)

    @property
    def win_rate(self) -> Decimal | None:
        """Realised win rate among the retrieved cases.

        The number that stops similarity being mistaken for endorsement. Ten
        similar trades that all lost is the finding.
        """
        outcomes = self.outcomes
        if not outcomes:
            return None
        wins = sum(1 for r in outcomes if r > 0)
        return Decimal(wins) / Decimal(len(outcomes))

    @property
    def mean_r(self) -> Decimal | None:
        outcomes = self.outcomes
        if not outcomes:
            return None
        return sum(outcomes, Decimal(0)) / Decimal(len(outcomes))

    @property
    def is_informative(self) -> bool:
        """Whether the sample is large enough to be worth reading.

        Below this, the retrieved set is an anecdote. Callers should present it
        as one rather than as a base rate.
        """
        return self.sample_size >= 5

    def summary(self) -> str:
        """One line, always carrying sample size and realised outcomes."""
        if not self.matches:
            return "No comparable historical cases found."

        parts = [f"{self.sample_size} of {self.candidates_searched} candidates matched."]
        rate, mean = self.win_rate, self.mean_r
        if rate is not None and mean is not None:
            parts.append(f"Win rate {rate:.0%}, mean {mean:.2f}R.")
        else:
            parts.append("No outcomes recorded.")
        if not self.is_informative:
            parts.append("Too few cases to generalise.")
        if not self.narrowed_by_metadata:
            parts.append(
                "Selected by text similarity alone, which does not reliably "
                "distinguish one setup from another — treat the grouping as "
                "unverified."
            )
        return " ".join(parts)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise EmbeddingError(
            f"Cannot compare vectors of length {len(a)} and {len(b)}. Different "
            f"dimensions mean different embedding models, and the result would be "
            f"meaningless rather than merely inaccurate."
        )
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore(Protocol):
    """Replaceable by pgvector or Qdrant without touching call sites."""

    def add(self, record: EmbeddingRecord) -> None: ...

    def search(
        self,
        query: Sequence[float],
        *,
        space: tuple[str, str, str, int],
        top_k: int = 10,
        filters: RetrievalFilter | None = None,
        min_relevance: Decimal = Decimal("0.0"),
    ) -> RetrievalResult: ...

    def __len__(self) -> int: ...


class InMemoryVectorStore:
    """Brute-force cosine search.

    Entirely adequate at research scale — 10^4 to 10^5 vectors compare in
    milliseconds, and an approximate index would add a dependency, an accuracy
    caveat and a tuning surface to solve a problem this project does not have.
    """

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: dict[str, EmbeddingRecord] = {}

    def add(self, record: EmbeddingRecord) -> None:
        self._records[record.record_id] = record

    def add_many(self, records: Iterable[EmbeddingRecord]) -> int:
        count = 0
        for record in records:
            self.add(record)
            count += 1
        return count

    def get(self, record_id: str) -> EmbeddingRecord | None:
        return self._records.get(record_id)

    def remove(self, record_id: str) -> bool:
        return self._records.pop(record_id, None) is not None

    def spaces(self) -> set[tuple[str, str, str, int]]:
        """Every embedding space present. More than one means a re-embed is due."""
        return {r.space for r in self._records.values()}

    def search(
        self,
        query: Sequence[float],
        *,
        space: tuple[str, str, str, int],
        top_k: int = 10,
        filters: RetrievalFilter | None = None,
        min_relevance: Decimal = Decimal("0.0"),
    ) -> RetrievalResult:
        """Search within one embedding space.

        Records outside ``space`` are excluded rather than converted. There is no
        meaningful conversion between embedding spaces, and comparing across them
        produces confident nonsense.

        ``min_relevance`` defaults to zero *here* because the store is a
        primitive: it reports what it finds. The relevance policy lives in
        ``EmbeddingService.retrieve``, which is what callers outside this module
        use. See ``DEFAULT_MIN_RELEVANCE``.
        """
        # Metadata filter first: filtering after ranking would let top_k be
        # consumed by irrelevant instruments and return nothing usable.
        candidates = [
            r
            for r in self._records.values()
            if r.space == space and (filters is None or filters.matches(r.metadata))
        ]

        scored: list[SimilarityMatch] = []
        for record in candidates:
            score = cosine_similarity(query, record.vector)
            relevance = Decimal(str(round(max(0.0, score), 6)))
            if relevance >= min_relevance:
                scored.append(
                    SimilarityMatch(
                        record_id=record.record_id,
                        relevance=relevance,
                        metadata=dict(record.metadata),
                    )
                )

        # Ties broken by record_id so results are reproducible.
        scored.sort(key=lambda m: (-m.relevance, m.record_id))
        return RetrievalResult(
            matches=tuple(scored[:top_k]),
            candidates_searched=len(candidates),
            space=space,
            narrowed_by_metadata=filters is not None and filters != RetrievalFilter(),
        )

    def __len__(self) -> int:
        return len(self._records)


class EmbeddingService:
    """Generates embeddings and answers retrieval queries.

    Degrades like everything else in this package: if the provider is
    unavailable, indexing and retrieval return empty rather than raising, and
    exact metadata filtering still works without any model at all.
    """

    def __init__(
        self,
        *,
        provider: ModelProvider,
        spec: ModelSpec,
        store: VectorStore | None = None,
    ) -> None:
        self.provider = provider
        self.spec = spec
        self.store = store or InMemoryVectorStore()

    @property
    def space(self) -> tuple[str, str, str, int]:
        if self.spec.dimensions is None:
            raise EmbeddingError(
                f"{self.spec.key} declares no dimensions; an embedding space "
                f"cannot be identified without them."
            )
        return (self.spec.model_id, self.spec.key, CHUNK_VERSION, self.spec.dimensions)

    async def index(
        self,
        record_id: str,
        text: str,
        *,
        created_at: datetime,
        metadata: dict[str, Any] | None = None,
    ) -> EmbeddingRecord | None:
        """Embed and store. Returns None if the provider was unavailable."""
        result = await self.provider.embed(self.spec, text)
        if not result.ok or result.embedding is None:
            return None

        record = EmbeddingRecord(
            record_id=record_id,
            vector=result.embedding,
            embedding_model=self.spec.model_id,
            embedding_version=self.spec.key,
            chunk_version=CHUNK_VERSION,
            source_hash=content_hash(text),
            created_at=created_at,
            metadata=metadata or {},
        )
        self.store.add(record)
        return record

    async def retrieve(
        self,
        query_text: str,
        *,
        top_k: int = 10,
        filters: RetrievalFilter | None = None,
        min_relevance: Decimal = DEFAULT_MIN_RELEVANCE,
    ) -> RetrievalResult:
        """Retrieve comparable cases.

        ``min_relevance`` defaults well above zero deliberately: returning the
        least-dissimilar item from a corpus with nothing relevant in it is worse
        than returning nothing, because it will be read as a comparable case.
        See ``DEFAULT_MIN_RELEVANCE`` for how the figure was chosen.
        """
        result = await self.provider.embed(self.spec, query_text)
        if not result.ok or result.embedding is None:
            return RetrievalResult(matches=(), candidates_searched=0)

        return self.store.search(
            result.embedding,
            space=self.space,
            top_k=top_k,
            filters=filters,
            min_relevance=min_relevance,
        )
