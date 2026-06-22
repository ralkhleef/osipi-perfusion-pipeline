"""Data models for Docker execution results."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExecutionResult:
    """A summary of one Docker execution attempt.

    Fields with defaults (``output_path``, ``output_files``, ``timed_out``) are
    optional so that existing code that constructs ``ExecutionResult`` with only
    the core fields continues to work without modification.
    """

    submission_path: str
    challenge_type: str
    image_name: str
    command: str
    exit_code: int
    stdout_path: str
    stderr_path: str
    started_at: str
    finished_at: str
    passed: bool
    # --- fields added in execution v2 ---
    output_path: str = ""
    output_files: tuple[str, ...] = ()
    timed_out: bool = False

    def to_dict(self) -> dict[str, object]:
        """Convert the result into a plain dictionary.

        ``output_files`` is serialised as a list for JSON compatibility.
        """
        d = asdict(self)
        d["output_files"] = list(d["output_files"])
        return d
