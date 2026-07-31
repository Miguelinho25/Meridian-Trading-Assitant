"""Health and system-state endpoints.

Reports component status without ever exposing a credential value — presence
only (security.md §2). The execution-safety block is deliberately prominent: the
first question anyone should be able to answer about a trading system is whether
it can place an order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter
from nemonis_config import PRODUCT_NAME, SAFETY_NOTICE, VERSION, get_settings, limits
from nemonis_db import chain_head, session_scope, verify_chain
from nemonis_db.killswitch import current_state, resolve
from nemonis_risk import LimitSet, compose
from nemonis_risk.profiles import SYSTEM_LIMITS, get_profile
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
    #: The limit the engine will actually enforce, after all four tiers compose.
    #:
    #: This previously reported ``settings.max_risk_per_trade_pct``, the raw
    #: system ceiling. That is the *loosest* tier by construction, so the
    #: persistent risk banner showed 1.00% while the CHALLENGE profile held the
    #: engine to 0.35% — overstating permitted risk by nearly 3x, on the one
    #: number an operator sees on every screen.
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


def effective_risk_per_trade() -> Decimal:
    """The risk-per-trade limit after tier composition.

    Shares its inputs with /api/risk/limits deliberately, and a test asserts the
    two endpoints agree. An operator reading a smaller number in the Risk Lab
    than in the always-visible header would have no way to know which binds.
    """
    settings = get_settings()
    composed = compose(
        SYSTEM_LIMITS, LimitSet(), get_profile(settings.risk_profile).limits, LimitSet()
    )
    value = composed.risk_per_trade_pct
    # Fail loud rather than falling back to the looser setting: an unknown limit
    # must never be presented as the permissive one.
    if value is None:
        raise RuntimeError(
            "No tier defines risk_per_trade_pct. The header must not fall back to "
            "the raw system setting, which is the loosest tier by construction."
        )
    return value


async def kill_switch_engaged() -> bool:
    """The switch as the trading loop sees it.

    Read from the store, not from configuration. /health reported
    ``settings.kill_switch`` alone, so an operator who engaged the switch through
    the API saw "clear" here — and the persistent UI banner reads this endpoint,
    which would have shown KILL SWITCH CLEAR while trading was halted. Two places
    reporting the same fact must not be able to disagree.

    Fails closed: if the store cannot be read, current_state reports engaged.
    """
    configured = get_settings().kill_switch
    try:
        async with session_scope() as session:
            stored = await current_state(session)
    except Exception:
        # The database is already reported as down by the component check above.
        # An unreadable switch is an engaged switch.
        return True
    return resolve(stored=stored, configured=configured).engaged


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
            kill_switch_engaged=await kill_switch_engaged(),
            max_risk_per_trade_pct=str(effective_risk_per_trade()),
            notice=SAFETY_NOTICE,
        ),
        components=components,
    )


@router.get("/health/live", summary="Liveness probe")
async def liveness() -> dict[str, Any]:
    return {"status": "ok"}
