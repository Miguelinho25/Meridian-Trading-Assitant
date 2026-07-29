"""The decision cycle — one bar of the trading pipeline.

**This is shared between backtesting and paper trading, and that is the point.**

If a live loop reimplemented the sequence below, backtest results would stop
predicting live behaviour and every validation statistic in
docs/backtesting-methodology.md would describe a system that no longer runs.
Divergence would not announce itself; it would show up as live results that
quietly fail to match a validated backtest. One implementation, two drivers.

The step order is the anti-look-ahead spine and is not negotiable:

    1. roll the trading day if it has changed
    2. settle: fills, exits, reconciliation, equity
    3. build BarView(<= i)          ← structurally cannot see i+1
    4. features -> regime -> strategies
    5. proposals -> risk engine as a *set*
    6. queue approved orders for bar i+1

Settlement completes before any decision, so a strategy sees a fully settled
account rather than a half-updated one. Approved orders queue for the *next*
bar, so a signal generated on bar i can only ever fill at bar i+1.

The cycle performs **no I/O**: no database, no network, no clock reads. Drivers
supply the bars and own persistence. That keeps replay deterministic and is
enforced by the import-linter contract on this package.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from nemonis_broker.account import Account
from nemonis_broker.broker import PaperBroker
from nemonis_config.settings import ApprovalMode, Mode, RiskProfileName
from nemonis_features.regime import DEFAULT_CLASSIFIER, RegimeClassifier
from nemonis_features.registry import FEATURES, FeatureDef
from nemonis_features.store import compute_row
from nemonis_marketdata.barview import BarView
from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.quality import assess_quote
from nemonis_marketdata.sessions import is_rollover, is_weekend, primary_session
from nemonis_marketdata.types import Candle, Quote
from nemonis_risk.context import (
    AccountState,
    MarketState,
    OpenPosition,
    PortfolioState,
    RiskContext,
    TradeProposal,
)
from nemonis_risk.portfolio import PortfolioRiskEngine
from nemonis_risk.propfirm import PropAccountState, PropFirmProfile, evaluate_profile
from nemonis_schemas.enums import OrderType, RiskVerdict
from nemonis_schemas.identifiers import IdPrefix, new_id
from nemonis_strategy.plugin import Signal, StrategyContext
from nemonis_strategy.registry import StrategyRegistry

#: Fallback daily and total loss limits as a fraction of starting balance, used
#: only when no prop-firm profile is configured.
_DEFAULT_DAILY_LOSS = Decimal("0.05")
_DEFAULT_TOTAL_LOSS = Decimal("0.10")


@dataclass(frozen=True, slots=True)
class CycleSettings:
    """Driver-supplied parameters.

    ``mode`` and ``kill_switch_engaged`` are explicit rather than defaulted
    because they differ between drivers in a safety-critical way. A backtest has
    no live kill switch and correctly passes False; a paper loop that inherited
    that default would ignore an engaged kill switch entirely. Making the caller
    state it means the safe value cannot be reached by omission.
    """

    starting_balance: Decimal
    risk_profile: RiskProfileName
    mode: Mode
    approval_mode: ApprovalMode
    kill_switch_engaged: bool
    warmup_bars: int = 51
    requested_risk_pct: Decimal = Decimal("0.35")
    #: Minutes until the next high-impact release. 9999 means "no calendar
    #: available", which is an assumption rather than a fact — None would block
    #: every trade, so the absence is recorded instead of inferred.
    minutes_to_news: int = 9999


@dataclass(slots=True)
class StepResult:
    """What one bar produced. Drivers decide what to persist."""

    equity: Decimal
    day_rolled: bool = False
    signals_generated: int = 0
    proposals_made: int = 0
    strategy_faults: int = 0
    submitted: int = 0
    #: (moment, strategy_id, verdict, binding constraint). Rejections are
    #: research data: a system that records only what it did cannot say what it
    #: declined, or why.
    decisions: list[tuple[datetime, str, RiskVerdict, str]] = field(default_factory=list)


class DecisionCycle:
    """One bar of the pipeline. Pure with respect to I/O."""

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
        settings: CycleSettings,
        prop_profile: PropFirmProfile | None = None,
        classifier: RegimeClassifier | None = None,
        features: tuple[FeatureDef, ...] = FEATURES,
    ) -> None:
        self.registry = registry
        self.specs = specs
        self.rates = rates
        self.settings = settings
        self.prop_profile = prop_profile
        self.classifier = classifier or DEFAULT_CLASSIFIER
        self.features = features
        self.portfolio_engine = PortfolioRiskEngine()

    def trading_day_start(self, moment: datetime) -> datetime:
        """Start of the trading day containing ``moment``.

        Falls back to UTC midnight when no prop-firm profile is configured. The
        daily loss limit comes from the risk profile and exists regardless, so
        skipping the reset for non-prop runs would leave the daily limit
        behaving as a lifetime limit.
        """
        if self.prop_profile is not None:
            return self.prop_profile.trading_day_start(moment)
        return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    def step(
        self,
        *,
        moment: datetime,
        views: dict[str, BarView],
        current_bars: dict[str, Candle],
        account: Account,
        broker: PaperBroker,
        trading_day: datetime,
    ) -> StepResult:
        """Advance one bar. Returns what happened; mutates account and broker."""
        result = StepResult(equity=Decimal(0))

        # 1. Roll the trading day before anything else in it.
        day = self.trading_day_start(moment)
        if day > trading_day:
            account.start_new_day()
            result.day_rolled = True

        # 2. Settle before deciding, so strategies see a settled account.
        broker.process_bar(current_bars, at=moment)
        # Refreshed every bar so a transient data gap blocks trading only while
        # it persists, rather than latching.
        broker.reconcile(current_bars)
        equity = broker.equity(current_bars)
        account.mark_equity(equity)
        result.equity = equity

        # 3-4. Decide. BarView is pinned and cannot see beyond this bar.
        contexts: dict[str, StrategyContext] = {}
        for symbol, view in views.items():
            if view.decision_index < self.settings.warmup_bars:
                continue
            row = compute_row(view, computed_at=moment, features=self.features)
            contexts[symbol] = StrategyContext(
                view=view,
                features=row.values,
                regime=self.classifier.classify(view),
                instrument=symbol,
                session=primary_session(moment),
                decision_time=view.decision_time,
            )

        if not contexts:
            return result

        outcomes = self.registry.generate_all(contexts)
        result.strategy_faults = sum(1 for o in outcomes if o.faulted)
        signals = [o.signal for o in outcomes if o.signal is not None]
        result.signals_generated = len(signals)
        if not signals:
            return result

        # 5. Evaluate as a set, so each approval counts against the next.
        risk_contexts = [
            self._risk_context(signal, account, current_bars, equity)
            for signal in signals
            if signal.instrument in current_bars
        ]
        result.proposals_made = len(risk_contexts)
        if not risk_contexts:
            return result

        evaluation = self.portfolio_engine.evaluate_set(risk_contexts, evaluated_at=moment)

        for decision in evaluation.decisions:
            ctx = self._by_id(risk_contexts, decision.proposal_id)
            if ctx is None:
                continue
            result.decisions.append(
                (
                    moment,
                    ctx.proposal.strategy_id,
                    decision.verdict,
                    decision.binding_constraint.value if decision.binding_constraint else "",
                )
            )
            if not decision.is_approved or decision.final_size_lots <= 0:
                continue

            # 6. Queue for the next bar. Never fills on the bar that decided it.
            broker.submit(
                decision=decision,
                proposal_hash=ctx.proposal.content_hash,
                instrument=ctx.proposal.instrument,
                direction=ctx.proposal.direction,
                order_type=OrderType.MARKET,
                size_lots=decision.final_size_lots,
                strategy_id=ctx.proposal.strategy_id,
                at=moment,
                stop_loss=ctx.proposal.stop,
                take_profit=ctx.proposal.target,
            )
            result.submitted += 1

        return result

    @staticmethod
    def _by_id(contexts: Sequence[RiskContext], proposal_id: str) -> RiskContext | None:
        return next((c for c in contexts if c.proposal.proposal_id == proposal_id), None)

    def _risk_context(
        self,
        signal: Signal,
        account: Account,
        bars: dict[str, Candle],
        equity: Decimal,
    ) -> RiskContext:
        s = self.settings
        bar = bars[signal.instrument]
        spec = self.specs[signal.instrument]

        quote = Quote(
            instrument=signal.instrument,
            bid=bar.bid_open,
            ask=bar.ask_open,
            source_time=signal.decision_time,
            arrival_time=signal.decision_time,
        )
        quality = assess_quote(quote, now=signal.decision_time, max_age_seconds=300, spec=spec)

        drawdown_consumed = Decimal(0)
        daily_limit = s.starting_balance * _DEFAULT_DAILY_LOSS
        total_limit = s.starting_balance * _DEFAULT_TOTAL_LOSS

        if self.prop_profile is not None:
            prop_state = PropAccountState(
                balance=account.balance,
                equity=equity,
                high_water_mark=account.high_water_mark,
                balance_at_day_start=account.balance_at_day_start,
                highest_equity_today=account.highest_equity_today,
            )
            evaluation = evaluate_profile(
                self.prop_profile, prop_state, evaluated_at=signal.decision_time
            )
            drawdown_consumed = evaluation.drawdown_consumed
            daily_limit = self.prop_profile.daily_loss_limit
            total_limit = self.prop_profile.total_loss_limit
        elif s.starting_balance > 0:
            drawdown_consumed = min(
                Decimal(1),
                max(Decimal(0), (account.high_water_mark - equity) / total_limit),
            )

        proposal = TradeProposal(
            proposal_id=new_id(IdPrefix.PROPOSAL),
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            instrument=signal.instrument,
            direction=signal.direction,
            entry=signal.entry,
            stop=signal.stop,
            target=signal.target,
            requested_risk_pct=s.requested_risk_pct,
            confidence=signal.confidence,
            decision_time=signal.decision_time,
            setup_type=signal.setup_type,
            regime_label=signal.regime_label,
        )

        open_positions = tuple(
            OpenPosition(
                instrument=p.instrument,
                direction=p.direction,
                lots=p.lots,
                entry=p.entry_price,
                stop=p.stop_loss or p.entry_price,
                strategy_id=p.strategy_id,
                open_risk_pct=s.requested_risk_pct,
            )
            for p in account.positions.values()
        )

        spread_multiple = (
            (bar.ask_open - bar.bid_open) / (spec.typical_spread_pips * spec.pip_size)
            if spec.typical_spread_pips > 0
            else Decimal(1)
        )

        return RiskContext(
            proposal=proposal,
            account=AccountState(
                account_id=account.account_id,
                currency=account.currency,
                balance=account.balance,
                equity=equity,
                high_water_mark=account.high_water_mark,
                drawdown_consumed=drawdown_consumed,
                daily_loss_used=max(Decimal(0), account.balance_at_day_start - equity),
                daily_loss_limit=daily_limit,
                total_loss_used=max(Decimal(0), s.starting_balance - equity),
                total_loss_limit=total_limit,
                is_reconciled=account.is_reconciled,
            ),
            market=MarketState(
                spec=spec,
                bid=bar.bid_open,
                ask=bar.ask_open,
                quality=quality,
                session=primary_session(signal.decision_time),
                is_weekend=is_weekend(signal.decision_time),
                is_rollover=is_rollover(signal.decision_time),
                minutes_to_news=s.minutes_to_news,
                atr=None,
                spread_multiple=spread_multiple,
                volatility_ratio=None,
            ),
            portfolio=PortfolioState(open_positions=open_positions),
            mode=s.mode,
            approval_mode=s.approval_mode,
            profile_name=s.risk_profile,
            kill_switch_engaged=s.kill_switch_engaged,
            rates=self.rates,
            specs=self.specs,
        )
