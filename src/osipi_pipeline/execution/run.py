"""Command line interface for Docker execution."""

from __future__ import annotations

import argparse
import sys

from osipi_pipeline.config.rules import challenge_types
from osipi_pipeline.execution.docker_runner import (
    DEFAULT_CPU_LIMIT,
    DEFAULT_MEMORY_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    DockerExecutionError,
    execute_submission,
)


def main(argv: list[str] | None = None) -> int:
    """Run a validated submission in Docker from the command line."""

    parser = argparse.ArgumentParser(description="Run an OSIPI submission in Docker")
    parser.add_argument(
        "--input", required=True,
        help="Path to an already-ingested submission folder",
    )
    parser.add_argument(
        "--challenge", required=True,
        help=f"Configured challenge type ({', '.join(challenge_types())})",
    )
    parser.add_argument(
        "--command", default=None,
        help=(
            "Shell command to run inside the container.  "
            "Defaults to run_config.json 'command' if present, "
            "otherwise 'python3 run.py'."
        ),
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Seconds before the container is killed (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--memory", default=DEFAULT_MEMORY_LIMIT,
        help=f"Docker --memory value (default: {DEFAULT_MEMORY_LIMIT})",
    )
    parser.add_argument(
        "--cpus", default=DEFAULT_CPU_LIMIT,
        help=f"Docker --cpus value (default: {DEFAULT_CPU_LIMIT})",
    )
    args = parser.parse_args(argv)

    try:
        result = execute_submission(
            args.input,
            challenge_type=args.challenge,
            command=args.command,
            timeout_seconds=args.timeout,
            memory_limit=args.memory,
            cpu_limit=args.cpus,
        )
    except DockerExecutionError as exc:
        print(f"Execution failed: {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0 if result.passed else 1


def _print_summary(result) -> None:
    status = "PASSED" if result.passed else ("TIMED OUT" if result.timed_out else "FAILED")
    print(f"Execution: {status}")
    print(f"Submission path: {result.submission_path}")
    print(f"Challenge type:  {result.challenge_type}")
    print(f"Image name:      {result.image_name}")
    print(f"Command:         {result.command}")
    print(f"Exit code:       {result.exit_code}")
    print(f"Stdout log:      {result.stdout_path}")
    print(f"Stderr log:      {result.stderr_path}")
    print(f"Output path:     {result.output_path}")
    if result.output_files:
        print(f"Output NIfTI files ({len(result.output_files)}):")
        for f in result.output_files:
            print(f"  {f}")
    else:
        print("Output NIfTI files: none")


if __name__ == "__main__":
    raise SystemExit(main())
