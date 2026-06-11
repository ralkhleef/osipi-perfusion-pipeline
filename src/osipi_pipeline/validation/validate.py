"""Basic validation checks for ingested submissions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from osipi_pipeline.validation.models import ValidationIssue, ValidationResult
from osipi_pipeline.validation.nifti_validator import validate_nifti_files

DEFAULT_VALIDATION_DIR = Path("data/outputs/validation")
KNOWN_CHALLENGE_TYPES = {"asl", "dce", "dsc"}
NIFTI_SUFFIXES = (".nii", ".nii.gz")
METADATA_SUFFIXES = {".json", ".yaml", ".yml", ".csv", ".tsv"}
CODE_SUFFIXES = {".py", ".m", ".r", ".sh", ".ipynb"}
EXPECTED_PARAMETER_MAPS = {
    "dce": ("ktrans", "kep", "vp"),
    "asl": ("cbf", "att"),
    "dsc": ("cbv", "cbf", "mtt"),
}

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
    nifti_summary: list[dict[str, Any]] = []

    if normalized_challenge not in KNOWN_CHALLENGE_TYPES:
        errors.append(
            ValidationIssue(
                severity="error",
                code="UNKNOWN_CHALLENGE_TYPE",
                message="Challenge type must be 'asl', 'dce', or 'dsc'.",
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
        return _finish_validation(path, normalized_challenge, errors, warnings, nifti_summary, output_dir)

    if not path.is_dir():
        errors.append(
            ValidationIssue(
                severity="error",
                code="SUBMISSION_PATH_NOT_FOLDER",
                message="Submission path must be a folder.",
                path=str(path),
            )
        )
        return _finish_validation(path, normalized_challenge, errors, warnings, nifti_summary, output_dir)

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
        return _finish_validation(path, normalized_challenge, errors, warnings, nifti_summary, output_dir)

    # Find NIfTI files and run checks on them.
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
    else:
        warnings.extend(_empty_nifti_warnings(nifti_files))
        warnings.extend(_missing_expected_map_warnings(nifti_files, normalized_challenge, path))

        # Run nibabel readability check on non-empty files.
        # 0-byte files are already reported above, so we skip them here.
        non_empty_niftis = [f for f in nifti_files if f.stat().st_size > 0]
        if non_empty_niftis:
            nifti_summary = validate_nifti_files(non_empty_niftis)
            errors, warnings = _apply_nifti_results(nifti_summary, errors, warnings)

    warnings.extend(_duplicate_filename_warnings(files))

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

    if not any(file_path.suffix.lower() in CODE_SUFFIXES for file_path in files):
        warnings.append(
            ValidationIssue(
                severity="warning",
                code="NO_CODE_FILES",
                message="No code files were found.",
                path=str(path),
            )
        )

    return _finish_validation(path, normalized_challenge, errors, warnings, nifti_summary, output_dir)

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
    nifti_summary: list[dict[str, Any]],
    output_dir: str | Path,
) -> ValidationResult:
    result = ValidationResult(
        submission_path=str(path.resolve()),
        challenge_type=challenge_type,
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        checked_at=datetime.now(timezone.utc).isoformat(),
        nifti_summary=nifti_summary,
    )
    save_validation_result(result, output_dir)
    return result

def _apply_nifti_results(
    nifti_results: list[dict[str, Any]],
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
) -> tuple[list[ValidationIssue], list[ValidationIssue]]:
    """Turn nibabel results into ValidationIssues and add them to the lists."""

    for nifti_result in nifti_results:
        file_path = nifti_result["file_path"]

        if not nifti_result["valid"]:
            for err_msg in nifti_result["errors"]:
                errors.append(
                    ValidationIssue(
                        severity="error",
                        code="NIFTI_UNREADABLE",
                        message=err_msg,
                        path=file_path,
                    )
                )

        for warn_msg in nifti_result["warnings"]:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="NIFTI_WARNING",
                    message=warn_msg,
                    path=file_path,
                )
            )

    return errors, warnings

def _is_nifti(path: Path) -> bool:
    return path.name.lower().endswith(NIFTI_SUFFIXES)

def _is_readme(path: Path) -> bool:
    return path.name.lower().startswith("readme")

def _is_docker_file(path: Path) -> bool:
    name = path.name.lower()
    return name == "dockerfile" or name == ".dockerignore" or name.startswith("docker-compose")

def _empty_nifti_warnings(nifti_files: list[Path]) -> list[ValidationIssue]:
    warnings: list[ValidationIssue] = []
    for file_path in nifti_files:
        if file_path.stat().st_size == 0:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="EMPTY_NIFTI_FILE",
                    message=".nii or .nii.gz file is empty.",
                    path=str(file_path),
                )
            )
    return warnings

def _missing_expected_map_warnings(
    nifti_files: list[Path],
    challenge_type: str,
    submission_path: Path,
) -> list[ValidationIssue]:
    expected_maps = EXPECTED_PARAMETER_MAPS.get(challenge_type, ())
    file_names = " ".join(file_path.name.lower() for file_path in nifti_files)
    warnings: list[ValidationIssue] = []
    for expected_map in expected_maps:
        if expected_map not in file_names:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="EXPECTED_MAP_MISSING",
                    message=f"Expected {expected_map} parameter map was not found.",
                    path=str(submission_path),
                )
            )
    return warnings

def _duplicate_filename_warnings(files: list[Path]) -> list[ValidationIssue]:
    seen: dict[str, list[Path]] = {}
    for file_path in files:
        seen.setdefault(file_path.name.lower(), []).append(file_path)

    warnings: list[ValidationIssue] = []
    for filename, matches in seen.items():
        if len(matches) > 1:
            paths = ", ".join(str(path) for path in matches)
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="DUPLICATE_FILENAME",
                    message=f"Filename appears more than once: {filename}",
                    path=paths,
                )
            )
    return warnings

def _safe_submission_id(raw_id: str) -> str:
    safe_id = "".join(character if character.isalnum() or character in "._-" else "_" for character in raw_id)
    return safe_id.strip("._-") or "submission"

def _print_summary(result: ValidationResult) -> None:
    status = "PASSED" if result.passed else "FAILED"
    print(f"Validation: {status}")
    print(f"Checked path: {result.submission_path}")
    print(f"Challenge type: {result.challenge_type}")
    print(f"Errors: {len(result.errors)}")
    print(f"Warnings: {len(result.warnings)}")

    issues = result.errors + result.warnings
    if issues:
        print("Issues:")
        for issue in issues:
            path_text = f" [{issue.path}]" if issue.path else ""
            print(f"- {issue.severity.upper()} {issue.code}: {issue.message}{path_text}")

    if result.nifti_summary:
        print(f"NIfTI files inspected: {len(result.nifti_summary)}")
        for entry in result.nifti_summary:
            valid_label = "OK" if entry["valid"] else "INVALID"
            shape_str = str(entry["shape"]) if entry["shape"] else "unknown"
            dtype_str = entry["dtype"] or "unknown"
            print(f"  [{valid_label}] {entry['file_path']}  shape={shape_str}  dtype={dtype_str}")

if __name__ == "__main__":
    raise SystemExit(main())
