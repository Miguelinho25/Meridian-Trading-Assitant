"""Market data: types, look-ahead-safe views, providers and quality assessment."""

from __future__ import annotations

from meridian_marketdata.barview import BarView, LookAheadError
from meridian_marketdata.instruments import (
    CORRELATION_CLUSTERS,
    WATCHLIST,
    InstrumentSpec,
    get_spec,
)
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
    "InstrumentSpec",
    "LookAheadError",
    "MarketDataError",
    "QualityIssue",
    "QualityReport",
    "Quote",
    "SyntheticGenerator",
    "active_sessions",
    "assess_quote",
    "assess_series",
    "get_spec",
    "is_rollover",
    "is_weekend",
    "liquidity_factor",
    "primary_session",
    "swap_multiplier",
]
