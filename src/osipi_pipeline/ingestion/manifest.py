"""
A manifest is a small inventory file. It tells us what source was ingested,
where the local working copy lives, and what kinds of files were found.
"""

# TODO: This file creates the manifest, which is the pipeline's record of an ingested submission.
# TODO: Later, add fields needed by validation, Docker execution, scoring, and reporting.
# TODO: Keep manifests small so they can be committed without storing large MRI datasets.

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from osipi_pipeline.ingestion.models import Manifest

NIFTI_SUFFIXES = (".nii", ".nii.gz")
METADATA_SUFFIXES = {".json", ".yaml", ".yml", ".csv", ".tsv"}
CODE_SUFFIXES = {".py", ".m", ".r", ".R", ".ipynb", ".sh", ".jl", ".c", ".cpp", ".h", ".hpp"}


def build_manifest(
    *,
    submission_id: str,
    challenge_type: str,
    original_path: str | Path,
    extracted_path: Path,
) -> Manifest:
    """Create a manifest for an extracted submission folder."""

    files = sorted(path for path in extracted_path.rglob("*") if path.is_file())
    # Store file names relative to the submission root so manifests are portable.
    relative_files = [(path, _as_relative_posix(path, extracted_path)) for path in files]

    return Manifest(
        submission_id=submission_id,
        challenge_type=challenge_type,
        original_path=_original_path_value(original_path),
        extracted_path=str(extracted_path.resolve()),
        file_count=len(files),
        nifti_files=[rel for path, rel in relative_files if _is_nifti(path)],
        metadata_files=[rel for path, rel in relative_files if path.suffix in METADATA_SUFFIXES],
        code_files=[rel for path, rel in relative_files if path.suffix in CODE_SUFFIXES],
        docker_files=[rel for path, rel in relative_files if _is_docker_file(path)],
        readme_files=[rel for path, rel in relative_files if path.name.lower().startswith("readme")],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


def save_manifest(manifest: Manifest, manifests_dir: Path) -> tuple[Path, Path]:
    """Save one manifest in both JSON and CSV formats."""

    manifests_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{manifest.challenge_type}_{manifest.submission_id}_manifest"
    json_path = manifests_dir / f"{base_name}.json"
    csv_path = manifests_dir / f"{base_name}.csv"

    manifest_data = manifest.to_dict()
    json_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(manifest_data.keys()))
        writer.writeheader()
        writer.writerow({key: _csv_value(value) for key, value in manifest_data.items()})

    return json_path, csv_path


def _is_nifti(path: Path) -> bool:
    """Return true for common NIfTI image filenames."""

    return path.name.lower().endswith(NIFTI_SUFFIXES)


def _is_docker_file(path: Path) -> bool:
    """Return true for Docker-related files."""

    name = path.name.lower()
    return name == "dockerfile" or name == ".dockerignore" or name.startswith("docker-compose")


def _as_relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _csv_value(value: object) -> object:
    """Flatten list values so one manifest fits in one CSV row."""

    if isinstance(value, list):
        return ";".join(value)
    return value


def _original_path_value(original_path: str | Path) -> str:
    """Keep URLs as URLs, but resolve local paths to absolute paths."""

    path_text = str(original_path)
    if "://" in path_text or path_text.startswith("git@"):
        return path_text
    return str(Path(original_path).resolve())
