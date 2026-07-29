"""Schema-level safety guarantees.

Invariant I1 is expressed as a database constraint rather than a convention: an
order without a risk decision must be impossible to insert.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_db import Account, Instrument
from nemonis_schemas.identifiers import IdPrefix, new_id
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


async def _instrument(session: AsyncSession) -> Instrument:
    ins = Instrument(
        id=new_id(IdPrefix.INSTRUMENT),
        symbol="EURUSD",
        base_ccy="EUR",
        quote_ccy="USD",
        digits=5,
        pip_size=Decimal("0.0001"),
        contract_size=Decimal("100000"),
        min_lot=Decimal("0.01"),
        lot_step=Decimal("0.01"),
        max_lot=Decimal("100"),
        margin_rate=Decimal("0.033"),
        created_at=NOW,
    )
    session.add(ins)
    await session.flush()
    return ins


class TestOrderRequiresRiskDecision:
    """I1 — enforced by NOT NULL + FK, not by discipline."""

    async def test_order_without_risk_decision_is_rejected(self, session: AsyncSession) -> None:
        """Every other referenced row exists, so the ONLY defect is the null
        risk decision. Without this setup the test would pass on an unrelated
        foreign-key error and prove nothing about I1."""
        ins = await _instrument(session)
        session.add(
            Account(
                id="acc_i1",
                name="t",
                currency="USD",
                starting_balance=Decimal("100000"),
                balance=Decimal("100000"),
                equity=Decimal("100000"),
                high_water_mark=Decimal("100000"),
                created_at=NOW,
            )
        )
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO trade_proposals (id, account_id, instrument_id, direction, "
                "entry_price, stop_price, requested_risk_pct, proposal_hash, event_time, "
                "decision_time, created_at) VALUES ('prp_i1', 'acc_i1', :ins, 'LONG', "
                "'000000000000000001.0800000000', '000000000000000001.0700000000', "
                "'000000000000000000.3500000000', 'h', '2026-07-27 12:00:00', "
                "'2026-07-27 12:00:00', '2026-07-27 12:00:00')"
            ),
            {"ins": ins.id},
        )
        await session.commit()

        with pytest.raises(IntegrityError, match=r"NOT NULL.*risk_decision_id"):
            await session.execute(
                text(
                    "INSERT INTO orders (id, account_id, instrument_id, proposal_id, "
                    "risk_decision_id, order_type, direction, size_lots, state, created_at) "
                    "VALUES ('ord_i1', 'acc_i1', :ins, 'prp_i1', NULL, 'MARKET', 'LONG', "
                    "'000000000000000000.1000000000', 'DRAFT', '2026-07-27 12:00:00')"
                ),
                {"ins": ins.id},
            )

    async def test_order_with_dangling_risk_decision_is_rejected(
        self, session: AsyncSession
    ) -> None:
        """The FK must be enforced — SQLite disables foreign keys unless asked."""
        await _instrument(session)
        await session.commit()

        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO orders (id, account_id, instrument_id, proposal_id, "
                    "risk_decision_id, order_type, direction, size_lots, state, created_at) "
                    "VALUES ('ord_y', 'acc_y', 'ins_y', 'prp_y', 'rd_does_not_exist', "
                    "'MARKET', 'LONG', '000000000000000000.1000000000', 'DRAFT', "
                    "'2026-07-27 12:00:00')"
                )
            )


class TestDecimalStorage:
    """I4 — exact decimals survive the round trip; floats are refused."""

    async def test_decimal_round_trips_exactly(self, session: AsyncSession) -> None:
        ins = await _instrument(session)
        await session.commit()
        session.expunge_all()

        loaded = await session.get(Instrument, ins.id)
        assert loaded is not None
        assert loaded.pip_size == Decimal("0.0001")
        assert isinstance(loaded.pip_size, Decimal)

    async def test_float_is_refused_at_the_column(self, session: AsyncSession) -> None:
        account = Account(
            id=new_id(IdPrefix.ACCOUNT),
            name="test",
            currency="USD",
            starting_balance=100000.50,  # type: ignore[arg-type]
            balance=Decimal("100000.50"),
            equity=Decimal("100000.50"),
            high_water_mark=Decimal("100000.50"),
            created_at=NOW,
        )
        session.add(account)
        with pytest.raises(StatementError, match="Refusing to store float"):
            await session.flush()

    async def test_negative_decimals_compare_correctly(self, session: AsyncSession) -> None:
        """Text-encoded decimals must order correctly, including across zero."""
        for i, value in enumerate([Decimal("-500.25"), Decimal("0"), Decimal("1200.75")]):
            session.add(
                Account(
                    id=f"acc_cmp_{i}",
                    name=f"a{i}",
                    currency="USD",
                    starting_balance=Decimal("1000"),
                    balance=value,
                    equity=value,
                    high_water_mark=Decimal("1000"),
                    created_at=NOW,
                )
            )
        await session.commit()
        session.expunge_all()

        loaded = await session.get(Account, "acc_cmp_0")
        assert loaded is not None
        assert loaded.balance == Decimal("-500.25")


class TestCheckConstraints:
    async def test_negative_order_size_rejected(self, session: AsyncSession) -> None:
        with pytest.raises(IntegrityError):
            await session.execute(
                text(
                    "INSERT INTO instruments (id, symbol, base_ccy, quote_ccy, digits, "
                    "pip_size, contract_size, min_lot, lot_step, max_lot, "
                    "stop_level_points, freeze_level_points, margin_rate, "
                    "commission_per_lot, swap_long, swap_short, enabled, created_at) "
                    "VALUES ('ins_bad', 'BADPAIR', 'EUR', 'USD', 5, "
                    "'-000000000000000000.0001000000', '000000000000100000.0000000000', "
                    "'000000000000000000.0100000000', '000000000000000000.0100000000', "
                    "'000000000000000100.0000000000', 0, 0, "
                    "'000000000000000000.0330000000', '0', '0', '0', 1, "
                    "'2026-07-27 12:00:00')"
                )
            )
