"""Build and run ingested submissions with Docker.

Execution v2 additions over v1:
  - Per-submission ``run_config.json`` for custom run commands.
  - Dedicated ``/output`` mount (read-write) so submitted code can write results.
  - Resource limits: ``--memory``, ``--cpus``, ``--network none``,
    ``--security-opt no-new-privileges``.
  - ``timeout_seconds`` enforced via ``subprocess.run(timeout=...)``.
  - Output NIfTI file collection after the run.
  - All artefacts for one run are stored under a single run directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from osipi_pipeline.execution.models import ExecutionResult

# ---------------------------------------------------------------------------
# Defaults — can be overridden per call or via run_config.json
# ---------------------------------------------------------------------------

DEFAULT_FALLBACK_DOCKERFILE = Path("docker/Dockerfile.example")
DEFAULT_EXECUTION_DIR       = Path("data/outputs/execution")
DEFAULT_RUN_COMMAND         = "python3 run.py"
DEFAULT_TIMEOUT_SECONDS     = 300   # 5 minutes
DEFAULT_MEMORY_LIMIT        = "4g"
DEFAULT_CPU_LIMIT           = "2.0"

NIFTI_SUFFIXES = (".nii", ".nii.gz")


class DockerExecutionError(RuntimeError):
    """Raised when Docker execution cannot start or build safely."""


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
) -> ExecutionResult:
    """Build a Docker image from the submission, run it, and save all artefacts.

    Directory layout created under ``output_dir``:

        {output_dir}/{challenge_type}_{submission_name}/
            execution_stdout.log   — combined build + run stdout
            execution_stderr.log   — combined build + run stderr
            outputs/               — mounted at /output inside the container
                *.nii.gz …         — files written by the submission

    Args:
        submission_path:   Path to the already-ingested submission folder.
        challenge_type:    Challenge identifier (``"dce"``, ``"asl"``, ``"dsc"``).
        command:           Shell command to run inside the container.  ``None``
                           means auto-resolve from ``run_config.json``, then
                           fall back to ``DEFAULT_RUN_COMMAND``.
        output_dir:        Parent directory for execution artefacts.
        fallback_dockerfile: Dockerfile to use when the submission has none.
        timeout_seconds:   Kill the container run after this many seconds.
        memory_limit:      Docker ``--memory`` value (e.g. ``"4g"``).
        cpu_limit:         Docker ``--cpus`` value (e.g. ``"2.0"``).

    Returns:
        An :class:`ExecutionResult` describing the outcome.

    Raises:
        DockerExecutionError: Docker is not installed, the submission path is
            invalid, or the Docker *build* step fails.
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

    # ── Resolve command ───────────────────────────────────────────────────────
    if command is None:
        command = _read_run_command(submission)

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
    build_result = _run_docker_build(
        image_name,
        dockerfile,
        _build_context(dockerfile, submission),
    )
    if build_result.returncode != 0:
        _write_logs(
            stdout_path, stderr_path,
            _section("Docker build stdout", build_result.stdout),
            _section("Docker build stderr", build_result.stderr),
        )
        raise DockerExecutionError(
            f"Docker build failed (exit {build_result.returncode}). "
            f"See logs: {stdout_path}"
        )

    # ── Docker run ────────────────────────────────────────────────────────────
    timed_out = False
    try:
        run_result = _run_docker_container(
            image_name,
            submission,
            output_host_path,
            command,
            memory_limit,
            cpu_limit,
            timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        timed_out  = True
        run_result = subprocess.CompletedProcess(
            [], 124, stdout="", stderr="Execution timed out."
        )

    finished_at = _timestamp()
    _write_logs(
        stdout_path,
        stderr_path,
        _section("Docker build stdout", build_result.stdout)
        + _section("Docker run stdout", run_result.stdout),
        _section("Docker build stderr", build_result.stderr)
        + _section("Docker run stderr", run_result.stderr),
    )

    # ── Collect output NIfTI files ────────────────────────────────────────────
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


def _read_run_command(submission: Path) -> str:
    """Read the run command from ``run_config.json`` if present, else use default.

    ``run_config.json`` format::

        {
            "command": "python3 run.py --input /submission --output /output"
        }

    Only the ``"command"`` key is used.  Any other keys are ignored.
    """
    config_path = submission / "run_config.json"
    if config_path.is_file():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(cfg, dict) and isinstance(cfg.get("command"), str):
                cmd = cfg["command"].strip()
                if cmd:
                    return cmd
        except (json.JSONDecodeError, OSError):
            pass  # fall through to default
    return DEFAULT_RUN_COMMAND


def _run_docker_build(
    image_name: str,
    dockerfile: Path,
    build_context: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker", "build",
            "-t", image_name,
            "-f", str(dockerfile),
            str(build_context),
        ],
        capture_output=True,
        text=True,
    )


def _run_docker_container(
    image_name: str,
    submission: Path,
    output_host_path: Path,
    command: str,
    memory_limit: str,
    cpu_limit: str,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run the Docker container with security and resource constraints.

    Mounts:
        ``/submission`` — read-only view of the submission folder.
        ``/output``     — read-write directory for submission outputs.

    Security:
        ``--network none``               — no outbound network access.
        ``--security-opt no-new-privileges`` — prevent privilege escalation.
    """
    return subprocess.run(
        [
            "docker", "run", "--rm",
            # --- submission mount (read-only) ---
            "-v", f"{submission.resolve()}:/submission:ro",
            # --- output mount (read-write) ---
            "-v", f"{output_host_path.resolve()}:/output:rw",
            # --- resource limits ---
            "--memory", memory_limit,
            "--cpus",   cpu_limit,
            # --- security ---
            "--network",      "none",
            "--security-opt", "no-new-privileges",
            image_name,
            "sh", "-lc", command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _collect_output_files(output_dir: Path) -> list[str]:
    """Return NIfTI file paths found in the output directory, relative to it."""
    if not output_dir.exists():
        return []
    return sorted(
        str(p.relative_to(output_dir))
        for p in output_dir.rglob("*")
        if p.is_file() and p.name.lower().endswith(NIFTI_SUFFIXES)
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
