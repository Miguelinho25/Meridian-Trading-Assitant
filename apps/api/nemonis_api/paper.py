"""Paper-trading sessions.

Read-only. Starting and stopping a session is a process-lifecycle action
(``scripts/paper_loop.py``), not an HTTP call: a request that spawned a trading
loop would put the decision to trade behind a button, and the loop outlives the
request that started it either way.

``bar_source`` is returned on every response and must be shown. A session fed
historical bars at speed behaves identically to one fed a live feed, so without
the label a replay's equity curve is indistinguishable from real paper
performance.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from nemonis_db import session_scope
from nemonis_db.models import PaperSessionRow
from nemonis_db.paper_store import (
    decision_breakdown,
    get_equity_curve,
    get_positions,
    get_trades,
    list_sessions,
    load_snapshot,
    recent_decisions,
)
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/paper", tags=["paper"])


def _s(value: Any) -> str | None:
    """Decimals as strings, without storage padding. See backtests._s."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: str
    mode: str
    #: REPLAY or LIVE. Must be surfaced: a replay is not paper performance.
    bar_source: str
    instruments: list[str]
    timeframe: str
    starting_balance: str
    balance: str
    equity: str
    ticks: int
    closed_trade_count: int
    open_position_count: int
    last_tick_at: str | None
    started_at: str
    halt_reason: str


class PositionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_id: str
    instrument: str
    direction: str
    lots: str
    entry_price: str
    opened_at: str
    strategy_id: str
    #: None means the position has no protective level, which is worth seeing.
    stop_loss: str | None
    take_profit: str | None


class SessionDetail(SessionSummary):
    model_config = ConfigDict(extra="forbid")
    approval_mode: str
    risk_profile: str
    prop_profile_id: str
    strategy_keys: list[str]
    high_water_mark: str
    balance_at_day_start: str
    realised_pnl: str
    total_commission: str
    signals_generated: int
    proposals_made: int
    orders_submitted: int
    rejections: int
    working_order_count: int
    positions: list[PositionOut]


class EquityPointOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str
    equity: str
    drawdown_pct: str


class TradeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trade_id: str
    instrument: str
    direction: str
    lots: str
    entry_price: str
    exit_price: str
    opened_at: str
    closed_at: str
    pnl: str
    exit_reason: str
    mfe_pips: str | None
    mae_pips: str | None


class DecisionGroup(BaseModel):
    """One verdict/reason pair and how often it bound."""

    model_config = ConfigDict(extra="forbid")
    verdict: str
    #: Empty for a clean approval. The absence is meaningful, not missing data.
    reason_code: str
    count: int
    share: str


class DecisionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str
    strategy_id: str
    verdict: str
    reason_code: str


def _summary(row: PaperSessionRow, *, open_positions: int) -> SessionSummary:
    import json

    return SessionSummary(
        id=row.id,
        status=row.status,
        mode=row.mode,
        bar_source=row.bar_source,
        instruments=list(json.loads(row.instruments or "[]")),
        timeframe=row.timeframe,
        starting_balance=_s(row.starting_balance) or "0",
        balance=_s(row.balance) or "0",
        equity=_s(row.equity) or "0",
        ticks=row.ticks,
        closed_trade_count=row.closed_trade_count,
        open_position_count=open_positions,
        last_tick_at=row.last_tick_at.isoformat() if row.last_tick_at else None,
        started_at=row.started_at.isoformat(),
        halt_reason=row.halt_reason,
    )


@router.get("", response_model=list[SessionSummary], summary="Paper sessions")
async def index(limit: int = Query(20, ge=1, le=100)) -> list[SessionSummary]:
    async with session_scope() as db:
        rows = await list_sessions(db, limit=limit)
        out = []
        for row in rows:
            positions = await get_positions(db, row.id)
            out.append(_summary(row, open_positions=len(positions)))
        return out


