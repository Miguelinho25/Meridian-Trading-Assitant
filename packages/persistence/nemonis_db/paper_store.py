"""Persisting and resuming paper-trading sessions.

Unlike backtest records these rows are mutable: a live session's state advances
every tick. A backtest is a finished result; a session is a position that still
exists.

What must survive a restart is **open exposure**. A session reloaded without its
positions and working orders has not lost bookkeeping — it has orphaned risk that
nothing will now manage, with stops that will never be honoured. So the snapshot
is a full replacement of positions and orders rather than an upsert: a position
closed while the process was down must *disappear* from the store, and a
merge-only write would resurrect it.

Like the backtest store, this takes plain data and returns plain data. The
session package never imports persistence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from nemonis_db.models import (
    PaperEquityPoint,
    PaperPosition,
    PaperSessionRow,
    PaperTrade,
    PaperWorkingOrder,
)


class SessionNotFoundError(RuntimeError):
    """No such paper session."""


def _json(values: list[str]) -> str:
    return json.dumps(sorted(values), separators=(",", ":"))


def _history(raw: str) -> list[dict[str, Any]]:
    """Rebuild an order's transition history.

    Timestamps were serialised as ISO strings; they are parsed back so a restored
    lifecycle holds real datetimes rather than text that only looks like one.
    """
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    rebuilt: list[dict[str, Any]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        item = dict(entry)
        when = item.get("at")
        if isinstance(when, str):
            item["at"] = datetime.fromisoformat(when)
        rebuilt.append(item)
    return rebuilt


def _list(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


@dataclass(slots=True)
class PositionRow:
    position_id: str
    instrument: str
    direction: str
    lots: Decimal
    entry_price: Decimal
    opened_at: datetime
    strategy_id: str = ""
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    commission_paid: Decimal = Decimal(0)
    best_price: Decimal | None = None
    worst_price: Decimal | None = None


@dataclass(slots=True)
class WorkingOrderRow:
    order_id: str
    proposal_hash: str
    instrument: str
    direction: str
    order_type: str
    lifecycle_state: str
    size_lots: Decimal
    lifecycle_history: list[dict[str, Any]] = field(default_factory=list)
    strategy_id: str = ""
    decision_id: str = ""
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    submitted_at: datetime | None = None


@dataclass(slots=True)
class ClosedTradeRow:
    trade_id: str
    instrument: str
    direction: str
    lots: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    pnl: Decimal
    commission: Decimal
    strategy_id: str = ""
    exit_reason: str = ""
    mfe_pips: Decimal | None = None
    mae_pips: Decimal | None = None
    ambiguous_exit: bool = False


@dataclass(slots=True)
class SessionSnapshot:
    """Everything needed to resume a session exactly where it stopped."""

    session_id: str
    status: str
    mode: str
    approval_mode: str
    risk_profile: str
    account_currency: str
    starting_balance: Decimal
    balance: Decimal
    equity: Decimal
    high_water_mark: Decimal
    balance_at_day_start: Decimal
    highest_equity_today: Decimal
    realised_pnl: Decimal
    total_commission: Decimal
    started_at: datetime
    updated_at: datetime

    prop_profile_id: str = ""
    #: REPLAY or LIVE. See the column comment on PaperSessionRow.
    bar_source: str = "REPLAY"
    instruments: list[str] = field(default_factory=list)
    timeframe: str = ""
    strategy_keys: list[str] = field(default_factory=list)
    seed: int = 0
    trading_day: datetime | None = None
    last_tick_at: datetime | None = None
    stopped_at: datetime | None = None
    halt_reason: str = ""

    ticks: int = 0
    signals_generated: int = 0
    proposals_made: int = 0
    orders_submitted: int = 0
    rejections: int = 0
    closed_trade_count: int = 0

    positions: list[PositionRow] = field(default_factory=list)
    working_orders: list[WorkingOrderRow] = field(default_factory=list)


async def save_snapshot(session: AsyncSession, snap: SessionSnapshot) -> None:
    """Write the session's current state, replacing positions and orders.

    Replacement, not merge. A position closed while the process was down must
    disappear from the store; an upsert-only write would leave it behind and the
    next restart would resurrect a position that no longer exists.
    """
    row = await session.get(PaperSessionRow, snap.session_id)
    if row is None:
        row = PaperSessionRow(id=snap.session_id, started_at=snap.started_at)
        session.add(row)

    row.status = snap.status
    row.mode = snap.mode
    row.approval_mode = snap.approval_mode
    row.risk_profile = snap.risk_profile
    row.prop_profile_id = snap.prop_profile_id
    row.instruments = _json(snap.instruments)
    row.timeframe = snap.timeframe
    row.strategy_keys = _json(snap.strategy_keys)
    row.seed = snap.seed
    row.bar_source = snap.bar_source
    row.account_currency = snap.account_currency
    row.starting_balance = snap.starting_balance
    row.balance = snap.balance
    row.equity = snap.equity
    row.high_water_mark = snap.high_water_mark
    row.balance_at_day_start = snap.balance_at_day_start
    row.highest_equity_today = snap.highest_equity_today
    row.realised_pnl = snap.realised_pnl
    row.total_commission = snap.total_commission
    row.trading_day = snap.trading_day
    row.ticks = snap.ticks
    row.signals_generated = snap.signals_generated
    row.proposals_made = snap.proposals_made
    row.orders_submitted = snap.orders_submitted
    row.rejections = snap.rejections
    row.closed_trade_count = snap.closed_trade_count
    row.last_tick_at = snap.last_tick_at
    row.stopped_at = snap.stopped_at
    row.updated_at = snap.updated_at
    row.halt_reason = snap.halt_reason

    await session.execute(delete(PaperPosition).where(PaperPosition.session_id == snap.session_id))
    await session.execute(
        delete(PaperWorkingOrder).where(PaperWorkingOrder.session_id == snap.session_id)
    )

    for p in snap.positions:
        session.add(
            PaperPosition(
                position_id=p.position_id,
                session_id=snap.session_id,
                instrument=p.instrument,
                direction=p.direction,
                lots=p.lots,
                entry_price=p.entry_price,
                opened_at=p.opened_at,
                strategy_id=p.strategy_id,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                commission_paid=p.commission_paid,
                best_price=p.best_price,
                worst_price=p.worst_price,
            )
        )

    for o in snap.working_orders:
        session.add(
            PaperWorkingOrder(
                order_id=o.order_id,
                session_id=snap.session_id,
                proposal_hash=o.proposal_hash,
                decision_id=o.decision_id,
                instrument=o.instrument,
                direction=o.direction,
                order_type=o.order_type,
                lifecycle_state=o.lifecycle_state,
                lifecycle_history=json.dumps(
                    o.lifecycle_history, default=str, separators=(",", ":")
                ),
                size_lots=o.size_lots,
                strategy_id=o.strategy_id,
                limit_price=o.limit_price,
                stop_price=o.stop_price,
                stop_loss=o.stop_loss,
                take_profit=o.take_profit,
                submitted_at=o.submitted_at,
            )
        )

    await session.flush()


async def load_snapshot(session: AsyncSession, session_id: str) -> SessionSnapshot | None:
    """Read a session's state, including its open exposure."""
    row = await session.get(PaperSessionRow, session_id)
    if row is None:
        return None

    positions = (
        (
            await session.execute(
                select(PaperPosition)
                .where(PaperPosition.session_id == session_id)
                .order_by(PaperPosition.opened_at)
            )
        )
        .scalars()
        .all()
    )
    orders = (
        (
            await session.execute(
                select(PaperWorkingOrder).where(PaperWorkingOrder.session_id == session_id)
            )
        )
        .scalars()
        .all()
    )

    return SessionSnapshot(
        session_id=row.id,
        status=row.status,
        mode=row.mode,
        approval_mode=row.approval_mode,
        risk_profile=row.risk_profile,
        prop_profile_id=row.prop_profile_id,
        instruments=_list(row.instruments),
        timeframe=row.timeframe,
        strategy_keys=_list(row.strategy_keys),
        seed=row.seed,
        bar_source=row.bar_source,
        account_currency=row.account_currency,
        starting_balance=row.starting_balance,
        balance=row.balance,
        equity=row.equity,
        high_water_mark=row.high_water_mark,
        balance_at_day_start=row.balance_at_day_start,
        highest_equity_today=row.highest_equity_today,
        realised_pnl=row.realised_pnl,
        total_commission=row.total_commission,
        trading_day=row.trading_day,
        ticks=row.ticks,
        signals_generated=row.signals_generated,
        proposals_made=row.proposals_made,
        orders_submitted=row.orders_submitted,
        rejections=row.rejections,
        closed_trade_count=row.closed_trade_count,
        started_at=row.started_at,
        last_tick_at=row.last_tick_at,
        stopped_at=row.stopped_at,
        updated_at=row.updated_at,
        halt_reason=row.halt_reason,
        positions=[
            PositionRow(
                position_id=p.position_id,
                instrument=p.instrument,
                direction=p.direction,
                lots=p.lots,
                entry_price=p.entry_price,
                opened_at=p.opened_at,
                strategy_id=p.strategy_id,
                stop_loss=p.stop_loss,
                take_profit=p.take_profit,
                commission_paid=p.commission_paid,
                best_price=p.best_price,
                worst_price=p.worst_price,
            )
            for p in positions
        ],
        working_orders=[
            WorkingOrderRow(
                order_id=o.order_id,
                proposal_hash=o.proposal_hash,
                decision_id=o.decision_id,
                instrument=o.instrument,
                direction=o.direction,
                order_type=o.order_type,
                lifecycle_state=o.lifecycle_state,
                lifecycle_history=_history(o.lifecycle_history),
                size_lots=o.size_lots,
                strategy_id=o.strategy_id,
                limit_price=o.limit_price,
                stop_price=o.stop_price,
                stop_loss=o.stop_loss,
                take_profit=o.take_profit,
                submitted_at=o.submitted_at,
            )
            for o in orders
        ],
    )


