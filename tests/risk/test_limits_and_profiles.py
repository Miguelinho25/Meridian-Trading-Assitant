"""Limit composition (I2) and the drawdown throttle."""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from meridian_config import limits as system_limits
from meridian_config.settings import Mode, RiskProfileName
from meridian_risk.limits import TIGHTEN_DIRECTION, LimitSet, Tighten, compose, require
from meridian_risk.profiles import (
    PROFILES,
    SYSTEM_LIMITS,
    get_profile,
    profile_allows_mode,
)

pytestmark = pytest.mark.risk


class TestCompositionTightensOnly:
    """Invariant I2. The whole safety model rests on this."""

    def test_ceiling_takes_the_minimum(self) -> None:
        result = compose(
            LimitSet(risk_per_trade_pct=Decimal("1.00")),
            LimitSet(risk_per_trade_pct=Decimal("0.35")),
        )
        assert result.risk_per_trade_pct == Decimal("0.35")

    def test_floor_takes_the_maximum(self) -> None:
        """A higher minimum confidence is stricter, so composition raises it."""
        result = compose(
            LimitSet(min_confidence=Decimal("0.30")),
            LimitSet(min_confidence=Decimal("0.70")),
        )
        assert result.min_confidence == Decimal("0.70")

    def test_news_buffer_composes_upward(self) -> None:
        """A larger buffer is stricter — the direction most easily got backwards."""
        result = compose(LimitSet(news_buffer_minutes=5), LimitSet(news_buffer_minutes=30))
        assert result.news_buffer_minutes == 30

    def test_loss_streak_composes_downward(self) -> None:
        """Cooling down after 2 losses is stricter than after 5."""
        result = compose(
            LimitSet(loss_streak_cooldown_after=5), LimitSet(loss_streak_cooldown_after=2)
        )
        assert result.loss_streak_cooldown_after == 2

    def test_a_profile_cannot_loosen_the_system_ceiling(self) -> None:
        """The headline guarantee, stated as a test."""
        greedy = LimitSet(risk_per_trade_pct=Decimal("5.00"))
        result = compose(SYSTEM_LIMITS, greedy)
        assert result.risk_per_trade_pct == system_limits.MAX_RISK_PER_TRADE_PCT
        assert result.risk_per_trade_pct < Decimal("5.00")

    def test_none_means_no_opinion(self) -> None:
        result = compose(
            LimitSet(risk_per_trade_pct=Decimal("0.5")), LimitSet(risk_per_trade_pct=None)
        )
        assert result.risk_per_trade_pct == Decimal("0.5")

    def test_all_none_stays_none(self) -> None:
        assert compose(LimitSet(), LimitSet()).risk_per_trade_pct is None

    def test_order_does_not_matter(self) -> None:
        """min/max are commutative — the guarantee does not depend on anyone
        remembering the right tier order."""
        a = LimitSet(risk_per_trade_pct=Decimal("0.5"), min_confidence=Decimal("0.4"))
        b = LimitSet(risk_per_trade_pct=Decimal("0.3"), min_confidence=Decimal("0.7"))
        assert compose(a, b) == compose(b, a)


class TestCompositionProperties:
    _CEILINGS = [n for n, d in TIGHTEN_DIRECTION.items() if d is Tighten.LOWER]
    _FLOORS = [n for n, d in TIGHTEN_DIRECTION.items() if d is Tighten.HIGHER]

    @settings(max_examples=200, deadline=None)
    @given(
        a=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5"), places=2),
        b=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5"), places=2),
        c=st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5"), places=2),
    )
    def test_composed_is_never_looser_than_any_tier(
        self, a: Decimal, b: Decimal, c: Decimal
    ) -> None:
        tiers = [
            LimitSet(risk_per_trade_pct=a, min_confidence=a),
            LimitSet(risk_per_trade_pct=b, min_confidence=b),
            LimitSet(risk_per_trade_pct=c, min_confidence=c),
        ]
        result = compose(*tiers)
        for tier in tiers:
            assert result.is_tighter_or_equal(tier)

    def test_every_field_has_a_declared_direction(self) -> None:
        """A field without a direction is silently excluded from composition,
        which is exactly the loosening I2 forbids. Enforced at import; asserted
        here so the reason is discoverable."""
        from dataclasses import fields

        assert {f.name for f in fields(LimitSet)} == set(TIGHTEN_DIRECTION)


class TestMissingLimitsFailClosed:
    def test_require_raises_rather_than_defaulting(self) -> None:
        """I7 — the engine must not invent a limit nobody set."""
        with pytest.raises(ValueError, match="will not substitute a default"):
            require(LimitSet(), "risk_per_trade_pct")

    def test_require_returns_a_present_limit(self) -> None:
        assert require(LimitSet(max_positions=4), "max_positions") == 4


