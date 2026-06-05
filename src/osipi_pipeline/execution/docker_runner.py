"""Build and run ingested submissions with Docker."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess

from osipi_pipeline.execution.models import ExecutionResult

DEFAULT_FALLBACK_DOCKERFILE = Path("docker/Dockerfile.example")
DEFAULT_EXECUTION_DIR = Path("data/outputs/execution")
DEFAULT_RUN_COMMAND = 'echo "OSIPI execution placeholder"'


class DockerExecutionError(RuntimeError):
    """Raised when Docker execution cannot start or build safely."""


def execute_submission(
    submission_path: str | Path,
    *,
    challenge_type: str,
    command: str = DEFAULT_RUN_COMMAND,
    output_dir: str | Path = DEFAULT_EXECUTION_DIR,
    fallback_dockerfile: str | Path = DEFAULT_FALLBACK_DOCKERFILE,
) -> ExecutionResult:
    """Build a Docker image, run a placeholder command, and save logs."""

    submission = Path(submission_path).expanduser()
    if not submission.exists():
        raise DockerExecutionError(f"Submission folder does not exist: {submission}")
    if not submission.is_dir():
        raise DockerExecutionError(f"Submission path must be a folder: {submission}")
    if shutil.which("docker") is None:
        raise DockerExecutionError("Docker is not installed or is not available on PATH.")

    dockerfile = detect_dockerfile(submission, fallback_dockerfile=fallback_dockerfile)
    image_name = _image_name(challenge_type, submission.name)
    stdout_path, stderr_path = _log_paths(output_dir, challenge_type, submission.name)
    started_at = _timestamp()

    build_result = _run_docker_build(image_name, dockerfile, _build_context(dockerfile, submission))
    if build_result.returncode != 0:
        _write_logs(stdout_path, stderr_path, build_result.stdout, build_result.stderr)
        raise DockerExecutionError(
            f"Docker build failed with exit code {build_result.returncode}. "
            f"See logs: {stdout_path}, {stderr_path}"
        )

    run_result = _run_docker_container(image_name, submission, command)
    finished_at = _timestamp()
    _write_logs(
        stdout_path,
        stderr_path,
        _format_log("Docker build stdout", build_result.stdout) + _format_log("Docker run stdout", run_result.stdout),
        _format_log("Docker build stderr", build_result.stderr) + _format_log("Docker run stderr", run_result.stderr),
    )

    return ExecutionResult(
        submission_path=str(submission.resolve()),
        challenge_type=challenge_type.lower(),
        image_name=image_name,
        command=command,
        exit_code=run_result.returncode,
        stdout_path=str(stdout_path.resolve()),
        stderr_path=str(stderr_path.resolve()),
        started_at=started_at,
        finished_at=finished_at,
        passed=run_result.returncode == 0,
    )


def detect_dockerfile(
    submission_path: str | Path,
    *,
    fallback_dockerfile: str | Path = DEFAULT_FALLBACK_DOCKERFILE,
) -> Path:
    """Use the submission Dockerfile when present, otherwise use the fallback."""

    submission = Path(submission_path)
    submission_dockerfile = submission / "Dockerfile"
    if submission_dockerfile.is_file():
        return submission_dockerfile

    fallback = Path(fallback_dockerfile)
    if fallback.is_file():
        return fallback

    raise DockerExecutionError(f"No Dockerfile found and fallback Dockerfile is missing: {fallback}")


def _run_docker_build(image_name: str, dockerfile: Path, build_context: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "build", "-t", image_name, "-f", str(dockerfile), str(build_context)],
        capture_output=True,
        text=True,
    )


def _run_docker_container(image_name: str, submission: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{submission.resolve()}:/submission:ro",
            image_name,
            "sh",
            "-lc",
            command,
        ],
        capture_output=True,
        text=True,
    )


def _build_context(dockerfile: Path, submission: Path) -> Path:
    if dockerfile.resolve() == (submission / "Dockerfile").resolve():
        return submission
    return dockerfile.parent


def _log_paths(output_dir: str | Path, challenge_type: str, submission_id: str) -> tuple[Path, Path]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    base_name = f"{_safe_name(challenge_type)}_{_safe_name(submission_id)}_execution"
    return output_path / f"{base_name}_stdout.log", output_path / f"{base_name}_stderr.log"


def _write_logs(stdout_path: Path, stderr_path: Path, stdout_text: str, stderr_text: str) -> None:
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")


def _format_log(title: str, text: str) -> str:
    return f"## {title}\n{text or ''}\n"


def _image_name(challenge_type: str, submission_id: str) -> str:
    return f"osipi-{_safe_name(challenge_type)}-{_safe_name(submission_id)}:latest"


def _safe_name(value: str) -> str:
    safe = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in safe.split("-") if part) or "submission"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
