"""Retrieval. Similarity must never be mistaken for endorsement."""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from meridian_router.embeddings import (
    CHUNK_VERSION,
    DEFAULT_MIN_RELEVANCE,
    SIMILARITY_RANKS_SETUPS_RELIABLY,
    EmbeddingError,
    EmbeddingRecord,
    EmbeddingService,
    InMemoryVectorStore,
    RetrievalFilter,
    content_hash,
    cosine_similarity,
)
from meridian_router.provider import NullProvider, OllamaProvider
from meridian_router.registry import ModelRegistry

LIVE = os.environ.get("MERIDIAN_TEST_OLLAMA") == "1"
EMBED = ModelRegistry().get("local-embed")
T = datetime(2026, 7, 27, tzinfo=UTC)

#: Measured against nomic-embed-text: three paraphrase queries, a seven-trade
#: corpus and unrelated prose. These are the observations DEFAULT_MIN_RELEVANCE
#: is derived from, so the tests below fail if the constant drifts off them.
WORST_CORRECT_MATCH = Decimal("0.502")
BEST_UNRELATED_MATCH = Decimal("0.407")
BEST_WRONG_SETUP = Decimal("0.585")


def a_record(
    record_id: str,
    vector: tuple[float, ...],
    *,
    model: str = "nomic-embed-text",
    version: str = "local-embed",
    **metadata,
) -> EmbeddingRecord:
    return EmbeddingRecord(
        record_id=record_id,
        vector=vector,
        embedding_model=model,
        embedding_version=version,
        chunk_version=CHUNK_VERSION,
        source_hash=content_hash(record_id),
        created_at=T,
        metadata=metadata,
    )


SPACE = ("nomic-embed-text", "local-embed", CHUNK_VERSION, 3)


def unit_record(record_id: str, cosine_to_x: Decimal, **metadata) -> EmbeddingRecord:
    """A record whose cosine similarity to (1, 0, 0) is exactly ``cosine_to_x``."""
    x = float(cosine_to_x)
    return a_record(record_id, (x, math.sqrt(max(0.0, 1.0 - x * x)), 0.0), **metadata)


class TestCosine:
    def test_identical_vectors_score_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_a_zero_vector_scores_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_mismatched_dimensions_are_refused(self) -> None:
        """Different dimensions mean different models — the result would be
        meaningless rather than merely inaccurate."""
        with pytest.raises(EmbeddingError, match="meaningless"):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])


class TestSpacesAreNeverMixed:
    """Re-embedding produces a different space; mixing yields scores that look
    plausible and mean nothing."""

    def test_a_different_model_is_excluded(self) -> None:
        store = InMemoryVectorStore()
        store.add(a_record("same", (1.0, 0.0, 0.0)))
        store.add(a_record("other", (1.0, 0.0, 0.0), model="different-model"))

        result = store.search((1.0, 0.0, 0.0), space=SPACE)
        assert [m.record_id for m in result.matches] == ["same"]

    def test_a_different_chunk_version_is_excluded(self) -> None:
        store = InMemoryVectorStore()
        store.add(a_record("current", (1.0, 0.0, 0.0)))
        stale = EmbeddingRecord(
            record_id="stale",
            vector=(1.0, 0.0, 0.0),
            embedding_model="nomic-embed-text",
            embedding_version="local-embed",
            chunk_version="0.9.0",
            source_hash="x",
            created_at=T,
            metadata={},
        )
        store.add(stale)
        result = store.search((1.0, 0.0, 0.0), space=SPACE)
        assert [m.record_id for m in result.matches] == ["current"]

    def test_multiple_spaces_are_visible(self) -> None:
        """More than one space present means a re-embed is due."""
        store = InMemoryVectorStore()
        store.add(a_record("a", (1.0, 0.0, 0.0)))
        store.add(a_record("b", (1.0, 0.0, 0.0), model="other"))
        assert len(store.spaces()) == 2


class TestStaleness:
    def test_a_changed_source_is_detected(self) -> None:
        record = EmbeddingRecord(
            record_id="r",
            vector=(1.0,),
            embedding_model="m",
            embedding_version="v",
            chunk_version=CHUNK_VERSION,
            source_hash=content_hash("original text"),
            created_at=T,
        )
        assert not record.is_stale_for("original text")
        assert record.is_stale_for("edited text")


