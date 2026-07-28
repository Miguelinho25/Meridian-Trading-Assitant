"""The risk engine. Every invariant in risk-engine.md §1 is asserted here."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from meridian_config.settings import ApprovalMode, Mode, RiskProfileName
from meridian_risk.context import OpenPosition, PortfolioState
from meridian_risk.decision import (
    DecisionForgeryError,
    RiskDecision,
)
from meridian_risk.engine import RiskEngine
from meridian_schemas.enums import DataQualityVerdict, Direction, RejectionCode, RiskVerdict

from tests.risk.conftest import (
    good_quality,
    make_account,
    make_context,
    make_market,
    make_proposal,
)

pytestmark = pytest.mark.risk


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine()


class TestHappyPath:
    def test_a_clean_proposal_is_approved(self, engine, ctx, now) -> None:
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.is_approved, decision.explanation
        assert decision.final_size_lots > 0

    def test_approved_size_respects_requested_risk(self, engine, ctx, now) -> None:
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.final_risk_pct <= ctx.proposal.requested_risk_pct

    def test_decision_records_provenance(self, engine, ctx, now) -> None:
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.rule_profile_version
        assert decision.rules_evaluated > 25
        assert decision.evaluated_at == now


class TestInvariantI1AndI8_Authorisation:
    """The decision token is the whole basis of risk-engine finality."""

    def test_decision_cannot_be_forged(self) -> None:
        """I8 — a strategy must not be able to fabricate an approval."""
        with pytest.raises(DecisionForgeryError, match="only be created by the risk engine"):
            RiskDecision(
                decision_id="rd_fake",
                proposal_id="prp_x",
                proposal_hash="sha256:whatever",
                verdict=RiskVerdict.APPROVED,
                requested_size_lots=Decimal("10"),
                final_size_lots=Decimal("10"),
                requested_risk_pct=Decimal("50"),
                final_risk_pct=Decimal("50"),
                risk_amount_account_ccy=Decimal("50000"),
                binding_constraint=None,
                reason_codes=(),
                explanation="forged",
                before_after={},
                rules_evaluated=0,
                rules_passed=0,
                rule_profile_version="fake",
                prop_profile_version=None,
                evaluated_at=None,  # type: ignore[arg-type]
            )

    def test_authorises_the_exact_order_it_sized(self, engine, ctx, now) -> None:
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.authorises(
            proposal_hash=ctx.proposal.content_hash, size_lots=decision.final_size_lots
        )

    def test_size_inflation_is_not_authorised(self, engine, ctx, now) -> None:
        """The realistic attack: approve 0.2 lots, submit 2.0."""
        decision = engine.evaluate(ctx, evaluated_at=now)
        inflated = decision.final_size_lots * 10
        assert not decision.authorises(proposal_hash=ctx.proposal.content_hash, size_lots=inflated)

    def test_mutated_proposal_is_not_authorised(self, engine, ctx, now) -> None:
        """Changing the stop after approval changes the risk entirely."""
        decision = engine.evaluate(ctx, evaluated_at=now)
        mutated = replace(ctx.proposal, stop=Decimal("1.05000"))
        assert mutated.content_hash != ctx.proposal.content_hash
        assert not decision.authorises(
            proposal_hash=mutated.content_hash, size_lots=decision.final_size_lots
        )

    def test_a_rejected_decision_authorises_nothing(self, engine, now) -> None:
        ctx = make_context(kill_switch_engaged=True)
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert not decision.is_approved
        assert not decision.authorises(
            proposal_hash=ctx.proposal.content_hash, size_lots=Decimal("0.1")
        )

    def test_authorisation_hash_changes_with_size(self, engine, ctx, now) -> None:
        from meridian_risk.decision import build_decision

        decision = engine.evaluate(ctx, evaluated_at=now)
        other = build_decision(
            **{
                **{
                    k: getattr(decision, k)
                    for k in (
                        "decision_id",
                        "proposal_id",
                        "proposal_hash",
                        "verdict",
                        "requested_size_lots",
                        "requested_risk_pct",
                        "final_risk_pct",
                        "risk_amount_account_ccy",
                        "binding_constraint",
                        "reason_codes",
                        "explanation",
                        "before_after",
                        "rules_evaluated",
                        "rules_passed",
                        "rule_profile_version",
                        "prop_profile_version",
                        "evaluated_at",
                        "outcomes",
                    )
                },
                "final_size_lots": decision.final_size_lots + Decimal("0.01"),
            }
        )
        assert other.authorisation_hash != decision.authorisation_hash

    def test_cosmetic_fields_do_not_break_the_binding(self, ctx) -> None:
        """Confidence does not change the trade's risk, so binding it would
        invalidate tokens on harmless edits without adding safety."""
        tweaked = replace(ctx.proposal, confidence=Decimal("0.99"), setup_type="other")
        assert tweaked.content_hash == ctx.proposal.content_hash


class TestInvariantI5_TotalEvaluation:
    def test_all_rules_run_even_after_a_rejection(self, engine, now) -> None:
        """A rejection report must list every reason, not just the first."""
        ctx = make_context(
            kill_switch_engaged=True,
            account=make_account(consecutive_losses=10),
            market=make_market(is_weekend=True, spread_multiple=Decimal("8")),
        )
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.rules_evaluated > 25
        assert len(decision.reason_codes) >= 3
        assert RejectionCode.KILL_SWITCH_ENGAGED in decision.reason_codes
        assert RejectionCode.WEEKEND_BLOCK in decision.reason_codes


class TestInvariantI6_Determinism:
    def test_same_input_same_decision(self, engine, ctx, now) -> None:
        a = engine.evaluate(ctx, evaluated_at=now)
        b = engine.evaluate(ctx, evaluated_at=now)
        assert a.verdict == b.verdict
        assert a.final_size_lots == b.final_size_lots
        assert a.reason_codes == b.reason_codes
        assert a.authorisation_hash == b.authorisation_hash

    def test_independent_engines_agree(self, ctx, now) -> None:
        assert (
            RiskEngine().evaluate(ctx, evaluated_at=now).final_size_lots
            == RiskEngine().evaluate(ctx, evaluated_at=now).final_size_lots
        )


class TestTierA_BlockingGates:
    @pytest.mark.parametrize(
        ("kwargs", "code"),
        [
            ({"kill_switch_engaged": True}, RejectionCode.KILL_SWITCH_ENGAGED),
            ({"emergency_shutdown": True}, RejectionCode.EMERGENCY_SHUTDOWN),
            ({"mode": Mode.RESEARCH}, RejectionCode.MODE_FORBIDS_EXECUTION),
            (
                {"approval_mode": ApprovalMode.OBSERVE_ONLY},
                RejectionCode.MODE_FORBIDS_EXECUTION,
            ),
            ({"strategy_approved": False}, RejectionCode.STRATEGY_NOT_APPROVED),
            ({"instrument_approved": False}, RejectionCode.INSTRUMENT_NOT_APPROVED),
            ({"session_approved": False}, RejectionCode.SESSION_NOT_APPROVED),
            ({"cooldown_active": True}, RejectionCode.DAILY_LOSS_COOLDOWN),
        ],
    )
    def test_gate_blocks(self, engine, now, kwargs, code) -> None:
        decision = engine.evaluate(make_context(**kwargs), evaluated_at=now)
        assert decision.verdict is RiskVerdict.REJECTED
        assert code in decision.reason_codes
        assert decision.final_size_lots == 0

    def test_weekend_blocks(self, engine, now) -> None:
        ctx = make_context(market=make_market(is_weekend=True))
        assert RejectionCode.WEEKEND_BLOCK in engine.evaluate(ctx, evaluated_at=now).reason_codes

    def test_rollover_blocks(self, engine, now) -> None:
        ctx = make_context(market=make_market(is_rollover=True))
        assert RejectionCode.ROLLOVER_BLOCK in engine.evaluate(ctx, evaluated_at=now).reason_codes

    def test_stale_data_blocks(self, engine, now) -> None:
        bad = replace(good_quality(), verdict=DataQualityVerdict.DEGRADED)
        ctx = make_context(market=make_market(quality=bad))
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.verdict is RiskVerdict.REJECTED

    def test_invalid_data_blocks(self, engine, now) -> None:
        bad = replace(good_quality(), verdict=DataQualityVerdict.INVALID)
        ctx = make_context(market=make_market(quality=bad))
        assert (
            RejectionCode.MARKET_DATA_INVALID in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_abnormal_spread_blocks(self, engine, now) -> None:
        ctx = make_context(market=make_market(spread_multiple=Decimal("6")))
        assert RejectionCode.ABNORMAL_SPREAD in engine.evaluate(ctx, evaluated_at=now).reason_codes

    def test_unreconciled_account_blocks(self, engine, now) -> None:
        ctx = make_context(account=make_account(is_reconciled=False))
        assert (
            RejectionCode.ACCOUNT_STATE_AMBIGUOUS
            in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_duplicate_order_blocks(self, engine, now) -> None:
        proposal = make_proposal()
        ctx = make_context(
            proposal=proposal, active_order_hashes=frozenset({proposal.content_hash})
        )
        assert RejectionCode.DUPLICATE_ORDER in engine.evaluate(ctx, evaluated_at=now).reason_codes

    def test_unknown_news_proximity_blocks(self, engine, now) -> None:
        """Unknown is not clear — the news gate fails closed."""
        ctx = make_context(market=make_market(minutes_to_news=None))
        assert (
            RejectionCode.NEWS_WINDOW_BLOCK in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_inside_news_buffer_blocks(self, engine, now) -> None:
        ctx = make_context(market=make_market(minutes_to_news=5))
        assert (
            RejectionCode.NEWS_WINDOW_BLOCK in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )


class TestTierB_AccountLimits:
    def test_daily_loss_reached_blocks(self, engine, now) -> None:
        ctx = make_context(account=make_account(daily_loss_used=Decimal("5000")))
        assert (
            RejectionCode.DAILY_LOSS_LIMIT_REACHED
            in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_projected_breach_blocks_before_it_happens(self, engine, now) -> None:
        """Blocking only on breaches already incurred would be too late."""
        ctx = make_context(account=make_account(daily_loss_used=Decimal("4900")))
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert RejectionCode.DAILY_LOSS_WOULD_BREACH in decision.reason_codes

    def test_total_loss_reached_blocks(self, engine, now) -> None:
        ctx = make_context(account=make_account(total_loss_used=Decimal("10000")))
        assert (
            RejectionCode.TOTAL_LOSS_LIMIT_REACHED
            in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_deep_drawdown_blocks_new_trades(self, engine, now) -> None:
        ctx = make_context(account=make_account(drawdown_consumed=Decimal("0.80")))
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.final_size_lots == 0


class TestTierC_ExposureClamps:
    def test_open_risk_budget_reduces_size(self, engine, now) -> None:
        crowded = PortfolioState(
            open_positions=(
                OpenPosition(
                    instrument="GBPUSD",
                    direction=Direction.LONG,
                    lots=Decimal("1"),
                    entry=Decimal("1.265"),
                    stop=Decimal("1.260"),
                    strategy_id="other",
                    open_risk_pct=Decimal("1.30"),
                ),
            )
        )
        decision = engine.evaluate(make_context(portfolio=crowded), evaluated_at=now)
        baseline = engine.evaluate(make_context(), evaluated_at=now)
        assert decision.final_size_lots < baseline.final_size_lots

    def test_position_count_cap_blocks(self, engine, now) -> None:
        positions = tuple(
            OpenPosition(
                instrument=sym,
                direction=Direction.LONG,
                lots=Decimal("0.1"),
                entry=Decimal("1"),
                stop=Decimal("0.99"),
                strategy_id="s",
                open_risk_pct=Decimal("0.05"),
            )
            for sym in ("GBPUSD", "USDJPY", "AUDUSD", "NZDUSD")
        )
        ctx = make_context(portfolio=PortfolioState(open_positions=positions))
        assert (
            RejectionCode.MAX_SIMULTANEOUS_POSITIONS
            in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_session_trade_cap_blocks(self, engine, now) -> None:
        ctx = make_context(account=make_account(trades_this_session=5))
        assert (
            RejectionCode.MAX_TRADES_PER_SESSION
            in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )

    def test_pending_risk_counts_against_the_budget(self, engine, now) -> None:
        """Proposals approved earlier in the same set are committed risk, even
        with no fill yet — otherwise a set could jointly overspend."""
        pending = PortfolioState(pending_risk_pct=Decimal("1.40"))
        decision = engine.evaluate(make_context(portfolio=pending), evaluated_at=now)
        baseline = engine.evaluate(make_context(), evaluated_at=now)
        assert decision.final_size_lots < baseline.final_size_lots


class TestTierD_QualityGates:
    def test_poor_reward_risk_blocks(self, engine, now) -> None:
        weak = make_proposal(target=Decimal("1.08600"))  # 0.33:1
        assert (
            RejectionCode.BELOW_MIN_REWARD_RISK
            in engine.evaluate(make_context(proposal=weak), evaluated_at=now).reason_codes
        )

    def test_missing_target_blocks(self, engine, now) -> None:
        """Unverifiable reward:risk is not the same as acceptable reward:risk."""
        assert (
            RejectionCode.BELOW_MIN_REWARD_RISK
            in engine.evaluate(
                make_context(proposal=make_proposal(target=None)), evaluated_at=now
            ).reason_codes
        )

    def test_low_confidence_blocks(self, engine, now) -> None:
        shy = make_proposal(confidence=Decimal("0.20"))
        assert (
            RejectionCode.BELOW_MIN_CONFIDENCE
            in engine.evaluate(make_context(proposal=shy), evaluated_at=now).reason_codes
        )

    def test_stop_too_tight_for_atr_blocks(self, engine, now) -> None:
        tight = make_proposal(stop=Decimal("1.08490"), target=Decimal("1.08800"))
        assert (
            RejectionCode.STOP_TOO_TIGHT
            in engine.evaluate(make_context(proposal=tight), evaluated_at=now).reason_codes
        )

    def test_stop_too_wide_for_atr_blocks(self, engine, now) -> None:
        wide = make_proposal(stop=Decimal("1.02000"), target=Decimal("1.30000"))
        assert (
            RejectionCode.STOP_TOO_WIDE
            in engine.evaluate(make_context(proposal=wide), evaluated_at=now).reason_codes
        )

    def test_loss_streak_triggers_cooldown(self, engine, now) -> None:
        ctx = make_context(account=make_account(consecutive_losses=3))
        assert (
            RejectionCode.CONSECUTIVE_LOSS_COOLDOWN
            in engine.evaluate(ctx, evaluated_at=now).reason_codes
        )


class TestTierE_DrawdownThrottle:
    def test_shallow_drawdown_does_not_reduce_size(self, engine, now) -> None:
        decision = engine.evaluate(
            make_context(account=make_account(drawdown_consumed=Decimal("0.10"))),
            evaluated_at=now,
        )
        assert decision.verdict is RiskVerdict.APPROVED

    def test_moderate_drawdown_reduces_size(self, engine, now) -> None:
        shallow = engine.evaluate(
            make_context(account=make_account(drawdown_consumed=Decimal("0.10"))),
            evaluated_at=now,
        )
        deeper = engine.evaluate(
            make_context(account=make_account(drawdown_consumed=Decimal("0.30"))),
            evaluated_at=now,
        )
        assert deeper.final_size_lots < shallow.final_size_lots
        assert deeper.verdict is RiskVerdict.APPROVED_REDUCED

    def test_size_decreases_monotonically_with_drawdown(self, engine, now) -> None:
        sizes = [
            engine.evaluate(
                make_context(account=make_account(drawdown_consumed=Decimal(str(d)))),
                evaluated_at=now,
            ).final_size_lots
            for d in ("0.05", "0.25", "0.45", "0.65", "0.80")
        ]
        assert sizes == sorted(sizes, reverse=True)

    def test_throttle_is_the_binding_constraint_when_it_binds(self, engine, now) -> None:
        decision = engine.evaluate(
            make_context(account=make_account(drawdown_consumed=Decimal("0.30"))),
            evaluated_at=now,
        )
        assert decision.binding_constraint is RejectionCode.DRAWDOWN_THROTTLE


class TestProfileComposition:
    def test_stricter_profile_gives_smaller_size(self, engine, now) -> None:
        preservation = engine.evaluate(
            make_context(profile_name=RiskProfileName.PRESERVATION), evaluated_at=now
        )
        challenge = engine.evaluate(
            make_context(profile_name=RiskProfileName.CHALLENGE), evaluated_at=now
        )
        assert preservation.final_size_lots < challenge.final_size_lots

    def test_a_greedy_request_is_capped_by_the_profile(self, engine, now) -> None:
        """I2 — a strategy asking for 5% gets the profile's ceiling, not 5%."""
        greedy = make_proposal(requested_risk_pct=Decimal("5.00"))
        decision = engine.evaluate(make_context(proposal=greedy), evaluated_at=now)
        assert decision.is_approved
        assert decision.final_risk_pct <= Decimal("0.35")


class TestExplanation:
    def test_rejection_explains_itself(self, engine, now) -> None:
        decision = engine.evaluate(make_context(kill_switch_engaged=True), evaluated_at=now)
        assert "Rejected" in decision.explanation
        assert "Kill switch" in decision.explanation

    def test_reduction_names_the_binding_constraint(self, engine, now) -> None:
        decision = engine.evaluate(
            make_context(account=make_account(drawdown_consumed=Decimal("0.30"))),
            evaluated_at=now,
        )
        assert "reduced" in decision.explanation.lower()

    def test_before_after_is_populated(self, engine, ctx, now) -> None:
        decision = engine.evaluate(ctx, evaluated_at=now)
        assert "open_risk_pct" in decision.before_after
        assert "before" in decision.before_after["open_risk_pct"]
