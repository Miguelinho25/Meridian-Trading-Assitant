"""Market-data provider interface.

One interface for synthetic generation, historical replay and file import — and,
later, a real vendor feed. Nothing downstream may know which is in use, because
a strategy that behaves differently in backtest and paper trading is worthless as
evidence about either.

Providers report data quality; they never decide anything about trading. The risk
engine consumes the verdict.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from nemonis_schemas.enums import Session, Timeframe

from nemonis_marketdata.instruments import InstrumentSpec, get_spec
from nemonis_marketdata.quality import QualityReport, assess_series
from nemonis_marketdata.sessions import is_weekend, primary_session
from nemonis_marketdata.types import Candle, Quote


@dataclass(frozen=True, slots=True)
class MarketStatus:
    instrument: str
    is_open: bool
    session: Session
    as_of: datetime

    @property
    def blocks_trading(self) -> bool:
        return not self.is_open


class MarketDataProvider(ABC):
    """Source of bars, quotes and market status.

    ``name`` appears in audit records and backtest lineage, so a stored result can
    always name the feed that produced it.
    """

    name: str = "abstract"
    #: True when data is generated rather than observed. Propagates to
    #: ResultProvenance so synthetic performance can never be read as real.
    is_synthetic: bool = True

    @abstractmethod
    def candles(
        self,
        instrument: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Bars in ``[start, end)``, oldest first."""

    @abstractmethod
    def latest_quote(self, instrument: str, *, now: datetime) -> Quote | None:
        """Most recent quote at or before ``now``, or None if unavailable.

        Returning None rather than raising is deliberate: absence is a normal
        state that the quality gate converts into a trading block.
        """

    def spec(self, instrument: str) -> InstrumentSpec:
        return get_spec(instrument)

    def market_status(self, instrument: str, *, now: datetime) -> MarketStatus:
        return MarketStatus(
            instrument=instrument,
            is_open=not is_weekend(now),
            session=primary_session(now),
            as_of=now,
        )

    def quality(
        self,
        instrument: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        now: datetime,
    ) -> QualityReport:
        """Assess the series this provider would return for the window."""
        return assess_series(
            self.candles(instrument, timeframe, start, end),
            spec=self.spec(instrument),
            timeframe=timeframe,
            now=now,
        )

    def instruments(self) -> list[str]:
        from nemonis_marketdata.instruments import WATCHLIST

        return sorted(WATCHLIST)

    def economic_events(self, start: datetime, end: datetime) -> list[dict[str, object]]:
        """Economic calendar.

        Empty by default. A provider without a calendar must return nothing rather
        than claim a clear window — the risk engine's news gate treats an empty
        calendar as "unknown", not "safe".
        """
        return []

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} synthetic={self.is_synthetic}>"
