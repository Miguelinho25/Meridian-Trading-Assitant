"""Data-quality assessment — the fail-closed gate (architecture.md §10).

Stale or invalid data must block new orders. This module produces the verdict the
risk engine consumes; it never decides anything about trading itself.

Design rule: **absence of evidence is not evidence of quality.** An empty window,
an unreadable timestamp or an unknown instrument all yield ``INVALID``, not ``OK``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from nemonis_schemas.enums import DataQualityVerdict, Timeframe

from nemonis_marketdata.holidays import is_market_holiday
from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.sessions import is_rollover, is_weekend
from nemonis_marketdata.types import Candle, Quote


@dataclass(frozen=True, slots=True)
class QualityIssue:
    code: str
    detail: str
    #: True if this alone must block trading.
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class QualityReport:
    instrument: str
    verdict: DataQualityVerdict
    #: 0.0 (unusable) to 1.0 (clean). Informational — the verdict is what gates.
    score: Decimal
    issues: tuple[QualityIssue, ...] = field(default_factory=tuple)
    bars_examined: int = 0
    assessed_at: datetime | None = None

    @property
    def blocks_trading(self) -> bool:
        """Whether this data may be traded on **right now**.

        Only OK permits new orders. DEGRADED is deliberately blocking: a
        'mostly fine' live feed is precisely the situation where a wrong fill is
        plausible enough to be missed, and the cost of pausing is a missed trade
        rather than a bad one.
        """
        return self.verdict is not DataQualityVerdict.OK

    @property
    def usable_for_research(self) -> bool:
        """Whether this series may be backtested on.

        A separate question from ``blocks_trading``, and conflating the two was a
        real bug: sixteen years of genuine daily history containing a dozen
        holiday gaps is entirely usable for research, but the live-trading rule
        would reject it outright and permanently.

        Only INVALID blocks here. DEGRADED is acceptable *provided the issues are
        surfaced* — which is why every consumer is expected to display
        ``issues`` alongside any result derived from the series.
        """
        return self.verdict is not DataQualityVerdict.INVALID

    @property
    def blocking_issues(self) -> tuple[QualityIssue, ...]:
        return tuple(i for i in self.issues if i.blocking)


def assess_quote(
    quote: Quote | None,
    *,
    now: datetime,
    max_age_seconds: int,
    spec: InstrumentSpec,
    max_spread_multiple: Decimal = Decimal("5.0"),
) -> QualityReport:
    """Assess a single live quote."""
    if quote is None:
        return QualityReport(
            instrument=spec.symbol,
            verdict=DataQualityVerdict.INVALID,
            score=Decimal(0),
            issues=(QualityIssue("NO_QUOTE", "No quote available for this instrument"),),
            assessed_at=now,
        )

    issues: list[QualityIssue] = []

    age = quote.age_seconds(now)
    if age > max_age_seconds:
        issues.append(
            QualityIssue(
                "MARKET_DATA_STALE",
                f"Quote is {age}s old, limit is {max_age_seconds}s",
            )
        )

    # A source timestamp in the future means clock skew or a corrupt feed.
    # Either way the age calculation is meaningless, so nothing may be trusted.
    if quote.source_time > now + timedelta(seconds=5):
        issues.append(
            QualityIssue(
                "SOURCE_TIME_IN_FUTURE",
                f"Source time {quote.source_time.isoformat()} is ahead of now "
                f"{now.isoformat()} — clock skew or corrupt feed",
            )
        )

    if quote.arrival_time < quote.source_time - timedelta(seconds=5):
        issues.append(
            QualityIssue(
                "ARRIVAL_BEFORE_SOURCE",
                "Quote arrived before it was produced; timestamps are unreliable",
            )
        )

    spread_pips = quote.spread / spec.pip_size
    if spread_pips > spec.typical_spread_pips * max_spread_multiple:
        issues.append(
            QualityIssue(
                "ABNORMAL_SPREAD",
                f"Spread {spread_pips:.1f} pips exceeds {max_spread_multiple}× the "
                f"typical {spec.typical_spread_pips}",
            )
        )
    if quote.spread <= 0:
        issues.append(QualityIssue("ZERO_OR_CROSSED_SPREAD", f"Spread is {quote.spread}"))

    return _finalise(spec.symbol, issues, bars=1, now=now)


def assess_series(
    bars: list[Candle],
    *,
    spec: InstrumentSpec,
    timeframe: Timeframe,
    now: datetime,
    max_spread_multiple: Decimal = Decimal("5.0"),
) -> QualityReport:
    """Assess a historical series for gaps, duplicates and outliers."""
    if not bars:
        return QualityReport(
            instrument=spec.symbol,
            verdict=DataQualityVerdict.INVALID,
            score=Decimal(0),
            issues=(QualityIssue("EMPTY_SERIES", "No bars in the requested window"),),
            assessed_at=now,
        )

    issues: list[QualityIssue] = []
    expected = timedelta(minutes=timeframe.minutes)

    times = [b.open_time for b in bars]
    if times != sorted(times):
        issues.append(QualityIssue("OUT_OF_ORDER", "Bars are not in chronological order"))

    duplicates = len(times) - len(set(times))
    if duplicates:
        issues.append(QualityIssue("DUPLICATE_BARS", f"{duplicates} duplicate timestamps"))

    # Count only gaps the market should have filled. Neither a weekend nor a
    # global holiday is a gap — Easter, Christmas and New Year account for most
    # absences in any genuine multi-year daily history, and counting them would
    # make every real dataset look defective.
    missing = 0
    for previous, current in pairwise(bars):
        delta = current.open_time - previous.open_time
        if delta <= expected:
            continue
        step = previous.open_time + expected
        while step < current.open_time:
            if not is_weekend(step) and not is_market_holiday(step.date()):
                missing += 1
            step += expected
    if missing:
        issues.append(
            QualityIssue(
                "MISSING_BARS",
                f"{missing} expected bars absent within open market hours",
                blocking=missing > len(bars) * 0.01,
            )
        )

    # Rollover bars are excluded: spreads widen sharply at rollover every day, in
    # every real feed. Counting them as a defect would mark every genuine series
    # degraded and, because DEGRADED blocks, would halt trading daily. Rollover is
    # blocked separately by the risk engine's ROLLOVER_BLOCK gate, which is the
    # correct place for it — this function assesses data faults, not market state.
    tradeable = [b for b in bars if not is_rollover(b.open_time)]
    wide = sum(
        1
        for b in tradeable
        if b.spread_close / spec.pip_size > spec.typical_spread_pips * max_spread_multiple
    )
    if wide:
        issues.append(
            QualityIssue(
                "WIDE_SPREADS",
                f"{wide} of {len(tradeable)} non-rollover bars exceed "
                f"{max_spread_multiple}× typical spread",
                blocking=wide > len(tradeable) * 0.05,
            )
        )

    outliers = _count_return_outliers(bars)
    if outliers:
        issues.append(
            QualityIssue(
                "PRICE_OUTLIERS",
                f"{outliers} bars move more than 20× the median bar range",
                blocking=False,  # Real shocks happen; flag for review, do not block history.
            )
        )

    return _finalise(spec.symbol, issues, bars=len(bars), now=now)


def _count_return_outliers(bars: list[Candle]) -> int:
    """Bars whose move dwarfs the typical range — usually a bad tick."""
    if len(bars) < 20:
        return 0
    ranges = sorted((b.bid_high - b.bid_low) for b in bars)
    median = ranges[len(ranges) // 2]
    if median <= 0:
        return 0
    threshold = median * 20
    return sum(1 for b in bars if (b.bid_high - b.bid_low) > threshold)


def _finalise(
    instrument: str, issues: list[QualityIssue], *, bars: int, now: datetime
) -> QualityReport:
    blocking = [i for i in issues if i.blocking]

    if blocking:
        verdict = DataQualityVerdict.INVALID
    elif issues:
        verdict = DataQualityVerdict.DEGRADED
    else:
        verdict = DataQualityVerdict.OK

    penalty = Decimal("0.35") * len(blocking) + Decimal("0.10") * (len(issues) - len(blocking))
    score = max(Decimal(0), Decimal(1) - penalty)

    return QualityReport(
        instrument=instrument,
        verdict=verdict,
        score=score,
        issues=tuple(issues),
        bars_examined=bars,
        assessed_at=now,
    )
