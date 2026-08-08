"""Basic validation checks for ingested submissions."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from osipi_pipeline.config.rules import (
    challenge_types,
    default_challenge_type,
    expected_maps_by_challenge,
    map_type_specs,
    tuple_setting,
)
from osipi_pipeline.ingestion.identity_parser import resolve_identity
from osipi_pipeline.validation.models import ValidationIssue, ValidationResult
from osipi_pipeline.validation.nifti_validator import validate_nifti_files

DEFAULT_VALIDATION_DIR = Path("data/outputs/validation")
NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")
METADATA_SUFFIXES = set(tuple_setting("metadata_suffixes"))
CODE_SUFFIXES = set(tuple_setting("code_extensions"))


def _known_challenge_types() -> tuple[str, ...]:
    return tuple(challenge_types())


def _default_challenge_type() -> str:
    return default_challenge_type()


def _expected_parameter_maps() -> dict[str, tuple[str, ...]]:
    return expected_maps_by_challenge()


def _map_display_name(map_id: str) -> str:
    spec = map_type_specs().get(str(map_id).lower(), {})
    return str(spec.get("display") or map_id)

def validate_submission(
    submission_path: str | Path,
    *,
    challenge_type: str,
    output_dir: str | Path = DEFAULT_VALIDATION_DIR,
) -> ValidationResult:
    """Validate one already-ingested submission folder and save JSON output."""

    path = Path(submission_path).expanduser()
    normalized_challenge = (challenge_type or _default_challenge_type()).lower()
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    nifti_summary: list[dict[str, Any]] = []

    if normalized_challenge not in _known_challenge_types():
        errors.append(
            ValidationIssue(
                severity="error",
                code="UNKNOWN_CHALLENGE_TYPE",
                message=f"Challenge type must be one of: {', '.join(_known_challenge_types())}.",
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
                message=f"At least one parameter map file is required ({_suffix_help(NIFTI_SUFFIXES)}).",
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

    warnings.extend(_duplicate_filename_warnings(files, path, normalized_challenge))

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
    parser.add_argument("--challenge", required=True, help=f"Configured challenge type ({', '.join(_known_challenge_types())})")
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
    name = path.name.lower()
    for configured in tuple_setting("readme_names"):
        if name == configured or (("." not in configured) and name.startswith(configured)):
            return True
    return False

def _is_docker_file(path: Path) -> bool:
    name = path.name.lower()
    return name == "dockerfile" or name.startswith("docker-compose")

def _empty_nifti_warnings(nifti_files: list[Path]) -> list[ValidationIssue]:
    warnings: list[ValidationIssue] = []
    for file_path in nifti_files:
        if file_path.stat().st_size == 0:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="EMPTY_NIFTI_FILE",
                    message="Parameter map file is empty.",
                    path=str(file_path),
                )
            )
    return warnings

def _suffix_help(suffixes: tuple[str, ...]) -> str:
    return ", ".join(suffixes) if suffixes else "configured NIfTI suffixes"

def _missing_expected_map_warnings(
    nifti_files: list[Path],
    challenge_type: str,
    submission_path: Path,
) -> list[ValidationIssue]:
    expected_maps = _expected_parameter_maps().get(challenge_type, ())
    file_names = " ".join(file_path.name.lower() for file_path in nifti_files)
    warnings: list[ValidationIssue] = []
    for expected_map in expected_maps:
        if expected_map not in file_names:
            warnings.append(
                ValidationIssue(
                    severity="warning",
                    code="EXPECTED_MAP_MISSING",
                    message=f"Expected {_map_display_name(expected_map)} parameter map was not found.",
                    path=str(submission_path),
                )
            )
    return warnings

def duplicate_filename_groups(
    files: list[Path],
    root: Path | None = None,
    challenge_type: str | None = None,
) -> list[tuple[str, list[Path]]]:
    """Group files that genuinely repeat a filename *within one scan*.

    A basename alone is not evidence of duplication. The DCE-2026 layout
    requires the same standard names in every scan directory,
    ``Synthetic/Participant1/Site1/Repeat1/Ktrans.nii.gz`` and
    ``…/Repeat2/Ktrans.nii.gz`` are two different scans, not a mistake, so
    keying on the basename alone warns about every correct submission. See
    CODE_WALKTHROUGH.md §B6.

    Files are therefore keyed on resolved scan identity plus filename. Where
    no identity can be resolved (a flat legacy submission) every file falls
    into one bucket, which reproduces the original behaviour exactly.
    """
    challenge = (challenge_type or "").strip().lower() or None
    seen: dict[tuple, list[Path]] = {}
    for file_path in files:
        name = file_path.name.lower()
        identity: dict[str, str | None] = {}
        if root is not None:
            try:
                relative = file_path.resolve().relative_to(Path(root).resolve())
                identity, _ = resolve_identity(relative.as_posix(), challenge=challenge)
            except (ValueError, OSError):
                identity = {}
        key = (
            identity.get("dataset"),
            identity.get("participant"),
            identity.get("site"),
            identity.get("repeat"),
            name,
        )
        seen.setdefault(key, []).append(file_path)

    return [
        (key[-1], matches) for key, matches in seen.items() if len(matches) > 1
    ]


def _duplicate_filename_warnings(
    files: list[Path],
    root: Path | None = None,
    challenge_type: str | None = None,
) -> list[ValidationIssue]:
    return [
        ValidationIssue(
            severity="warning",
            code="DUPLICATE_FILENAME",
            message=f"Filename appears more than once within one scan: {filename}",
            path=", ".join(str(path) for path in matches),
        )
        for filename, matches in duplicate_filename_groups(files, root, challenge_type)
    ]

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
