from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from nemonis_config import FrozenClock, reset_settings_cache
from nemonis_db import Base, create_engine, create_session_factory
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's real .env from changing test outcomes."""
    for var in (
        "NEMONIS_MODE",
        "NEMONIS_BROKER_EXECUTION_ENABLED",
        "NEMONIS_APPROVAL_MODE",
        "NEMONIS_RISK_PROFILE",
        "NEMONIS_MAX_RISK_PER_TRADE_PCT",
        "NEMONIS_KILL_SWITCH",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC))


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """In-memory database with the full schema, torn down per test."""
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = create_session_factory(engine)
    async with factory() as s:
        yield s

    await engine.dispose()
