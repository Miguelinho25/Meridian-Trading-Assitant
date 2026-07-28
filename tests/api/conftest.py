"""Shared API client fixture."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from meridian_api.app import create_app


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncIterator[AsyncClient]:
    """Client against a temporary database, with lifespan skipped.

    The app's own lifespan writes an audit event and touches the configured
    database; tests supply their own so runs cannot interfere with each other.
    """
    monkeypatch.setenv("MERIDIAN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/t.db")

    import meridian_db.session as session_module
    from meridian_config import reset_settings_cache
    from meridian_db import Base, create_engine, dispose_engine

    reset_settings_cache()
    await dispose_engine()

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_module._engine = engine
    session_module._factory = session_module.create_session_factory(engine)

    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    await engine.dispose()
    session_module._engine = None
    session_module._factory = None
    reset_settings_cache()
