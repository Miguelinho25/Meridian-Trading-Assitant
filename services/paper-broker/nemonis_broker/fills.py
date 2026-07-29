"""Fill model (architecture.md §6).

The assumptions here decide whether a backtest means anything. Every one is
pessimistic where the truth is unknowable, and every one is stated rather than
implied.

Three rules carry most of the weight:

* **Decisions on bar i fill on bar i+1.** The one-bar delay is not configurable
  downward. Removing it is the difference between a plausible backtest and a
  fantasy, because deciding on a bar's close and filling at that same close is
  information you did not have.
* **Bid/ask, never mid.** Buys pay the ask, sells receive the bid. A mid-price
  fill silently refunds half the spread on every trade.
* **Stop-loss wins an ambiguous bar.** When both the stop and the target fall
  inside one bar, the path is unknowable, so the adverse outcome is assumed. The
  count of such bars is reported, so a reader knows how often it mattered.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from random import Random

from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.types import Candle
from nemonis_schemas.enums import Direction, OrderType
from nemonis_schemas.money import quantise_price


class SlippageModel(StrEnum):
    NONE = "NONE"
    FIXED = "FIXED"
    PROPORTIONAL_TO_SPREAD = "PROPORTIONAL_TO_SPREAD"
    STOCHASTIC = "STOCHASTIC"


class FillReason(StrEnum):
    MARKET = "MARKET"
    LIMIT_TOUCHED = "LIMIT_TOUCHED"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    GAP_THROUGH = "GAP_THROUGH"


@dataclass(frozen=True, slots=True)
class FillModel:
    slippage: SlippageModel = SlippageModel.PROPORTIONAL_TO_SPREAD
    fixed_slippage_pips: Decimal = Decimal("0.5")
    #: Fraction of the spread lost to slippage under the proportional model.
    spread_fraction: Decimal = Decimal("0.25")
    #: Multiplier on slippage when a gap carries price through a level.
    gap_penalty: Decimal = Decimal("2.0")

    def slippage_price(
        self, spec: InstrumentSpec, bar: Candle, rng: Random | None = None
    ) -> Decimal:
        """Adverse price movement on execution, in price units. Never negative."""
        if self.slippage is SlippageModel.NONE:
            return Decimal(0)
        if self.slippage is SlippageModel.FIXED:
            return self.fixed_slippage_pips * spec.pip_size
        if self.slippage is SlippageModel.PROPORTIONAL_TO_SPREAD:
            return bar.spread_close * self.spread_fraction
        # Stochastic: right-skewed, because slippage is occasionally much worse
        # than typical and never better than zero.
        if rng is None:
            raise ValueError("STOCHASTIC slippage requires a seeded Random for determinism")
        draw = abs(rng.gauss(0.0, 1.0)) * float(self.spread_fraction)
        return bar.spread_close * Decimal(str(round(draw, 6)))


@dataclass(frozen=True, slots=True)
class FillResult:
    filled: bool
    price: Decimal | None = None
    reason: FillReason | None = None
    slippage: Decimal = Decimal(0)
    #: True when both stop and target were reachable in the same bar and the
    #: adverse outcome was assumed.
    ambiguous_bar: bool = False
    detail: str = ""


def fill_market(
    *,
    spec: InstrumentSpec,
    direction: Direction,
    bar: Candle,
    model: FillModel,
    rng: Random | None = None,
) -> FillResult:
    """Fill a market order on the *next* bar's open.

    Buys pay the ask and sells receive the bid. Slippage always moves against the
    order.
    """
    slip = model.slippage_price(spec, bar, rng)
    price = bar.ask_open + slip if direction is Direction.LONG else bar.bid_open - slip
    return FillResult(
        filled=True,
        price=quantise_price(price, spec.digits),
        reason=FillReason.MARKET,
        slippage=slip,
    )


def fill_limit(
    *,
    spec: InstrumentSpec,
    direction: Direction,
    limit_price: Decimal,
    bar: Candle,
    model: FillModel,
) -> FillResult:
    """Fill a limit order only if price traded **through** the level.

    Merely touching the level is not enough. Assuming a fill on a touch credits
    the best possible outcome at the exact extreme of a bar, which is the single
    most flattering assumption available to a backtest.

    No slippage: a limit order fills at its price or better, never worse.
    """
    if direction is Direction.LONG:
        # Buy limit sits below price; the ask must trade below it.
        traded_through = bar.ask_low < limit_price
    else:
        traded_through = bar.bid_high > limit_price

    if not traded_through:
        return FillResult(filled=False, detail="Price did not trade through the limit")

    return FillResult(
        filled=True,
        price=quantise_price(limit_price, spec.digits),
        reason=FillReason.LIMIT_TOUCHED,
    )


def fill_stop(
    *,
    spec: InstrumentSpec,
    direction: Direction,
    stop_price: Decimal,
    bar: Candle,
    model: FillModel,
    rng: Random | None = None,
) -> FillResult:
    """Fill a stop order, honouring gaps.

    A stop becomes a market order once touched, so it fills at the *worse* of the
    trigger price and where the market actually was. On a gap, that is the bar's
    open — which is what a stop-loss actually meets on a Sunday reopening, and a
    common thing for naive models to ignore in the trader's favour.
    """
    slip = model.slippage_price(spec, bar, rng)

    if direction is Direction.LONG:  # buy stop, above price
        if bar.ask_high < stop_price:
            return FillResult(filled=False, detail="Stop not reached")
        gapped = bar.ask_open > stop_price
        base = bar.ask_open if gapped else stop_price
        price = base + slip * (model.gap_penalty if gapped else Decimal(1))
    else:  # sell stop, below price
        if bar.bid_low > stop_price:
            return FillResult(filled=False, detail="Stop not reached")
        gapped = bar.bid_open < stop_price
        base = bar.bid_open if gapped else stop_price
        price = base - slip * (model.gap_penalty if gapped else Decimal(1))

    return FillResult(
        filled=True,
        price=quantise_price(price, spec.digits),
        reason=FillReason.GAP_THROUGH if gapped else FillReason.STOP_TRIGGERED,
        slippage=slip,
        detail="Gapped through the stop" if gapped else "",
    )


def resolve_exit(
    *,
    spec: InstrumentSpec,
    direction: Direction,
    stop_loss: Decimal | None,
    take_profit: Decimal | None,
    bar: Candle,
    model: FillModel,
    rng: Random | None = None,
) -> FillResult:
    """Resolve an open position's exit for one bar.

    When both levels are reachable within the same bar, the stop-loss is assumed
    to have been hit first. Intrabar path is unknowable from OHLC, and assuming
    the favourable ordering would inflate every result. The flag is set so the
    frequency is reportable rather than buried.
    """
    if direction is Direction.LONG:
        stop_hit = stop_loss is not None and bar.bid_low <= stop_loss
        target_hit = take_profit is not None and bar.bid_high >= take_profit
    else:
        stop_hit = stop_loss is not None and bar.ask_high >= stop_loss
        target_hit = take_profit is not None and bar.ask_low <= take_profit

    if not stop_hit and not target_hit:
        return FillResult(filled=False, detail="Neither level reached")

    ambiguous = stop_hit and target_hit

    if stop_hit:
        assert stop_loss is not None
        exit_direction = Direction.SHORT if direction is Direction.LONG else Direction.LONG
        result = fill_stop(
            spec=spec,
            direction=exit_direction,
            stop_price=stop_loss,
            bar=bar,
            model=model,
            rng=rng,
        )
        # The exit is triggered by definition; if the stop-order geometry says
        # otherwise, fall back to the stop price itself rather than not exiting.
        price = result.price if result.filled else quantise_price(stop_loss, spec.digits)
        return FillResult(
            filled=True,
            price=price,
            reason=FillReason.STOP_LOSS,
            slippage=result.slippage,
            ambiguous_bar=ambiguous,
            detail=(
                "Stop and target both reachable in this bar; stop assumed first"
                if ambiguous
                else result.detail
            ),
        )

    assert take_profit is not None
    return FillResult(
        filled=True,
        price=quantise_price(take_profit, spec.digits),
        reason=FillReason.TAKE_PROFIT,
    )


def commission_for(spec: InstrumentSpec, lots: Decimal) -> Decimal:
    """Round-turn commission, charged in full on entry.

    Charging the whole cost up front is the conservative treatment: it cannot
    flatter a trade that closes early.
    """
    return spec.commission_per_lot * lots


def fill_for_order(
    *,
    order_type: OrderType,
    spec: InstrumentSpec,
    direction: Direction,
    bar: Candle,
    model: FillModel,
    limit_price: Decimal | None = None,
    stop_price: Decimal | None = None,
    rng: Random | None = None,
) -> FillResult:
    """Dispatch to the right fill rule for an order type."""
    if order_type is OrderType.MARKET:
        return fill_market(spec=spec, direction=direction, bar=bar, model=model, rng=rng)
    if order_type is OrderType.LIMIT:
        if limit_price is None:
            raise ValueError("LIMIT order requires a limit price")
        return fill_limit(
            spec=spec, direction=direction, limit_price=limit_price, bar=bar, model=model
        )
    if order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
        if stop_price is None:
            raise ValueError(f"{order_type.value} order requires a stop price")
        return fill_stop(
            spec=spec,
            direction=direction,
            stop_price=stop_price,
            bar=bar,
            model=model,
            rng=rng,
        )
    raise ValueError(f"Unsupported order type {order_type}")
