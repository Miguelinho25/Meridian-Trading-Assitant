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

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from nemonis_broker.account import Account
from nemonis_broker.broker import ClosedTrade, PaperBroker
from nemonis_broker.fills import FillModel
from nemonis_config.clock import ReplayClock
from nemonis_config.settings import ApprovalMode, Mode, RiskProfileName
from nemonis_features.regime import DEFAULT_CLASSIFIER, RegimeClassifier
from nemonis_features.registry import FEATURES, FeatureDef
from nemonis_marketdata.barview import BarView
from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.types import Candle
from nemonis_risk.propfirm import PropFirmProfile
from nemonis_schemas.enums import ResultProvenance, RiskVerdict
from nemonis_strategy.registry import StrategyRegistry

from nemonis_backtest.cycle import CycleSettings, DecisionCycle


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

    def run(self, series: dict[str, list[Candle]], config: BacktestConfig) -> BacktestResult:
        """Execute the loop. Deterministic for a given seed and inputs.

        The per-bar decision pipeline lives in :class:`DecisionCycle`, shared
        with paper trading. This method is the *replay driver*: it owns the
        timeline, the equity curve and the result record. Everything about how a
        decision is reached is the cycle's, so a live loop cannot drift from what
        was backtested.
        """
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

        cycle = DecisionCycle(
            registry=self.registry,
            specs=self.specs,
            rates=self.rates,
            prop_profile=self.prop_profile,
            classifier=self.classifier,
            features=self.features,
            settings=CycleSettings(
                starting_balance=config.starting_balance,
                risk_profile=config.risk_profile,
                mode=Mode.BACKTEST,
                approval_mode=ApprovalMode.AUTO_PAPER_FULL,
                # A backtest has no live kill switch. Stated rather than
                # defaulted, so a driver that does have one cannot inherit this.
                kill_switch_engaged=False,
                warmup_bars=config.warmup_bars,
            ),
        )

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

        by_time = {
            sym: {b.open_time: b for b in bars if config.start <= b.open_time < config.end}
            for sym, bars in series.items()
        }
        indices: dict[str, dict[datetime, int]] = {
            sym: {b.open_time: i for i, b in enumerate(bars)} for sym, bars in series.items()
        }

        clock = ReplayClock(timeline[0])
        peak = config.starting_balance
        trading_day = cycle.trading_day_start(timeline[0])

        for moment in timeline:
            # The clock cannot be moved beyond this bar.
            clock.admit_bar(moment)

            current_bars = {sym: table[moment] for sym, table in by_time.items() if moment in table}
            if not current_bars:
                continue

            views = {}
            for symbol in current_bars:
                index = indices[symbol].get(moment)
                if index is not None:
                    views[symbol] = BarView(series[symbol], index)

            step = cycle.step(
                moment=moment,
                views=views,
                current_bars=current_bars,
                account=account,
                broker=broker,
                trading_day=trading_day,
            )

            if step.day_rolled:
                trading_day = cycle.trading_day_start(moment)
                result.daily_resets += 1

            peak = max(peak, step.equity)
            drawdown_pct = (peak - step.equity) / peak * Decimal(100) if peak > 0 else Decimal(0)
            result.max_drawdown_pct = max(result.max_drawdown_pct, drawdown_pct)
            result.equity_curve.append(
                EquityPoint(
                    at=moment,
                    equity=step.equity,
                    balance=account.balance,
                    drawdown_pct=drawdown_pct,
                    open_positions=len(account.positions),
                )
            )
            result.bars_processed += 1
            result.signals_generated += step.signals_generated
            result.proposals_made += step.proposals_made
            result.strategy_faults += step.strategy_faults
            result.decisions.extend(step.decisions)

        result.trades = list(broker.state.closed_trades)
        result.ambiguous_bars = broker.state.ambiguous_bars
        result.final_balance = account.balance
        result.peak_equity = peak
        return result
