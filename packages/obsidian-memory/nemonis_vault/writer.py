"""Safe vault writes (obsidian-memory.md §4).

Every write is atomic and backed up. The vault may be open in Obsidian, sitting
in a synced folder, or backed by iCloud while we write to it — a truncated note
is not an acceptable outcome in any of those.

The path check is the other half. Note names derive partly from user-supplied
text (instrument names, setup tags, journal titles), so a filename is untrusted
input and must never be able to escape the vault root.
"""

from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

#: Characters that are illegal, reserved, or path separators on some platform.
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLLAPSE = re.compile(r"[-\s]+")

#: Reserved on Windows even with an extension. A vault synced to a Windows
#: machine would silently fail to create these.
_RESERVED: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

MAX_STEM_LENGTH: Final = 120


class VaultError(RuntimeError):
    """A vault operation was refused."""


def slugify(text: str, *, max_length: int = MAX_STEM_LENGTH) -> str:
    """Turn arbitrary text into a safe filename stem.

    Normalises to ASCII, strips separators and control characters, collapses
    whitespace, and truncates. Empty results become ``untitled`` rather than an
    empty filename.
    """
    normalised = unicodedata.normalize("NFKD", text)
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii")
    cleaned = _UNSAFE.sub("", ascii_text)
    collapsed = _COLLAPSE.sub("-", cleaned).strip("-. ")
    truncated = collapsed[:max_length].rstrip("-. ")

    if not truncated:
        return "untitled"
    if truncated.lower() in _RESERVED:
        return f"{truncated}-note"
    return truncated


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: Path
    written: bool
    backup: Path | None = None
    reason: str = ""


class VaultWriter:
    """Atomic, backed-up writes confined to a vault root."""

    def __init__(self, root: Path | str, *, backup_retention: int = 5) -> None:
        self.root = Path(root).resolve()
        self.backup_retention = backup_retention
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, *parts: str) -> Path:
        """Resolve a path inside the vault, refusing anything that escapes it.

        Checks the *resolved* path rather than the input, so ``..`` segments,
        absolute paths and symlinks pointing outside are all caught. Validating
        the string alone would miss every one of them.
        """
        if not parts:
            raise VaultError("No path supplied")

        candidate = self.root.joinpath(*parts)
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise VaultError(f"Cannot resolve {candidate}: {exc}") from exc

        if resolved != self.root and self.root not in resolved.parents:
            raise VaultError(
                f"Refusing to write outside the vault: {'/'.join(parts)!r} resolves "
                f"to {resolved}, which is not under {self.root}."
            )
        return resolved

    def write(
        self, relative: str, content: str, *, folder: str = "", at: datetime | None = None
    ) -> WriteResult:
        """Write a note atomically, backing up any differing existing content.

        Unchanged content is a no-op, so file modification times keep meaning
        something and a sync loop cannot churn the vault.
        """
        path = self.resolve(folder, relative) if folder else self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                return WriteResult(path=path, written=False, reason="unchanged")
            backup = self._backup(path, existing, at=at)
        else:
            backup = None

        # Temp file in the same directory, so os.replace is a true atomic rename
        # rather than a cross-filesystem copy.
        temp = path.with_name(f".{path.name}.tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            raise VaultError(f"Failed to write {path}: {exc}") from exc

        return WriteResult(path=path, written=True, backup=backup)

    def _backup(self, path: Path, content: str, *, at: datetime | None) -> Path:
        stamp = (at or datetime.now().astimezone()).strftime("%Y%m%dT%H%M%S")
        backup = path.with_name(f"{path.name}.backup-{stamp}")
        backup.write_text(content, encoding="utf-8")
        self._prune_backups(path)
        return backup

    def _prune_backups(self, path: Path) -> None:
        """Keep only the most recent backups. Unbounded growth is its own bug."""
        pattern = f"{path.name}.backup-*"
        backups = sorted(path.parent.glob(pattern))
        for stale in backups[: -self.backup_retention] if self.backup_retention else backups:
            stale.unlink(missing_ok=True)

    def read(self, relative: str, *, folder: str = "") -> str | None:
        path = self.resolve(folder, relative) if folder else self.resolve(relative)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def exists(self, relative: str, *, folder: str = "") -> bool:
        path = self.resolve(folder, relative) if folder else self.resolve(relative)
        return path.exists()

    def list_notes(self, folder: str = "") -> list[Path]:
        base = self.resolve(folder) if folder else self.root
        if not base.exists():
            return []
        return sorted(p for p in base.rglob("*.md") if ".backup-" not in p.name)

    def delete_backups(self, folder: str = "") -> int:
        base = self.resolve(folder) if folder else self.root
        removed = 0
        for backup in base.rglob("*.backup-*"):
            backup.unlink(missing_ok=True)
            removed += 1
        return removed

    def wipe(self) -> None:
        """Remove everything under the root. Tests only."""
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
