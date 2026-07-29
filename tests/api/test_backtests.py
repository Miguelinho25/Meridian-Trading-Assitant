"""Backtest research endpoints.

The unit tests here exist because of a real bug. ``max_drawdown_pct`` is already
a percentage — engine.py computes ``(peak - equity) / peak * 100`` — while the
throttle bands use fractions (``0.20`` meaning 20%) under the same ``_pct``
naming. The Backtest Lab multiplied by 100 a second time and rendered a 5.36%
drawdown as **535.7%**. The unit is pinned here so the ambiguity cannot bite
silently again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from nemonis_db import session_scope
from nemonis_db.backtest_store import EquityRow, RunRecord, TradeRow, record_run

T = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def a_record(run_id: str = "bt_test1", **overrides) -> RunRecord:
    base = {
        "id": run_id,
        "manifest_hash": "sha256:aaa",
        "result_hash": "sha256:bbb",
        "manifest": {"seed": 42},
        "manifest_version": "1.0.0",
        "strategy_key": "ema-pullback",
        "strategy_version": "1.0.0",
        "started_at": T,
        "completed_at": T + timedelta(seconds=5),
        "created_at": T,
        "instruments": ("EURUSD",),
        "timeframe": "D1",
        "provenance": "IN_SAMPLE",
        "trade_count": 1,
        "net_pnl": Decimal("-4818.05"),
        # 5.357%, not 0.05357. The unit this file exists to pin.
        "max_drawdown_pct": Decimal("5.3572631432"),
        "is_reproducible": True,
        "survives_all": None,
        "metrics": {"net_pnl": Decimal("-4818.05")},
    }
    return RunRecord(**{**base, **overrides})


@pytest.fixture
async def seeded(client: AsyncClient) -> AsyncClient:
    async with session_scope() as db:
        await record_run(
            db,
            a_record(
                equity_curve=[
                    EquityRow(
                        at=T + timedelta(days=i),
                        equity=Decimal(100000 - i * 10),
                        balance=Decimal(100000 - i * 10),
                        drawdown_pct=Decimal("1.5"),
                    )
                    for i in range(2000)
                ],
                trades=[
                    TradeRow(
                        instrument="EURUSD",
                        direction="LONG",
                        entry_at=T,
                        entry_price=Decimal("1.1000"),
                        exit_price=Decimal("1.0950"),
                        pnl=Decimal("-500"),
                        mfe_pips=Decimal("12.5"),
                        mae_pips=Decimal("60.0"),
                    )
                ],
            ),
        )
    return client


class TestDrawdownUnits:
    """A 5.36% drawdown displayed as 535.7% is not a cosmetic defect: it makes a
    survivable strategy look like a blown account, and the reverse."""

    async def test_drawdown_is_served_as_a_percentage_not_a_fraction(
        self, seeded: AsyncClient
    ) -> None:
        run = (await seeded.get("/api/backtests")).json()[0]
        assert Decimal(run["max_drawdown_pct"]) == Decimal("5.3572631432")

    async def test_a_plausible_drawdown_stays_under_one_hundred(self, seeded: AsyncClient) -> None:
        """Catches a fraction/percentage flip in either direction: a fraction
        served here would read as 0.05%, a double-scaled value as 535%."""
        run = (await seeded.get("/api/backtests")).json()[0]
        drawdown = Decimal(run["max_drawdown_pct"])
        assert Decimal(1) < drawdown < Decimal(100)


class TestIndex:
    async def test_runs_are_listed(self, seeded: AsyncClient) -> None:
        body = (await seeded.get("/api/backtests")).json()
        assert len(body) == 1
        assert body[0]["strategy_key"] == "ema-pullback"

    async def test_numbers_cross_the_wire_as_strings(self, seeded: AsyncClient) -> None:
        run = (await seeded.get("/api/backtests")).json()[0]
        assert isinstance(run["net_pnl"], str)
        assert isinstance(run["max_drawdown_pct"], str)

    async def test_unvalidated_is_not_reported_as_a_pass(self, seeded: AsyncClient) -> None:
        """None means validation was not run. Coercing it to false would say
        'failed'; to true would be a lie."""
        assert (await seeded.get("/api/backtests")).json()[0]["survives_all"] is None

    async def test_an_in_sample_run_is_not_evidence(self, seeded: AsyncClient) -> None:
        assert (await seeded.get("/api/backtests")).json()[0]["is_evidence"] is False


class TestAppendOnlyOverHttp:
    """Records are append-only, so no route may edit or delete one."""

    async def test_no_write_routes_exist(self, seeded: AsyncClient) -> None:
        for method in (seeded.post, seeded.put, seeded.patch, seeded.delete):
            assert (await method("/api/backtests")).status_code == 405

    async def test_a_run_cannot_be_deleted(self, seeded: AsyncClient) -> None:
        assert (await seeded.delete("/api/backtests/bt_test1")).status_code == 405


class TestDetail:
    async def test_the_full_manifest_is_returned(self, seeded: AsyncClient) -> None:
        body = (await seeded.get("/api/backtests/bt_test1")).json()
        assert body["manifest"] == {"seed": 42}
        assert body["manifest_version"] == "1.0.0"

    async def test_a_missing_run_is_404(self, seeded: AsyncClient) -> None:
        assert (await seeded.get("/api/backtests/bt_nope")).status_code == 404

    async def test_a_reproducible_run_carries_no_reason(self, seeded: AsyncClient) -> None:
        assert (await seeded.get("/api/backtests/bt_test1")).json()["irreproducible_reason"] == ""


class TestEquityDownsampling:
    async def test_a_long_curve_is_downsampled(self, seeded: AsyncClient) -> None:
        points = (await seeded.get("/api/backtests/bt_test1/equity?max_points=100")).json()
        assert 0 < len(points) <= 102

    async def test_the_endpoints_of_the_curve_are_preserved(self, seeded: AsyncClient) -> None:
        """Downsampling by stride can drop the final point, which is exactly the
        one a reader looks at — the equity the run actually ended on."""
        full = (await seeded.get("/api/backtests/bt_test1/equity?max_points=5000")).json()
        sampled = (await seeded.get("/api/backtests/bt_test1/equity?max_points=100")).json()
        assert sampled[0]["at"] == full[0]["at"]
        assert sampled[-1]["at"] == full[-1]["at"]
        assert sampled[-1]["equity"] == full[-1]["equity"]


class TestTrades:
    async def test_excursions_are_returned(self, seeded: AsyncClient) -> None:
        """MFE/MAE are how stop placement is judged; the P&L alone destroys them."""
        trade = (await seeded.get("/api/backtests/bt_test1/trades")).json()[0]
        assert Decimal(trade["mfe_pips"]) == Decimal("12.5")
        assert Decimal(trade["mae_pips"]) == Decimal("60.0")

    async def test_storage_padding_is_stripped_for_display(self, seeded: AsyncClient) -> None:
        """DecimalText stores at a fixed scale; "12.5000000000" would reach the
        UI as noise."""
        trade = (await seeded.get("/api/backtests/bt_test1/trades")).json()[0]
        assert trade["mfe_pips"] == "12.5"

    async def test_stripping_never_produces_exponent_notation(self, seeded: AsyncClient) -> None:
        """Decimal.normalize() turns 100 into 1E+2, which is not a price."""
        for value in (await seeded.get("/api/backtests")).json()[0].values():
            if isinstance(value, str):
                assert "E+" not in value
                assert "E-" not in value


class TestDeterminismBreaks:
    async def test_none_when_results_agree(self, seeded: AsyncClient) -> None:
        assert (await seeded.get("/api/backtests/determinism-breaks")).json() == []

    async def test_a_break_is_reported(self, seeded: AsyncClient) -> None:
        async with session_scope() as db:
            await record_run(db, a_record("bt_test2", result_hash="sha256:different"))

        breaks = (await seeded.get("/api/backtests/determinism-breaks")).json()
        assert len(breaks) == 1
        assert set(breaks[0]["run_ids"]) == {"bt_test1", "bt_test2"}
        assert "not deterministic" in breaks[0]["summary"]
