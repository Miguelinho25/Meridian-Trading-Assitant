"""Backtest research records.

Read-only. Runs are created by ``scripts/run_backtest.py``, not over HTTP: a
backtest takes seconds to minutes, and a request that holds a connection open
that long is a denial-of-service surface rather than a feature. More importantly,
records are append-only — there is no route here that can edit or delete one,
because a result whose row could be changed afterwards is not evidence.

Numbers cross the wire as strings throughout, for the same reason as the risk
endpoints: JavaScript has one numeric type and it is a float.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from nemonis_db import session_scope
from nemonis_db.backtest_store import (
    find_determinism_breaks,
    get_equity_curve,
    get_run,
    get_trades,
    list_runs,
)
from nemonis_db.models import BacktestRun
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/backtests", tags=["backtests"])


class RunSummary(BaseModel):
    """One row in the Backtest Lab index."""

    model_config = ConfigDict(extra="forbid")
    id: str
    strategy_key: str
    strategy_version: str
    created_at: str
    duration_ms: int
    trade_count: int
    provenance: str
    net_pnl: str | None
    max_drawdown_pct: str | None
    #: None means validation was not run — different from run-and-failed, and
    #: never to be rendered as a pass.
    survives_all: bool | None
    #: Whether these numbers may be called a result at all.
    is_evidence: bool
    is_reproducible: bool
    git_dirty: bool
    manifest_hash: str
    result_hash: str
    instruments: list[str]
    timeframe: str


class RunDetail(RunSummary):
    """A run with its full manifest and verdicts."""

    model_config = ConfigDict(extra="forbid")
    manifest: dict[str, Any]
    manifest_version: str
    metrics: dict[str, Any]
    validation: dict[str, Any]
    git_commit: str
    git_branch: str
    engine_version: str
    feature_pipeline_version: str
    risk_profile_version: str
    market_data_provider: str
    dataset_version: str
    data_start: str | None
    data_end: str | None
    bar_count: int
    spread_assumed: bool
    spread_model: str
    slippage_model: str
    commission_model: str
    risk_profile: str
    starting_balance: str | None
    account_currency: str
    seed: int
    parameters: dict[str, Any]
    signals_generated: int
    proposals_made: int
    rejections: int
    notes: str
    #: Why the run cannot be reproduced, when it cannot. Empty otherwise.
    irreproducible_reason: str


class EquityPointOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str
    equity: str
    drawdown_pct: str


class TradeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int
    instrument: str
    direction: str
    entry_at: str
    exit_at: str | None
    entry_price: str
    exit_price: str | None
    lots: str | None
    pnl: str | None
    commission: str | None
    exit_reason: str
    mfe_pips: str | None
    mae_pips: str | None
    ambiguous_exit: bool


class DeterminismBreakOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_hash: str
    run_ids: list[str]
    result_hashes: list[str]
    summary: str


def _s(value: Any) -> str | None:
    """Serialise a Decimal without storage padding, and without losing value.

    DecimalText stores at a fixed scale, so a stored 12.5 round-trips as
    "12.5000000000" and would reach the UI as visual noise. ``normalize`` strips
    the padding but switches integers to exponent form (100 -> 1E+2), so the
    result is re-formatted with ``f``. Both steps are value-preserving; nothing
    here rounds.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def _json_field(raw: str) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _list_field(raw: str) -> list[str]:
    import json

    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _summary(run: BacktestRun) -> RunSummary:
    return RunSummary(
        id=run.id,
        strategy_key=run.strategy_key,
        strategy_version=run.strategy_version,
        created_at=run.created_at.isoformat(),
        duration_ms=run.duration_ms,
        trade_count=run.trade_count,
        provenance=run.provenance,
        net_pnl=_s(run.net_pnl),
        max_drawdown_pct=_s(run.max_drawdown_pct),
        survives_all=run.survives_all,
        is_evidence=run.is_evidence,
        is_reproducible=run.is_reproducible,
        git_dirty=run.git_dirty,
        manifest_hash=run.manifest_hash,
        result_hash=run.result_hash,
        instruments=_list_field(run.instruments),
        timeframe=run.timeframe,
    )


