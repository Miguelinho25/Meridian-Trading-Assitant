#!/usr/bin/env python3
"""Import daily FX history from Yahoo Finance into Ñemonis's CSV format.

    python scripts/import_yahoo.py --start 2010-01-01
    python scripts/import_yahoo.py --pairs EURUSD GBPUSD --start 2020-01-01

Writes one CSV per instrument to ``data/raw/``, readable by ``FileProvider``.

IMPORTANT — what this data is and is not
----------------------------------------
Yahoo publishes **mid prices only**. There is no bid/ask, so spread cannot be
measured, only assumed. This importer writes true mid OHLC columns and leaves the
spread assumption to load time, where ``FileProvider`` requires it explicitly and
flags every derived result with ``spread_assumed=True``.

That matters because spread is a first-order trading cost. A backtest on this data
tells you whether a strategy survives *real price action* — genuine trends, gaps,
holidays, volatility clustering — which synthetic data cannot test. It does not
tell you whether the strategy survives real costs. For that, tick data with true
bid/ask is required.

Yahoo data is for personal research use. Check its terms before relying on it for
anything else.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "raw"

#: Ñemonis symbol -> Yahoo ticker.
YAHOO_TICKERS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCHF": "USDCHF=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCAD": "USDCAD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
}


def fetch(symbol: str, start: str, end: str) -> list[dict[str, object]]:
    """Fetch daily bars and normalise them.

    Timestamps are stamped 00:00 UTC on the session date. Yahoo gives a bare date
    with no time or zone; an FX "day" conventionally runs to 21:00/22:00 UTC, so
    this is an approximation. It is made explicit here rather than left implicit,
    because ``FileProvider`` refuses naive timestamps outright — assuming a zone
    silently could shift a bar across a session or daily-reset boundary.
    """
    import yfinance as yf

    ticker = YAHOO_TICKERS[symbol]
    frame = yf.download(
        ticker, start=start, end=end, interval="1d", progress=False, auto_adjust=False
    )
    if frame is None or frame.empty:
        return []

    # yfinance returns MultiIndex columns for a single ticker; flatten them.
    if hasattr(frame.columns, "nlevels") and frame.columns.nlevels > 1:
        frame.columns = frame.columns.get_level_values(0)

    rows: list[dict[str, object]] = []
    flat = 0

    skipped_nan = 0

    for index, record in frame.iterrows():
        try:
            raw = [float(record[k]) for k in ("Open", "High", "Low", "Close")]
        except (TypeError, ValueError, KeyError):
            continue

        # Real feeds carry NaN for holidays and missing sessions. These must be
        # dropped *before* reaching Decimal: Decimal("NaN") constructs happily,
        # then raises InvalidOperation on the first comparison rather than
        # returning False. Synthetic data never exercises this path.
        if any(math.isnan(v) or math.isinf(v) for v in raw):
            skipped_nan += 1
            continue

        o, h, low, c = (Decimal(str(round(v, 6))) for v in raw)

        if o <= 0 or h <= 0 or low <= 0 or c <= 0:
            continue
        if h < low:
            continue

        # Holidays and half-days appear as bars with no range at all. They are
        # kept — they are real, and the quality assessor should see them — but
        # counted so the operator knows how many are present.
        if o == h == low == c:
            flat += 1

        moment = index.to_pydatetime() if hasattr(index, "to_pydatetime") else index
        rows.append(
            {
                "time": moment.replace(tzinfo=UTC).isoformat(),
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "volume": 0,
            }
        )

    if skipped_nan:
        print(f"[{skipped_nan} NaN rows dropped] ", end="", file=sys.stderr)
    if flat:
        print(f"[{flat} zero-range bars kept] ", end="", file=sys.stderr)

    return rows


def write_csv(rows: list[dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["time", "open", "high", "low", "close", "volume"]
        )
        writer.writeheader()
        writer.writerows(rows)  # type: ignore[arg-type]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", nargs="*", default=list(YAHOO_TICKERS))
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--end", default=datetime.now(UTC).date().isoformat())
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    unknown = [p for p in args.pairs if p not in YAHOO_TICKERS]
    if unknown:
        print(f"Unknown pairs: {', '.join(unknown)}", file=sys.stderr)
        return 2

    print(f"Yahoo Finance daily bars, {args.start} to {args.end}")
    print("MID PRICES ONLY — spread must be assumed at load time.\n")

    total = 0
    failed: list[str] = []

    for symbol in args.pairs:
        print(f"  {symbol} ...", end=" ", flush=True)
        try:
            rows = fetch(symbol, args.start, args.end)
        except Exception as exc:
            print(f"FAILED ({type(exc).__name__}: {exc})")
            failed.append(symbol)
            continue

        if not rows:
            print("no data returned")
            failed.append(symbol)
            continue

        path = args.out / f"{symbol}_D1.csv"
        write_csv(rows, path)
        total += len(rows)
        print(f"{len(rows)} bars -> {path.relative_to(REPO_ROOT)}")

    print(f"\n{total} bars across {len(args.pairs) - len(failed)} instruments.")
    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
    print("\nLoad with:")
    print("  FileProvider.from_csv(path, instrument='EURUSD',")
    print("                        timeframe=Timeframe.D1, spread_pips=Decimal('1.0'))")
    print("Every result from this data carries spread_assumed=True.")
    return 1 if failed and total == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
