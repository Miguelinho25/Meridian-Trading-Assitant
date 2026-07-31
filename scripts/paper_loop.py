"""Drive a paper session and persist every tick.

The edge that owns both the session and the database. The session itself performs
no I/O, so this is where bars come in and state goes out.

    python scripts/paper_loop.py --replay --instruments EURUSD GBPUSD
    python scripts/paper_loop.py --resume ps_abc123 --replay

Two bar sources, and the distinction is recorded on the session rather than left
implicit:

``--replay``
    Historical bars fed at speed. Behaves identically to a live feed, which is
    exactly why it is labelled REPLAY — without that, a replay's equity curve is
    indistinguishable from real paper performance.

``--live``
    Poll the configured provider for bars that have not been seen. On daily bars
    that is one tick a day, which is honest rather than exciting.

State is written after **every acting tick**, not on exit. A process killed
between ticks must lose at most one bar, and a session whose state is only
flushed at shutdown loses everything the moment it is killed rather than stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from nemonis_config import get_settings
from nemonis_config.settings import ApprovalMode, Mode
from nemonis_db import session_scope
from nemonis_db.killswitch import KillSwitchState, current_state, resolve
from nemonis_db.paper_store import (
    ClosedTradeRow,
    DecisionRow,
    PositionRow,
    SessionSnapshot,
    WorkingOrderRow,
    load_snapshot,
    record_decisions,
    record_equity,
    record_trades,
    save_snapshot,
)
from nemonis_marketdata.instruments import WATCHLIST
from nemonis_marketdata.providers import FileProvider
from nemonis_marketdata.types import Candle
from nemonis_paper import PaperSession
from nemonis_risk import GENERIC_TWO_PHASE
from nemonis_schemas.enums import Timeframe
from nemonis_strategy.baselines import MovingAverageTrend, VolatilityBreakout
from nemonis_strategy.plugin import LifecycleStatus
from nemonis_strategy.registry import StrategyRegistry
from nemonis_vault.notes import render_trade_note, trade_note_filename
from nemonis_vault.writer import VaultWriter

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "data" / "raw"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)

_stopping = False


def _request_stop(*_: object) -> None:
    """Ask the loop to finish its current tick and exit.

    Cooperative rather than immediate: aborting mid-tick could persist an account
    that has been debited for a fill whose position was not yet recorded.
    """
    global _stopping
    _stopping = True


def open_vault() -> VaultWriter | None:
    """The research vault, if syncing is enabled.

    Returns None rather than raising when disabled or unwritable: a paper loop
    must not stop trading because a note could not be filed. The journal is a
    record of what happened, not a precondition for it happening.
    """
    settings = get_settings()
    if not settings.vault_sync_enabled:
        return None
    try:
        return VaultWriter(settings.vault_path)
    except OSError as exc:
        print(f"  Vault unavailable ({exc}); journal notes will not be written.")
        return None


def file_trade_notes(
    vault: VaultWriter | None,
    trades: tuple,
    *,
    bar_source: str,
    at: datetime,
) -> int:
    """Write one note per closed trade.

    ``synthetic`` is set from the bar source, not hardcoded. A REPLAY session's
    trades never happened in a live market, and the frontmatter must say so or a
    later query over the vault would treat them as real performance.
    """
    if vault is None or not trades:
        return 0

    written = 0
    for trade in trades:
        spec = WATCHLIST.get(trade.instrument)
        if spec is None:
            continue
        try:
            note = render_trade_note(
                trade,
                spec=spec,
                generated_at=at,
                synthetic=True,
                rule_profile_result=f"paper session, bars: {bar_source}",
            )
            vault.write(trade_note_filename(trade), note, folder="trades")
            written += 1
        except Exception as exc:
            # One unwritable note must not halt the loop or lose the others.
            print(f"  Could not file a note for {trade.trade_id}: {exc}")
    return written


def build_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    for factory in (MovingAverageTrend, VolatilityBreakout):
        registry.register(factory(), status=LifecycleStatus.CANDIDATE)
    return registry


def load_series(
    instruments: list[str], timeframe: str, spread_pips: str
) -> dict[str, list[Candle]]:
    tf = Timeframe(timeframe)
    series: dict[str, list[Candle]] = {}
    for symbol in instruments:
        path = DATA / f"{symbol}_{timeframe}.csv"
        if not path.exists():
            raise SystemExit(f"No data file: {path}")
        provider = FileProvider.from_csv(
            path, instrument=symbol, timeframe=tf, spread_pips=Decimal(spread_pips)
        )
        series[symbol] = provider.candles(symbol, tf, EPOCH, FAR_FUTURE)
    return series


def aligned_timeline(series: dict[str, list[Candle]]) -> list[tuple[datetime, dict[str, Candle]]]:
    """Bars grouped by instant, so every instrument advances together."""
    by_time: dict[datetime, dict[str, Candle]] = {}
    for symbol, bars in series.items():
        for bar in bars:
            by_time.setdefault(bar.open_time, {})[symbol] = bar
    return [(moment, by_time[moment]) for moment in sorted(by_time)]


def _rates() -> dict[str, Decimal]:
    return {
        "USD": Decimal(1),
        "EUR": Decimal("1.08"),
        "GBP": Decimal("1.27"),
        "JPY": Decimal("0.0067"),
    }


#: Latest kill-switch reading, refreshed before every tick. A mutable holder
#: because PaperSession takes a *synchronous* callable and reading the store is
#: async — the runner does the I/O, the session just asks.
#:
#: Seeded True, not False. Before the first read the state is genuinely unknown,
#: and an unknown state must block trading. A False seed would let exactly one
#: tick through on a switch that was already engaged.
_kill_switch_state: dict[str, bool] = {"engaged": True}


async def refresh_kill_switch() -> KillSwitchState:
    """Re-read the switch. Called before each tick, never cached across ticks.

    Reading once at start-up would mean engaging it only took effect on the next
    restart, which is the defect the stored switch exists to fix.
    """
    configured = get_settings().kill_switch
    async with session_scope() as db:
        stored = await current_state(db)
    resolved = resolve(stored=stored, configured=configured)
    _kill_switch_state["engaged"] = resolved.engaged
    return resolved


def make_session(session_id: str, instruments: list[str], balance: str, seed: int) -> PaperSession:
    settings = get_settings()
    return PaperSession(
        session_id=session_id,
        registry=build_registry(),
        specs={s: WATCHLIST[s] for s in instruments if s in WATCHLIST},
        rates=_rates(),
        starting_balance=Decimal(balance),
        risk_profile=settings.risk_profile,
        # PAPER regardless of the configured mode: this loop has no broker
        # adapter to reach, and inheriting a mode that implied one would be the
        # single most dangerous default in the system.
        mode=Mode.PAPER,
        approval_mode=ApprovalMode.AUTO_PAPER_FULL,
        # Reads the value refreshed before this tick, which comes from the
        # database rather than configuration. Engaging it through the API or the
        # UI stops the *next decision*, with no restart.
        kill_switch=lambda: _kill_switch_state["engaged"],
        prop_profile=GENERIC_TWO_PHASE,
        seed=seed,
    )


def to_snapshot(
    session: PaperSession,
    *,
    status: str,
    bar_source: str,
    instruments: list[str],
    timeframe: str,
    started_at: datetime,
    stats: dict[str, int],
    halt_reason: str = "",
) -> SessionSnapshot:
    state = session.state()
    return SessionSnapshot(
        session_id=session.session_id,
        status=status,
        mode=session.mode.value,
        approval_mode=ApprovalMode.AUTO_PAPER_FULL.value,
        risk_profile=get_settings().risk_profile.value,
        prop_profile_id=GENERIC_TWO_PHASE.profile_id,
        bar_source=bar_source,
        instruments=instruments,
        timeframe=timeframe,
        strategy_keys=[r.manifest.key for r in build_registry().all()],
        seed=0,
        account_currency="USD",
        starting_balance=Decimal("100000"),
        balance=state["balance"],
        equity=state["equity"],
        high_water_mark=state["high_water_mark"],
        balance_at_day_start=state["balance_at_day_start"],
        highest_equity_today=state["highest_equity_today"],
        realised_pnl=state["realised_pnl"],
        total_commission=state["total_commission"],
        trading_day=state["trading_day"],
        ticks=state["ticks"],
        last_tick_at=state["last_tick_at"],
        started_at=started_at,
        updated_at=datetime.now(UTC),
        stopped_at=datetime.now(UTC) if status != "RUNNING" else None,
        halt_reason=halt_reason,
        signals_generated=stats["signals"],
        proposals_made=stats["proposals"],
        orders_submitted=stats["submitted"],
        rejections=stats["rejections"],
        # Carried through resume rather than read from the live broker. A
        # restored session starts with an empty closed-trades list -- those
        # trades live in paper_trades, not in memory -- so counting only what is
        # in memory under-reports every trade from before the restart. This
        # reported 629 against 713 stored rows before the fix.
        closed_trade_count=stats["closed"] + len(session.closed_trades),
        positions=[PositionRow(**p) for p in state["positions"]],
        working_orders=[WorkingOrderRow(**o) for o in state["working_orders"]],
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run a paper-trading session.")
    parser.add_argument("--instruments", nargs="+", default=["EURUSD", "GBPUSD"])
    parser.add_argument("--timeframe", default="D1")
    parser.add_argument("--balance", default="100000")
    parser.add_argument("--spread-pips", default="1.2")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--resume", default="", help="Resume an existing session id")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--replay", action="store_true", help="Feed historical bars at speed")
    source.add_argument("--live", action="store_true", help="Poll the provider for new bars")
    parser.add_argument("--max-ticks", type=int, default=0, help="0 means the whole series")
    parser.add_argument(
        "--from-bar", type=int, default=0, help="Skip ahead in a replay (warmup is still needed)"
    )
    args = parser.parse_args()

    if args.live:
        # Stated rather than silently degraded: the provider polls daily bars, so
        # a live loop ticks about once a day and would look broken otherwise.
        print("--live is not implemented: the configured provider serves daily bars,")
        print("so a live loop would tick once per day. Use --replay for now.")
        return 2

    bar_source = "REPLAY"
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    series = load_series(args.instruments, args.timeframe, args.spread_pips)
    steps = aligned_timeline(series)
    if args.from_bar:
        steps = steps[args.from_bar :]
    if args.max_ticks:
        steps = steps[: args.max_ticks]

    session_id = args.resume or f"ps_{uuid.uuid4().hex[:12]}"
    session = make_session(session_id, args.instruments, args.balance, args.seed)
    started_at = datetime.now(UTC)
    stats = {"signals": 0, "proposals": 0, "submitted": 0, "rejections": 0, "closed": 0}

    if args.resume:
        async with session_scope() as db:
            snap = await load_snapshot(db, session_id)
        if snap is None:
            print(f"No session {session_id} to resume.")
            return 1
        session.restore(
            {
                "balance": snap.balance,
                "equity": snap.equity,
                "high_water_mark": snap.high_water_mark,
                "balance_at_day_start": snap.balance_at_day_start,
                "highest_equity_today": snap.highest_equity_today,
                "realised_pnl": snap.realised_pnl,
                "total_commission": snap.total_commission,
                "trading_day": snap.trading_day,
                "ticks": snap.ticks,
                "last_tick_at": snap.last_tick_at,
                "positions": [asdict(p) for p in snap.positions],
                "working_orders": [asdict(o) for o in snap.working_orders],
            }
        )
        stats = {
            "signals": snap.signals_generated,
            "proposals": snap.proposals_made,
            "submitted": snap.orders_submitted,
            "rejections": snap.rejections,
            "closed": snap.closed_trade_count,
        }
        started_at = snap.started_at
        print(
            f"Resumed {session_id}: {len(snap.positions)} open position(s), "
            f"{len(snap.working_orders)} working order(s), balance {snap.balance}"
        )
        # Bars already consumed must not be replayed: settling one twice would
        # double-count its fills. The session's own staleness guard would reject
        # them, but skipping is cheaper and states the intent.
        if snap.last_tick_at is not None:
            steps = [(m, b) for m, b in steps if m > snap.last_tick_at]

    print(f"Session {session_id} — {bar_source}, {len(steps)} bars to process\n")

    peak = Decimal(args.balance)
    acted = 0
    vault = open_vault()
    notes_written = 0

    halted_announced = False

    for moment, bars in steps:
        if _stopping:
            print("\nStop requested — finishing cleanly.")
            break

        # Before the tick, not after: a switch engaged a moment ago must stop the
        # decision about to be made, not the one after it.
        switch = await refresh_kill_switch()
        if switch.engaged and not halted_announced:
            print(f"  {switch.summary}")
            print("  Positions are still settled; no new trade will be permitted.")
            halted_announced = True
        elif not switch.engaged and halted_announced:
            print(f"  Kill switch released at {moment.date()} — trading resumes.")
            halted_announced = False

        outcome = session.tick(bars, at=moment)
        if not outcome.acted:
            continue

        acted += 1
        stats["signals"] += outcome.signals_generated
        stats["proposals"] += outcome.proposals_made
        stats["submitted"] += outcome.submitted
        stats["rejections"] += sum(1 for d in outcome.decisions if d[2] == "REJECTED")

        peak = max(peak, outcome.equity)
        drawdown = (peak - outcome.equity) / peak * Decimal(100) if peak > 0 else Decimal(0)

        # Written every acting tick, not on exit: a process killed between ticks
        # must lose at most one bar.
        async with session_scope() as db:
            await save_snapshot(
                db,
                to_snapshot(
                    session,
                    status="RUNNING",
                    bar_source=bar_source,
                    instruments=args.instruments,
                    timeframe=args.timeframe,
                    started_at=started_at,
                    stats=stats,
                ),
            )
            await record_equity(
                db,
                session_id,
                at=moment,
                equity=outcome.equity,
                balance=outcome.balance,
                drawdown_pct=drawdown,
                open_positions=len(session.account.positions),
            )
            # Every decision, not just the approvals. The rejection reasons are
            # what answer "why is it not trading?", and an aggregate count cannot.
            if outcome.decisions:
                await record_decisions(
                    db,
                    session_id,
                    [
                        DecisionRow(
                            at=when,
                            strategy_id=strategy_id,
                            verdict=verdict,
                            reason_code=reason,
                        )
                        for when, strategy_id, verdict, reason in outcome.decisions
                    ],
                )

            notes_written += file_trade_notes(
                vault, outcome.closed_trades, bar_source=bar_source, at=moment
            )

            if outcome.closed_trades:
                await record_trades(
                    db,
                    session_id,
                    [
                        ClosedTradeRow(
                            trade_id=t.trade_id,
                            instrument=t.instrument,
                            direction=t.direction.value,
                            lots=t.lots,
                            entry_price=t.entry_price,
                            exit_price=t.exit_price,
                            opened_at=t.opened_at,
                            closed_at=t.closed_at,
                            pnl=t.pnl_account_ccy,
                            commission=t.commission,
                            strategy_id=t.strategy_id,
                            exit_reason=t.reason.value,
                            mfe_pips=t.mfe_pips,
                            mae_pips=t.mae_pips,
                            ambiguous_exit=t.ambiguous_exit,
                        )
                        for t in outcome.closed_trades
                    ],
                )

    async with session_scope() as db:
        await save_snapshot(
            db,
            to_snapshot(
                session,
                status="STOPPED",
                bar_source=bar_source,
                instruments=args.instruments,
                timeframe=args.timeframe,
                started_at=started_at,
                stats=stats,
                halt_reason="Stop requested" if _stopping else "",
            ),
        )

    print(f"  session      {session_id}")
    print(f"  ticks acted  {acted}")
    print(f"  signals      {stats['signals']}")
    print(f"  proposals    {stats['proposals']}  submitted {stats['submitted']}")
    print(f"  rejections   {stats['rejections']}")
    this_run = len(session.closed_trades)
    print(f"  trades       {stats['closed'] + this_run} ({this_run} this run)")
    print(f"  balance      {session.account.balance}")
    print(f"  open         {len(session.account.positions)} position(s)")
    if vault is not None:
        print(f"  notes        {notes_written} filed to {vault.root}")
    if _kill_switch_state["engaged"]:
        print("\n  KILL SWITCH ENGAGED — positions were settled, no new trade permitted.")
    return 0


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(asyncio.run(main()))
