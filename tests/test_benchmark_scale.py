"""The scalability benchmark.

"Scalable to handle multiple submissions and large imaging datasets" is a
stated non-functional requirement that had never been measured. The numbers
quoted on the GSoC page come from ``scripts/benchmark_scale.py``, so the
script has to keep working, and the claim on the page has to keep matching
what the script measures.

The sweep here is the quick one. Timing the real sweep inside a test suite
would make the suite slow and the assertion flaky; what is checked is that
the measurement is sound, not how fast this particular machine is.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_scale.py"


def _env() -> dict:
    """The caller's environment with the project on the path.

    Replacing os.environ outright dropped the site-packages the interpreter
    was installed with, so the script failed to import nibabel and every test
    below reported a benchmark failure that was really a fixture bug.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(ROOT / "backend"), str(ROOT / "src")])
    return env


@pytest.fixture(scope="module")
def measurements(tmp_path_factory) -> list[dict]:
    """Run the quick sweep once and reuse it."""
    out = tmp_path_factory.mktemp("bench") / "scale.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--quick", "--json", str(out)],
        cwd=ROOT, capture_output=True, text=True,
        env=_env(),
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(out.read_text(encoding="utf-8"))


def test_the_benchmark_runs(measurements) -> None:
    assert len(measurements) >= 2


def test_every_step_is_timed_separately(measurements) -> None:
    """A single total would say something is slow without saying which part."""
    for row in measurements:
        for step in ("write", "ingest", "validate"):
            assert step in row, f"{step} was not measured"
            assert row[step]["seconds"] >= 0
            assert row[step]["peak_mb"] >= 0


def test_the_sweep_actually_grows(measurements) -> None:
    """A benchmark whose cases are the same size measures nothing."""
    sizes = [row["input_mb"] for row in measurements]
    assert sizes[-1] > sizes[0], sizes
    assert measurements[-1]["files"] > measurements[0]["files"]


def test_memory_is_bounded_rather_than_proportional(measurements) -> None:
    """The claim on the GSoC page: files are read one at a time.

    If the pipeline held a whole submission in memory, peak usage would climb
    with the input. It should stay near the size of the largest single file
    instead, so this asserts peak grows far more slowly than input does.
    """
    first, last = measurements[0], measurements[-1]
    input_growth = last["input_mb"] / max(first["input_mb"], 0.01)
    peak_first = max(first[s]["peak_mb"] for s in ("ingest", "validate"))
    peak_last = max(last[s]["peak_mb"] for s in ("ingest", "validate"))
    peak_growth = peak_last / max(peak_first, 0.01)
    assert peak_growth < input_growth, (
        f"peak memory grew {peak_growth:.1f}x while the input grew "
        f"{input_growth:.1f}x, so the pipeline is holding the dataset")


def test_no_files_are_left_behind(measurements, tmp_path) -> None:
    """Everything the benchmark writes lives in a temporary directory."""
    before = {p.name for p in ROOT.iterdir()}
    subprocess.run(
        [sys.executable, str(SCRIPT), "--quick"], cwd=ROOT,
        capture_output=True, text=True,
        env=_env(),
    )
    assert {p.name for p in ROOT.iterdir()} == before


# ── The published numbers match what is measured ──────────────────────────

def test_the_gsoc_page_quotes_the_script_that_produces_the_numbers() -> None:
    """A figure with no way to reproduce it is a claim, not a measurement."""
    page = (ROOT / "docs" / "gsoc.html").read_text(encoding="utf-8")
    assert "benchmark_scale.py" in page, (
        "the page quotes scale figures without naming the script behind them")


def test_the_published_memory_claim_is_the_one_the_script_checks() -> None:
    """Keeps the page and the assertion above describing the same property."""
    page = (ROOT / "docs" / "gsoc.html").read_text(encoding="utf-8")
    match = re.search(r"peak memory stays near (\d+) MB", page)
    assert match, "the page no longer states a peak memory figure"
    assert 1 <= int(match.group(1)) <= 512, (
        "a peak far outside the measured range suggests the figure was not "
        "updated with the measurement")


def test_the_published_claim_covers_four_dimensional_data() -> None:
    """The 3-D figure alone was true and misleading at the same time.

    The benchmark measured only 3-D volumes, so it reported comfortable
    numbers while the pipeline could not in fact read the DCE challenge data:
    one 4-D concentration curve is 8 MB on disk and needed 2.38 GB to load.
    A page that quotes the 3-D figure without saying so invites someone to
    plan capacity from it and be badly wrong.
    """
    page = (ROOT / "docs" / "gsoc.html").read_text(encoding="utf-8")
    assert "4-D" in page, "the page does not mention 4-D data at all"
    assert re.search(r"0\.54 GB|chunk", page), (
        "the page states a 3-D peak without saying how 4-D data is handled")


def test_the_sweep_actually_measures_four_dimensional_data() -> None:
    """Otherwise the claim above has nothing behind it."""
    from importlib import util
    spec = util.spec_from_file_location("bench", SCRIPT)
    module = util.module_from_spec(spec)
    sys.modules["bench"] = module
    spec.loader.exec_module(module)
    assert any(case[2] > 0 for case in module.DEFAULT_SWEEP), (
        "no 4-D case in the default sweep, so the benchmark cannot fail the "
        "way the pipeline actually failed")
