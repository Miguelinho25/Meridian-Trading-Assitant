"""System state — versions, configuration and the integrity of the record.

Reports what is configured without ever reporting a secret's *value*. Presence
only: knowing whether an API key is set is operationally necessary, and knowing
what it is never is. Every provider is reported as configured or absent, and the
redaction that guarantees this is exercised by the test suite rather than
assumed.

The audit chain verification is the part that matters most. A hash-chained log
whose integrity is never checked is a log nobody has reason to trust, so this
verifies it on demand and reports where it broke if it did.
"""

from __future__ import annotations

from fastapi import APIRouter
from nemonis_backtest.manifest import MANIFEST_VERSION
from nemonis_config import PRODUCT_NAME, VERSION, get_settings
from nemonis_db import chain_head, session_scope, verify_chain
from nemonis_features.registry import FEATURE_VERSION
from nemonis_risk.profiles import PROFILE_VERSION
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/system", tags=["system"])


class VersionsOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    version: str
    environment: str
    #: Every versioned component that can change a result. A backtest recorded
    #: under different values is a different experiment.
    engine: str
    feature_pipeline: str
    risk_profiles: str
    manifest_schema: str


class ConfigOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: str
    approval_mode: str
    risk_profile: str
    broker_execution_enabled: bool
    storage_backend: str
    market_data_provider: str
    vault_path: str
    vault_sync_enabled: bool
    ollama_enabled: bool
    ollama_model: str
    #: "configured" or "absent". Never the value — knowing a key is set is
    #: operationally necessary; knowing what it is never is.
    providers: dict[str, str]


class AuditOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    valid: bool
    events: int
    head: str
    #: Populated only on failure, and left empty otherwise rather than filled
    #: with a reassuring placeholder.
    broken_at: str
    #: Optional upstream: ChainVerification.detail is None on a clean chain, and
    #: coercing that to "" here would be fine — declaring it non-optional and
    #: passing None through was not, and crashed the endpoint.
    detail: str
    notice: str


@router.get("/versions", response_model=VersionsOut, summary="Component versions")
async def versions() -> VersionsOut:
    settings = get_settings()
    return VersionsOut(
        product=PRODUCT_NAME,
        version=VERSION,
        environment=settings.environment,
        engine=VERSION,
        feature_pipeline=FEATURE_VERSION,
        risk_profiles=PROFILE_VERSION,
        manifest_schema=MANIFEST_VERSION,
    )


@router.get("/config", response_model=ConfigOut, summary="Effective configuration")
async def config() -> ConfigOut:
    settings = get_settings()
    return ConfigOut(
        mode=settings.mode.value,
        approval_mode=settings.approval_mode.value,
        risk_profile=settings.risk_profile.value,
        broker_execution_enabled=settings.broker_execution_enabled,
        storage_backend=settings.storage_backend,
        market_data_provider=settings.market_data_provider,
        vault_path=str(settings.vault_path),
        vault_sync_enabled=settings.vault_sync_enabled,
        ollama_enabled=settings.ollama_enabled,
        ollama_model=settings.ollama_worker_model if settings.ollama_enabled else "",
        providers=settings.provider_status(),
    )


@router.get("/audit", response_model=AuditOut, summary="Audit chain integrity")
async def audit() -> AuditOut:
    """Verify the hash chain end to end.

    A chain nobody verifies is a chain nobody has reason to trust. Failure is
    reported with the event it broke at, because "the audit log is broken" is
    not actionable and "it broke at event 4,312" is.
    """
    try:
        async with session_scope() as session:
            head, count = await chain_head(session)
            result = await verify_chain(session)
    except Exception as exc:
        return AuditOut(
            valid=False,
            events=0,
            head="",
            broken_at="",
            detail=f"Could not verify: {type(exc).__name__}: {exc}",
            notice="An unverifiable chain must not be reported as a valid one.",
        )

    return AuditOut(
        valid=result.valid,
        events=count,
        head=head,
        broken_at=str(result.broken_at) if result.broken_at else "",
        detail=result.detail or "",
        notice=(
            "Every proposal, rejection, modification and simulated execution is "
            "hash-chained. A broken chain means the record has been altered."
        ),
    )
