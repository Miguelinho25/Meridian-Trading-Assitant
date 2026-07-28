"""Backtest engine: determinism, look-ahead safety, and honest reporting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from meridian_backtest import BacktestConfig, BacktestEngine, compute_metrics
from meridian_backtest.metrics import SUPPRESSION_THRESHOLD, BiasFlag
from meridian_broker.fills import FillModel, SlippageModel
from meridian_marketdata import SyntheticGenerator
from meridian_marketdata.instruments import WATCHLIST
from meridian_risk.propfirm import GENERIC_TWO_PHASE
from meridian_schemas.enums import ResultProvenance
from meridian_strategy import (
    LifecycleStatus,
    MovingAverageTrend,
    StrategyRegistry,
    VolatilityBreakout,
)

from tests.risk.conftest import RATES

START = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture
def series() -> dict[str, list]:
    return {
        sym: SyntheticGenerator(sym, seed=2024).generate_list(START, 900)
        for sym in ("EURUSD", "GBPUSD")
    }


def make_engine(*, strategies=None) -> BacktestEngine:
    registry = StrategyRegistry()
    for factory in strategies or (MovingAverageTrend, VolatilityBreakout):
        registry.register(factory(), status=LifecycleStatus.ACTIVE)
    return BacktestEngine(
        registry=registry,
        specs=dict(WATCHLIST),
        rates=RATES,
        prop_profile=GENERIC_TWO_PHASE,
    )


def make_config(series, **kw) -> BacktestConfig:
    end = max(b.open_time for bars in series.values() for b in bars)
    base = {
        "instruments": tuple(series),
        "start": START,
        "end": end,
        "fill_model": FillModel(slippage=SlippageModel.NONE),
        "seed": 11,
    }
    return BacktestConfig(**{**base, **kw})


class TestItRuns:
    def test_produces_an_equity_curve(self, series) -> None:
        result = make_engine().run(series, make_config(series))
        assert result.bars_processed > 500
        assert len(result.equity_curve) == result.bars_processed

    def test_generates_signals_and_proposals(self, series) -> None:
        result = make_engine().run(series, make_config(series))
        assert result.signals_generated > 0
        assert result.proposals_made > 0

    def test_records_rejections_not_just_fills(self, series) -> None:
        """A system that only records what it did cannot tell you what it declined."""
        result = make_engine().run(series, make_config(series))
        assert result.decisions
        assert len(result.rejections) < len(result.decisions)

    def test_no_strategy_faults(self, series) -> None:
        assert make_engine().run(series, make_config(series)).strategy_faults == 0


class TestDeterminism:
    def test_identical_runs_produce_identical_ledgers(self, series) -> None:
        """Stage E done-criterion. Non-determinism makes every result worthless."""
        a = make_engine().run(series, make_config(series))
        b = make_engine().run(series, make_config(series))

        assert a.bars_processed == b.bars_processed
        assert a.final_balance == b.final_balance
        assert len(a.trades) == len(b.trades)
        assert [
            (t.instrument, t.entry_price, t.exit_price, t.pnl_account_ccy) for t in a.trades
        ] == [(t.instrument, t.entry_price, t.exit_price, t.pnl_account_ccy) for t in b.trades]

    def test_deterministic_under_stochastic_slippage(self, series) -> None:
        config = make_config(series, fill_model=FillModel(slippage=SlippageModel.STOCHASTIC))
        a = make_engine().run(series, config)
        b = make_engine().run(series, config)
        assert [t.pnl_account_ccy for t in a.trades] == [t.pnl_account_ccy for t in b.trades]

    def test_equity_curves_match_exactly(self, series) -> None:
        a = make_engine().run(series, make_config(series))
        b = make_engine().run(series, make_config(series))
        assert [p.equity for p in a.equity_curve] == [p.equity for p in b.equity_curve]


class TestLookAheadSafety:
    def test_altering_the_future_does_not_change_earlier_bars(self, series) -> None:
        """The strongest available check: replace the tail with different data
        and require the earlier equity curve to be untouched."""
        cut = 600
        truncated = {sym: bars[:cut] for sym, bars in series.items()}
        end = max(b.open_time for bars in truncated.values() for b in bars)

        baseline = make_engine().run(truncated, make_config(truncated, end=end))

        tampered = {
            sym: bars[:cut]
            + SyntheticGenerator(sym, seed=999).generate_list(bars[cut].open_time, 300)
            for sym, bars in series.items()
        }
        after = make_engine().run(tampered, make_config(truncated, end=end))

        assert [p.equity for p in baseline.equity_curve] == [p.equity for p in after.equity_curve]

    def test_warmup_bars_produce_no_trades(self, series) -> None:
        early_end = series["EURUSD"][40].open_time
        result = make_engine().run(series, make_config(series, end=early_end))
        assert result.trades == []


class TestHonestReporting:
    def test_synthetic_results_are_disqualified(self, series) -> None:
        result = make_engine().run(series, make_config(series))
        metrics = compute_metrics(
            result.trades,
            provenance=ResultProvenance.SYNTHETIC,
            max_drawdown_pct=result.max_drawdown_pct,
        )
        assert not metrics.is_evidence
        assert any(f.code is BiasFlag.SYNTHETIC_DATA for f in metrics.flags)

    def test_small_samples_are_suppressed_not_caveated(self, series) -> None:
        """A caveat beside a big green number gets ignored; an absent number
        does not."""
        result = make_engine().run(series, make_config(series))
        metrics = compute_metrics(result.trades[:5], provenance=ResultProvenance.OUT_OF_SAMPLE)
        assert metrics.suppressed
        assert metrics.profit_factor is None
        assert "INSUFFICIENT EVIDENCE" in metrics.headline

    def test_in_sample_is_never_evidence(self, series) -> None:
        result = make_engine().run(series, make_config(series))
        metrics = compute_metrics(result.trades, provenance=ResultProvenance.IN_SAMPLE)
        assert not metrics.is_evidence

    def test_headline_always_carries_n_and_provenance(self, series) -> None:
        result = make_engine().run(series, make_config(series))
        headline = compute_metrics(
            result.trades, provenance=ResultProvenance.OUT_OF_SAMPLE
        ).headline
        assert "n=" in headline or "INSUFFICIENT" in headline

    def test_assumed_spread_is_flagged(self, series) -> None:
        result = make_engine().run(series, make_config(series))
        metrics = compute_metrics(
            result.trades, provenance=ResultProvenance.OUT_OF_SAMPLE, spread_assumed=True
        )
        assert any(f.code is BiasFlag.SPREAD_ASSUMED for f in metrics.flags)


class TestBiasFlagsFireOnRiggedFixtures:
    """Stage E done-criterion: the flags must actually catch what they claim."""

    def _fake_trades(self, n: int, *, win_rate: float, instrument: str = "EURUSD"):
        from meridian_broker.broker import ClosedTrade
        from meridian_broker.fills import FillReason
        from meridian_schemas.enums import Direction

        trades = []
        for i in range(n):
            win = (i / n) < win_rate
            trades.append(
                ClosedTrade(
                    trade_id=f"t{i}",
                    instrument=instrument,
                    direction=Direction.LONG,
                    lots=Decimal("0.1"),
                    entry_price=Decimal("1.08"),
                    exit_price=Decimal("1.09"),
                    opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                    closed_at=datetime(2026, (i % 12) + 1, 1, tzinfo=UTC),
                    strategy_id="s",
                    pnl_account_ccy=Decimal("100") if win else Decimal("-50"),
                    commission=Decimal("3.5"),
                    reason=FillReason.TAKE_PROFIT,
                    mfe_pips=Decimal(10),
                    mae_pips=Decimal(5),
                )
            )
        return trades

    def test_implausible_win_rate_flags_lookahead(self) -> None:
        metrics = compute_metrics(
            self._fake_trades(200, win_rate=0.95),
            provenance=ResultProvenance.OUT_OF_SAMPLE,
        )
        assert any(f.code is BiasFlag.LOOKAHEAD_SUSPECTED for f in metrics.flags)
        assert not metrics.is_evidence

    def test_single_instrument_dependence_is_flagged(self) -> None:
        trades = self._fake_trades(120, win_rate=0.5, instrument="GBPJPY")
        metrics = compute_metrics(trades, provenance=ResultProvenance.OUT_OF_SAMPLE)
        assert any(f.code is BiasFlag.SINGLE_INSTRUMENT_DEPENDENCE for f in metrics.flags)

    def test_overfitting_is_flagged_when_in_sample_dominates(self) -> None:
        metrics = compute_metrics(
            self._fake_trades(150, win_rate=0.5),
            provenance=ResultProvenance.OUT_OF_SAMPLE,
            in_sample_profit_factor=Decimal("9.0"),
        )
        assert any(f.code is BiasFlag.OVERFITTING_SUSPECTED for f in metrics.flags)

    def test_a_clean_result_can_qualify_as_evidence(self) -> None:
        """The converse — otherwise the flags could be blocking everything and
        the tests above would prove nothing."""
        trades = []
        for i, sym in enumerate(["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"] * 40):
            from meridian_broker.broker import ClosedTrade
            from meridian_broker.fills import FillReason
            from meridian_schemas.enums import Direction

            trades.append(
                ClosedTrade(
                    trade_id=f"t{i}",
                    instrument=sym,
                    direction=Direction.LONG,
                    lots=Decimal("0.1"),
                    entry_price=Decimal("1.08"),
                    exit_price=Decimal("1.09"),
                    opened_at=datetime(2024, 1, 1, tzinfo=UTC),
                    closed_at=datetime(2024 + i % 2, (i % 12) + 1, 1, tzinfo=UTC),
                    strategy_id="s",
                    pnl_account_ccy=Decimal("120") if i % 2 else Decimal("-100"),
                    commission=Decimal("1.0"),
                    reason=FillReason.TAKE_PROFIT,
                    mfe_pips=Decimal(10),
                    mae_pips=Decimal(5),
                )
            )
        metrics = compute_metrics(
            trades, provenance=ResultProvenance.OUT_OF_SAMPLE, period_days=900
        )
        assert metrics.is_evidence, [f.code.value for f in metrics.flags]


class TestMetricsQuality:
    def test_expectancy_interval_is_reported(self) -> None:
        from meridian_broker.broker import ClosedTrade
        from meridian_broker.fills import FillReason
        from meridian_schemas.enums import Direction

        trades = [
            ClosedTrade(
                trade_id=f"t{i}",
                instrument="EURUSD",
                direction=Direction.LONG,
                lots=Decimal("0.1"),
                entry_price=Decimal("1.08"),
                exit_price=Decimal("1.09"),
                opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                closed_at=datetime(2026, 1, 2, tzinfo=UTC),
                strategy_id="s",
                pnl_account_ccy=Decimal("100") if i % 3 else Decimal("-90"),
                commission=Decimal("3.5"),
                reason=FillReason.TAKE_PROFIT,
                mfe_pips=Decimal(10),
                mae_pips=Decimal(5),
            )
            for i in range(SUPPRESSION_THRESHOLD + 20)
        ]
        metrics = compute_metrics(trades, provenance=ResultProvenance.OUT_OF_SAMPLE)
        assert metrics.expectancy_ci is not None
        low, high = metrics.expectancy_ci
        assert low <= metrics.expectancy <= high

    def test_interval_is_reproducible(self) -> None:
        from meridian_broker.broker import ClosedTrade
        from meridian_broker.fills import FillReason
        from meridian_schemas.enums import Direction

        trades = [
            ClosedTrade(
                trade_id=f"t{i}",
                instrument="EURUSD",
                direction=Direction.LONG,
                lots=Decimal("0.1"),
                entry_price=Decimal("1.08"),
                exit_price=Decimal("1.09"),
                opened_at=datetime(2026, 1, 1, tzinfo=UTC),
                closed_at=datetime(2026, 1, 2, tzinfo=UTC),
                strategy_id="s",
                pnl_account_ccy=Decimal("100") if i % 3 else Decimal("-90"),
                commission=Decimal("3.5"),
                reason=FillReason.TAKE_PROFIT,
                mfe_pips=Decimal(10),
                mae_pips=Decimal(5),
            )
            for i in range(60)
        ]
        a = compute_metrics(trades, provenance=ResultProvenance.OUT_OF_SAMPLE)
        b = compute_metrics(trades, provenance=ResultProvenance.OUT_OF_SAMPLE)
        assert a.expectancy_ci == b.expectancy_ci
