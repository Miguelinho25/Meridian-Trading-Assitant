"""The research journal — notes in the Obsidian vault.

Read-only over HTTP, and that boundary is a safety property rather than a
convenience. The brief is explicit: arbitrary Markdown edits must never modify
account balances, order history or audit records. The vault is a place for the
operator's *interpretation* of what happened; the record of what happened lives
in the database. Letting an HTTP call write notes would blur two things that must
stay separate.

Notes are read, parsed and served with their machine-written fields and their
human-written ones distinguished.

There is deliberately no "edited by hand" flag. The recorded content hash covers
only the *generated* section, not the editable sections appended after it, so
recomputing it over the whole body marks every note as edited — which an earlier
version of this module did, on all 35 notes, none of which anyone had touched. A
signal that is wrong every time is worse than no signal, and detecting real edits
needs the expected hash from the database rather than the file alone.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from nemonis_config import get_settings
from nemonis_vault.notes import extract_user_fields, parse_note
from pydantic import BaseModel, ConfigDict

router = APIRouter(prefix="/api/journal", tags=["journal"])

#: Frontmatter keys the system writes. Everything else in a note is the
#: operator's, and the two are never merged in a response.
MACHINE_PREFIX = "nemonis_"


class NoteSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str
    folder: str
    note_id: str
    note_type: str
    instrument: str
    direction: str
    strategy: str
    session: str
    pnl: str
    generated_at: str
    #: True when the note carries the synthetic marker. A simulated trade must
    #: never be readable as real performance from a listing.
    synthetic: bool
    #: True when the operator has written into any editable section. Derived from
    #: the sections themselves, not from a hash comparison the file cannot
    #: support.
    has_notes: bool


class NoteDetail(NoteSummary):
    model_config = ConfigDict(extra="forbid")
    #: Frontmatter the system wrote.
    machine_fields: dict[str, str]
    #: Frontmatter the operator wrote or edited.
    user_fields: dict[str, str]
    body: str


class VaultStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    exists: bool
    sync_enabled: bool
    note_count: int
    folders: list[str]
    notice: str


def _vault_root() -> Path:
    return Path(get_settings().vault_path).resolve()


def _read(path: Path) -> tuple[dict[str, str], str, bool]:
    """Parse a note into (frontmatter, body, has_notes)."""
    text = path.read_text(encoding="utf-8")
    parsed = parse_note(text)
    front = {str(k): str(v) for k, v in parsed.frontmatter.items()}
    # An editable field with anything in it means the operator has written here.
    written = extract_user_fields(text)
    has_notes = any(v.strip() for v in written.values())
    return front, parsed.body, has_notes


def _summary(path: Path, root: Path) -> NoteSummary | None:
    try:
        front, _body, has_notes = _read(path)
    except Exception:
        # A malformed note is skipped rather than failing the whole listing. The
        # vault is user-editable by design, so one broken file is expected.
        return None

    return NoteSummary(
        filename=path.name,
        folder=str(path.parent.relative_to(root)) if path.parent != root else "",
        note_id=front.get("nemonis_id", ""),
        note_type=front.get("nemonis_type", ""),
        instrument=front.get("instrument", ""),
        direction=front.get("direction", ""),
        strategy=front.get("strategy", ""),
        session=front.get("session", ""),
        pnl=front.get("pnl_account_ccy", ""),
        generated_at=front.get("nemonis_generated_at", ""),
        synthetic=str(front.get("synthetic", "")).lower() in {"true", "yes"},
        has_notes=has_notes,
    )


@router.get("/status", response_model=VaultStatus, summary="Vault location and size")
async def status() -> VaultStatus:
    settings = get_settings()
    root = _vault_root()
    notes = list(root.rglob("*.md")) if root.exists() else []
    folders = sorted({str(p.parent.relative_to(root)) for p in notes if p.parent != root})
    return VaultStatus(
        path=str(root),
        exists=root.exists(),
        sync_enabled=settings.vault_sync_enabled,
        note_count=len(notes),
        folders=folders,
        notice=(
            "The vault holds interpretation, not record. Editing a note cannot "
            "change a balance, an order or an audit entry — those live in the "
            "database and are not writable from here."
        ),
    )


@router.get("", response_model=list[NoteSummary], summary="Notes in the vault")
async def index(
    instrument: str | None = None,
    strategy: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> list[NoteSummary]:
    root = _vault_root()
    if not root.exists():
        return []

    summaries: list[NoteSummary] = []
    # Newest first by filename, which begins with the trade date.
    for path in sorted(root.rglob("*.md"), key=lambda p: p.name, reverse=True):
        summary = _summary(path, root)
        if summary is None:
            continue
        if instrument and summary.instrument != instrument:
            continue
        if strategy and summary.strategy != strategy:
            continue
        summaries.append(summary)
        if len(summaries) >= limit:
            break
    return summaries


@router.get("/{filename:path}", response_model=NoteDetail, summary="One note")
async def detail(filename: str) -> NoteDetail:
    root = _vault_root()
    candidate = (root / filename).resolve()

    # Resolved-path check, not a string check: '..' segments, absolute paths and
    # symlinks pointing outside are all caught here and none of them would be by
    # inspecting the input.
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=400, detail="Path escapes the vault")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"No note {filename}")

    summary = _summary(candidate, root)
    if summary is None:
        raise HTTPException(status_code=422, detail=f"{filename} could not be parsed")

    front, body, _has_notes = _read(candidate)
    return NoteDetail(
        **summary.model_dump(),
        machine_fields={k: v for k, v in front.items() if k.startswith(MACHINE_PREFIX)},
        user_fields={k: v for k, v in front.items() if not k.startswith(MACHINE_PREFIX)},
        body=body,
    )
