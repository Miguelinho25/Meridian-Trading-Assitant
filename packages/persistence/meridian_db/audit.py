"""Hash-chained audit log (security.md §5).

Each event stores ``hash = SHA256(prev_hash || canonical_payload)``. Altering or
deleting any event breaks every subsequent link, which ``verify_chain`` detects.

This does not make tampering impossible — anyone with database access could
rewrite the whole chain. It makes tampering *detectable*, which is the achievable
goal for a local single-user system and is what an audit trail is actually for.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Final

from meridian_config.redaction import redact_mapping
from meridian_schemas.enums import AuditEventType
from meridian_schemas.identifiers import IdPrefix, new_id
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meridian_db.models import AuditEvent

#: First link. A fixed, recognisable value so an empty chain is unambiguous.
GENESIS_HASH: Final = "0" * 64


class AuditChainError(RuntimeError):
    """The audit chain failed verification."""


def canonical_json(payload: dict[str, Any]) -> str:
    """Deterministic JSON: sorted keys, no whitespace, non-ASCII escaped.

    Determinism matters — the same payload must hash identically on every machine
    and every Python version, or verification produces false alarms.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(prev_hash: str, payload_json: str) -> str:
    return hashlib.sha256(f"{prev_hash}{payload_json}".encode()).hexdigest()


async def _latest(session: AsyncSession) -> AuditEvent | None:
    result = await session.execute(select(AuditEvent).order_by(AuditEvent.sequence.desc()).limit(1))
    return result.scalar_one_or_none()


async def append_event(
    session: AsyncSession,
    *,
    event_type: AuditEventType,
    payload: dict[str, Any],
    occurred_at: datetime,
    actor: str = "system",
    entity_type: str | None = None,
    entity_id: str | None = None,
    request_id: str | None = None,
) -> AuditEvent:
    """Append one event, linked to the current chain head.

    The payload is redacted before hashing, so the stored hash covers exactly what
    is stored — verification would otherwise fail on any record whose payload was
    redacted after hashing.
    """
    safe_payload = redact_mapping(payload)
    payload_json = canonical_json(safe_payload)

    head = await _latest(session)
    prev_hash = head.hash if head else GENESIS_HASH
    sequence = (head.sequence + 1) if head else 1

    event = AuditEvent(
        id=new_id(IdPrefix.AUDIT_EVENT),
        sequence=sequence,
        event_type=event_type.value,
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        request_id=request_id,
        payload=payload_json,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, payload_json),
        occurred_at=occurred_at,
        created_at=occurred_at,
    )
    session.add(event)
    await session.flush()
    return event


class ChainVerification:
    """Outcome of a chain verification."""

    __slots__ = ("broken_at", "detail", "events_checked", "valid")

    def __init__(
        self,
        valid: bool,
        events_checked: int,
        broken_at: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.valid = valid
        self.events_checked = events_checked
        self.broken_at = broken_at
        self.detail = detail

    def __repr__(self) -> str:
        if self.valid:
            return f"<ChainVerification valid events={self.events_checked}>"
        return f"<ChainVerification BROKEN at={self.broken_at} detail={self.detail!r}>"

    def raise_if_broken(self) -> None:
        if not self.valid:
            raise AuditChainError(f"Audit chain broken at {self.broken_at}: {self.detail}")


async def verify_chain(session: AsyncSession, *, batch_size: int = 1000) -> ChainVerification:
    """Walk the chain and confirm every link.

    Streams in batches so a long history does not have to fit in memory.
    """
    expected_prev = GENESIS_HASH
    checked = 0
    offset = 0

    while True:
        result = await session.execute(
            select(AuditEvent).order_by(AuditEvent.sequence).offset(offset).limit(batch_size)
        )
        batch = list(result.scalars().all())
        if not batch:
            break

        for event in batch:
            if event.prev_hash != expected_prev:
                return ChainVerification(
                    False,
                    checked,
                    event.id,
                    f"prev_hash {event.prev_hash[:12]}… does not match preceding "
                    f"hash {expected_prev[:12]}… — an event was altered or removed",
                )
            recomputed = compute_hash(event.prev_hash, event.payload)
            if recomputed != event.hash:
                return ChainVerification(
                    False,
                    checked,
                    event.id,
                    f"stored hash {event.hash[:12]}… does not match recomputed "
                    f"{recomputed[:12]}… — the payload was altered",
                )
            expected_prev = event.hash
            checked += 1

        offset += batch_size

    return ChainVerification(True, checked)


async def chain_head(session: AsyncSession) -> tuple[str, int]:
    """Current chain head hash and event count. For health reporting."""
    head = await _latest(session)
    count = await session.scalar(select(func.count()).select_from(AuditEvent)) or 0
    return (head.hash if head else GENESIS_HASH, int(count))
