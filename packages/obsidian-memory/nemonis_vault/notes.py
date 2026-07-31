"""Note generation and the frontmatter contract (obsidian-memory.md §3).

The vault is a derived, human-readable layer. Editing a note can never change an
account balance, an order, a fill or an audit record — the sync-back path writes
only to an allowlisted set of fields, and that allowlist is published *in the
note itself* so it is visible to the reader and to the sync engine alike.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final

from nemonis_broker.broker import ClosedTrade
from nemonis_marketdata.instruments import InstrumentSpec
from nemonis_marketdata.sessions import primary_session

from nemonis_vault.writer import slugify

FRONTMATTER_DELIMITER: Final = "---"
SCHEMA_VERSION: Final = "trade-note@1"

#: Fields a human may edit. Everything else is regenerated from the database.
TRADE_EDITABLE_FIELDS: Final[tuple[str, ...]] = (
    "what_worked",
    "what_failed",
    "lesson",
    "user_tags",
    "screenshots",
)

#: Controlled vocabulary for wiki links (obsidian-memory.md §6).
#: Version-controlled and human-approved. An LLM may *suggest* additions, but
#: free-form model-invented tags would fragment the graph within weeks.
SESSION_LINKS: Final[dict[str, str]] = {
    "LONDON": "London Session",
    "NEW_YORK": "New York Session",
    "TOKYO": "Tokyo Session",
    "SYDNEY": "Sydney Session",
    "CLOSED": "Off Session",
}

REGIME_LINKS: Final[dict[str, str]] = {
    "TRENDING": "Trend Regime",
    "RANGING": "Range Regime",
    "HIGH": "High Volatility",
    "NORMAL": "Normal Volatility",
    "LOW": "Low Volatility",
    "UNKNOWN": "Unknown Regime",
}


class NoteError(RuntimeError):
    """A note could not be generated or parsed."""


@dataclass(frozen=True, slots=True)
class ParsedNote:
    frontmatter: dict[str, Any]
    body: str
    #: Content hash over generated material only, so a permitted user edit does
    #: not register as a conflict.
    content_hash: str


def _yaml_value(value: Any) -> str:
    """Render a value as flat YAML.

    Deliberately minimal rather than pulling in a YAML library: frontmatter stays
    flat and machine-readable so Dataview can query it and the sync engine can
    parse it without ambiguity. Nested structures are not permitted.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_value(v) for v in value) + "]"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    if any(c in text for c in ':#[]{}"\n') or text != text.strip():
        escaped = text.replace('"', '\\"').replace("\n", " ")
        return f'"{escaped}"'
    return text


def render_frontmatter(fields: dict[str, Any]) -> str:
    lines = [FRONTMATTER_DELIMITER]
    lines.extend(f"{key}: {_yaml_value(value)}" for key, value in fields.items())
    lines.append(FRONTMATTER_DELIMITER)
    return "\n".join(lines)


def parse_note(text: str) -> ParsedNote:
    """Parse a note's frontmatter and body.

    Tolerant by design: a note a human has been editing may be malformed, and
    refusing to read it would mean losing their work rather than surfacing it.
    """
    if not text.startswith(FRONTMATTER_DELIMITER):
        return ParsedNote(frontmatter={}, body=text, content_hash=hash_content(text))

    parts = text.split(FRONTMATTER_DELIMITER, 2)
    if len(parts) < 3:
        return ParsedNote(frontmatter={}, body=text, content_hash=hash_content(text))

    frontmatter: dict[str, Any] = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            frontmatter[key.strip()] = (
                [v.strip().strip('"') for v in inner.split(",") if v.strip()] if inner else []
            )
        else:
            frontmatter[key.strip()] = value.strip('"') if value != "null" else None

    return ParsedNote(
        frontmatter=frontmatter,
        body=parts[2].lstrip("\n"),
        content_hash=frontmatter.get("nemonis_content_hash", ""),
    )


def hash_content(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()[:32]}"


def _links_for(instrument: str, session: str, regime: str, strategy: str) -> list[str]:
    """Wiki links from the controlled vocabulary.

    Hub notes are created on first reference elsewhere, so there are no orphan
    links.
    """
    links = [instrument, SESSION_LINKS.get(session, "Off Session")]
    for part in regime.split("/"):
        if part in REGIME_LINKS:
            links.append(REGIME_LINKS[part])
    if strategy:
        links.append(f"Strategy-{strategy}")
    return links


def trade_note_filename(trade: ClosedTrade) -> str:
    date = trade.closed_at.strftime("%Y-%m-%d")
    stem = slugify(f"{date}-{trade.instrument}-{trade.direction.value.lower()}")
    return f"{stem}-{trade.trade_id[-8:]}.md"