async def record_equity(
    session: AsyncSession,
    session_id: str,
    *,
    at: datetime,
    equity: Decimal,
    balance: Decimal,
    drawdown_pct: Decimal,
    open_positions: int,
) -> None:
    """Append an equity point, ignoring a repeat of the same instant.

    A retried tick must not double-write the curve.
    """
    existing = (
        await session.execute(
            select(PaperEquityPoint.id).where(
                PaperEquityPoint.session_id == session_id, PaperEquityPoint.at == at
            )
        )
    ).first()
    if existing is not None:
        return

    session.add(
        PaperEquityPoint(
            session_id=session_id,
            at=at,
            equity=equity,
            balance=balance,
            drawdown_pct=drawdown_pct,
            open_positions=open_positions,
        )
    )
    await session.flush()


async def record_trades(
    session: AsyncSession, session_id: str, trades: list[ClosedTradeRow]
) -> int:
    """Append closed trades, skipping any already recorded.

    A closed trade is a finished fact, so this never updates one. Skipping
    duplicates makes a retried tick safe rather than a source of phantom trades.
    """
    written = 0
    for t in trades:
        if await session.get(PaperTrade, t.trade_id) is not None:
            continue
        session.add(
            PaperTrade(
                trade_id=t.trade_id,
                session_id=session_id,
                instrument=t.instrument,
                direction=t.direction,
                strategy_id=t.strategy_id,
                lots=t.lots,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                opened_at=t.opened_at,
                closed_at=t.closed_at,
                pnl=t.pnl,
                commission=t.commission,
                exit_reason=t.exit_reason,
                mfe_pips=t.mfe_pips,
                mae_pips=t.mae_pips,
                ambiguous_exit=t.ambiguous_exit,
            )
        )
        written += 1
    await session.flush()
    return written


