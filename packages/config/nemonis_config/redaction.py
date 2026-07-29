"""Redaction — one implementation, used by logging, the model router and the vault.

security.md §3. A single implementation means a gap is fixed once rather than in
three places that drift apart.

The absolute-money rule is the non-obvious one: balances are replaced rather than
masked, because "risked 0.35%, result -1.0R" is both safer *and* more useful to a
model than "£47,318.22 in account 5583991". Percentages compare across accounts;
absolute figures do not.
"""

from __future__ import annotations

import re
from typing import Any, Final

REDACTED: Final = "«redacted»"

_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer", re.compile(r"(?i)\b(bearer|authorization:)\s+[A-Za-z0-9._\-]{12,}")),
    (
        "private_key",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # Long digit runs that look like account numbers. Deliberately not applied to
    # decimals — a price or a P&L figure must survive redaction intact.
    ("account_number", re.compile(r"(?<![\d.])\d{8,}(?![\d.])")),
)

#: Keys whose values are replaced wholesale, whatever they contain.
_SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "key",
        "secret",
        "token",
        "password",
        "passwd",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "access_token",
        "refresh_token",
        "session",
        "cookie",
        "account_number",
        "account_id",
        "login",
        "broker_account",
    }
)

#: Keys holding absolute money that should never reach a model or a log verbatim.
_ABSOLUTE_MONEY_KEYS: Final[frozenset[str]] = frozenset(
    {"balance", "equity", "free_margin", "margin", "deposit", "withdrawal"}
)

_MAX_DEPTH: Final = 12


def redact_text(text: str) -> str:
    """Replace every known secret pattern in a string."""
    for _name, pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_value(key: str | None, value: Any, _depth: int = 0) -> Any:
    """Redact a value, using its key as a hint.

    Recurses through mappings and sequences. Depth-limited so a cyclic or
    pathologically nested structure cannot hang the logger.
    """
    if _depth > _MAX_DEPTH:
        return REDACTED

    normalised = key.lower().strip("_") if key else ""

    if normalised in _SENSITIVE_KEYS:
        return REDACTED
    if normalised in _ABSOLUTE_MONEY_KEYS:
        return REDACTED

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        redacted = [redact_value(None, v, _depth + 1) for v in value]
        return type(value)(redacted) if isinstance(value, tuple) else redacted
    return value


def redact_mapping(data: dict[str, Any]) -> dict[str, Any]:
    """Redact a whole mapping. The entry point for log records and prompt payloads."""
    return {k: redact_value(str(k), v) for k, v in data.items()}


def contains_secret(text: str) -> bool:
    """True if the text still matches a secret pattern.

    Used by tests and by the pre-send assertion in the model router, so a
    redaction gap fails loudly rather than leaking quietly.
    """
    return any(pattern.search(text) for _name, pattern in _PATTERNS)
