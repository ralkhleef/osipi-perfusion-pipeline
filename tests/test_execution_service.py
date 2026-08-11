"""The service layer around Docker execution.

This is the module that decides what gets run, for how long, and what the
caller is told afterwards. It was also the least covered module in the
project at 17 percent, which is the wrong way round: it is the only place
that hands participant-supplied code to a container.

Docker itself is not exercised here. ``execute_submission`` is replaced with
a stub so the decisions this layer makes can be checked without a daemon:
which directory becomes the build context, which timeout wins, what happens
when the container will not start, and what reaches the caller when
something fails. The container sandbox flags are covered separately in
``test_execution.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def service(monkeypatch, tmp_path):
    """The service with its extraction root pointed at a temp directory."""
    import services.execution_service as execution_service
    from services import path_config

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(execution_service, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(execution_service, "OUTPUTS_DIR", tmp_path / "outputs")
    return execution_service, extracted


def _result(tmp_path: Path, **overrides):
    """An ExecutionResult shaped like the real runner returns."""
    from osipi_pipeline.execution.models import ExecutionResult

    stdout = tmp_path / "execution_stdout.log"
    stderr = tmp_path / "execution_stderr.log"
    stdout.write_text("## Docker build stdout\nbuilding\n"
                      "## Docker run stdout\nran the model\n")
    stderr.write_text("## Docker build stderr\n\n"
                      "## Docker run stderr\na warning\n")
    fields = dict(
        submission_path="/somewhere", challenge_type="dce",
        image_name="osipi-sub", command="python run.py", exit_code=0,
        stdout_path=str(stdout), stderr_path=str(stderr),
        started_at="2026-01-01T00:00:00Z", finished_at="2026-01-01T00:01:00Z",
        passed=True,
    )
    fields.update(overrides)
    return ExecutionResult(**fields)


def _capture(monkeypatch, module, tmp_path, **overrides):
    """Replace the runner and record the arguments it was called with."""
    calls: dict = {}

    def fake_execute(submission_path, **kwargs):
        calls["submission_path"] = Path(submission_path)
        calls.update(kwargs)
        return _result(tmp_path, **overrides)

    monkeypatch.setattr(module, "execute_submission", fake_execute)
    return calls


# ── Pre-flight refusals: these happen before Docker is involved ───────────

def test_an_unknown_submission_is_refused_without_touching_docker(service, monkeypatch):
    module, _ = service

    def explode(*args, **kwargs):
        raise AssertionError("Docker must not be reached for a missing submission")

    monkeypatch.setattr(module, "execute_submission", explode)
    out = module.run_submission("no_such_thing", challenge_type="dce")
    assert out["success"] is False
    assert "not found" in out["message"].lower()


def test_a_submission_without_a_dockerfile_is_refused(service, monkeypatch):
    """No silent fallback to a default image: the caller is told plainly."""
    module, extracted = service
    (extracted / "sub").mkdir()

    monkeypatch.setattr(module, "execute_submission",
                        lambda *a, **k: pytest.fail("should not run"))
    out = module.run_submission("sub", challenge_type="dce")
    assert out["success"] is False
    assert "Dockerfile" in out["message"]


def test_two_dockerfiles_are_refused_rather_than_guessed_between(service):
    """Picking one would mean scoring code the submitter did not nominate."""
    module, extracted = service
    root = extracted / "sub"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    (root / "a" / "Dockerfile").write_text("FROM python:3.11")
    (root / "b" / "Dockerfile").write_text("FROM python:3.11")

    out = module.run_submission("sub", challenge_type="dce")
    assert out["success"] is False
    assert "Multiple Dockerfiles" in out["message"]
    assert "a/Dockerfile" in out["message"] and "b/Dockerfile" in out["message"]


def test_a_path_traversing_id_cannot_escape_the_extraction_root(service, monkeypatch):
    module, _ = service
    monkeypatch.setattr(module, "execute_submission",
                        lambda *a, **k: pytest.fail("should not run"))
    out = module.run_submission("../../etc", challenge_type="dce")
    assert out["success"] is False


# ── Which directory becomes the build context ─────────────────────────────

def test_a_dockerfile_at_the_root_makes_the_root_the_build_context(service, monkeypatch,
                                                                   tmp_path):
    module, extracted = service
    root = extracted / "sub"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.11")

    calls = _capture(monkeypatch, module, tmp_path)
    module.run_submission("sub", challenge_type="dce")
    assert calls["submission_path"] == root


def test_a_wrapper_folder_is_unwrapped_to_find_the_build_context(service, monkeypatch,
                                                                 tmp_path):
    """A ZIP that extracts as team_name/Dockerfile is the common case."""
    module, extracted = service
    inner = extracted / "sub" / "team_alpha"
    inner.mkdir(parents=True)
    (inner / "Dockerfile").write_text("FROM python:3.11")

    calls = _capture(monkeypatch, module, tmp_path)
    module.run_submission("sub", challenge_type="dce")
    assert calls["submission_path"] == inner, \
        "the build context must be the folder holding the Dockerfile"


# ── Timeout resolution ────────────────────────────────────────────────────

def test_the_caller_timeout_beats_the_submission_config(service, monkeypatch, tmp_path):
    """A submission must not be able to grant itself a longer run."""
    module, extracted = service
    root = extracted / "sub"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.11")
    (root / "run_config.json").write_text(json.dumps({"timeout_seconds": 99999}))

    calls = _capture(monkeypatch, module, tmp_path)
    module.run_submission("sub", challenge_type="dce", timeout_seconds=30)
    assert calls["timeout_seconds"] == 30


def test_the_submission_config_is_used_when_the_caller_does_not_say(service, monkeypatch,
                                                                    tmp_path):
    module, extracted = service
    root = extracted / "sub"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.11")
    (root / "run_config.json").write_text(json.dumps({"timeout_seconds": 45}))

    calls = _capture(monkeypatch, module, tmp_path)
    module.run_submission("sub", challenge_type="dce")
    assert calls["timeout_seconds"] == 45


@pytest.mark.parametrize("payload", [
    "{ not json",
    json.dumps(["a", "list"]),
    json.dumps({"timeout_seconds": -5}),
    json.dumps({"timeout_seconds": "an hour"}),
    json.dumps({"timeout_seconds": 0}),
])
def test_a_malformed_run_config_falls_back_to_the_default_timeout(service, monkeypatch,
                                                                  tmp_path, payload):
    """A submission cannot disable its own timeout by writing nonsense."""
    from osipi_pipeline.execution.docker_runner import DEFAULT_TIMEOUT_SECONDS

    module, extracted = service
    root = extracted / "sub"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.11")
    (root / "run_config.json").write_text(payload)

    calls = _capture(monkeypatch, module, tmp_path)
    module.run_submission("sub", challenge_type="dce")
    assert calls["timeout_seconds"] == DEFAULT_TIMEOUT_SECONDS


# ── What the caller is told afterwards ────────────────────────────────────

@pytest.fixture()
def runnable(service):
    module, extracted = service
    root = extracted / "sub"
    root.mkdir()
    (root / "Dockerfile").write_text("FROM python:3.11")
    return module, root


def test_a_failing_container_is_still_a_successful_call(runnable, monkeypatch, tmp_path):
    """`success` means the pipeline ran, not that the submission passed.

    Conflating the two would report a broken submission as a broken pipeline.
    """
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path, exit_code=1, passed=False)
    out = module.run_submission("sub", challenge_type="dce")
    assert out["success"] is True
    assert out["passed"] is False
    assert out["exit_code"] == 1


def test_a_docker_error_is_reported_as_a_message_not_an_exception(runnable, monkeypatch):
    from osipi_pipeline.execution.docker_runner import DockerExecutionError

    module, _ = runnable

    def boom(*args, **kwargs):
        raise DockerExecutionError("daemon is not running")

    monkeypatch.setattr(module, "execute_submission", boom)
    out = module.run_submission("sub", challenge_type="dce")
    assert out["success"] is False
    assert "daemon is not running" in out["message"]


def test_an_unexpected_error_does_not_escape_the_service(runnable, monkeypatch):
    """The API layer above this returns 500 on an exception."""
    module, _ = runnable

    def boom(*args, **kwargs):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(module, "execute_submission", boom)
    out = module.run_submission("sub", challenge_type="dce")
    assert out["success"] is False
    assert "something nobody predicted" in out["message"]


def test_a_container_that_would_not_start_is_flagged_separately(runnable, monkeypatch,
                                                                tmp_path):
    """Exit 125 is Docker refusing, not the submission failing.

    The interface shows a different explanation for each, so the two must not
    be reported the same way.
    """
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path, exit_code=125, passed=False)
    out = module.run_submission("sub", challenge_type="dce")
    assert out["container_start_failed"] is True


def test_a_build_failure_is_not_mistaken_for_a_start_failure(runnable, monkeypatch,
                                                             tmp_path):
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path, exit_code=125, passed=False,
             build_failed=True)
    out = module.run_submission("sub", challenge_type="dce")
    assert out["container_start_failed"] is False


def test_a_timeout_is_not_mistaken_for_a_start_failure(runnable, monkeypatch, tmp_path):
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path, exit_code=125, passed=False, timed_out=True)
    out = module.run_submission("sub", challenge_type="dce")
    assert out["container_start_failed"] is False
    assert out["timed_out"] is True


def test_the_previews_carry_the_run_output_not_the_build_output(runnable, monkeypatch,
                                                                tmp_path):
    """A reader wants what the submission printed, not the image build log."""
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path)
    out = module.run_submission("sub", challenge_type="dce")
    assert "ran the model" in out["stdout_preview"]
    assert "building" not in out["stdout_preview"]
    assert "a warning" in out["stderr_preview"]


def test_a_container_that_would_not_start_shows_the_whole_stderr(runnable, monkeypatch,
                                                                 tmp_path):
    """On exit 125 the daemon's message is at the top, above any section."""
    module, _ = runnable
    stderr = tmp_path / "125.log"
    stderr.write_text("docker: Error response from daemon: no such image\n"
                      "## Docker run stderr\n")
    _capture(monkeypatch, module, tmp_path, exit_code=125, passed=False,
             stderr_path=str(stderr))
    out = module.run_submission("sub", challenge_type="dce")
    assert "Error response from daemon" in out["stderr_preview"]


