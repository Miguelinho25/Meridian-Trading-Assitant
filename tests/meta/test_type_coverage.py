"""The type-checking gate must actually cover the code.

`make lint` ran `mypy packages apps` for the whole build, silently excluding
`services/` — the risk engine, paper broker, backtest engine, market data and
feature pipeline. 42 errors were hiding behind missing PEP 561 markers, and the
gate reported success because it was checking almost nothing.

These tests fail when a package or source root escapes the gate again. A lint
target that passes because it checked nothing is worse than no lint target: it
is a false assurance that gets trusted.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = ("packages", "services", "apps")


def package_dirs() -> list[Path]:
    """Every importable meridian_* package in the monorepo."""
    return sorted(
        d
        for root in SOURCE_ROOTS
        for d in (REPO / root).glob("*/meridian_*")
        if d.is_dir() and (d / "__init__.py").exists()
    )


def test_packages_are_discovered() -> None:
    """Guards the discovery itself — an empty glob would make every test below
    pass vacuously, which is the failure mode this file exists to catch."""
    found = package_dirs()
    assert len(found) >= 10, f"expected the full monorepo, found {found}"


@pytest.mark.parametrize("package", package_dirs(), ids=lambda p: p.name)
def test_every_package_ships_a_py_typed_marker(package: Path) -> None:
    """Without PEP 561 markers mypy refuses to follow cross-package imports and
    reports import-not-found instead of checking the code."""
    assert (package / "py.typed").exists(), (
        f"{package.relative_to(REPO)} has no py.typed. mypy will not follow "
        f"imports into it, and everything importing it goes unchecked."
    )


def test_the_lint_target_checks_every_source_root() -> None:
    makefile = (REPO / "Makefile").read_text()
    declared = re.search(r"^SOURCE_ROOTS\s*:=\s*(.+)$", makefile, re.M)
    assert declared is not None, "Makefile no longer declares SOURCE_ROOTS"

    roots = set(declared.group(1).split())
    on_disk = {r for r in SOURCE_ROOTS if (REPO / r).is_dir()}
    assert on_disk <= roots, f"source roots excluded from mypy: {on_disk - roots}"


def test_mypy_runs_over_the_declared_roots() -> None:
    """The lint recipe must use SOURCE_ROOTS rather than its own hardcoded list,
    which is how the two drifted apart in the first place."""
    makefile = (REPO / "Makefile").read_text()
    assert "mypy $(SOURCE_ROOTS)" in makefile


def test_import_linter_graphs_every_package() -> None:
    """A contract can only constrain packages present in the module graph."""
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    graphed = set(config["tool"]["importlinter"]["root_packages"])
    on_disk = {d.name for d in package_dirs()}
    assert on_disk <= graphed, f"packages outside the boundary graph: {on_disk - graphed}"
