"""The health endpoint is how an operator answers 'can this place a trade?'."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


class TestHealthReportsSafetyState:
    async def test_returns_ok(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_reports_broker_execution_disabled(self, client: AsyncClient) -> None:
        safety = (await client.get("/health")).json()["execution_safety"]
        assert safety["broker_execution_enabled"] is False
        assert safety["live_execution_implemented"] is False

    async def test_reports_safe_defaults(self, client: AsyncClient) -> None:
        safety = (await client.get("/health")).json()["execution_safety"]
        assert safety["mode"] == "research"
        assert safety["approval_mode"] == "MANUAL_APPROVAL"
        assert safety["risk_profile"] == "CHALLENGE"

    async def test_audit_chain_verified_on_empty_database(self, client: AsyncClient) -> None:
        """Stage A done-criterion, surfaced through the API."""
        components = (await client.get("/health")).json()["components"]
        assert components["audit_chain"]["status"] == "ok"
        assert "verified" in components["audit_chain"]["detail"]


class TestHealthNeverLeaksSecrets:
    async def test_provider_keys_reported_as_presence_only(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        body = (await client.get("/health")).text
        assert "sk-" not in body
        assert "api_key" not in body

    async def test_unconfigured_providers_are_disabled_not_erroring(
        self, client: AsyncClient
    ) -> None:
        components = (await client.get("/health")).json()["components"]
        assert components["openai"]["status"] == "disabled"
        assert components["anthropic"]["status"] == "disabled"


class TestRequestContext:
    async def test_request_id_is_echoed(self, client: AsyncClient) -> None:
        response = await client.get("/health/live")
        assert "X-Request-ID" in response.headers
        assert response.headers["X-Request-ID"].startswith("req_")

    async def test_supplied_request_id_is_preserved(self, client: AsyncClient) -> None:
        response = await client.get("/health/live", headers={"X-Request-ID": "req_mine"})
        assert response.headers["X-Request-ID"] == "req_mine"
