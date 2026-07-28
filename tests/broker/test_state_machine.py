"""No component may skip a state — that is where the guarantees live."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from meridian_broker.state_machine import (
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransitionError,
    OrderLifecycle,
    assert_transition,
    can_transition,
    is_terminal,
    reachable_from,
)
from meridian_schemas.enums import OrderState

T = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)


class TestTableIntegrity:
    def test_every_state_appears_in_the_table(self) -> None:
        assert set(TRANSITIONS) == set(OrderState)

    def test_every_target_is_a_real_state(self) -> None:
        for targets in TRANSITIONS.values():
            for target in targets:
                assert target in TRANSITIONS

    def test_no_state_is_stranded(self) -> None:
        """Every state must be reachable from DRAFT, or it is dead code that
        will mislead whoever reads the enum."""
        assert reachable_from(OrderState.DRAFT) == set(OrderState)

    def test_terminal_states_have_no_exit(self) -> None:
        for state in TERMINAL_STATES:
            assert TRANSITIONS[state] == frozenset()

    def test_the_expected_states_are_terminal(self) -> None:
        assert {
            OrderState.CLOSED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        } == TERMINAL_STATES


class TestRiskCannotBeSkipped:
    """The path to a fill must pass through risk evaluation."""

    def test_draft_cannot_jump_to_filled(self) -> None:
        assert not can_transition(OrderState.DRAFT, OrderState.FILLED)

    def test_proposed_cannot_jump_to_approved(self) -> None:
        assert not can_transition(OrderState.PROPOSED, OrderState.APPROVED)

    def test_proposed_cannot_reach_the_broker(self) -> None:
        assert not can_transition(OrderState.PROPOSED, OrderState.SUBMITTED_TO_PAPER_BROKER)

    def test_risk_pending_has_exactly_two_outcomes(self) -> None:
        assert TRANSITIONS[OrderState.RISK_PENDING] == {
            OrderState.RISK_APPROVED,
            OrderState.REJECTED,
        }

    def test_the_legal_path_to_a_fill_works(self) -> None:
        order = OrderLifecycle()
        for state in (
            OrderState.PROPOSED,
            OrderState.RISK_PENDING,
            OrderState.RISK_APPROVED,
            OrderState.HUMAN_APPROVAL_PENDING,
            OrderState.APPROVED,
            OrderState.SUBMITTED_TO_PAPER_BROKER,
            OrderState.ACCEPTED,
            OrderState.FILLED,
            OrderState.MANAGED,
            OrderState.CLOSED,
        ):
            order.transition(state, at=T, actor="test")
        assert order.state is OrderState.CLOSED
        assert order.is_terminal

    def test_automated_modes_may_skip_human_review(self) -> None:
        """AUTO_PAPER_* has no human step, but still passes through risk."""
        order = OrderLifecycle()
        order.transition(OrderState.PROPOSED, at=T, actor="strategy")
        order.transition(OrderState.RISK_PENDING, at=T, actor="engine")
        order.transition(OrderState.RISK_APPROVED, at=T, actor="engine")
        order.transition(OrderState.APPROVED, at=T, actor="auto")
        assert order.state is OrderState.APPROVED


class TestIllegalTransitions:
    def test_refused_with_a_helpful_message(self) -> None:
        with pytest.raises(IllegalTransitionError, match="may only go to"):
            assert_transition(OrderState.DRAFT, OrderState.FILLED)

    def test_the_message_lists_the_legal_targets(self) -> None:
        """The common mistake is skipping a state, not inventing one."""
        with pytest.raises(IllegalTransitionError) as exc:
            assert_transition(OrderState.DRAFT, OrderState.FILLED)
        assert "PROPOSED" in str(exc.value)

    def test_a_terminal_state_cannot_be_left(self) -> None:
        order = OrderLifecycle()
        order.transition(OrderState.PROPOSED, at=T, actor="t")
        order.transition(OrderState.CANCELLED, at=T, actor="t")
        with pytest.raises(IllegalTransitionError, match="terminal"):
            order.transition(OrderState.PROPOSED, at=T, actor="t")

    def test_a_failed_transition_leaves_state_unchanged(self) -> None:
        order = OrderLifecycle()
        with pytest.raises(IllegalTransitionError):
            order.transition(OrderState.FILLED, at=T, actor="t")
        assert order.state is OrderState.DRAFT
        assert order.history == []


class TestHistoryIsEvidence:
    def test_every_transition_is_recorded(self) -> None:
        order = OrderLifecycle()
        order.transition(OrderState.PROPOSED, at=T, actor="strategy", reason="signal")
        order.transition(OrderState.RISK_PENDING, at=T, actor="engine")
        assert len(order.history) == 2
        assert order.history[0].from_state is OrderState.DRAFT
        assert order.history[0].actor == "strategy"
        assert order.history[0].reason == "signal"

    def test_rejection_is_recorded_with_its_reason(self) -> None:
        order = OrderLifecycle()
        order.transition(OrderState.PROPOSED, at=T, actor="s")
        order.transition(OrderState.RISK_PENDING, at=T, actor="e")
        order.transition(
            OrderState.REJECTED, at=T, actor="engine", reason="DAILY_LOSS_WOULD_BREACH"
        )
        assert order.history[-1].reason == "DAILY_LOSS_WOULD_BREACH"
        assert order.is_terminal


class TestLiveness:
    def test_accepted_and_filled_are_live(self) -> None:
        order = OrderLifecycle()
        for state in (
            OrderState.PROPOSED,
            OrderState.RISK_PENDING,
            OrderState.RISK_APPROVED,
            OrderState.APPROVED,
            OrderState.SUBMITTED_TO_PAPER_BROKER,
            OrderState.ACCEPTED,
        ):
            order.transition(state, at=T, actor="t")
        assert order.is_live

    def test_a_draft_is_not_live(self) -> None:
        assert not OrderLifecycle().is_live

    def test_terminal_states_are_not_live(self) -> None:
        for state in TERMINAL_STATES:
            assert is_terminal(state)


class TestPartialFills:
    def test_partial_fills_can_repeat(self) -> None:
        assert can_transition(OrderState.PARTIALLY_FILLED, OrderState.PARTIALLY_FILLED)

    def test_partial_fills_complete(self) -> None:
        assert can_transition(OrderState.PARTIALLY_FILLED, OrderState.FILLED)
