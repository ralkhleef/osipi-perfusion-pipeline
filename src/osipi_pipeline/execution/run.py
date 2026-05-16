"""Command line interface for Docker execution."""

from __future__ import annotations

import argparse
import sys

from osipi_pipeline.execution.docker_runner import DockerExecutionError, execute_submission


def main(argv: list[str] | None = None) -> int:
    """Run a validated submission in Docker from the command line."""

    parser = argparse.ArgumentParser(description="Run an OSIPI submission in Docker")
    parser.add_argument("--input", required=True, help="Path to an already-ingested submission folder")
    parser.add_argument("--challenge", required=True, help="Challenge type, such as asl or dce")
    args = parser.parse_args(argv)

    try:
        result = execute_submission(args.input, challenge_type=args.challenge)
    except DockerExecutionError as exc:
        print(f"Execution failed: {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0 if result.passed else 1


def _print_summary(result) -> None:
    status = "PASSED" if result.passed else "FAILED"
    print(f"Execution: {status}")
    print(f"Submission path: {result.submission_path}")
    print(f"Challenge type: {result.challenge_type}")
    print(f"Image name: {result.image_name}")
    print(f"Command: {result.command}")
    print(f"Exit code: {result.exit_code}")
    print(f"Stdout log: {result.stdout_path}")
    print(f"Stderr log: {result.stderr_path}")


if __name__ == "__main__":
    raise SystemExit(main())

