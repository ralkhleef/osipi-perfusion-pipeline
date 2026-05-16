"""Basic validation checks for ingested submissions."""

# TODO: This file checks whether an ingested submission looks usable.
# TODO: Later, add real NIfTI reading, BIDS checks, and challenge-specific validation rules.
# TODO: Validation stays separate from ingestion so each pipeline step has one job.

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from osipi_pipeline.validation.models import ValidationIssue, ValidationResult

DEFAULT_VALIDATION_DIR = Path("outputs/validation")
KNOWN_CHALLENGE_TYPES = {"asl", "dce"}
NIFTI_SUFFIXES = (".nii", ".nii.gz")
METADATA_SUFFIXES = {".json", ".yaml", ".yml", ".csv", ".tsv"}
CODE_SUFFIXES = {".py", ".m", ".r", ".R", ".ipynb", ".sh", ".jl", ".c", ".cpp", ".h", ".hpp"}


def validate_submission(
    submission_path: str | Path,
    *,
    challenge_type: str,
    output_dir: str | Path = DEFAULT_VALIDATION_DIR,
) -> ValidationResult:
    """Validate one already-ingested submission folder and save JSON output."""

    path = Path(submission_path).expanduser()
    normalized_challenge = challenge_type.lower()
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    if normalized_challenge not in KNOWN_CHALLENGE_TYPES:
        errors.append(
            ValidationIssue(
                severity="error",
                code="UNKNOWN_CHALLENGE_TYPE",
                message="Challenge type must be 'asl' or 'dce'.",
            )
        )

    if not path.exists():
        errors.append(
            ValidationIssue(
                severity="error",
                code="SUBMISSION_FOLDER_MISSING",
                message="Submission folder does not exist.",
                path=str(path),
            )
        )
        return _finish_validation(path, normalized_challenge, errors, warnings, output_dir)

    if not path.is_dir():
        errors.append(
            ValidationIssue(
                severity="error",
                code="SUBMISSION_PATH_NOT_FOLDER",
                message="Submission path must be a folder.",
                path=str(path),
            )
        )
        return _finish_validation(path, normalized_challenge, errors, warnings, output_dir)

    files = sorted(file_path for file_path in path.rglob("*") if file_path.is_file())
    if not files:
        errors.append(
            ValidationIssue(
                severity="error",
                code="SUBMISSION_FOLDER_EMPTY",
                message="Submission folder is empty.",
                path=str(path),
            )
        )
        return _finish_validation(path, normalized_challenge, errors, warnings, output_dir)

    nifti_files = [file_path for file_path in files if _is_nifti(file_path)]
    if not nifti_files:
        errors.append(
            ValidationIssue(
                severity="error",
                code="NO_NIFTI_FILES",
                message="At least one .nii or .nii.gz file is required.",
                path=str(path),
            )
        )

    if not any(_is_readme(file_path) or file_path.suffix.lower() in METADATA_SUFFIXES for file_path in files):
        errors.append(
            ValidationIssue(
                severity="error",
                code="NO_README_OR_METADATA",
                message="A README or metadata file is required.",
                path=str(path),
            )
        )

    if not any(_is_docker_file(file_path) for file_path in files):
        warnings.append(
            ValidationIssue(
                severity="warning",
                code="DOCKERFILE_MISSING",
                message="Dockerfile is missing.",
                path=str(path),
            )
        )

    if not any(file_path.suffix in CODE_SUFFIXES for file_path in files):
        warnings.append(
            ValidationIssue(
                severity="warning",
                code="NO_CODE_FILES",
                message="No code files were found.",
                path=str(path),
            )
        )

    return _finish_validation(path, normalized_challenge, errors, warnings, output_dir)


def save_validation_result(result: ValidationResult, output_dir: str | Path) -> Path:
    """Save validation results as JSON."""

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    submission_id = _safe_submission_id(Path(result.submission_path).name)
    file_path = output_path / f"{result.challenge_type}_{submission_id}_validation.json"
    file_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    return file_path


def main(argv: list[str] | None = None) -> int:
    """Run validation from the command line."""

    parser = argparse.ArgumentParser(description="Validate an ingested OSIPI submission")
    parser.add_argument("--input", required=True, help="Path to an already-ingested submission folder")
    parser.add_argument("--challenge", required=True, help="Challenge type, such as asl or dce")
    args = parser.parse_args(argv)

    result = validate_submission(args.input, challenge_type=args.challenge)
    _print_summary(result)
    return 0 if result.passed else 1


def _finish_validation(
    path: Path,
    challenge_type: str,
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
    output_dir: str | Path,
) -> ValidationResult:
    result = ValidationResult(
        submission_path=str(path.resolve()),
        challenge_type=challenge_type,
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
    save_validation_result(result, output_dir)
    return result


def _is_nifti(path: Path) -> bool:
    return path.name.lower().endswith(NIFTI_SUFFIXES)


def _is_readme(path: Path) -> bool:
    return path.name.lower().startswith("readme")


def _is_docker_file(path: Path) -> bool:
    name = path.name.lower()
    return name == "dockerfile" or name == ".dockerignore" or name.startswith("docker-compose")


def _safe_submission_id(raw_id: str) -> str:
    safe_id = "".join(character if character.isalnum() or character in "._-" else "_" for character in raw_id)
    return safe_id.strip("._-") or "submission"


def _print_summary(result: ValidationResult) -> None:
    status = "PASSED" if result.passed else "FAILED"
    print(f"Validation: {status}")
    print(f"Errors: {len(result.errors)}")
    print(f"Warnings: {len(result.warnings)}")

    issues = result.errors + result.warnings
    if issues:
        print("Issues:")
        for issue in issues:
            path_text = f" [{issue.path}]" if issue.path else ""
            print(f"- {issue.severity.upper()} {issue.code}: {issue.message}{path_text}")


if __name__ == "__main__":
    raise SystemExit(main())

