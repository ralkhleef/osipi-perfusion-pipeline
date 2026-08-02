"""Shared pytest configuration.

Its one job is to stop the suite reporting green when large parts of it never
ran. ``tests/test_api.py`` guards itself with ``pytest.importorskip`` so the
suite still works in a minimal environment, but that means a missing ``httpx``
silently disables every API and report-rendering test while pytest prints a
reassuring "passed". A report redesign shipped with five broken tests exactly
that way.

Set ``OSIPI_REQUIRE_FULL_TESTS=1`` (do this in CI) to turn those skips into a
hard failure.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

# Packages the full suite needs, with the area each one gates.
_OPTIONAL_DEPS = {
    "fastapi": "API endpoint tests",
    "httpx": "API endpoint tests (TestClient)",
    "numpy": "NIfTI and scoring tests",
    "nibabel": "NIfTI validation tests",
    "reportlab": "PDF report rendering tests",
}


def _missing() -> dict[str, str]:
    return {
        name: area for name, area in _OPTIONAL_DEPS.items()
        if importlib.util.find_spec(name) is None
    }


def pytest_configure(config: pytest.Config) -> None:
    missing = _missing()
    if not missing:
        return
    lines = [f"  - {name}: disables {area}" for name, area in sorted(missing.items())]
    message = (
        "Optional test dependencies are missing, so parts of the suite will be "
        "skipped:\n" + "\n".join(lines)
        + "\n  Install with: pip install -r requirements-test.txt"
    )
    if os.environ.get("OSIPI_REQUIRE_FULL_TESTS") == "1":
        raise pytest.UsageError(
            message + "\n  OSIPI_REQUIRE_FULL_TESTS=1 requires a complete run."
        )
    config.stash.setdefault(_WARNING_KEY, []).append(message)


_WARNING_KEY: pytest.StashKey[list[str]] = pytest.StashKey()


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Repeat the warning at the end, where it will actually be read."""
    for message in config.stash.get(_WARNING_KEY, []) or []:
        terminalreporter.write_sep("!", "incomplete test run", yellow=True)
        terminalreporter.write_line(message)
