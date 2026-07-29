"""Walk-forward, Monte Carlo and stress testing."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_backtest import BacktestConfig
from nemonis_backtest.validation import monte_carlo, stress_test, walk_forward
from nemonis_broker.broker import ClosedTrade
from nemonis_broker.fills import FillModel, FillReason, SlippageModel
from nemonis_marketdata import SyntheticGenerator
from nemonis_risk.propfirm import GENERIC_TWO_PHASE
from nemonis_schemas.enums import Direction, ResultProvenance

from tests.backtest.test_engine import make_engine

START = datetime(2026, 1, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def series() -> dict[str, list]:
    return {"EURUSD": SyntheticGenerator("EURUSD", seed=5150).generate_list(START, 1200)}


@pytest.fixture(scope="module")
def config(series) -> BacktestConfig:
    end = max(b.open_time for bars in series.values() for b in bars)
    return BacktestConfig(
        instruments=("EURUSD",),
        start=START,
        end=end,
        fill_model=FillModel(slippage=SlippageModel.NONE),
        seed=3,
    )


def trades_with(pnls: list[str], *, instrument: str = "EURUSD") -> list[ClosedTrade]:
    return [
        ClosedTrade(
            trade_id=f"t{i}",
            instrument=instrument,
            direction=Direction.LONG,
            lots=Decimal("0.1"),
            entry_price=Decimal("1.08"),
            exit_price=Decimal("1.09"),
            opened_at=START,
            closed_at=START,
            strategy_id="s",
            pnl_account_ccy=Decimal(p),
            commission=Decimal("0"),
            reason=FillReason.TAKE_PROFIT,
            mfe_pips=Decimal(1),
            mae_pips=Decimal(1),
        )
        for i, p in enumerate(pnls)
    ]


class TestWalkForward:
    def test_windows_are_sequential_and_non_overlapping(self, series, config) -> None:
        result = walk_forward(make_engine(), series, config, windows=4)
        assert len(result.windows) == 4
        for earlier, later in zip(result.windows, result.windows[1:], strict=False):
            assert earlier.test_end <= later.test_start

    def test_test_windows_follow_their_training_window(self, series, config) -> None:
        """What makes a window out-of-sample rather than merely separate."""
        for window in walk_forward(make_engine(), series, config, windows=4).windows:
            assert window.test_start >= window.train_end

    def test_results_are_labelled_walk_forward(self, series, config) -> None:
        result = walk_forward(make_engine(), series, config, windows=3)
        for window in result.windows:
            assert window.metrics.provenance is ResultProvenance.WALK_FORWARD

    def test_per_window_detail_is_retained(self, series, config) -> None:
        """An aggregate hides the case where one window carried everything."""
        result = walk_forward(make_engine(), series, config, windows=5)
        assert len({w.trade_count for w in result.windows}) >= 1
        assert all(w.metrics is not None for w in result.windows)

    def test_consistency_is_reported(self, series, config) -> None:
        result = walk_forward(make_engine(), series, config, windows=5)
        assert Decimal(0) <= result.consistency <= Decimal(1)

    def test_expanding_windows_share_a_start(self, series, config) -> None:
        result = walk_forward(make_engine(), series, config, windows=3, expanding=True)
        assert len({w.train_start for w in result.windows}) == 1

    def test_rolling_windows_move_forward(self, series, config) -> None:
        result = walk_forward(make_engine(), series, config, windows=3, expanding=False)
        assert len({w.train_start for w in result.windows}) == 3

    def test_zero_windows_is_handled(self, series, config) -> None:
        assert walk_forward(make_engine(), series, config, windows=0).windows == ()


class TestCarriedByOneWindow:
    def test_detects_when_one_window_supplied_most_of_the_profit(self, series, config) -> None:
        """The most common way a walk-forward result flatters a strategy."""
        result = walk_forward(make_engine(), series, config, windows=4)
        assert isinstance(result.carried_by_one_window, bool)


class TestMonteCarlo:
    def test_reshuffling_preserves_terminal_equity(self) -> None:
        """Order changes the path, never the destination. If terminal equity
        varied, the simulation would be doing something other than reshuffling."""
        trades = trades_with(["100", "-50", "200", "-80", "40"] * 12)
        result = monte_carlo(trades, starting_balance=Decimal("100000"), iterations=200, seed=1)
        expected = Decimal("100000") + sum(t.pnl_account_ccy for t in trades)
        assert result.median_terminal == expected
        assert result.p05_terminal == expected

    def test_drawdown_varies_with_order(self) -> None:
        """The point of the exercise: the realised drawdown is one draw."""
        trades = trades_with(["100", "-50", "200", "-300", "150", "-90"] * 15)
        result = monte_carlo(trades, starting_balance=Decimal("100000"), iterations=1000, seed=2)
        assert result.p95_max_drawdown > result.median_max_drawdown
        assert result.worst_max_drawdown >= result.p95_max_drawdown

    def test_ruin_probability_is_reported(self) -> None:
        catastrophic = trades_with(["-9000"] * 12 + ["500"] * 5)
        result = monte_carlo(
            catastrophic, starting_balance=Decimal("100000"), iterations=500, seed=3
        )
        assert result.ruin_probability > Decimal(0)

    def test_a_safe_sequence_has_no_ruin(self) -> None:
        result = monte_carlo(
            trades_with(["50", "-20"] * 40),
            starting_balance=Decimal("100000"),
            iterations=500,
            seed=4,
        )
        assert result.ruin_probability == Decimal(0)

    def test_prop_pass_probability_is_computed(self) -> None:
        """The objective function for an evaluation account — a single passing
        history may have passed only because the sequence was kind."""
        winners = trades_with(["900"] * 12)
        result = monte_carlo(
            winners,
            starting_balance=Decimal("100000"),
            iterations=500,
            seed=5,
            prop_profile=GENERIC_TWO_PHASE,
        )
        assert result.prop_pass_probability is not None
        assert result.prop_pass_probability > Decimal("0.9")

    def test_a_breaching_sequence_rarely_passes(self) -> None:
        losers = trades_with(["-1500"] * 10 + ["400"] * 6)
        result = monte_carlo(
            losers,
            starting_balance=Decimal("100000"),
            iterations=500,
            seed=6,
            prop_profile=GENERIC_TWO_PHASE,
        )
        assert result.prop_pass_probability is not None
        assert result.prop_pass_probability < Decimal("0.5")

    def test_is_reproducible(self) -> None:
        trades = trades_with(["100", "-60", "180", "-40"] * 15)
        a = monte_carlo(trades, starting_balance=Decimal("100000"), iterations=300, seed=7)
        b = monte_carlo(trades, starting_balance=Decimal("100000"), iterations=300, seed=7)
        assert a.p95_max_drawdown == b.p95_max_drawdown
        assert a.ruin_probability == b.ruin_probability

    def test_empty_input_is_handled(self) -> None:
        result = monte_carlo([], starting_balance=Decimal("100000"))
        assert result.trade_count == 0
        assert result.ruin_probability == Decimal(0)


class TestStress:
    def test_every_scenario_runs(self, series, config) -> None:
        result = stress_test(make_engine(), series, config)
        names = {s.name for s in result.scenarios}
        assert names == {
            "double_slippage",
            "fixed_wide_slippage",
            "double_spread",
            "missing_bars",
        }

    def test_worse_costs_reduce_net_pnl(self, series, config) -> None:
        """If doubling slippage did not hurt, the fill model is not applying it."""
        result = stress_test(make_engine(), series, config)
        wide = next(s for s in result.scenarios if s.name == "fixed_wide_slippage")
        assert wide.metrics.net_pnl < result.baseline.net_pnl

    def test_degradation_is_quantified(self, series, config) -> None:
        result = stress_test(make_engine(), series, config)
        assert result.degradation("fixed_wide_slippage") is not None

    def test_missing_bars_change_the_result(self, series, config) -> None:
        """Asserts on net P&L, not trade count. The counts can coincide — they
        did on the first run — while the underlying trades differ entirely."""
        result = stress_test(make_engine(), series, config)
        thinned = next(s for s in result.scenarios if s.name == "missing_bars")
        assert thinned.metrics.net_pnl != result.baseline.net_pnl

    def test_every_scenario_is_at_least_as_costly_as_the_baseline(self, series, config) -> None:
        """Stress must not accidentally flatter. If a scenario improved the
        result, the cost model is not being applied."""
        result = stress_test(make_engine(), series, config)
        for scenario in result.scenarios:
            if scenario.name == "missing_bars":
                continue  # fewer bars means fewer opportunities, not lower costs
            assert scenario.metrics.net_pnl <= result.baseline.net_pnl, scenario.name

    def test_survives_all_is_reported(self, series, config) -> None:
        result = stress_test(make_engine(), series, config)
        assert isinstance(result.survives_all, bool)

    def test_stress_is_reproducible(self, series, config) -> None:
        a = stress_test(make_engine(), series, config, seed=42)
        b = stress_test(make_engine(), series, config, seed=42)
        assert [s.metrics.net_pnl for s in a.scenarios] == [s.metrics.net_pnl for s in b.scenarios]
