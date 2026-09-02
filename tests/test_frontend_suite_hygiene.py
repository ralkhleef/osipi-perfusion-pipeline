"""A frontend suite that does not finish must not look like one that passed.

Each JS suite counts its own checks and prints one line at the end:

    === Results: 47 passed, 0 failed ===

Everything downstream trusts that line, and twice in one afternoon it was
absent while the run still looked healthy:

* new checks were appended *below* the tally, so they printed their OK lines,
  were never counted, and could never fail the run;
* a suite threw before reaching the end, printing no tally at all, so grepping
  the CI log for failures found nothing to find.

Both read as green. The workflow now fails any suite that finishes without a
tally, which catches the second case at runtime. These tests catch the first
one here, where the fix is cheap, rather than after a push.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUITES = sorted((ROOT / "tests").glob("*_test.js"))

TALLY = re.compile(r"^console\.log\(`\\n=== Results: \$\{passed\} passed, \$\{failed\} failed ===")


def test_there_are_frontend_suites_to_check() -> None:
    """Guards the parametrised tests below from passing vacuously."""
    assert len(SUITES) >= 10, [p.name for p in SUITES]


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.name)
def test_the_suite_prints_a_tally(suite: Path) -> None:
    source = suite.read_text(encoding="utf-8")
    assert TALLY.search(source, ) or "=== Results:" in source, (
        f"{suite.name} never prints a results line"
    )


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.name)
def test_nothing_runs_after_the_tally(suite: Path) -> None:
    """The tally is the last statement, so no check can be added below it.

    A check written after the tally still prints its OK line, which is exactly
    why this is worth enforcing: the output looks complete and the number is
    wrong.
    """
    lines = suite.read_text(encoding="utf-8").split("\n")
    at = [i for i, line in enumerate(lines) if "=== Results:" in line]
    assert at, f"{suite.name} never prints a results line"

    after = [
        line for line in lines[at[-1] + 1:]
        if line.strip() and not line.strip().startswith("//")
        and "process.exit" not in line
    ]
    assert not after, (
        f"{suite.name} has {len(after)} line(s) after its tally; anything that "
        f"runs there is printed but never counted: {after[:3]}"
    )


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: p.name)
def test_the_suite_actually_prints_the_tally_when_run(suite: Path) -> None:
    """The check the source alone cannot make: run it and read the output.

    A suite can hold a well-formed tally on line 400 and still throw on line
    12. Only running it tells the difference.
    """
    result = subprocess.run(
        ["node", suite.name], cwd=ROOT / "tests",
        capture_output=True, text=True, timeout=180,
    )
    output = result.stdout + result.stderr
    assert re.search(r"^=== Results: \d+ passed, \d+ failed ===$", output, re.M), (
        f"{suite.name} finished without a results line, so its checks did not "
        f"run. Exit code {result.returncode}. Last output:\n"
        + "\n".join(output.strip().split("\n")[-6:])
    )
    assert result.returncode == 0, f"{suite.name} failed:\n{output[-1500:]}"


def test_the_workflow_fails_a_suite_that_prints_no_tally() -> None:
    """The runtime half of the guard, pinned so it cannot be quietly dropped.

    Without it a crashing suite is reported only by its exit code, and a suite
    that exits 0 after throwing inside a callback is not reported at all.
    """
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "=== Results: [0-9]+ passed, [0-9]+ failed ===" in workflow, (
        "the workflow no longer checks that a suite printed its tally"
    )
    assert "its checks did not run" in workflow
    # Suites are discovered, not listed: a list drifts the moment one is added,
    # which is how this project lost coverage before.
    assert "suites=(tests/*_test.js)" in workflow