class TestProfiles:
    def test_all_five_names_resolve(self) -> None:
        for name in RiskProfileName:
            assert get_profile(name) is not None

    def test_custom_starts_from_challenge(self) -> None:
        assert get_profile(RiskProfileName.CUSTOM).name is RiskProfileName.CHALLENGE

    def test_challenge_is_the_recommended_profile(self) -> None:
        recommended = [p for p in PROFILES.values() if p.recommended]
        assert [p.name for p in recommended] == [RiskProfileName.CHALLENGE]

    def test_experimental_is_never_recommended(self) -> None:
        assert not PROFILES[RiskProfileName.EXPERIMENTAL].recommended

    def test_no_profile_exceeds_the_system_ceiling(self) -> None:
        for profile in PROFILES.values():
            effective = compose(SYSTEM_LIMITS, profile.limits)
            assert effective.risk_per_trade_pct is not None
            assert effective.risk_per_trade_pct <= system_limits.MAX_RISK_PER_TRADE_PCT

    def test_profiles_are_ordered_by_risk(self) -> None:
        order = [
            RiskProfileName.PRESERVATION,
            RiskProfileName.CHALLENGE,
            RiskProfileName.ASSERTIVE,
            RiskProfileName.EXPERIMENTAL,
        ]
        risks = [PROFILES[n].limits.risk_per_trade_pct for n in order]
        assert risks == sorted(risks)

    def test_stricter_profiles_demand_more_confidence(self) -> None:
        assert (
            PROFILES[RiskProfileName.PRESERVATION].limits.min_confidence
            > PROFILES[RiskProfileName.EXPERIMENTAL].limits.min_confidence
        )

    def test_no_profile_permits_broker_mode(self) -> None:
        for profile in PROFILES.values():
            assert not profile_allows_mode(profile, Mode.BROKER)

    def test_unknown_profile_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown risk profile"):
            get_profile("NOT_A_PROFILE")  # type: ignore[arg-type]


class TestDrawdownThrottle:
    def test_normal_risk_when_drawdown_is_shallow(self) -> None:
        profile = PROFILES[RiskProfileName.CHALLENGE]
        assert profile.multiplier_at(Decimal("0.10")).risk_multiplier == Decimal("1.00")

    def test_risk_is_cut_as_drawdown_deepens(self) -> None:
        profile = PROFILES[RiskProfileName.CHALLENGE]
        assert profile.multiplier_at(Decimal("0.30")).risk_multiplier == Decimal("0.75")
        assert profile.multiplier_at(Decimal("0.50")).risk_multiplier == Decimal("0.50")
        assert profile.multiplier_at(Decimal("0.70")).risk_multiplier == Decimal("0.25")

    def test_zero_multiplier_above_the_block_threshold(self) -> None:
        profile = PROFILES[RiskProfileName.CHALLENGE]
        assert profile.multiplier_at(Decimal("0.80")).risk_multiplier == Decimal(0)

    def test_deeper_bands_demand_more_confidence(self) -> None:
        profile = PROFILES[RiskProfileName.CHALLENGE]
        assert profile.multiplier_at(Decimal("0.50")).confidence_uplift > Decimal(0)
        assert profile.multiplier_at(Decimal("0.50")).reward_risk_uplift > Decimal(0)

    @settings(max_examples=200, deadline=None)
    @given(
        shallow=st.decimals(min_value=0, max_value=Decimal("0.99"), places=3),
        extra=st.decimals(min_value=0, max_value=Decimal("0.99"), places=3),
    )
    def test_multiplier_is_monotone_non_increasing(self, shallow: Decimal, extra: Decimal) -> None:
        """Deeper drawdown must never produce a larger size. Tested across every
        profile, because a mis-ordered band in any one of them is a live hazard."""
        deep = shallow + extra
        for profile in PROFILES.values():
            assert (
                profile.multiplier_at(deep).risk_multiplier
                <= profile.multiplier_at(shallow).risk_multiplier
            )

    def test_out_of_range_input_throttles_hardest(self) -> None:
        """A mis-specified prop profile could yield >1.0 consumed. That must
        throttle harder, never fall through to normal risk."""
        for profile in PROFILES.values():
            assert profile.multiplier_at(Decimal("5.0")).risk_multiplier == Decimal(0)

    def test_negative_input_is_treated_as_zero(self) -> None:
        profile = PROFILES[RiskProfileName.CHALLENGE]
        assert profile.multiplier_at(Decimal("-0.5")).risk_multiplier == Decimal("1.00")

    def test_bands_are_contiguous_with_no_gaps(self) -> None:
        for profile in PROFILES.values():
            bands = profile.throttle
            for earlier, later in pairwise(bands):
                assert earlier.to_consumed == later.from_consumed, profile.name

    def test_preservation_cuts_earlier_than_challenge(self) -> None:
        preservation = PROFILES[RiskProfileName.PRESERVATION]
        challenge = PROFILES[RiskProfileName.CHALLENGE]
        at_25pct = Decimal("0.25")
        assert (
            preservation.multiplier_at(at_25pct).risk_multiplier
            < challenge.multiplier_at(at_25pct).risk_multiplier
        )
