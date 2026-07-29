"""Event-driven backtest loop (backtesting-methodology.md §2).

Step order is the anti-look-ahead spine and is not negotiable:

    for each bar i:
        1. advance the clock to bar i's open
        2. ingest bar i, run the data-quality gate
        3. mark open positions; resolve exits; fill orders queued at i-1
        4. update account and drawdown state
        5. build BarView(<= i)          ← structurally cannot see i+1
        6. features -> regime -> strategies
        7. proposals -> risk engine -> queue orders for bar i+1

Steps 3–4 complete before any decision, so a strategy sees a fully settled
account rather than a half-updated one. Step 7 queues for the *next* bar, so a
signal generated on bar i can only ever fill at bar i+1.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from nemonis_broker.account import Account
from nemonis_broker.broker import ClosedTrade, PaperBroker
from nemonis_broker.fills import FillModel
from nemonis_config.clock import ReplayClock
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
from nemonis_schemas.enums import OrderType, ResultProvenance, RiskVerdict
from nemonis_schemas.identifiers import IdPrefix, new_id
from nemonis_strategy.plugin import Signal, StrategyContext
from nemonis_strategy.registry import StrategyRegistry


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    instruments: tuple[str, ...]
    start: datetime
    end: datetime
    starting_balance: Decimal = Decimal("100000")
    account_currency: str = "USD"
    risk_profile: RiskProfileName = RiskProfileName.CHALLENGE
    fill_model: FillModel = field(default_factory=FillModel)
    seed: int = 0
    #: Where the data came from. Propagates onto every metric so a synthetic
    #: result can never be mistaken for a real one.
    provenance: ResultProvenance = ResultProvenance.SYNTHETIC
    #: True when the source was mid-only and a spread was assumed at load time.
    spread_assumed: bool = False
    warmup_bars: int = 51


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: Decimal
    balance: Decimal
    drawdown_pct: Decimal
    open_positions: int


@dataclass(slots=True)
class BacktestResult:
    config: BacktestConfig
    trades: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    #: Every risk decision, approved or not. Rejections are research data:
    #: a system that only records what it did cannot tell you what it declined.
    decisions: list[tuple[datetime, str, RiskVerdict, str]] = field(default_factory=list)
    signals_generated: int = 0
    proposals_made: int = 0
    bars_processed: int = 0
    ambiguous_bars: int = 0
    strategy_faults: int = 0
    #: Times the trading day rolled and the daily loss reference was reset.
    #: Zero over a multi-day run means the daily limit is behaving as a lifetime
    #: limit — the defect this counter exists to make visible.
    daily_resets: int = 0
    final_balance: Decimal = Decimal(0)
    peak_equity: Decimal = Decimal(0)
    max_drawdown_pct: Decimal = Decimal(0)

    @property
    def rejections(self) -> list[tuple[datetime, str, RiskVerdict, str]]:
        return [d for d in self.decisions if d[2] is RiskVerdict.REJECTED]


class BacktestEngine:
    """Runs a strategy registry over historical bars."""

    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        specs: dict[str, InstrumentSpec],
        rates: dict[str, Decimal],
        prop_profile: PropFirmProfile | None = None,
        classifier: RegimeClassifier | None = None,
        features: tuple[FeatureDef, ...] = FEATURES,
    ) -> None:
        self.registry = registry
        self.specs = specs
        self.rates = rates
        self.prop_profile = prop_profile
        self.classifier = classifier or DEFAULT_CLASSIFIER
        self.features = features

    def _trading_day_start(self, moment: datetime) -> datetime:
        """Start of the trading day containing ``moment``.

        Falls back to UTC midnight when no prop-firm profile is configured. The
        daily loss limit comes from the risk profile and exists regardless, so
        skipping the reset here would leave non-prop runs with the very defect
        this method was added to fix — a daily limit that never releases.
        """
        if self.prop_profile is not None:
            return self.prop_profile.trading_day_start(moment)
        return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    def run(self, series: dict[str, list[Candle]], config: BacktestConfig) -> BacktestResult:
        """Execute the loop. Deterministic for a given seed and inputs."""
        result = BacktestResult(config=config)

        account = Account(
            account_id="bt",
            currency=config.account_currency,
            starting_balance=config.starting_balance,
            balance=config.starting_balance,
            high_water_mark=config.starting_balance,
        )
        broker = PaperBroker(
            account,
            specs=self.specs,
            rates=self.rates,
            fill_model=config.fill_model,
            seed=config.seed,
        )
        portfolio_engine = PortfolioRiskEngine()

        # Bars are aligned on a shared timeline so every instrument advances
        # together. A per-instrument loop would let one instrument's strategy see
        # a later timestamp than another's.
        timeline = sorted(
            {
                bar.open_time
                for bars in series.values()
                for bar in bars
                if config.start <= bar.open_time < config.end
            }
        )
        if not timeline:
            return result

        by_time: dict[str, dict[datetime, Candle]] = {
            sym: {b.open_time: b for b in bars} for sym, bars in series.items()
        }
        indices: dict[str, dict[datetime, int]] = {
            sym: {b.open_time: i for i, b in enumerate(bars)} for sym, bars in series.items()
        }

        clock = ReplayClock(timeline[0])
        peak = config.starting_balance
        trading_day = self._trading_day_start(timeline[0])

        for moment in timeline:
            # 1. Advance the clock. It cannot be moved beyond this bar.
            clock.admit_bar(moment)

            current_bars = {sym: table[moment] for sym, table in by_time.items() if moment in table}
            if not current_bars:
                continue

            # 1a. Roll the trading day before anything else in it.
            #
            # Without this the daily reference never moves, so `daily_loss_used`
            # measures the loss since the *start of the backtest* rather than
            # since the last reset. The daily limit then behaves as a lifetime
            # limit: once cumulative drawdown reaches it, every subsequent trade
            # is rejected DAILY_LOSS_WOULD_BREACH and never recovers. A 2010-2026
            # run stopped trading in March 2017 and produced no trades across the
            # remaining 56% of the timeline, while still reporting metrics as if
            # it had covered the whole period.
            #
            # The reset uses the profile's own timezone and reset hour, so it
            # lands where the firm's day actually starts rather than at UTC
            # midnight.
            day = self._trading_day_start(moment)
            if day > trading_day:
                account.start_new_day()
                trading_day = day
                result.daily_resets += 1

            # 2-4. Settle everything before any decision is made.
            broker.process_bar(current_bars, at=moment)
            # Refresh reconciliation every bar so a transient data gap blocks
            # trading only while it persists.
            broker.reconcile(current_bars)
            equity = broker.equity(current_bars)
            account.mark_equity(equity)
            peak = max(peak, equity)
            drawdown_pct = (peak - equity) / peak * Decimal(100) if peak > 0 else Decimal(0)
            result.max_drawdown_pct = max(result.max_drawdown_pct, drawdown_pct)

            result.equity_curve.append(
                EquityPoint(
                    at=moment,
                    equity=equity,
                    balance=account.balance,
                    drawdown_pct=drawdown_pct,
                    open_positions=len(account.positions),
                )
            )
            result.bars_processed += 1

            # 5-6. Decide. BarView is pinned to this bar and cannot see beyond it.
            contexts: dict[str, StrategyContext] = {}
            for symbol in current_bars:
                index = indices[symbol].get(moment)
                if index is None or index < config.warmup_bars:
                    continue
                view = BarView(series[symbol], index)
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
                continue

            outcomes = self.registry.generate_all(contexts)
            result.strategy_faults += sum(1 for o in outcomes if o.faulted)
            signals = [o.signal for o in outcomes if o.signal is not None]
            result.signals_generated += len(signals)

            if not signals:
                continue

            # 7. Risk-evaluate as a set, then queue for the next bar.
            risk_contexts = [
                self._risk_context(signal, account, current_bars, config, equity)
                for signal in signals
                if signal.instrument in current_bars
            ]
            result.proposals_made += len(risk_contexts)
            if not risk_contexts:
                continue

            evaluation = portfolio_engine.evaluate_set(risk_contexts, evaluated_at=moment)

            for decision, ctx in zip(
                evaluation.decisions,
                [self._by_id(risk_contexts, d.proposal_id) for d in evaluation.decisions],
                strict=False,
            ):
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

        result.trades = list(broker.state.closed_trades)
        result.ambiguous_bars = broker.state.ambiguous_bars
        result.final_balance = account.balance
        result.peak_equity = peak
        return result

    @staticmethod
    def _by_id(contexts: Sequence[RiskContext], proposal_id: str) -> RiskContext | None:
        return next((c for c in contexts if c.proposal.proposal_id == proposal_id), None)

    def _risk_context(
        self,
        signal: Signal,
        account: Account,
        bars: dict[str, Candle],
        config: BacktestConfig,
        equity: Decimal,
    ) -> RiskContext:
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
        daily_limit = config.starting_balance * Decimal("0.05")
        total_limit = config.starting_balance * Decimal("0.10")

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
        elif config.starting_balance > 0:
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
            requested_risk_pct=Decimal("0.35"),
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
                open_risk_pct=Decimal("0.35"),
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
                total_loss_used=max(Decimal(0), config.starting_balance - equity),
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
                # No economic calendar in a backtest. Set to a value outside any
                # buffer rather than None, which would block every trade — and
                # record the assumption, because it is one.
                minutes_to_news=9999,
                atr=None,
                spread_multiple=spread_multiple,
                volatility_ratio=None,
            ),
            portfolio=PortfolioState(open_positions=open_positions),
            mode=Mode.BACKTEST,
            approval_mode=ApprovalMode.AUTO_PAPER_FULL,
            profile_name=config.risk_profile,
            kill_switch_engaged=False,
            rates=self.rates,
            specs=self.specs,
        )
