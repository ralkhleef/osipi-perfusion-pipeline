"""Tests for Docker execution (docker_runner + execution_service)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the pipeline package is importable regardless of working directory
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from osipi_pipeline.execution import docker_runner
from osipi_pipeline.execution.docker_runner import (
    DEFAULT_RUN_COMMAND,
    DEFAULT_TIMEOUT_SECONDS,
    DockerExecutionError,
    detect_dockerfile,
    execute_submission,
)
from osipi_pipeline.execution.models import ExecutionResult

# Path to the fake submission fixture used for integration-style unit tests
FIXTURES_DIR     = Path(__file__).parent / "fixtures"
FAKE_SUBMISSION  = FIXTURES_DIR / "fake_submission"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_submission(tmp_path: Path, *, with_dockerfile: bool = True) -> Path:
    """Create a minimal submission folder, optionally with a Dockerfile."""
    sub = tmp_path / "submission"
    sub.mkdir()
    if with_dockerfile:
        (sub / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
    return sub


def _fake_docker(build_rc: int = 0, run_rc: int = 0):
    """Return a monkeypatched subprocess.run that simulates docker build/run."""
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        if command[1] == "build":
            return subprocess.CompletedProcess(
                command, build_rc,
                stdout="build stdout" if build_rc == 0 else "build err",
                stderr="build stderr" if build_rc != 0 else "",
            )
        return subprocess.CompletedProcess(
            command, run_rc,
            stdout="run stdout" if run_rc == 0 else "run err",
            stderr="run stderr" if run_rc != 0 else "",
        )

    return fake_run, calls


# ===========================================================================
# detect_dockerfile
# ===========================================================================

def test_detect_dockerfile_uses_submission_dockerfile(tmp_path: Path) -> None:
    sub = _make_submission(tmp_path)
    result = detect_dockerfile(sub, fallback_dockerfile=tmp_path / "missing")
    assert result == sub / "Dockerfile"


def test_detect_dockerfile_uses_fallback_when_submission_has_none(tmp_path: Path) -> None:
    sub = _make_submission(tmp_path, with_dockerfile=False)
    fallback = tmp_path / "Dockerfile.example"
    fallback.write_text("FROM alpine:3.20\n", encoding="utf-8")
    assert detect_dockerfile(sub, fallback_dockerfile=fallback) == fallback


def test_detect_dockerfile_raises_when_both_missing(tmp_path: Path) -> None:
    sub = _make_submission(tmp_path, with_dockerfile=False)
    with pytest.raises(DockerExecutionError, match="No Dockerfile"):
        detect_dockerfile(sub, fallback_dockerfile=tmp_path / "missing")


# ===========================================================================
# ExecutionResult model
# ===========================================================================

def test_execution_result_to_dict_includes_output_file_count() -> None:
    result = ExecutionResult(
        submission_path="/tmp/s",
        challenge_type="dce",
        image_name="osipi-dce-s:latest",
        command="python3 run.py",
        exit_code=0,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        passed=True,
        output_files=("a.nii", "b.nii.gz"),
    )
    d = result.to_dict()
    assert d["output_file_count"] == 2
    assert isinstance(d["output_files"], list)
    assert d["passed"] is True


def test_execution_result_build_failed_defaults_false() -> None:
    result = ExecutionResult(
        submission_path="/tmp/s",
        challenge_type="dce",
        image_name="i",
        command="c",
        exit_code=0,
        stdout_path="/tmp/o",
        stderr_path="/tmp/e",
        started_at="x",
        finished_at="y",
        passed=True,
    )
    assert result.build_failed is False
    assert result.to_dict()["build_failed"] is False


# ===========================================================================
# Docker not installed
# ===========================================================================

def test_docker_unavailable_raises_clear_error(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    monkeypatch.setattr(docker_runner.shutil, "which", lambda _name: None)
    with pytest.raises(DockerExecutionError, match="Docker is not installed"):
        execute_submission(sub, challenge_type="dce")


# ===========================================================================
# Build failure → ExecutionResult with build_failed=True (no exception)
# ===========================================================================

def test_build_failure_returns_result_not_exception(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    fake_run, calls = _fake_docker(build_rc=1)

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    result = execute_submission(sub, challenge_type="dce", output_dir=tmp_path / "exec")

    assert result.passed is False
    assert result.build_failed is True
    assert result.exit_code == 1
    # Only build was called, not run
    assert len(calls) == 1
    assert calls[0][1] == "build"
    # Logs were saved
    stdout_txt = Path(result.stdout_path).read_text(encoding="utf-8")
    stderr_txt = Path(result.stderr_path).read_text(encoding="utf-8")
    assert "build err" in stdout_txt or "build stderr" in stderr_txt


def test_build_failure_logs_contain_build_output(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)

    def fake_run(command, **_kwargs):
        if command[1] == "build":
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="syntax error in FROM")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    result = execute_submission(sub, challenge_type="dce", output_dir=tmp_path / "exec")

    assert result.build_failed is True
    stderr_txt = Path(result.stderr_path).read_text(encoding="utf-8")
    assert "syntax error in FROM" in stderr_txt


# ===========================================================================
# Run failure (build passes, run fails)
# ===========================================================================

def test_run_failure_saves_logs_and_marks_failed(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    fake_run, calls = _fake_docker(build_rc=0, run_rc=7)

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    result = execute_submission(sub, challenge_type="dce", output_dir=tmp_path / "exec")

    assert result.passed is False
    assert result.build_failed is False
    assert result.exit_code == 7
    assert calls[0][0:2] == ["docker", "build"]
    assert calls[1][0:2] == ["docker", "run"]
    stdout_txt = Path(result.stdout_path).read_text(encoding="utf-8")
    stderr_txt = Path(result.stderr_path).read_text(encoding="utf-8")
    assert "build stdout" in stdout_txt
    assert "run err" in stdout_txt
    assert "run stderr" in stderr_txt


# ===========================================================================
# Timeout
# ===========================================================================

def test_timeout_sets_timed_out_and_exit_124(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    call_count = [0]

    def fake_run(command, **_kwargs):
        call_count[0] += 1
        if command[1] == "build":
            return subprocess.CompletedProcess(command, 0, stdout="build ok", stderr="")
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    result = execute_submission(
        sub, challenge_type="dce", output_dir=tmp_path / "exec", timeout_seconds=1
    )

    assert result.timed_out is True
    assert result.passed is False
    assert result.exit_code == 124
    assert call_count[0] == 2  # build + run


# ===========================================================================
# Output file collection
# ===========================================================================

def test_output_files_collected_after_run(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    exec_dir = tmp_path / "exec"

    def fake_run(command, **_kwargs):
        if command[1] == "build":
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        # Simulate the container writing files to the output dir
        # Find /output mount from the command
        for i, arg in enumerate(command):
            if arg == "-v" and "/output" in command[i + 1]:
                host_out = Path(command[i + 1].split(":")[0])
                host_out.mkdir(parents=True, exist_ok=True)
                (host_out / "Ktrans.nii.gz").write_bytes(b"\x00" * 8)
                (host_out / "report.csv").write_text("a,b\n1,2\n")
                break
        return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    result = execute_submission(sub, challenge_type="dce", output_dir=exec_dir)

    assert result.passed is True
    assert len(result.output_files) == 2
    assert result.to_dict()["output_file_count"] == 2
    names = {Path(f).name for f in result.output_files}
    assert "Ktrans.nii.gz" in names
    assert "report.csv" in names


# ===========================================================================
# run_config.json — command and timeout_seconds
# ===========================================================================

def test_run_config_command_is_used(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    (sub / "run_config.json").write_text(
        json.dumps({"command": "python3 /submission/custom.py"}), encoding="utf-8"
    )
    used_commands: list[str] = []

    def fake_run(command, **_kwargs):
        if command[1] == "run":
            # Last arg is the sh -lc command string
            used_commands.append(command[-1])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    execute_submission(sub, challenge_type="dce", output_dir=tmp_path / "exec")

    assert used_commands and "custom.py" in used_commands[0]


def test_run_config_timeout_overrides_default(tmp_path: Path, monkeypatch) -> None:
    sub = _make_submission(tmp_path)
    (sub / "run_config.json").write_text(
        json.dumps({"timeout_seconds": 999}), encoding="utf-8"
    )
    used_timeouts: list[int] = []

    def fake_run(command, timeout=None, **_kwargs):
        if command[1] == "run":
            used_timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    execute_submission(
        sub,
        challenge_type="dce",
        output_dir=tmp_path / "exec",
        # Pass the code default so run_config.json can override
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )

    assert used_timeouts == [999]


def test_explicit_timeout_wins_over_run_config(tmp_path: Path, monkeypatch) -> None:
    """When the caller passes a non-default timeout, run_config.json is ignored."""
    sub = _make_submission(tmp_path)
    (sub / "run_config.json").write_text(
        json.dumps({"timeout_seconds": 999}), encoding="utf-8"
    )
    used_timeouts: list[int] = []

    def fake_run(command, timeout=None, **_kwargs):
        if command[1] == "run":
            used_timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    execute_submission(
        sub,
        challenge_type="dce",
        output_dir=tmp_path / "exec",
        timeout_seconds=42,   # explicit non-default value
    )

    # run_config says 999, but caller explicitly passed 42 — 42 should win
    assert used_timeouts == [42]


# ===========================================================================
# Fake submission fixture — smoke test (no Docker required)
# ===========================================================================

def test_fake_submission_fixture_has_required_files() -> None:
    """Sanity-check that the fixture directory is complete."""
    assert FAKE_SUBMISSION.is_dir(), f"Fixture missing: {FAKE_SUBMISSION}"
    assert (FAKE_SUBMISSION / "Dockerfile").is_file()
    assert (FAKE_SUBMISSION / "run_config.json").is_file()
    assert (FAKE_SUBMISSION / "run.py").is_file()


def test_fake_submission_run_config_is_valid_json() -> None:
    cfg_path = FAKE_SUBMISSION / "run_config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert isinstance(cfg.get("command"), str) and cfg["command"]
    assert isinstance(cfg.get("timeout_seconds"), int) and cfg["timeout_seconds"] > 0


def test_fake_submission_run_py_is_valid_python() -> None:
    run_py = FAKE_SUBMISSION / "run.py"
    source = run_py.read_text(encoding="utf-8")
    compile(source, str(run_py), "exec")  # raises SyntaxError on bad code
