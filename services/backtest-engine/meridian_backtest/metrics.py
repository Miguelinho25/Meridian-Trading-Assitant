"""Performance metrics and bias detection (backtesting-methodology.md §5–6).

Two rules govern everything here:

**No number without its sample size and provenance.** A profit factor of 1.6 from
40 trades and one from 4,000 are different claims. Displaying them identically is
the most common way a backtest misleads its own author.

**Below 30 trades, metrics are suppressed entirely.** Not shown with a caveat —
suppressed. A caveat beside a large green number gets ignored; an absent number
does not.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from meridian_broker.broker import ClosedTrade
from meridian_schemas.enums import ResultProvenance

#: Below this, no metrics are reported at all.
SUPPRESSION_THRESHOLD = 30
#: Below this, metrics are reported but flagged as insufficient.
SUFFICIENCY_THRESHOLD = 100


class BiasFlag(StrEnum):
    INSUFFICIENT_TRADES = "INSUFFICIENT_TRADES"
    SHORT_PERIOD = "SHORT_PERIOD"
    SINGLE_INSTRUMENT_DEPENDENCE = "SINGLE_INSTRUMENT_DEPENDENCE"
    REGIME_CONCENTRATION = "REGIME_CONCENTRATION"
    PERIOD_CONCENTRATION = "PERIOD_CONCENTRATION"
    OVERFITTING_SUSPECTED = "OVERFITTING_SUSPECTED"
    UNREALISTIC_FILLS = "UNREALISTIC_FILLS"
    LOOKAHEAD_SUSPECTED = "LOOKAHEAD_SUSPECTED"
    COST_DOMINATED = "COST_DOMINATED"
    SYNTHETIC_DATA = "SYNTHETIC_DATA"
    SPREAD_ASSUMED = "SPREAD_ASSUMED"
    HIGH_AMBIGUOUS_BAR_RATE = "HIGH_AMBIGUOUS_BAR_RATE"


@dataclass(frozen=True, slots=True)
class Flag:
    code: BiasFlag
    detail: str
    #: True when the flag alone should stop a result being treated as evidence.
    disqualifying: bool = False


@dataclass(frozen=True, slots=True)
class Metrics:
    """Performance summary. Never separated from its sample size."""

    provenance: ResultProvenance
    trade_count: int
    #: True when the sample is too small for any metric to be shown.
    suppressed: bool

    net_pnl: Decimal = Decimal(0)
    gross_profit: Decimal = Decimal(0)
    gross_loss: Decimal = Decimal(0)
    profit_factor: Decimal | None = None
    expectancy: Decimal | None = None
    win_rate: Decimal | None = None
    average_winner: Decimal | None = None
    average_loser: Decimal | None = None
    payoff_ratio: Decimal | None = None
    max_drawdown_pct: Decimal = Decimal(0)
    longest_losing_streak: int = 0
    longest_winning_streak: int = 0
    total_commission: Decimal = Decimal(0)
    #: Bootstrap 95% interval on expectancy. None when the sample is too small.
    expectancy_ci: tuple[Decimal, Decimal] | None = None
    flags: tuple[Flag, ...] = field(default_factory=tuple)

    @property
    def is_evidence(self) -> bool:
        """Whether this may be described as a result at all.

        Requires a sufficient sample, out-of-sample or walk-forward provenance,
        and no disqualifying flag. In-sample numbers never qualify, however good.
        """
        return (
            not self.suppressed
            and self.trade_count >= SUFFICIENCY_THRESHOLD
            and self.provenance in {ResultProvenance.OUT_OF_SAMPLE, ResultProvenance.WALK_FORWARD}
            and not any(f.disqualifying for f in self.flags)
        )

    @property
    def headline(self) -> str:
        """One line safe to display anywhere. Always carries n and provenance."""
        if self.suppressed:
            return (
                f"INSUFFICIENT EVIDENCE — {self.trade_count} trades "
                f"(minimum {SUPPRESSION_THRESHOLD})"
            )
        label = "" if self.is_evidence else " — NOT EVIDENCE"
        return (
            f"{self.provenance.value}: net {self.net_pnl}, "
            f"PF {self.profit_factor if self.profit_factor is not None else 'n/a'}, "
            f"n={self.trade_count}{label}"
        )


def _decimal_mean(values: list[Decimal]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values)) if values else Decimal(0)


def _bootstrap_ci(
    values: list[Decimal], *, seed: int = 12345, iterations: int = 2000
) -> tuple[Decimal, Decimal] | None:
    """Percentile bootstrap interval on the mean.

    Seeded so a reported interval is reproducible. Non-parametric because trade
    P&L is not normally distributed, and assuming it is understates the tails
    that actually matter.
    """
    if len(values) < SUPPRESSION_THRESHOLD:
        return None
    from random import Random

    rng = Random(seed)
    floats = [float(v) for v in values]
    n = len(floats)
    means = sorted(sum(rng.choice(floats) for _ in range(n)) / n for _ in range(iterations))
    low = means[int(0.025 * iterations)]
    high = means[int(0.975 * iterations)]
    return Decimal(str(round(low, 2))), Decimal(str(round(high, 2)))


def _streaks(trades: list[ClosedTrade]) -> tuple[int, int]:
    longest_loss = longest_win = current_loss = current_win = 0
    for trade in trades:
        if trade.pnl_account_ccy > 0:
            current_win += 1
            current_loss = 0
        elif trade.pnl_account_ccy < 0:
            current_loss += 1
            current_win = 0
        longest_win = max(longest_win, current_win)
        longest_loss = max(longest_loss, current_loss)
    return longest_loss, longest_win


def compute_metrics(
    trades: list[ClosedTrade],
    *,
    provenance: ResultProvenance,
    max_drawdown_pct: Decimal = Decimal(0),
    period_days: int = 0,
    ambiguous_bars: int = 0,
    bars_processed: int = 0,
    spread_assumed: bool = False,
    in_sample_profit_factor: Decimal | None = None,
) -> Metrics:
    """Compute metrics and run every bias check."""
    count = len(trades)
    flags = list(
        _detect_bias(
            trades,
            provenance=provenance,
            period_days=period_days,
            ambiguous_bars=ambiguous_bars,
            bars_processed=bars_processed,
            spread_assumed=spread_assumed,
            in_sample_profit_factor=in_sample_profit_factor,
        )
    )

    if count < SUPPRESSION_THRESHOLD:
        return Metrics(
            provenance=provenance,
            trade_count=count,
            suppressed=True,
            net_pnl=sum((t.pnl_account_ccy for t in trades), Decimal(0)),
            flags=tuple(flags),
        )

    pnls = [t.pnl_account_ccy for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins, Decimal(0))
    gross_loss = abs(sum(losses, Decimal(0)))
    longest_loss, longest_win = _streaks(trades)

    return Metrics(
        provenance=provenance,
        trade_count=count,
        suppressed=False,
        net_pnl=sum(pnls, Decimal(0)),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        expectancy=_decimal_mean(pnls),
        win_rate=Decimal(len(wins)) / Decimal(count) * Decimal(100),
        average_winner=_decimal_mean(wins) if wins else None,
        average_loser=_decimal_mean(losses) if losses else None,
        payoff_ratio=(
            abs(_decimal_mean(wins) / _decimal_mean(losses))
            if wins and losses and _decimal_mean(losses) != 0
            else None
        ),
        max_drawdown_pct=max_drawdown_pct,
        longest_losing_streak=longest_loss,
        longest_winning_streak=longest_win,
        total_commission=sum((t.commission for t in trades), Decimal(0)),
        expectancy_ci=_bootstrap_ci(pnls),
        flags=tuple(flags),
    )


def _detect_bias(
    trades: list[ClosedTrade],
    *,
    provenance: ResultProvenance,
    period_days: int,
    ambiguous_bars: int,
    bars_processed: int,
    spread_assumed: bool,
    in_sample_profit_factor: Decimal | None,
) -> list[Flag]:
    flags: list[Flag] = []
    count = len(trades)

    if provenance is ResultProvenance.SYNTHETIC:
        flags.append(
            Flag(
                BiasFlag.SYNTHETIC_DATA,
                "Synthetic data contains only the structure the generator was "
                "written to contain. Not evidence about any real market.",
                disqualifying=True,
            )
        )

    if spread_assumed:
        flags.append(
            Flag(
                BiasFlag.SPREAD_ASSUMED,
                "Source was mid-only; spread was assumed rather than measured, so "
                "trading costs are approximate.",
            )
        )

    if count < SUPPRESSION_THRESHOLD:
        flags.append(
            Flag(
                BiasFlag.INSUFFICIENT_TRADES,
                f"{count} trades is below the {SUPPRESSION_THRESHOLD} minimum; "
                f"metrics are suppressed.",
                disqualifying=True,
            )
        )
    elif count < SUFFICIENCY_THRESHOLD:
        flags.append(
            Flag(
                BiasFlag.INSUFFICIENT_TRADES,
                f"{count} trades is below the {SUFFICIENCY_THRESHOLD} needed for "
                f"expectancy to mean much.",
                disqualifying=True,
            )
        )

    if 0 < period_days < 730:
        flags.append(
            Flag(
                BiasFlag.SHORT_PERIOD,
                f"{period_days} days is under two years — unlikely to contain varied regimes.",
            )
        )

    if not trades:
        return flags

    total_profit = sum((t.pnl_account_ccy for t in trades if t.pnl_account_ccy > 0), Decimal(0))
    if total_profit > 0:
        by_instrument: dict[str, float] = {}
        for trade in trades:
            if trade.pnl_account_ccy > 0:
                by_instrument[trade.instrument] = by_instrument.get(trade.instrument, 0.0) + float(
                    trade.pnl_account_ccy
                )
        top_symbol, top_profit = max(by_instrument.items(), key=lambda kv: kv[1])
        share = Decimal(str(top_profit)) / total_profit
        if share > Decimal("0.60"):
            flags.append(
                Flag(
                    BiasFlag.SINGLE_INSTRUMENT_DEPENDENCE,
                    f"{share:.0%} of gross profit came from {top_symbol}. This is a "
                    f"statement about {top_symbol}, not about the strategy.",
                )
            )

        months: dict[str, float] = {}
        for trade in trades:
            if trade.pnl_account_ccy > 0:
                key = trade.closed_at.strftime("%Y-%m")
                months[key] = months.get(key, 0.0) + float(trade.pnl_account_ccy)
        if months:
            best_month = max(months.values())
            month_share = Decimal(str(best_month)) / total_profit
            if month_share > Decimal("0.40") and len(months) > 1:
                flags.append(
                    Flag(
                        BiasFlag.PERIOD_CONCENTRATION,
                        f"{month_share:.0%} of gross profit came from a single month.",
                    )
                )

    commission = sum((t.commission for t in trades), Decimal(0))
    if total_profit > 0 and commission > total_profit * Decimal("0.5"):
        flags.append(
            Flag(
                BiasFlag.COST_DOMINATED,
                f"Commission ({commission}) exceeds half of gross profit "
                f"({total_profit}). Costs, not edge, dominate this result.",
            )
        )

    wins = sum(1 for t in trades if t.pnl_account_ccy > 0)
    win_rate = Decimal(wins) / Decimal(count)
    if win_rate > Decimal("0.80") and count >= SUPPRESSION_THRESHOLD:
        flags.append(
            Flag(
                BiasFlag.LOOKAHEAD_SUSPECTED,
                f"{win_rate:.0%} win rate is implausible in liquid FX and usually "
                f"indicates leakage rather than skill.",
                disqualifying=True,
            )
        )

    if in_sample_profit_factor is not None and in_sample_profit_factor > 0:
        gross_loss = abs(
            sum((t.pnl_account_ccy for t in trades if t.pnl_account_ccy < 0), Decimal(0))
        )
        if gross_loss > 0:
            out_pf = total_profit / gross_loss
            if in_sample_profit_factor > out_pf * 2:
                flags.append(
                    Flag(
                        BiasFlag.OVERFITTING_SUSPECTED,
                        f"In-sample profit factor {in_sample_profit_factor:.2f} is more "
                        f"than double out-of-sample {out_pf:.2f}.",
                        disqualifying=True,
                    )
                )

    if bars_processed > 0:
        rate = Decimal(ambiguous_bars) / Decimal(bars_processed)
        if rate > Decimal("0.05"):
            flags.append(
                Flag(
                    BiasFlag.HIGH_AMBIGUOUS_BAR_RATE,
                    f"{rate:.1%} of bars had stop and target both reachable. The "
                    f"stop was assumed each time, so results depend heavily on "
                    f"that assumption.",
                )
            )

    return flags


def sharpe_ratio(pnls: list[Decimal], *, periods_per_year: int = 252) -> Decimal | None:
    """Annualised Sharpe on per-trade P&L.

    Reported for comparability, but per-trade Sharpe is a weak statistic on small
    samples and should never be read without the trade count beside it.
    """
    if len(pnls) < 2:
        return None
    values = [float(p) for p in pnls]
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    sd = math.sqrt(variance)
    if sd == 0:
        return None
    return Decimal(str(round(mean / sd * math.sqrt(periods_per_year), 4)))
