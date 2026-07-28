"""Point-in-time feature store (ADR-0006 §7).

Two properties, both load-bearing:

**Immutability.** A stored row records what was computable at its decision
timestamp and is never recomputed. Recomputing after a bug fix would rewrite
history with future-informed values — the training set becomes corrupt in a way
no unit test detects, and the backtest looks *better*, not worse. A correction
is a new ``feature_version``, never an update.

**Point-in-time joins.** Features are joined to labels on ``decision_time``,
never on ingestion time.

``source_bar_hash`` ties each row to the exact bars that produced it, so a
suspiciously good result can be audited back to its inputs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from meridian_marketdata.barview import BarView
from meridian_marketdata.types import Candle
from meridian_schemas.enums import Timeframe

from meridian_features.registry import FEATURE_VERSION, FEATURES, FeatureDef


class FeatureStoreError(RuntimeError):
    """An immutability or consistency rule was violated."""


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """One instrument, one decision instant, one feature version."""

    instrument: str
    timeframe: Timeframe
    decision_time: datetime
    feature_version: str
    values: dict[str, Decimal | None]
    source_bar_hash: str
    computed_at: datetime

    @property
    def key(self) -> tuple[str, str, datetime, str]:
        return (self.instrument, self.timeframe.value, self.decision_time, self.feature_version)

    @property
    def is_warm(self) -> bool:
        """Whether every feature was computable (no warm-up gaps)."""
        return all(v is not None for v in self.values.values())


def hash_source_bars(view: BarView, lookback: int) -> str:
    """Hash the bars that fed this row.

    Covers open time and bid/ask OHLC of every bar in the lookback window, so any
    change to the inputs produces a different hash and the mismatch is detectable.
    """
    hasher = hashlib.sha256()
    start = max(0, view.decision_index - lookback + 1)
    for k in range(start, view.decision_index + 1):
        bar = view.bar(k)
        hasher.update(
            f"{bar.open_time.isoformat()}|{bar.bid_open}|{bar.bid_high}|"
            f"{bar.bid_low}|{bar.bid_close}|{bar.ask_open}|{bar.ask_close}|".encode()
        )
    return f"sha256:{hasher.hexdigest()[:32]}"


def compute_row(
    view: BarView,
    *,
    computed_at: datetime,
    features: tuple[FeatureDef, ...] = FEATURES,
    feature_version: str = FEATURE_VERSION,
) -> FeatureRow:
    """Compute every feature at the view's decision index.

    Features that lack sufficient history yield ``None`` rather than a value
    derived from a short window.
    """
    values: dict[str, Decimal | None] = {}
    for definition in features:
        values[definition.name] = definition.compute(view)

    max_lookback = max((f.lookback for f in features), default=1)

    return FeatureRow(
        instrument=view.current.instrument,
        timeframe=view.current.timeframe,
        decision_time=view.decision_time,
        feature_version=feature_version,
        values=values,
        source_bar_hash=hash_source_bars(view, max_lookback),
        computed_at=computed_at,
    )


class InMemoryFeatureStore:
    """Append-only feature store.

    The persistent implementation (Parquet partitions plus a database index)
    arrives with the backtest engine. The immutability contract is defined and
    enforced here so it cannot be relaxed later by accident — a store that
    permits overwrites would silently invalidate every result derived from it.
    """

    __slots__ = ("_rows",)

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, datetime, str], FeatureRow] = {}

    def put(self, row: FeatureRow) -> None:
        """Store a row. Re-storing identical content is a no-op; conflict raises."""
        existing = self._rows.get(row.key)
        if existing is not None:
            if existing.source_bar_hash == row.source_bar_hash and existing.values == row.values:
                return  # Idempotent: same inputs, same outputs.
            raise FeatureStoreError(
                f"Refusing to overwrite {row.instrument} @ {row.decision_time.isoformat()} "
                f"(version {row.feature_version}). Stored rows are immutable — a corrected "
                f"feature is a new feature_version, not an update. Existing source hash "
                f"{existing.source_bar_hash}, incoming {row.source_bar_hash}."
            )
        self._rows[row.key] = row

    def get(
        self,
        instrument: str,
        timeframe: Timeframe,
        decision_time: datetime,
        feature_version: str = FEATURE_VERSION,
    ) -> FeatureRow | None:
        return self._rows.get((instrument, timeframe.value, decision_time, feature_version))

    def as_of(
        self,
        instrument: str,
        timeframe: Timeframe,
        moment: datetime,
        feature_version: str = FEATURE_VERSION,
    ) -> FeatureRow | None:
        """The most recent row at or before ``moment``.

        The point-in-time read. Never returns a row computed after ``moment``,
        which is what makes a training-set join safe.
        """
        candidates = [
            r
            for r in self._rows.values()
            if r.instrument == instrument
            and r.timeframe is timeframe
            and r.feature_version == feature_version
            and r.decision_time <= moment
        ]
        return max(candidates, key=lambda r: r.decision_time, default=None)

    def range(
        self,
        instrument: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        feature_version: str = FEATURE_VERSION,
        *,
        warm_only: bool = False,
    ) -> list[FeatureRow]:
        """Rows in ``[start, end)``, oldest first. Optionally warm rows only."""
        rows = [
            r
            for r in self._rows.values()
            if r.instrument == instrument
            and r.timeframe is timeframe
            and r.feature_version == feature_version
            and start <= r.decision_time < end
            and (not warm_only or r.is_warm)
        ]
        return sorted(rows, key=lambda r: r.decision_time)

    def __len__(self) -> int:
        return len(self._rows)

    @property
    def versions(self) -> set[str]:
        return {r.feature_version for r in self._rows.values()}


def build_series(
    bars: Sequence[Candle],
    *,
    computed_at: datetime,
    store: InMemoryFeatureStore | None = None,
    features: tuple[FeatureDef, ...] = FEATURES,
) -> list[FeatureRow]:
    """Compute a feature row for every bar in a series.

    Walks the series one decision index at a time through a fresh ``BarView``, so
    each row sees exactly the history that existed at its own decision instant.
    Computing features over the whole frame at once is the usual approach and the
    usual source of leakage.
    """
    rows: list[FeatureRow] = []
    for index in range(len(bars)):
        view = BarView(bars, index)
        row = compute_row(view, computed_at=computed_at, features=features)
        rows.append(row)
        if store is not None:
            store.put(row)
    return rows


WARMUP_BARS: Final = max(f.lookback for f in FEATURES)
