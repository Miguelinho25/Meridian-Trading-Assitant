"""Limit tiers and monotone composition (risk-engine.md §2, invariant I2).

Limits arrive from four tiers:

    SYSTEM  →  ACCOUNT  →  PROFILE  →  STRATEGY/INSTRUMENT  →  EFFECTIVE

and compose **by tightening only**. No profile, strategy, prompt, UI control or
API call can loosen a limit set at a higher tier.

The subtlety is that "tighter" is not always "smaller". A larger news buffer is
stricter; a smaller loss-streak threshold is stricter. Getting one direction
backwards would silently loosen a limit while looking like a tightening, so each
field declares its direction explicitly and a property test asserts the result is
never looser than any contributing tier.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from typing import Final


class Tighten(StrEnum):
    """Which direction makes a limit stricter."""

    LOWER = "LOWER"  # ceiling: compose with min()
    HIGHER = "HIGHER"  # floor:   compose with max()


#: Direction per field. Every field of ``LimitSet`` must appear here; a startup
#: check enforces it, so adding a limit without declaring its direction fails
#: loudly rather than composing wrongly.
TIGHTEN_DIRECTION: Final[dict[str, Tighten]] = {
    # Ceilings — smaller is stricter.
    "risk_per_trade_pct": Tighten.LOWER,
    "daily_risk_budget_pct": Tighten.LOWER,
    "max_open_risk_pct": Tighten.LOWER,
    "max_instrument_exposure_pct": Tighten.LOWER,
    "max_currency_exposure_pct": Tighten.LOWER,
    "max_correlated_exposure_pct": Tighten.LOWER,
    "max_strategy_budget_pct": Tighten.LOWER,
    "max_margin_utilisation_pct": Tighten.LOWER,
    "max_positions": Tighten.LOWER,
    "max_trades_per_session": Tighten.LOWER,
    "max_slippage_pips": Tighten.LOWER,
    "loss_streak_cooldown_after": Tighten.LOWER,
    "max_stop_atr_multiple": Tighten.LOWER,
    # Floors — larger is stricter.
    "min_reward_risk": Tighten.HIGHER,
    "min_confidence": Tighten.HIGHER,
    "news_buffer_minutes": Tighten.HIGHER,
    "min_stop_atr_multiple": Tighten.HIGHER,
}


@dataclass(frozen=True, slots=True)
class LimitSet:
    """One tier's limits. ``None`` means "this tier expresses no opinion"."""

    risk_per_trade_pct: Decimal | None = None
    daily_risk_budget_pct: Decimal | None = None
    max_open_risk_pct: Decimal | None = None
    max_instrument_exposure_pct: Decimal | None = None
    max_currency_exposure_pct: Decimal | None = None
    max_correlated_exposure_pct: Decimal | None = None
    max_strategy_budget_pct: Decimal | None = None
    max_margin_utilisation_pct: Decimal | None = None
    max_positions: int | None = None
    max_trades_per_session: int | None = None
    max_slippage_pips: Decimal | None = None
    loss_streak_cooldown_after: int | None = None
    max_stop_atr_multiple: Decimal | None = None
    min_reward_risk: Decimal | None = None
    min_confidence: Decimal | None = None
    news_buffer_minutes: int | None = None
    min_stop_atr_multiple: Decimal | None = None

    def is_tighter_or_equal(self, other: LimitSet) -> bool:
        """Whether every limit here is at least as strict as ``other``'s."""
        for field_name, direction in TIGHTEN_DIRECTION.items():
            mine = getattr(self, field_name)
            theirs = getattr(other, field_name)
            if theirs is None:
                continue
            if mine is None:
                return False  # other constrains something we do not
            if direction is Tighten.LOWER and mine > theirs:
                return False
            if direction is Tighten.HIGHER and mine < theirs:
                return False
        return True


