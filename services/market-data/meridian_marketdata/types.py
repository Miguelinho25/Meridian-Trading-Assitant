"""Market-data value types.

Bid and ask are modelled separately throughout. There is no "price" field and no
mid-price convenience accessor on ``Candle`` — a single price is the assumption
that makes a backtest look better than reality, and the cheapest way to prevent
it is to make it unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from meridian_schemas.enums import Timeframe


class MarketDataError(RuntimeError):
    """Market data was malformed or unusable."""


@dataclass(frozen=True, slots=True)
class Candle:
    """One bar, with separate bid and ask OHLC.

    Frozen because a bar that has been ingested is history. Mutating one after
    the fact is how a backtest silently stops matching what was recorded.
    """

    instrument: str
    timeframe: Timeframe
    #: Bar open time (the interval is [open_time, open_time + timeframe)).
    open_time: datetime

    bid_open: Decimal
    bid_high: Decimal
    bid_low: Decimal
    bid_close: Decimal

    ask_open: Decimal
    ask_high: Decimal
    ask_low: Decimal
    ask_close: Decimal

    volume: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.open_time.tzinfo is None:
            raise MarketDataError(f"{self.instrument} bar has a naive timestamp")
        if self.bid_high < self.bid_low or self.ask_high < self.ask_low:
            raise MarketDataError(f"{self.instrument} @ {self.open_time}: high is below low")
        # Ask below bid is a crossed quote — always a data fault, never a market.
        if self.ask_close < self.bid_close or self.ask_open < self.bid_open:
            raise MarketDataError(
                f"{self.instrument} @ {self.open_time}: crossed quote "
                f"(ask {self.ask_close} < bid {self.bid_close})"
            )
        if self.bid_low <= 0 or self.ask_low <= 0:
            raise MarketDataError(f"{self.instrument} @ {self.open_time}: non-positive price")

    @property
    def spread_close(self) -> Decimal:
        return self.ask_close - self.bid_close

    @property
    def close_time(self) -> datetime:
        from datetime import timedelta

        return self.open_time + timedelta(minutes=self.timeframe.minutes)


@dataclass(frozen=True, slots=True)
class Quote:
    """A point-in-time bid/ask observation.

    Carries the three timestamps that make staleness measurable and look-ahead
    auditable (data-model.md §1).
    """

    instrument: str
    bid: Decimal
    ask: Decimal
    #: When the venue says it happened.
    source_time: datetime
    #: When we received it.
    arrival_time: datetime

    def __post_init__(self) -> None:
        if self.ask < self.bid:
            raise MarketDataError(
                f"{self.instrument}: crossed quote, ask {self.ask} < bid {self.bid}"
            )
        if self.bid <= 0:
            raise MarketDataError(f"{self.instrument}: non-positive bid {self.bid}")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    def age_seconds(self, now: datetime) -> Decimal:
        """How stale this quote is at ``now``. Never negative."""
        delta = (now - self.source_time).total_seconds()
        return Decimal(str(max(0.0, delta)))
