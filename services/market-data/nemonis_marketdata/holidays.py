"""FX market holidays.

The global FX market is closed on a small number of days beyond weekends. Those
gaps are market structure, not data faults — counting them as missing bars makes
every genuine multi-year history look defective.

Deliberately minimal: only closures with broad, global effect. National holidays
that shut a single centre (US Thanksgiving, UK bank holidays) leave FX trading
thin but open, so they are *not* listed — a thin market is a risk-engine concern
(session liquidity), not a data-quality one.

Verified against observed gaps in 2010–2026 daily history for the default
watchlist. This is not a substitute for a broker's published calendar; a broker
connection should supply its own.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache
from typing import Final

#: (month, day) closures observed globally every year.
FIXED_CLOSURES: Final[frozenset[tuple[int, int]]] = frozenset(
    {
        (1, 1),  # New Year's Day
        (12, 25),  # Christmas Day
        (12, 26),  # Boxing Day / St Stephen's Day
    }
)


@lru_cache(maxsize=256)
def easter_sunday(year: int) -> date:
    """Gregorian Easter (Meeus/Jones/Butcher algorithm).

    Computed rather than tabulated because Easter moves, and a hard-coded table
    silently stops working the year it runs out.
    """
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month, day = divmod(h + ll - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=256)
def _holidays_for_year(year: int) -> frozenset[date]:
    easter = easter_sunday(year)
    days = {date(year, month, day) for month, day in FIXED_CLOSURES}
    days.add(easter - timedelta(days=2))  # Good Friday
    days.add(easter + timedelta(days=1))  # Easter Monday
    return frozenset(days)


def is_market_holiday(day: date) -> bool:
    """Whether the global FX market is closed for a holiday on ``day``."""
    return day in _holidays_for_year(day.year)


def holidays_between(start: date, end: date) -> frozenset[date]:
    """Every closure in ``[start, end]``."""
    days: set[date] = set()
    for year in range(start.year, end.year + 1):
        days |= {d for d in _holidays_for_year(year) if start <= d <= end}
    return frozenset(days)


def expected_closures(start: date, end: date) -> int:
    """Count of holiday closures in the window, weekends excluded.

    A holiday falling on a Saturday or Sunday costs no trading day, so counting it
    would over-forgive missing bars.
    """
    return sum(1 for d in holidays_between(start, end) if d.weekday() < 5)
