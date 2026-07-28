"""Risk configuration endpoints.

The Risk Lab's job is to let an operator see what is actually enforced. Two
failure modes matter more than anything cosmetic: displaying a limit the engine
does not enforce, and offering any route that could loosen one.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

from httpx import AsyncClient
from meridian_risk import LimitSet, compose
from meridian_risk.limits import TIGHTEN_DIRECTION
from meridian_risk.profiles import SYSTEM_LIMITS, get_profile


class TestEffectiveLimits:
    async def test_every_limit_is_reported(self, client: AsyncClient) -> None:
        """A limit missing from the Risk Lab is an unmonitored limit."""
        body = (await client.get("/api/risk/limits")).json()
        assert {row["field_name"] for row in body["limits"]} == set(TIGHTEN_DIRECTION)

    async def test_displayed_values_match_what_the_engine_composes(
        self, client: AsyncClient
    ) -> None:
        """The failure that would matter: showing a number the engine does not use."""
        body = (await client.get("/api/risk/limits")).json()
        profile = get_profile(body["risk_profile"])
        expected = compose(SYSTEM_LIMITS, LimitSet(), profile.limits, LimitSet())

        for row in body["limits"]:
            actual = getattr(expected, row["field_name"])
            shown = None if row["value"] is None else Decimal(row["value"])
            assert shown == actual, f"{row['field_name']}: shown {shown}, enforced {actual}"

    async def test_the_binding_tier_is_named(self, client: AsyncClient) -> None:
        body = (await client.get("/api/risk/limits")).json()
        for row in body["limits"]:
            if row["value"] is not None:
                assert row["bound_by"], f"{row['field_name']} has a value but no source tier"

    async def test_all_four_tiers_are_present(self, client: AsyncClient) -> None:
        """Empty tiers are declared, not omitted, so the composition the operator
        sees is the same four-tier one the engine performs."""
        body = (await client.get("/api/risk/limits")).json()
        for row in body["limits"]:
            assert [t["tier"] for t in row["tier_values"]] == [
                "system",
                "account",
                "profile",
                "strategy",
            ]

    async def test_tightening_direction_is_exposed(self, client: AsyncClient) -> None:
        """Which way is stricter is not guessable from the number alone."""
        body = (await client.get("/api/risk/limits")).json()
        directions = {row["field_name"]: row["tightens"] for row in body["limits"]}
        assert directions["risk_per_trade_pct"] == "LOWER"
        assert directions["min_reward_risk"] == "HIGHER"

    async def test_numbers_cross_the_wire_as_strings(self, client: AsyncClient) -> None:
        """A JSON number would reach JavaScript as a float."""
        body = (await client.get("/api/risk/limits")).json()
        for row in body["limits"]:
            assert row["value"] is None or isinstance(row["value"], str)

    async def test_a_tightened_limit_is_flagged(self, client: AsyncClient) -> None:
        """The visible evidence that tiers only ever tighten."""
        body = (await client.get("/api/risk/limits")).json()
        rows = {row["field_name"]: row for row in body["limits"]}
        risk = rows["risk_per_trade_pct"]
        assert risk["was_tightened"] is True
        assert risk["bound_by"] == ["profile"]

    async def test_the_notice_states_the_guarantee(self, client: AsyncClient) -> None:
        body = (await client.get("/api/risk/limits")).json()
        assert "tightening only" in body["notice"]


class TestNoLooseningRouteExists:
    """Invariant I5. The risk engine cannot be overridden by a UI control, so
    there must be no write route here to be secured later."""

    async def test_limits_reject_writes(self, client: AsyncClient) -> None:
        for method in (client.post, client.put, client.patch, client.delete):
            response = await method("/api/risk/limits")
            assert response.status_code == 405, f"{method.__name__} is routed"

    async def test_profiles_reject_writes(self, client: AsyncClient) -> None:
        for method in (client.post, client.put, client.patch, client.delete):
            assert (await method("/api/risk/profiles")).status_code == 405


class TestThrottleCurve:
    async def test_bands_are_contiguous_and_ordered(self, client: AsyncClient) -> None:
        bands = (await client.get("/api/risk/throttle")).json()
        assert bands
        for lower, upper in pairwise(bands):
            assert Decimal(lower["to_consumed"]) == Decimal(upper["from_consumed"])

    async def test_risk_falls_as_drawdown_deepens(self, client: AsyncClient) -> None:
        """The point of the curve. Recovery is convex against the account."""
        multipliers = [
            Decimal(b["risk_multiplier"]) for b in (await client.get("/api/risk/throttle")).json()
        ]
        assert multipliers == sorted(multipliers, reverse=True)

    async def test_selectivity_rises_across_the_trading_bands(self, client: AsyncClient) -> None:
        """Size is not the only response — the quality floors rise too, so
        showing the multiplier alone would understate it.

        Only bands that permit trading are considered. Once the multiplier
        reaches zero the uplifts are dead parameters and sit at zero; asserting
        over those would fail on a curve that is entirely correct.
        """
        bands = (await client.get("/api/risk/throttle")).json()
        trading = [b for b in bands if Decimal(b["risk_multiplier"]) > 0]
        assert len(trading) >= 2

        uplifts = [Decimal(b["confidence_uplift"]) for b in trading]
        rr_uplifts = [Decimal(b["reward_risk_uplift"]) for b in trading]
        assert uplifts == sorted(uplifts)
        assert rr_uplifts == sorted(rr_uplifts)
        assert uplifts[-1] > 0, "the deepest trading band must be the most selective"


class TestProfiles:
    async def test_exactly_one_profile_is_active(self, client: AsyncClient) -> None:
        profiles = (await client.get("/api/risk/profiles")).json()
        assert sum(1 for p in profiles if p["active"]) == 1

    async def test_experimental_is_confined_to_research_modes(self, client: AsyncClient) -> None:
        profiles = {p["name"]: p for p in (await client.get("/api/risk/profiles")).json()}
        if "EXPERIMENTAL" in profiles:
            assert "live" not in profiles["EXPERIMENTAL"]["allowed_modes"]
