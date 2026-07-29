"""Order state machine (architecture.md §7).

Every transition is validated. No component may skip a required state — an order
cannot arrive at FILLED without having been risk-approved, submitted and
accepted, because those states are where the guarantees live.

The transition table is data rather than scattered ``if`` statements so it can be
read, diffed and tested as a whole. A reachability test asserts no state is
stranded and no terminal state has an exit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from nemonis_schemas.enums import OrderState

#: Permitted transitions. Anything absent is refused.
TRANSITIONS: Final[dict[OrderState, frozenset[OrderState]]] = {
    OrderState.DRAFT: frozenset({OrderState.PROPOSED, OrderState.CANCELLED}),
    OrderState.PROPOSED: frozenset({OrderState.RISK_PENDING, OrderState.CANCELLED}),
    # The only two ways out of risk evaluation.
    OrderState.RISK_PENDING: frozenset({OrderState.RISK_APPROVED, OrderState.REJECTED}),
    OrderState.RISK_APPROVED: frozenset(
        {
            OrderState.HUMAN_APPROVAL_PENDING,
            OrderState.APPROVED,  # automated modes skip human review
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.HUMAN_APPROVAL_PENDING: frozenset(
        {OrderState.APPROVED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    OrderState.APPROVED: frozenset(
        {OrderState.SUBMITTED_TO_PAPER_BROKER, OrderState.CANCELLED, OrderState.EXPIRED}
    ),
    OrderState.SUBMITTED_TO_PAPER_BROKER: frozenset({OrderState.ACCEPTED, OrderState.REJECTED}),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELLED}
    ),
    OrderState.FILLED: frozenset({OrderState.MANAGED}),
    OrderState.MANAGED: frozenset({OrderState.CLOSED}),
    # Terminal.
    OrderState.CLOSED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}

TERMINAL_STATES: Final[frozenset[OrderState]] = frozenset(
    s for s, targets in TRANSITIONS.items() if not targets
)

#: States in which an order can still become a position.
LIVE_STATES: Final[frozenset[OrderState]] = frozenset(
    {
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.MANAGED,
    }
)


class IllegalTransitionError(RuntimeError):
    """A transition not permitted by the state machine was attempted."""


@dataclass(frozen=True, slots=True)
class Transition:
    from_state: OrderState | None
    to_state: OrderState
    at: datetime
    actor: str
    reason: str = ""


def can_transition(from_state: OrderState, to_state: OrderState) -> bool:
    return to_state in TRANSITIONS.get(from_state, frozenset())


def assert_transition(from_state: OrderState, to_state: OrderState) -> None:
    """Raise unless the transition is permitted.

    The error names the legal targets, because the common mistake is skipping an
    intermediate state rather than inventing an impossible one.
    """
    if not can_transition(from_state, to_state):
        allowed = sorted(s.value for s in TRANSITIONS.get(from_state, frozenset()))
        raise IllegalTransitionError(
            f"{from_state.value} -> {to_state.value} is not a permitted transition. "
            f"From {from_state.value} the order may only go to: "
            f"{', '.join(allowed) or '(terminal)'}."
        )


def is_terminal(state: OrderState) -> bool:
    return state in TERMINAL_STATES


def reachable_from(start: OrderState = OrderState.DRAFT) -> frozenset[OrderState]:
    """Every state reachable from ``start``. Used to prove nothing is stranded."""
    seen: set[OrderState] = set()
    frontier = [start]
    while frontier:
        state = frontier.pop()
        if state in seen:
            continue
        seen.add(state)
        frontier.extend(TRANSITIONS.get(state, frozenset()))
    return frozenset(seen)


@dataclass(slots=True)
class OrderLifecycle:
    """Tracks one order's state and its full transition history.

    The history is the evidence: ``architecture.md`` requires every transition to
    be recorded, and reconstructing it after the fact from a final state is
    impossible.
    """

    state: OrderState = OrderState.DRAFT
    history: list[Transition] = field(default_factory=list)

    def transition(
        self, to: OrderState, *, at: datetime, actor: str, reason: str = ""
    ) -> Transition:
        assert_transition(self.state, to)
        record = Transition(from_state=self.state, to_state=to, at=at, actor=actor, reason=reason)
        self.state = to
        self.history.append(record)
        return record

    @property
    def is_terminal(self) -> bool:
        return is_terminal(self.state)

    @property
    def is_live(self) -> bool:
        return self.state in LIVE_STATES

    def visited(self, state: OrderState) -> bool:
        return state is self.history[0].from_state if self.history else False
