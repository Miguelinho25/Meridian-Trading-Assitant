"""Dialect-neutral column types (data-model.md §4).

The decimal type is the important one: SQLite has no NUMERIC, and storing money as
REAL would reintroduce exactly the float error the domain layer refuses. Text
encoding preserves the value exactly on both backends.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, String, TypeDecorator


class DecimalText(TypeDecorator[Decimal]):
    """Exact decimal storage on any backend.

    Postgres gets NUMERIC(28,10); SQLite gets zero-padded text that sorts and
    compares correctly. Always returns ``Decimal``, never ``float``.
    """

    impl = String
    cache_ok = True

    _SCALE = 10
    _WIDTH = 18  # integer digits before the point

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import NUMERIC

            return dialect.type_descriptor(NUMERIC(28, self._SCALE))
        return dialect.type_descriptor(String(40))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            raise TypeError(
                f"Refusing to store float {value!r} in a decimal column — pass a Decimal or str."
            )
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        if dialect.name == "postgresql":
            return value
        # Zero-pad so lexical ordering matches numeric ordering, and keep the
        # sign leading so negatives compare correctly against positives.
        quantised = value.quantize(Decimal(1).scaleb(-self._SCALE))
        sign = "-" if quantised < 0 else "0"
        digits = f"{abs(quantised):0{self._WIDTH + self._SCALE + 1}.{self._SCALE}f}"
        return f"{sign}{digits}"

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        text = str(value)
        if dialect.name != "postgresql" and text and text[0] in {"0", "-"}:
            sign, body = text[0], text[1:]
            return Decimal(body) * (-1 if sign == "-" else 1)
        return Decimal(text)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetimes on any backend.

    SQLite drops tzinfo silently, which turns a UTC timestamp into a naive one and
    quietly corrupts any daily-reset or session-boundary comparison. This
    re-attaches UTC on read and rejects naive values on write.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime(timezone=True))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"Expected datetime, got {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError(
                f"Refusing to store naive datetime {value!r} — all timestamps must be "
                f"timezone-aware UTC (data-model.md §1)."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        moment: datetime = value
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
