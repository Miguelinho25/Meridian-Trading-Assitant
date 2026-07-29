"""The paper session.

The headline test is the equivalence one: fed the same bars, a paper session must
produce the same trades as a backtest. If it does not, every validation statistic
describes a system that no longer runs, and the divergence would surface only as
live results quietly failing to match a validated backtest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from nemonis_backtest import BacktestConfig, BacktestEngine
from nemonis_config.settings import ApprovalMode, Mode, RiskProfileName
from nemonis_marketdata import SyntheticGenerator
from nemonis_marketdata.instruments import WATCHLIST
from nemonis_paper import PaperSession, SessionRefusedError
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
SYMBOLS = ("EURUSD", "GBPUSD")


@pytest.fixture
def series() -> dict[str, list]:
    return {s: SyntheticGenerator(s, seed=2024).generate_list(START, 400) for s in SYMBOLS}


def a_registry() -> StrategyRegistry:
    registry = StrategyRegistry()
    for factory in (MovingAverageTrend, VolatilityBreakout):
        registry.register(factory(), status=LifecycleStatus.CANDIDATE)
    return registry


def a_session(*, kill_switch=lambda: False, mode: Mode = Mode.PAPER) -> PaperSession:
    return PaperSession(
        session_id="ps_test",
        registry=a_registry(),
        specs=dict(WATCHLIST),
        rates=RATES,
        starting_balance=Decimal("100000"),
        risk_profile=RiskProfileName.CHALLENGE,
        mode=mode,
        approval_mode=ApprovalMode.AUTO_PAPER_FULL,
        kill_switch=kill_switch,
        prop_profile=GENERIC_TWO_PHASE,
    )


def a_config(series: dict[str, list]) -> BacktestConfig:
    return BacktestConfig(
        instruments=SYMBOLS,
        start=START,
        end=max(b[-1].open_time for b in series.values()),
        starting_balance=Decimal("100000"),
        provenance=ResultProvenance.IN_SAMPLE,
    )


def timeline(series: dict[str, list], config: BacktestConfig) -> list:
    """Bars grouped by instant, exactly as the backtest aligns them."""
    by_time: dict = {}
    for symbol, bars in series.items():
        for bar in bars:
            if config.start <= bar.open_time < config.end:
                by_time.setdefault(bar.open_time, {})[symbol] = bar
    return [(moment, by_time[moment]) for moment in sorted(by_time)]


def run_backtest(series: dict[str, list], config: BacktestConfig):
    engine = BacktestEngine(
        registry=a_registry(),
        specs=dict(WATCHLIST),
        rates=RATES,
        prop_profile=GENERIC_TWO_PHASE,
    )
    return engine.run(series, config)


class TestItMatchesTheBacktest:
    """The property the shared DecisionCycle exists to guarantee."""

    def test_the_same_bars_produce_the_same_trades(self, series: dict[str, list]) -> None:
        config = a_config(series)
        expected = run_backtest(series, config)

        session = a_session()
        for moment, bars in timeline(series, config):
            session.tick(bars, at=moment)

        assert session.closed_trades, "no trades — the fixture proves nothing"
        assert len(session.closed_trades) == len(expected.trades), (
            f"paper produced {len(session.closed_trades)} trades, backtest "
            f"{len(expected.trades)}. The drivers have diverged."
        )

        for live, replayed in zip(session.closed_trades, expected.trades, strict=True):
            assert live.instrument == replayed.instrument
            assert live.direction == replayed.direction
            assert live.opened_at == replayed.opened_at
            assert live.entry_price == replayed.entry_price
            assert live.exit_price == replayed.exit_price
            assert live.pnl_account_ccy == replayed.pnl_account_ccy

    def test_final_balances_agree(self, series: dict[str, list]) -> None:
        config = a_config(series)
        expected = run_backtest(series, config)

        session = a_session()
        for moment, bars in timeline(series, config):
            session.tick(bars, at=moment)

        assert session.account.balance == expected.final_balance


class TestModeGuard:
    """No broker adapter exists, so no mode implying one may start a session."""

    def test_broker_mode_is_refused(self) -> None:
        """BROKER is the only mode implying real execution. It is rejected at
        startup by configuration as well, but a mode check living in one place is
        one edit away from not existing."""
        with pytest.raises(SessionRefusedError, match="broker adapter"):
            a_session(mode=Mode.BROKER)

    def test_paper_mode_is_permitted(self) -> None:
        assert a_session(mode=Mode.PAPER).mode is Mode.PAPER

    def test_every_mode_is_classified(self) -> None:
        """A mode added later must be deliberately permitted or refused, never
        silently allowed by falling outside both sets."""
        from nemonis_paper import PERMITTED_MODES

        refused = set(Mode) - PERMITTED_MODES
        assert refused == {Mode.BROKER}, (
            f"unclassified modes: {refused - {Mode.BROKER}}. Add each to "
            f"PERMITTED_MODES or confirm it should be refused."
        )


class TestTheKillSwitchStopsTradesNotSettlement:
    """A switch that skipped the tick would leave open positions unmarked and
    their stops unhonoured — abandoning the exposure it was pulled to contain."""

    def test_it_is_read_every_tick_not_at_construction(self) -> None:
        engaged = {"value": False}
        session = a_session(kill_switch=lambda: engaged["value"])
        assert not session.kill_switch_engaged
        engaged["value"] = True
        assert session.kill_switch_engaged, (
            "the session captured the switch at construction; engaging it would "
            "only take effect on restart"
        )

    def test_settlement_still_happens_while_engaged(self, series: dict[str, list]) -> None:
        engaged = {"value": False}
        session = a_session(kill_switch=lambda: engaged["value"])
        steps = timeline(series, a_config(series))

        for moment, bars in steps[:250]:
            session.tick(bars, at=moment)

        engaged["value"] = True
        moment, bars = steps[250]
        outcome = session.tick(bars, at=moment)

        assert outcome.acted, "the tick was skipped; positions would go unmanaged"
        assert "settled" in outcome.reason
        assert outcome.equity > 0, "equity was not marked while the switch was on"

    def test_no_new_trade_is_submitted_while_engaged(self, series: dict[str, list]) -> None:
        session = a_session(kill_switch=lambda: True)
        submitted = sum(
            session.tick(bars, at=moment).submitted
            for moment, bars in timeline(series, a_config(series))
        )
        assert submitted == 0, f"{submitted} orders submitted with the kill switch engaged"


class TestStaleAndRepeatedBars:
    def test_a_repeated_bar_is_ignored(self, series: dict[str, list]) -> None:
        """A feed re-sending its last bar on reconnect must not settle it twice."""
        session = a_session()
        first = {s: series[s][0] for s in SYMBOLS}
        assert session.ingest(first)
        assert session.ingest(first) == {}

    def test_an_out_of_order_bar_is_ignored(self, series: dict[str, list]) -> None:
        session = a_session()
        session.ingest({s: series[s][5] for s in SYMBOLS})
        assert session.ingest({s: series[s][2] for s in SYMBOLS}) == {}

    def test_a_tick_with_no_fresh_bar_does_nothing(self, series: dict[str, list]) -> None:
        """The previous bar is not reused: that would fabricate a price the
        market never printed."""
        session = a_session()
        bars = {s: series[s][0] for s in SYMBOLS}
        session.tick(bars, at=START)
        again = session.tick(bars, at=START)
        assert again.blocked
        assert "never printed" in again.reason

    def test_history_is_bounded(self, series: dict[str, list]) -> None:
        """An unbounded window would grow without limit in a long-running process."""
        session = a_session()
        session.max_history_bars = 60
        for i in range(200):
            session.ingest({"EURUSD": series["EURUSD"][i]})
        assert len(session.history["EURUSD"]) == 60