def _validate_coverage() -> None:
    """Every field must declare a tightening direction.

    Run at import. Adding a limit and forgetting its direction would otherwise
    silently exclude it from composition, leaving it uncomposed at whatever the
    lowest tier set — precisely the loosening I2 forbids.
    """
    declared = set(TIGHTEN_DIRECTION)
    actual = {f.name for f in fields(LimitSet)}
    missing = actual - declared
    if missing:
        raise RuntimeError(
            f"LimitSet fields without a tightening direction: {sorted(missing)}. "
            f"Add them to TIGHTEN_DIRECTION — an undeclared field is excluded from "
            f"composition and would not be tightened by higher tiers."
        )
    extra = declared - actual
    if extra:
        raise RuntimeError(f"TIGHTEN_DIRECTION names fields that do not exist: {sorted(extra)}")


_validate_coverage()


def compose(*tiers: LimitSet) -> LimitSet:
    """Compose tiers by tightening only (I2).

    Order is irrelevant — min and max are commutative and associative — which is
    itself worth knowing: the guarantee does not depend on anyone remembering to
    pass the tiers in the right sequence.
    """
    result: dict[str, object] = {}

    for field_name, direction in TIGHTEN_DIRECTION.items():
        values = [v for v in (getattr(t, field_name) for t in tiers) if v is not None]
        if not values:
            result[field_name] = None
        elif direction is Tighten.LOWER:
            result[field_name] = min(values)
        else:
            result[field_name] = max(values)

    return LimitSet(**result)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LimitOrigin:
    """One effective limit and where it came from.

    Composition answers *what* the limit is; the operator also needs *why*. A
    Risk Lab that shows only the effective number cannot show that a tier
    tightened it, which is the property the whole tier system exists to provide.
    """

    field_name: str
    value: Decimal | int | None
    direction: Tighten
    #: Tiers holding the winning value. More than one means a tie, not a conflict.
    bound_by: tuple[str, ...]
    #: Every tier's value, in the order supplied. Shows what was superseded.
    tier_values: tuple[tuple[str, Decimal | int | None], ...]

    @property
    def is_unset(self) -> bool:
        """No tier expressed an opinion. ``require`` rejects rather than default."""
        return self.value is None

    @property
    def was_tightened(self) -> bool:
        """Whether any tier held a looser value that this one overrode."""
        return sum(1 for _, v in self.tier_values if v is not None) > len(self.bound_by)


def explain(**tiers: LimitSet) -> tuple[LimitOrigin, ...]:
    """Compose, and report which tier bound each limit.

    Keyword-only so every tier is named and the provenance is legible.

    This must never disagree with :func:`compose`. A UI displaying a limit the
    engine does not enforce would be worse than one displaying nothing, so a
    property test asserts the two agree for every field on arbitrary inputs.
    """
    named = tuple(tiers.items())
    origins: list[LimitOrigin] = []

    for field_name, direction in TIGHTEN_DIRECTION.items():
        pairs = tuple((name, getattr(tier, field_name)) for name, tier in named)
        present = [(name, v) for name, v in pairs if v is not None]

        if not present:
            winner: Decimal | int | None = None
            bound_by: tuple[str, ...] = ()
        else:
            pick = min if direction is Tighten.LOWER else max
            winner = pick(v for _, v in present)
            bound_by = tuple(name for name, v in present if v == winner)

        origins.append(
            LimitOrigin(
                field_name=field_name,
                value=winner,
                direction=direction,
                bound_by=bound_by,
                tier_values=pairs,
            )
        )

    return tuple(origins)


def require(limits: LimitSet, field_name: str) -> Decimal | int:
    """Read a limit that must be present.

    Fail-closed (I7): an absent limit means no tier expressed an opinion, and the
    engine must not invent one. Rejecting is the only safe response.
    """
    value = getattr(limits, field_name)
    if value is None:
        raise ValueError(
            f"No tier defines {field_name!r}. The risk engine will not substitute a "
            f"default for a missing limit — set it in the system tier."
        )
    return value  # type: ignore[no-any-return]
