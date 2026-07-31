"""Run a backtest and record it as a reproducible research object.

This is the edge that owns both sides. The backtest engine may not import
persistence — a database call inside the replay loop would make runs
irreproducible, and an import-linter contract enforces it — so the translation
from ``BacktestResult`` to a stored ``RunRecord`` happens here and nowhere else.

    python scripts/run_backtest.py --instruments EURUSD GBPUSD --validate

The manifest is captured *before* the run, from git state as it stands at that
moment. A run started against a dirty tree is recorded as permanently
irreproducible rather than being refused: refusing would tempt the obvious
workaround of a throwaway commit, which produces a manifest that looks pinned
and is not.
"""

from __future__ import annotations

import argparse
import asyncio
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nemonis_backtest import BacktestConfig, BacktestEngine, compute_metrics
from nemonis_backtest.manifest import (
    MANIFEST_VERSION,
    BacktestManifest,
    DataIdentity,
    ExecutionModel,
    ModelIdentity,
    capture_code_identity,
    dataset_fingerprint,
    result_hash,
)
from nemonis_backtest.validation import monte_carlo, stress_test, walk_forward
from nemonis_broker.broker import ClosedTrade
from nemonis_broker.fills import FillModel, SlippageModel
from nemonis_config import VERSION, get_settings
from nemonis_db import session_scope
from nemonis_db.backtest_store import EquityRow as StoreEquityRow
from nemonis_db.backtest_store import RunRecord, TradeRow, record_run
from nemonis_features.registry import FEATURE_VERSION
from nemonis_marketdata.instruments import WATCHLIST
from nemonis_marketdata.providers import FileProvider
from nemonis_marketdata.types import Candle
from nemonis_risk import GENERIC_TWO_PHASE
from nemonis_risk.profiles import PROFILE_VERSION
from nemonis_schemas.enums import ResultProvenance, Timeframe
from nemonis_strategy.baselines import MovingAverageTrend, VolatilityBreakout
from nemonis_strategy.plugin import LifecycleStatus
from nemonis_strategy.registry import StrategyRegistry

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data" / "raw"

#: Wide bounds — the runner wants every bar in the file and lets the config
#: derive the real range from what was actually loaded.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


def _rates() -> dict[str, Decimal]:
    """Conversion rates to the account currency. Flat for a research run."""
    return {
        "USD": Decimal(1),
        "EUR": Decimal("1.08"),
        "GBP": Decimal("1.27"),
        "JPY": Decimal("0.0067"),
    }


def build_manifest(
    *,
    config: BacktestConfig,
    strategy_key: str,
    strategy_version: str,
    parameters: dict[str, Any],
    dataset_version: str,
    provider: str,
    bar_count: int,
    timeframe: str,
) -> BacktestManifest:
    """Capture every input. Reads git state — I/O, done once, before the run."""
    fill = config.fill_model
    return BacktestManifest(
        strategy_key=strategy_key,
        strategy_version=strategy_version,
        parameters=parameters,
        seed=config.seed,
        code=capture_code_identity(
            REPO,
            engine_version=VERSION,
            feature_pipeline_version=FEATURE_VERSION,
            risk_profile_version=PROFILE_VERSION,
        ),
        data=DataIdentity(
            provider=provider,
            dataset_version=dataset_version,
            instruments=config.instruments,
            timeframe=timeframe,
            start=config.start,
            end=config.end,
            provenance=config.provenance.value,
            spread_assumed=config.spread_assumed,
            bar_count=bar_count,
        ),
        execution=ExecutionModel(
            slippage_model=fill.slippage.value,
            fixed_slippage_pips=fill.fixed_slippage_pips,
            spread_fraction=fill.spread_fraction,
            gap_penalty=fill.gap_penalty,
            commission_model="PER_LOT",
            commission_per_lot=Decimal("7.00"),
            spread_model="ASSUMED_FROM_MID" if config.spread_assumed else "SOURCE_BID_ASK",
            starting_balance=config.starting_balance,
            account_currency=config.account_currency,
            risk_profile=config.risk_profile.value,
            warmup_bars=config.warmup_bars,
        ),
        # Empty, and honestly so: the deterministic pipeline consults no model.
        models=ModelIdentity(),
    )


def _trade_fingerprint(t: ClosedTrade) -> str:
    """Identifies a trade for the result hash.

    Includes prices and timing, not just P&L: two runs can reach the same net
    result through different entries, and that is a determinism break the
    summary would hide.
    """
    return (
        f"{t.instrument}|{t.direction.value}|{t.opened_at.isoformat()}|"
        f"{t.closed_at.isoformat()}|{t.entry_price}|{t.exit_price}|"
        f"{t.lots}|{t.pnl_account_ccy}|{t.reason.value}"
    )