class TestMetadataFilteringHappensFirst:
    """Filtering after ranking would let top_k be consumed by irrelevant
    instruments and return nothing usable."""

    def _store(self) -> InMemoryVectorStore:
        store = InMemoryVectorStore()
        for i in range(20):
            store.add(
                a_record(
                    f"gbp{i}",
                    (1.0, 0.0, 0.0),
                    instrument="GBPJPY",
                    direction="LONG",
                    result="win",
                )
            )
        store.add(
            a_record(
                "eur1",
                (0.9, 0.1, 0.0),
                instrument="EURUSD",
                direction="LONG",
                result="loss",
                outcome_r="-1.0",
            )
        )
        return store

    def test_the_filtered_instrument_survives_a_crowded_corpus(self) -> None:
        result = self._store().search(
            (1.0, 0.0, 0.0),
            space=SPACE,
            top_k=5,
            filters=RetrievalFilter(instrument="EURUSD"),
        )
        assert [m.record_id for m in result.matches] == ["eur1"]
        assert result.candidates_searched == 1

    @pytest.mark.parametrize(
        ("field_name", "value", "expected"),
        [
            ("instrument", "EURUSD", 1),
            ("direction", "LONG", 21),
            ("result", "loss", 1),
            ("result", "win", 20),
        ],
    )
    def test_each_filter_narrows_correctly(self, field_name, value, expected) -> None:
        result = self._store().search(
            (1.0, 0.0, 0.0),
            space=SPACE,
            top_k=100,
            filters=RetrievalFilter(**{field_name: value}),
        )
        assert result.candidates_searched == expected

    def test_a_date_range_filters(self) -> None:
        store = InMemoryVectorStore()
        store.add(a_record("old", (1.0, 0.0, 0.0), closed_at=T - timedelta(days=400)))
        store.add(a_record("new", (1.0, 0.0, 0.0), closed_at=T))
        result = store.search(
            (1.0, 0.0, 0.0),
            space=SPACE,
            filters=RetrievalFilter(date_from=T - timedelta(days=30)),
        )
        assert [m.record_id for m in result.matches] == ["new"]


class TestSimilarityIsNotEndorsement:
    def test_outcomes_are_reported_alongside_matches(self) -> None:
        """Ten similar trades that all lost is the finding."""
        store = InMemoryVectorStore()
        for i in range(10):
            store.add(a_record(f"t{i}", (1.0, 0.0, 0.0), outcome_r="-1.0"))

        result = store.search((1.0, 0.0, 0.0), space=SPACE, top_k=10)
        assert result.win_rate == Decimal(0)
        assert result.mean_r == Decimal("-1.0")

    def test_sample_size_is_always_available(self) -> None:
        store = InMemoryVectorStore()
        store.add(a_record("t1", (1.0, 0.0, 0.0), outcome_r="2.0"))
        result = store.search((1.0, 0.0, 0.0), space=SPACE)
        assert result.sample_size == 1
        assert result.candidates_searched == 1

    def test_a_tiny_sample_is_marked_uninformative(self) -> None:
        store = InMemoryVectorStore()
        for i in range(3):
            store.add(a_record(f"t{i}", (1.0, 0.0, 0.0), outcome_r="3.0"))
        result = store.search((1.0, 0.0, 0.0), space=SPACE)
        assert not result.is_informative
        assert "Too few cases to generalise" in result.summary()

    def test_a_reasonable_sample_is_informative(self) -> None:
        store = InMemoryVectorStore()
        for i in range(8):
            store.add(a_record(f"t{i}", (1.0, 0.0, 0.0), outcome_r="1.0"))
        assert store.search((1.0, 0.0, 0.0), space=SPACE).is_informative

    def test_an_empty_result_says_so_plainly(self) -> None:
        result = InMemoryVectorStore().search((1.0, 0.0, 0.0), space=SPACE)
        assert "No comparable historical cases" in result.summary()


