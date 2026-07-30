"""Backtest engine: determinism, look-ahead safety, and honest reporting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_backtest import BacktestConfig, BacktestEngine, compute_metrics
from nemonis_backtest.cycle import DecisionCycle
from nemonis_backtest.metrics import SUPPRESSION_THRESHOLD, BiasFlag
from nemonis_broker.broker import PaperBroker
from nemonis_broker.fills import FillModel, SlippageModel
from nemonis_marketdata import SyntheticGenerator
from nemonis_marketdata.instruments import WATCHLIST
from nemonis_risk.propfirm import GENERIC_TWO_PHASE
from nemonis_schemas.enums import ResultProvenance
from nemonis_strategy import (
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


def _registry(strategies=None) -> StrategyRegistry:
    registry = StrategyRegistry()
    for factory in strategies or (MovingAverageTrend, VolatilityBreakout):
        registry.register(factory(), status=LifecycleStatus.ACTIVE)
    return registry


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


class TestStopsAndTargetsAreTradeablePrices:
    """Protective levels must sit on the venue's tick grid by the time they leave
    the pipeline.

    Strategies compute them as ATR multiples, so they arrive with the full
    precision of the division — a paper session held a EURUSD stop of
    1.053292142857142857142857143, the repeating decimal of a 14-bar mean. Every
    layer accepted it because nothing between the strategy and the broker
    asserted anything about price precision, and the paper broker is happy to
    fill at any Decimal. The first real broker adapter would have had the order
    rejected outright.
    """

    def _run_capturing(self, series, monkeypatch):
        """Run a backtest, capturing (signal, proposal) pairs and submitted orders.

        Pairs are taken where the conversion happens rather than reconstructed
        afterwards. Matching a submitted stop back to its signal by proximity
        does not work: ATR moves slowly, so consecutive bars produce stops well
        within a tick of each other and the wrong signal gets matched.
        """
        pairs = []
        submitted = []

        risk_context = DecisionCycle._risk_context
        submit = PaperBroker.submit

        def spy_risk_context(self, signal, account, bars, equity):
            ctx = risk_context(self, signal, account, bars, equity)
            pairs.append((signal, ctx.proposal))
            return ctx

        def spy_submit(self, **kw):
            submitted.append((kw["instrument"], kw.get("stop_loss"), kw.get("take_profit")))
            return submit(self, **kw)

        monkeypatch.setattr(DecisionCycle, "_risk_context", spy_risk_context)
        monkeypatch.setattr(PaperBroker, "submit", spy_submit)
        make_engine().run(series, make_config(series))
        return pairs, submitted

    def test_the_fixture_really_does_produce_off_grid_levels(self, series, monkeypatch) -> None:
        """Guards the assertions below from passing vacuously.

        If the baselines ever started emitting levels that happen to land on the
        grid, the on-grid assertions would hold whether or not quantisation
        existed, and the regression would be free to come back unnoticed.
        """
        pairs, _ = self._run_capturing(series, monkeypatch)
        assert pairs, "no signals reached the risk engine — the fixture proves nothing"

        off_grid = [s for s, _ in pairs if s.stop % WATCHLIST[s.instrument].tick_size != 0]
        assert off_grid, (
            "every raw strategy stop already sat on the tick grid, so the "
            "quantisation assertions below would prove nothing"
        )

    def test_every_level_reaching_the_broker_lands_on_the_grid(self, series, monkeypatch) -> None:
        _, submitted = self._run_capturing(series, monkeypatch)
        assert submitted, "no orders submitted — the fixture is not exercising the pipeline"

        for symbol, stop, target in submitted:
            tick = WATCHLIST[symbol].tick_size
            assert stop is not None
            assert stop % tick == 0, (
                f"{symbol} stop {stop} is not a multiple of {tick}. No venue would "
                f"accept this order."
            )
            if target is not None:
                assert target % tick == 0, f"{symbol} target {target} is not a multiple of {tick}."

    def test_quantising_widened_stops_rather_than_tightening_them(
        self, series, monkeypatch
    ) -> None:
        """The safety direction, on every proposal the pipeline built.

        Sizing divides by the stop distance, so a stop pulled *closer* to entry
        would leave the position risking more than the operator authorised.
        """
        pairs, _ = self._run_capturing(series, monkeypatch)
        assert pairs

        for signal, proposal in pairs:
            assert abs(signal.entry - proposal.stop) >= abs(signal.entry - signal.stop), (
                f"{signal.instrument} {signal.direction.value}: stop moved from "
                f"{signal.stop} to {proposal.stop} against entry {signal.entry} — "
                f"nearer entry than the strategy asked. The position is sized against "
                f"this distance, so realised risk would exceed authorised risk."
            )

    def test_targets_are_never_moved_further_out(self, series, monkeypatch) -> None:
        """The mirror: quantisation may shave reward, never invent it."""
        pairs, _ = self._run_capturing(series, monkeypatch)
        checked = 0
        for signal, proposal in pairs:
            if signal.target is None:
                continue
            assert proposal.target is not None
            checked += 1
            assert abs(signal.entry - proposal.target) <= abs(signal.entry - signal.target), (
                f"{signal.instrument}: target moved from {signal.target} to "
                f"{proposal.target} against entry {signal.entry} — further out than "
                f"the strategy claimed, flattering reward-to-risk."
            )
        assert checked, "no proposal carried a target"

    def test_proposals_carry_grid_aligned_levels(self, series, monkeypatch) -> None:
        """Asserted on the proposal, not just on what reached the broker.

        Rejected proposals never reach a broker, but they are recorded as
        research data and read back — they should be as well-formed as the
        approved ones.
        """
        pairs, _ = self._run_capturing(series, monkeypatch)
        for _signal, proposal in pairs:
            tick = WATCHLIST[proposal.instrument].tick_size
            assert proposal.stop % tick == 0
            if proposal.target is not None:
                assert proposal.target % tick == 0


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
        from nemonis_broker.broker import ClosedTrade
        from nemonis_broker.fills import FillReason
        from nemonis_schemas.enums import Direction

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
            from nemonis_broker.broker import ClosedTrade
            from nemonis_broker.fills import FillReason
            from nemonis_schemas.enums import Direction

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
        from nemonis_broker.broker import ClosedTrade
        from nemonis_broker.fills import FillReason
        from nemonis_schemas.enums import Direction

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
        from nemonis_broker.broker import ClosedTrade
        from nemonis_broker.fills import FillReason
        from nemonis_schemas.enums import Direction

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


class TestTheDailyResetActuallyHappens:
    """The daily loss reference must move with the trading day.

    It did not. ``Account.start_new_day()`` existed and had no callers, so
    ``balance_at_day_start`` stayed at the opening balance for the whole run and
    ``daily_loss_used`` measured the loss since the *start of the backtest*. The
    daily limit therefore behaved as a lifetime limit: once cumulative drawdown
    reached it, every later proposal was rejected DAILY_LOSS_WOULD_BREACH and
    never recovered.

    On real 2010-2026 data the engine stopped trading in March 2017 and produced
    nothing across the remaining 56% of the timeline, while still reporting
    metrics as though it had covered the full period. Fixing it took the run from
    311 trades to 723 -- and net P&L from -4,818 to -7,076, because the bug had
    been truncating the losing tail.

    The whole 259-test suite passed throughout. Nothing asserted that trading
    survives the day it first loses money.
    """

    def test_trading_continues_into_the_final_quarter_of_the_run(
        self, series: dict[str, list]
    ) -> None:
        """The observable symptom: trades stop early and never resume."""
        result = make_engine().run(series, make_config(series))
        assert result.trades, "no trades at all — fixture is not exercising the engine"

        start = min(b[0].open_time for b in series.values())
        end = max(b[-1].open_time for b in series.values())
        final_quarter = start + (end - start) * 3 // 4

        late = [t for t in result.trades if t.opened_at >= final_quarter]
        assert late, (
            f"No trades after {final_quarter.date()} despite {len(result.trades)} "
            f"earlier ones. The daily loss reference has probably stopped resetting, "
            f"turning the daily limit into a lifetime one."
        )

    def test_the_daily_limit_releases_the_next_day(self, series: dict[str, list]) -> None:
        """The direct reproduction, with a limit tight enough to bite immediately.

        The default 5% daily allowance is never consumed by the synthetic
        fixture, so a test using it passes whether or not the reset exists --
        which is exactly how the bug survived.

        1% is chosen deliberately between two failure modes: it must exceed one
        trade's risk (0.35%) or every proposal is rejected on its own projected
        loss and no latch is ever demonstrated, and it must be small enough that
        a few losing trades consume it within the fixture's span.
        """
        tight = replace(GENERIC_TWO_PHASE, max_daily_loss_pct=Decimal("1.00"))
        engine = BacktestEngine(
            registry=_registry(),
            specs=dict(WATCHLIST),
            rates=RATES,
            prop_profile=tight,
        )
        result = engine.run(series, make_config(series))

        blocks = [
            when
            for when, _sid, _v, reason in result.decisions
            if reason == "DAILY_LOSS_WOULD_BREACH"
        ]
        assert blocks, (
            "the tight daily limit was never hit — the fixture cannot reproduce "
            "the latch, so this test would prove nothing"
        )

        first_block = min(blocks)
        later = [t for t in result.trades if t.opened_at.date() > first_block.date()]
        assert later, (
            f"The daily loss limit first blocked a trade on {first_block.date()} and "
            f"nothing traded on any later day. A daily limit that never releases is "
            f"a lifetime limit."
        )

    def test_the_reference_resets_once_per_trading_day(self, series: dict[str, list]) -> None:
        """The mechanism, asserted directly.

        Emergent-behaviour tests proved unreliable here: whether the latch is
        *permanent* depends on whether equity ever recovers above the threshold,
        which the synthetic fixture does and the real 2010-2026 data did not. Two
        earlier versions of this test passed with the fix removed. Counting the
        resets is fixture-independent -- with the defect it is exactly zero,
        whatever the prices do.
        """
        result = make_engine().run(series, make_config(series))
        assert result.daily_resets > 0, (
            f"The trading day never rolled across {result.bars_processed} bars. "
            f"daily_loss_used is therefore measured from the opening balance for "
            f"the entire run, and the daily limit is a lifetime limit."
        )

        # Exact, not approximate: one reset per day boundary crossed, counted
        # against the profile's own timezone rather than UTC midnight. Comparing
        # to bar count would be wrong -- the fixture is intraday, so many bars
        # share a trading day.
        days = {
            GENERIC_TWO_PHASE.trading_day_start(bar.open_time)
            for bars in series.values()
            for bar in bars
        }
        assert result.daily_resets == len(days) - 1, (
            f"{result.daily_resets} resets across {len(days)} distinct trading "
            f"days. The day boundary is being computed wrongly."
        )