def render_trade_note(
    trade: ClosedTrade,
    *,
    spec: InstrumentSpec,
    generated_at: datetime,
    risk_pct: Decimal | None = None,
    r_multiple: Decimal | None = None,
    regime_label: str = "UNKNOWN",
    setup_type: str = "",
    confidence: Decimal | None = None,
    ai_decision: str = "",
    rule_profile_result: str = "",
    synthetic: bool = True,
    user_fields: dict[str, Any] | None = None,
) -> str:
    """Render a trade note.

    ``synthetic`` propagates into the frontmatter so simulated performance can
    never be mistaken for real in any view that queries the vault.
    """
    session = primary_session(trade.closed_at).value
    links = _links_for(trade.instrument, session, regime_label, trade.strategy_id)
    user = user_fields or {}

    generated_body = "\n".join(
        [
            f"# {trade.instrument} {trade.direction.value} — {trade.closed_at:%Y-%m-%d}",
            "",
            "## Execution",
            "",
            f"- Entry: `{trade.entry_price}`",
            f"- Exit: `{trade.exit_price}`",
            f"- Size: `{trade.lots}` lots",
            f"- Exit reason: `{trade.reason.value}`",
            f"- P&L: `{trade.pnl_account_ccy}`",
            f"- Commission: `{trade.commission}`",
            f"- MFE / MAE: `{trade.mfe_pips:.1f}` / `{trade.mae_pips:.1f}` pips",
            (
                "- **Ambiguous exit**: stop and target were both reachable in the "
                "closing bar; the stop was assumed."
                if trade.ambiguous_exit
                else ""
            ),
            "",
            "## Context",
            "",
            f"- Strategy: [[Strategy-{trade.strategy_id}]]",
            f"- Session: [[{SESSION_LINKS.get(session, 'Off Session')}]]",
            f"- Regime: `{regime_label}`",
            f"- Instrument: [[{trade.instrument}]]",
            "",
            "## Links",
            "",
            " · ".join(f"[[{link}]]" for link in links),
            "",
        ]
    )

    content_hash = hash_content(generated_body)

    frontmatter = render_frontmatter(
        {
            "nemonis_id": trade.trade_id,
            "nemonis_type": "trade",
            "nemonis_generated_at": generated_at,
            "nemonis_content_hash": content_hash,
            "nemonis_schema": SCHEMA_VERSION,
            "nemonis_editable": list(TRADE_EDITABLE_FIELDS),
            "instrument": trade.instrument,
            "direction": trade.direction.value.lower(),
            "session": session,
            "strategy": trade.strategy_id,
            "setup": setup_type,
            "regime": regime_label,
            "entry": trade.entry_price,
            "exit": trade.exit_price,
            "lots": trade.lots,
            "risk_pct": risk_pct,
            "r_result": r_multiple,
            "pnl_account_ccy": trade.pnl_account_ccy,
            "commission": trade.commission,
            "mfe_pips": trade.mfe_pips,
            "mae_pips": trade.mae_pips,
            "confidence": confidence,
            "ai_decision": ai_decision,
            "rule_profile_result": rule_profile_result,
            "ambiguous_exit": trade.ambiguous_exit,
            "synthetic": synthetic,
            "tags": [
                "trade",
                trade.instrument.lower(),
                session.lower(),
                trade.direction.value.lower(),
                "win" if trade.pnl_account_ccy > 0 else "loss",
            ],
        }
    )

    user_section = "\n".join(
        [
            "## Review",
            "",
            "> The fields below are yours. Everything above is regenerated from the",
            "> database and edits to it will be restored on the next sync.",
            "",
            f"**What worked:** {user.get('what_worked', '')}",
            "",
            f"**What failed:** {user.get('what_failed', '')}",
            "",
            f"**Lesson:** {user.get('lesson', '')}",
            "",
        ]
    )

    return f"{frontmatter}\n\n{generated_body}\n{user_section}"


def extract_user_fields(note: str) -> dict[str, str]:
    """Pull the allowlisted user fields back out of an edited note.

    Only these fields ever travel back toward the database, and they land in a
    JSON column on ``journal_notes`` — never anywhere near an account, an order
    or the audit log.
    """
    fields: dict[str, str] = {}
    # [ \t]* rather than \s*: \s* consumes the newline that separates one label
    # from the next, so an *empty* field swallowed the following label as its own
    # value — an untouched note reported what_worked="**What failed:**", and that
    # text would then travel back toward the database as if the operator had
    # written it. Only same-line whitespace may be skipped.
    patterns = {
        "what_worked": r"\*\*What worked:\*\*[ \t]*(.*?)(?=\n\n|\n\*\*|$)",
        "what_failed": r"\*\*What failed:\*\*[ \t]*(.*?)(?=\n\n|\n\*\*|$)",
        "lesson": r"\*\*Lesson:\*\*[ \t]*(.*?)(?=\n\n|\n\*\*|$)",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, note, re.DOTALL)
        if match:
            value = match.group(1).strip()
            if value:
                fields[name] = value
    return fields


@dataclass(frozen=True, slots=True)
class Conflict:
    field_name: str
    stored_value: str
    edited_value: str


def detect_conflicts(note: str, expected_hash: str) -> list[Conflict]:
    """Detect edits to generated content.

    A mismatched hash means someone changed a field the database owns. Their text
    is preserved in a callout rather than discarded, but it never becomes truth.
    """
    parsed = parse_note(note)
    actual = parsed.frontmatter.get("nemonis_content_hash", "")
    if actual and actual != expected_hash:
        return [
            Conflict(
                field_name="generated_body",
                stored_value=expected_hash,
                edited_value=str(actual),
            )
        ]
    return []


def render_conflict_callout(conflicts: list[Conflict], *, at: datetime) -> str:
    lines = [f"> [!warning] Sync conflict — {at.isoformat()}"]
    for conflict in conflicts:
        lines.append(
            f"> The field `{conflict.field_name}` is generated from the database and was restored."
        )
        lines.append(f"> Your version is preserved here: `{conflict.edited_value}`")
    return "\n".join(lines)
