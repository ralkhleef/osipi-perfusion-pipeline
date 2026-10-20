"""The README and maintainer notes must point at files that exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Documentation a maintainer reads before touching the code.
DOCS: list[Path] = [ROOT / "README.md", *sorted((ROOT / "notes").glob("*.md"))]

TRACKED_ROOTS = frozenset({
    "backend", "src", "tests", "config", "docs",
    "frontend", "scripts", "examples", "notes",
})

#: A backticked token containing a slash, e.g. `backend/scoring.py`.
PATH_IN_BACKTICKS = re.compile(r"`([A-Za-z0-9_.\-/*]+/[A-Za-z0-9_.\-/*]*)`")


def repository_paths(text: str) -> set[str]:
    """Backticked paths that claim to live in one of the tracked source trees."""
    return {
        raw for raw in PATH_IN_BACKTICKS.findall(text)
        if raw.split("/", 1)[0] in TRACKED_ROOTS
    }


def test_there_is_documentation_to_check() -> None:
    assert DOCS, "no README or maintainer notes found"
    assert (ROOT / "notes" / "CODE_WALKTHROUGH.md") in DOCS


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_repository_path_it_names_exists(doc: Path) -> None:
    referenced = repository_paths(doc.read_text(encoding="utf-8"))
    missing = sorted(
        raw for raw in referenced
        if "*" not in raw and not (ROOT / raw.rstrip("/")).exists()
    )
    assert not missing, (
        f"{doc.relative_to(ROOT)} points at paths that do not exist: {missing}"
    )


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_glob_it_names_matches_something(doc: Path) -> None:
    """`backend/services/*.py` in a command must still match files."""
    globs = sorted(
        raw for raw in repository_paths(doc.read_text(encoding="utf-8"))
        if "*" in raw
    )
    empty = [raw for raw in globs if not list(ROOT.glob(raw))]
    assert not empty, (
        f"{doc.relative_to(ROOT)} uses globs that match nothing: {empty}"
    )


def test_the_check_would_have_caught_the_walkthrough_regression() -> None:
    """The specific drift this file exists to prevent."""
    assert repository_paths("see `backend/docker_runner.py` for execution") == {
        "backend/docker_runner.py"
    }
    assert not (ROOT / "backend" / "docker_runner.py").exists()
    assert (ROOT / "src/osipi_pipeline/execution/docker_runner.py").exists()


FRONTEND_SUITE_LINE = re.compile(r"^\s*node\s+tests/[A-Za-z0-9_.\-]+\.js\s*$", re.M)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_frontend_suites_are_discovered_not_listed(doc: Path) -> None:
    text = doc.read_text(encoding="utf-8")
    hardcoded = FRONTEND_SUITE_LINE.findall(text)
    assert not hardcoded, (
        f"{doc.relative_to(ROOT)} names individual frontend suites "
        f"({[h.strip() for h in hardcoded]}); use "
        f'`for suite in tests/*_test.js; do node "$suite"; done` so the list '
        "cannot fall behind the directory."
    )


def test_the_frontend_suites_that_exist_are_actually_runnable() -> None:
    """Discovery is only safe if the glob finds the suites CI runs."""
    suites = sorted((ROOT / "tests").glob("*_test.js"))
    assert len(suites) >= 10, f"expected the frontend suites, found {suites}"
