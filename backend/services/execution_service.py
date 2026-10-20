"""FastAPI service layer for Docker execution."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR   = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from osipi_pipeline.execution.docker_runner import (  # noqa: E402
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    DockerExecutionError,
    execute_submission,
)

from services.ingest_service import make_safe_id  # noqa: E402
from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR  # noqa: E402
from services.validation_service import validate_generated_outputs  # noqa: E402

# Number of bytes from each log file to include in the API response.
_LOG_PREVIEW_BYTES = 8 * 1024  # 8 KB


def run_submission(
    submission_id: str,
    *,
    challenge_type: str,
    timeout_seconds: Optional[int] = None,
    memory_limit: str = DEFAULT_MEMORY_LIMIT,
    cpu_limit: str = DEFAULT_CPU_LIMIT,
    input_dir: Optional[Path] = None,
    map_type: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        safe_id = make_safe_id(submission_id)
    except Exception:
        return _err(f"Submission ID is invalid: {submission_id!r}")
    submission_path = EXTRACTED_DIR / safe_id
    try:
        submission_path.resolve().relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        return _err("Submission ID contains an invalid path.")

    if not submission_path.exists():
        return _err(f"Submission not found: {submission_id!r}")
    if not submission_path.is_dir():
        return _err(f"Submission path is not a directory: {submission_id!r}")

    # ── Locate Dockerfile (handles wrapper-folder ZIPs) ──────────────────────
    effective_root, dockerfile_err = _find_execution_root(submission_path)
    if dockerfile_err:
        return _err(dockerfile_err)

    # ── Resolve timeout from run_config.json when caller did not specify ──────
    effective_timeout = timeout_seconds
    if effective_timeout is None:
        cfg = _read_run_config(effective_root)
        effective_timeout = cfg.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS

    # ── Execute ───────────────────────────────────────────────────────────────
    try:
        abs_output_dir = OUTPUTS_DIR / "execution"
        result = execute_submission(
            effective_root,
            challenge_type=challenge_type,
            output_dir=abs_output_dir,
            timeout_seconds=effective_timeout,
            memory_limit=memory_limit,
            cpu_limit=cpu_limit,
            input_dir=input_dir,
        )
    except DockerExecutionError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _err(f"Unexpected error during execution: {exc}")

    # ── Post-execution output validation ──────────────────────────────────────
    output_host_path = Path(result.output_path) if result.output_path else None
    if output_host_path and output_host_path.is_dir():
        output_validation = validate_generated_outputs(
            output_host_path,
            challenge_type=challenge_type,
            map_type=map_type,
        )
    else:
        output_validation = {
            "passed": False,
            "nifti_count": 0,
            "output_files": [],
            "errors": [{"severity": "error", "code": "OUTPUT_DIR_MISSING",
                        "message": "Output directory was not created during execution.", "path": None}],
            "warnings": [],
        }

    # ── Build response ────────────────────────────────────────────────────────
    data: Dict[str, Any] = {"success": True, **result.to_dict()}
    data["stdout_preview"]    = _read_log_section(result.stdout_path, "run stdout")
    # For exit 125 (container couldn't start), read full stderr, it contains
    # the docker daemon's error message, which is the most useful debug signal.
    if result.exit_code == 125 and not result.build_failed:
        data["stderr_preview"] = _read_file_head(result.stderr_path)
    else:
        data["stderr_preview"] = _read_log_section(result.stderr_path, "run stderr")
    data["output_validation"]    = output_validation
    data["process_passed"]       = bool(result.passed)
    data["output_complete"]      = bool(output_validation.get("passed"))
    data["ready_for_analysis"]   = bool(result.passed and output_validation.get("passed"))
    data["analysis_status"]      = (
        "ready" if data["ready_for_analysis"]
        else "output_incomplete" if result.passed
        else "execution_failed"
    )
    data["container_start_failed"] = (
        result.exit_code == 125 and not result.build_failed and not result.timed_out
    )
    data.setdefault("output_file_count", len(result.output_files))
    return data


def _read_log_section(log_path: str, section_keyword: str) -> str:
    """Return the content of a named section from a combined log file.

    Log files are formatted as::

        ## Docker build stdout
        ...
        ## Docker run stdout
        ...

    If the section cannot be found, returns the last ``_LOG_PREVIEW_BYTES``
    of the whole file so *something* is always visible.
    """
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    # Find the section header (case-insensitive keyword match)
    keyword_lower = section_keyword.lower()
    lines = content.splitlines(keepends=True)
    section_start: Optional[int] = None
    section_end: Optional[int] = None

    for i, line in enumerate(lines):
        if line.startswith("## ") and keyword_lower in line.lower():
            section_start = i + 1  # content starts after the header
        elif section_start is not None and line.startswith("## "):
            section_end = i
            break

    if section_start is not None:
        chunk = "".join(lines[section_start:section_end])
    else:
        # Section header not found: return tail of the file
        chunk = content

    # Return the first _LOG_PREVIEW_BYTES characters of the extracted chunk
    return chunk[:_LOG_PREVIEW_BYTES]


def _read_file_head(log_path: str) -> str:
    """Return up to _LOG_PREVIEW_BYTES from the start of a log file."""
    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return content[:_LOG_PREVIEW_BYTES]


def _find_execution_root(submission_path: Path) -> tuple:
    """Return the directory containing the Dockerfile to use for execution."""
    # Fast path: Dockerfile at the submission root
    if (submission_path / "Dockerfile").is_file():
        return submission_path, None

    # Recursive search
    found = [p for p in submission_path.rglob("Dockerfile") if p.is_file()]

    if not found:
        return None, (
            "No Dockerfile found in the submission. "
            "Add a Dockerfile to enable Docker execution."
        )

    if len(found) > 1:
        rel_paths = ", ".join(
            str(p.relative_to(submission_path)) for p in sorted(found)
        )
        return None, (
            f"Multiple Dockerfiles found: {rel_paths}. "
            "Keep a single Dockerfile at the submission root (or in exactly "
            "one top-level folder) to enable execution."
        )

    # Exactly one Dockerfile found in a subdirectory, use its parent as root
    effective_root = found[0].parent
    return effective_root, None


def _read_run_config(submission_path: Path) -> dict:
    """Read ``run_config.json`` from a submission folder."""
    config_path = submission_path / "run_config.json"
    result: dict = {"command": None, "timeout_seconds": None}
    if not config_path.is_file():
        return result
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            return result
        if isinstance(cfg.get("command"), str) and cfg["command"].strip():
            result["command"] = cfg["command"].strip()
        raw_timeout = cfg.get("timeout_seconds")
        if isinstance(raw_timeout, (int, float)) and raw_timeout > 0:
            result["timeout_seconds"] = int(raw_timeout)
    except (json.JSONDecodeError, OSError):
        pass
    return result


def _err(message: str) -> Dict[str, Any]:
    return {"success": False, "message": message}
