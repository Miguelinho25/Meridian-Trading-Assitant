"""Health and system-state endpoints.

Reports component status without ever exposing a credential value — presence
only (security.md §2). The execution-safety block is deliberately prominent: the
first question anyone should be able to answer about a trading system is whether
it can place an order.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from meridian_config import PRODUCT_NAME, SAFETY_NOTICE, VERSION, get_settings, limits
from meridian_db import chain_head, session_scope, verify_chain
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

router = APIRouter(tags=["system"])

Status = Literal["ok", "degraded", "down", "disabled"]


class ComponentHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status
    detail: str | None = None


class ExecutionSafety(BaseModel):
    """What this build can and cannot do. Never omitted from a health response."""

    model_config = ConfigDict(extra="forbid")
    mode: str
    approval_mode: str
    risk_profile: str
    broker_execution_enabled: bool
    live_execution_implemented: bool
    kill_switch_engaged: bool
    max_risk_per_trade_pct: str
    notice: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    version: str
    environment: str
    status: Status
    execution_safety: ExecutionSafety
    components: dict[str, ComponentHealth]


async def _database_health() -> tuple[ComponentHealth, ComponentHealth]:
    """Probe connectivity and audit-chain integrity."""
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
            head, count = await chain_head(session)
            verification = await verify_chain(session)
    except Exception as exc:
        down = ComponentHealth(status="down", detail=f"{type(exc).__name__}: {exc}")
        return down, ComponentHealth(status="down", detail="unverifiable: database unreachable")

    db = ComponentHealth(status="ok", detail=f"{count} audit events")
    if verification.valid:
        audit = ComponentHealth(status="ok", detail=f"chain verified, head {head[:12]}…")
    else:
        audit = ComponentHealth(
            status="down", detail=f"CHAIN BROKEN at {verification.broken_at}: {verification.detail}"
        )
    return db, audit


@router.get("/health", response_model=HealthResponse, summary="System and safety state")
async def health() -> HealthResponse:
    settings = get_settings()
    db, audit = await _database_health()

    providers = settings.provider_status()
    components: dict[str, ComponentHealth] = {
        "database": db,
        "audit_chain": audit,
        "market_data": ComponentHealth(
            status="ok", detail=f"provider={settings.market_data_provider}"
        ),
        "ollama": ComponentHealth(
            status="ok" if settings.ollama_enabled else "disabled",
            detail=settings.ollama_worker_model if settings.ollama_enabled else "no-LLM mode",
        ),
        "anthropic": ComponentHealth(
            status="ok" if providers["anthropic"] == "configured" else "disabled",
            detail=providers["anthropic"],
        ),
        "openai": ComponentHealth(
            status="ok" if providers["openai"] == "configured" else "disabled",
            detail=providers["openai"],
        ),
    }

    # A broken audit chain or an unreachable database degrades the whole system:
    # neither state may be reported as healthy.
    critical = {"database", "audit_chain"}
    overall: Status = "ok"
    if any(components[c].status == "down" for c in critical):
        overall = "down"
    elif any(c.status == "degraded" for c in components.values()):
        overall = "degraded"

    return HealthResponse(
        product=PRODUCT_NAME,
        version=VERSION,
        environment=settings.environment,
        status=overall,
        execution_safety=ExecutionSafety(
            mode=settings.mode.value,
            approval_mode=settings.approval_mode.value,
            risk_profile=settings.risk_profile.value,
            broker_execution_enabled=settings.broker_execution_enabled,
            live_execution_implemented=limits.LIVE_EXECUTION_IMPLEMENTED,
            kill_switch_engaged=settings.kill_switch,
            max_risk_per_trade_pct=str(settings.max_risk_per_trade_pct),
            notice=SAFETY_NOTICE,
        ),
        components=components,
    )


@router.get("/health/live", summary="Liveness probe")
async def liveness() -> dict[str, Any]:
    return {"status": "ok"}
