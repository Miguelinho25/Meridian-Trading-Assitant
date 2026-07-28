"""Market data: types, look-ahead-safe views, providers and quality assessment."""

from __future__ import annotations

from meridian_marketdata.barview import BarView, LookAheadError
from meridian_marketdata.holidays import easter_sunday, holidays_between, is_market_holiday
from meridian_marketdata.instruments import (
    CORRELATION_CLUSTERS,
    WATCHLIST,
    InstrumentSpec,
    get_spec,
)
from meridian_marketdata.provider import MarketDataProvider, MarketStatus
from meridian_marketdata.providers import FileProvider, ReplayProvider, SyntheticProvider
from meridian_marketdata.quality import (
    QualityIssue,
    QualityReport,
    assess_quote,
    assess_series,
)
from meridian_marketdata.sessions import (
    active_sessions,
    is_rollover,
    is_weekend,
    liquidity_factor,
    primary_session,
    swap_multiplier,
)
from meridian_marketdata.synthetic import SyntheticGenerator
from meridian_marketdata.types import Candle, MarketDataError, Quote

__all__ = [
    "CORRELATION_CLUSTERS",
    "WATCHLIST",
    "BarView",
    "Candle",
    "FileProvider",
    "InstrumentSpec",
    "LookAheadError",
    "MarketDataError",
    "MarketDataProvider",
    "MarketStatus",
    "QualityIssue",
    "QualityReport",
    "Quote",
    "ReplayProvider",
    "SyntheticGenerator",
    "SyntheticProvider",
    "active_sessions",
    "assess_quote",
    "assess_series",
    "easter_sunday",
    "get_spec",
    "holidays_between",
    "is_market_holiday",
    "is_rollover",
    "is_weekend",
    "liquidity_factor",
    "primary_session",
    "swap_multiplier",
]
