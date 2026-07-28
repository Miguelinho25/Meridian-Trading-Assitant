"""The risk engine — the only path to an order.

Pure and deterministic (I6). Everything arrives in a ``RiskContext``; the engine
reads no clock, opens no connection and touches no database.

Evaluation order:

    1. Compose limits across all four tiers (tightening only, I2).
    2. Apply the drawdown throttle to the requested risk.
    3. Size the position from risk, stop distance and pip value.
    4. Run every rule (I5) — no short-circuit, even after a rejection.
    5. Take the minimum of all clamps.
    6. Re-verify the sizing invariant, then issue a bound decision.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from meridian_schemas.enums import RejectionCode, RiskVerdict
from meridian_schemas.identifiers import IdPrefix, new_id
from meridian_schemas.money import floor_to_step

from meridian_risk.context import RiskContext
from meridian_risk.decision import RiskDecision, RuleOutcome, build_decision
from meridian_risk.forex import (
    ForexError,
    calculate_position_size,
    loss_at_stop,
)
from meridian_risk.limits import LimitSet, compose
from meridian_risk.profiles import (
    PROFILE_VERSION,
    SYSTEM_LIMITS,
    ThrottleBand,
    get_profile,
)
from meridian_risk.rules import (
    ACCOUNT_RULES,
    BLOCKING_RULES,
    EXPOSURE_RULES,
    PROJECTED_LOSS_RULES,
    QUALITY_RULES,
)


class RiskEngine:
    """Evaluates proposals. Holds no mutable state between calls."""

    def __init__(
        self,
        *,
        account_limits: LimitSet | None = None,
        strategy_limits: dict[str, LimitSet] | None = None,
        prop_profile_version: str | None = None,
    ) -> None:
        self.account_limits = account_limits or LimitSet()
        self.strategy_limits = strategy_limits or {}
        self.prop_profile_version = prop_profile_version

    # -- Limit composition -------------------------------------------------

    def effective_limits(self, ctx: RiskContext) -> tuple[LimitSet, ThrottleBand]:
        """Compose all four tiers, then apply the drawdown throttle.

        The throttle tightens confidence and reward:risk as drawdown deepens; it
        is applied *after* composition so it can only ever add strictness.
        """
        profile = get_profile(ctx.profile_name)
        band = profile.multiplier_at(ctx.account.drawdown_consumed)

        throttled = LimitSet(
            min_confidence=(
                (profile.limits.min_confidence or Decimal(0)) + band.confidence_uplift
                if band.confidence_uplift
                else None
            ),
            min_reward_risk=(
                (profile.limits.min_reward_risk or Decimal(0)) + band.reward_risk_uplift
                if band.reward_risk_uplift
                else None
            ),
        )

        limits = compose(
            SYSTEM_LIMITS,
            self.account_limits,
            profile.limits,
            self.strategy_limits.get(ctx.proposal.strategy_id, LimitSet()),
            throttled,
        )
        return limits, band

    # -- Evaluation --------------------------------------------------------

    def evaluate(self, ctx: RiskContext, *, evaluated_at: datetime) -> RiskDecision:
        """Evaluate one proposal. Always returns a decision, never raises."""
        limits, band = self.effective_limits(ctx)
        outcomes: list[RuleOutcome] = []

        requested_risk = ctx.proposal.requested_risk_pct
        ceiling = limits.risk_per_trade_pct
        capped_risk = min(requested_risk, ceiling) if ceiling is not None else requested_risk

        # The throttle multiplies risk. A zero multiplier means no new trades,
        # which the Tier B drawdown gate also blocks — belt and braces.
        throttled_risk = capped_risk * band.risk_multiplier

        sizing = None
        sizing_error: RuleOutcome | None = None
        if throttled_risk > 0:
            try:
                sizing = calculate_position_size(
                    spec=ctx.market.spec,
                    equity=ctx.account.equity,
                    risk_pct=throttled_risk,
                    entry=ctx.proposal.entry,
                    stop=ctx.proposal.stop,
                    direction=ctx.proposal.direction,
                    account_ccy=ctx.account.currency,
                    rates=ctx.rates,
                )
            except ForexError as exc:
                sizing_error = RuleOutcome(code=exc.code, passed=False, detail=str(exc))
        else:
            sizing_error = RuleOutcome(
                code=RejectionCode.DRAWDOWN_THROTTLE,
                passed=False,
                detail=(
                    f"Drawdown throttle multiplier is 0 at "
                    f"{ctx.account.drawdown_consumed:.0%} consumed — no new trades."
                ),
            )

        base_size = sizing.lots if sizing else Decimal(0)
        realised_pct = sizing.realised_risk_pct if sizing else Decimal(0)
        projected_loss = (
            loss_at_stop(
                spec=ctx.market.spec,
                lots=base_size,
                stop_pips=sizing.stop_pips,
                pip_value_per_lot=sizing.pip_value_per_lot,
            )
            if sizing
            else Decimal(0)
        )

        # Every rule runs, always (I5).
        for rule in BLOCKING_RULES + ACCOUNT_RULES + QUALITY_RULES:
            outcomes.append(rule(ctx, limits, realised_pct))
        for rule in PROJECTED_LOSS_RULES:
            outcomes.append(rule(ctx, limits, projected_loss))
        for rule in EXPOSURE_RULES:
            outcomes.append(rule(ctx, limits, realised_pct))
        if sizing_error is not None:
            outcomes.append(sizing_error)

        return self._decide(
            ctx,
            outcomes=outcomes,
            limits=limits,
            band=band,
            base_size=base_size,
            sizing=sizing,
            requested_risk=requested_risk,
            evaluated_at=evaluated_at,
        )

    def _decide(
        self,
        ctx: RiskContext,
        *,
        outcomes: list[RuleOutcome],
        limits: LimitSet,
        band: ThrottleBand,
        base_size: Decimal,
        sizing: object,
        requested_risk: Decimal,
        evaluated_at: datetime,
    ) -> RiskDecision:
        failures = [o for o in outcomes if not o.passed]
        clamps = [o for o in outcomes if o.clamp_to_lots is not None]

        # Smallest clamp wins — the most restrictive rule binds.
        clamp_values = [o.clamp_to_lots for o in outcomes if o.clamp_to_lots is not None]
        scale = min(clamp_values, default=Decimal(1))
        final_size = floor_to_step(base_size * scale, ctx.market.spec.lot_step)

        binding: RejectionCode | None = None
        if failures:
            verdict = RiskVerdict.REJECTED
            final_size = Decimal(0)
            binding = failures[0].code
        elif final_size < ctx.market.spec.min_lot:
            verdict = RiskVerdict.REJECTED
            final_size = Decimal(0)
            binding = RejectionCode.SIZE_BELOW_MINIMUM_LOT
            failures = [
                RuleOutcome(
                    code=RejectionCode.SIZE_BELOW_MINIMUM_LOT,
                    passed=False,
                    detail=(
                        f"After clamping, size falls below the "
                        f"{ctx.market.spec.min_lot} minimum lot."
                    ),
                )
            ]
            outcomes.extend(failures)
        elif final_size < base_size:
            verdict = RiskVerdict.APPROVED_REDUCED
            binding = min(clamps, key=lambda o: o.clamp_to_lots or Decimal(0)).code
        elif band.risk_multiplier < Decimal(1):
            verdict = RiskVerdict.APPROVED_REDUCED
            binding = RejectionCode.DRAWDOWN_THROTTLE
        else:
            verdict = RiskVerdict.APPROVED

        final_risk_pct = Decimal(0)
        risk_amount = Decimal(0)
        if final_size > 0 and sizing is not None:
            ratio = final_size / base_size if base_size > 0 else Decimal(0)
            final_risk_pct = sizing.realised_risk_pct * ratio  # type: ignore[attr-defined]
            risk_amount = sizing.realised_risk_account_ccy * ratio  # type: ignore[attr-defined]

        reason_codes = tuple(
            dict.fromkeys(  # preserve order, drop duplicates
                [o.code for o in failures] + [o.code for o in clamps]
            )
        )

        return build_decision(
            decision_id=new_id(IdPrefix.RISK_DECISION),
            proposal_id=ctx.proposal.proposal_id,
            proposal_hash=ctx.proposal.content_hash,
            verdict=verdict,
            requested_size_lots=base_size,
            final_size_lots=final_size,
            requested_risk_pct=requested_risk,
            final_risk_pct=final_risk_pct,
            risk_amount_account_ccy=risk_amount,
            binding_constraint=binding,
            reason_codes=reason_codes,
            explanation=_explain(ctx, verdict, binding, base_size, final_size, band, failures),
            before_after=_before_after(ctx, final_risk_pct),
            rules_evaluated=len(outcomes),
            rules_passed=len(outcomes) - len(failures),
            rule_profile_version=PROFILE_VERSION,
            prop_profile_version=self.prop_profile_version,
            evaluated_at=evaluated_at,
            outcomes=tuple(outcomes),
        )


def _explain(
    ctx: RiskContext,
    verdict: RiskVerdict,
    binding: RejectionCode | None,
    base_size: Decimal,
    final_size: Decimal,
    band: ThrottleBand,
    failures: list[RuleOutcome],
) -> str:
    """Operator-facing explanation, generated deterministically.

    Templated by code, never by a language model: it must be reproducible from
    stored inputs and must never invent a number.
    """
    if verdict is RiskVerdict.REJECTED:
        reasons = "; ".join(f.detail for f in failures[:4] if f.detail)
        more = f" (+{len(failures) - 4} more)" if len(failures) > 4 else ""
        return f"Rejected: {reasons}{more}"

    if verdict is RiskVerdict.APPROVED_REDUCED:
        note = ""
        if band.risk_multiplier < Decimal(1):
            note = (
                f" Drawdown throttle at {ctx.account.drawdown_consumed:.0%} consumed "
                f"caps risk at {band.risk_multiplier:.0%} of configured."
            )
        return (
            f"Approved with size reduced from {base_size} to {final_size} lots. "
            f"Binding constraint: {binding.value if binding else 'unknown'}.{note}"
        )

    return (
        f"Approved at {final_size} lots, risking "
        f"{ctx.proposal.requested_risk_pct}% on {ctx.proposal.instrument}."
    )


def _before_after(ctx: RiskContext, added_risk_pct: Decimal) -> dict[str, dict[str, str]]:
    open_before = ctx.portfolio.open_risk_pct
    return {
        "open_risk_pct": {
            "before": str(open_before),
            "after": str(open_before + added_risk_pct),
        },
        "daily_loss_used": {
            "before": str(ctx.account.daily_loss_used),
            "after": str(ctx.account.daily_loss_used),
        },
        "drawdown_consumed": {
            "before": f"{ctx.account.drawdown_consumed:.4f}",
            "after": f"{ctx.account.drawdown_consumed:.4f}",
        },
    }
