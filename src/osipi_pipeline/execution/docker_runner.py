"""Build and run ingested submissions in restricted Docker containers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from osipi_pipeline.execution.models import ExecutionResult


DEFAULT_FALLBACK_DOCKERFILE = Path(__file__).with_name("Dockerfile.example")
DEFAULT_EXECUTION_DIR       = Path("data/outputs/execution")
DEFAULT_RUN_COMMAND         = "python3 run.py"
DEFAULT_TIMEOUT_SECONDS     = 300   # 5 minutes
DEFAULT_BUILD_TIMEOUT_SECONDS = 1800  # 30 minutes
DEFAULT_MEMORY_LIMIT        = "4g"
DEFAULT_CPU_LIMIT           = "2.0"
DEFAULT_PIDS_LIMIT          = "512"
DEFAULT_REMOVE_IMAGE        = True

# ---------------------------------------------------------------------------
# Host-path translation for containers started through the Docker socket
# ---------------------------------------------------------------------------
# The backend runs inside a container, but volume mounts in `docker run -v`
# are evaluated by the **host** Docker daemon, so paths must be host-visible.
#
# docker-compose.yml sets these env vars to the absolute host paths:
#   HOST_SUBMISSIONS_DIR      → /host/path/to/project/submissions
#   HOST_OUTPUTS_DIR          → /host/path/to/project/data/outputs
#   HOST_REFERENCE_DATA_DIR   → /host/path/to/project/data/reference_data
#
# When the env vars are absent (local development without Docker), paths are
# passed through unchanged.
_DOOD_MAP: list[tuple[Path, str]] = [
    (Path("/app/submissions"),        "HOST_SUBMISSIONS_DIR"),
    (Path("/app/data/outputs"),       "HOST_OUTPUTS_DIR"),
    (Path("/app/data/reference_data"), "HOST_REFERENCE_DATA_DIR"),
]


def _to_host_path(container_path: Path) -> Path:
    """Translate a container-internal path to its host-visible equivalent."""
    resolved = container_path.resolve()
    for container_root, env_var in _DOOD_MAP:
        host_root_str = os.environ.get(env_var, "").strip()
        if not host_root_str:
            continue
        try:
            rel = resolved.relative_to(container_root)
            return Path(host_root_str) / rel
        except ValueError:
            continue
    return resolved


#: Reported when `docker build` is killed for exceeding its timeout. 124 is the
#: conventional shell timeout code and matches what the run path already uses.
_BUILD_TIMEOUT_EXIT_CODE = 124

#: Cleanup must not outlast the work it cleans up after.
_IMAGE_REMOVAL_TIMEOUT_SECONDS = 60


class DockerExecutionError(RuntimeError):
    """Raised for pre-flight failures such as unavailable Docker or bad paths."""


def execute_submission(
    submission_path: str | Path,
    *,
    challenge_type: str,
    command: str | None = None,
    output_dir: str | Path = DEFAULT_EXECUTION_DIR,
    fallback_dockerfile: str | Path = DEFAULT_FALLBACK_DOCKERFILE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    build_timeout_seconds: int = DEFAULT_BUILD_TIMEOUT_SECONDS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    cpu_limit: str = DEFAULT_CPU_LIMIT,
    pids_limit: str = DEFAULT_PIDS_LIMIT,
    remove_image: bool = DEFAULT_REMOVE_IMAGE,
    input_dir: str | Path | None = None,
) -> ExecutionResult:
    """Build a Docker image from the submission, run it, and save all artefacts."""
    submission = Path(submission_path).expanduser()
    if not submission.exists():
        raise DockerExecutionError(f"Submission folder does not exist: {submission}")
    if not submission.is_dir():
        raise DockerExecutionError(f"Submission path must be a folder: {submission}")
    if shutil.which("docker") is None:
        raise DockerExecutionError(
            "Docker is not installed or is not available on PATH."
        )

    # ── Resolve command (and optionally timeout) from run_config.json ─────────
    if command is None:
        cfg = _read_run_config(submission)
        command = cfg["command"] or DEFAULT_RUN_COMMAND
        # Use run_config timeout only when the caller left the default unchanged
        if cfg["timeout_seconds"] is not None and timeout_seconds == DEFAULT_TIMEOUT_SECONDS:
            timeout_seconds = cfg["timeout_seconds"]

    # ── Set up run directory ──────────────────────────────────────────────────
    run_dir = (
        Path(output_dir)
        / f"{_safe_name(challenge_type)}_{_safe_name(submission.name)}"
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path      = run_dir / "execution_stdout.log"
    stderr_path      = run_dir / "execution_stderr.log"
    output_host_path = run_dir / "outputs"
    output_host_path.mkdir(parents=True, exist_ok=True)

    dockerfile  = detect_dockerfile(submission, fallback_dockerfile=fallback_dockerfile)
    image_name  = _image_name(challenge_type, submission.name)
    started_at  = _timestamp()

    # ── Docker build ──────────────────────────────────────────────────────────
    build_result, build_cmd = _run_docker_build(
        image_name,
        dockerfile,
        _build_context(dockerfile, submission),
        timeout_seconds=build_timeout_seconds,
    )
    build_cmd_str = " ".join(build_cmd)
    if build_result.returncode != 0:
        _write_logs(
            stdout_path, stderr_path,
            _section("Docker build command", build_cmd_str)
            + _section("Docker build stdout", build_result.stdout),
            _section("Docker build stderr", build_result.stderr),
        )
        return ExecutionResult(
            submission_path=str(submission.resolve()),
            challenge_type=challenge_type.lower(),
            image_name=image_name,
            command=command,
            exit_code=build_result.returncode,
            stdout_path=str(stdout_path.resolve()),
            stderr_path=str(stderr_path.resolve()),
            started_at=started_at,
            finished_at=_timestamp(),
            passed=False,
            timed_out=build_result.returncode == _BUILD_TIMEOUT_EXIT_CODE,
            build_failed=True,
            docker_build_cmd=build_cmd_str,
        )

    # ── Docker run ────────────────────────────────────────────────────────────
    timed_out  = False
    run_cmd: list[str] = []   # initialized before try so it's always bound
    input_path = Path(input_dir).expanduser() if input_dir else None
    try:
        run_result, run_cmd = _run_docker_container(
            image_name,
            submission,
            output_host_path,
            command,
            memory_limit,
            cpu_limit,
            timeout_seconds,
            pids_limit=pids_limit,
            input_host_path=input_path,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        run_result = subprocess.CompletedProcess(
            [], 124, stdout="", stderr="Execution timed out."
        )

    run_cmd_str = " ".join(run_cmd)
    finished_at = _timestamp()
    _write_logs(
        stdout_path,
        stderr_path,
        _section("Docker build command", build_cmd_str)
        + _section("Docker run command", run_cmd_str)
        + _section("Docker build stdout", build_result.stdout)
        + _section("Docker run stdout", run_result.stdout),
        _section("Docker build stderr", build_result.stderr)
        + _section("Docker run stderr", run_result.stderr),
    )

    # ── Collect all output files ──────────────────────────────────────────────
    output_files = _collect_output_files(output_host_path)

    # Outputs are already on disk, so the image has nothing left to give.
    if remove_image:
        _remove_image(image_name)

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
        passed=run_result.returncode == 0 and not timed_out,
        output_path=str(output_host_path.resolve()),
        output_files=tuple(output_files),
        timed_out=timed_out,
        docker_build_cmd=build_cmd_str,
        docker_run_cmd=run_cmd_str,
    )


def detect_dockerfile(
    submission_path: str | Path,
    *,
    fallback_dockerfile: str | Path = DEFAULT_FALLBACK_DOCKERFILE,
) -> Path:
    """Return the Dockerfile to use: submission's own, or the pipeline fallback."""
    submission = Path(submission_path)
    submission_dockerfile = submission / "Dockerfile"
    if submission_dockerfile.is_file():
        return submission_dockerfile

    fallback = Path(fallback_dockerfile)
    if fallback.is_file():
        return fallback

    raise DockerExecutionError(
        f"No Dockerfile found in submission and fallback is missing: {fallback}"
    )


