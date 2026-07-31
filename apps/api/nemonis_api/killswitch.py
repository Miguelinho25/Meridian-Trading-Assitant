"""The kill switch — the only write surface in this API.

Everything else here is read-only by design. This is the exception, and it is the
right one: the control that *stops* trading is precisely the control an operator
must be able to reach without a shell, a config file or a restart.

The asymmetry carries through to HTTP. Engaging takes an optional reason and
always succeeds. Disengaging requires a reason and a confirmation flag, and is
refused without both. That is not ceremony — an unexplained release is
indistinguishable from an accidental one, and the two actions differ enormously
in consequence.

Engaging does **not** close open positions. The switch stops new trades;
liquidating a book unattended is a far larger action than "stop trading", and one
nobody asked for. Existing positions keep being managed with their stops honoured
— a switch that abandoned them would discard the exposure it was pulled to
contain.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from nemonis_config import get_settings
from nemonis_db import session_scope
from nemonis_db.killswitch import (
    current_state,
    disengage,
    engage,
    history,
    resolve,
)
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter(prefix="/api/kill-switch", tags=["kill-switch"])


class StateOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engaged: bool
    reason: str
    actor: str
    since: str | None
    #: True when the state could not be read and was assumed engaged. An operator
    #: seeing "engaged" deserves to know whether someone engaged it or whether
    #: the system cannot tell.
    indeterminate: bool
    #: True when configuration engaged it. That cannot be released over HTTP —
    #: it is a deployment-level halt and needs a deployment-level change.
    from_configuration: bool
    summary: str


class EventOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    engaged: bool
    reason: str
    actor: str
    at: str


class EngageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Optional. Moving toward safety must never be blocked on paperwork.
    reason: str = ""
    actor: str = "operator"


class DisengageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Mandatory, and the length floor is deliberate: "ok" is not a reason.
    reason: str = Field(min_length=10)
    actor: str = "operator"
    #: A second, explicit act. Releasing a halt should not be reachable by a
    #: single mistyped request.
    confirm: bool


def _out(state, *, from_configuration: bool) -> StateOut:  # type: ignore[no-untyped-def]
    return StateOut(
        engaged=state.engaged,
        reason=state.reason,
        actor=state.actor,
        since=state.since.isoformat() if state.since else None,
        indeterminate=state.indeterminate,
        from_configuration=from_configuration,
        summary=state.summary,
    )


async def _resolved() -> tuple[object, bool]:
    """Current state, plus whether configuration is what engaged it."""
    configured = get_settings().kill_switch
    async with session_scope() as db:
        stored = await current_state(db)
    return resolve(stored=stored, configured=configured), configured and not stored.engaged


@router.get("", response_model=StateOut, summary="Current kill-switch state")
async def state() -> StateOut:
    resolved, from_config = await _resolved()
    return _out(resolved, from_configuration=from_config)


@router.get("/history", response_model=list[EventOut], summary="Every engagement")
async def events(limit: int = 50) -> list[EventOut]:
    """Append-only. Why it was engaged is the first question asked afterwards,
    and a release must never erase the engagement it followed."""
    async with session_scope() as db:
        return [
            EventOut(
                id=e.id,
                engaged=e.engaged,
                reason=e.reason,
                actor=e.actor,
                at=e.at.isoformat(),
            )
            for e in await history(db, limit=limit)
        ]


@router.post("/engage", response_model=StateOut, summary="Halt new trading")
async def engage_switch(request: EngageRequest) -> StateOut:
    """Stop new trades immediately.

    Idempotent: engaging an already-engaged switch records the new reason rather
    than erroring. An operator hitting this twice during an incident should get
    the state they asked for.
    """
    async with session_scope() as db:
        await engage(db, reason=request.reason, actor=request.actor, at=datetime.now(UTC))
    resolved, from_config = await _resolved()
    return _out(resolved, from_configuration=from_config)


@router.post("/disengage", response_model=StateOut, summary="Permit trading again")
async def disengage_switch(request: DisengageRequest) -> StateOut:
    """Release the halt. Requires a reason and explicit confirmation."""
    if not request.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "Releasing the kill switch requires confirm=true. Engaging is a "
                "move toward safety and needs no confirmation; releasing it is "
                "not, and should not be reachable by a single mistyped request."
            ),
        )

    settings = get_settings()
    if settings.kill_switch:
        # A configuration halt is a deployment-level decision. Letting an HTTP
        # call override it would mean the safest way to stop this system could be
        # undone by the least privileged path into it.
        raise HTTPException(
            status_code=409,
            detail=(
                "NEMONIS_KILL_SWITCH is set in configuration. That is a "
                "deployment-level halt and cannot be released over HTTP — change "
                "the configuration and restart."
            ),
        )

    async with session_scope() as db:
        try:
            await disengage(db, reason=request.reason, actor=request.actor, at=datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved, from_config = await _resolved()
    return _out(resolved, from_configuration=from_config)
