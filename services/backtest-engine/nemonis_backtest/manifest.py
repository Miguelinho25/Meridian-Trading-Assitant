"""The reproducibility manifest.

A backtest result is worthless without the exact inputs that produced it. This
module captures them as one immutable, hashable record, so a run can be
reproduced years later — or shown to be irreproducible, which is the more
important case.

Two hashes, and the separation between them is the point:

``manifest_hash``
    Every input. Two runs sharing this hash **must** produce identical output.

``result_hash``
    Every output. Same manifest with a different result hash means determinism
    has broken somewhere, and that is detectable rather than merely regrettable.

The subtlety worth stating: **a git commit hash does not identify the code if the
working tree was dirty.** A manifest that records the commit and stops looks
reproducible while being nothing of the sort. ``code_is_pinned`` is false in that
case and the run is marked irreproducible, permanently — the uncommitted edits it
ran against are unrecoverable, and no later commit makes them recoverable.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

#: Bumped when the manifest's own structure changes. Part of the hash, because a
#: manifest laid out differently is not comparable with an older one even if its
#: values look the same.
MANIFEST_VERSION = "1.0.0"


def _canonical(value: Any) -> Any:
    """Convert to a form that hashes identically across platforms and runs.

    Decimals become strings rather than floats: ``float(Decimal("0.1"))`` is
    platform-dependent in its last bits, and a manifest hash that varies by
    machine would make every cross-machine comparison a false mismatch.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def canonical_hash(payload: dict[str, Any]) -> str:
    """Stable hash over a payload. Sorted keys, no whitespace, UTF-8."""
    encoded = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CodeIdentity:
    """Which code ran.

    ``dirty`` is not a warning to be triaged later — it decides whether this run
    can ever be reproduced.
    """

    git_commit: str
    git_branch: str
    #: True when tracked files differed from the commit at run time.
    dirty: bool
    engine_version: str
    feature_pipeline_version: str
    risk_profile_version: str

    @property
    def is_pinned(self) -> bool:
        """Whether the commit actually identifies the code that ran."""
        return bool(self.git_commit) and not self.dirty


@dataclass(frozen=True, slots=True)
class DataIdentity:
    """Which data was fed in."""

    provider: str
    dataset_version: str
    instruments: tuple[str, ...]
    timeframe: str
    start: datetime
    end: datetime
    #: SYNTHETIC / REAL — propagated onto every metric so a synthetic result can
    #: never be mistaken for a real one.
    provenance: str
    #: True when the source was mid-only and a spread was assumed at load time.
    spread_assumed: bool
    bar_count: int = 0


@dataclass(frozen=True, slots=True)
class ExecutionModel:
    """How fills, costs and sizing were simulated.

    Changing any of these changes the result, so all of them are in the hash.
    Recording "slippage: proportional" without its parameter would produce two
    runs that share a manifest hash and disagree.
    """

    slippage_model: str
    fixed_slippage_pips: Decimal
    spread_fraction: Decimal
    gap_penalty: Decimal
    commission_model: str
    commission_per_lot: Decimal
    spread_model: str
    starting_balance: Decimal
    account_currency: str
    risk_profile: str
    warmup_bars: int


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Which language models were consulted, if any.

    Empty is the normal case and the honest one: the deterministic pipeline does
    not consult a model, so most runs record none. Storing the field regardless
    means a run that *did* use one is distinguishable from a run made before the
    field existed.
    """

    #: {role: "model_id@version"} — e.g. {"critique": "llama3.2:3b@local-worker"}
    models: dict[str, str] = field(default_factory=dict)
    embedding_model: str = ""
    embedding_version: str = ""


@dataclass(frozen=True, slots=True)
class BacktestManifest:
    """Everything needed to reproduce one run."""

    strategy_key: str
    strategy_version: str
    #: Strategy parameters. Part of the hash — same strategy, different params is
    #: a different experiment.
    parameters: dict[str, Any]
    seed: int
    code: CodeIdentity
    data: DataIdentity
    execution: ExecutionModel
    models: ModelIdentity = field(default_factory=ModelIdentity)
    manifest_version: str = MANIFEST_VERSION

    def canonical(self) -> dict[str, Any]:
        canonical: dict[str, Any] = _canonical(asdict(self))
        return canonical

    @property
    def manifest_hash(self) -> str:
        """The reproducibility key. Same hash must mean same result."""
        return canonical_hash(asdict(self))

    @property
    def is_reproducible(self) -> bool:
        """Whether this run could be reproduced from what is recorded.

        False when the working tree was dirty: the uncommitted edits are
        unrecoverable, and no later commit recovers them. Recorded honestly
        rather than hidden, because a research archive whose provenance is
        unreliable is worse than one that admits which entries are unreliable.
        """
        return self.code.is_pinned

    @property
    def irreproducible_reason(self) -> str:
        if self.code.is_pinned:
            return ""
        if not self.code.git_commit:
            return "No git commit was recorded; the code that ran is unidentified."
        return (
            f"The working tree was dirty at {self.code.git_commit[:12]}. The commit "
            f"does not identify the code that ran, and the uncommitted changes are "
            f"not recoverable from this record."
        )


def capture_code_identity(
    repo: Path | None = None,
    *,
    engine_version: str,
    feature_pipeline_version: str,
    risk_profile_version: str,
) -> CodeIdentity:
    """Read git state. I/O — call from the runner, never from the engine loop.

    Missing git is not an error: a run outside a checkout is still a run, and it
    is recorded as unpinned rather than refused.
    """
    cwd = repo or Path.cwd()

    def git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout.strip() if result.returncode == 0 else ""

    commit = git("rev-parse", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    # --porcelain lists modified tracked files; any output means dirty.
    # Untracked files are excluded deliberately: a stray scratch file in the
    # working directory does not change what the code did.
    dirty = bool(git("status", "--porcelain", "--untracked-files=no")) if commit else False

    return CodeIdentity(
        git_commit=commit,
        git_branch=branch,
        dirty=dirty,
        engine_version=engine_version,
        feature_pipeline_version=feature_pipeline_version,
        risk_profile_version=risk_profile_version,
    )


def result_hash(
    *,
    metrics: dict[str, Any],
    trade_count: int,
    final_balance: Decimal,
    trade_fingerprints: list[str],
) -> str:
    """Hash over the outputs.

    Includes per-trade fingerprints, not just summary metrics: two runs can land
    on the same net P&L through different trades, and that is a determinism
    break the summary alone would hide.
    """
    return canonical_hash(
        {
            "metrics": metrics,
            "trade_count": trade_count,
            "final_balance": final_balance,
            "trades": trade_fingerprints,
        }
    )
