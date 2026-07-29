"""Audit chain and append-only guarantees (security.md §5, data-model.md §1)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from nemonis_config import FrozenClock
from nemonis_db import (
    GENESIS_HASH,
    AppendOnlyViolationError,
    append_event,
    chain_head,
    verify_chain,
)
from nemonis_db.audit import canonical_json, compute_hash
from nemonis_schemas.enums import AuditEventType
from sqlalchemy import text
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession


async def _append(session: AsyncSession, clock: FrozenClock, n: int) -> None:
    for i in range(n):
        await append_event(
            session,
            event_type=AuditEventType.PROPOSAL_CREATED,
            payload={"index": i, "instrument": "EURUSD"},
            occurred_at=clock.now(),
        )
        clock.advance(timedelta(seconds=1))


class TestChainConstruction:
    async def test_empty_chain_verifies(self, session: AsyncSession) -> None:
        """Stage A done-criterion: the chain verifies on an empty database."""
        result = await verify_chain(session)
        assert result.valid
        assert result.events_checked == 0

    async def test_empty_chain_head_is_genesis(self, session: AsyncSession) -> None:
        head, count = await chain_head(session)
        assert head == GENESIS_HASH
        assert count == 0

    async def test_first_event_links_to_genesis(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        event = await append_event(
            session,
            event_type=AuditEventType.SYSTEM_STARTED,
            payload={"version": "0.1.0"},
            occurred_at=clock.now(),
        )
        assert event.prev_hash == GENESIS_HASH

    async def test_chain_of_many_verifies(self, session: AsyncSession, clock: FrozenClock) -> None:
        await _append(session, clock, 50)
        result = await verify_chain(session)
        assert result.valid
        assert result.events_checked == 50

    async def test_chain_spans_batch_boundary(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        """Batched verification must not lose the link between batches."""
        await _append(session, clock, 25)
        result = await verify_chain(session, batch_size=10)
        assert result.valid
        assert result.events_checked == 25


class TestTamperDetection:
    async def test_altered_payload_breaks_the_chain(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        await _append(session, clock, 5)
        await session.commit()

        # Bypass the ORM guard, as an attacker with database access would.
        await session.execute(
            text("UPDATE audit_events SET payload = :p WHERE sequence = 3"),
            {"p": canonical_json({"index": 999, "instrument": "TAMPERED"})},
        )
        await session.commit()

        result = await verify_chain(session)
        assert not result.valid
        assert "payload was altered" in (result.detail or "")

    async def test_deleted_event_breaks_the_chain(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        await _append(session, clock, 5)
        await session.commit()

        await session.execute(text("DELETE FROM audit_events WHERE sequence = 3"))
        await session.commit()

        result = await verify_chain(session)
        assert not result.valid
        assert "altered or removed" in (result.detail or "")

    async def test_raise_if_broken(self, session: AsyncSession, clock: FrozenClock) -> None:
        await _append(session, clock, 3)
        await session.commit()
        await session.execute(text("DELETE FROM audit_events WHERE sequence = 2"))
        await session.commit()

        result = await verify_chain(session)
        with pytest.raises(Exception, match="Audit chain broken"):
            result.raise_if_broken()


class TestAppendOnlyGuard:
    async def test_update_is_refused(self, session: AsyncSession, clock: FrozenClock) -> None:
        event = await append_event(
            session,
            event_type=AuditEventType.SYSTEM_STARTED,
            payload={"a": 1},
            occurred_at=clock.now(),
        )
        await session.commit()

        event.actor = "someone-else"
        with pytest.raises(AppendOnlyViolationError, match="UPDATE refused"):
            await session.flush()

    async def test_delete_is_refused(self, session: AsyncSession, clock: FrozenClock) -> None:
        event = await append_event(
            session,
            event_type=AuditEventType.SYSTEM_STARTED,
            payload={"a": 1},
            occurred_at=clock.now(),
        )
        await session.commit()

        await session.delete(event)
        with pytest.raises(AppendOnlyViolationError, match="DELETE refused"):
            await session.flush()


class TestRedactionBeforeHashing:
    async def test_secrets_never_reach_the_audit_log(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        event = await append_event(
            session,
            event_type=AuditEventType.MODEL_INVOKED,
            payload={"api_key": "sk-abcdefghij0123456789", "model": "llama3.2:3b"},
            occurred_at=clock.now(),
        )
        assert "sk-abcdefghij" not in event.payload
        assert "llama3.2:3b" in event.payload

    async def test_redacted_payload_still_verifies(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        """The hash must cover what is stored, or verification false-alarms."""
        await append_event(
            session,
            event_type=AuditEventType.MODEL_INVOKED,
            payload={"api_key": "sk-abcdefghij0123456789"},
            occurred_at=clock.now(),
        )
        result = await verify_chain(session)
        assert result.valid


class TestCanonicalisation:
    def test_key_order_does_not_change_the_hash(self) -> None:
        a = canonical_json({"b": 2, "a": 1})
        b = canonical_json({"a": 1, "b": 2})
        assert a == b
        assert compute_hash(GENESIS_HASH, a) == compute_hash(GENESIS_HASH, b)

    def test_different_payloads_hash_differently(self) -> None:
        h1 = compute_hash(GENESIS_HASH, canonical_json({"a": 1}))
        h2 = compute_hash(GENESIS_HASH, canonical_json({"a": 2}))
        assert h1 != h2


class TestUTCEnforcement:
    async def test_naive_datetime_is_refused(self, session: AsyncSession) -> None:
        """A naive timestamp silently corrupts daily-reset comparisons.

        SQLAlchemy wraps the type-level ValueError in StatementError, so the
        assertion targets the wrapper and its underlying cause.
        """
        with pytest.raises(StatementError, match="timezone-aware") as exc_info:
            await append_event(
                session,
                event_type=AuditEventType.SYSTEM_STARTED,
                payload={},
                occurred_at=datetime(2026, 7, 27, 12, 0, 0),
            )
        assert isinstance(exc_info.value.orig, ValueError)

    async def test_timestamps_return_as_utc(
        self, session: AsyncSession, clock: FrozenClock
    ) -> None:
        await append_event(
            session,
            event_type=AuditEventType.SYSTEM_STARTED,
            payload={},
            occurred_at=clock.now(),
        )
        await session.commit()
        head, _ = await chain_head(session)
        assert head != GENESIS_HASH