class TestRelevanceThreshold:
    def test_irrelevant_matches_are_excluded(self) -> None:
        """Returning the least-dissimilar item from a corpus with nothing
        relevant is worse than returning nothing."""
        store = InMemoryVectorStore()
        store.add(a_record("orthogonal", (0.0, 1.0, 0.0)))
        result = store.search((1.0, 0.0, 0.0), space=SPACE, min_relevance=Decimal("0.3"))
        assert result.matches == ()

    def test_relevant_matches_survive(self) -> None:
        store = InMemoryVectorStore()
        store.add(a_record("close", (0.95, 0.05, 0.0)))
        result = store.search((1.0, 0.0, 0.0), space=SPACE, min_relevance=Decimal("0.3"))
        assert len(result.matches) == 1


class TestDeterminism:
    def test_ties_break_reproducibly(self) -> None:
        store = InMemoryVectorStore()
        for name in ("zebra", "alpha", "mike"):
            store.add(a_record(name, (1.0, 0.0, 0.0)))
        first = [m.record_id for m in store.search((1.0, 0.0, 0.0), space=SPACE).matches]
        second = [m.record_id for m in store.search((1.0, 0.0, 0.0), space=SPACE).matches]
        assert first == second == sorted(first)

    def test_ordering_is_by_descending_relevance(self) -> None:
        store = InMemoryVectorStore()
        store.add(a_record("far", (0.5, 0.5, 0.0)))
        store.add(a_record("near", (1.0, 0.0, 0.0)))
        matches = store.search((1.0, 0.0, 0.0), space=SPACE).matches
        assert [m.record_id for m in matches] == ["near", "far"]


class TestDegradation:
    async def test_indexing_returns_none_without_a_provider(self) -> None:
        service = EmbeddingService(provider=NullProvider(), spec=EMBED)
        assert await service.index("r1", "text", created_at=T) is None

    async def test_retrieval_returns_empty_without_a_provider(self) -> None:
        service = EmbeddingService(provider=NullProvider(), spec=EMBED)
        result = await service.retrieve("query")
        assert result.matches == ()
        assert result.sample_size == 0

    async def test_a_provider_failure_does_not_raise(self) -> None:
        service = EmbeddingService(
            provider=OllamaProvider(base_url="http://localhost:1"), spec=EMBED
        )
        assert await service.retrieve("query") is not None


