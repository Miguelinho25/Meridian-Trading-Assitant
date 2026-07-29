"""Redaction must hold for logs, prompts and the vault (security.md §3).

NOTE: this file contains deliberately secret-shaped strings — a redaction test
cannot be written without them. Every value is synthetic and non-functional
(``AKIAIOSFODNN7EXAMPLE`` is AWS's own published example key). This path is
excluded by exact name from `make secret-scan`; see SECRET_SCAN_EXCLUDES in the
Makefile. Never put a real credential here.
"""

from __future__ import annotations

import pytest
from nemonis_config.redaction import REDACTED, contains_secret, redact_mapping, redact_text

SECRETS = [
    "sk-abcdefghij0123456789ABCDEF",
    "sk-ant-api03-abcdefghij0123456789",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-123456789012-abcdefghijkl",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_never_survives_redaction(secret: str) -> None:
    redacted = redact_text(f"prefix {secret} suffix")
    assert secret not in redacted
    assert not contains_secret(redacted)


def test_sensitive_keys_are_replaced_wholesale() -> None:
    out = redact_mapping({"api_key": "anything at all", "password": "hunter2"})
    assert out == {"api_key": REDACTED, "password": REDACTED}


def test_absolute_balances_are_removed() -> None:
    """Models get percentages and R multiples, never absolute money."""
    out = redact_mapping({"balance": "47318.22", "equity": "46900.00", "risk_pct": "0.35"})
    assert out["balance"] == REDACTED
    assert out["equity"] == REDACTED
    assert out["risk_pct"] == "0.35"


def test_prices_and_pnl_survive() -> None:
    """Redaction must not destroy the numbers the system reasons about."""
    out = redact_mapping({"entry_price": "1.08432", "r_multiple": "-1.00", "pnl_pct": "-0.35"})
    assert out["entry_price"] == "1.08432"
    assert out["r_multiple"] == "-1.00"


def test_nested_structures_are_redacted() -> None:
    out = redact_mapping(
        {"trade": {"notes": ["contact me@example.com", "key sk-abcdefghij0123456789"]}}
    )
    flattened = str(out)
    assert "me@example.com" not in flattened
    assert "sk-abcdefghij" not in flattened


def test_cyclic_structure_does_not_hang() -> None:
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic
    assert redact_mapping(cyclic) is not None


def test_contains_secret_detects_unredacted() -> None:
    assert contains_secret("token sk-abcdefghij0123456789ABCDEF")
    assert not contains_secret("perfectly ordinary trade note about EURUSD at 1.0843")
