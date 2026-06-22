"""FastAPI service layer for Docker execution.

Resolves a ``submission_id`` to its extracted folder path, delegates to the
pipeline's :func:`execute_submission` function, and returns a JSON-serialisable
result dict with log previews.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Locate the pipeline package regardless of working directory.
# The package lives at  <repo-root>/src/osipi_pipeline/
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR   = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from osipi_pipeline.execution.docker_runner import (  # noqa: E402
    DEFAULT_CPU_LIMIT,
    DEFAULT_EXECUTION_DIR,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    DockerExecutionError,
    execute_submission,
)

from services.path_config import EXTRACTED_DIR  # noqa: E402

# Number of bytes from each log file to include in the API response.
_LOG_PREVIEW_BYTES = 8 * 1024  # 8 KB


def run_submission(
    submission_id: str,
    *,
    challenge_type: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    cpu_limit: str = DEFAULT_CPU_LIMIT,
) -> Dict[str, Any]:
    """Execute a previously ingested submission inside Docker.

    Args:
        submission_id:   ID returned by the ingestion API.
        challenge_type:  Challenge string (``"dce"``, ``"asl"``, ``"dsc"``).
        timeout_seconds: Kill the container after this many seconds.
        memory_limit:    Docker ``--memory`` value (e.g. ``"4g"``).
        cpu_limit:       Docker ``--cpus`` value (e.g. ``"2.0"``).

    Returns:
        A plain dict with the full :class:`ExecutionResult` fields plus
        ``stdout_preview`` and ``stderr_preview`` (first 8 KB of each log).
        On failure, returns ``{"success": False, "message": "<reason>"}``.
    """
    # ── Resolve and validate path ─────────────────────────────────────────────
    submission_path = EXTRACTED_DIR / submission_id
    try:
        submission_path.resolve().relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        return _err(f"Submission ID contains an invalid path: {submission_id!r}")

    if not submission_path.exists():
        return _err(f"Submission not found: {submission_id!r}")
    if not submission_path.is_dir():
        return _err(f"Submission path is not a directory: {submission_id!r}")

    # ── Execute ───────────────────────────────────────────────────────────────
    try:
        result = execute_submission(
            submission_path,
            challenge_type=challenge_type,
            output_dir=DEFAULT_EXECUTION_DIR,
            timeout_seconds=timeout_seconds,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
        )
    except DockerExecutionError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"Unexpected error during execution: {exc}")

    # ── Build response ────────────────────────────────────────────────────────
    data: Dict[str, Any] = {"success": True, **result.to_dict()}
    data["stdout_preview"] = _read_preview(result.stdout_path)
    data["stderr_preview"] = _read_preview(result.stderr_path)
    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_preview(log_path: str) -> str:
    """Return the first ``_LOG_PREVIEW_BYTES`` bytes of a log file as text."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(_LOG_PREVIEW_BYTES)
    except OSError:
        return ""


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "message": message}