@router.get("", response_model=list[RunSummary], summary="Recorded backtest runs")
async def index(
    strategy_key: str | None = None,
    reproducible_only: bool = False,
    provenance: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> list[RunSummary]:
    async with session_scope() as session:
        runs = await list_runs(
            session,
            strategy_key=strategy_key,
            reproducible_only=reproducible_only,
            provenance=provenance,
            limit=limit,
        )
        return [_summary(r) for r in runs]


@router.get("/determinism-breaks", response_model=list[DeterminismBreakOut])
async def determinism_breaks() -> list[DeterminismBreakOut]:
    """Runs sharing a manifest but disagreeing on the result.

    Should always be empty. When it is not, the statistics of every run involved
    are meaningless, so this is checked by query rather than by memory.
    """
    async with session_scope() as session:
        return [
            DeterminismBreakOut(
                manifest_hash=b.manifest_hash,
                run_ids=list(b.run_ids),
                result_hashes=list(b.result_hashes),
                summary=b.summary,
            )
            for b in await find_determinism_breaks(session)
        ]


@router.get("/{run_id}", response_model=RunDetail, summary="One run, with its manifest")
async def detail(run_id: str) -> RunDetail:
    async with session_scope() as session:
        run = await get_run(session, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"No backtest run {run_id}")

        reason = ""
        if not run.is_reproducible:
            reason = (
                f"The working tree was dirty at {run.git_commit[:12]}. The commit does "
                f"not identify the code that ran, and the uncommitted changes are not "
                f"recoverable from this record."
                if run.git_commit
                else "No git commit was recorded; the code that ran is unidentified."
            )

        return RunDetail(
            **_summary(run).model_dump(),
            manifest=_json_field(run.manifest_json),
            manifest_version=run.manifest_version,
            metrics=_json_field(run.metrics),
            validation=_json_field(run.validation),
            git_commit=run.git_commit,
            git_branch=run.git_branch,
            engine_version=run.engine_version,
            feature_pipeline_version=run.feature_pipeline_version,
            risk_profile_version=run.risk_profile_version,
            market_data_provider=run.market_data_provider,
            dataset_version=run.dataset_version,
            data_start=run.data_start.isoformat() if run.data_start else None,
            data_end=run.data_end.isoformat() if run.data_end else None,
            bar_count=run.bar_count,
            spread_assumed=run.spread_assumed,
            spread_model=run.spread_model,
            slippage_model=run.slippage_model,
            commission_model=run.commission_model,
            risk_profile=run.risk_profile,
            starting_balance=_s(run.starting_balance),
            account_currency=run.account_currency,
            seed=run.seed,
            parameters=_json_field(run.parameters),
            signals_generated=run.signals_generated,
            proposals_made=run.proposals_made,
            rejections=run.rejections,
            notes=run.notes,
            irreproducible_reason=reason,
        )


@router.get("/{run_id}/equity", response_model=list[EquityPointOut])
async def equity(run_id: str, max_points: int = Query(600, ge=50, le=5000)) -> list[EquityPointOut]:
    """The equity and drawdown curves.

    Downsampled by even stride when long. The first and last points are always
    kept, so the visible start and end of the curve are the real ones rather
    than whichever samples the stride happened to land on.
    """
    async with session_scope() as session:
        points = await get_equity_curve(session, run_id)
        if len(points) > max_points:
            stride = len(points) // max_points + 1
            sampled = points[::stride]
            if sampled[-1] is not points[-1]:
                sampled.append(points[-1])
            points = sampled

        return [
            EquityPointOut(
                at=p.at.isoformat(),
                equity=str(p.equity),
                drawdown_pct=str(p.drawdown_pct),
            )
            for p in points
        ]


@router.get("/{run_id}/trades", response_model=list[TradeOut])
async def trades(run_id: str, limit: int = Query(500, ge=1, le=5000)) -> list[TradeOut]:
    async with session_scope() as session:
        return [
            TradeOut(
                sequence=t.sequence,
                instrument=t.instrument,
                direction=t.direction,
                entry_at=t.entry_at.isoformat(),
                exit_at=t.exit_at.isoformat() if t.exit_at else None,
                entry_price=str(t.entry_price),
                exit_price=_s(t.exit_price),
                lots=_s(t.lots),
                pnl=_s(t.pnl),
                commission=_s(t.commission),
                exit_reason=t.exit_reason,
                mfe_pips=_s(t.mfe_pips),
                mae_pips=_s(t.mae_pips),
                ambiguous_exit=t.ambiguous_exit,
            )
            for t in (await get_trades(session, run_id))[:limit]
        ]
