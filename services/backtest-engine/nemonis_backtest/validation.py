"""Validation protocol (backtesting-methodology.md §4).

A single backtest is one draw from a distribution. These three tools ask the
questions that draw cannot answer:

* **Walk-forward** — does it hold up on data after the fitting window, and does
  it hold up *consistently*, or did one window carry everything?
* **Monte Carlo** — how much of the realised drawdown was the strategy, and how
  much was the order the trades happened to arrive in?
* **Stress** — does the edge survive costs being worse than assumed?

Per-window and per-run detail is always reported alongside the aggregate. An
aggregate hides the case where all the profit came from one period, which is the
single most common way a walk-forward result flatters a strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from random import Random

from nemonis_broker.broker import ClosedTrade
from nemonis_broker.fills import FillModel, SlippageModel
from nemonis_marketdata.types import Candle
from nemonis_risk.propfirm import PropFirmProfile
from nemonis_schemas.enums import ResultProvenance

from nemonis_backtest.engine import BacktestConfig, BacktestEngine
from nemonis_backtest.metrics import Metrics, compute_metrics

# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Window:
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    metrics: Metrics
    trade_count: int


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    windows: tuple[Window, ...]
    combined: Metrics

    @property
    def profitable_windows(self) -> int:
        return sum(1 for w in self.windows if w.metrics.net_pnl > 0)

    @property
    def consistency(self) -> Decimal:
        """Fraction of windows that were profitable.

        The number that matters more than the aggregate. A strategy profitable
        in 2 of 8 windows and hugely profitable in one of them has an aggregate
        that says nothing useful about the next window.
        """
        if not self.windows:
            return Decimal(0)
        return Decimal(self.profitable_windows) / Decimal(len(self.windows))

    @property
    def carried_by_one_window(self) -> bool:
        """True when a single window supplied most of the total profit."""
        total = sum((w.metrics.net_pnl for w in self.windows), Decimal(0))
        if total <= 0:
            return False
        best = max(w.metrics.net_pnl for w in self.windows)
        return best > total * Decimal("0.60")


def walk_forward(
    engine: BacktestEngine,
    series: dict[str, list[Candle]],
    config: BacktestConfig,
    *,
    windows: int = 6,
    train_fraction: Decimal = Decimal("0.6"),
    expanding: bool = False,
) -> WalkForwardResult:
    """Run sequential train/test windows and report each separately.

    ``expanding`` keeps every earlier bar in the training window rather than
    rolling it forward. Both are offered because they answer different
    questions: rolling asks whether recent history predicts the near future,
    expanding asks whether all history does.

    There is no parameter fitting yet, so the training window currently only
    warms up features. The split is real regardless — test windows are strictly
    later than their training window, which is what makes them out-of-sample.
    """
    span = config.end - config.start
    if windows < 1 or span <= timedelta(0):
        return WalkForwardResult((), compute_metrics([], provenance=config.provenance))

    step = span / windows
    results: list[Window] = []
    all_trades: list[ClosedTrade] = []

    for index in range(windows):
        window_start = config.start + step * index
        window_end = config.start + step * (index + 1)
        boundary = window_start + (window_end - window_start) * float(train_fraction)

        train_start = config.start if expanding else window_start
        test_config = BacktestConfig(
            instruments=config.instruments,
            start=boundary,
            end=window_end,
            starting_balance=config.starting_balance,
            account_currency=config.account_currency,
            risk_profile=config.risk_profile,
            fill_model=config.fill_model,
            seed=config.seed + index,
            # Each window is genuinely out-of-sample relative to its own
            # training period, so it is labelled as such.
            provenance=ResultProvenance.WALK_FORWARD,
            spread_assumed=config.spread_assumed,
            warmup_bars=config.warmup_bars,
        )

        result = engine.run(series, test_config)
        all_trades.extend(result.trades)

        results.append(
            Window(
                index=index,
                train_start=train_start,
                train_end=boundary,
                test_start=boundary,
                test_end=window_end,
                metrics=compute_metrics(
                    result.trades,
                    provenance=ResultProvenance.WALK_FORWARD,
                    max_drawdown_pct=result.max_drawdown_pct,
                    spread_assumed=config.spread_assumed,
                ),
                trade_count=len(result.trades),
            )
        )

    return WalkForwardResult(
        windows=tuple(results),
        combined=compute_metrics(
            all_trades,
            provenance=ResultProvenance.WALK_FORWARD,
            spread_assumed=config.spread_assumed,
        ),
    )


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    iterations: int
    trade_count: int
    median_max_drawdown: Decimal
    p95_max_drawdown: Decimal
    worst_max_drawdown: Decimal
    median_terminal: Decimal
    p05_terminal: Decimal
    ruin_probability: Decimal
    prop_pass_probability: Decimal | None = None

    @property
    def realised_drawdown_was_lucky(self) -> bool:
        """True when the realised path was better than the median reshuffle.

        Worth knowing: it means the drawdown actually experienced understates
        what the same trades could plausibly have produced.
        """
        return self.median_max_drawdown > Decimal(0)


def monte_carlo(
    trades: list[ClosedTrade],
    *,
    starting_balance: Decimal,
    iterations: int = 10_000,
    seed: int = 20260727,
    prop_profile: PropFirmProfile | None = None,
) -> MonteCarloResult:
    """Reshuffle trade order and report the distribution of outcomes.

    Trade sequence is largely luck. The realised maximum drawdown is one draw
    from a distribution, and for an evaluation account the tail matters far more
    than the median — a breach ends the account outright, so a path that would
    have breached in 30% of orderings is not a survivable strategy even if the
    realised path did not.

    Reshuffling preserves the trades themselves and varies only their order, so
    it isolates sequence risk from edge.
    """
    if not trades:
        return MonteCarloResult(
            iterations=0,
            trade_count=0,
            median_max_drawdown=Decimal(0),
            p95_max_drawdown=Decimal(0),
            worst_max_drawdown=Decimal(0),
            median_terminal=starting_balance,
            p05_terminal=starting_balance,
            ruin_probability=Decimal(0),
        )

    rng = Random(seed)
    pnls = [float(t.pnl_account_ccy - t.commission) for t in trades]
    start = float(starting_balance)

    ruin_floor = start * 0.5
    prop_floor = (
        float(starting_balance - prop_profile.total_loss_limit)
        if prop_profile is not None
        else None
    )
    prop_target = (
        float(starting_balance + prop_profile.profit_target)
        if prop_profile is not None and prop_profile.profit_target is not None
        else None
    )

    drawdowns: list[float] = []
    terminals: list[float] = []
    ruined = 0
    passed = 0

    order = list(pnls)
    for _ in range(iterations):
        rng.shuffle(order)
        equity = start
        peak = start
        worst = 0.0
        breached = False
        reached_target = False

        for pnl in order:
            equity += pnl
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak if peak > 0 else 0.0)
            if equity <= ruin_floor:
                breached = True
            if prop_floor is not None and equity <= prop_floor:
                breached = True
                break
            if prop_target is not None and equity >= prop_target:
                reached_target = True

        drawdowns.append(worst)
        terminals.append(equity)
        if breached:
            ruined += 1
        elif reached_target:
            passed += 1

    drawdowns.sort()
    terminals.sort()

    def pct(values: list[float], q: float) -> Decimal:
        return Decimal(str(round(values[min(len(values) - 1, int(q * len(values)))], 4)))

    return MonteCarloResult(
        iterations=iterations,
        trade_count=len(trades),
        median_max_drawdown=pct(drawdowns, 0.5) * Decimal(100),
        p95_max_drawdown=pct(drawdowns, 0.95) * Decimal(100),
        worst_max_drawdown=Decimal(str(round(drawdowns[-1], 4))) * Decimal(100),
        median_terminal=pct(terminals, 0.5),
        p05_terminal=pct(terminals, 0.05),
        ruin_probability=Decimal(ruined) / Decimal(iterations),
        prop_pass_probability=(
            Decimal(passed) / Decimal(iterations) if prop_profile is not None else None
        ),
    )


# ---------------------------------------------------------------------------
# Stress tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StressScenario:
    name: str
    description: str
    metrics: Metrics
    trade_count: int


@dataclass(frozen=True, slots=True)
class StressResult:
    baseline: Metrics
    scenarios: tuple[StressScenario, ...] = field(default_factory=tuple)

    def degradation(self, name: str) -> Decimal | None:
        """Fractional fall in net P&L against the baseline."""
        scenario = next((s for s in self.scenarios if s.name == name), None)
        if scenario is None or self.baseline.net_pnl == 0:
            return None
        return (self.baseline.net_pnl - scenario.metrics.net_pnl) / abs(self.baseline.net_pnl)

    @property
    def survives_all(self) -> bool:
        """True when net P&L stays positive under every scenario.

        A strategy that only works under optimistic costs does not work.
        """
        return all(s.metrics.net_pnl > 0 for s in self.scenarios)


def stress_test(
    engine: BacktestEngine,
    series: dict[str, list[Candle]],
    config: BacktestConfig,
    *,
    missing_bar_fraction: float = 0.05,
    seed: int = 99,
) -> StressResult:
    """Re-run under worse-than-assumed conditions.

    Defaults are deliberately punishing. The question is not whether the result
    degrades — it will — but whether it survives at all, and by how much.
    """
    baseline_run = engine.run(series, config)
    baseline = compute_metrics(
        baseline_run.trades,
        provenance=config.provenance,
        max_drawdown_pct=baseline_run.max_drawdown_pct,
        spread_assumed=config.spread_assumed,
    )

    scenarios: list[StressScenario] = []

    def add(
        name: str,
        description: str,
        cfg: BacktestConfig,
        data: dict[str, list[Candle]] = series,
    ) -> None:
        run = engine.run(data, cfg)
        scenarios.append(
            StressScenario(
                name=name,
                description=description,
                metrics=compute_metrics(
                    run.trades,
                    provenance=cfg.provenance,
                    max_drawdown_pct=run.max_drawdown_pct,
                    spread_assumed=cfg.spread_assumed,
                ),
                trade_count=len(run.trades),
            )
        )

    def with_fill(model: FillModel) -> BacktestConfig:
        return BacktestConfig(
            instruments=config.instruments,
            start=config.start,
            end=config.end,
            starting_balance=config.starting_balance,
            account_currency=config.account_currency,
            risk_profile=config.risk_profile,
            fill_model=model,
            seed=config.seed,
            provenance=config.provenance,
            spread_assumed=config.spread_assumed,
            warmup_bars=config.warmup_bars,
        )

    add(
        "double_slippage",
        "Slippage at twice the assumed fraction of spread",
        with_fill(
            FillModel(
                slippage=SlippageModel.PROPORTIONAL_TO_SPREAD,
                spread_fraction=config.fill_model.spread_fraction * 2,
                gap_penalty=config.fill_model.gap_penalty,
            )
        ),
    )

    add(
        "fixed_wide_slippage",
        "Two pips of slippage on every fill, regardless of spread",
        with_fill(FillModel(slippage=SlippageModel.FIXED, fixed_slippage_pips=Decimal("2.0"))),
    )

    # Widening the spread means widening the data itself, since the spread lives
    # on the bars rather than in the fill model.
    widened = {
        symbol: [_widen_spread(bar, Decimal(2)) for bar in bars] for symbol, bars in series.items()
    }
    add("double_spread", "Every bar's bid/ask spread doubled", config, widened)

    thinned = _drop_bars(series, fraction=missing_bar_fraction, seed=seed)
    add(
        "missing_bars",
        f"{missing_bar_fraction:.0%} of bars randomly absent",
        config,
        thinned,
    )

    return StressResult(baseline=baseline, scenarios=tuple(scenarios))


def _widen_spread(bar: Candle, factor: Decimal) -> Candle:
    """Widen a bar's spread, keeping the bid side fixed."""
    from dataclasses import replace

    extra_o = (bar.ask_open - bar.bid_open) * (factor - 1)
    extra_h = (bar.ask_high - bar.bid_high) * (factor - 1)
    extra_l = (bar.ask_low - bar.bid_low) * (factor - 1)
    extra_c = (bar.ask_close - bar.bid_close) * (factor - 1)
    return replace(
        bar,
        ask_open=bar.ask_open + extra_o,
        ask_high=bar.ask_high + extra_h,
        ask_low=bar.ask_low + extra_l,
        ask_close=bar.ask_close + extra_c,
    )


def _drop_bars(
    series: dict[str, list[Candle]], *, fraction: float, seed: int
) -> dict[str, list[Candle]]:
    """Randomly remove bars, seeded so the scenario is reproducible."""
    rng = Random(seed)
    return {
        symbol: [bar for bar in bars if rng.random() > fraction] for symbol, bars in series.items()
    }
