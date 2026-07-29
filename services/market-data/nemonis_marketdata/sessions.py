"""Trading sessions, weekend closure and rollover.

Session boundaries drive liquidity, spread and volatility in the synthetic
generator, and feed the risk engine's session and weekend gates.

All windows are UTC and ignore DST. Real session times shift with London and New
York daylight saving, which matters for precise session-edge strategies — a
Stage E refinement, recorded here so the simplification is not mistaken for
correctness.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Final

from nemonis_schemas.enums import Session

#: (open_hour_utc, close_hour_utc). Sydney wraps midnight.
SESSION_HOURS: Final[dict[Session, tuple[int, int]]] = {
    Session.SYDNEY: (21, 6),
    Session.TOKYO: (23, 8),
    Session.LONDON: (7, 16),
    Session.NEW_YORK: (12, 21),
}

#: Relative liquidity, used to scale synthetic volatility and spread.
SESSION_LIQUIDITY: Final[dict[Session, Decimal]] = {
    Session.LONDON: Decimal("1.00"),
    Session.NEW_YORK: Decimal("0.95"),
    Session.TOKYO: Decimal("0.55"),
    Session.SYDNEY: Decimal("0.35"),
    Session.CLOSED: Decimal("0.15"),
}

#: Daily rollover. Spreads widen sharply and swap is charged.
ROLLOVER_HOUR_UTC: Final = 21
ROLLOVER_WINDOW_MINUTES: Final = 5

#: Triple swap is charged on Wednesday for the weekend value date.
TRIPLE_SWAP_WEEKDAY: Final = 2  # Wednesday


def _in_window(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start < end else hour >= start or hour < end


def active_sessions(moment: datetime) -> tuple[Session, ...]:
    """Every session open at ``moment``. Empty outside all of them."""
    if is_weekend(moment):
        return ()
    hour = moment.hour
    return tuple(s for s, (start, end) in SESSION_HOURS.items() if _in_window(hour, start, end))


def primary_session(moment: datetime) -> Session:
    """The dominant session, for labelling a trade.

    Overlaps resolve to the more liquid side — London during London/New York,
    because that is where the flow is.
    """
    active = active_sessions(moment)
    if not active:
        return Session.CLOSED
    return max(active, key=lambda s: SESSION_LIQUIDITY[s])


def liquidity_factor(moment: datetime) -> Decimal:
    """Relative liquidity in [0.15, ~1.4].

    Overlapping sessions add liquidity rather than replacing it, which is what
    produces the London/New York volatility peak.
    """
    active = active_sessions(moment)
    if not active:
        return SESSION_LIQUIDITY[Session.CLOSED]
    best = max(SESSION_LIQUIDITY[s] for s in active)
    overlap_bonus = Decimal("0.20") * (len(active) - 1)
    return best + overlap_bonus


def is_weekend(moment: datetime) -> bool:
    """Whether the FX market is closed.

    Closes Friday 21:00 UTC, reopens Sunday 21:00 UTC. Not simply "Saturday or
    Sunday" — Friday evening and Sunday evening are the parts that actually catch
    people out, because a naive weekday check leaves them tradeable.
    """
    weekday = moment.weekday()
    if weekday == 5:  # Saturday
        return True
    if weekday == 4 and moment.hour >= 21:  # Friday after close
        return True
    # Sunday before reopen
    return weekday == 6 and moment.hour < 21


def is_rollover(moment: datetime) -> bool:
    """Whether ``moment`` falls in the daily rollover window."""
    return moment.hour == ROLLOVER_HOUR_UTC and moment.minute < ROLLOVER_WINDOW_MINUTES


def swap_multiplier(moment: datetime) -> int:
    """Swap charge multiplier: 3 on the triple-swap weekday, else 1, 0 off-rollover."""
    if not is_rollover(moment):
        return 0
    return 3 if moment.weekday() == TRIPLE_SWAP_WEEKDAY else 1


def next_session_open(moment: datetime) -> datetime:
    """The next instant the market is open. Used to skip weekend gaps."""
    probe = moment.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(24 * 4):
        if not is_weekend(probe):
            return probe
        probe += timedelta(hours=1)
    raise RuntimeError(f"No session open found within 4 days of {moment}")


__all__ = [
    "ROLLOVER_HOUR_UTC",
    "SESSION_HOURS",
    "SESSION_LIQUIDITY",
    "TRIPLE_SWAP_WEEKDAY",
    "active_sessions",
    "is_rollover",
    "is_weekend",
    "liquidity_factor",
    "next_session_open",
    "primary_session",
    "swap_multiplier",
    "time",
]
