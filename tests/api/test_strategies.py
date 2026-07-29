"""The strategy registry endpoint.

Two properties matter more than the listing itself: that nothing claims to be
ACTIVE without evidence, and that the author's *prior* is never presented as a
capability *filter*.
"""

from __future__ import annotations

from httpx import AsyncClient


class TestNothingIsActiveWithoutEvidence:
    """The Backtest Lab reports zero runs qualifying as evidence. A registry
    simultaneously reporting live strategies would contradict it."""

    async def test_no_strategy_is_active(self, client: AsyncClient) -> None:
        body = (await client.get("/api/strategies")).json()
        active = [s for s in body["strategies"] if s["status"] == "ACTIVE"]
        assert active == [], (
            f"{[s['key'] for s in active]} are ACTIVE, but no backtest has passed "
            f"validation. ACTIVE must mean promoted on evidence."
        )

    async def test_baselines_are_still_runnable(self, client: AsyncClient) -> None:
        """Demoting them must not silently disable research."""
        body = (await client.get("/api/strategies")).json()
        assert body["strategies"]
        assert all(s["is_runnable"] for s in body["strategies"])

    async def test_the_funnel_reports_empty_stages(self, client: AsyncClient) -> None:
        """A funnel showing only populated stages hides that nothing was promoted."""
        funnel = (await client.get("/api/strategies")).json()["funnel"]
        assert [f["status"] for f in funnel] == [
            "REGISTERED",
            "CANDIDATE",
            "PAPER",
            "ACTIVE",
            "RETIRED",
            "QUARANTINED",
        ]
        assert next(f for f in funnel if f["status"] == "ACTIVE")["count"] == 0


class TestPriorsAreNotFilters:
    """Filtering on expected_regimes would make the author's belief
    unfalsifiable and suppress the signals that would reveal they were wrong
    (strategy-platform.md §6)."""

    async def test_expected_regimes_is_a_separate_field(self, client: AsyncClient) -> None:
        for s in (await client.get("/api/strategies")).json()["strategies"]:
            assert "expected_regimes" in s
            assert "supported_instruments" in s
            assert "supported_sessions" in s

    async def test_a_stated_prior_does_not_restrict_instruments(self, client: AsyncClient) -> None:
        """The baselines declare expected regimes and no instrument limit. If a
        prior were being treated as a constraint, that combination could not
        survive."""
        strategies = (await client.get("/api/strategies")).json()["strategies"]
        with_priors = [s for s in strategies if s["expected_regimes"]]
        assert with_priors, "no strategy declares a prior — this proves nothing"
        for s in with_priors:
            assert s["supported_instruments"] is None


class TestEveryStrategyStatesABelief:
    async def test_hypotheses_are_present_and_substantial(self, client: AsyncClient) -> None:
        """A strategy that cannot state what it believes cannot be evaluated
        against whether that belief held."""
        for s in (await client.get("/api/strategies")).json()["strategies"]:
            assert len(s["hypothesis"].strip()) > 40, s["key"]

    async def test_baselines_disclaim_being_an_edge(self, client: AsyncClient) -> None:
        for s in (await client.get("/api/strategies")).json()["strategies"]:
            assert "BASELINE" in s["hypothesis"].upper()


class TestReadOnly:
    async def test_promotion_is_not_an_http_action(self, client: AsyncClient) -> None:
        """Promotion is an evidence decision. A route that skipped that would be
        the quickest way to trade an unvalidated strategy."""
        for method in (client.post, client.put, client.patch, client.delete):
            assert (await method("/api/strategies")).status_code == 405