def test_a_missing_log_file_yields_an_empty_preview_not_a_crash(runnable, monkeypatch,
                                                                tmp_path):
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path,
             stdout_path=str(tmp_path / "gone.log"),
             stderr_path=str(tmp_path / "also-gone.log"))
    out = module.run_submission("sub", challenge_type="dce")
    assert out["stdout_preview"] == "" and out["stderr_preview"] == ""


def test_a_huge_log_is_truncated_before_it_reaches_the_response(runnable, monkeypatch,
                                                                tmp_path):
    """A runaway submission can print gigabytes. The API response cannot."""
    module, _ = runnable
    stdout = tmp_path / "big.log"
    stdout.write_text("## Docker run stdout\n" + ("x" * 200_000))
    _capture(monkeypatch, module, tmp_path, stdout_path=str(stdout))
    out = module.run_submission("sub", challenge_type="dce")
    assert len(out["stdout_preview"]) <= module._LOG_PREVIEW_BYTES


def test_a_missing_output_directory_is_reported_as_a_validation_error(runnable,
                                                                      monkeypatch,
                                                                      tmp_path):
    """A container can exit 0 and write nothing. That is not a pass."""
    module, _ = runnable
    _capture(monkeypatch, module, tmp_path, output_path=str(tmp_path / "never-made"))
    out = module.run_submission("sub", challenge_type="dce")
    validation = out["output_validation"]
    assert validation["passed"] is False
    assert validation["errors"][0]["code"] == "OUTPUT_DIR_MISSING"
    assert out["process_passed"] is True
    assert out["output_complete"] is False
    assert out["ready_for_analysis"] is False
    assert out["analysis_status"] == "output_incomplete"


def test_process_success_and_output_completeness_are_separate(runnable, monkeypatch,
                                                               tmp_path):
    module, _ = runnable
    output = tmp_path / "generated"
    output.mkdir()
    _capture(monkeypatch, module, tmp_path, output_path=str(output), passed=True)
    monkeypatch.setattr(module, "validate_generated_outputs", lambda *a, **k: {
        "passed": False,
        "output_complete": False,
        "nifti_count": 1,
        "output_files": ["CBF.nii.gz"],
        "errors": [{"code": "REQUIRED_MAP_MISSING", "message": "ATT is missing."}],
        "warnings": [],
    })

    out = module.run_submission("sub", challenge_type="asl")

    assert out["passed"] is True
    assert out["process_passed"] is True
    assert out["output_complete"] is False
    assert out["ready_for_analysis"] is False


def test_the_resource_limits_the_caller_sets_are_passed_through(runnable, monkeypatch,
                                                                tmp_path):
    module, _ = runnable
    calls = _capture(monkeypatch, module, tmp_path)
    module.run_submission("sub", challenge_type="dce",
                          memory_limit="1g", cpu_limit="0.5")
    assert calls["memory_limit"] == "1g"
    assert calls["cpu_limit"] == "0.5"
