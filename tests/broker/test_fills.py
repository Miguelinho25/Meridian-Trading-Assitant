"""Fill realism. Every optimistic assumption here inflates every backtest."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from random import Random

import pytest
from nemonis_broker.fills import (
    FillModel,
    FillReason,
    SlippageModel,
    commission_for,
    fill_limit,
    fill_market,
    fill_stop,
    resolve_exit,
)
from nemonis_marketdata.instruments import get_spec
from nemonis_marketdata.types import Candle
from nemonis_schemas.enums import Direction, Timeframe

EURUSD = get_spec("EURUSD")
T = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


def bar(bid_o="1.0850", bid_h="1.0870", bid_l="1.0830", bid_c="1.0860", spread="0.0001") -> Candle:
    s = Decimal(spread)
    return Candle(
        instrument="EURUSD",
        timeframe=Timeframe.H1,
        open_time=T,
        bid_open=Decimal(bid_o),
        bid_high=Decimal(bid_h),
        bid_low=Decimal(bid_l),
        bid_close=Decimal(bid_c),
        ask_open=Decimal(bid_o) + s,
        ask_high=Decimal(bid_h) + s,
        ask_low=Decimal(bid_l) + s,
        ask_close=Decimal(bid_c) + s,
    )


NO_SLIP = FillModel(slippage=SlippageModel.NONE)


class TestBidAskNotMid:
    def test_buy_pays_the_ask(self) -> None:
        """A mid-price fill silently refunds half the spread on every trade."""
        b = bar()
        result = fill_market(spec=EURUSD, direction=Direction.LONG, bar=b, model=NO_SLIP)
        assert result.price == b.ask_open
        assert result.price > b.bid_open

    def test_sell_receives_the_bid(self) -> None:
        b = bar()
        result = fill_market(spec=EURUSD, direction=Direction.SHORT, bar=b, model=NO_SLIP)
        assert result.price == b.bid_open
        assert result.price < b.ask_open

    def test_the_spread_is_a_real_cost(self) -> None:
        b = bar()
        buy = fill_market(spec=EURUSD, direction=Direction.LONG, bar=b, model=NO_SLIP)
        sell = fill_market(spec=EURUSD, direction=Direction.SHORT, bar=b, model=NO_SLIP)
        assert buy.price - sell.price == b.ask_open - b.bid_open


class TestSlippageAlwaysHurts:
    @pytest.mark.parametrize(
        "model",
        [
            FillModel(slippage=SlippageModel.FIXED),
            FillModel(slippage=SlippageModel.PROPORTIONAL_TO_SPREAD),
        ],
    )
    def test_buy_slips_upward(self, model: FillModel) -> None:
        b = bar()
        result = fill_market(spec=EURUSD, direction=Direction.LONG, bar=b, model=model)
        assert result.price > b.ask_open

    @pytest.mark.parametrize(
        "model",
        [
            FillModel(slippage=SlippageModel.FIXED),
            FillModel(slippage=SlippageModel.PROPORTIONAL_TO_SPREAD),
        ],
    )
    def test_sell_slips_downward(self, model: FillModel) -> None:
        b = bar()
        result = fill_market(spec=EURUSD, direction=Direction.SHORT, bar=b, model=model)
        assert result.price < b.bid_open

    def test_stochastic_slippage_is_seeded(self) -> None:
        """An unseeded RNG would break replay determinism."""
        model = FillModel(slippage=SlippageModel.STOCHASTIC)
        a = fill_market(
            spec=EURUSD, direction=Direction.LONG, bar=bar(), model=model, rng=Random(42)
        )
        b = fill_market(
            spec=EURUSD, direction=Direction.LONG, bar=bar(), model=model, rng=Random(42)
        )
        assert a.price == b.price

    def test_stochastic_without_a_seed_is_refused(self) -> None:
        model = FillModel(slippage=SlippageModel.STOCHASTIC)
        with pytest.raises(ValueError, match="seeded Random"):
            fill_market(spec=EURUSD, direction=Direction.LONG, bar=bar(), model=model)


class TestLimitRequiresTradingThrough:
    def test_a_mere_touch_does_not_fill(self) -> None:
        """Filling on a touch credits the best possible outcome at the exact
        extreme of a bar — the most flattering assumption available."""
        b = bar(bid_l="1.0830")
        touch = b.ask_low  # exactly at the limit
        result = fill_limit(
            spec=EURUSD, direction=Direction.LONG, limit_price=touch, bar=b, model=NO_SLIP
        )
        assert not result.filled

    def test_trading_through_fills(self) -> None:
        b = bar(bid_l="1.0830")
        above = b.ask_low + Decimal("0.0005")
        result = fill_limit(
            spec=EURUSD, direction=Direction.LONG, limit_price=above, bar=b, model=NO_SLIP
        )
        assert result.filled
        assert result.price == above

    def test_limit_never_slips(self) -> None:
        """A limit fills at its price or better, never worse."""
        b = bar(bid_l="1.0800")
        limit = Decimal("1.0840")
        result = fill_limit(
            spec=EURUSD,
            direction=Direction.LONG,
            limit_price=limit,
            bar=b,
            model=FillModel(slippage=SlippageModel.FIXED),
        )
        assert result.price == limit
        assert result.slippage == Decimal(0)


class TestStopsHonourGaps:
    def test_stop_not_reached_does_not_fill(self) -> None:
        b = bar(bid_h="1.0870")
        result = fill_stop(
            spec=EURUSD,
            direction=Direction.LONG,
            stop_price=Decimal("1.0900"),
            bar=b,
            model=NO_SLIP,
        )
        assert not result.filled

    def test_normal_trigger_fills_at_the_stop(self) -> None:
        b = bar(bid_o="1.0850", bid_h="1.0900")
        result = fill_stop(
            spec=EURUSD,
            direction=Direction.LONG,
            stop_price=Decimal("1.0880"),
            bar=b,
            model=NO_SLIP,
        )
        assert result.filled
        assert result.reason is FillReason.STOP_TRIGGERED

    def test_a_gap_fills_worse_than_the_stop(self) -> None:
        """What a stop-loss actually meets on a Sunday reopening."""
        gapped = bar(bid_o="1.0700", bid_h="1.0750", bid_l="1.0690", bid_c="1.0720")
        result = fill_stop(
            spec=EURUSD,
            direction=Direction.SHORT,
            stop_price=Decimal("1.0800"),
            bar=gapped,
            model=NO_SLIP,
        )
        assert result.filled
        assert result.reason is FillReason.GAP_THROUGH
        assert result.price < Decimal("1.0800")
        assert result.price == gapped.bid_open


class TestAmbiguousBarFavoursTheStop:
    def test_stop_wins_when_both_are_reachable(self) -> None:
        """Intrabar path is unknowable from OHLC. Assuming the favourable
        ordering would inflate every result."""
        wide = bar(bid_o="1.0850", bid_h="1.0950", bid_l="1.0750", bid_c="1.0860")
        result = resolve_exit(
            spec=EURUSD,
            direction=Direction.LONG,
            stop_loss=Decimal("1.0800"),
            take_profit=Decimal("1.0900"),
            bar=wide,
            model=NO_SLIP,
        )
        assert result.filled
        assert result.reason is FillReason.STOP_LOSS
        assert result.ambiguous_bar

    def test_ambiguity_is_reported_not_buried(self) -> None:
        wide = bar(bid_o="1.0850", bid_h="1.0950", bid_l="1.0750")
        result = resolve_exit(
            spec=EURUSD,
            direction=Direction.LONG,
            stop_loss=Decimal("1.0800"),
            take_profit=Decimal("1.0900"),
            bar=wide,
            model=NO_SLIP,
        )
        assert "stop assumed first" in result.detail

    def test_unambiguous_target_is_taken(self) -> None:
        up = bar(bid_o="1.0850", bid_h="1.0950", bid_l="1.0845", bid_c="1.0940")
        result = resolve_exit(
            spec=EURUSD,
            direction=Direction.LONG,
            stop_loss=Decimal("1.0800"),
            take_profit=Decimal("1.0900"),
            bar=up,
            model=NO_SLIP,
        )
        assert result.reason is FillReason.TAKE_PROFIT
        assert not result.ambiguous_bar

    def test_neither_level_reached(self) -> None:
        quiet = bar(bid_o="1.0850", bid_h="1.0860", bid_l="1.0840")
        result = resolve_exit(
            spec=EURUSD,
            direction=Direction.LONG,
            stop_loss=Decimal("1.0800"),
            take_profit=Decimal("1.0900"),
            bar=quiet,
            model=NO_SLIP,
        )
        assert not result.filled

    def test_short_position_exits_mirror_correctly(self) -> None:
        b = bar(bid_o="1.0850", bid_h="1.0950", bid_l="1.0750")
        result = resolve_exit(
            spec=EURUSD,
            direction=Direction.SHORT,
            stop_loss=Decimal("1.0900"),
            take_profit=Decimal("1.0800"),
            bar=b,
            model=NO_SLIP,
        )
        assert result.reason is FillReason.STOP_LOSS


class TestCommission:
    def test_charged_per_lot(self) -> None:
        assert commission_for(EURUSD, Decimal("2")) == EURUSD.commission_per_lot * 2

    def test_round_turn_charged_in_full_on_entry(self) -> None:
        """Conservative: it cannot flatter a trade that closes early."""
        assert commission_for(EURUSD, Decimal("1")) == EURUSD.commission_per_lot