def to_trade_rows(trades: list[ClosedTrade]) -> list[TradeRow]:
    return [
        TradeRow(
            instrument=t.instrument,
            direction=t.direction.value,
            entry_at=t.opened_at,
            exit_at=t.closed_at,
            entry_price=t.entry_price,
            exit_price=t.exit_price,
            lots=t.lots,
            exit_reason=t.reason.value,
            pnl=t.pnl_account_ccy,
            commission=t.commission,
            strategy_key=t.strategy_id,
            trade_id=t.trade_id,
            mfe_pips=t.mfe_pips,
            mae_pips=t.mae_pips,
            ambiguous_exit=t.ambiguous_exit,
            # r_multiple is deliberately absent: the risk actually authorised at
            # entry is not carried on ClosedTrade, and deriving it from the stop
            # would assume the stop never moved. A wrong R is worse than none.
            r_multiple=None,
        )
        for t in trades
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run and record a backtest.")
    parser.add_argument("--instruments", nargs="+", default=["EURUSD", "GBPUSD"])
    parser.add_argument("--timeframe", default="D1")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--balance", default="100000")
    parser.add_argument(
        "--validate", action="store_true", help="Run walk-forward, Monte Carlo and stress"
    )
    parser.add_argument(
        "--spread-pips", default="1.2", help="Synthesised spread for mid-only sources"
    )
    parser.add_argument(
        "--slippage",
        default="PROPORTIONAL_TO_SPREAD",
        choices=[m.value for m in SlippageModel],
        help="STOCHASTIC is the only model that consumes the seed",
    )
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    settings = get_settings()
    timeframe = Timeframe(args.timeframe)

    # The daily CSVs are mid-only, so a spread is synthesised. That is an
    # assumption, not data — it propagates onto spread_assumed and into the
    # manifest so any result derived from it stays identifiable as such.
    series: dict[str, list[Candle]] = {}
    spread_assumed = False
    for symbol in args.instruments:
        path = DATA / f"{symbol}_{args.timeframe}.csv"
        if not path.exists():
            print(f"No data file: {path}")
            return 1
        provider = FileProvider.from_csv(
            path,
            instrument=symbol,
            timeframe=timeframe,
            spread_pips=Decimal(args.spread_pips),
        )
        series[symbol] = provider.candles(symbol, timeframe, EPOCH, FAR_FUTURE)
        spread_assumed = spread_assumed or provider.spread_assumed

    bar_count = sum(len(b) for b in series.values())
    starts = [b[0].open_time for b in series.values()]
    ends = [b[-1].open_time for b in series.values()]

    config = BacktestConfig(
        fill_model=FillModel(slippage=SlippageModel(args.slippage)),
        instruments=tuple(series),
        start=min(starts),
        end=max(ends),
        starting_balance=Decimal(args.balance),
        seed=args.seed,
        provenance=ResultProvenance.IN_SAMPLE,
        spread_assumed=spread_assumed,
    )

    registry = StrategyRegistry()
    for factory in (MovingAverageTrend, VolatilityBreakout):
        # CANDIDATE, not ACTIVE: runnable for research, but ACTIVE is reserved
        # for strategies promoted on evidence, and none has been.
        registry.register(factory(), status=LifecycleStatus.CANDIDATE)

    strategies = registry.all()
    manifest = build_manifest(
        config=config,
        strategy_key="+".join(sorted(r.manifest.key for r in strategies)),
        strategy_version="+".join(sorted(r.manifest.version for r in strategies)),
        parameters={
            r.manifest.key: dict(getattr(r.manifest, "parameters", {})) for r in strategies
        },
        # Content, not clock. See dataset_fingerprint's docstring.
        dataset_version=dataset_fingerprint(series),
        provider="file:yahoo-daily",
        bar_count=bar_count,
        timeframe=args.timeframe,
    )

    if not manifest.is_reproducible:
        print(f"WARNING — {manifest.irreproducible_reason}")
        print("The run will be recorded and flagged as irreproducible.\n")

    engine = BacktestEngine(
        registry=registry,
        specs={s: WATCHLIST[s] for s in series if s in WATCHLIST},
        rates=_rates(),
        prop_profile=GENERIC_TWO_PHASE,
    )

    started = datetime.now(UTC)
    tick = time.perf_counter()
    result = engine.run(series, config)
    duration_ms = int((time.perf_counter() - tick) * 1000)
    completed = datetime.now(UTC)

    metrics = compute_metrics(
        result.trades,
        provenance=config.provenance,
        max_drawdown_pct=result.max_drawdown_pct,
        period_days=(config.end - config.start).days,
        ambiguous_bars=result.ambiguous_bars,
        bars_processed=result.bars_processed,
        spread_assumed=config.spread_assumed,
    )

    validation: dict[str, Any] = {}
    survives_all: bool | None = None
    if args.validate:
        wf = walk_forward(engine, series, config)
        # The prop profile is passed so Monte Carlo can report pass probability
        # against the *evaluation* rules rather than only ruin. For a challenge
        # account, "would this have passed" is the question, not "would it have
        # survived".
        mc = monte_carlo(
            result.trades,
            starting_balance=config.starting_balance,
            prop_profile=GENERIC_TWO_PHASE,
        )
        st = stress_test(engine, series, config)

        validation = {
            "walk_forward": {
                "windows": len(wf.windows),
                "profitable_windows": wf.profitable_windows,
                "consistency": wf.consistency,
            },
            "monte_carlo": {
                "iterations": mc.iterations,
                "ruin_probability": mc.ruin_probability,
                "prop_pass_probability": mc.prop_pass_probability,
                "median_max_drawdown": mc.median_max_drawdown,
                "p95_max_drawdown": mc.p95_max_drawdown,
                "p05_terminal": mc.p05_terminal,
            },
            "stress": {
                sc.name: {
                    "net_pnl": sc.metrics.net_pnl,
                    "degradation": st.degradation(sc.name),
                }
                for sc in st.scenarios
            },
            "stress_survives_all": st.survives_all,
        }

        # Every gate, not a hand-rolled subset. survives_all is StressResult's own
        # property; recomputing it here would let the two drift.
        pass_probability = mc.prop_pass_probability
        survives_all = (
            wf.consistency > Decimal("0.5")
            and st.survives_all
            and pass_probability is not None
            and pass_probability >= Decimal("0.5")
        )

    fingerprints = [_trade_fingerprint(t) for t in result.trades]
    metrics_dict = {k: v for k, v in asdict(metrics).items() if not isinstance(v, tuple)}

    record = RunRecord(
        id=f"bt_{uuid.uuid4().hex[:16]}",
        manifest_hash=manifest.manifest_hash,
        result_hash=result_hash(
            metrics=metrics_dict,
            trade_count=len(result.trades),
            final_balance=result.final_balance,
            trade_fingerprints=fingerprints,
        ),
        manifest=manifest.canonical(),
        manifest_version=MANIFEST_VERSION,
        strategy_key=manifest.strategy_key,
        strategy_version=manifest.strategy_version,
        strategy_lifecycle=LifecycleStatus.CANDIDATE.value,
        started_at=started,
        completed_at=completed,
        created_at=completed,
        duration_ms=duration_ms,
        git_commit=manifest.code.git_commit,
        git_branch=manifest.code.git_branch,
        git_dirty=manifest.code.dirty,
        is_reproducible=manifest.is_reproducible,
        engine_version=VERSION,
        feature_pipeline_version=FEATURE_VERSION,
        risk_profile_version=PROFILE_VERSION,
        market_data_provider=manifest.data.provider,
        dataset_version=manifest.data.dataset_version,
        instruments=config.instruments,
        timeframe=args.timeframe,
        data_start=config.start,
        data_end=config.end,
        bar_count=bar_count,
        provenance=config.provenance.value,
        spread_assumed=config.spread_assumed,
        spread_model=manifest.execution.spread_model,
        slippage_model=manifest.execution.slippage_model,
        commission_model=manifest.execution.commission_model,
        risk_profile=config.risk_profile.value,
        starting_balance=config.starting_balance,
        account_currency=config.account_currency,
        seed=config.seed,
        parameters=manifest.parameters,
        metrics=metrics_dict,
        validation=validation,
        survives_all=survives_all,
        is_evidence=metrics.is_evidence,
        trade_count=len(result.trades),
        final_balance=result.final_balance,
        net_pnl=metrics.net_pnl,
        max_drawdown_pct=result.max_drawdown_pct,
        signals_generated=result.signals_generated,
        proposals_made=result.proposals_made,
        rejections=len(result.rejections),
        notes=args.notes,
        equity_curve=[
            StoreEquityRow(
                at=p.at,
                equity=p.equity,
                balance=p.balance,
                drawdown_pct=p.drawdown_pct,
                open_positions=p.open_positions,
            )
            for p in result.equity_curve
        ],
        trades=to_trade_rows(result.trades),
    )

    async with session_scope() as db:
        await record_run(db, record)

    print(f"  run          {record.id}")
    print(f"  manifest     {manifest.manifest_hash[:26]}…")
    print(f"  result       {record.result_hash[:26]}…")
    print(f"  reproducible {'yes' if manifest.is_reproducible else 'NO'}")
    print(f"  trades       {len(result.trades)}  in {duration_ms} ms")
    print(f"  {metrics.headline}")
    if survives_all is not None:
        print(f"  survives_all {survives_all}")
    print(f"\n  stored to {settings.database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
