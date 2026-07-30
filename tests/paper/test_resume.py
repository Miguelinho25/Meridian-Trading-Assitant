"""Resuming a paper session.

The failure this guards against is not lost bookkeeping. A session reloaded
without its positions and working orders has **orphaned live exposure** — risk
nothing will now manage, with stops that will never be honoured. Everything else
in a snapshot can be recomputed; that cannot.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from nemonis_config.settings import ApprovalMode, Mode, RiskProfileName
from nemonis_db.paper_store import (
    ClosedTradeRow,
    DecisionRow,
    PositionRow,
    SessionSnapshot,
    WorkingOrderRow,
    decision_breakdown,
    get_equity_curve,
    get_positions,
    get_trades,
    load_snapshot,
    recent_decisions,
    record_decisions,
    record_equity,
    record_trades,
    save_snapshot,
)
from nemonis_marketdata import SyntheticGenerator
from nemonis_marketdata.instruments import WATCHLIST
from nemonis_paper import PaperSession
from nemonis_risk.propfirm import GENERIC_TWO_PHASE
from nemonis_strategy import (
    LifecycleStatus,
    MovingAverageTrend,
    StrategyRegistry,
    VolatilityBreakout,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.risk.conftest import RATES

T = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
START = datetime(2026, 1, 5, tzinfo=UTC)
SYMBOLS = ("EURUSD", "GBPUSD")


def a_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    for factory in (MovingAverageTrend, VolatilityBreakout):
        registry.register(factory(), status=LifecycleStatus.CANDIDATE)
    return registry


def a_session(session_id: str = "ps_1") -> PaperSession:
    return PaperSession(
        session_id=session_id,
        registry=a_registry(),
        specs=dict(WATCHLIST),
        rates=RATES,
        starting_balance=Decimal("100000"),
        risk_profile=RiskProfileName.CHALLENGE,
        mode=Mode.PAPER,
        approval_mode=ApprovalMode.AUTO_PAPER_FULL,
        kill_switch=lambda: False,
        prop_profile=GENERIC_TWO_PHASE,
    )


def a_snapshot(session: PaperSession, **overrides) -> SessionSnapshot:
    state = session.state()
    base = {
        "session_id": session.session_id,
        "status": "RUNNING",
        "mode": session.mode.value,
        "approval_mode": "AUTO_PAPER_FULL",
        "risk_profile": "CHALLENGE",
        "account_currency": "USD",
        "starting_balance": Decimal("100000"),
        "balance": state["balance"],
        "equity": state["equity"],
        "high_water_mark": state["high_water_mark"],
        "balance_at_day_start": state["balance_at_day_start"],
        "highest_equity_today": state["highest_equity_today"],
        "realised_pnl": state["realised_pnl"],
        "total_commission": state["total_commission"],
        "trading_day": state["trading_day"],
        "ticks": state["ticks"],
        "last_tick_at": state["last_tick_at"],
        "started_at": T,
        "updated_at": T,
        "instruments": list(SYMBOLS),
        "positions": [PositionRow(**p) for p in state["positions"]],
        "working_orders": [WorkingOrderRow(**o) for o in state["working_orders"]],
    }
    return SessionSnapshot(**{**base, **overrides})


def bars_upto(count: int) -> list:
    series = {s: SyntheticGenerator(s, seed=2024).generate_list(START, 400) for s in SYMBOLS}
    by_time: dict = {}
    for symbol, bars in series.items():
        for bar in bars:
            by_time.setdefault(bar.open_time, {})[symbol] = bar
    return [(m, by_time[m]) for m in sorted(by_time)][:count]


async def a_running_session(steps: int = 200) -> PaperSession:
    session = a_session()
    for moment, bars in bars_upto(steps):
        session.tick(bars, at=moment)
    return session


class TestOpenExposureSurvivesARestart:
    """The one thing a snapshot must never lose."""

    async def test_open_positions_are_restored(self, session: AsyncSession) -> None:
        live = await a_running_session()
        if not live.account.positions:
            pytest.skip("fixture held no open position at the cut-off")

        await save_snapshot(session, a_snapshot(live))
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None

        resumed = a_session()
        resumed.restore(
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

        assert set(resumed.account.positions) == set(live.account.positions)
        for pid, original in live.account.positions.items():
            restored = resumed.account.positions[pid]
            assert restored.instrument == original.instrument
            assert restored.direction == original.direction
            assert restored.lots == original.lots
            assert restored.entry_price == original.entry_price

            # Losing these would leave the position with no protective levels.
            #
            # Compared to storage precision rather than exactly. DecimalText
            # stores 10 decimal places, and these stops arrive from risk-sizing
            # division carrying ~28 significant digits, so the round trip loses
            # digits below 1e-10 — about 1e-6 of a pip. That is immaterial to a
            # price, but it is only immaterial because the tolerance is stated;
            # an exact assertion here would fail for a reason that has nothing to
            # do with exposure being preserved. See the separate issue about
            # quantising stops to the instrument tick grid, which no venue would
            # accept unquantised.
            tolerance = Decimal("1e-9")
            assert restored.stop_loss is not None
            assert original.stop_loss is not None
            assert abs(restored.stop_loss - original.stop_loss) < tolerance
            if original.take_profit is None:
                assert restored.take_profit is None
            else:
                assert restored.take_profit is not None
                assert abs(restored.take_profit - original.take_profit) < tolerance

    async def test_the_daily_loss_reference_survives(self, session: AsyncSession) -> None:
        """Restoring the balance without this would make the first tick after a
        restart measure its daily loss from the wrong point — the same defect
        that turned the daily limit into a lifetime one."""
        live = await a_running_session()
        await save_snapshot(session, a_snapshot(live))
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None
        assert snap.balance_at_day_start == live.account.balance_at_day_start
        assert snap.highest_equity_today == live.account.highest_equity_today
        assert snap.trading_day == live.trading_day

    async def test_account_totals_survive(self, session: AsyncSession) -> None:
        live = await a_running_session()
        await save_snapshot(session, a_snapshot(live))
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None
        assert snap.balance == live.account.balance
        assert snap.high_water_mark == live.account.high_water_mark
        assert snap.realised_pnl == live.account.realised_pnl
        assert snap.total_commission == live.account.total_commission


class TestSnapshotsReplaceRatherThanMerge:
    """A position closed while the process was down must disappear. An
    upsert-only write would resurrect it on the next restart."""

    async def test_a_closed_position_is_removed(self, session: AsyncSession) -> None:
        live = await a_running_session()
        await save_snapshot(session, a_snapshot(live))
        before = len(await get_positions(session, "ps_1"))

        # Simulate every position closing, then snapshot again.
        live.account.positions.clear()
        await save_snapshot(session, a_snapshot(live))

        after = await get_positions(session, "ps_1")
        assert after == [], f"{len(after)} positions survived a flat snapshot (was {before})"

    async def test_a_filled_working_order_is_removed(self, session: AsyncSession) -> None:
        live = await a_running_session(100)
        assert live.broker.state.working, "no order in flight — nothing to remove"
        await save_snapshot(session, a_snapshot(live))
        live.broker.state.working.clear()
        await save_snapshot(session, a_snapshot(live))
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None
        assert snap.working_orders == []


class TestOrderHistorySurvives:
    """OrderLifecycle's docstring notes that rebuilding history from a final
    state is impossible, and architecture.md requires every transition."""

    async def test_transition_history_round_trips(self, session: AsyncSession) -> None:
        # 100 steps, not the default 200: at 200 the fixture holds no queued
        # order, so the test would skip and prove nothing. Chosen by inspecting
        # where orders are actually in flight.
        live = await a_running_session(100)
        orders = live.state()["working_orders"]
        assert orders, "no working order queued — this test would prove nothing"

        await save_snapshot(session, a_snapshot(live))
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None

        original = {o["order_id"]: o["lifecycle_history"] for o in orders}
        for restored in snap.working_orders:
            expected = original[restored.order_id]
            assert len(restored.lifecycle_history) == len(expected)
            for got, want in zip(restored.lifecycle_history, expected, strict=True):
                assert got["to_state"] == want["to_state"]
                assert got["actor"] == want["actor"]
                # Parsed back to a datetime, not left as text that looks like one.
                assert isinstance(got["at"], datetime)
                assert got["at"] == want["at"]


class TestAppendOnlyRecords:
    async def test_a_repeated_equity_instant_is_not_double_written(
        self, session: AsyncSession
    ) -> None:
        """A retried tick must not double-write the curve."""
        live = await a_running_session(60)
        await save_snapshot(session, a_snapshot(live))
        for _ in range(2):
            await record_equity(
                session,
                "ps_1",
                at=T,
                equity=Decimal("100500"),
                balance=Decimal("100000"),
                drawdown_pct=Decimal("0"),
                open_positions=1,
            )
        assert len(await get_equity_curve(session, "ps_1")) == 1

    async def test_a_repeated_trade_is_skipped(self, session: AsyncSession) -> None:
        live = await a_running_session(60)
        await save_snapshot(session, a_snapshot(live))
        trade = ClosedTradeRow(
            trade_id="t1",
            instrument="EURUSD",
            direction="LONG",
            lots=Decimal("0.1"),
            entry_price=Decimal("1.1"),
            exit_price=Decimal("1.11"),
            opened_at=T,
            closed_at=T + timedelta(hours=1),
            pnl=Decimal("100"),
            commission=Decimal("0.7"),
        )
        assert await record_trades(session, "ps_1", [trade]) == 1
        assert await record_trades(session, "ps_1", [trade]) == 0
        assert len(await get_trades(session, "ps_1")) == 1


class TestDenormalisedCountersSurviveResume:
    """A restored session's broker starts with an empty closed-trades list --
    those trades live in paper_trades, not in memory. A counter taken from memory
    alone under-reports everything from before the restart, which is what the
    runner did: it wrote 629 against 713 stored rows."""

    async def test_the_trade_count_must_include_pre_restart_trades(
        self, session: AsyncSession
    ) -> None:
        live = await a_running_session(100)
        first_run = len(live.closed_trades)
        assert first_run > 0, "no trades in the first run — this proves nothing"

        await save_snapshot(session, a_snapshot(live, closed_trade_count=first_run))

        # Resume: a fresh session with restored exposure but no in-memory trades.
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None
        resumed = a_session()
        resumed.restore(
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
        assert resumed.closed_trades == [], (
            "restore reinstated closed trades into memory; they are persisted "
            "separately and would then be double-counted"
        )

        # The runner carries the prior count forward. Writing len(closed_trades)
        # here would report 0 for a session that has traded.
        carried = snap.closed_trade_count + len(resumed.closed_trades)
        assert carried == first_run

    async def test_a_snapshot_after_resume_does_not_lose_the_count(
        self, session: AsyncSession
    ) -> None:
        live = await a_running_session(100)
        first_run = len(live.closed_trades)
        await save_snapshot(session, a_snapshot(live, closed_trade_count=first_run))

        resumed = a_session()
        await save_snapshot(session, a_snapshot(resumed, closed_trade_count=first_run + 0))
        snap = await load_snapshot(session, "ps_1")
        assert snap is not None
        assert snap.closed_trade_count == first_run


class TestDecisionsAreRecordedNotJustCounted:
    """The runner counted rejections and discarded their reasons. A system that
    records only what it did cannot say what it declined, or why — and 'why is it
    not trading?' is the most common question asked of a running session."""

    async def test_rejections_are_stored_with_their_reason(self, session: AsyncSession) -> None:
        live = await a_running_session(120)
        await save_snapshot(session, a_snapshot(live))

        # Replay the tick decisions the runner would have written.
        #
        # One session across the whole loop, not a fresh one per tick: a new
        # session has no history, never clears warmup, and therefore produces no
        # decisions at all — which made an earlier version of this test skip and
        # prove nothing.
        replay = a_session("ps_replay")
        rows = [
            DecisionRow(at=when, strategy_id=sid, verdict=verdict, reason_code=reason)
            for moment, bars in bars_upto(120)
            for when, sid, verdict, reason in replay.tick(bars, at=moment).decisions
        ]
        assert rows, "no decisions produced — this test would prove nothing"

        await record_decisions(session, "ps_1", rows)
        stored = await recent_decisions(session, "ps_1", limit=1000)
        assert len(stored) == len(rows)
        assert any(d.verdict == "REJECTED" for d in stored)
        assert any(d.reason_code for d in stored), "no reason codes survived"

    async def test_the_breakdown_aggregates_by_reason(self, session: AsyncSession) -> None:
        live = await a_running_session(60)
        await save_snapshot(session, a_snapshot(live))
        await record_decisions(
            session,
            "ps_1",
            [
                DecisionRow(at=T, strategy_id="a", verdict="REJECTED", reason_code="X"),
                DecisionRow(at=T, strategy_id="a", verdict="REJECTED", reason_code="X"),
                DecisionRow(at=T, strategy_id="b", verdict="APPROVED", reason_code=""),
            ],
        )
        breakdown = await decision_breakdown(session, "ps_1")
        assert ("REJECTED", "X", 2) in breakdown
        assert ("APPROVED", "", 1) in breakdown
        # Most frequent first, so the dominant blocker leads.
        assert breakdown[0][2] >= breakdown[-1][2]

    async def test_a_clean_approval_carries_no_reason_code(self, session: AsyncSession) -> None:
        """The absence is meaningful, so it is not backfilled with a placeholder."""
        live = await a_running_session(60)
        await save_snapshot(session, a_snapshot(live))
        await record_decisions(
            session, "ps_1", [DecisionRow(at=T, strategy_id="a", verdict="APPROVED")]
        )
        stored = await recent_decisions(session, "ps_1")
        assert stored[0].reason_code == ""
