"""Backtest research records.

Two guarantees are load-bearing here. Runs are append-only, because a result
whose row could be edited afterwards is not evidence of anything. And a
determinism break — same manifest, different result — must be findable by query
rather than by someone remembering to look.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from nemonis_db.backtest_store import (
    EquityRow,
    ImmutableRecordError,
    RunRecord,
    TradeRow,
    find_determinism_breaks,
    get_equity_curve,
    get_run,
    get_trades,
    list_runs,
    record_run,
)
from sqlalchemy.ext.asyncio import AsyncSession

T = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def a_record(run_id: str = "run-1", **overrides) -> RunRecord:
    base = {
        "id": run_id,
        "manifest_hash": "sha256:aaa",
        "result_hash": "sha256:bbb",
        "manifest": {"strategy_key": "ema", "seed": 42},
        "manifest_version": "1.0.0",
        "strategy_key": "ema-pullback",
        "strategy_version": "1.0.0",
        "started_at": T,
        "completed_at": T + timedelta(seconds=8),
        "created_at": T,
        "duration_ms": 8000,
        "instruments": ("EURUSD", "GBPUSD"),
        "provenance": "REAL",
        "is_reproducible": True,
        "trade_count": 2,
        "metrics": {"net_pnl": Decimal("1250.50"), "profit_factor": Decimal("1.02")},
        "validation": {"survives_all": False, "monte_carlo_pass_pct": Decimal("0.182")},
        "survives_all": False,
        "final_balance": Decimal("101250.50"),
    }
    return RunRecord(**{**base, **overrides})


class TestRecording:
    async def test_a_run_is_stored_and_retrievable(self, session: AsyncSession) -> None:
        await record_run(session, a_record())
        run = await get_run(session, "run-1")
        assert run is not None
        assert run.strategy_key == "ema-pullback"
        assert run.trade_count == 2

    async def test_the_full_manifest_is_stored_as_the_record_of_truth(
        self, session: AsyncSession
    ) -> None:
        """Columns are for querying; the manifest must survive a schema change."""
        await record_run(session, a_record())
        run = await get_run(session, "run-1")
        assert run is not None
        assert json.loads(run.manifest_json) == {"strategy_key": "ema", "seed": 42}

    async def test_decimals_survive_the_round_trip_exactly(self, session: AsyncSession) -> None:
        """Stored as text, never as float — a rounded P&L is a wrong P&L."""
        await record_run(session, a_record(final_balance=Decimal("101250.50")))
        run = await get_run(session, "run-1")
        assert run is not None
        assert run.final_balance == Decimal("101250.50")
        assert json.loads(run.metrics)["net_pnl"] == "1250.50"

    async def test_the_equity_curve_is_stored_in_order(self, session: AsyncSession) -> None:
        curve = [
            EquityRow(
                at=T + timedelta(days=i),
                equity=Decimal(100000 + i),
                balance=Decimal(100000 + i),
                drawdown_pct=Decimal("0.0"),
            )
            for i in range(5)
        ]
        await record_run(session, a_record(equity_curve=curve))
        points = await get_equity_curve(session, "run-1")
        assert [p.sequence for p in points] == [0, 1, 2, 3, 4]
        assert points[4].equity == Decimal("100004")

    async def test_trades_are_stored_individually(self, session: AsyncSession) -> None:
        """Two runs can reach the same net P&L through different trades."""
        trades = [
            TradeRow(
                instrument="EURUSD",
                direction="LONG",
                entry_at=T,
                entry_price=Decimal("1.1000"),
                exit_price=Decimal("1.1050"),
                pnl=Decimal("500"),
                r_multiple=Decimal("2.0"),
                session="LONDON",
                regime_label="TRENDING",
            )
        ]
        await record_run(session, a_record(trades=trades))
        stored = await get_trades(session, "run-1")
        assert len(stored) == 1
        assert stored[0].r_multiple == Decimal("2.0")
        assert stored[0].regime_label == "TRENDING"
        # Inherits the run's strategy when the trade does not name one.
        assert stored[0].strategy_key == "ema-pullback"


class TestAppendOnly:
    """A result whose row could be edited afterwards is not evidence."""

    async def test_re_recording_the_same_id_is_refused(self, session: AsyncSession) -> None:
        await record_run(session, a_record())
        with pytest.raises(ImmutableRecordError, match="append-only"):
            await record_run(session, a_record(net_pnl=Decimal("999999")))

    async def test_the_original_survives_a_refused_overwrite(self, session: AsyncSession) -> None:
        await record_run(session, a_record(final_balance=Decimal("101250.50")))
        with pytest.raises(ImmutableRecordError):
            await record_run(session, a_record(final_balance=Decimal("999999")))
        run = await get_run(session, "run-1")
        assert run is not None
        assert run.final_balance == Decimal("101250.50")


class TestReproducibilityIsRecordedHonestly:
    async def test_a_dirty_run_is_marked_irreproducible(self, session: AsyncSession) -> None:
        await record_run(
            session, a_record(git_dirty=True, is_reproducible=False, git_commit="a" * 40)
        )
        run = await get_run(session, "run-1")
        assert run is not None
        assert run.git_dirty
        assert not run.is_reproducible

    async def test_irreproducible_runs_can_be_filtered_out(self, session: AsyncSession) -> None:
        await record_run(session, a_record("clean", is_reproducible=True))
        await record_run(session, a_record("dirty", is_reproducible=False, git_dirty=True))
        assert [r.id for r in await list_runs(session, reproducible_only=True)] == ["clean"]

    async def test_unrun_validation_is_not_a_pass(self, session: AsyncSession) -> None:
        """None means validation was not run, which differs from run-and-failed
        and must never be displayed as a pass."""
        await record_run(session, a_record(survives_all=None))
        run = await get_run(session, "run-1")
        assert run is not None
        assert run.survives_all is None

        assert await list_runs(session, survives_all=True) == []
        assert await list_runs(session, survives_all=False) == []


class TestFiltering:
    async def test_runs_are_newest_first(self, session: AsyncSession) -> None:
        await record_run(session, a_record("old", created_at=T - timedelta(days=2)))
        await record_run(session, a_record("new", created_at=T))
        assert [r.id for r in await list_runs(session)] == ["new", "old"]

    async def test_filtering_by_strategy(self, session: AsyncSession) -> None:
        await record_run(session, a_record("a", strategy_key="ema"))
        await record_run(session, a_record("b", strategy_key="breakout"))
        assert [r.id for r in await list_runs(session, strategy_key="ema")] == ["a"]

    async def test_synthetic_runs_are_separable_from_real_ones(self, session: AsyncSession) -> None:
        """A synthetic result must never be mistaken for a real one."""
        await record_run(session, a_record("real", provenance="REAL"))
        await record_run(session, a_record("synth", provenance="SYNTHETIC"))
        assert [r.id for r in await list_runs(session, provenance="REAL")] == ["real"]


class TestDeterminismBreaks:
    """Same inputs, different outputs. The query the two-hash design exists for."""

    async def test_no_breaks_when_results_agree(self, session: AsyncSession) -> None:
        await record_run(session, a_record("a", manifest_hash="m1", result_hash="r1"))
        await record_run(session, a_record("b", manifest_hash="m1", result_hash="r1"))
        assert await find_determinism_breaks(session) == []

    async def test_different_manifests_are_not_a_break(self, session: AsyncSession) -> None:
        """Different inputs are expected to give different results."""
        await record_run(session, a_record("a", manifest_hash="m1", result_hash="r1"))
        await record_run(session, a_record("b", manifest_hash="m2", result_hash="r2"))
        assert await find_determinism_breaks(session) == []

    async def test_the_same_manifest_with_different_results_is_reported(
        self, session: AsyncSession
    ) -> None:
        await record_run(session, a_record("a", manifest_hash="m1", result_hash="r1"))
        await record_run(session, a_record("b", manifest_hash="m1", result_hash="r2"))

        breaks = await find_determinism_breaks(session)
        assert len(breaks) == 1
        assert breaks[0].manifest_hash == "m1"
        assert set(breaks[0].run_ids) == {"a", "b"}
        assert set(breaks[0].result_hashes) == {"r1", "r2"}

    async def test_the_summary_states_the_consequence(self, session: AsyncSession) -> None:
        """Not 'results differ' — that none of their statistics can be trusted."""
        await record_run(session, a_record("a", manifest_hash="m1", result_hash="r1"))
        await record_run(session, a_record("b", manifest_hash="m1", result_hash="r2"))
        summary = (await find_determinism_breaks(session))[0].summary
        assert "not deterministic" in summary
        assert "cannot be trusted" in summary or "can be trusted" in summary
