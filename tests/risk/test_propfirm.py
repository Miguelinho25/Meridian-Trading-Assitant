"""Prop-firm rules. The distinctions here decide whether an account survives."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from meridian_risk.propfirm import (
    GENERIC_TWO_PHASE,
    DailyLossReference,
    DrawdownType,
    LossBasis,
    PropAccountState,
    PropFirmProfile,
    RuleStatus,
    evaluate_profile,
)

pytestmark = pytest.mark.risk

NOW = datetime(2026, 7, 27, 15, 0, tzinfo=UTC)


def profile(**kw) -> PropFirmProfile:
    from dataclasses import replace

    return replace(GENERIC_TWO_PHASE, **kw)


def account(**kw) -> PropAccountState:
    base = {
        "balance": Decimal("100000"),
        "equity": Decimal("100000"),
        "high_water_mark": Decimal("100000"),
        "balance_at_day_start": Decimal("100000"),
        "highest_equity_today": Decimal("100000"),
    }
    return PropAccountState(**{**base, **kw})


def rule(result, name: str):
    return next(r for r in result.rules if r.rule == name)


class TestDailyLossBasis:
    """Equity vs balance decides whether an open loser breaches the limit."""

    def test_equity_basis_counts_floating_loss(self) -> None:
        result = evaluate_profile(
            profile(daily_loss_basis=LossBasis.EQUITY),
            account(balance=Decimal("100000"), equity=Decimal("97000")),
            evaluated_at=NOW,
        )
        assert rule(result, "max_daily_loss").used == Decimal("3000")

    def test_balance_basis_ignores_floating_loss(self) -> None:
        """The same open position, and no daily loss recorded — which is why
        conflating the two gets accounts killed."""
        result = evaluate_profile(
            profile(daily_loss_basis=LossBasis.BALANCE),
            account(balance=Decimal("100000"), equity=Decimal("97000")),
            evaluated_at=NOW,
        )
        assert rule(result, "max_daily_loss").used == Decimal("0")


class TestDailyLossReference:
    def test_from_balance_at_reset(self) -> None:
        result = evaluate_profile(
            profile(daily_loss_reference=DailyLossReference.BALANCE_AT_RESET),
            account(
                balance_at_day_start=Decimal("100000"),
                highest_equity_today=Decimal("103000"),
                equity=Decimal("101000"),
            ),
            evaluated_at=NOW,
        )
        # Up on the day, so nothing used.
        assert rule(result, "max_daily_loss").used == Decimal("0")

    def test_from_highest_equity_is_stricter(self) -> None:
        """Giving back an intraday gain counts against you."""
        result = evaluate_profile(
            profile(daily_loss_reference=DailyLossReference.HIGHEST_EQUITY),
            account(
                balance_at_day_start=Decimal("100000"),
                highest_equity_today=Decimal("103000"),
                equity=Decimal("101000"),
            ),
            evaluated_at=NOW,
        )
        assert rule(result, "max_daily_loss").used == Decimal("2000")


class TestDrawdownFloor:
    def test_static_floor_is_fixed_below_starting_balance(self) -> None:
        p = profile(total_loss_type=DrawdownType.STATIC)
        assert p.drawdown_floor(Decimal("100000")) == Decimal("90000")
        # Profit does not raise a static floor.
        assert p.drawdown_floor(Decimal("120000")) == Decimal("90000")

    def test_trailing_floor_follows_the_high_water_mark(self) -> None:
        p = profile(
            total_loss_type=DrawdownType.TRAILING,
            trailing_stops_at_initial_balance=False,
        )
        assert p.drawdown_floor(Decimal("100000")) == Decimal("90000")
        assert p.drawdown_floor(Decimal("105000")) == Decimal("95000")

    def test_trailing_locks_at_the_initial_balance(self) -> None:
        """The floor trails up until it reaches the starting balance, then stops.

        With a 10,000 allowance the lock engages once the high-water mark passes
        110,000 — below that the floor is still genuinely trailing.
        """
        p = profile(
            total_loss_type=DrawdownType.TRAILING,
            trailing_stops_at_initial_balance=True,
        )
        assert p.drawdown_floor(Decimal("108000")) == Decimal("98000")  # still trailing
        assert p.drawdown_floor(Decimal("110000")) == Decimal("100000")  # exactly at the lock
        assert p.drawdown_floor(Decimal("150000")) == Decimal("100000")  # locked

    def test_unlocked_trailing_keeps_climbing(self) -> None:
        p = profile(
            total_loss_type=DrawdownType.TRAILING,
            trailing_stops_at_initial_balance=False,
        )
        assert p.drawdown_floor(Decimal("150000")) == Decimal("140000")


class TestDrawdownConsumed:
    """The value that feeds the throttle."""

    def test_untouched_account_consumes_nothing(self) -> None:
        result = evaluate_profile(profile(), account(), evaluated_at=NOW)
        assert result.drawdown_consumed == Decimal(0)

    def test_half_the_allowance(self) -> None:
        result = evaluate_profile(profile(), account(equity=Decimal("95000")), evaluated_at=NOW)
        assert result.drawdown_consumed == Decimal("0.5")

    def test_clamped_to_one(self) -> None:
        result = evaluate_profile(profile(), account(equity=Decimal("50000")), evaluated_at=NOW)
        assert result.drawdown_consumed == Decimal(1)

    def test_never_negative(self) -> None:
        result = evaluate_profile(profile(), account(equity=Decimal("120000")), evaluated_at=NOW)
        assert result.drawdown_consumed >= Decimal(0)


class TestProjection:
    """Blocking only on breaches already incurred would be too late."""

    def test_projected_breach_is_flagged_before_it_happens(self) -> None:
        result = evaluate_profile(
            profile(),
            account(equity=Decimal("99000"), balance_at_day_start=Decimal("100000")),
            evaluated_at=NOW,
            projected_loss=Decimal("4500"),
        )
        assert any(r.rule == "max_daily_loss_projected" for r in result.rules)
        assert result.blocks_trading

    def test_survivable_projection_does_not_block(self) -> None:
        result = evaluate_profile(
            profile(), account(), evaluated_at=NOW, projected_loss=Decimal("350")
        )
        assert not result.blocks_trading

    def test_projected_consumption_is_reported(self) -> None:
        result = evaluate_profile(
            profile(), account(), evaluated_at=NOW, projected_loss=Decimal("2000")
        )
        assert result.projected_drawdown_consumed == Decimal("0.2")


class TestBufferWarnings:
    def test_warns_before_the_limit(self) -> None:
        # Day start close to current equity, so only the *total* loss rule is in
        # warning — otherwise the daily limit breaches and blocks for an
        # unrelated reason, and the test proves nothing about buffers.
        result = evaluate_profile(
            profile(),
            account(equity=Decimal("91500"), balance_at_day_start=Decimal("92000")),
            evaluated_at=NOW,
        )
        assert rule(result, "max_total_loss").status is RuleStatus.BUFFER_WARNING
        assert not result.blocks_trading

    def test_violation_blocks(self) -> None:
        result = evaluate_profile(
            profile(),
            account(equity=Decimal("89000"), balance_at_day_start=Decimal("89500")),
            evaluated_at=NOW,
        )
        assert rule(result, "max_total_loss").status is RuleStatus.VIOLATED
        assert result.blocks_trading


class TestTradingDays:
    def test_minimum_days_in_progress(self) -> None:
        result = evaluate_profile(profile(), account(trading_days_completed=2), evaluated_at=NOW)
        assert rule(result, "min_trading_days").status is RuleStatus.IN_PROGRESS

    def test_minimum_days_met(self) -> None:
        result = evaluate_profile(profile(), account(trading_days_completed=4), evaluated_at=NOW)
        assert rule(result, "min_trading_days").status is RuleStatus.OK

    def test_inactivity_breach(self) -> None:
        result = evaluate_profile(profile(), account(days_since_last_trade=45), evaluated_at=NOW)
        assert rule(result, "inactivity").status is RuleStatus.VIOLATED


class TestConsistencyRule:
    def test_concentrated_profit_breaches(self) -> None:
        result = evaluate_profile(
            profile(
                consistency_rule_enabled=True,
                max_single_day_profit_pct_of_total=Decimal("40"),
            ),
            account(total_profit=Decimal("8000"), largest_single_day_profit=Decimal("5000")),
            evaluated_at=NOW,
        )
        assert rule(result, "consistency").status is RuleStatus.VIOLATED

    def test_spread_profit_passes(self) -> None:
        result = evaluate_profile(
            profile(
                consistency_rule_enabled=True,
                max_single_day_profit_pct_of_total=Decimal("40"),
            ),
            account(total_profit=Decimal("8000"), largest_single_day_profit=Decimal("2000")),
            evaluated_at=NOW,
        )
        assert rule(result, "consistency").status is RuleStatus.OK


class TestResetTimezone:
    def test_day_boundary_uses_a_real_timezone(self) -> None:
        """An hour's error in the reset can breach a limit that looked to have room."""
        p = profile(reset_timezone="America/New_York", reset_time=time(17, 0))
        # 20:00 UTC on 27 July is 16:00 New York — before the 17:00 reset, so the
        # trading day began at 17:00 on the 26th.
        start = p.trading_day_start(datetime(2026, 7, 27, 20, 0, tzinfo=UTC))
        assert start.date() == date(2026, 7, 26)

    def test_after_reset_starts_a_new_day(self) -> None:
        p = profile(reset_timezone="America/New_York", reset_time=time(17, 0))
        start = p.trading_day_start(datetime(2026, 7, 27, 22, 0, tzinfo=UTC))
        assert start.date() == date(2026, 7, 27)

    def test_dst_is_handled(self) -> None:
        """A fixed local reset lands at a different UTC instant across DST.

        London midnight is 00:00 UTC in winter and 23:00 the previous day in
        summer. An hour's error here can breach a limit that looked to have room.
        """
        p = profile(reset_timezone="Europe/London", reset_time=time(0, 0))
        winter = p.trading_day_start(datetime(2026, 1, 15, 12, 0, tzinfo=UTC))
        summer = p.trading_day_start(datetime(2026, 7, 15, 12, 0, tzinfo=UTC))
        assert (winter.hour, winter.day) == (0, 15)
        assert (summer.hour, summer.day) == (23, 14)


