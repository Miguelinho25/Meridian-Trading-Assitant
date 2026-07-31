"""Kill-switch endpoints — the only write surface in this API.

The asymmetry is the design: engaging must be trivially easy, releasing must not
be reachable by a single mistyped request.
"""

from __future__ import annotations

from httpx import AsyncClient


class TestEngaging:
    async def test_it_starts_clear(self, client: AsyncClient) -> None:
        body = (await client.get("/api/kill-switch")).json()
        assert body["engaged"] is False
        assert body["indeterminate"] is False

    async def test_engaging_needs_only_a_post(self, client: AsyncClient) -> None:
        """Moving toward safety must never be blocked on paperwork."""
        body = (await client.post("/api/kill-switch/engage", json={})).json()
        assert body["engaged"] is True

    async def test_the_reason_and_actor_are_recorded(self, client: AsyncClient) -> None:
        await client.post(
            "/api/kill-switch/engage",
            json={"reason": "Spread blew out on GBPJPY", "actor": "miguel"},
        )
        body = (await client.get("/api/kill-switch")).json()
        assert body["reason"] == "Spread blew out on GBPJPY"
        assert body["actor"] == "miguel"
        assert "ENGAGED by miguel" in body["summary"]

    async def test_engaging_twice_succeeds(self, client: AsyncClient) -> None:
        """An operator hitting the control twice in an incident must get the
        state they asked for, not an error."""
        await client.post("/api/kill-switch/engage", json={"reason": "first"})
        second = await client.post("/api/kill-switch/engage", json={"reason": "second"})
        assert second.status_code == 200
        assert second.json()["engaged"] is True


class TestReleasingIsDeliberatelyHarder:
    async def test_a_short_reason_is_rejected(self, client: AsyncClient) -> None:
        """ "ok" is not a reason."""
        await client.post("/api/kill-switch/engage", json={"reason": "incident"})
        response = await client.post(
            "/api/kill-switch/disengage", json={"reason": "ok", "confirm": True}
        )
        assert response.status_code == 422

    async def test_confirmation_is_required(self, client: AsyncClient) -> None:
        await client.post("/api/kill-switch/engage", json={"reason": "incident"})
        response = await client.post(
            "/api/kill-switch/disengage",
            json={"reason": "Feed recovered and stable", "confirm": False},
        )
        assert response.status_code == 400
        assert "confirm=true" in response.json()["detail"]

    async def test_a_refused_release_leaves_it_engaged(self, client: AsyncClient) -> None:
        """A rejected release must not half-apply."""
        await client.post("/api/kill-switch/engage", json={"reason": "incident"})
        await client.post(
            "/api/kill-switch/disengage",
            json={"reason": "Feed recovered and stable", "confirm": False},
        )
        assert (await client.get("/api/kill-switch")).json()["engaged"] is True

    async def test_a_reasoned_confirmed_release_works(self, client: AsyncClient) -> None:
        await client.post("/api/kill-switch/engage", json={"reason": "incident"})
        response = await client.post(
            "/api/kill-switch/disengage",
            json={"reason": "Feed recovered, spreads normal for 30 minutes", "confirm": True},
        )
        assert response.status_code == 200
        assert response.json()["engaged"] is False


class TestConfigurationHaltsCannotBeReleasedOverHttp:
    """A deployment-level halt must not be undoable by the least privileged path
    into the system."""

    async def test_a_config_halt_is_reported_as_engaged(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        from nemonis_config import get_settings, reset_settings_cache

        monkeypatch.setenv("NEMONIS_KILL_SWITCH", "true")
        reset_settings_cache()
        try:
            body = (await client.get("/api/kill-switch")).json()
            assert body["engaged"] is True
            assert body["from_configuration"] is True
            assert body["actor"] == "configuration"
        finally:
            monkeypatch.delenv("NEMONIS_KILL_SWITCH", raising=False)
            reset_settings_cache()
            get_settings()

    async def test_releasing_a_config_halt_is_refused(
        self, client: AsyncClient, monkeypatch
    ) -> None:
        from nemonis_config import get_settings, reset_settings_cache

        monkeypatch.setenv("NEMONIS_KILL_SWITCH", "true")
        reset_settings_cache()
        try:
            response = await client.post(
                "/api/kill-switch/disengage",
                json={"reason": "Trying to release a deployment halt", "confirm": True},
            )
            assert response.status_code == 409
            assert "deployment-level" in response.json()["detail"]
        finally:
            monkeypatch.delenv("NEMONIS_KILL_SWITCH", raising=False)
            reset_settings_cache()
            get_settings()


class TestHistoryIsAppendOnly:
    async def test_every_transition_is_kept(self, client: AsyncClient) -> None:
        await client.post("/api/kill-switch/engage", json={"reason": "spread blew out"})
        await client.post(
            "/api/kill-switch/disengage",
            json={"reason": "Feed recovered and stable again", "confirm": True},
        )
        events = (await client.get("/api/kill-switch/history")).json()
        assert len(events) == 2
        # Newest first, so the current state leads.
        assert events[0]["engaged"] is False
        assert events[1]["engaged"] is True

    async def test_a_release_does_not_erase_the_engagement(self, client: AsyncClient) -> None:
        """Why it was engaged is the first question asked afterwards."""
        await client.post("/api/kill-switch/engage", json={"reason": "spread blew out"})
        await client.post(
            "/api/kill-switch/disengage",
            json={"reason": "Feed recovered and stable again", "confirm": True},
        )
        reasons = [e["reason"] for e in (await client.get("/api/kill-switch/history")).json()]
        assert "spread blew out" in reasons

    async def test_history_rejects_writes(self, client: AsyncClient) -> None:
        for method in (client.post, client.put, client.patch, client.delete):
            assert (await method("/api/kill-switch/history")).status_code == 405


class TestTheHealthEndpointAgrees:
    async def test_health_reflects_an_engaged_switch(self, client: AsyncClient) -> None:
        """Two places report this. They must not disagree — an operator checking
        one and acting on the other is exactly how a halt gets missed."""
        await client.post("/api/kill-switch/engage", json={"reason": "incident"})
        safety = (await client.get("/health")).json()["execution_safety"]
        assert safety["kill_switch_engaged"] is True
