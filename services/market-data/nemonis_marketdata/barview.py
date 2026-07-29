"""BarView — structural look-ahead prevention (architecture.md §4).

Documenting "do not use future data" does not prevent using future data. This
makes the mistake raise.

A strategy or feature never receives the full series. It receives a window pinned
to the decision index ``i``:

    series:   [ b0 b1 b2 b3 b4 b5 b6 b7 b8 b9 ]
                            ↑ decision index i=4
    BarView:  [ b0 b1 b2 b3 b4 ]  ─── readable
                              [ b5 … b9 ]  ─── LookAheadError

The subtle part is negative indexing. In a plain list ``bars[-1]`` is the last
bar of the *whole series* — the future. Here it is the bar at the decision index.
That single difference is the most common silent leak in retail backtesting, and
it is why this class exists rather than a slice: a slice would be correct once,
then wrong the moment someone passed the original list somewhere else.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import overload

from nemonis_marketdata.types import Candle


class LookAheadError(IndexError):
    """A read was attempted beyond the decision index.

    Deliberately an ``IndexError`` subclass so it cannot be swallowed by a bare
    ``except IndexError`` that was written to handle short series — the caller
    has to notice.
    """


class BarView(Sequence[Candle]):
    """A read-only window over bars, bounded at the decision index.

    ``len(view)`` is the number of *visible* bars, so ordinary loops and slices
    behave as though the future does not exist. Reaching past the boundary by
    explicit index raises.
    """

    __slots__ = ("_bars", "_i")

    def __init__(self, bars: Sequence[Candle], decision_index: int) -> None:
        if decision_index < 0:
            raise ValueError(f"decision_index must be non-negative, got {decision_index}")
        if decision_index >= len(bars):
            raise ValueError(
                f"decision_index {decision_index} is outside a series of {len(bars)} bars"
            )
        self._bars = bars
        self._i = decision_index

    # --- Sequence protocol -------------------------------------------------

    def __len__(self) -> int:
        """Number of visible bars — indices 0..decision_index inclusive."""
        return self._i + 1

    @overload
    def __getitem__(self, index: int) -> Candle: ...

    @overload
    def __getitem__(self, index: slice) -> list[Candle]: ...

    def __getitem__(self, index: int | slice) -> Candle | list[Candle]:
        """Overloaded so ``view[i]`` is a Candle and ``view[a:b]`` is a list.

        Without the overloads every caller gets a union back and has to narrow it,
        which pushes isinstance noise into feature code that should read cleanly.
        """
        if isinstance(index, slice):
            return [self.bar(k) for k in range(*index.indices(len(self)))]
        return self.bar(index)

    def bar(self, index: int) -> Candle:
        """Bounded single-bar access. The bounds check lives here, once."""
        resolved = index + len(self) if index < 0 else index

        if resolved > self._i:
            raise LookAheadError(
                f"Attempted to read bar {resolved} at decision index {self._i}. "
                f"This is future data. If a feature needs a longer history, extend "
                f"the lookback; never widen the view."
            )
        if resolved < 0:
            raise IndexError(
                f"Index {index} reaches before the start of the series ({len(self)} bars visible)"
            )
        return self._bars[resolved]

    def __iter__(self) -> Iterator[Candle]:
        for k in range(len(self)):
            yield self._bars[k]

    # --- Decision-point accessors -----------------------------------------

    @property
    def decision_index(self) -> int:
        return self._i

    @property
    def current(self) -> Candle:
        """The bar being decided on."""
        return self._bars[self._i]

    @property
    def decision_time(self) -> datetime:
        """The instant a decision is being made.

        The *open* of the current bar, not its close. A strategy deciding at bar
        ``i`` knows the bar has opened; it does not yet know where it closes.
        Using the close to trade the close is the classic fantasy fill.
        """
        return self._bars[self._i].open_time

    def last_closed(self) -> Candle:
        """The most recent fully-closed bar.

        The safest thing for a feature to use. Raises if the decision is on the
        very first bar, because there is no closed history yet.
        """
        if self._i == 0:
            raise LookAheadError(
                "No closed bar exists at decision index 0 — a feature needing "
                "closed history must declare a lookback of at least 1."
            )
        return self._bars[self._i - 1]

    def closes(self, count: int, *, side: str = "bid") -> list[Decimal]:
        """The last ``count`` closes up to and including the current bar.

        Oldest first. Raises if fewer than ``count`` bars are visible, rather than
        silently returning a short series — a moving average quietly computed over
        3 bars instead of 20 is a bug that produces plausible numbers.
        """
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        if count > len(self):
            raise LookAheadError(
                f"Requested {count} closes but only {len(self)} bars are visible at "
                f"decision index {self._i}. Wait for sufficient history rather than "
                f"computing over a short window."
            )
        attr = "bid_close" if side == "bid" else "ask_close"
        start = self._i - count + 1
        return [getattr(self._bars[k], attr) for k in range(start, self._i + 1)]

    def has_history(self, bars: int) -> bool:
        """Whether ``bars`` bars of history are available. Never raises."""
        return len(self) >= bars

    def __repr__(self) -> str:
        return f"<BarView i={self._i} visible={len(self)} of {len(self._bars)}>"
