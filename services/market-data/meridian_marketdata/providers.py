"""Concrete market-data providers: synthetic, replay and file import.

All three satisfy the same interface, so a strategy cannot behave differently
between backtest, replay and paper trading.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from meridian_schemas.enums import Timeframe

from meridian_marketdata.instruments import InstrumentSpec, get_spec
from meridian_marketdata.provider import MarketDataProvider
from meridian_marketdata.synthetic import SyntheticGenerator
from meridian_marketdata.types import Candle, MarketDataError, Quote


class SyntheticProvider(MarketDataProvider):
    """Generates bars on demand from a seed.

    Results are memoised per (instrument, timeframe, start) so repeated calls in
    one run return identical objects — a provider that regenerated slightly
    different data on each call would silently break replay determinism.
    """

    name = "synthetic"
    is_synthetic = True

    def __init__(self, *, seed: int, anchor: datetime | None = None) -> None:
        self.seed = seed
        #: Generation origin. All windows are computed as offsets from here, so
        #: the same bar has the same value regardless of the window requested.
        self.anchor = anchor or datetime(
            2026, 1, 5, 0, 0, tzinfo=datetime.now().astimezone().tzinfo
        )
        self._cache: dict[tuple[str, str], list[Candle]] = {}

    def _series(self, instrument: str, timeframe: Timeframe, upto: datetime) -> list[Candle]:
        key = (instrument, timeframe.value)
        cached = self._cache.get(key)
        needed = int((upto - self.anchor).total_seconds() / 60 / timeframe.minutes) + 2

        if cached is not None and len(cached) >= needed:
            return cached

        generator = SyntheticGenerator(instrument, seed=self.seed, timeframe=timeframe)
        series = generator.generate_list(self.anchor, max(needed, 256))
        self._cache[key] = series
        return series

    def candles(
        self, instrument: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        if end <= start:
            return []
        series = self._series(instrument, timeframe, end)
        return [c for c in series if start <= c.open_time < end]

    def latest_quote(self, instrument: str, *, now: datetime) -> Quote | None:
        series = self._series(instrument, Timeframe.M15, now)
        prior = [c for c in series if c.open_time <= now]
        if not prior:
            return None
        bar = prior[-1]
        return Quote(
            instrument=instrument,
            bid=bar.bid_close,
            ask=bar.ask_close,
            source_time=bar.open_time,
            arrival_time=bar.open_time,
        )


class ReplayProvider(MarketDataProvider):
    """Deterministic playback of a fixed series.

    Enforces the replay contract: a caller may only see bars at or before the
    current playback position. Requesting beyond it returns nothing rather than
    revealing the future, so a bug in an event loop cannot leak data through the
    provider — the loop's own guard is not the only line of defence.
    """

    name = "replay"

    def __init__(
        self,
        series: dict[str, list[Candle]],
        *,
        timeframe: Timeframe = Timeframe.H1,
        is_synthetic: bool = True,
    ) -> None:
        self.timeframe = timeframe
        self.is_synthetic = is_synthetic
        self._series = {k: sorted(v, key=lambda c: c.open_time) for k, v in series.items()}
        self._times = {k: [c.open_time for c in v] for k, v in self._series.items()}
        self._position: datetime | None = None

    @classmethod
    def from_generator(
        cls,
        instruments: list[str],
        *,
        seed: int,
        start: datetime,
        bars: int,
        timeframe: Timeframe = Timeframe.H1,
    ) -> ReplayProvider:
        return cls(
            {
                symbol: SyntheticGenerator(symbol, seed=seed, timeframe=timeframe).generate_list(
                    start, bars
                )
                for symbol in instruments
            },
            timeframe=timeframe,
        )

    @property
    def position(self) -> datetime | None:
        """Current playback position. Nothing after this is visible."""
        return self._position

    def seek(self, moment: datetime) -> None:
        """Move playback to ``moment``. Rewinding is refused."""
        if self._position is not None and moment < self._position:
            raise MarketDataError(
                f"Refusing to rewind replay from {self._position.isoformat()} to "
                f"{moment.isoformat()}. Replay moves forward only; construct a new "
                f"provider to start again."
            )
        self._position = moment

    def advance(self, steps: int = 1) -> datetime | None:
        """Move forward by whole bars. Returns the new position."""
        if self._position is None:
            first = min((t[0] for t in self._times.values() if t), default=None)
            self._position = first
            return self._position
        self._position += timedelta(minutes=self.timeframe.minutes * steps)
        return self._position

    def _visible(self, instrument: str) -> list[Candle]:
        series = self._series.get(instrument, [])
        if self._position is None:
            return []
        cut = bisect_right(self._times[instrument], self._position)
        return series[:cut]

    def candles(
        self, instrument: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        if timeframe is not self.timeframe:
            raise MarketDataError(
                f"Replay holds {self.timeframe.value} bars; {timeframe.value} was requested. "
                f"Resampling would change the fill model — load the intended timeframe."
            )
        # Clamped to the playback position, never beyond it.
        return [c for c in self._visible(instrument) if start <= c.open_time < end]

    def latest_quote(self, instrument: str, *, now: datetime) -> Quote | None:
        visible = [c for c in self._visible(instrument) if c.open_time <= now]
        if not visible:
            return None
        bar = visible[-1]
        return Quote(
            instrument=instrument,
            bid=bar.bid_close,
            ask=bar.ask_close,
            source_time=bar.open_time,
            arrival_time=bar.open_time,
        )

    def instruments(self) -> list[str]:
        return sorted(self._series)


class FileProvider(MarketDataProvider):
    """Historical bars loaded from CSV or Parquet.

    Expected columns (case-insensitive), one row per bar:

        time, bid_open, bid_high, bid_low, bid_close,
              ask_open, ask_high, ask_low, ask_close [, volume]

    Mid-only sources are also accepted via ``spread_pips``, which synthesises a
    symmetric bid/ask around the mid. That is an assumption, not data — it is
    recorded on the provider so any result derived from it can be identified,
    and ``is_synthetic`` stays False only when true bid/ask columns were present.
    """

    name = "file"

    def __init__(
        self,
        series: dict[str, list[Candle]],
        *,
        timeframe: Timeframe,
        source: str,
        spread_assumed: bool = False,
    ) -> None:
        self._series = {k: sorted(v, key=lambda c: c.open_time) for k, v in series.items()}
        self.timeframe = timeframe
        self.source = source
        self.spread_assumed = spread_assumed
        self.is_synthetic = False
        self.name = f"file:{source}"

    @classmethod
    def from_csv(
        cls,
        path: Path | str,
        *,
        instrument: str,
        timeframe: Timeframe,
        spread_pips: Decimal | None = None,
    ) -> FileProvider:
        path = Path(path)
        if not path.exists():
            raise MarketDataError(f"No such data file: {path}")

        spec = get_spec(instrument)
        bars: list[Candle] = []

        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise MarketDataError(f"{path} has no header row")
            columns = {name.lower().strip(): name for name in reader.fieldnames}

            has_bid_ask = "bid_close" in columns and "ask_close" in columns
            if not has_bid_ask and spread_pips is None:
                raise MarketDataError(
                    f"{path} has no bid/ask columns. Either supply true bid/ask data or "
                    f"pass spread_pips to synthesise a spread — but be aware that a "
                    f"synthesised spread is an assumption, and backtest costs will only "
                    f"be as realistic as that assumption."
                )

            for line, row in enumerate(reader, start=2):
                try:
                    bars.append(
                        _row_to_candle(
                            row, columns, instrument, timeframe, spec, spread_pips, has_bid_ask
                        )
                    )
                # InvalidOperation subclasses ArithmeticError, not ValueError, so
                # it is listed explicitly — otherwise a malformed number escapes
                # as a bare decimal error with no file or line to work from.
                except (ValueError, KeyError, InvalidOperation, MarketDataError) as exc:
                    raise MarketDataError(f"{path}:{line}: {exc}") from exc

        if not bars:
            raise MarketDataError(f"{path} contained no rows")

        return cls(
            {instrument: bars},
            timeframe=timeframe,
            source=path.name,
            spread_assumed=not has_bid_ask,
        )

    @classmethod
    def from_parquet(
        cls,
        path: Path | str,
        *,
        instrument: str,
        timeframe: Timeframe,
        spread_pips: Decimal | None = None,
    ) -> FileProvider:
        try:
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise MarketDataError(
                "Parquet support needs pyarrow: pip install -e '.[quant]'"
            ) from exc

        table = pq.read_table(str(path))
        rows = table.to_pylist()
        if not rows:
            raise MarketDataError(f"{path} contained no rows")

        spec = get_spec(instrument)
        columns = {k.lower(): k for k in rows[0]}
        has_bid_ask = "bid_close" in columns and "ask_close" in columns
        if not has_bid_ask and spread_pips is None:
            raise MarketDataError(f"{path} has no bid/ask columns and no spread_pips supplied")

        bars = []
        for index, row in enumerate(rows, start=1):
            try:
                bars.append(
                    _row_to_candle(
                        row, columns, instrument, timeframe, spec, spread_pips, has_bid_ask
                    )
                )
            except (ValueError, KeyError, InvalidOperation, MarketDataError) as exc:
                raise MarketDataError(f"{path} row {index}: {exc}") from exc
        return cls(
            {instrument: bars},
            timeframe=timeframe,
            source=Path(path).name,
            spread_assumed=not has_bid_ask,
        )

    def candles(
        self, instrument: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        if timeframe is not self.timeframe:
            raise MarketDataError(
                f"This file holds {self.timeframe.value} bars, {timeframe.value} requested"
            )
        return [c for c in self._series.get(instrument, []) if start <= c.open_time < end]

    def latest_quote(self, instrument: str, *, now: datetime) -> Quote | None:
        prior = [c for c in self._series.get(instrument, []) if c.open_time <= now]
        if not prior:
            return None
        bar = prior[-1]
        return Quote(
            instrument=instrument,
            bid=bar.bid_close,
            ask=bar.ask_close,
            source_time=bar.open_time,
            arrival_time=bar.open_time,
        )

    def instruments(self) -> list[str]:
        return sorted(self._series)


def _parse_time(raw: object) -> datetime:
    """Parse a timestamp, requiring timezone information.

    Takes ``object`` because CSV and Parquet rows are untyped at the boundary;
    everything is validated here rather than trusted.

    A naive timestamp in market data is ambiguous by an amount that can move a bar
    across a session or daily-reset boundary, so it is rejected rather than assumed
    to be UTC.
    """
    if isinstance(raw, datetime):
        moment = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        raise MarketDataError(
            f"Timestamp {raw!r} has no timezone. Add an offset (e.g. '+00:00') — "
            f"assuming UTC could shift bars across session and daily-reset boundaries."
        )
    return moment


def _row_to_candle(
    row: dict[str, object],
    columns: dict[str, str],
    instrument: str,
    timeframe: Timeframe,
    spec: InstrumentSpec,
    spread_pips: Decimal | None,
    has_bid_ask: bool,
) -> Candle:
    def value(name: str) -> Decimal:
        return Decimal(str(row[columns[name]]))

    time_key = columns.get("time") or columns.get("timestamp") or columns.get("date")
    if time_key is None:
        raise MarketDataError("No time/timestamp/date column")
    open_time = _parse_time(row[time_key])

    if has_bid_ask:
        return Candle(
            instrument=instrument,
            timeframe=timeframe,
            open_time=open_time,
            bid_open=value("bid_open"),
            bid_high=value("bid_high"),
            bid_low=value("bid_low"),
            bid_close=value("bid_close"),
            ask_open=value("ask_open"),
            ask_high=value("ask_high"),
            ask_low=value("ask_low"),
            ask_close=value("ask_close"),
            volume=value("volume") if "volume" in columns else Decimal(0),
        )

    assert spread_pips is not None
    half = (spread_pips * spec.pip_size) / 2
    o, h, low, c = value("open"), value("high"), value("low"), value("close")
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        open_time=open_time,
        bid_open=o - half,
        bid_high=h - half,
        bid_low=low - half,
        bid_close=c - half,
        ask_open=o + half,
        ask_high=h + half,
        ask_low=low + half,
        ask_close=c + half,
        volume=value("volume") if "volume" in columns else Decimal(0),
    )
