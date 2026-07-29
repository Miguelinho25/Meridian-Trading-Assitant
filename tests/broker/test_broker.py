"""The broker's authorisation check is where risk-engine finality becomes real."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from nemonis_broker.account import Account
from nemonis_broker.broker import AuthorisationError, PaperBroker
from nemonis_broker.fills import FillModel, SlippageModel
from nemonis_marketdata.instruments import WATCHLIST, get_spec
from nemonis_marketdata.types import Candle
from nemonis_risk.engine import RiskEngine
from nemonis_schemas.enums import Direction, OrderType, Timeframe

from tests.risk.conftest import RATES, make_context

T = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
EURUSD = get_spec("EURUSD")
NO_SLIP = FillModel(slippage=SlippageModel.NONE)


def bar(o="1.0850", h="1.0870", low="1.0830", c="1.0860", *, at=T) -> Candle:
    s = Decimal("0.0001")
    return Candle(
        instrument="EURUSD",
        timeframe=Timeframe.H1,
        open_time=at,
        bid_open=Decimal(o),
        bid_high=Decimal(h),
        bid_low=Decimal(low),
        bid_close=Decimal(c),
        ask_open=Decimal(o) + s,
        ask_high=Decimal(h) + s,
        ask_low=Decimal(low) + s,
        ask_close=Decimal(c) + s,
    )


@pytest.fixture
def account() -> Account:
    return Account(
        account_id="acc_test",
        currency="USD",
        starting_balance=Decimal("100000"),
        balance=Decimal("100000"),
        high_water_mark=Decimal("100000"),
    )


@pytest.fixture
def broker(account) -> PaperBroker:
    return PaperBroker(account, specs=dict(WATCHLIST), rates=RATES, fill_model=NO_SLIP, seed=7)


@pytest.fixture
def decision():
    ctx = make_context()
    return RiskEngine().evaluate(ctx, evaluated_at=T), ctx


class TestAuthorisationIsEnforced:
    """Invariant I1, at the point it actually matters."""

    def test_a_valid_token_is_accepted(self, broker, decision) -> None:
        d, ctx = decision
        order = broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=T,
        )
        assert order.order_id
        assert broker.state.incidents == []

    def test_size_inflation_is_refused(self, broker, decision) -> None:
        """The realistic attack: approve 0.2 lots, submit 2.0."""
        d, ctx = decision
        with pytest.raises(AuthorisationError, match="does not match the authorised"):
            broker.submit(
                decision=d,
                proposal_hash=ctx.proposal.content_hash,
                instrument="EURUSD",
                direction=Direction.LONG,
                order_type=OrderType.MARKET,
                size_lots=d.final_size_lots * 10,
                strategy_id="s",
                at=T,
            )

    def test_a_mutated_proposal_is_refused(self, broker, decision) -> None:
        d, ctx = decision
        mutated = replace(ctx.proposal, stop=Decimal("1.05000"))
        with pytest.raises(AuthorisationError, match="hash mismatch"):
            broker.submit(
                decision=d,
                proposal_hash=mutated.content_hash,
                instrument="EURUSD",
                direction=Direction.LONG,
                order_type=OrderType.MARKET,
                size_lots=d.final_size_lots,
                strategy_id="s",
                at=T,
            )

    def test_a_rejected_decision_authorises_nothing(self, broker) -> None:
        ctx = make_context(kill_switch_engaged=True)
        d = RiskEngine().evaluate(ctx, evaluated_at=T)
        with pytest.raises(AuthorisationError, match="not an approval"):
            broker.submit(
                decision=d,
                proposal_hash=ctx.proposal.content_hash,
                instrument="EURUSD",
                direction=Direction.LONG,
                order_type=OrderType.MARKET,
                size_lots=Decimal("0.1"),
                strategy_id="s",
                at=T,
            )

    def test_an_unauthorised_attempt_raises_a_critical_incident(self, broker, decision) -> None:
        """Reaching this point means the risk engine was bypassed — a defect
        that must be visible, not a silent rejection."""
        d, ctx = decision
        with pytest.raises(AuthorisationError):
            broker.submit(
                decision=d,
                proposal_hash=ctx.proposal.content_hash,
                instrument="EURUSD",
                direction=Direction.LONG,
                order_type=OrderType.MARKET,
                size_lots=d.final_size_lots * 5,
                strategy_id="s",
                at=T,
            )
        assert len(broker.state.incidents) == 1
        incident = broker.state.incidents[0]
        assert incident.severity == "CRITICAL"
        assert incident.category == "UNAUTHORISED_ORDER"

    def test_no_position_is_created_by_a_refused_order(self, broker, decision) -> None:
        d, ctx = decision
        with pytest.raises(AuthorisationError):
            broker.submit(
                decision=d,
                proposal_hash=ctx.proposal.content_hash,
                instrument="EURUSD",
                direction=Direction.LONG,
                order_type=OrderType.MARKET,
                size_lots=d.final_size_lots * 3,
                strategy_id="s",
                at=T,
            )
        broker.process_bar({"EURUSD": bar()}, at=T)
        assert broker.account.positions == {}

    def test_size_cannot_be_modified_after_acceptance(self, broker, decision) -> None:
        """Changing size would invalidate the authorisation it was accepted under."""
        d, ctx = decision
        order = broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=T,
        )
        broker.modify(order.order_id, at=T, stop_loss=Decimal("1.0800"))
        assert order.size_lots == d.final_size_lots
        assert not hasattr(broker.modify, "size_lots")


class TestOrderLifecycle:
    def test_an_accepted_order_fills_on_the_next_bar(self, broker, decision) -> None:
        d, ctx = decision
        broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=T,
        )
        assert broker.account.positions == {}
        broker.process_bar({"EURUSD": bar()}, at=T + timedelta(hours=1))
        assert len(broker.account.positions) == 1

    def test_a_cancelled_order_never_fills(self, broker, decision) -> None:
        d, ctx = decision
        order = broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=T,
        )
        broker.cancel(order.order_id, at=T, reason="operator")
        broker.process_bar({"EURUSD": bar()}, at=T + timedelta(hours=1))
        assert broker.account.positions == {}

    def test_the_transition_history_is_complete(self, broker, decision) -> None:
        d, ctx = decision
        order = broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=T,
        )
        states = [t.to_state.value for t in order.lifecycle.history]
        assert "SUBMITTED_TO_PAPER_BROKER" in states
        assert "ACCEPTED" in states


class TestAccountingAndReconciliation:
    def _open(self, broker, decision, at=T):
        d, ctx = decision
        broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=at,
            stop_loss=Decimal("1.0800"),
            take_profit=Decimal("1.0950"),
        )
        broker.process_bar({"EURUSD": bar()}, at=at + timedelta(hours=1))

    def test_equity_equals_balance_plus_floating(self, broker, decision) -> None:
        self._open(broker, decision)
        bars = {"EURUSD": bar(c="1.0900")}
        prices = broker.current_prices(bars)
        floating = broker.account.floating_pnl(prices, broker.specs, broker.rates)
        assert broker.equity(bars) == broker.account.balance + floating

    def test_reconciliation_passes_in_normal_operation(self, broker, decision) -> None:
        self._open(broker, decision)
        assert broker.reconcile({"EURUSD": bar(c="1.0900")})

    def test_a_winning_trade_increases_the_balance(self, broker, decision) -> None:
        self._open(broker, decision)
        before = broker.account.balance
        broker.process_bar(
            {"EURUSD": bar(o="1.0940", h="1.0980", low="1.0935", c="1.0970")},
            at=T + timedelta(hours=2),
        )
        assert broker.account.balance > before
        assert len(broker.state.closed_trades) == 1

    def test_a_losing_trade_decreases_the_balance(self, broker, decision) -> None:
        self._open(broker, decision)
        before = broker.account.balance
        broker.process_bar(
            {"EURUSD": bar(o="1.0790", h="1.0795", low="1.0750", c="1.0760")},
            at=T + timedelta(hours=2),
        )
        assert broker.account.balance < before

    def test_commission_is_charged(self, broker, decision) -> None:
        self._open(broker, decision)
        broker.process_bar(
            {"EURUSD": bar(o="1.0940", h="1.0980", low="1.0935", c="1.0970")},
            at=T + timedelta(hours=2),
        )
        assert broker.account.total_commission > 0

    def test_high_water_mark_only_rises(self, broker, decision) -> None:
        self._open(broker, decision)
        peak = broker.account.high_water_mark
        broker.process_bar(
            {"EURUSD": bar(o="1.0790", h="1.0795", low="1.0750", c="1.0760")},
            at=T + timedelta(hours=2),
        )
        assert broker.account.high_water_mark == peak

    def test_an_unpriceable_position_marks_the_account_unreconciled(self, broker, decision) -> None:
        """Silently valuing it at zero would corrupt every downstream limit."""
        self._open(broker, decision)
        assert not broker.account.reconcile({}, broker.specs, broker.rates)


class TestExcursions:
    def test_mfe_and_mae_are_recorded(self, broker, decision) -> None:
        d, ctx = decision
        broker.submit(
            decision=d,
            proposal_hash=ctx.proposal.content_hash,
            instrument="EURUSD",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            size_lots=d.final_size_lots,
            strategy_id="s",
            at=T,
            stop_loss=Decimal("1.0700"),
            take_profit=Decimal("1.0990"),
        )
        broker.process_bar({"EURUSD": bar()}, at=T + timedelta(hours=1))
        broker.process_bar({"EURUSD": bar(c="1.0900")}, at=T + timedelta(hours=2))
        broker.process_bar(
            {"EURUSD": bar(o="1.0980", h="1.0995", low="1.0975", c="1.0990")},
            at=T + timedelta(hours=3),
        )
        trade = broker.state.closed_trades[0]
        assert trade.mfe_pips > 0
        assert trade.mae_pips >= 0


class TestDeterminism:
    def test_identical_runs_produce_identical_ledgers(self, decision) -> None:
        d, ctx = decision

        def run() -> list[tuple]:
            acct = Account(
                account_id="a",
                currency="USD",
                starting_balance=Decimal("100000"),
                balance=Decimal("100000"),
                high_water_mark=Decimal("100000"),
            )
            b = PaperBroker(
                acct,
                specs=dict(WATCHLIST),
                rates=RATES,
                fill_model=FillModel(slippage=SlippageModel.STOCHASTIC),
                seed=99,
            )
            b.submit(
                decision=d,
                proposal_hash=ctx.proposal.content_hash,
                instrument="EURUSD",
                direction=Direction.LONG,
                order_type=OrderType.MARKET,
                size_lots=d.final_size_lots,
                strategy_id="s",
                at=T,
                stop_loss=Decimal("1.0800"),
                take_profit=Decimal("1.0950"),
            )
            for i, c in enumerate(["1.0860", "1.0900", "1.0955"]):
                b.process_bar(
                    {"EURUSD": bar(c=c, at=T + timedelta(hours=i + 1))},
                    at=T + timedelta(hours=i + 1),
                )
            return [(t.entry_price, t.exit_price, t.pnl_account_ccy) for t in b.state.closed_trades]

        assert run() == run()

    def test_accounting_reconciles_after_a_randomised_sequence(self, decision) -> None:
        """Stage D done-criterion."""
        d, ctx = decision
        rng = random.Random(4321)
        acct = Account(
            account_id="a",
            currency="USD",
            starting_balance=Decimal("100000"),
            balance=Decimal("100000"),
            high_water_mark=Decimal("100000"),
        )
        b = PaperBroker(acct, specs=dict(WATCHLIST), rates=RATES, fill_model=NO_SLIP, seed=1)

        price = Decimal("1.0850")
        for i in range(60):
            moment = T + timedelta(hours=i)
            if rng.random() < 0.3 and not b.account.positions:
                b.submit(
                    decision=d,
                    proposal_hash=ctx.proposal.content_hash,
                    instrument="EURUSD",
                    direction=Direction.LONG,
                    order_type=OrderType.MARKET,
                    size_lots=d.final_size_lots,
                    strategy_id="s",
                    at=moment,
                    stop_loss=price - Decimal("0.0050"),
                    take_profit=price + Decimal("0.0080"),
                )
            price += Decimal(str(round(rng.gauss(0, 0.0015), 5)))
            price = max(Decimal("1.0000"), price)
            candle = bar(
                o=str(price),
                h=str(price + Decimal("0.0020")),
                low=str(price - Decimal("0.0020")),
                c=str(price),
                at=moment,
            )
            b.process_bar({"EURUSD": candle}, at=moment)
            assert b.reconcile({"EURUSD": candle}), f"failed to reconcile at bar {i}"
