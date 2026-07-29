"""Set-based evaluation (I11). Reproducibility is the property that matters."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from nemonis_risk.portfolio import PortfolioRiskEngine, rank_proposals
from nemonis_schemas.enums import RejectionCode

from tests.risk.conftest import make_context, make_market, make_proposal

pytestmark = pytest.mark.risk


@pytest.fixture
def portfolio_engine() -> PortfolioRiskEngine:
    return PortfolioRiskEngine()


def ctx_for(
    pid: str, *, confidence: str = "0.75", instrument: str = "EURUSD", entry: str = "1.08500"
):
    """A context whose proposal differs from others by id, price and confidence."""
    entry_d = Decimal(entry)
    stop = entry_d - Decimal("0.00300")
    target = entry_d + Decimal("0.00900")
    return make_context(
        proposal=make_proposal(
            proposal_id=pid,
            strategy_id=f"strategy-{pid}",
            instrument=instrument,
            entry=entry_d,
            stop=stop,
            target=target,
            confidence=Decimal(confidence),
        ),
        market=make_market(instrument=instrument, bid=entry_d, ask=entry_d + Decimal("0.00008")),
    )


class TestDeterministicOrdering:
    """I11 — resolution must not depend on arrival order."""

    def test_ranking_is_by_confidence_descending(self) -> None:
        contexts = [
            ctx_for("a", confidence="0.60"),
            ctx_for("b", confidence="0.90"),
            ctx_for("c", confidence="0.75"),
        ]
        ordered = rank_proposals(contexts)
        assert [c.proposal.proposal_id for c in ordered] == ["b", "c", "a"]

    def test_shuffling_the_input_does_not_change_the_ranking(self) -> None:
        contexts = [ctx_for(str(i), confidence="0.70") for i in range(8)]
        baseline = [c.proposal.proposal_id for c in rank_proposals(contexts)]

        rng = random.Random(1234)
        for _ in range(20):
            shuffled = contexts[:]
            rng.shuffle(shuffled)
            assert [c.proposal.proposal_id for c in rank_proposals(shuffled)] == baseline

    def test_equal_confidence_breaks_on_content_hash_not_position(self) -> None:
        """A tie must resolve on the trade's economics, which are stable, rather
        than on list position, which is not."""
        a = ctx_for("a", confidence="0.70", entry="1.08500")
        b = ctx_for("b", confidence="0.70", entry="1.09500")
        forward = [c.proposal.proposal_id for c in rank_proposals([a, b])]
        backward = [c.proposal.proposal_id for c in rank_proposals([b, a])]
        assert forward == backward

    def test_whole_evaluation_is_order_independent(self, portfolio_engine, now) -> None:
        """The headline reproducibility guarantee: same set, same outcome."""
        contexts = [
            ctx_for("p1", confidence="0.90"),
            ctx_for("p2", confidence="0.80", instrument="GBPUSD", entry="1.26500"),
            ctx_for("p3", confidence="0.70", instrument="AUDUSD", entry="0.65500"),
            ctx_for("p4", confidence="0.60", instrument="NZDUSD", entry="0.60500"),
        ]
        baseline = portfolio_engine.evaluate_set(contexts, evaluated_at=now)

        rng = random.Random(99)
        for _ in range(10):
            shuffled = contexts[:]
            rng.shuffle(shuffled)
            result = portfolio_engine.evaluate_set(shuffled, evaluated_at=now)
            assert result.ranking == baseline.ranking
            assert result.total_approved_risk_pct == baseline.total_approved_risk_pct
            for pid in baseline.ranking:
                mine = result.for_proposal(pid)
                theirs = baseline.for_proposal(pid)
                assert mine is not None
                assert theirs is not None
                assert mine.final_size_lots == theirs.final_size_lots
                assert mine.verdict == theirs.verdict


class TestSharedBudget:
    def test_earlier_approvals_consume_the_budget(self, portfolio_engine, now) -> None:
        """Each proposal sees what higher-ranked ones already committed."""
        contexts = [
            ctx_for(f"p{i}", confidence=f"0.{95 - i}", instrument=sym, entry=price)
            for i, (sym, price) in enumerate(
                [
                    ("EURUSD", "1.08500"),
                    ("GBPUSD", "1.26500"),
                    ("AUDUSD", "0.65500"),
                    ("NZDUSD", "0.60500"),
                ]
            )
        ]
        result = portfolio_engine.evaluate_set(contexts, evaluated_at=now)
        sizes = [result.for_proposal(pid).final_size_lots for pid in result.ranking]
        # Later proposals face a progressively smaller remaining budget.
        assert sizes[0] >= sizes[-1]

    def test_set_total_respects_the_open_risk_budget(self, portfolio_engine, now) -> None:
        """The joint-breach case: proposals that individually pass must not
        collectively exceed the budget."""
        contexts = [
            ctx_for(f"p{i}", confidence="0.80", instrument=sym, entry=price)
            for i, (sym, price) in enumerate(
                [
                    ("EURUSD", "1.08500"),
                    ("GBPUSD", "1.26500"),
                    ("AUDUSD", "0.65500"),
                    ("NZDUSD", "0.60500"),
                ]
            )
        ]
        result = portfolio_engine.evaluate_set(contexts, evaluated_at=now)
        # CHALLENGE allows 1.50% open risk.
        assert result.total_approved_risk_pct <= Decimal("1.50")

    def test_each_proposal_alone_would_pass(self, portfolio_engine, now) -> None:
        """Establishes that the joint constraint is what bound them, not an
        individual defect — without this the test above proves nothing."""
        for sym, price in [("EURUSD", "1.08500"), ("GBPUSD", "1.26500")]:
            single = portfolio_engine.evaluate_set(
                [ctx_for("solo", instrument=sym, entry=price)], evaluated_at=now
            )
            assert single.decisions[0].is_approved

    def test_budget_exhaustion_reduces_then_rejects(self, portfolio_engine, now) -> None:
        """Many competing proposals: the tail gets nothing rather than the set
        collectively overspending."""
        contexts = [
            ctx_for(f"p{i}", confidence=f"0.{90 - i}", instrument=sym, entry=price)
            for i, (sym, price) in enumerate(
                [
                    ("EURUSD", "1.08500"),
                    ("GBPUSD", "1.26500"),
                    ("AUDUSD", "0.65500"),
                    ("NZDUSD", "0.60500"),
                    ("USDCAD", "1.37500"),
                    ("USDCHF", "0.89500"),
                ]
            )
        ]
        result = portfolio_engine.evaluate_set(contexts, evaluated_at=now)
        assert result.total_approved_risk_pct <= Decimal("1.50")
        assert len(result.approved) < len(contexts)


class TestEdgeCases:
    def test_empty_set(self, portfolio_engine, now) -> None:
        result = portfolio_engine.evaluate_set([], evaluated_at=now)
        assert result.decisions == ()
        assert result.total_approved_risk_pct == Decimal(0)

    def test_single_proposal_matches_solo_evaluation(self, portfolio_engine, now) -> None:
        from nemonis_risk.engine import RiskEngine

        ctx = ctx_for("solo")
        as_set = portfolio_engine.evaluate_set([ctx], evaluated_at=now).decisions[0]
        alone = RiskEngine().evaluate(ctx, evaluated_at=now)
        assert as_set.final_size_lots == alone.final_size_lots
        assert as_set.verdict == alone.verdict

    def test_a_rejected_proposal_consumes_no_budget(self, portfolio_engine, now) -> None:
        blocked = make_context(
            proposal=make_proposal(proposal_id="blocked", confidence=Decimal("0.99")),
            kill_switch_engaged=True,
        )
        good = ctx_for("good", confidence="0.50")

        with_blocked = portfolio_engine.evaluate_set([blocked, good], evaluated_at=now)
        alone = portfolio_engine.evaluate_set([good], evaluated_at=now)

        assert with_blocked.for_proposal("blocked").verdict.value == "REJECTED"
        assert (
            with_blocked.for_proposal("good").final_size_lots
            == alone.for_proposal("good").final_size_lots
        )

    def test_all_rejected_yields_zero_committed(self, portfolio_engine, now) -> None:
        contexts = [
            make_context(proposal=make_proposal(proposal_id=f"p{i}"), kill_switch_engaged=True)
            for i in range(3)
        ]
        result = portfolio_engine.evaluate_set(contexts, evaluated_at=now)
        assert result.total_approved_risk_pct == Decimal(0)
        assert not result.approved


class TestCorrelationAcrossTheSet:
    def test_correlated_proposals_are_jointly_constrained(self, portfolio_engine, now) -> None:
        """Three long USD-major proposals are one short-USD bet. The set must
        recognise that, not treat them as three independent trades."""
        contexts = [
            ctx_for("eur", confidence="0.90", instrument="EURUSD", entry="1.08500"),
            ctx_for("gbp", confidence="0.85", instrument="GBPUSD", entry="1.26500"),
            ctx_for("aud", confidence="0.80", instrument="AUDUSD", entry="0.65500"),
        ]
        result = portfolio_engine.evaluate_set(contexts, evaluated_at=now)

        # The correlated cap (0.75% on CHALLENGE) binds well before the 1.50%
        # open-risk budget — three trades at 0.35% would only reach 1.05%.
        assert result.total_approved_risk_pct <= Decimal("0.80")
        bound = [d for d in result.decisions if d.was_reduced or not d.is_approved]
        assert bound, "correlated exposure should have bound at least one proposal"
        assert any(
            RejectionCode.MAX_CORRELATED_EXPOSURE in d.reason_codes for d in result.decisions
        )
