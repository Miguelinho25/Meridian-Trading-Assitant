"""Reading and writing backtest research records.

The store takes **plain data**, never engine types. An import-linter contract
forbids ``nemonis_backtest`` from reaching persistence — a model or database call
inside the replay loop would make runs irreproducible — so the translation from
``BacktestResult`` happens at the edge that owns both, and neither side depends
on the other.

Writes are inserts. There is no update path, and that is the guarantee that makes
the archive worth keeping: a result whose row could be edited afterwards is not
evidence of anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nemonis_db.models import BacktestEquityPoint, BacktestRun, BacktestTrade


class ImmutableRecordError(RuntimeError):
    """An attempt to alter a stored run."""


def _json(value: Any) -> str:
    """Canonical JSON: sorted keys, Decimals as strings."""

    def encode(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, dict):
            return {str(k): encode(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [encode(x) for x in v]
        return v

    return json.dumps(encode(value), sort_keys=True, separators=(",", ":"))


@dataclass(slots=True)
class EquityRow:
    at: datetime
    equity: Decimal
    balance: Decimal
    drawdown_pct: Decimal
    open_positions: int = 0


@dataclass(slots=True)
class TradeRow:
    instrument: str
    direction: str
    entry_at: datetime
    entry_price: Decimal
    exit_at: datetime | None = None
    exit_price: Decimal | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    lots: Decimal | None = None
    exit_reason: str = ""
    pnl: Decimal | None = None
    r_multiple: Decimal | None = None
    commission: Decimal | None = None
    session: str = ""
    regime_label: str = ""
    strategy_key: str = ""


@dataclass(slots=True)
class RunRecord:
    """One completed run, ready to store.

    ``manifest`` is the full canonical manifest dict. The scalar fields beside it
    are denormalised copies used for querying and sorting; the manifest remains
    the record of truth, so a run stays readable if those columns later change.
    """

    id: str
    manifest_hash: str
    result_hash: str
    manifest: dict[str, Any]
    manifest_version: str

    strategy_key: str
    strategy_version: str
    started_at: datetime
    completed_at: datetime
    created_at: datetime

    status: str = "COMPLETED"
    strategy_lifecycle: str = ""
    git_commit: str = ""
    git_branch: str = ""
    git_dirty: bool = False
    is_reproducible: bool = False
    engine_version: str = ""
    feature_pipeline_version: str = ""
    risk_profile_version: str = ""

    market_data_provider: str = ""
    dataset_version: str = ""
    instruments: tuple[str, ...] = ()
    timeframe: str = ""
    data_start: datetime | None = None
    data_end: datetime | None = None
    bar_count: int = 0
    provenance: str = ""
    spread_assumed: bool = False

    spread_model: str = ""
    slippage_model: str = ""
    commission_model: str = ""
    risk_profile: str = ""
    starting_balance: Decimal | None = None
    account_currency: str = ""
    seed: int = 0
    parameters: dict[str, Any] = field(default_factory=dict)
    ai_models: dict[str, str] = field(default_factory=dict)

    metrics: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    survives_all: bool | None = None
    is_evidence: bool = False
    trade_count: int = 0
    final_balance: Decimal | None = None
    net_pnl: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    signals_generated: int = 0
    proposals_made: int = 0
    rejections: int = 0

    duration_ms: int = 0
    notes: str = ""

    equity_curve: list[EquityRow] = field(default_factory=list)
    trades: list[TradeRow] = field(default_factory=list)


async def record_run(session: AsyncSession, record: RunRecord) -> str:
    """Insert a completed run. Never updates.

    Re-recording an existing id is refused rather than silently overwriting: two
    different runs sharing an id means the caller has a bug, and resolving it by
    replacing the earlier result would destroy research data.
    """
    existing = await session.get(BacktestRun, record.id)
    if existing is not None:
        raise ImmutableRecordError(
            f"Backtest run {record.id} is already recorded. Runs are append-only; "
            f"a re-run is a new run with its own id, not a replacement."
        )

    run = BacktestRun(
        id=record.id,
        manifest_hash=record.manifest_hash,
        result_hash=record.result_hash,
        manifest_json=_json(record.manifest),
        manifest_version=record.manifest_version,
        status=record.status,
        strategy_key=record.strategy_key,
        strategy_version=record.strategy_version,
        strategy_lifecycle=record.strategy_lifecycle,
        git_commit=record.git_commit,
        git_branch=record.git_branch,
        git_dirty=record.git_dirty,
        is_reproducible=record.is_reproducible,
        engine_version=record.engine_version,
        feature_pipeline_version=record.feature_pipeline_version,
        risk_profile_version=record.risk_profile_version,
        market_data_provider=record.market_data_provider,
        dataset_version=record.dataset_version,
        instruments=_json(list(record.instruments)),
        timeframe=record.timeframe,
        data_start=record.data_start,
        data_end=record.data_end,
        bar_count=record.bar_count,
        provenance=record.provenance,
        spread_assumed=record.spread_assumed,
        spread_model=record.spread_model,
        slippage_model=record.slippage_model,
        commission_model=record.commission_model,
        risk_profile=record.risk_profile,
        starting_balance=record.starting_balance,
        account_currency=record.account_currency,
        seed=record.seed,
        parameters=_json(record.parameters),
        ai_models=_json(record.ai_models),
        metrics=_json(record.metrics),
        validation=_json(record.validation),
        survives_all=record.survives_all,
        is_evidence=record.is_evidence,
        trade_count=record.trade_count,
        final_balance=record.final_balance,
        net_pnl=record.net_pnl,
        max_drawdown_pct=record.max_drawdown_pct,
        signals_generated=record.signals_generated,
        proposals_made=record.proposals_made,
        rejections=record.rejections,
        started_at=record.started_at,
        completed_at=record.completed_at,
        duration_ms=record.duration_ms,
        created_at=record.created_at,
        notes=record.notes,
    )
    session.add(run)

    for i, point in enumerate(record.equity_curve):
        session.add(
            BacktestEquityPoint(
                run_id=record.id,
                sequence=i,
                at=point.at,
                equity=point.equity,
                balance=point.balance,
                drawdown_pct=point.drawdown_pct,
                open_positions=point.open_positions,
            )
        )

    for i, trade in enumerate(record.trades):
        session.add(
            BacktestTrade(
                run_id=record.id,
                sequence=i,
                instrument=trade.instrument,
                direction=trade.direction,
                strategy_key=trade.strategy_key or record.strategy_key,
                entry_at=trade.entry_at,
                exit_at=trade.exit_at,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                stop_price=trade.stop_price,
                target_price=trade.target_price,
                lots=trade.lots,
                exit_reason=trade.exit_reason,
                pnl=trade.pnl,
                r_multiple=trade.r_multiple,
                commission=trade.commission,
                session=trade.session,
                regime_label=trade.regime_label,
            )
        )

    await session.flush()
    return record.id


async def get_run(session: AsyncSession, run_id: str) -> BacktestRun | None:
    return await session.get(BacktestRun, run_id)


async def get_equity_curve(session: AsyncSession, run_id: str) -> list[BacktestEquityPoint]:
    """The equity and drawdown curve, in order.

    Explicit rather than a lazy relationship: the list view has no use for
    thousands of equity points, and under asyncio an implicit load fails far
    from its cause.
    """
    query = (
        select(BacktestEquityPoint)
        .where(BacktestEquityPoint.run_id == run_id)
        .order_by(BacktestEquityPoint.sequence)
    )
    return list((await session.execute(query)).scalars().all())


async def get_trades(session: AsyncSession, run_id: str) -> list[BacktestTrade]:
    """Every trade a run generated, in execution order."""
    query = (
        select(BacktestTrade).where(BacktestTrade.run_id == run_id).order_by(BacktestTrade.sequence)
    )
    return list((await session.execute(query)).scalars().all())


async def list_runs(
    session: AsyncSession,
    *,
    strategy_key: str | None = None,
    survives_all: bool | None = None,
    reproducible_only: bool = False,
    provenance: str | None = None,
    limit: int = 50,
) -> list[BacktestRun]:
    """Recent runs, newest first. The Backtest Lab's index."""
    query = select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)
    if strategy_key is not None:
        query = query.where(BacktestRun.strategy_key == strategy_key)
    if survives_all is not None:
        query = query.where(BacktestRun.survives_all.is_(survives_all))
    if reproducible_only:
        query = query.where(BacktestRun.is_reproducible.is_(True))
    if provenance is not None:
        query = query.where(BacktestRun.provenance == provenance)
    return list((await session.execute(query)).scalars().all())


