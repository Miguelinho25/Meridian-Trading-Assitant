"""FastAPI application.

The HTTP edge owns no business logic: it validates, delegates and serialises.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from nemonis_config import (
    PRODUCT_NAME,
    SAFETY_NOTICE,
    VERSION,
    SystemClock,
    configure_logging,
    get_logger,
    get_settings,
)
from nemonis_db import append_event, dispose_engine, session_scope, verify_chain
from nemonis_schemas.enums import AuditEventType

from nemonis_api.backtests import router as backtests_router
from nemonis_api.health import router as health_router
from nemonis_api.journal import router as journal_router
from nemonis_api.killswitch import router as killswitch_router
from nemonis_api.middleware import RequestContextMiddleware
from nemonis_api.paper import router as paper_router
from nemonis_api.propfirm import router as propfirm_router
from nemonis_api.risk import router as risk_router
from nemonis_api.strategies import router as strategies_router
from nemonis_api.system import router as system_router

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shut-down.

    Verifies the audit chain at boot. A broken chain does not stop the process —
    research and backtesting remain useful — but it is logged as an error and the
    health endpoint reports the system as down, so it cannot pass unnoticed.
    """
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    clock = SystemClock()  # the I/O edge is the only tier permitted a wall clock

    log.info(
        "starting",
        product=PRODUCT_NAME,
        version=VERSION,
        mode=settings.mode.value,
        approval_mode=settings.approval_mode.value,
        risk_profile=settings.risk_profile.value,
        broker_execution_enabled=settings.broker_execution_enabled,
        storage=settings.storage_backend,
    )

    try:
        async with session_scope() as session:
            verification = await verify_chain(session)
            if not verification.valid:
                log.error(
                    "audit_chain_broken",
                    broken_at=verification.broken_at,
                    detail=verification.detail,
                )
            else:
                log.info("audit_chain_verified", events=verification.events_checked)

            await append_event(
                session,
                event_type=AuditEventType.SYSTEM_STARTED,
                payload={
                    "version": VERSION,
                    "mode": settings.mode.value,
                    "approval_mode": settings.approval_mode.value,
                    "broker_execution_enabled": settings.broker_execution_enabled,
                },
                occurred_at=clock.now(),
                actor="system",
            )
    except Exception as exc:
        # Startup continues; /health reports the failure. Refusing to boot would
        # remove the operator's ability to diagnose it through the UI.
        log.error("startup_database_unavailable", error=str(exc))

    yield

    log.info("shutting_down")
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=f"{PRODUCT_NAME} API",
        version=VERSION,
        description=SAFETY_NOTICE,
        lifespan=lifespan,
    )

    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Request-ID"],
    )

    app.include_router(health_router)
    app.include_router(risk_router)
    app.include_router(backtests_router)
    app.include_router(strategies_router)
    app.include_router(propfirm_router)
    app.include_router(paper_router)
    app.include_router(killswitch_router)
    app.include_router(journal_router)
    app.include_router(system_router)
    return app


app = create_app()
