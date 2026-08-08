"""Build and run ingested submissions with Docker.

Execution v2 additions over v1:
  - Per-submission ``run_config.json`` for custom run commands and timeout.
  - Dedicated ``/output`` mount (read-write) so submitted code can write results.
  - Resource limits: ``--memory``, ``--cpus``, ``--network none``,
    ``--security-opt no-new-privileges``.
  - ``timeout_seconds`` enforced via ``subprocess.run(timeout=...)``.
  - Output file collection (all files, not just NIfTI) after the run.
  - All artefacts for one run stored under a single run directory.
  - Build failures return an ``ExecutionResult`` with ``build_failed=True``
    (logs are saved); callers no longer need to catch ``DockerExecutionError``
    for the build step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from osipi_pipeline.execution.models import ExecutionResult

# ---------------------------------------------------------------------------
# Defaults: can be overridden per call or via run_config.json
# ---------------------------------------------------------------------------

DEFAULT_FALLBACK_DOCKERFILE = Path("docker/Dockerfile.example")
DEFAULT_EXECUTION_DIR       = Path("data/outputs/execution")
DEFAULT_RUN_COMMAND         = "python3 run.py"
DEFAULT_TIMEOUT_SECONDS     = 300   # 5 minutes
DEFAULT_MEMORY_LIMIT        = "4g"
DEFAULT_CPU_LIMIT           = "2.0"

# ---------------------------------------------------------------------------
# Docker-outside-of-Docker (DooD) path translation
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
    """Translate a container-internal path to its host-visible equivalent.

    Uses the ``HOST_*`` env vars injected by docker-compose.  Falls back to
    the original path when the env vars are not set (bare-metal / local dev).
    """
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


class DockerExecutionError(RuntimeError):
    """Raised for *pre-flight* failures: Docker not installed, bad path, etc.

    Build and run failures do NOT raise this error, they are captured in the
    returned ``ExecutionResult`` (``build_failed=True`` or ``passed=False``).
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_submission(
    submission_path: str | Path,
    *,
    challenge_type: str,
    command: str | None = None,
    output_dir: str | Path = DEFAULT_EXECUTION_DIR,
    fallback_dockerfile: str | Path = DEFAULT_FALLBACK_DOCKERFILE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    cpu_limit: str = DEFAULT_CPU_LIMIT,
    input_dir: str | Path | None = None,
) -> ExecutionResult:
    """Build a Docker image from the submission, run it, and save all artefacts.

    Directory layout created under ``output_dir``:

        {output_dir}/{challenge_type}_{submission_name}/
            execution_stdout.log  , combined build + run stdout
            execution_stderr.log  , combined build + run stderr
            outputs/              , mounted at /output inside the container
                <any files>       , all files written by the submission

    Pre-flight failures (Docker not installed, bad path) raise
    :class:`DockerExecutionError`.

    Build failures do **not** raise: they return an ``ExecutionResult`` with
    ``build_failed=True`` and ``passed=False``.  Log files are written so the
    caller can show build output to the user.

    Args:
        submission_path:     Path to the already-ingested submission folder.
        challenge_type:      Configured challenge identifier.
        command:             Shell command to run inside the container.  ``None``
                             means auto-resolve from ``run_config.json``, then
                             fall back to ``DEFAULT_RUN_COMMAND``.
        output_dir:          Parent directory for execution artefacts.
        fallback_dockerfile: Dockerfile to use when the submission has none.
        timeout_seconds:     Kill the container run after this many seconds.
                             ``run_config.json`` ``timeout_seconds`` can override
                             the code default when ``command`` is also auto-resolved.
        memory_limit:        Docker ``--memory`` value (e.g. ``"4g"``).
        cpu_limit:           Docker ``--cpus`` value (e.g. ``"2.0"``).

    Returns:
        An :class:`ExecutionResult` describing the outcome.

    Raises:
        DockerExecutionError: Docker not installed, path invalid.
    """
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_run_config(submission: Path) -> dict:
    """Parse ``run_config.json`` from the submission folder.

    Recognised keys:

    ``command``
        Shell command to run inside the container.
        Default: ``None`` (falls back to ``DEFAULT_RUN_COMMAND``).

    ``timeout_seconds``
        Integer number of seconds before the container is killed.
        Default: ``None`` (falls back to ``DEFAULT_TIMEOUT_SECONDS``).

    Example ``run_config.json``::

        {
            "command": "python /submission/run.py --output /output",
            "timeout_seconds": 600
        }

    Unknown keys are silently ignored.  Parse errors return empty config.
    """
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
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Build the Docker image and return (result, command_list)."""
    cmd = [
        "docker", "build",
        "-t", image_name,
        "-f", str(dockerfile),
        str(build_context),
    ]
    return subprocess.run(cmd, capture_output=True, text=True), cmd


def _run_docker_container(
    image_name: str,
    submission: Path,
    output_host_path: Path,
    command: str,
    memory_limit: str,
    cpu_limit: str,
    timeout_seconds: int,
    *,
    input_host_path: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the Docker container with security and resource constraints.

    Mounts:
        ``/submission``, read-only view of the submission folder.
        ``/output``    , read-write directory for submission outputs.
        ``/input``     , read-only challenge input data (when provided).

    Security:
        ``--network none``              , no outbound network access.
        ``--security-opt no-new-privileges``, prevent privilege escalation.

    DooD path translation:
        Paths are translated from container-internal paths to host-visible
        equivalents via ``_to_host_path()`` before being passed to docker run,
        because the Docker daemon receiving the command runs on the *host*
        machine and does not know the backend container's internal filesystem.
    """
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


def _collect_output_files(output_dir: Path) -> list[str]:
    """Return all file paths found in the output directory, relative to it.

    Collects every file written by the submission (not just NIfTI), so the
    caller can report what was produced.
    """
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