@dataclass(frozen=True, slots=True)
class DeterminismBreak:
    """Same inputs, different outputs. Should be impossible."""

    manifest_hash: str
    result_hashes: tuple[str, ...]
    run_ids: tuple[str, ...]

    @property
    def summary(self) -> str:
        return (
            f"{len(self.run_ids)} runs share manifest {self.manifest_hash[:19]}… but "
            f"produced {len(self.result_hashes)} different results. The backtest is "
            f"not deterministic for these inputs, so none of their statistics can be "
            f"trusted."
        )


async def find_determinism_breaks(session: AsyncSession) -> list[DeterminismBreak]:
    """Runs sharing a manifest hash but disagreeing on the result.

    The query the whole two-hash design exists to make possible. Determinism is
    what makes every validation statistic meaningful, so a break must be
    discoverable by query rather than by someone remembering to look.
    """
    grouped = (
        select(BacktestRun.manifest_hash)
        .group_by(BacktestRun.manifest_hash)
        .having(func.count(func.distinct(BacktestRun.result_hash)) > 1)
    )
    suspect = list((await session.execute(grouped)).scalars().all())
    if not suspect:
        return []

    rows = (
        await session.execute(
            select(BacktestRun.manifest_hash, BacktestRun.result_hash, BacktestRun.id)
            .where(BacktestRun.manifest_hash.in_(suspect))
            .order_by(BacktestRun.manifest_hash, BacktestRun.created_at)
        )
    ).all()

    breaks: dict[str, tuple[list[str], list[str]]] = {}
    for manifest_hash, result_h, run_id in rows:
        results, ids = breaks.setdefault(manifest_hash, ([], []))
        if result_h not in results:
            results.append(result_h)
        ids.append(run_id)

    return [
        DeterminismBreak(
            manifest_hash=manifest_hash,
            result_hashes=tuple(results),
            run_ids=tuple(ids),
        )
        for manifest_hash, (results, ids) in breaks.items()
    ]
