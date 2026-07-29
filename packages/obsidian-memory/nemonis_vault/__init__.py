"""Obsidian vault: safe writes, note generation and the sync boundary."""

from __future__ import annotations

from nemonis_vault.notes import (
    SESSION_LINKS,
    TRADE_EDITABLE_FIELDS,
    Conflict,
    NoteError,
    ParsedNote,
    detect_conflicts,
    extract_user_fields,
    hash_content,
    parse_note,
    render_conflict_callout,
    render_frontmatter,
    render_trade_note,
    trade_note_filename,
)
from nemonis_vault.writer import VaultError, VaultWriter, WriteResult, slugify

__all__ = [
    "SESSION_LINKS",
    "TRADE_EDITABLE_FIELDS",
    "Conflict",
    "NoteError",
    "ParsedNote",
    "VaultError",
    "VaultWriter",
    "WriteResult",
    "detect_conflicts",
    "extract_user_fields",
    "hash_content",
    "parse_note",
    "render_conflict_callout",
    "render_frontmatter",
    "render_trade_note",
    "slugify",
    "trade_note_filename",
]
