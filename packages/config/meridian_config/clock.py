"""Clock injection — architecture.md §3.

There is no ``datetime.now()`` below the I/O edge. Every core function takes a
``Clock``. This is what makes replay deterministic, makes look-ahead impossible on
the time axis, and makes timezone boundaries testable without waiting for them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


class ClockError(RuntimeError):
    """A clock invariant was violated."""


@runtime_checkable
class Clock(Protocol):
    """Source of the current time. Always timezone-aware UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time. The only clock permitted to read the host clock.

    Guards against backward movement (NTP correction, manual change, suspend).
    A clock that goes backwards can corrupt daily-loss windows and cooldowns, so
    it is treated as an incident rather than tolerated.
    """

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last: datetime | None = None

    def now(self) -> datetime:
        current = datetime.now(UTC)
        if self._last is not None and current < self._last:
            raise ClockError(
                f"System clock moved backwards: {self._last.isoformat()} -> "
                f"{current.isoformat()}. Refusing to proceed."
            )
        self._last = current
        return current


class FrozenClock:
    """A clock that only moves when told to. For tests."""

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ClockError("FrozenClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ClockError("Cannot advance a clock by a negative interval")
        self._now += delta
        return self._now

    def set(self, moment: datetime) -> datetime:
        """Jump to an absolute time. Backward jumps are rejected."""
        if moment.tzinfo is None:
            raise ClockError("FrozenClock requires a timezone-aware datetime")
        target = moment.astimezone(UTC)
        if target < self._now:
            raise ClockError(f"Refusing backward jump: {self._now} -> {target}")
        self._now = target
        return self._now


class ReplayClock:
    """Backtest clock. Advances only as bars are consumed.

    Cannot be advanced to a time beyond the bar under consideration, which is the
    time-axis half of look-ahead prevention (the data-axis half is ``BarView``).
    """

    __slots__ = ("_horizon", "_now")

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ClockError("ReplayClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)
        self._horizon = self._now

    def now(self) -> datetime:
        return self._now

    def admit_bar(self, bar_time: datetime) -> None:
        """Raise the horizon to a newly-ingested bar, then move to it."""
        target = bar_time.astimezone(UTC)
        if target < self._now:
            raise ClockError(f"Bar out of order: {target} precedes {self._now}")
        self._horizon = target
        self._now = target

    @property
    def horizon(self) -> datetime:
        """The latest bar admitted. Nothing may look beyond this."""
        return self._horizon
