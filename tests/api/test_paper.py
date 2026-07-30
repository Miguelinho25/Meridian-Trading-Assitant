"""Paper session endpoints.

Two properties carry weight beyond the listing itself: a replay must never be
presentable as live paper performance, and the decision breakdown must survive as
reason codes rather than a bare count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from nemonis_db import session_scope
from nemonis_db.paper_store import (
    DecisionRow,
    PositionRow,
    SessionSnapshot,
    record_decisions,
    record_equity,
    save_snapshot,
)

T = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def a_snapshot(session_id: str = "ps_api", **overrides) -> SessionSnapshot:
    base = {
        "session_id": session_id,
        "status": "RUNNING",
        "mode": "paper",
        "approval_mode": "AUTO_PAPER_FULL",
        "risk_profile": "CHALLENGE",
        "account_currency": "USD",
        "starting_balance": Decimal("100000"),
        "balance": Decimal("97097.34"),
        "equity": Decimal("97121.70"),
        "high_water_mark": Decimal("100017.46"),
        "balance_at_day_start": Decimal("97097.34"),
        "highest_equity_today": Decimal("97121.70"),
        "realised_pnl": Decimal("-2902.66"),
        "total_commission": Decimal("74.20"),
        "started_at": T,
        "updated_at": T,
        "instruments": ["EURUSD", "GBPUSD"],
        "timeframe": "D1",
        "bar_source": "REPLAY",
        "ticks": 500,
        "closed_trade_count": 106,
        "positions": [
            PositionRow(
                position_id="pos_1",
                instrument="EURUSD",
                direction="SHORT",
                lots=Decimal("0.01"),
                entry_price=Decimal("1.3611"),
                opened_at=T,
                stop_loss=Decimal("1.3809"),
            ),
            # No stop: unprotected exposure the UI must flag rather than blank.
            PositionRow(
                position_id="pos_2",
                instrument="GBPUSD",
                direction="LONG",
                lots=Decimal("0.02"),
                entry_price=Decimal("1.5000"),
                opened_at=T,
            ),
        ],
    }
    return SessionSnapshot(**{**base, **overrides})


@pytest.fixture
async def seeded(client: AsyncClient) -> AsyncClient:
    async with session_scope() as db:
        await save_snapshot(db, a_snapshot())
        await record_equity(
            db,
            "ps_api",
            at=T,
            equity=Decimal("97121.70"),
            balance=Decimal("97097.34"),
            drawdown_pct=Decimal("2.9"),
            open_positions=2,
        )
        await record_decisions(
            db,
            "ps_api",
            [
                DecisionRow(
                    at=T, strategy_id="ma", verdict="REJECTED", reason_code="SIZE_BELOW_MINIMUM_LOT"
                ),
                DecisionRow(
                    at=T, strategy_id="ma", verdict="REJECTED", reason_code="SIZE_BELOW_MINIMUM_LOT"
                ),
                DecisionRow(
                    at=T, strategy_id="vb", verdict="REJECTED", reason_code="BELOW_MIN_CONFIDENCE"
                ),
                DecisionRow(at=T, strategy_id="ma", verdict="APPROVED", reason_code=""),
            ],
        )
    return client


class TestReplayIsNeverPresentableAsLive:
    """A replay-fed session behaves identically to a live one. Without the label
    its equity curve is indistinguishable from real paper performance."""

    async def test_bar_source_is_on_the_listing(self, seeded: AsyncClient) -> None:
        row = (await seeded.get("/api/paper")).json()[0]
        assert row["bar_source"] == "REPLAY"

    async def test_bar_source_is_on_the_detail(self, seeded: AsyncClient) -> None:
        body = (await seeded.get("/api/paper/ps_api")).json()
        assert body["bar_source"] == "REPLAY"

    async def test_the_mode_is_never_broker(self, seeded: AsyncClient) -> None:
        """A CHECK constraint enforces it; this asserts the wire agrees."""
        for row in (await seeded.get("/api/paper")).json():
            assert row["mode"] in {"paper", "research", "backtest"}


class TestOpenExposureIsReported:
    async def test_positions_are_returned(self, seeded: AsyncClient) -> None:
        body = (await seeded.get("/api/paper/ps_api")).json()
        assert len(body["positions"]) == 2
        assert body["open_position_count"] == 2

    async def test_a_position_without_a_stop_reports_null_not_zero(
        self, seeded: AsyncClient
    ) -> None:
        """Zero would read as a stop at price zero. None says there is no stop,
        which is the finding."""
        body = (await seeded.get("/api/paper/ps_api")).json()
        unprotected = next(p for p in body["positions"] if p["instrument"] == "GBPUSD")
        assert unprotected["stop_loss"] is None

    async def test_numbers_cross_the_wire_as_strings(self, seeded: AsyncClient) -> None:
        body = (await seeded.get("/api/paper/ps_api")).json()
        assert isinstance(body["balance"], str)
        assert isinstance(body["positions"][0]["lots"], str)

    async def test_storage_padding_is_stripped(self, seeded: AsyncClient) -> None:
        body = (await seeded.get("/api/paper/ps_api")).json()
        assert body["positions"][0]["lots"] == "0.01"


class TestDecisionBreakdown:
    """A rejection count cannot answer why a session is not trading."""

    async def test_reasons_are_grouped_and_counted(self, seeded: AsyncClient) -> None:
        groups = (await seeded.get("/api/paper/ps_api/decisions")).json()
        by_reason = {(g["verdict"], g["reason_code"]): g["count"] for g in groups}
        assert by_reason[("REJECTED", "SIZE_BELOW_MINIMUM_LOT")] == 2
        assert by_reason[("REJECTED", "BELOW_MIN_CONFIDENCE")] == 1
        assert by_reason[("APPROVED", "")] == 1

    async def test_the_dominant_blocker_leads(self, seeded: AsyncClient) -> None:
        groups = (await seeded.get("/api/paper/ps_api/decisions")).json()
        assert groups[0]["reason_code"] == "SIZE_BELOW_MINIMUM_LOT"

    async def test_shares_sum_to_one(self, seeded: AsyncClient) -> None:
        groups = (await seeded.get("/api/paper/ps_api/decisions")).json()
        assert abs(sum(Decimal(g["share"]) for g in groups) - Decimal(1)) < Decimal("0.001")

    async def test_a_clean_approval_has_an_empty_reason(self, seeded: AsyncClient) -> None:
        groups = (await seeded.get("/api/paper/ps_api/decisions")).json()
        approved = next(g for g in groups if g["verdict"] == "APPROVED")
        assert approved["reason_code"] == ""


class TestReadOnly:
    """Starting a session is a process action. A route that spawned a trading
    loop would put the decision to trade behind a button."""

    async def test_no_write_routes(self, seeded: AsyncClient) -> None:
        for method in (seeded.post, seeded.put, seeded.patch, seeded.delete):
            assert (await method("/api/paper")).status_code == 405

    async def test_a_session_cannot_be_deleted(self, seeded: AsyncClient) -> None:
        assert (await seeded.delete("/api/paper/ps_api")).status_code == 405

    async def test_a_missing_session_is_404(self, seeded: AsyncClient) -> None:
        assert (await seeded.get("/api/paper/ps_nope")).status_code == 404


class TestEquityEndpoints:
    async def test_the_curve_is_returned(self, seeded: AsyncClient) -> None:
        points = (await seeded.get("/api/paper/ps_api/equity")).json()
        assert len(points) == 1
        assert Decimal(points[0]["equity"]) == Decimal("97121.70")

    async def test_a_long_curve_keeps_both_endpoints(self, seeded: AsyncClient) -> None:
        """The final point is the equity the session actually reached; dropping it
        to a stride would misreport where it stands."""
        async with session_scope() as db:
            for i in range(1, 400):
                await record_equity(
                    db,
                    "ps_api",
                    at=T + timedelta(hours=i),
                    equity=Decimal(97000 + i),
                    balance=Decimal(97000 + i),
                    drawdown_pct=Decimal("1"),
                    open_positions=1,
                )
        full = (await seeded.get("/api/paper/ps_api/equity?max_points=5000")).json()
        sampled = (await seeded.get("/api/paper/ps_api/equity?max_points=60")).json()
        assert sampled[0]["at"] == full[0]["at"]
        assert sampled[-1]["at"] == full[-1]["at"]
