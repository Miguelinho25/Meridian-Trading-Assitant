"""Persistence layer.

Not in the brief's original tree: the brief listed no shared database package, but
models, the decimal type, the audit chain and the session factory are needed by
several services. Duplicating them per service would guarantee drift in exactly
the code where drift is least acceptable. Documented here rather than in an ADR
because it adds a package without changing any boundary.
"""

from __future__ import annotations

from nemonis_db.audit import (
    GENESIS_HASH,
    AuditChainError,
    ChainVerification,
    append_event,
    canonical_json,
    chain_head,
    compute_hash,
    verify_chain,
)
from nemonis_db.models import (
    APPEND_ONLY_TABLES,
    Account,
    AuditEvent,
    Base,
    Incident,
    Instrument,
    KillSwitchEvent,
    Order,
    OrderStateTransition,
    RiskAssessment,
    TradeProposal,
)
from nemonis_db.session import (
    AppendOnlyViolationError,
    create_engine,
    create_session_factory,
    dispose_engine,
    get_engine,
    get_session_factory,
    session_scope,
)
from nemonis_db.types import DecimalText, UTCDateTime

__all__ = [
    "APPEND_ONLY_TABLES",
    "GENESIS_HASH",
    "Account",
    "AppendOnlyViolationError",
    "AuditChainError",
    "AuditEvent",
    "Base",
    "ChainVerification",
    "DecimalText",
    "Incident",
    "Instrument",
    "KillSwitchEvent",
    "Order",
    "OrderStateTransition",
    "RiskAssessment",
    "TradeProposal",
    "UTCDateTime",
    "append_event",
    "canonical_json",
    "chain_head",
    "compute_hash",
    "create_engine",
    "create_session_factory",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "session_scope",
    "verify_chain",
]
