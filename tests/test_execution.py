"""Tests for Docker execution v1."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from osipi_pipeline.execution import docker_runner
from osipi_pipeline.execution.docker_runner import DockerExecutionError, detect_dockerfile, execute_submission
from osipi_pipeline.execution.models import ExecutionResult


def test_detects_submission_dockerfile(tmp_path: Path) -> None:
    submission = tmp_path / "submission"
    submission.mkdir()
    dockerfile = submission / "Dockerfile"
    dockerfile.write_text("FROM alpine:3.20\n", encoding="utf-8")

    assert detect_dockerfile(submission, fallback_dockerfile=tmp_path / "fallback") == dockerfile


def test_uses_fallback_dockerfile_when_submission_has_none(tmp_path: Path) -> None:
    submission = tmp_path / "submission"
    submission.mkdir()
    fallback = tmp_path / "Dockerfile.example"
    fallback.write_text("FROM alpine:3.20\n", encoding="utf-8")

    assert detect_dockerfile(submission, fallback_dockerfile=fallback) == fallback


def test_execution_result_model_to_dict() -> None:
    result = ExecutionResult(
        submission_path="/tmp/submission",
        challenge_type="dce",
        image_name="osipi-dce-submission:latest",
        command='echo "OSIPI execution placeholder"',
        exit_code=0,
        stdout_path="/tmp/stdout.log",
        stderr_path="/tmp/stderr.log",
        started_at="2026-01-01T00:00:00+00:00",
        finished_at="2026-01-01T00:00:01+00:00",
        passed=True,
    )

    assert result.to_dict()["passed"] is True
    assert result.to_dict()["image_name"] == "osipi-dce-submission:latest"


def test_docker_unavailable_raises_clear_error(tmp_path: Path, monkeypatch) -> None:
    submission = tmp_path / "submission"
    submission.mkdir()
    monkeypatch.setattr(docker_runner.shutil, "which", lambda _name: None)

    with pytest.raises(DockerExecutionError, match="Docker is not installed"):
        execute_submission(submission, challenge_type="dce")


def test_docker_run_failure_saves_logs_and_marks_failed(tmp_path: Path, monkeypatch) -> None:
    submission = tmp_path / "submission"
    submission.mkdir()
    (submission / "Dockerfile").write_text("FROM alpine:3.20\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "build":
            return subprocess.CompletedProcess(command, 0, stdout="build ok", stderr="")
        return subprocess.CompletedProcess(command, 7, stdout="run out", stderr="run err")

    monkeypatch.setattr(docker_runner.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(docker_runner.subprocess, "run", fake_run)

    result = execute_submission(
        submission,
        challenge_type="dce",
        output_dir=tmp_path / "execution",
    )

    assert result.passed is False
    assert result.exit_code == 7
    assert calls[0][0:2] == ["docker", "build"]
    assert calls[1][0:2] == ["docker", "run"]
    assert "build ok" in Path(result.stdout_path).read_text(encoding="utf-8")
    assert "run out" in Path(result.stdout_path).read_text(encoding="utf-8")
    assert "run err" in Path(result.stderr_path).read_text(encoding="utf-8")