class TestVerificationDiscipline:
    def test_bundled_profile_is_unverified(self) -> None:
        """It ships with invented values and must never look authoritative."""
        assert not GENERIC_TWO_PHASE.is_verified
        assert "EXAMPLE" in GENERIC_TWO_PHASE.name
        assert "SYNTHETIC" in GENERIC_TWO_PHASE.source

    def test_unverified_profile_warns(self) -> None:
        result = evaluate_profile(profile(), account(), evaluated_at=NOW)
        assert any("never been verified" in w for w in result.warnings)

    def test_stale_verification_warns(self) -> None:
        p = profile(last_verified_at=date(2025, 1, 1))
        result = evaluate_profile(p, account(), evaluated_at=NOW)
        assert any("stale" in w for w in result.warnings)

    def test_recent_verification_does_not_warn(self) -> None:
        p = profile(last_verified_at=date(2026, 7, 1), verified_by="operator")
        result = evaluate_profile(p, account(), evaluated_at=NOW)
        assert not any("stale" in w for w in result.warnings)


class TestDeterminism:
    def test_same_inputs_same_evaluation(self) -> None:
        a = evaluate_profile(profile(), account(equity=Decimal("96000")), evaluated_at=NOW)
        b = evaluate_profile(profile(), account(equity=Decimal("96000")), evaluated_at=NOW)
        assert a.drawdown_consumed == b.drawdown_consumed
        assert [r.status for r in a.rules] == [r.status for r in b.rules]
