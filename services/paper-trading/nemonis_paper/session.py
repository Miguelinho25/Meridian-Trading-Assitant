"""A continuous paper-trading session.

The live driver. It ticks as bars arrive rather than iterating a fixed timeline,
and holds state between ticks so a session survives across days — but the
decision pipeline it calls is :class:`DecisionCycle`, the same one the backtest
replays. That is the whole point: a live loop with its own copy of the pipeline
would drift from what was validated, silently.

**Paper only, permanently in this build.** No broker adapter exists, and the
session refuses to start in any mode that implies one. That is checked here as
well as in configuration, because a mode check that lives in only one place is
one edit away from not existing.

Two things a replay does not have to worry about, and this does:

*The kill switch is read every tick, not at construction.* Engaging it must stop
the next decision, not the next restart — and it stops *new trades only*. The
tick still settles, because a switch that skipped settlement would leave open
positions unmarked and their stops unhonoured, abandoning the exact exposure it
was pulled to contain.

*Bars arrive late, out of order, or not at all.* A tick with no fresh bar
settles nothing and decides nothing rather than reusing the previous bar, which
would fabricate a price the market never printed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from nemonis_backtest.cycle import CycleSettings, DecisionCycle, StepResult
from nemonis_broker.account import Account, Position
from nemonis_broker.broker import ClosedTrade, PaperBroker, WorkingOrder
from nemonis_broker.fills import FillModel
from nemonis_broker.state_machine import OrderLifecycle, Transition
from nemonis_config.settings import ApprovalMode, Mode, RiskProfileName
from nemonis_features.regime import RegimeClassifier
from nemonis_marketdata.barview import BarView
from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.types import Candle
from nemonis_risk.propfirm import PropFirmProfile
from nemonis_schemas.enums import Direction, OrderState, OrderType
from nemonis_strategy.registry import StrategyRegistry

#: Modes this session may run in. LIVE is absent deliberately: no broker adapter
#: exists, and adding one to this set would not create it — it would only remove
#: the guard that says so.
PERMITTED_MODES = frozenset({Mode.PAPER, Mode.RESEARCH, Mode.BACKTEST})


class SessionRefusedError(RuntimeError):
    """The session declined to start or to act."""


@dataclass(frozen=True, slots=True)
class TickOutcome:
    """What one tick did, and why it did nothing when it did nothing."""

    at: datetime
    #: False when the tick was skipped. ``reason`` says which guard fired.
    acted: bool
    reason: str = ""
    equity: Decimal = Decimal(0)
    balance: Decimal = Decimal(0)
    day_rolled: bool = False
    signals_generated: int = 0
    proposals_made: int = 0
    submitted: int = 0
    strategy_faults: int = 0
    closed_trades: tuple[ClosedTrade, ...] = field(default_factory=tuple)
    decisions: tuple[tuple[datetime, str, str, str], ...] = field(default_factory=tuple)

    @property
    def blocked(self) -> bool:
        return not self.acted


class PaperSession:
    """Holds account and broker state across ticks.

    ``history`` accumulates bars per instrument so a BarView can be built with
    the same lookback a backtest would have. It is bounded: an unbounded window
    would grow without limit in a long-running process, and no feature declares a
    lookback anywhere near the cap.
    """

    def __init__(
        self,
        *,
        session_id: str,
        registry: StrategyRegistry,
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
        starting_balance: Decimal,
        risk_profile: RiskProfileName,
        mode: Mode,
        approval_mode: ApprovalMode,
        kill_switch: Callable[[], bool],
        account_currency: str = "USD",
        prop_profile: PropFirmProfile | None = None,
        classifier: RegimeClassifier | None = None,
        fill_model: FillModel | None = None,
        seed: int = 0,
        warmup_bars: int = 51,
        max_history_bars: int = 2000,
    ) -> None:
        if mode not in PERMITTED_MODES:
            raise SessionRefusedError(
                f"A paper session cannot run in {mode.value} mode. No broker adapter "
                f"exists in this build, so there is nothing for a live mode to reach."
            )

        self.session_id = session_id
        self.mode = mode
        self.max_history_bars = max_history_bars
        #: Read every tick rather than captured once — engaging the kill switch
        #: must stop the next decision, not the next restart.
        self._kill_switch = kill_switch

        self.account = Account(
            account_id=session_id,
            currency=account_currency,
            starting_balance=starting_balance,
            balance=starting_balance,
            high_water_mark=starting_balance,
        )
        self.broker = PaperBroker(
            self.account,
            specs=specs,
            rates=rates,
            fill_model=fill_model or FillModel(),
            seed=seed,
        )
        self.cycle = DecisionCycle(
            registry=registry,
            specs=specs,
            rates=rates,
            prop_profile=prop_profile,
            classifier=classifier,
            settings=CycleSettings(
                starting_balance=starting_balance,
                risk_profile=risk_profile,
                mode=mode,
                approval_mode=approval_mode,
                # Placeholder only: the live value is injected per tick below.
                kill_switch_engaged=False,
                warmup_bars=warmup_bars,
            ),
        )

        self.history: dict[str, list[Candle]] = {}
        self.trading_day: datetime | None = None
        self.ticks = 0
        self.last_tick_at: datetime | None = None
        #: Equity at the last acting tick, so a snapshot records the marked value
        #: rather than the balance, which ignores floating P&L.
        self.last_equity = starting_balance
        self._closed_seen = 0

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch()

    def ingest(self, bars: dict[str, Candle]) -> dict[str, Candle]:
        """Append bars to history, ignoring ones already seen.

        Returns only the genuinely new bars. A feed that re-sends the last bar —
        common on reconnect — must not be replayed as a fresh one, or the session
        would settle the same bar twice and double-count its fills.
        """
        fresh: dict[str, Candle] = {}
        for symbol, bar in bars.items():
            series = self.history.setdefault(symbol, [])
            if series and bar.open_time <= series[-1].open_time:
                continue
            series.append(bar)
            if len(series) > self.max_history_bars:
                del series[: len(series) - self.max_history_bars]
            fresh[symbol] = bar
        return fresh

    def tick(self, bars: dict[str, Candle], *, at: datetime) -> TickOutcome:
        """Advance the session by the supplied bars.

        Every guard here fails closed: an unclear state produces no trade rather
        than a trade made on an assumption.
        """
        self.ticks += 1
        self.last_tick_at = at

        fresh = self.ingest(bars)
        if not fresh:
            return TickOutcome(
                at=at,
                acted=False,
                reason=(
                    "No fresh bar. The previous bar is not reused: it would fabricate "
                    "a price the market never printed at this instant."
                ),
                equity=self.account.balance,
                balance=self.account.balance,
            )

        views = {
            symbol: BarView(self.history[symbol], len(self.history[symbol]) - 1) for symbol in fresh
        }

        if self.trading_day is None:
            self.trading_day = self.cycle.trading_day_start(at)

        # Inject the live kill-switch reading into the settings the risk engine
        # sees. Read now, not at construction: engaging it must stop the next
        # decision, not the next restart. Rebuilt per tick because CycleSettings
        # is frozen, which is what stops a stale reading persisting unnoticed.
        #
        # Deliberately *not* an early return. The kill switch stops new trades;
        # it must not stop settlement. Skipping the tick entirely would leave
        # open positions unmarked and their stops unhonoured — the switch would
        # abandon the very exposure it was pulled to contain. So the tick still
        # settles, and the risk engine rejects every new proposal with an
        # auditable reason code rather than the session silently declining.
        engaged = self.kill_switch_engaged
        self.cycle.settings = CycleSettings(
            starting_balance=self.cycle.settings.starting_balance,
            risk_profile=self.cycle.settings.risk_profile,
            mode=self.cycle.settings.mode,
            approval_mode=self.cycle.settings.approval_mode,
            kill_switch_engaged=engaged,
            warmup_bars=self.cycle.settings.warmup_bars,
            requested_risk_pct=self.cycle.settings.requested_risk_pct,
            minutes_to_news=self.cycle.settings.minutes_to_news,
        )

        before = len(self.broker.state.closed_trades)
        step: StepResult = self.cycle.step(
            moment=at,
            views=views,
            current_bars=fresh,
            account=self.account,
            broker=self.broker,
            trading_day=self.trading_day,
        )
        if step.day_rolled:
            self.trading_day = self.cycle.trading_day_start(at)

        self.last_equity = step.equity
        closed = tuple(self.broker.state.closed_trades[before:])
        self._closed_seen = len(self.broker.state.closed_trades)

        return TickOutcome(
            at=at,
            acted=True,
            reason=(
                "Kill switch engaged: positions still settled, no new trade permitted."
                if engaged
                else ""
            ),
            equity=step.equity,
            balance=self.account.balance,
            day_rolled=step.day_rolled,
            signals_generated=step.signals_generated,
            proposals_made=step.proposals_made,
            submitted=step.submitted,
            strategy_faults=step.strategy_faults,
            closed_trades=closed,
            decisions=tuple(
                (when, sid, verdict.value, constraint)
                for when, sid, verdict, constraint in step.decisions
            ),
        )

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        return list(self.broker.state.closed_trades)

    # --- Resumption ---------------------------------------------------------
    #
    # Expressed as plain dictionaries rather than persistence types: the session
    # must not import the database layer, and a driver owns the writing. What
    # matters here is completeness — a restore that omits a position leaves
    # exposure nothing will manage, with stops that will never be honoured.

    def state(self) -> dict[str, Any]:
        """Everything needed to resume exactly where this stopped."""
        a = self.account
        return {
            "balance": a.balance,
            "equity": self.last_equity,
            "high_water_mark": a.high_water_mark,
            # Without this the first tick after a restart measures its daily loss
            # from the wrong reference, which is how a daily limit silently
            # becomes a lifetime one.
            "balance_at_day_start": a.balance_at_day_start,
            "highest_equity_today": a.highest_equity_today,
            "realised_pnl": a.realised_pnl,
            "total_commission": a.total_commission,
            "trading_day": self.trading_day,
            "ticks": self.ticks,
            "last_tick_at": self.last_tick_at,
            "positions": [
                {
                    "position_id": p.position_id,
                    "instrument": p.instrument,
                    "direction": p.direction.value,
                    "lots": p.lots,
                    "entry_price": p.entry_price,
                    "opened_at": p.opened_at,
                    "strategy_id": p.strategy_id,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
                    "commission_paid": p.commission_paid,
                    "best_price": p.best_price,
                    "worst_price": p.worst_price,
                }
                for p in a.positions.values()
            ],
            "working_orders": [
                {
                    "order_id": o.order_id,
                    "proposal_hash": o.proposal_hash,
                    "decision_id": o.decision_id,
                    "instrument": o.instrument,
                    "direction": o.direction.value,
                    "order_type": o.order_type.value,
                    # State *and* history. architecture.md requires every
                    # transition recorded, and OrderLifecycle's own docstring
                    # notes that reconstructing the history from a final state is
                    # impossible — so persisting only the state would destroy
                    # evidence that cannot be recovered.
                    "lifecycle_state": o.lifecycle.state.value,
                    "lifecycle_history": [
                        {
                            "from_state": t.from_state.value if t.from_state else None,
                            "to_state": t.to_state.value,
                            "at": t.at,
                            "actor": t.actor,
                            "reason": t.reason,
                        }
                        for t in o.lifecycle.history
                    ],
                    "size_lots": o.size_lots,
                    "strategy_id": o.strategy_id,
                    "limit_price": o.limit_price,
                    "stop_price": o.stop_price,
                    "stop_loss": o.stop_loss,
                    "take_profit": o.take_profit,
                    "submitted_at": o.submitted_at,
                }
                for o in self.broker.state.working.values()
            ],
        }

    def restore(self, state: dict[str, Any]) -> None:
        """Reinstate persisted state onto a freshly constructed session.

        Positions and working orders are *replaced*, not merged: a stale entry
        surviving a restore would be exposure the account does not actually hold.
        """
        a = self.account
        a.balance = state["balance"]
        a.high_water_mark = state["high_water_mark"]
        a.balance_at_day_start = state["balance_at_day_start"]
        a.highest_equity_today = state["highest_equity_today"]
        a.realised_pnl = state["realised_pnl"]
        a.total_commission = state["total_commission"]
        self.trading_day = state.get("trading_day")
        self.ticks = state.get("ticks", 0)
        self.last_tick_at = state.get("last_tick_at")
        self.last_equity = state.get("equity", a.balance)

        a.positions.clear()
        for row in state.get("positions", ()):
            a.positions[row["position_id"]] = Position(
                position_id=row["position_id"],
                instrument=row["instrument"],
                direction=Direction(row["direction"]),
                lots=row["lots"],
                entry_price=row["entry_price"],
                opened_at=row["opened_at"],
                strategy_id=row["strategy_id"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                commission_paid=row["commission_paid"],
                best_price=row["best_price"],
                worst_price=row["worst_price"],
            )

        self.broker.state.working.clear()
        for row in state.get("working_orders", ()):
            self.broker.state.working[row["order_id"]] = WorkingOrder(
                order_id=row["order_id"],
                proposal_hash=row["proposal_hash"],
                decision_id=row["decision_id"],
                instrument=row["instrument"],
                direction=Direction(row["direction"]),
                order_type=OrderType(row["order_type"]),
                lifecycle=OrderLifecycle(
                    state=OrderState(row["lifecycle_state"]),
                    history=[
                        Transition(
                            from_state=(OrderState(t["from_state"]) if t["from_state"] else None),
                            to_state=OrderState(t["to_state"]),
                            at=t["at"],
                            actor=t["actor"],
                            reason=t.get("reason", ""),
                        )
                        for t in row.get("lifecycle_history", ())
                    ],
                ),
                size_lots=row["size_lots"],
                strategy_id=row["strategy_id"],
                limit_price=row["limit_price"],
                stop_price=row["stop_price"],
                stop_loss=row["stop_loss"],
                take_profit=row["take_profit"],
                submitted_at=row["submitted_at"],
            )
