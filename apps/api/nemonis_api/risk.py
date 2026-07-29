"""Risk configuration endpoints.

Read-only, and that is not a temporary limitation. The risk engine has final
authority (risk-engine.md, invariant I5); an HTTP route that could loosen a limit
would be precisely the override the architecture forbids, so no write path
exists here to be secured later.

Every number crosses the wire as a string. JavaScript has one numeric type and it
is a float — serialising 0.35 as a JSON number invites a UI to redisplay it as
0.34999999999999998, and a risk limit is the last place to accept that.
"""

from __future__ import annotations

from fastapi import APIRouter
from nemonis_config import get_settings
from nemonis_risk import LimitSet, explain
from nemonis_risk.profiles import PROFILES, SYSTEM_LIMITS, get_profile, profile_allows_mode
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/risk", tags=["risk"])

#: Tier order, loosest authority last. Mirrors the SYSTEM → ACCOUNT → PROFILE →
#: STRATEGY chain in risk-engine.md §2. Naming them here keeps the wire format
#: stable if the engine's internal ordering ever changes.
TIER_ORDER = ("system", "account", "profile", "strategy")


class TierValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: str
    #: None means the tier expressed no opinion on this limit.
    value: str | None


class EffectiveLimit(BaseModel):
    """One limit, its effective value, and where that value came from."""

    model_config = ConfigDict(extra="forbid")
    field_name: str
    value: str | None
    #: LOWER for ceilings, HIGHER for floors. Which way is stricter is not
    #: guessable from the number alone — a bigger news buffer is tighter.
    tightens: str
    bound_by: list[str]
    tier_values: list[TierValue]
    #: True when some tier held a looser value that this one overrode. The
    #: visible evidence that tiers only ever tighten.
    was_tightened: bool
    #: True when no tier set it. The engine rejects rather than substituting a
    #: default, so this is a blocking condition, not a blank field.
    unset: bool


class LimitsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    risk_profile: str
    profile_description: str
    mode: str
    profile_allows_mode: bool
    limits: list[EffectiveLimit]
    notice: str


class ThrottleBandOut(BaseModel):
    """One band of the drawdown-response curve.

    Size is not the only thing that tightens as drawdown deepens: the confidence
    and reward:risk floors rise too, so the system becomes more selective as well
    as smaller. Showing only the multiplier would understate the response.
    """

    model_config = ConfigDict(extra="forbid")
    from_consumed: str
    to_consumed: str
    risk_multiplier: str
    confidence_uplift: str
    reward_risk_uplift: str


class ProfileSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str
    recommended: bool
    allowed_modes: list[str]
    active: bool


def _str(value: object | None) -> str | None:
    return None if value is None else str(value)


@router.get("/limits", response_model=LimitsResponse, summary="Effective limits and provenance")
async def effective_limits() -> LimitsResponse:
    """The limits actually in force, with the tier that bound each one.

    The account and strategy tiers are empty here: neither a funded account nor a
    per-strategy override exists in this build. They are returned as declared
    tiers holding no opinion rather than omitted, so the composition the operator
    sees is the same four-tier one the engine performs.
    """
    settings = get_settings()
    profile = get_profile(settings.risk_profile)

    origins = explain(
        system=SYSTEM_LIMITS,
        account=LimitSet(),
        profile=profile.limits,
        strategy=LimitSet(),
    )

    return LimitsResponse(
        risk_profile=settings.risk_profile.value,
        profile_description=profile.description,
        mode=settings.mode.value,
        profile_allows_mode=profile_allows_mode(profile, settings.mode),
        limits=[
            EffectiveLimit(
                field_name=o.field_name,
                value=_str(o.value),
                tightens=o.direction.value,
                bound_by=list(o.bound_by),
                tier_values=[TierValue(tier=name, value=_str(v)) for name, v in o.tier_values],
                was_tightened=o.was_tightened,
                unset=o.is_unset,
            )
            for o in origins
        ],
        notice=(
            "Limits compose by tightening only. No profile, strategy, prompt, UI "
            "control or API call can loosen a limit set at a higher tier."
        ),
    )


@router.get("/throttle", response_model=list[ThrottleBandOut], summary="Drawdown throttle curve")
async def throttle_curve() -> list[ThrottleBandOut]:
    """How position size is cut as drawdown deepens.

    Recovery is convex against the account — 20% lost needs 25% to regain, 50%
    needs 100% — so size falls as the hole gets deeper.
    """
    profile = get_profile(get_settings().risk_profile)
    return [
        ThrottleBandOut(
            from_consumed=str(band.from_consumed),
            to_consumed=str(band.to_consumed),
            risk_multiplier=str(band.risk_multiplier),
            confidence_uplift=str(band.confidence_uplift),
            reward_risk_uplift=str(band.reward_risk_uplift),
        )
        for band in profile.throttle
    ]


@router.get("/profiles", response_model=list[ProfileSummary], summary="Available risk profiles")
async def profiles() -> list[ProfileSummary]:
    active = get_settings().risk_profile
    return [
        ProfileSummary(
            name=name.value,
            description=p.description,
            recommended=p.recommended,
            allowed_modes=sorted(m.value for m in p.allowed_modes),
            active=name == active,
        )
        for name, p in PROFILES.items()
    ]
