"""Engine, session factory and the append-only guard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from meridian_config import get_settings
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from meridian_db.models import APPEND_ONLY_TABLES


class AppendOnlyViolationError(RuntimeError):
    """An UPDATE or DELETE was attempted on an append-only table."""


def _install_append_only_guard(session: Session) -> None:
    """Block mutation of append-only tables at the ORM level.

    Postgres also gets a database trigger in the migration. This guard is the
    SQLite equivalent and a second line of defence on both — a corrective row is
    the only permitted way to change recorded history (data-model.md §1).
    """
    for obj in session.dirty:
        table = getattr(obj, "__tablename__", None)
        if table in APPEND_ONLY_TABLES and session.is_modified(obj):
            raise AppendOnlyViolationError(
                f"UPDATE refused on append-only table {table!r}. Record a compensating "
                f"entry instead of altering history."
            )
    for obj in session.deleted:
        table = getattr(obj, "__tablename__", None)
        if table in APPEND_ONLY_TABLES:
            raise AppendOnlyViolationError(f"DELETE refused on append-only table {table!r}.")


def create_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    """Build the async engine, applying per-dialect pragmas."""
    settings = get_settings()
    database_url = url or settings.database_url

    if database_url.startswith("sqlite"):
        # Ensure the parent directory exists — SQLite will not create it.
        path_part = database_url.split("///")[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(database_url, echo=echo, future=True)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            # Foreign keys are OFF by default in SQLite, which would silently
            # disable the orders → risk_assessments constraint carrying I1.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


@event.listens_for(Session, "before_flush")
def _before_flush_guard(session: Session, _flush_context: Any, _instances: Any) -> None:
    """Append-only guard, registered once on the Session class itself.

    Global rather than per-factory so it cannot be bypassed by constructing a
    session through some other route — the guarantee is only worth having if it
    holds everywhere.
    """
    _install_append_only_guard(session)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _factory
    if _factory is None:
        _factory = create_session_factory(get_engine())
    return _factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
