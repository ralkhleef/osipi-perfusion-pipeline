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
    output_path: str = ""
    output_files: tuple[str, ...] = ()
    timed_out: bool = False
    build_failed: bool = False
    # Commands are retained for local troubleshooting and provenance.
    docker_build_cmd: str = ""
    docker_run_cmd: str = ""

    def to_dict(self) -> dict[str, object]:
        """Convert the result into a plain dictionary.

        ``output_files`` is serialised as a list for JSON compatibility.
        ``output_file_count`` is added as a convenience field.
        """
        d = asdict(self)
        d["output_files"] = list(d["output_files"])
        d["output_file_count"] = len(d["output_files"])
        return d
