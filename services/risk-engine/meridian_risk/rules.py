"""The rule catalogue (risk-engine.md §4).

Every rule is a pure function ``(RiskContext, LimitSet, Decimal) -> RuleOutcome``.

**Evaluation is total (I5).** Every rule runs on every evaluation, even after one
has already rejected. Short-circuiting would be marginally faster and would
produce rejection reports listing only the first problem — which sends an
operator round a loop of fixing one thing at a time. The cost is nanoseconds.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from meridian_config.settings import ApprovalMode, Mode
from meridian_marketdata.instruments import CORRELATION_CLUSTERS
from meridian_schemas.enums import DataQualityVerdict, RejectionCode

from meridian_risk.context import RiskContext
from meridian_risk.decision import RuleOutcome
from meridian_risk.limits import LimitSet

Rule = Callable[[RiskContext, LimitSet, Decimal], RuleOutcome]


def _ok(code: RejectionCode) -> RuleOutcome:
    return RuleOutcome(code=code, passed=True)


def _reject(code: RejectionCode, detail: str, **kw: str | None) -> RuleOutcome:
    return RuleOutcome(code=code, passed=False, detail=detail, **kw)  # type: ignore[arg-type]


def _clamp(code: RejectionCode, lots: Decimal, detail: str, **kw: str | None) -> RuleOutcome:
    return RuleOutcome(
        code=code,
        passed=True,
        clamp_to_lots=lots,
        detail=detail,
        **kw,
    )


# ---------------------------------------------------------------------------
# Tier A — blocking gates. Nothing trades while any of these holds.
# ---------------------------------------------------------------------------


def rule_kill_switch(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.kill_switch_engaged:
        return _reject(
            RejectionCode.KILL_SWITCH_ENGAGED,
            "Kill switch engaged (or its state could not be read, which is treated identically).",
        )
    return _ok(RejectionCode.KILL_SWITCH_ENGAGED)


def rule_emergency_shutdown(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.emergency_shutdown:
        return _reject(RejectionCode.EMERGENCY_SHUTDOWN, "Emergency shutdown active.")
    return _ok(RejectionCode.EMERGENCY_SHUTDOWN)


def rule_mode_permits_execution(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.mode is Mode.RESEARCH:
        return _reject(
            RejectionCode.MODE_FORBIDS_EXECUTION,
            "Research mode records proposals but never submits them.",
        )
    if ctx.approval_mode is ApprovalMode.OBSERVE_ONLY:
        return _reject(
            RejectionCode.MODE_FORBIDS_EXECUTION,
            "OBSERVE_ONLY records proposals without submitting.",
        )
    return _ok(RejectionCode.MODE_FORBIDS_EXECUTION)


def rule_market_data_valid(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    quality = ctx.market.quality
    if quality.verdict is DataQualityVerdict.INVALID:
        codes = ", ".join(i.code for i in quality.blocking_issues) or "unspecified"
        return _reject(RejectionCode.MARKET_DATA_INVALID, f"Market data invalid: {codes}.")
    if quality.blocks_trading:
        return _reject(
            RejectionCode.MARKET_DATA_STALE,
            f"Market data is {quality.verdict.value}; only OK permits new orders.",
        )
    if ctx.market.ask <= ctx.market.bid:
        return _reject(
            RejectionCode.MARKET_DATA_INVALID,
            f"Crossed quote: ask {ctx.market.ask} <= bid {ctx.market.bid}.",
        )
    return _ok(RejectionCode.MARKET_DATA_INVALID)


def rule_weekend(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.market.is_weekend:
        return _reject(RejectionCode.WEEKEND_BLOCK, "Market closed for the weekend.")
    return _ok(RejectionCode.WEEKEND_BLOCK)


def rule_rollover(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.market.is_rollover:
        return _reject(
            RejectionCode.ROLLOVER_BLOCK,
            "Within the rollover window: spreads widen sharply and fills are unreliable.",
        )
    return _ok(RejectionCode.ROLLOVER_BLOCK)


def rule_news_window(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    buffer_minutes = limits.news_buffer_minutes
    if buffer_minutes is None:
        return _ok(RejectionCode.NEWS_WINDOW_BLOCK)
    if ctx.market.minutes_to_news is None:
        # Unknown is not clear. Without a calendar we cannot assert the window is
        # safe, and asserting it anyway is how a position ends up open across a
        # rate decision.
        return _reject(
            RejectionCode.NEWS_WINDOW_BLOCK,
            "Economic calendar unavailable; proximity to high-impact news is "
            "unknown and cannot be assumed safe.",
        )
    if abs(ctx.market.minutes_to_news) < buffer_minutes:
        return _reject(
            RejectionCode.NEWS_WINDOW_BLOCK,
            f"{abs(ctx.market.minutes_to_news)} minutes from high-impact news; "
            f"profile requires {buffer_minutes}.",
            limit=str(buffer_minutes),
        )
    return _ok(RejectionCode.NEWS_WINDOW_BLOCK)


def rule_abnormal_spread(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    threshold = Decimal("3.0")
    if ctx.market.spread_multiple > threshold:
        return _reject(
            RejectionCode.ABNORMAL_SPREAD,
            f"Spread is {ctx.market.spread_multiple:.1f}× typical, above {threshold}×.",
            limit=str(threshold),
        )
    return _ok(RejectionCode.ABNORMAL_SPREAD)


def rule_abnormal_volatility(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    ratio = ctx.market.volatility_ratio
    if ratio is not None and ratio > Decimal("3.0"):
        return _reject(
            RejectionCode.ABNORMAL_VOLATILITY,
            f"Volatility is {ratio:.1f}× its recent norm.",
        )
    return _ok(RejectionCode.ABNORMAL_VOLATILITY)


def rule_account_reconciled(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if not ctx.account.is_reconciled:
        return _reject(
            RejectionCode.ACCOUNT_STATE_AMBIGUOUS,
            "Account reconciliation failed; position and balance state is ambiguous.",
        )
    return _ok(RejectionCode.ACCOUNT_STATE_AMBIGUOUS)


def rule_duplicate_order(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.proposal.content_hash in ctx.active_order_hashes:
        return _reject(
            RejectionCode.DUPLICATE_ORDER,
            "An identical order is already live — a duplicate would double the intended exposure.",
        )
    return _ok(RejectionCode.DUPLICATE_ORDER)


# ---------------------------------------------------------------------------
# Tier B — account and prop-firm limits. Non-overridable.
# ---------------------------------------------------------------------------


def rule_daily_loss(ctx: RiskContext, _l: LimitSet, size: Decimal) -> RuleOutcome:
    account = ctx.account
    if account.daily_loss_used >= account.daily_loss_limit:
        return _reject(
            RejectionCode.DAILY_LOSS_LIMIT_REACHED,
            f"Daily loss {account.daily_loss_used} has reached the "
            f"{account.daily_loss_limit} limit.",
            before=str(account.daily_loss_used),
            limit=str(account.daily_loss_limit),
        )
    return _ok(RejectionCode.DAILY_LOSS_LIMIT_REACHED)


def rule_daily_loss_would_breach(
    ctx: RiskContext, _l: LimitSet, projected_loss: Decimal
) -> RuleOutcome:
    """Blocks a trade whose stop-out would breach the daily limit.

    Blocking only on breaches already incurred is too late: the point of a limit
    is not to record that it was passed.
    """
    account = ctx.account
    projected = account.daily_loss_used + projected_loss
    if projected > account.daily_loss_limit:
        return _reject(
            RejectionCode.DAILY_LOSS_WOULD_BREACH,
            f"Loss at stop ({projected_loss}) would take daily loss to {projected}, "
            f"past the {account.daily_loss_limit} limit.",
            before=str(account.daily_loss_used),
            after=str(projected),
            limit=str(account.daily_loss_limit),
        )
    return _ok(RejectionCode.DAILY_LOSS_WOULD_BREACH)


def rule_total_loss(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    account = ctx.account
    if account.total_loss_used >= account.total_loss_limit:
        return _reject(
            RejectionCode.TOTAL_LOSS_LIMIT_REACHED,
            f"Total loss {account.total_loss_used} has reached the "
            f"{account.total_loss_limit} limit.",
            limit=str(account.total_loss_limit),
        )
    return _ok(RejectionCode.TOTAL_LOSS_LIMIT_REACHED)


def rule_total_loss_would_breach(
    ctx: RiskContext, _l: LimitSet, projected_loss: Decimal
) -> RuleOutcome:
    account = ctx.account
    projected = account.total_loss_used + projected_loss
    if projected > account.total_loss_limit:
        return _reject(
            RejectionCode.TOTAL_LOSS_WOULD_BREACH,
            f"Loss at stop would take total loss to {projected}, past the "
            f"{account.total_loss_limit} limit.",
            after=str(projected),
            limit=str(account.total_loss_limit),
        )
    return _ok(RejectionCode.TOTAL_LOSS_WOULD_BREACH)


def rule_drawdown_block(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    """Hard block above the configured drawdown fraction."""
    from meridian_config import limits as system

    consumed = ctx.account.drawdown_consumed
    if consumed >= system.DRAWDOWN_KILL_THRESHOLD:
        return _reject(
            RejectionCode.EMERGENCY_SHUTDOWN,
            f"{consumed:.0%} of allowed drawdown consumed — past the "
            f"{system.DRAWDOWN_KILL_THRESHOLD:.0%} kill threshold.",
        )
    if consumed >= system.DRAWDOWN_BLOCK_THRESHOLD:
        return _reject(
            RejectionCode.TRAILING_DRAWDOWN_BREACH,
            f"{consumed:.0%} of allowed drawdown consumed — past the "
            f"{system.DRAWDOWN_BLOCK_THRESHOLD:.0%} block threshold. Existing "
            f"positions may be managed; no new trades.",
        )
    return _ok(RejectionCode.TRAILING_DRAWDOWN_BREACH)


# ---------------------------------------------------------------------------
# Tier C — exposure. These clamp rather than reject.
# ---------------------------------------------------------------------------


def _scale_to_budget(
    size: Decimal, current: Decimal, additional: Decimal, budget: Decimal
) -> Decimal:
    """Largest size keeping ``current + scaled_additional`` within ``budget``."""
    if additional <= 0:
        return size
    room = budget - current
    if room <= 0:
        return Decimal(0)
    if additional <= room:
        return size
    return size * (room / additional)


def rule_max_open_risk(ctx: RiskContext, limits: LimitSet, new_risk_pct: Decimal) -> RuleOutcome:
    budget = limits.max_open_risk_pct
    if budget is None:
        return _ok(RejectionCode.MAX_OPEN_RISK)
    current = ctx.portfolio.open_risk_pct
    if current + new_risk_pct <= budget:
        return _ok(RejectionCode.MAX_OPEN_RISK)
    return _clamp(
        RejectionCode.MAX_OPEN_RISK,
        _scale_to_budget(Decimal(1), current, new_risk_pct, budget),
        f"Open risk {current}% plus {new_risk_pct}% exceeds the {budget}% budget.",
        before=str(current),
        limit=str(budget),
    )


def rule_max_positions(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    cap = limits.max_positions
    if cap is None or ctx.portfolio.position_count < cap:
        return _ok(RejectionCode.MAX_SIMULTANEOUS_POSITIONS)
    return _reject(
        RejectionCode.MAX_SIMULTANEOUS_POSITIONS,
        f"{ctx.portfolio.position_count} positions open; cap is {cap}.",
        limit=str(cap),
    )


def rule_max_trades_per_session(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    cap = limits.max_trades_per_session
    if cap is None or ctx.account.trades_this_session < cap:
        return _ok(RejectionCode.MAX_TRADES_PER_SESSION)
    return _reject(
        RejectionCode.MAX_TRADES_PER_SESSION,
        f"{ctx.account.trades_this_session} trades this session; cap is {cap}.",
        limit=str(cap),
    )


def rule_instrument_exposure(
    ctx: RiskContext, limits: LimitSet, new_risk_pct: Decimal
) -> RuleOutcome:
    budget = limits.max_instrument_exposure_pct
    if budget is None:
        return _ok(RejectionCode.MAX_INSTRUMENT_EXPOSURE)
    current = ctx.portfolio.risk_in_instrument(ctx.proposal.instrument)
    if current + new_risk_pct <= budget:
        return _ok(RejectionCode.MAX_INSTRUMENT_EXPOSURE)
    return _clamp(
        RejectionCode.MAX_INSTRUMENT_EXPOSURE,
        _scale_to_budget(Decimal(1), current, new_risk_pct, budget),
        f"{ctx.proposal.instrument} exposure {current}% plus {new_risk_pct}% "
        f"exceeds the {budget}% cap.",
        before=str(current),
        limit=str(budget),
    )


def rule_currency_exposure(
    ctx: RiskContext, limits: LimitSet, new_risk_pct: Decimal
) -> RuleOutcome:
    """Both legs counted. Long EURUSD is long EUR and short USD."""
    budget = limits.max_currency_exposure_pct
    spec = ctx.specs.get(ctx.proposal.instrument) or ctx.market.spec
    if budget is None or spec is None:
        return _ok(RejectionCode.MAX_CURRENCY_EXPOSURE)

    from meridian_schemas.enums import Direction

    exposure = ctx.portfolio.currency_exposure(ctx.specs)
    sign = Decimal(1) if ctx.proposal.direction is Direction.LONG else Decimal(-1)

    worst_ccy, worst_after, worst_before = "", Decimal(0), Decimal(0)
    for ccy, delta in ((spec.base_ccy, sign), (spec.quote_ccy, -sign)):
        before = exposure.get(ccy, Decimal(0))
        after = abs(before + delta * new_risk_pct)
        if after > worst_after:
            worst_ccy, worst_after, worst_before = ccy, after, before

    if worst_after <= budget:
        return _ok(RejectionCode.MAX_CURRENCY_EXPOSURE)

    room = budget - abs(worst_before)
    scale = max(Decimal(0), room / new_risk_pct) if new_risk_pct > 0 else Decimal(0)
    return _clamp(
        RejectionCode.MAX_CURRENCY_EXPOSURE,
        scale,
        f"Net {worst_ccy} exposure would reach {worst_after}%, above the "
        f"{budget}% cap (both legs counted).",
        before=str(worst_before),
        after=str(worst_after),
        limit=str(budget),
    )


def rule_correlated_exposure(
    ctx: RiskContext, limits: LimitSet, new_risk_pct: Decimal
) -> RuleOutcome:
    budget = limits.max_correlated_exposure_pct
    if budget is None:
        return _ok(RejectionCode.MAX_CORRELATED_EXPOSURE)

    instrument = ctx.proposal.instrument
    worst_cluster, worst_total = "", Decimal(0)
    for cluster, members in CORRELATION_CLUSTERS.items():
        if instrument not in members:
            continue
        total = sum(
            (p.open_risk_pct for p in ctx.portfolio.open_positions if p.instrument in members),
            Decimal(0),
        )
        if total + new_risk_pct > worst_total:
            worst_cluster, worst_total = cluster, total + new_risk_pct

    if not worst_cluster or worst_total <= budget:
        return _ok(RejectionCode.MAX_CORRELATED_EXPOSURE)

    current = worst_total - new_risk_pct
    return _clamp(
        RejectionCode.MAX_CORRELATED_EXPOSURE,
        _scale_to_budget(Decimal(1), current, new_risk_pct, budget),
        f"Cluster {worst_cluster} exposure would reach {worst_total}%, above the {budget}% cap.",
        before=str(current),
        limit=str(budget),
    )


def rule_strategy_budget(ctx: RiskContext, limits: LimitSet, new_risk_pct: Decimal) -> RuleOutcome:
    """Per-strategy share of the portfolio budget (ADR-0007).

    Stops one strategy consuming the whole book, which matters far more once
    dozens are running.
    """
    budget = limits.max_strategy_budget_pct
    if budget is None:
        return _ok(RejectionCode.MAX_STRATEGY_BUDGET)
    current = ctx.portfolio.risk_by_strategy(ctx.proposal.strategy_id)
    if current + new_risk_pct <= budget:
        return _ok(RejectionCode.MAX_STRATEGY_BUDGET)
    return _clamp(
        RejectionCode.MAX_STRATEGY_BUDGET,
        _scale_to_budget(Decimal(1), current, new_risk_pct, budget),
        f"Strategy {ctx.proposal.strategy_id} holds {current}%; adding "
        f"{new_risk_pct}% exceeds its {budget}% share.",
        before=str(current),
        limit=str(budget),
    )


# ---------------------------------------------------------------------------
# Tier D — quality gates.
# ---------------------------------------------------------------------------


def rule_min_reward_risk(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    minimum = limits.min_reward_risk
    if minimum is None:
        return _ok(RejectionCode.BELOW_MIN_REWARD_RISK)
    ratio = ctx.proposal.reward_risk
    if ratio is None:
        return _reject(
            RejectionCode.BELOW_MIN_REWARD_RISK,
            f"No target given, so reward:risk cannot be verified against the {minimum} minimum.",
            limit=str(minimum),
        )
    if ratio < minimum:
        return _reject(
            RejectionCode.BELOW_MIN_REWARD_RISK,
            f"Reward:risk {ratio:.2f} is below the {minimum} minimum.",
            before=f"{ratio:.2f}",
            limit=str(minimum),
        )
    return _ok(RejectionCode.BELOW_MIN_REWARD_RISK)


def rule_min_confidence(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    minimum = limits.min_confidence
    if minimum is None or ctx.proposal.confidence >= minimum:
        return _ok(RejectionCode.BELOW_MIN_CONFIDENCE)
    return _reject(
        RejectionCode.BELOW_MIN_CONFIDENCE,
        f"Confidence {ctx.proposal.confidence} is below the {minimum} minimum.",
        before=str(ctx.proposal.confidence),
        limit=str(minimum),
    )


def rule_stop_within_atr_bounds(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    atr = ctx.market.atr
    if atr is None or atr <= 0:
        return _ok(RejectionCode.STOP_TOO_TIGHT)
    distance = abs(ctx.proposal.entry - ctx.proposal.stop)
    multiple = distance / atr

    low, high = limits.min_stop_atr_multiple, limits.max_stop_atr_multiple
    if low is not None and multiple < low:
        return _reject(
            RejectionCode.STOP_TOO_TIGHT,
            f"Stop is {multiple:.2f}× ATR, below the {low}× minimum — likely to be hit by noise.",
            before=f"{multiple:.2f}",
            limit=str(low),
        )
    if high is not None and multiple > high:
        return _reject(
            RejectionCode.STOP_TOO_WIDE,
            f"Stop is {multiple:.2f}× ATR, above the {high}× maximum.",
            before=f"{multiple:.2f}",
            limit=str(high),
        )
    return _ok(RejectionCode.STOP_TOO_TIGHT)


def rule_broker_stop_level(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    spec = ctx.market.spec
    if spec.stop_level_points <= 0:
        return _ok(RejectionCode.STOP_INSIDE_BROKER_STOP_LEVEL)
    minimum = Decimal(spec.stop_level_points) * (spec.pip_size / 10)
    distance = abs(ctx.proposal.entry - ctx.proposal.stop)
    if distance < minimum:
        return _reject(
            RejectionCode.STOP_INSIDE_BROKER_STOP_LEVEL,
            f"Stop is {distance} from entry, inside the broker's {minimum} minimum.",
        )
    return _ok(RejectionCode.STOP_INSIDE_BROKER_STOP_LEVEL)


def rule_cooldown(ctx: RiskContext, limits: LimitSet, _s: Decimal) -> RuleOutcome:
    if ctx.cooldown_active:
        return _reject(RejectionCode.DAILY_LOSS_COOLDOWN, "A loss cooldown is active.")
    threshold = limits.loss_streak_cooldown_after
    if threshold is not None and ctx.account.consecutive_losses >= threshold:
        return _reject(
            RejectionCode.CONSECUTIVE_LOSS_COOLDOWN,
            f"{ctx.account.consecutive_losses} consecutive losses; the profile "
            f"pauses at {threshold}.",
            before=str(ctx.account.consecutive_losses),
            limit=str(threshold),
        )
    return _ok(RejectionCode.CONSECUTIVE_LOSS_COOLDOWN)


def rule_strategy_approved(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if not ctx.strategy_approved:
        return _reject(
            RejectionCode.STRATEGY_NOT_APPROVED,
            f"Strategy version {ctx.proposal.strategy_version} is not approved for "
            f"{ctx.mode.value} mode.",
        )
    return _ok(RejectionCode.STRATEGY_NOT_APPROVED)


def rule_instrument_approved(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if not ctx.instrument_approved:
        return _reject(
            RejectionCode.INSTRUMENT_NOT_APPROVED,
            f"{ctx.proposal.instrument} is not in the approved instrument list.",
        )
    return _ok(RejectionCode.INSTRUMENT_NOT_APPROVED)


def rule_session_approved(ctx: RiskContext, _l: LimitSet, _s: Decimal) -> RuleOutcome:
    if not ctx.session_approved:
        return _reject(
            RejectionCode.SESSION_NOT_APPROVED,
            f"{ctx.market.session.value} is not permitted by the active profile.",
        )
    return _ok(RejectionCode.SESSION_NOT_APPROVED)


#: Registry, in reporting order. Tier A first so a rejection reads top-down, but
#: order does not affect the outcome — every rule runs regardless (I5).
BLOCKING_RULES: tuple[Rule, ...] = (
    rule_kill_switch,
    rule_emergency_shutdown,
    rule_mode_permits_execution,
    rule_market_data_valid,
    rule_weekend,
    rule_rollover,
    rule_news_window,
    rule_abnormal_spread,
    rule_abnormal_volatility,
    rule_account_reconciled,
    rule_duplicate_order,
)

ACCOUNT_RULES: tuple[Rule, ...] = (
    rule_daily_loss,
    rule_total_loss,
    rule_drawdown_block,
)

#: Rules taking the projected loss at stop rather than a risk percentage.
PROJECTED_LOSS_RULES: tuple[Rule, ...] = (
    rule_daily_loss_would_breach,
    rule_total_loss_would_breach,
)

EXPOSURE_RULES: tuple[Rule, ...] = (
    rule_max_open_risk,
    rule_max_positions,
    rule_max_trades_per_session,
    rule_instrument_exposure,
    rule_currency_exposure,
    rule_correlated_exposure,
    rule_strategy_budget,
)

QUALITY_RULES: tuple[Rule, ...] = (
    rule_min_reward_risk,
    rule_min_confidence,
    rule_stop_within_atr_bounds,
    rule_broker_stop_level,
    rule_cooldown,
    rule_strategy_approved,
    rule_instrument_approved,
    rule_session_approved,
)

ALL_RULE_COUNT: int = (
    len(BLOCKING_RULES)
    + len(ACCOUNT_RULES)
    + len(PROJECTED_LOSS_RULES)
    + len(EXPOSURE_RULES)
    + len(QUALITY_RULES)
)