def _read_run_config(submission: Path) -> dict:
    """Parse ``run_config.json`` from the submission folder."""
    config_path = submission / "run_config.json"
    result: dict = {"command": None, "timeout_seconds": None}
    if not config_path.is_file():
        return result
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return result
        if isinstance(cfg.get("command"), str):
            cmd = cfg["command"].strip()
            if cmd:
                result["command"] = cmd
        raw_timeout = cfg.get("timeout_seconds")
        if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
            result["timeout_seconds"] = int(raw_timeout)
    except (json.JSONDecodeError, OSError):
        pass
    return result


def _run_docker_build(
    image_name: str,
    dockerfile: Path,
    build_context: Path,
    *,
    timeout_seconds: int = DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Build the Docker image and return (result, command_list)."""
    cmd = [
        "docker", "build",
        "-t", image_name,
        "-f", str(dockerfile),
        str(build_context),
    ]
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds
        ), cmd
    except subprocess.TimeoutExpired as expired:
        return subprocess.CompletedProcess(
            cmd,
            _BUILD_TIMEOUT_EXIT_CODE,
            stdout=_decode(expired.stdout),
            stderr=(
                _decode(expired.stderr)
                + f"\nDocker build timed out after {timeout_seconds} seconds."
            ),
        ), cmd


def _decode(stream: str | bytes | None) -> str:
    """Output captured before a timeout, which may be bytes or absent."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def _run_docker_container(
    image_name: str,
    submission: Path,
    output_host_path: Path,
    command: str,
    memory_limit: str,
    cpu_limit: str,
    timeout_seconds: int,
    *,
    pids_limit: str = DEFAULT_PIDS_LIMIT,
    input_host_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the Docker container with security and resource constraints."""
    # Translate container paths → host paths for the volume mounts
    host_submission   = _to_host_path(submission.resolve())
    host_output       = _to_host_path(output_host_path.resolve())

    cmd = [
        "docker", "run", "--rm",
        # --- submission mount (read-only) ---
        "-v", f"{host_submission}:/submission:ro",
        # --- output mount (read-write) ---
        "-v", f"{host_output}:/output:rw",
    ]
    # --- optional challenge input data mount (read-only) ---
    if input_host_path is not None and input_host_path.is_dir():
        host_input = _to_host_path(input_host_path.resolve())
        cmd += ["-v", f"{host_input}:/input:ro"]
    cmd += [
        # --- working directory inside container ---
        "-w", "/submission",
        # --- resource limits ---
        "--memory", memory_limit,
        "--cpus",   cpu_limit,
        "--pids-limit", str(pids_limit),
        # --- security ---
        "--network",      "none",
        "--security-opt", "no-new-privileges",
        image_name,
        "sh", "-lc", command,
    ]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    ), cmd


def _remove_image(image_name: str) -> bool:
    """Delete the built image."""
    try:
        result = subprocess.run(
            ["docker", "image", "rm", "-f", image_name],
            capture_output=True, text=True, timeout=_IMAGE_REMOVAL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _collect_output_files(output_dir: Path) -> list[str]:
    """Return all file paths found in the output directory, relative to it."""
    if not output_dir.exists():
        return []
    return sorted(
        str(p.relative_to(output_dir))
        for p in output_dir.rglob("*")
        if p.is_file()
    )


def _build_context(dockerfile: Path, submission: Path) -> Path:
    """Use the submission as build context only when its own Dockerfile is used."""
    if dockerfile.resolve() == (submission / "Dockerfile").resolve():
        return submission
    return dockerfile.parent


def _write_logs(
    stdout_path: Path,
    stderr_path: Path,
    stdout_text: str,
    stderr_text: str,
) -> None:
    stdout_path.write_text(stdout_text, encoding="utf-8")
    stderr_path.write_text(stderr_text, encoding="utf-8")


def _section(title: str, text: str) -> str:
    """Format a named log section."""
    return f"## {title}\n{text or ''}\n"


def _image_name(challenge_type: str, submission_id: str) -> str:
    return f"osipi-{_safe_name(challenge_type)}-{_safe_name(submission_id)}:latest"


def _safe_name(value: str) -> str:
    safe = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in safe.split("-") if part) or "submission"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