async def list_sessions(session: AsyncSession, *, limit: int = 20) -> list[PaperSessionRow]:
    query = select(PaperSessionRow).order_by(PaperSessionRow.updated_at.desc()).limit(limit)
    return list((await session.execute(query)).scalars().all())


async def get_trades(
    session: AsyncSession, session_id: str, *, limit: int = 200
) -> list[PaperTrade]:
    query = (
        select(PaperTrade)
        .where(PaperTrade.session_id == session_id)
        .order_by(PaperTrade.closed_at.desc())
        .limit(limit)
    )
    return list((await session.execute(query)).scalars().all())


async def get_equity_curve(
    session: AsyncSession, session_id: str, *, limit: int = 2000
) -> list[PaperEquityPoint]:
    query = (
        select(PaperEquityPoint)
        .where(PaperEquityPoint.session_id == session_id)
        .order_by(PaperEquityPoint.at)
        .limit(limit)
    )
    return list((await session.execute(query)).scalars().all())


async def get_positions(session: AsyncSession, session_id: str) -> list[PaperPosition]:
    query = (
        select(PaperPosition)
        .where(PaperPosition.session_id == session_id)
        .order_by(PaperPosition.opened_at)
    )
    return list((await session.execute(query)).scalars().all())


def require_snapshot(snap: SessionSnapshot | None, session_id: str) -> SessionSnapshot:
    if snap is None:
        raise SessionNotFoundError(f"No paper session {session_id}")
    return snap


def snapshot_metadata(row: PaperSessionRow) -> dict[str, Any]:
    """Summary fields for a listing, without loading exposure."""
    return {
        "id": row.id,
        "status": row.status,
        "mode": row.mode,
        "instruments": _list(row.instruments),
        "bar_source": row.bar_source,
        "ticks": row.ticks,
        "balance": row.balance,
        "equity": row.equity,
        "closed_trade_count": row.closed_trade_count,
        "last_tick_at": row.last_tick_at,
        "halt_reason": row.halt_reason,
    }
