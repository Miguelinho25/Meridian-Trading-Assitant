"""Seeded synthetic FX generator.

Produces realistic *structure* — session-varying volatility, spread widening at
rollover and in thin liquidity, weekend gaps, occasional shocks — without
claiming to reproduce real market dynamics.

**What this is for:** exercising the pipeline end to end with no paid data feed,
and providing deterministic fixtures. It is a test harness.

**What it is not for:** evaluating whether a strategy works. Synthetic data
contains exactly the structure the generator was written to contain, so a
strategy tuned on it is tuned on this module's assumptions. Every backtest run
against it is labelled ``ResultProvenance.SYNTHETIC`` and no profitability claim
may reference it. See backtesting-methodology.md §1.

Determinism: identical seed and parameters produce byte-identical output. The
RNG is seeded per instrument so adding a pair to the watchlist does not change
the series of the others — otherwise every stored fixture would shift whenever
the watchlist changed.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from random import Random

from nemonis_schemas.enums import Timeframe

from nemonis_marketdata.instruments import InstrumentSpec, get_spec
from nemonis_marketdata.sessions import is_rollover, is_weekend, liquidity_factor
from nemonis_marketdata.types import Candle

#: Plausible opening levels. Starting points only, not forecasts.
BASE_PRICES: dict[str, str] = {
    "EURUSD": "1.08500",
    "GBPUSD": "1.26500",
    "USDJPY": "157.200",
    "USDCHF": "0.89500",
    "AUDUSD": "0.65500",
    "NZDUSD": "0.60500",
    "USDCAD": "1.37500",
    "EURGBP": "0.85500",
    "EURJPY": "170.400",
    "GBPJPY": "199.000",
}

#: Annualised volatility, roughly typical for liquid FX.
ANNUAL_VOL: dict[str, str] = {
    "EURUSD": "0.070",
    "GBPUSD": "0.085",
    "USDJPY": "0.095",
    "USDCHF": "0.075",
    "AUDUSD": "0.100",
    "NZDUSD": "0.105",
    "USDCAD": "0.070",
    "EURGBP": "0.060",
    "EURJPY": "0.100",
    "GBPJPY": "0.115",
}

_BARS_PER_YEAR_H1 = 6000  # ~250 trading days × 24h, less weekends


class SyntheticGenerator:
    """Deterministic bar generator for one instrument."""

    def __init__(
        self,
        symbol: str,
        *,
        seed: int,
        timeframe: Timeframe = Timeframe.H1,
        spec: InstrumentSpec | None = None,
    ) -> None:
        self.symbol = symbol
        self.spec = spec or get_spec(symbol)
        self.timeframe = timeframe
        self.seed = seed
        self._rng = Random(self._instrument_seed(symbol, seed))

    @staticmethod
    def _instrument_seed(symbol: str, seed: int) -> int:
        """Derive a per-instrument seed.

        Hashing the symbol into the seed means EURUSD's series does not change
        when GBPJPY is added to the watchlist. Without this, every committed
        fixture would shift on any watchlist edit.
        """
        digest = hashlib.sha256(f"{seed}:{symbol}".encode()).digest()
        return int.from_bytes(digest[:8], "big")

    def _bar_volatility(self, moment: datetime) -> float:
        """Per-bar volatility, scaled by session liquidity."""
        annual = float(ANNUAL_VOL.get(self.symbol, "0.080"))
        bars_per_year = _BARS_PER_YEAR_H1 * (60 / self.timeframe.minutes)
        base = annual / math.sqrt(bars_per_year)
        # Thin liquidity means larger, choppier moves per unit of flow.
        return base * (1.0 + 0.6 * (1.0 - float(liquidity_factor(moment))))

    def _spread_pips(self, moment: datetime) -> Decimal:
        """Spread in pips: widens in thin liquidity, sharply at rollover."""
        typical = float(self.spec.typical_spread_pips)
        liquidity = float(liquidity_factor(moment))
        widened = typical * (1.0 + 1.8 * (1.0 - min(liquidity, 1.0)))
        if is_rollover(moment):
            widened *= 6.0
        jitter = 1.0 + self._rng.uniform(-0.12, 0.30)  # right-skewed, as observed
        return Decimal(str(round(max(typical * 0.5, widened * jitter), 2)))

    def generate(
        self, start: datetime, bars: int, *, shock_probability: float = 0.002
    ) -> Iterator[Candle]:
        """Yield ``bars`` candles from ``start``, skipping weekend closure.

        Weekend bars are not emitted at all, and the reopening bar carries a gap —
        which is what a stop-loss actually meets on a Sunday open, and a common
        thing for naive generators to omit.
        """
        price = float(BASE_PRICES.get(self.symbol, "1.00000"))
        moment = start
        emitted = 0
        step = timedelta(minutes=self.timeframe.minutes)
        pip = float(self.spec.pip_size)
        digits = self.spec.digits
        was_closed = False

        def q(value: float) -> Decimal:
            return Decimal(str(round(value, digits)))

        while emitted < bars:
            if is_weekend(moment):
                moment += step
                was_closed = True
                continue

            vol = self._bar_volatility(moment)

            # Weekend gap: the market reprices while closed.
            if was_closed:
                price *= 1.0 + self._rng.gauss(0.0, vol * 2.5)
                was_closed = False

            # Occasional shock — fat tails are a real feature of FX, and a
            # generator without them flatters every stop-loss assumption.
            if self._rng.random() < shock_probability:
                price *= 1.0 + self._rng.gauss(0.0, vol * 8.0)

            open_price = price
            close_price = open_price * (1.0 + self._rng.gauss(0.0, vol))
            # Wick extents beyond the body.
            high = max(open_price, close_price) * (1.0 + abs(self._rng.gauss(0.0, vol * 0.6)))
            low = min(open_price, close_price) * (1.0 - abs(self._rng.gauss(0.0, vol * 0.6)))

            spread_price = float(self._spread_pips(moment)) * pip

            yield Candle(
                instrument=self.symbol,
                timeframe=self.timeframe,
                open_time=moment,
                bid_open=q(open_price),
                bid_high=q(high),
                bid_low=q(low),
                bid_close=q(close_price),
                ask_open=q(open_price + spread_price),
                ask_high=q(high + spread_price),
                ask_low=q(low + spread_price),
                ask_close=q(close_price + spread_price),
                volume=Decimal(str(round(1000 * float(liquidity_factor(moment)), 2))),
            )

            price = close_price
            moment += step
            emitted += 1

    def generate_list(self, start: datetime, bars: int, **kwargs: float) -> list[Candle]:
        return list(self.generate(start, bars, **kwargs))
