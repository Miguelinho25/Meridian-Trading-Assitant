"""Journal and system endpoints.

The journal boundary is a safety property: the brief forbids arbitrary Markdown
edits from touching balances, order history or audit records. The vault holds
interpretation; the database holds record.
"""

from __future__ import annotations

from httpx import AsyncClient


class TestTheVaultIsReadOnlyOverHttp:
    """Notes are edited in Obsidian, against files. An HTTP write path would blur
    interpretation and record, which must stay separate."""

    async def test_no_write_routes(self, client: AsyncClient) -> None:
        for method in (client.post, client.put, client.patch, client.delete):
            assert (await method("/api/journal")).status_code == 405

    async def test_a_path_escaping_the_vault_is_refused(self, client: AsyncClient) -> None:
        """Resolved-path check, not a string check: '..' segments would not be
        caught by inspecting the input."""
        response = await client.get("/api/journal/../../etc/passwd")
        assert response.status_code in {400, 404}

    async def test_an_absolute_path_is_refused(self, client: AsyncClient) -> None:
        assert (await client.get("/api/journal//etc/passwd")).status_code in {400, 404}

    async def test_a_missing_note_is_404(self, client: AsyncClient) -> None:
        assert (await client.get("/api/journal/trades/nope.md")).status_code == 404


class TestVaultStatus:
    async def test_status_reports_the_boundary(self, client: AsyncClient) -> None:
        body = (await client.get("/api/journal/status")).json()
        assert "interpretation" in body["notice"]
        assert isinstance(body["note_count"], int)

    async def test_an_absent_vault_lists_nothing_rather_than_erroring(
        self, client: AsyncClient
    ) -> None:
        """A missing vault is a normal state, not a failure."""
        assert (await client.get("/api/journal")).status_code == 200


class TestSystemNeverLeaksACredential:
    """Presence is operationally necessary; the value never is."""

    async def test_providers_report_state_not_values(self, client: AsyncClient) -> None:
        providers = (await client.get("/api/system/config")).json()["providers"]
        assert providers
        for state in providers.values():
            assert state in {"configured", "unconfigured", "enabled", "disabled"}

    async def test_no_secret_shaped_string_appears(self, client: AsyncClient) -> None:
        body = (await client.get("/api/system/config")).text
        for marker in ("sk-", "AKIA", "ghp_", "BEGIN "):
            assert marker not in body


class TestVersionsCoverEveryReproducibilityInput:
    async def test_each_versioned_component_is_reported(self, client: AsyncClient) -> None:
        """A backtest recorded under different values is a different experiment,
        so each of these belongs in the manifest and on this page."""
        body = (await client.get("/api/system/versions")).json()
        for field in ("engine", "feature_pipeline", "risk_profiles", "manifest_schema"):
            assert body[field], f"{field} is empty"


class TestAuditIntegrity:
    async def test_a_clean_chain_verifies(self, client: AsyncClient) -> None:
        body = (await client.get("/api/system/audit")).json()
        assert body["valid"] is True
        assert body["broken_at"] == ""

    async def test_detail_is_a_string_even_when_absent(self, client: AsyncClient) -> None:
        """ChainVerification.detail is None on a clean chain. Declaring the
        response field non-optional and passing None through crashed the
        endpoint with a validation error."""
        body = (await client.get("/api/system/audit")).json()
        assert isinstance(body["detail"], str)

    async def test_audit_rejects_writes(self, client: AsyncClient) -> None:
        for method in (client.post, client.put, client.patch, client.delete):
            assert (await method("/api/system/audit")).status_code == 405
