"""Paper broker.

The single most important line in this module is the authorisation check in
``submit``. Everything the risk engine does is worthless if an order can reach a
fill without a matching decision token, so there is exactly one path to a
position and it passes through that check.

The broker uses the same interfaces a live adapter would, so promoting to a real
broker later is an adapter swap rather than a redesign — but no live adapter
exists, and none is implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from random import Random

from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.types import Candle
from nemonis_risk.decision import RiskDecision
from nemonis_risk.forex import convert_quote_to_account
from nemonis_schemas.enums import Direction, OrderState, OrderType
from nemonis_schemas.identifiers import IdPrefix, new_id
from nemonis_schemas.money import quantise_money

from nemonis_broker.account import Account, Position
from nemonis_broker.fills import (
    FillModel,
    FillReason,
    commission_for,
    fill_for_order,
    resolve_exit,
)
from nemonis_broker.state_machine import OrderLifecycle


class AuthorisationError(RuntimeError):
    """An order was submitted without a valid decision token.

    Raised rather than returned, because reaching this point means a caller has
    bypassed the risk engine — a defect, not a market condition.
    """


@dataclass(slots=True)
class WorkingOrder:
    order_id: str
    proposal_hash: str
    instrument: str
    direction: Direction
    order_type: OrderType
    size_lots: Decimal
    strategy_id: str
    lifecycle: OrderLifecycle
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    submitted_at: datetime | None = None
    #: The decision that authorised it. Retained for audit.
    decision_id: str = ""


@dataclass(frozen=True, slots=True)
class Incident:
    severity: str
    category: str
    summary: str
    detail: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    trade_id: str
    instrument: str
    direction: Direction
    lots: Decimal
    entry_price: Decimal
    exit_price: Decimal
    opened_at: datetime
    closed_at: datetime
    strategy_id: str
    pnl_account_ccy: Decimal
    commission: Decimal
    reason: FillReason
    mfe_pips: Decimal
    mae_pips: Decimal
    ambiguous_exit: bool = False


@dataclass(slots=True)
class BrokerState:
    working: dict[str, WorkingOrder] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    incidents: list[Incident] = field(default_factory=list)
    #: Bars where stop and target were both reachable and the stop was assumed.
    ambiguous_bars: int = 0


class PaperBroker:
    """Simulated execution. Deterministic given a seed."""

    def __init__(
        self,
        account: Account,
        *,
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
        fill_model: FillModel | None = None,
        seed: int = 0,
    ) -> None:
        self.account = account
        self.specs = specs
        self.rates = rates
        self.fill_model = fill_model or FillModel()
        self.state = BrokerState()
        self._rng = Random(seed)

    # -- Submission --------------------------------------------------------

    def submit(
        self,
        *,
        decision: RiskDecision,
        proposal_hash: str,
        instrument: str,
        direction: Direction,
        order_type: OrderType,
        size_lots: Decimal,
        strategy_id: str,
        at: datetime,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> WorkingOrder:
        """Accept an order **only** if the decision token authorises exactly it.

        This is the enforcement point for invariant I1. The check covers both the
        trade's economics and its size, so neither mutating the stop after
        approval nor inflating the lots gets through.
        """
        if not decision.authorises(proposal_hash=proposal_hash, size_lots=size_lots):
            reason = (
                "verdict is not an approval"
                if not decision.is_approved
                else (
                    "proposal hash mismatch"
                    if proposal_hash != decision.proposal_hash
                    else f"size {size_lots} does not match the authorised "
                    f"{decision.final_size_lots}"
                )
            )
            self._raise_incident(
                severity="CRITICAL",
                category="UNAUTHORISED_ORDER",
                summary="Order submitted without a valid risk authorisation",
                detail=(
                    f"{instrument} {direction.value} {size_lots} lots rejected: {reason}. "
                    f"Decision {decision.decision_id} authorises "
                    f"{decision.final_size_lots} lots for proposal "
                    f"{decision.proposal_hash[:20]}…"
                ),
                at=at,
            )
            raise AuthorisationError(
                f"Refusing to submit {instrument} {direction.value} {size_lots} lots: "
                f"{reason}. Every order must carry a risk decision that authorises "
                f"exactly its economics and size (invariant I1)."
            )

        lifecycle = OrderLifecycle(state=OrderState.APPROVED)
        lifecycle.transition(
            OrderState.SUBMITTED_TO_PAPER_BROKER,
            at=at,
            actor="broker",
            reason=f"decision {decision.decision_id}",
        )
        lifecycle.transition(OrderState.ACCEPTED, at=at, actor="broker")

        order = WorkingOrder(
            order_id=new_id(IdPrefix.ORDER),
            proposal_hash=proposal_hash,
            instrument=instrument,
            direction=direction,
            order_type=order_type,
            size_lots=size_lots,
            strategy_id=strategy_id,
            lifecycle=lifecycle,
            limit_price=limit_price,
            stop_price=stop_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            submitted_at=at,
            decision_id=decision.decision_id,
        )
        self.state.working[order.order_id] = order
        return order

    def cancel(self, order_id: str, *, at: datetime, reason: str = "") -> WorkingOrder:
        order = self.state.working[order_id]
        order.lifecycle.transition(OrderState.CANCELLED, at=at, actor="operator", reason=reason)
        del self.state.working[order_id]
        return order

    def modify(
        self,
        order_id: str,
        *,
        at: datetime,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> WorkingOrder:
        """Adjust protective levels.

        Size cannot be modified. Changing it would invalidate the authorisation
        the order was accepted under, and re-authorising is the risk engine's job,
        not the broker's.
        """
        order = self.state.working[order_id]
        if stop_loss is not None:
            order.stop_loss = stop_loss
        if take_profit is not None:
            order.take_profit = take_profit
        return order

    # -- Bar processing ----------------------------------------------------

    def process_bar(self, bars: dict[str, Candle], *, at: datetime) -> None:
        """Advance the simulation by one bar.

        Order matters: exits are resolved before entries, so a position opened
        this bar cannot also be closed by it. Entering and exiting within the
        same bar requires intrabar data we do not have.
        """
        self._resolve_exits(bars, at=at)
        self._fill_working(bars, at=at)
        self._mark_positions(bars)

    def _fill_working(self, bars: dict[str, Candle], *, at: datetime) -> None:
        for order_id in list(self.state.working):
            order = self.state.working[order_id]
            bar = bars.get(order.instrument)
            spec = self.specs.get(order.instrument)
            if bar is None or spec is None:
                continue

            result = fill_for_order(
                order_type=order.order_type,
                spec=spec,
                direction=order.direction,
                bar=bar,
                model=self.fill_model,
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                rng=self._rng,
            )
            if not result.filled or result.price is None:
                continue

            commission = commission_for(spec, order.size_lots)
            position = Position(
                position_id=new_id(IdPrefix.POSITION),
                instrument=order.instrument,
                direction=order.direction,
                lots=order.size_lots,
                entry_price=result.price,
                opened_at=at,
                strategy_id=order.strategy_id,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
                commission_paid=commission,
            )
            position.mark(result.price)

            order.lifecycle.transition(OrderState.FILLED, at=at, actor="broker")
            order.lifecycle.transition(OrderState.MANAGED, at=at, actor="broker")
            self.account.positions[position.position_id] = position
            del self.state.working[order_id]

    def _resolve_exits(self, bars: dict[str, Candle], *, at: datetime) -> None:
        for position_id in list(self.account.positions):
            position = self.account.positions[position_id]
            bar = bars.get(position.instrument)
            spec = self.specs.get(position.instrument)
            if bar is None or spec is None:
                continue

            result = resolve_exit(
                spec=spec,
                direction=position.direction,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
                bar=bar,
                model=self.fill_model,
                rng=self._rng,
            )
            if not result.filled or result.price is None:
                continue

            if result.ambiguous_bar:
                self.state.ambiguous_bars += 1

            self._close(
                position, result.price, result.reason, at=at, ambiguous=result.ambiguous_bar
            )

    def _close(
        self,
        position: Position,
        exit_price: Decimal,
        reason: FillReason | None,
        *,
        at: datetime,
        ambiguous: bool = False,
    ) -> ClosedTrade:
        spec = self.specs[position.instrument]
        quote_pnl = position.unrealised_quote(exit_price, spec)
        conversion = convert_quote_to_account(spec.quote_ccy, self.account.currency, self.rates)
        pnl = quantise_money(quote_pnl * conversion.rate)

        self.account.book_realised(pnl, commission=position.commission_paid)
        mfe, mae = position.excursions(spec)

        trade = ClosedTrade(
            trade_id=new_id(IdPrefix.TRADE),
            instrument=position.instrument,
            direction=position.direction,
            lots=position.lots,
            entry_price=position.entry_price,
            exit_price=exit_price,
            opened_at=position.opened_at,
            closed_at=at,
            strategy_id=position.strategy_id,
            pnl_account_ccy=pnl,
            commission=position.commission_paid,
            reason=reason or FillReason.MARKET,
            mfe_pips=mfe,
            mae_pips=mae,
            ambiguous_exit=ambiguous,
        )
        self.state.closed_trades.append(trade)
        del self.account.positions[position.position_id]
        return trade

    def close_position(self, position_id: str, *, bar: Candle, at: datetime) -> ClosedTrade:
        """Close at market — used by the kill switch and manual intervention."""
        position = self.account.positions[position_id]
        price = bar.bid_open if position.direction is Direction.LONG else bar.ask_open
        return self._close(position, price, FillReason.MARKET, at=at)

    def _mark_positions(self, bars: dict[str, Candle]) -> None:
        for position in self.account.positions.values():
            bar = bars.get(position.instrument)
            if bar is None:
                continue
            price = bar.bid_close if position.direction is Direction.LONG else bar.ask_close
            position.mark(price)

    # -- Reporting ---------------------------------------------------------

    def current_prices(self, bars: dict[str, Candle]) -> dict[str, Decimal]:
        return {sym: bar.bid_close for sym, bar in bars.items()}

    def equity(self, bars: dict[str, Candle]) -> Decimal:
        return self.account.equity(self.current_prices(bars), self.specs, self.rates)

    def reconcile(self, bars: dict[str, Candle]) -> bool:
        return self.account.reconcile(self.current_prices(bars), self.specs, self.rates)

    def _raise_incident(
        self, *, severity: str, category: str, summary: str, detail: str, at: datetime
    ) -> None:
        self.state.incidents.append(
            Incident(
                severity=severity,
                category=category,
                summary=summary,
                detail=detail,
                at=at,
            )
        )
