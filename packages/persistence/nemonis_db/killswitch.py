"""The kill switch.

Three properties matter more than anything else here.

**It works without a restart.** ``settings.kill_switch`` is read at start-up, so
engaging it there means editing a file and restarting a process — during which
the loop keeps trading. The authoritative state therefore lives in the database,
where a running loop re-reads it every tick.

**An unknown state is an engaged state.** If the database cannot be reached, or
the answer cannot be parsed, this reports *engaged*. Every other component in
this system fails closed and so does this one: the cost of halting a paper loop
that did not need halting is nil, and the cost of the reverse is unbounded.

**Disengaging is harder than engaging.** Engaging moves toward safety and needs
only an actor. Disengaging moves away from it and requires an explicit reason,
which is recorded. The asymmetry is deliberate — the two actions are not
symmetric in consequence and should not be equally easy.

State is derived from the append-only event log rather than kept in a mutable
row: the latest event *is* the state. A separate flag could drift out of step
with its own history, and then neither could be trusted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from nemonis_schemas.identifiers import IdPrefix, new_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nemonis_db.models import KillSwitchEvent

#: Reasons the system engages the switch itself. Automatic engagement is not a
#: courtesy — the brief requires a stale feed, a model outage or an ambiguous
#: account state to prevent new trades rather than merely be logged.
AUTOMATIC_ACTOR: Final = "system"
MANUAL_ACTOR_FALLBACK: Final = "operator"


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """The current state, and how it was reached."""

    engaged: bool
    reason: str
    actor: str
    since: datetime | None
    #: True when the state could not be read and was assumed engaged. The
    #: distinction matters: an operator seeing "engaged" deserves to know whether
    #: someone engaged it or whether the system simply cannot tell.
    indeterminate: bool = False

    @property
    def blocks_trading(self) -> bool:
        return self.engaged

    @property
    def summary(self) -> str:
        if self.indeterminate:
            return (
                f"Kill switch state could not be read ({self.reason}). Treated as "
                f"ENGAGED: an unknown state must block trading, never permit it."
            )
        if not self.engaged:
            return "Clear. New trades are permitted, subject to the risk engine."
        return f"ENGAGED by {self.actor}: {self.reason}"


#: What the system reports when it cannot determine the state. Constructed here
#: rather than at each call site so no caller can accidentally default to False.
def _indeterminate(detail: str) -> KillSwitchState:
    return KillSwitchState(
        engaged=True,
        reason=detail,
        actor="system",
        since=None,
        indeterminate=True,
    )


async def current_state(session: AsyncSession) -> KillSwitchState:
    """Read the switch. Returns *engaged* if the answer cannot be determined."""
    try:
        row = (
            await session.execute(
                select(KillSwitchEvent).order_by(KillSwitchEvent.at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    # Deliberately broad: any failure to read means the state is unknown, and
    # an unknown state must block trading. Narrowing this would let some
    # unanticipated error escape and be treated as "clear".
    except Exception as exc:
        return _indeterminate(f"{type(exc).__name__}: {exc}")

    if row is None:
        # No event has ever been recorded, which is the clean initial state
        # rather than an unreadable one.
        return KillSwitchState(engaged=False, reason="", actor="", since=None, indeterminate=False)

    return KillSwitchState(
        engaged=row.engaged,
        reason=row.reason,
        actor=row.actor,
        since=row.at,
        indeterminate=False,
    )


async def engage(
    session: AsyncSession, *, reason: str, actor: str, at: datetime
) -> KillSwitchState:
    """Halt new trading.

    Idempotent: engaging an already-engaged switch records the new reason rather
    than refusing. Refusing would mean an operator hitting the control twice in
    an incident gets an error instead of the state they asked for.

    Open positions are **not** closed. The switch stops new trades; liquidating
    an entire book unattended is a far larger action than "stop trading", and one
    the operator has not asked for. Existing positions keep being managed, with
    their stops honoured.
    """
    session.add(
        KillSwitchEvent(
            id=new_id(IdPrefix.KILL_SWITCH),
            engaged=True,
            reason=reason or "No reason given",
            actor=actor or MANUAL_ACTOR_FALLBACK,
            at=at,
        )
    )
    await session.flush()
    return await current_state(session)


async def disengage(
    session: AsyncSession, *, reason: str, actor: str, at: datetime
) -> KillSwitchState:
    """Permit trading again.

    A reason is mandatory. Engaging is a move toward safety and needs only an
    actor; disengaging is a move away from it, and the record of *why* is the
    only thing that makes the decision reviewable afterwards.
    """
    if not reason.strip():
        raise ValueError(
            "Disengaging the kill switch requires a reason. Engaging is a move "
            "toward safety; releasing it is not, and an unexplained release is "
            "indistinguishable from an accidental one."
        )

    session.add(
        KillSwitchEvent(
            id=new_id(IdPrefix.KILL_SWITCH),
            engaged=False,
            reason=reason,
            actor=actor or MANUAL_ACTOR_FALLBACK,
            at=at,
        )
    )
    await session.flush()
    return await current_state(session)


async def history(session: AsyncSession, *, limit: int = 50) -> list[KillSwitchEvent]:
    """Every engagement and release, newest first. Append-only."""
    query = select(KillSwitchEvent).order_by(KillSwitchEvent.at.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())


def resolve(*, stored: KillSwitchState, configured: bool) -> KillSwitchState:
    """Combine the stored state with the static configuration flag.

    Either source engaging it is enough. ``NEMONIS_KILL_SWITCH=true`` is a
    deployment-level halt that a database write must not be able to override, and
    a stored engagement must survive a config that says otherwise. Two switches
    in series, not in parallel.
    """
    if configured and not stored.engaged:
        return KillSwitchState(
            engaged=True,
            reason="NEMONIS_KILL_SWITCH is set in configuration.",
            actor="configuration",
            since=None,
        )
    return stored
