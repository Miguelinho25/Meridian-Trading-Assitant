"""Prop-firm rule engine (prop-firm-profiles.md).

No firm is hard-coded. A profile is versioned, dated, user-verifiable data that
this module evaluates against account state.

Four fields are separated because they are commonly conflated, and each pairing
decides whether an account survives:

* ``daily_loss_basis`` — equity (includes floating) or balance (closed only).
  Decides whether an open loser breaches the limit. One of the most common ways
  evaluations are failed.
* ``daily_loss_reference`` — measured from the balance at reset, or from the
  day's highest equity. The latter is far stricter: give back an intraday gain
  and it counts against you.
* ``total_loss_type`` — a static floor is a fundamentally different game from a
  trailing one that follows equity upward.
* ``trailing_stops_at_initial_balance`` — whether a trailing floor locks once it
  reaches the starting balance. Without this, a trader who is up 8% still has
  only the original drawdown allowance beneath them.

**Meridian ships no real firm's rules.** The bundled profile is a clearly
labelled example with invented values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final
from zoneinfo import ZoneInfo


class Phase(StrEnum):
    EVALUATION_1 = "EVALUATION_1"
    EVALUATION_2 = "EVALUATION_2"
    FUNDED = "FUNDED"


class LossBasis(StrEnum):
    EQUITY = "EQUITY"  # includes floating P&L
    BALANCE = "BALANCE"  # closed trades only


class DailyLossReference(StrEnum):
    BALANCE_AT_RESET = "BALANCE_AT_RESET"
    HIGHEST_EQUITY = "HIGHEST_EQUITY"  # stricter: giving back a gain counts


class DrawdownType(StrEnum):
    STATIC = "STATIC"
    TRAILING = "TRAILING"


class RuleStatus(StrEnum):
    OK = "OK"
    BUFFER_WARNING = "BUFFER_WARNING"
    IN_PROGRESS = "IN_PROGRESS"
    VIOLATED = "VIOLATED"


@dataclass(frozen=True, slots=True)
class PropFirmProfile:
    profile_id: str
    name: str
    version: str
    phase: Phase

    starting_balance: Decimal
    account_currency: str
    profit_target_pct: Decimal | None

    max_daily_loss_pct: Decimal
    daily_loss_basis: LossBasis
    daily_loss_reference: DailyLossReference
    reset_time: time
    reset_timezone: str

    max_total_loss_pct: Decimal
    total_loss_type: DrawdownType
    trailing_basis: LossBasis = LossBasis.EQUITY
    trailing_stops_at_initial_balance: bool = True

    min_trading_days: int = 0
    max_trading_days: int | None = None
    inactivity_days: int | None = None

    consistency_rule_enabled: bool = False
    max_single_day_profit_pct_of_total: Decimal | None = None

    weekend_holding_allowed: bool = True
    overnight_holding_allowed: bool = True
    news_trading_restricted: bool = False
    news_buffer_minutes: int = 0
    ea_allowed: bool = True
    instrument_restrictions: tuple[str, ...] = ()

    buffer_warning_pct: Decimal = Decimal("20.00")

    # --- Provenance. Unverified rules are worse than no rules. ---
    source: str = "SYNTHETIC EXAMPLE — values invented for testing"
    rule_source_date: date | None = None
    last_verified_at: date | None = None
    verified_by: str | None = None
    notes: str = ""
    enabled: bool = True

    @property
    def is_verified(self) -> bool:
        return self.last_verified_at is not None

    def verification_age_days(self, today: date) -> int | None:
        if self.last_verified_at is None:
            return None
        return (today - self.last_verified_at).days

    def is_stale(self, today: date, *, max_age_days: int = 90) -> bool:
        """Whether the rules need re-checking against the firm's current terms.

        Unverified counts as stale. The system cannot check a firm's terms for
        you; it can refuse to let you forget that you have not.
        """
        age = self.verification_age_days(today)
        return age is None or age > max_age_days

    # --- Derived limits ---------------------------------------------------

    @property
    def daily_loss_limit(self) -> Decimal:
        return self.starting_balance * self.max_daily_loss_pct / Decimal(100)

    @property
    def total_loss_limit(self) -> Decimal:
        return self.starting_balance * self.max_total_loss_pct / Decimal(100)

    @property
    def profit_target(self) -> Decimal | None:
        if self.profit_target_pct is None:
            return None
        return self.starting_balance * self.profit_target_pct / Decimal(100)

    def drawdown_floor(self, high_water_mark: Decimal) -> Decimal:
        """The equity level at which the account is breached.

        Static: fixed beneath the starting balance, whatever profit is made.

        Trailing: follows the high-water mark up. If
        ``trailing_stops_at_initial_balance`` the floor stops rising once it
        reaches the starting balance — without that, a trader up 8% still has
        only the original allowance beneath them, which is a materially
        different and much harsher game.
        """
        if self.total_loss_type is DrawdownType.STATIC:
            return self.starting_balance - self.total_loss_limit

        trailing = high_water_mark - self.total_loss_limit
        if self.trailing_stops_at_initial_balance:
            return min(trailing, self.starting_balance)
        return trailing

    def trading_day_start(self, moment: datetime) -> datetime:
        """Start of the trading day containing ``moment``, in UTC.

        Uses a real named timezone so DST is handled. Getting a daily reset wrong
        by an hour can breach a limit that appeared to have room.
        """
        zone = ZoneInfo(self.reset_timezone)
        local = moment.astimezone(zone)
        candidate = local.replace(
            hour=self.reset_time.hour,
            minute=self.reset_time.minute,
            second=0,
            microsecond=0,
        )
        if local < candidate:
            candidate -= timedelta(days=1)
        return candidate.astimezone(moment.tzinfo or zone)


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: str
    status: RuleStatus
    limit: Decimal | None = None
    used: Decimal | None = None
    remaining: Decimal | None = None
    used_pct_of_limit: Decimal | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    profile_id: str
    profile_version: str
    evaluated_at: datetime
    rules: tuple[RuleResult, ...]
    #: Fraction of the drawdown allowance consumed, in [0, 1]. Feeds the
    #: throttle, so its denominator always matches the active profile.
    drawdown_consumed: Decimal
    projected_drawdown_consumed: Decimal | None = None
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> RuleStatus:
        if any(r.status is RuleStatus.VIOLATED for r in self.rules):
            return RuleStatus.VIOLATED
        if any(r.status is RuleStatus.BUFFER_WARNING for r in self.rules):
            return RuleStatus.BUFFER_WARNING
        return RuleStatus.OK

    @property
    def blocking(self) -> tuple[RuleResult, ...]:
        return tuple(r for r in self.rules if r.status is RuleStatus.VIOLATED)

    @property
    def blocks_trading(self) -> bool:
        return bool(self.blocking)


@dataclass(frozen=True, slots=True)
class PropAccountState:
    """Account facts the prop rules need. Supplied, never fetched."""

    balance: Decimal
    equity: Decimal
    #: Highest equity (or balance) ever reached, per ``trailing_basis``.
    high_water_mark: Decimal
    balance_at_day_start: Decimal
    highest_equity_today: Decimal
    trading_days_completed: int = 0
    largest_single_day_profit: Decimal = Decimal(0)
    total_profit: Decimal = Decimal(0)
    days_since_last_trade: int = 0


def evaluate_profile(
    profile: PropFirmProfile,
    account: PropAccountState,
    *,
    evaluated_at: datetime,
    projected_loss: Decimal = Decimal(0),
) -> RuleEvaluation:
    """Evaluate every rule, and project the effect of a proposed trade.

    ``projected_loss`` is the account-currency loss if the proposed trade hits
    its stop. Evaluating against the *projected* state is the whole point:
    blocking only on breaches already incurred is too late, because the purpose
    of a limit is not to record that it was passed.
    """
    rules: list[RuleResult] = []
    warnings: list[str] = []
    buffer = profile.buffer_warning_pct / Decimal(100)

    # --- Daily loss ----------------------------------------------------
    current = account.equity if profile.daily_loss_basis is LossBasis.EQUITY else account.balance
    reference = (
        account.balance_at_day_start
        if profile.daily_loss_reference is DailyLossReference.BALANCE_AT_RESET
        else account.highest_equity_today
    )
    daily_used = max(Decimal(0), reference - current)
    daily_limit = profile.daily_loss_limit
    daily_pct = _pct(daily_used, daily_limit)

    rules.append(
        RuleResult(
            rule="max_daily_loss",
            status=_status(daily_used, daily_limit, buffer),
            limit=daily_limit,
            used=daily_used,
            remaining=max(Decimal(0), daily_limit - daily_used),
            used_pct_of_limit=daily_pct,
            detail=(
                f"{profile.daily_loss_basis.value} basis, measured from "
                f"{profile.daily_loss_reference.value}"
            ),
        )
    )

    # --- Total loss / drawdown -----------------------------------------
    floor = profile.drawdown_floor(account.high_water_mark)
    allowance = max(Decimal(0), account.high_water_mark - floor)
    total_used = max(Decimal(0), account.high_water_mark - account.equity)
    if profile.total_loss_type is DrawdownType.STATIC:
        total_used = max(Decimal(0), profile.starting_balance - account.equity)
        allowance = profile.total_loss_limit

    consumed = _fraction(total_used, allowance)

    rules.append(
        RuleResult(
            rule="max_total_loss",
            status=_status(total_used, allowance, buffer),
            limit=allowance,
            used=total_used,
            remaining=max(Decimal(0), allowance - total_used),
            used_pct_of_limit=_pct(total_used, allowance),
            detail=(f"{profile.total_loss_type.value} drawdown; breach floor at {floor} equity"),
        )
    )

    # --- Projection ------------------------------------------------------
    projected_consumed: Decimal | None = None
    if projected_loss > 0:
        projected_daily = daily_used + projected_loss
        projected_total = total_used + projected_loss
        projected_consumed = _fraction(projected_total, allowance)

        if projected_daily > daily_limit:
            rules.append(
                RuleResult(
                    rule="max_daily_loss_projected",
                    status=RuleStatus.VIOLATED,
                    limit=daily_limit,
                    used=projected_daily,
                    detail=(
                        f"Loss at stop would take daily loss to {projected_daily}, "
                        f"past the {daily_limit} limit."
                    ),
                )
            )
        if projected_total > allowance:
            rules.append(
                RuleResult(
                    rule="max_total_loss_projected",
                    status=RuleStatus.VIOLATED,
                    limit=allowance,
                    used=projected_total,
                    detail=(
                        f"Loss at stop would take drawdown to {projected_total}, "
                        f"past the {allowance} allowance."
                    ),
                )
            )

    # --- Profit target ---------------------------------------------------
    target = profile.profit_target
    if target is not None:
        achieved = account.equity - profile.starting_balance
        rules.append(
            RuleResult(
                rule="profit_target",
                status=RuleStatus.OK if achieved >= target else RuleStatus.IN_PROGRESS,
                limit=target,
                used=achieved,
                remaining=max(Decimal(0), target - achieved),
                used_pct_of_limit=_pct(achieved, target),
            )
        )

    # --- Trading days ----------------------------------------------------
    if profile.min_trading_days:
        met = account.trading_days_completed >= profile.min_trading_days
        rules.append(
            RuleResult(
                rule="min_trading_days",
                status=RuleStatus.OK if met else RuleStatus.IN_PROGRESS,
                limit=Decimal(profile.min_trading_days),
                used=Decimal(account.trading_days_completed),
                remaining=Decimal(
                    max(0, profile.min_trading_days - account.trading_days_completed)
                ),
            )
        )

    if profile.max_trading_days is not None:
        exceeded = account.trading_days_completed > profile.max_trading_days
        rules.append(
            RuleResult(
                rule="max_trading_days",
                status=RuleStatus.VIOLATED if exceeded else RuleStatus.OK,
                limit=Decimal(profile.max_trading_days),
                used=Decimal(account.trading_days_completed),
            )
        )

    if profile.inactivity_days is not None:
        stale = account.days_since_last_trade > profile.inactivity_days
        rules.append(
            RuleResult(
                rule="inactivity",
                status=RuleStatus.VIOLATED if stale else RuleStatus.OK,
                limit=Decimal(profile.inactivity_days),
                used=Decimal(account.days_since_last_trade),
            )
        )

    # --- Consistency ------------------------------------------------------
    if (
        profile.consistency_rule_enabled
        and profile.max_single_day_profit_pct_of_total
        and account.total_profit > 0
    ):
        share = account.largest_single_day_profit / account.total_profit * Decimal(100)
        breached = share > profile.max_single_day_profit_pct_of_total
        rules.append(
            RuleResult(
                rule="consistency",
                status=RuleStatus.VIOLATED if breached else RuleStatus.OK,
                limit=profile.max_single_day_profit_pct_of_total,
                used=share,
                detail=(
                    f"Largest single day is {share:.1f}% of total profit; "
                    f"cap is {profile.max_single_day_profit_pct_of_total}%."
                ),
            )
        )

    if not profile.is_verified:
        warnings.append("Profile has never been verified against the firm's official terms.")
    if profile.is_stale(evaluated_at.date()):
        warnings.append("Rule verification is stale — re-check the firm's current terms.")

    return RuleEvaluation(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        evaluated_at=evaluated_at,
        rules=tuple(rules),
        drawdown_consumed=consumed,
        projected_drawdown_consumed=projected_consumed,
        warnings=tuple(warnings),
    )


def _pct(used: Decimal, limit: Decimal) -> Decimal:
    return Decimal(0) if limit <= 0 else (used / limit) * Decimal(100)


def _fraction(used: Decimal, allowance: Decimal) -> Decimal:
    """Consumed fraction, clamped to [0, 1].

    Clamped because it feeds the throttle, whose bands assume that domain — an
    out-of-range value there would fall through to the most restrictive band,
    which is safe, but a clamped value is honest and testable.
    """
    if allowance <= 0:
        return Decimal(1)
    return max(Decimal(0), min(Decimal(1), used / allowance))


def _status(used: Decimal, limit: Decimal, buffer: Decimal) -> RuleStatus:
    if limit <= 0:
        return RuleStatus.VIOLATED
    if used >= limit:
        return RuleStatus.VIOLATED
    if used >= limit * (Decimal(1) - buffer):
        return RuleStatus.BUFFER_WARNING
    return RuleStatus.OK


#: A generic two-phase example. Every value is invented — see `source`.
GENERIC_TWO_PHASE: Final = PropFirmProfile(
    profile_id="generic-2phase",
    name="Generic Two-Phase Evaluation (EXAMPLE — NOT A REAL FIRM)",
    version="0.1.0",
    phase=Phase.EVALUATION_1,
    starting_balance=Decimal("100000"),
    account_currency="USD",
    profit_target_pct=Decimal("8.00"),
    max_daily_loss_pct=Decimal("5.00"),
    daily_loss_basis=LossBasis.EQUITY,
    daily_loss_reference=DailyLossReference.BALANCE_AT_RESET,
    reset_time=time(0, 0),
    reset_timezone="America/New_York",
    max_total_loss_pct=Decimal("10.00"),
    total_loss_type=DrawdownType.STATIC,
    min_trading_days=4,
    inactivity_days=30,
    notes="Replace every value with the firm's published terms before relying on this.",
)

PROP_PROFILES: Final[dict[str, PropFirmProfile]] = {
    GENERIC_TWO_PHASE.profile_id: GENERIC_TWO_PHASE,
}
