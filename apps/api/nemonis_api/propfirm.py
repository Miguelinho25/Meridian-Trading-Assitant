"""Prop-firm rule profiles.

Read-only. The interesting content is not the headline percentages — every firm
publishes those — but the definitional choices underneath them, which decide
whether an account survives and which are routinely misread:

* ``daily_loss_basis`` EQUITY counts *floating* losses, so an open position that
  is temporarily under water can breach the daily limit without a single trade
  being closed.
* ``daily_loss_reference`` HIGHEST_EQUITY measures from the day's peak, so giving
  back an intraday gain consumes the allowance even while up on the day.
* ``total_loss_type`` TRAILING follows equity upward, so profit permanently
  raises the floor.

Each is returned with the consequence spelled out rather than as a bare enum. A
UI showing "TRAILING" alone tells an operator nothing they can act on.

Verification leads everything. The bundled profile is a clearly-labelled example
with invented numbers, and an unverified rule set is more dangerous than none:
it invites confidence in limits that may not match the firm's actual terms.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from nemonis_risk.propfirm import (
    PROP_PROFILES,
    DailyLossReference,
    DrawdownType,
    LossBasis,
    PropFirmProfile,
)
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/prop-firm", tags=["prop-firm"])

#: Plain-English consequence per definitional choice. Held here rather than in
#: the UI so the API and any future client cannot drift apart on what a rule
#: actually means.
CONSEQUENCES: dict[str, dict[str, str]] = {
    "daily_loss_basis": {
        LossBasis.EQUITY.value: (
            "Floating losses count. An open position that is temporarily under water "
            "can breach the daily limit without any trade being closed."
        ),
        LossBasis.BALANCE.value: (
            "Only closed trades count. An open position under water does not consume "
            "the daily allowance until it is closed."
        ),
    },
    "daily_loss_reference": {
        DailyLossReference.BALANCE_AT_RESET.value: (
            "Measured from the balance at the daily reset. Intraday gains are not "
            "clawed back — being up on the day restores room."
        ),
        DailyLossReference.HIGHEST_EQUITY.value: (
            "Measured from the day's highest equity. Giving back an intraday gain "
            "consumes the allowance even while still up on the day. Stricter."
        ),
    },
    "total_loss_type": {
        DrawdownType.STATIC.value: (
            "The floor is fixed at the starting balance. Profit does not raise it."
        ),
        DrawdownType.TRAILING.value: (
            "The floor follows equity upward. Profit permanently raises the level "
            "you must stay above, so a gain then given back can breach it."
        ),
    },
}


class RuleWithConsequence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_name: str
    value: str
    consequence: str
    #: True where the alternative setting would be more forgiving.
    stricter_option: bool


class Verification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_verified: bool
    source: str
    last_verified_at: str | None
    verified_by: str | None
    verification_age_days: int | None
    is_stale: bool
    warning: str


class ProfileOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str
    name: str
    version: str
    phase: str
    enabled: bool
    starting_balance: str
    account_currency: str
    profit_target_pct: str | None
    max_daily_loss_pct: str
    max_total_loss_pct: str
    reset_time: str
    reset_timezone: str
    trailing_stops_at_initial_balance: bool
    min_trading_days: int
    max_trading_days: int | None
    inactivity_days: int | None
    consistency_rule_enabled: bool
    max_single_day_profit_pct_of_total: str | None
    weekend_holding_allowed: bool
    overnight_holding_allowed: bool
    news_trading_restricted: bool
    news_buffer_minutes: int
    ea_allowed: bool
    instrument_restrictions: list[str]
    buffer_warning_pct: str
    notes: str
    #: The definitional choices, each with its consequence stated.
    definitions: list[RuleWithConsequence]
    verification: Verification


def _verification(profile: PropFirmProfile) -> Verification:
    today = datetime.now(UTC).date()
    stale = profile.is_stale(today)

    if not profile.is_verified:
        warning = (
            "These rules have never been verified against the firm's published terms. "
            "Every value here is an invented example. Trading an evaluation against "
            "unverified limits is more dangerous than trading against none, because "
            "the numbers invite confidence they have not earned."
        )
    elif stale:
        age = profile.verification_age_days(today)
        warning = (
            f"Last verified {age} days ago. Firms change their terms without notice; "
            f"re-check before relying on these limits."
        )
    else:
        warning = ""

    return Verification(
        is_verified=profile.is_verified,
        source=profile.source,
        last_verified_at=profile.last_verified_at.isoformat() if profile.last_verified_at else None,
        verified_by=profile.verified_by,
        verification_age_days=profile.verification_age_days(today),
        is_stale=stale,
        warning=warning,
    )


def _definitions(profile: PropFirmProfile) -> list[RuleWithConsequence]:
    chosen = {
        "daily_loss_basis": profile.daily_loss_basis.value,
        "daily_loss_reference": profile.daily_loss_reference.value,
        "total_loss_type": profile.total_loss_type.value,
    }
    stricter = {
        "daily_loss_basis": LossBasis.EQUITY.value,
        "daily_loss_reference": DailyLossReference.HIGHEST_EQUITY.value,
        "total_loss_type": DrawdownType.TRAILING.value,
    }
    return [
        RuleWithConsequence(
            field_name=name,
            value=value,
            consequence=CONSEQUENCES[name][value],
            stricter_option=value == stricter[name],
        )
        for name, value in chosen.items()
    ]


def _to_out(profile: PropFirmProfile) -> ProfileOut:
    return ProfileOut(
        profile_id=profile.profile_id,
        name=profile.name,
        version=profile.version,
        phase=profile.phase.value,
        enabled=profile.enabled,
        starting_balance=str(profile.starting_balance),
        account_currency=profile.account_currency,
        profit_target_pct=(
            str(profile.profit_target_pct) if profile.profit_target_pct is not None else None
        ),
        max_daily_loss_pct=str(profile.max_daily_loss_pct),
        max_total_loss_pct=str(profile.max_total_loss_pct),
        reset_time=profile.reset_time.strftime("%H:%M"),
        reset_timezone=profile.reset_timezone,
        trailing_stops_at_initial_balance=profile.trailing_stops_at_initial_balance,
        min_trading_days=profile.min_trading_days,
        max_trading_days=profile.max_trading_days,
        inactivity_days=profile.inactivity_days,
        consistency_rule_enabled=profile.consistency_rule_enabled,
        max_single_day_profit_pct_of_total=(
            str(profile.max_single_day_profit_pct_of_total)
            if profile.max_single_day_profit_pct_of_total is not None
            else None
        ),
        weekend_holding_allowed=profile.weekend_holding_allowed,
        overnight_holding_allowed=profile.overnight_holding_allowed,
        news_trading_restricted=profile.news_trading_restricted,
        news_buffer_minutes=profile.news_buffer_minutes,
        ea_allowed=profile.ea_allowed,
        instrument_restrictions=list(profile.instrument_restrictions),
        buffer_warning_pct=str(profile.buffer_warning_pct),
        notes=profile.notes,
        definitions=_definitions(profile),
        verification=_verification(profile),
    )


@router.get("", response_model=list[ProfileOut], summary="Prop-firm rule profiles")
async def index() -> list[ProfileOut]:
    return [_to_out(p) for p in PROP_PROFILES.values()]