@router.get("/{session_id}", response_model=SessionDetail, summary="One session")
async def detail(session_id: str) -> SessionDetail:
    async with session_scope() as db:
        row = await db.get(PaperSessionRow, session_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No paper session {session_id}")
        snap = await load_snapshot(db, session_id)
        assert snap is not None  # the row exists, so the snapshot does

        return SessionDetail(
            **_summary(row, open_positions=len(snap.positions)).model_dump(),
            approval_mode=row.approval_mode,
            risk_profile=row.risk_profile,
            prop_profile_id=row.prop_profile_id,
            strategy_keys=snap.strategy_keys,
            high_water_mark=_s(row.high_water_mark) or "0",
            balance_at_day_start=_s(row.balance_at_day_start) or "0",
            realised_pnl=_s(row.realised_pnl) or "0",
            total_commission=_s(row.total_commission) or "0",
            signals_generated=row.signals_generated,
            proposals_made=row.proposals_made,
            orders_submitted=row.orders_submitted,
            rejections=row.rejections,
            working_order_count=len(snap.working_orders),
            positions=[
                PositionOut(
                    position_id=p.position_id,
                    instrument=p.instrument,
                    direction=p.direction,
                    lots=_s(p.lots) or "0",
                    entry_price=_s(p.entry_price) or "0",
                    opened_at=p.opened_at.isoformat(),
                    strategy_id=p.strategy_id,
                    stop_loss=_s(p.stop_loss),
                    take_profit=_s(p.take_profit),
                )
                for p in snap.positions
            ],
        )


@router.get("/{session_id}/equity", response_model=list[EquityPointOut])
async def equity(
    session_id: str, max_points: int = Query(600, ge=50, le=5000)
) -> list[EquityPointOut]:
    """Downsampled by stride, keeping both endpoints.

    The final point is the equity the session actually reached; dropping it to a
    stride would misreport where it stands now.
    """
    async with session_scope() as db:
        points = await get_equity_curve(db, session_id, limit=100000)
        if len(points) > max_points:
            stride = len(points) // max_points + 1
            sampled = points[::stride]
            if sampled and sampled[-1] is not points[-1]:
                sampled.append(points[-1])
            points = sampled
        return [
            EquityPointOut(
                at=p.at.isoformat(),
                equity=_s(p.equity) or "0",
                drawdown_pct=_s(p.drawdown_pct) or "0",
            )
            for p in points
        ]


@router.get("/{session_id}/trades", response_model=list[TradeOut])
async def trades(session_id: str, limit: int = Query(100, ge=1, le=1000)) -> list[TradeOut]:
    async with session_scope() as db:
        return [
            TradeOut(
                trade_id=t.trade_id,
                instrument=t.instrument,
                direction=t.direction,
                lots=_s(t.lots) or "0",
                entry_price=_s(t.entry_price) or "0",
                exit_price=_s(t.exit_price) or "0",
                opened_at=t.opened_at.isoformat(),
                closed_at=t.closed_at.isoformat(),
                pnl=_s(t.pnl) or "0",
                exit_reason=t.exit_reason,
                mfe_pips=_s(t.mfe_pips),
                mae_pips=_s(t.mae_pips),
            )
            for t in await get_trades(db, session_id, limit=limit)
        ]


@router.get("/{session_id}/decisions", response_model=list[DecisionGroup])
async def decisions(session_id: str) -> list[DecisionGroup]:
    """Why the session traded, and more usefully why it did not.

    A rejection count alone cannot answer that, which is why the reason codes are
    stored rather than tallied.
    """
    async with session_scope() as db:
        rows = await decision_breakdown(db, session_id)
        total = sum(n for _, _, n in rows) or 1
        return [
            DecisionGroup(
                verdict=verdict,
                reason_code=reason,
                count=n,
                share=f"{n / total:.4f}",
            )
            for verdict, reason, n in rows
        ]


@router.get("/{session_id}/decisions/recent", response_model=list[DecisionOut])
async def decisions_recent(
    session_id: str, limit: int = Query(100, ge=1, le=500)
) -> list[DecisionOut]:
    async with session_scope() as db:
        return [
            DecisionOut(
                at=d.at.isoformat(),
                strategy_id=d.strategy_id,
                verdict=d.verdict,
                reason_code=d.reason_code,
            )
            for d in await recent_decisions(db, session_id, limit=limit)
        ]