class TestServiceIntegration:
    async def test_index_then_retrieve(self) -> None:
        vectors = {"a": [1.0] + [0.0] * 767, "b": [0.0, 1.0] + [0.0] * 766}
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            key = "a" if calls["n"] % 2 == 0 else "b"
            calls["n"] += 1
            return httpx.Response(200, json={"embedding": vectors[key]})

        provider = OllamaProvider(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        service = EmbeddingService(provider=provider, spec=EMBED)

        record = await service.index(
            "t1",
            "EURUSD long trend",
            created_at=T,
            metadata={"instrument": "EURUSD", "outcome_r": "1.5"},
        )
        assert record is not None
        assert len(record.vector) == 768

        result = await service.retrieve("EURUSD long trend", min_relevance=Decimal("0.0"))
        assert result.sample_size == 1
        assert result.matches[0].outcome_r == Decimal("1.5")


@pytest.mark.skipif(not LIVE, reason="set MERIDIAN_TEST_OLLAMA=1 to run against Ollama")
class TestAgainstRealEmbeddings:
    async def test_related_text_scores_above_unrelated(self) -> None:
        """The property the whole retrieval layer depends on."""
        service = EmbeddingService(provider=OllamaProvider(), spec=EMBED)

        await service.index(
            "trend",
            "EURUSD long trend continuation on a moving average cross",
            created_at=T,
            metadata={"instrument": "EURUSD", "outcome_r": "1.2"},
        )
        await service.index(
            "unrelated",
            "The cat sat on the mat in the afternoon sunshine",
            created_at=T,
            metadata={"instrument": "EURUSD", "outcome_r": "-1.0"},
        )

        result = await service.retrieve(
            "EURUSD trend following long entry", top_k=2, min_relevance=Decimal("0.0")
        )
        assert result.sample_size == 2
        assert result.matches[0].record_id == "trend"
        assert result.matches[0].relevance > result.matches[1].relevance


class TestRelevanceFloorIsEvidenceBased:
    """The floor is pinned from both sides by measurement.

    An earlier 0.60 came from a single query that nearly duplicated its document
    and silently filtered out every real paraphrase. Asserting only "above the
    noise" would not have caught that, so both bounds are checked.
    """

    def test_the_floor_rejects_the_worst_measured_noise(self) -> None:
        assert DEFAULT_MIN_RELEVANCE > BEST_UNRELATED_MATCH

    def test_the_floor_admits_the_worst_measured_genuine_match(self) -> None:
        """The bound that would have caught the 0.60 guess."""
        assert DEFAULT_MIN_RELEVANCE < WORST_CORRECT_MATCH

    def test_unrelated_prose_is_excluded_at_the_default(self) -> None:
        store = InMemoryVectorStore()
        store.add(unit_record("noise", BEST_UNRELATED_MATCH))
        result = store.search((1.0, 0.0, 0.0), space=SPACE, min_relevance=DEFAULT_MIN_RELEVANCE)
        assert result.matches == ()

    def test_a_genuine_paraphrase_survives_the_default(self) -> None:
        store = InMemoryVectorStore()
        store.add(unit_record("paraphrase", WORST_CORRECT_MATCH))
        result = store.search((1.0, 0.0, 0.0), space=SPACE, min_relevance=DEFAULT_MIN_RELEVANCE)
        assert [m.record_id for m in result.matches] == ["paraphrase"]


class TestSimilarityDoesNotRankSetups:
    """Measured: the best wrong-setup match (0.585) outscored the worst correct
    one (0.502). Ordering is not evidence of comparability."""

    def test_the_limitation_is_recorded_in_code(self) -> None:
        assert SIMILARITY_RANKS_SETUPS_RELIABLY is False

    def test_a_wrong_setup_can_outrank_a_correct_one(self) -> None:
        """Reproduces the measured inversion so no future change assumes the
        ranking is sound."""
        store = InMemoryVectorStore()
        store.add(unit_record("correct_setup", WORST_CORRECT_MATCH))
        store.add(unit_record("wrong_setup", BEST_WRONG_SETUP))
        matches = store.search(
            (1.0, 0.0, 0.0), space=SPACE, min_relevance=DEFAULT_MIN_RELEVANCE
        ).matches
        assert [m.record_id for m in matches] == ["wrong_setup", "correct_setup"]

    def test_similarity_only_selection_is_flagged(self) -> None:
        store = InMemoryVectorStore()
        store.add(unit_record("r", Decimal("0.9")))
        result = store.search((1.0, 0.0, 0.0), space=SPACE)
        assert not result.narrowed_by_metadata
        assert "similarity alone" in result.summary()

    def test_a_metadata_filter_removes_the_caveat(self) -> None:
        store = InMemoryVectorStore()
        store.add(unit_record("r", Decimal("0.9"), instrument="EURUSD"))
        result = store.search(
            (1.0, 0.0, 0.0), space=SPACE, filters=RetrievalFilter(instrument="EURUSD")
        )
        assert result.narrowed_by_metadata
        assert "similarity alone" not in result.summary()

    def test_an_empty_filter_does_not_count_as_narrowing(self) -> None:
        """RetrievalFilter() constrains nothing, so it must not suppress the
        caveat merely by being passed."""
        store = InMemoryVectorStore()
        store.add(unit_record("r", Decimal("0.9")))
        result = store.search((1.0, 0.0, 0.0), space=SPACE, filters=RetrievalFilter())
        assert not result.narrowed_by_metadata


class TestSummaryFormatting:
    def test_sample_size_survives_when_outcomes_are_absent(self) -> None:
        """An earlier version dropped the candidate count when no outcomes were
        recorded, via a conditional-expression precedence bug."""
        store = InMemoryVectorStore()
        store.add(a_record("no-outcome", (1.0, 0.0, 0.0)))
        summary = store.search((1.0, 0.0, 0.0), space=SPACE).summary()
        assert "1 of 1 candidates matched" in summary
        assert "No outcomes recorded" in summary

    def test_outcomes_are_reported_when_present(self) -> None:
        store = InMemoryVectorStore()
        for i in range(6):
            store.add(a_record(f"t{i}", (1.0, 0.0, 0.0), outcome_r="-1.0"))
        summary = store.search((1.0, 0.0, 0.0), space=SPACE).summary()
        assert "Win rate 0%" in summary
        assert "mean -1.00R" in summary
        assert "Too few" not in summary
