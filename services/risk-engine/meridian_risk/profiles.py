"""Risk profiles and the drawdown throttle (risk-engine.md §6–7).

Profiles are data, and they compose as one tier among four. A profile can only
ever tighten what the system and account tiers allow — ``EXPERIMENTAL`` asking for
1.00% under a system cap of 0.50% yields 0.50%, silently and correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final

from meridian_config import limits as system_limits
from meridian_config.settings import Mode, RiskProfileName

from meridian_risk.limits import LimitSet

PROFILE_VERSION: Final = "risk-profiles@0.1.0"


@dataclass(frozen=True, slots=True)
class ThrottleBand:
    """One band of the drawdown-response curve."""

    #: Fraction of allowed drawdown consumed, lower bound (inclusive).
    from_consumed: Decimal
    #: Upper bound (exclusive).
    to_consumed: Decimal
    risk_multiplier: Decimal
    #: Added to the profile's minimum confidence in this band.
    confidence_uplift: Decimal = Decimal(0)
    #: Added to the profile's minimum reward:risk in this band.
    reward_risk_uplift: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class RiskProfile:
    name: RiskProfileName
    limits: LimitSet
    throttle: tuple[ThrottleBand, ...]
    #: Modes this profile may run in. EXPERIMENTAL is confined to research.
    allowed_modes: frozenset[Mode]
    description: str
    #: False for anything that should never be presented as a default.
    recommended: bool = False

    def multiplier_at(self, drawdown_consumed: Decimal) -> ThrottleBand:
        """The throttle band for a given drawdown fraction.

        Above the last band the most restrictive band applies, so an unexpected
        input (>1.0, from a mis-specified prop profile) throttles harder rather
        than falling through to normal risk.
        """
        clamped = max(Decimal(0), drawdown_consumed)
        for band in self.throttle:
            if band.from_consumed <= clamped < band.to_consumed:
                return band
        return self.throttle[-1]


def _standard_throttle() -> tuple[ThrottleBand, ...]:
    """The documented default curve.

    Drawdown recovery is convex against you — 20% lost needs 25% to recover, 50%
    needs 100% — so cutting size as drawdown deepens converts a potentially
    terminal path into a survivable one. For an evaluation account, where a breach
    ends the account outright, that trade is overwhelmingly correct.

    Stepwise rather than interpolated by default: predictable and auditable beats
    smooth when a human has to reason about why a size was halved.
    """
    return (
        ThrottleBand(Decimal("0.00"), Decimal("0.20"), Decimal("1.00")),
        ThrottleBand(Decimal("0.20"), Decimal("0.40"), Decimal("0.75")),
        ThrottleBand(
            Decimal("0.40"),
            Decimal("0.60"),
            Decimal("0.50"),
            confidence_uplift=Decimal("0.10"),
            reward_risk_uplift=Decimal("0.25"),
        ),
        ThrottleBand(
            Decimal("0.60"),
            Decimal("0.75"),
            Decimal("0.25"),
            confidence_uplift=Decimal("0.15"),
            reward_risk_uplift=Decimal("0.50"),
        ),
        # Above the block threshold the multiplier is zero: management of open
        # positions only. Enforced separately by a Tier A gate, but expressed here
        # too so the curve alone is never a route to a live size.
        ThrottleBand(Decimal("0.75"), Decimal("0.90"), Decimal("0.00")),
        ThrottleBand(Decimal("0.90"), Decimal("9.99"), Decimal("0.00")),
    )


def _aggressive_throttle() -> tuple[ThrottleBand, ...]:
    """Cuts earlier and harder. Used by PRESERVATION."""
    return (
        ThrottleBand(Decimal("0.00"), Decimal("0.15"), Decimal("1.00")),
        ThrottleBand(Decimal("0.15"), Decimal("0.30"), Decimal("0.60")),
        ThrottleBand(
            Decimal("0.30"),
            Decimal("0.50"),
            Decimal("0.35"),
            confidence_uplift=Decimal("0.10"),
            reward_risk_uplift=Decimal("0.25"),
        ),
        ThrottleBand(Decimal("0.50"), Decimal("9.99"), Decimal("0.00")),
    )


def _lenient_throttle() -> tuple[ThrottleBand, ...]:
    """Slower to cut. EXPERIMENTAL only, and never in a broker-connected mode."""
    return (
        ThrottleBand(Decimal("0.00"), Decimal("0.30"), Decimal("1.00")),
        ThrottleBand(Decimal("0.30"), Decimal("0.55"), Decimal("0.80")),
        ThrottleBand(Decimal("0.55"), Decimal("0.80"), Decimal("0.50")),
        ThrottleBand(Decimal("0.80"), Decimal("9.99"), Decimal("0.00")),
    )


_RESEARCH_MODES: Final = frozenset({Mode.RESEARCH, Mode.BACKTEST, Mode.PAPER})


PROFILES: Final[dict[RiskProfileName, RiskProfile]] = {
    RiskProfileName.PRESERVATION: RiskProfile(
        name=RiskProfileName.PRESERVATION,
        description="Protect capital. Few, high-quality trades; cuts size early.",
        allowed_modes=_RESEARCH_MODES,
        throttle=_aggressive_throttle(),
        limits=LimitSet(
            risk_per_trade_pct=Decimal("0.15"),
            daily_risk_budget_pct=Decimal("0.50"),
            max_open_risk_pct=Decimal("0.50"),
            max_instrument_exposure_pct=Decimal("0.30"),
            max_currency_exposure_pct=Decimal("0.40"),
            max_correlated_exposure_pct=Decimal("0.30"),
            max_strategy_budget_pct=Decimal("0.25"),
            max_margin_utilisation_pct=Decimal("10.00"),
            max_positions=2,
            max_trades_per_session=2,
            max_slippage_pips=Decimal("1.5"),
            loss_streak_cooldown_after=2,
            min_reward_risk=Decimal("2.0"),
            min_confidence=Decimal("0.70"),
            news_buffer_minutes=30,
            min_stop_atr_multiple=Decimal("0.5"),
            max_stop_atr_multiple=Decimal("4.0"),
        ),
    ),
    RiskProfileName.CHALLENGE: RiskProfile(
        name=RiskProfileName.CHALLENGE,
        description=(
            "Prop-firm evaluation. Optimises for survival and rule compliance, "
            "increasing selectivity under stress rather than frequency."
        ),
        allowed_modes=_RESEARCH_MODES,
        throttle=_standard_throttle(),
        recommended=True,
        limits=LimitSet(
            risk_per_trade_pct=Decimal("0.35"),
            daily_risk_budget_pct=Decimal("1.50"),
            max_open_risk_pct=Decimal("1.50"),
            max_instrument_exposure_pct=Decimal("0.70"),
            max_currency_exposure_pct=Decimal("1.00"),
            max_correlated_exposure_pct=Decimal("0.75"),
            max_strategy_budget_pct=Decimal("0.50"),
            max_margin_utilisation_pct=Decimal("20.00"),
            max_positions=4,
            max_trades_per_session=5,
            max_slippage_pips=Decimal("2.0"),
            loss_streak_cooldown_after=3,
            min_reward_risk=Decimal("1.5"),
            min_confidence=Decimal("0.55"),
            news_buffer_minutes=15,
            min_stop_atr_multiple=Decimal("0.4"),
            max_stop_atr_multiple=Decimal("5.0"),
        ),
    ),
    RiskProfileName.ASSERTIVE: RiskProfile(
        name=RiskProfileName.ASSERTIVE,
        description="Higher per-trade risk. Warned, and still subject to account limits.",
        allowed_modes=_RESEARCH_MODES,
        throttle=_standard_throttle(),
        limits=LimitSet(
            risk_per_trade_pct=Decimal("0.60"),
            daily_risk_budget_pct=Decimal("2.50"),
            max_open_risk_pct=Decimal("2.50"),
            max_instrument_exposure_pct=Decimal("1.20"),
            max_currency_exposure_pct=Decimal("1.60"),
            max_correlated_exposure_pct=Decimal("1.25"),
            max_strategy_budget_pct=Decimal("0.80"),
            max_margin_utilisation_pct=Decimal("30.00"),
            max_positions=6,
            max_trades_per_session=8,
            max_slippage_pips=Decimal("2.5"),
            loss_streak_cooldown_after=4,
            min_reward_risk=Decimal("1.2"),
            min_confidence=Decimal("0.45"),
            news_buffer_minutes=10,
            min_stop_atr_multiple=Decimal("0.3"),
            max_stop_atr_multiple=Decimal("6.0"),
        ),
    ),
    RiskProfileName.EXPERIMENTAL: RiskProfile(
        name=RiskProfileName.EXPERIMENTAL,
        description=(
            "EXPERIMENTAL — research and paper only. Not recommended, and it "
            "cannot migrate to a broker-connected mode."
        ),
        allowed_modes=_RESEARCH_MODES,
        throttle=_lenient_throttle(),
        limits=LimitSet(
            risk_per_trade_pct=system_limits.MAX_RISK_PER_TRADE_PCT,
            daily_risk_budget_pct=Decimal("3.00"),
            max_open_risk_pct=Decimal("3.00"),
            max_instrument_exposure_pct=Decimal("1.50"),
            max_currency_exposure_pct=Decimal("2.00"),
            max_correlated_exposure_pct=Decimal("1.50"),
            max_strategy_budget_pct=Decimal("1.00"),
            max_margin_utilisation_pct=Decimal("40.00"),
            max_positions=8,
            max_trades_per_session=12,
            max_slippage_pips=Decimal("4.0"),
            loss_streak_cooldown_after=5,
            min_reward_risk=Decimal("1.0"),
            min_confidence=Decimal("0.30"),
            news_buffer_minutes=5,
            min_stop_atr_multiple=Decimal("0.2"),
            max_stop_atr_multiple=Decimal("8.0"),
        ),
    ),
}

# CUSTOM starts from CHALLENGE and is edited within system hard limits. It is not
# a preset, so it is constructed on demand rather than listed here.


#: The system tier: the ceiling every other tier is clamped against.
SYSTEM_LIMITS: Final = LimitSet(
    risk_per_trade_pct=system_limits.MAX_RISK_PER_TRADE_PCT,
    daily_risk_budget_pct=system_limits.MAX_DAILY_RISK_BUDGET_PCT,
    max_open_risk_pct=system_limits.MAX_OPEN_RISK_PCT,
    max_positions=system_limits.MAX_SIMULTANEOUS_POSITIONS,
    max_trades_per_session=system_limits.MAX_TRADES_PER_SESSION,
)


def _validate_throttle_curves() -> None:
    """Selectivity must never loosen as drawdown deepens.

    Run at import, over every profile.

    The bands above the block threshold carry zero uplift because their risk
    multiplier is zero: no trade can be sized, so the quality floors are dead
    parameters. That is safe only while the multiplier stays at zero. Raising it
    later to permit small trades would silently make the deepest band *less*
    selective than the one above it — the curve would loosen at exactly the point
    it resumed trading.

    So the rule is conditional on the multiplier: among bands that permit
    trading, uplifts must be non-decreasing. A future edit that reopens a blocked
    band without setting its floors fails here rather than in production.
    """
    for name, profile in PROFILES.items():
        trading = [b for b in profile.throttle if b.risk_multiplier > 0]
        for shallower, deeper in pairwise(trading):
            if deeper.risk_multiplier > shallower.risk_multiplier:
                raise RuntimeError(
                    f"{name}: risk multiplier rises from {shallower.risk_multiplier} to "
                    f"{deeper.risk_multiplier} as drawdown deepens. The throttle must "
                    f"only cut size."
                )
            for label, shallow_v, deep_v in (
                ("confidence_uplift", shallower.confidence_uplift, deeper.confidence_uplift),
                ("reward_risk_uplift", shallower.reward_risk_uplift, deeper.reward_risk_uplift),
            ):
                if deep_v < shallow_v:
                    raise RuntimeError(
                        f"{name}: {label} falls from {shallow_v} to {deep_v} between the "
                        f"band at {shallower.from_consumed} and the deeper one at "
                        f"{deeper.from_consumed}, both of which permit trading. "
                        f"Selectivity must not loosen as drawdown deepens."
                    )


def get_profile(name: RiskProfileName) -> RiskProfile:
    if name is RiskProfileName.CUSTOM:
        return PROFILES[RiskProfileName.CHALLENGE]
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f"Unknown risk profile {name!r}") from None


def profile_allows_mode(profile: RiskProfile, mode: Mode) -> bool:
    return mode in profile.allowed_modes


_validate_throttle_curves()
