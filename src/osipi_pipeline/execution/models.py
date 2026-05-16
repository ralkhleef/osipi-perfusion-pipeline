"""Data models for Docker execution results."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ExecutionResult:
    """A summary of one Docker execution attempt."""

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

    def to_dict(self) -> dict[str, object]:
        """Convert the result into a plain dictionary."""

        return asdict(self)

