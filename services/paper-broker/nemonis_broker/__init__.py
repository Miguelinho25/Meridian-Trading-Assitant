"""Paper broker: order state machine, fill model and accounting."""

from __future__ import annotations

from nemonis_broker.account import Account, Position, ReconciliationError
from nemonis_broker.broker import (
    AuthorisationError,
    BrokerState,
    ClosedTrade,
    Incident,
    PaperBroker,
    WorkingOrder,
)
from nemonis_broker.fills import (
    FillModel,
    FillReason,
    FillResult,
    SlippageModel,
    commission_for,
    fill_for_order,
    fill_limit,
    fill_market,
    fill_stop,
    resolve_exit,
)
from nemonis_broker.state_machine import (
    LIVE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    IllegalTransitionError,
    OrderLifecycle,
    Transition,
    assert_transition,
    can_transition,
    is_terminal,
    reachable_from,
)

__all__ = [
    "LIVE_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Account",
    "AuthorisationError",
    "BrokerState",
    "ClosedTrade",
    "FillModel",
    "FillReason",
    "FillResult",
    "IllegalTransitionError",
    "Incident",
    "OrderLifecycle",
    "PaperBroker",
    "Position",
    "ReconciliationError",
    "SlippageModel",
    "Transition",
    "WorkingOrder",
    "assert_transition",
    "can_transition",
    "commission_for",
    "fill_for_order",
    "fill_limit",
    "fill_market",
    "fill_stop",
    "is_terminal",
    "reachable_from",
    "resolve_exit",
]
