"""Prefixed ULID identifiers (data-model.md §1).

ULIDs sort lexicographically by creation time and need no coordination. The prefix
makes a mis-joined ID obvious on sight — ``ord_01JQ…`` in a ``trade_id`` column is
a bug you can see in a log line rather than one you debug for an hour.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator
from ulid import ULID


class IdPrefix(StrEnum):
    INSTRUMENT = "ins"
    CANDLE = "cdl"
    STRATEGY = "str"
    STRATEGY_VERSION = "sv"
    SIGNAL = "sig"
    PROPOSAL = "prp"
    RISK_DECISION = "rd"
    ORDER = "ord"
    FILL = "fil"
    POSITION = "pos"
    TRADE = "tr"
    ACCOUNT = "acc"
    ACCOUNT_SNAPSHOT = "as"
    PROP_PROFILE = "pf"
    RULE_EVALUATION = "re"
    BACKTEST = "bt"
    EXPERIMENT = "exp"
    MODEL_INVOCATION = "mi"
    AI_CRITIQUE = "aic"
    JOURNAL_NOTE = "jn"
    EMBEDDING = "emb"
    INCIDENT = "inc"
    KILL_SWITCH = "ks"
    AUDIT_EVENT = "ae"
    REGIME = "rgm"


def new_id(prefix: IdPrefix) -> str:
    """Mint a new prefixed ULID."""
    return f"{prefix.value}_{ULID()}"


def parse_prefix(identifier: str) -> str:
    """Return the prefix of an identifier, or raise if malformed."""
    prefix, sep, rest = identifier.partition("_")
    if not sep or not rest:
        raise ValueError(f"Malformed identifier: {identifier!r}")
    return prefix


def validate_id(identifier: str, expected: IdPrefix) -> str:
    """Assert an identifier carries the expected prefix."""
    actual = parse_prefix(identifier)
    if actual != expected.value:
        raise ValueError(
            f"Expected a {expected.name} id (prefix {expected.value!r}), got {actual!r}"
        )
    return identifier


def typed_id(prefix: IdPrefix) -> type[str]:
    """Build an annotated str type that validates its prefix."""
    return Annotated[str, AfterValidator(lambda v: validate_id(v, prefix))]  # type: ignore[return-value]
