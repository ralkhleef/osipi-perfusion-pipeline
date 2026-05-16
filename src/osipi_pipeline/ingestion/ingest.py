"""
This module accepts one input source, creates a normalized local working copy, 
builds a manifest, and prints a summary.
"""

# TODO: This file handles the first pipeline step: bringing a submission into the workspace.
# TODO: Later, connect this ingestion command to validation, scoring, and reporting.
# TODO: Keep this file simple so folders, zip files, GitHub, and future sources all work the same way.

from __future__ import annotations

import argparse
import shutil
import zipfile
from pathlib import Path

from osipi_pipeline.config.challenge_types import CHALLENGE_TYPES
from osipi_pipeline.ingestion.detector import detect_challenge_type
from osipi_pipeline.ingestion.manifest import build_manifest, save_manifest
from osipi_pipeline.ingestion.models import Manifest
from osipi_pipeline.ingestion.sources import materialize_source, resolve_source

DEFAULT_EXTRACTED_ROOT = Path("submissions/extracted")
DEFAULT_MANIFESTS_DIR = Path("outputs/manifests")


def ingest_submission(
    input_path: str | Path,
    *,
    challenge: str | None = None,
    extracted_root: str | Path = DEFAULT_EXTRACTED_ROOT,
    manifests_dir: str | Path = DEFAULT_MANIFESTS_DIR,
) -> Manifest:
    """Ingest a folder, zip, or GitHub repo and write JSON/CSV manifests."""

    source = resolve_source(input_path)
    submission_id = source.submission_id
    normalized_challenge = _normalize_challenge_override(challenge)

    staging_root = Path(extracted_root)
    # Staging is a temporary holding area before we know the challenge type.
    initial_target = staging_root / "_staging" / submission_id
    materialize_source(source, initial_target)

    challenge_type = normalized_challenge or detect_challenge_type(initial_target)
    # The extracted path is the final normalized workspace location.
    final_target = staging_root / challenge_type / submission_id
    if final_target != initial_target:
        _prepare_destination(final_target)
        final_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(initial_target), str(final_target))
        _cleanup_empty_staging(staging_root / "_staging")

    manifest = build_manifest(
        submission_id=submission_id,
        challenge_type=challenge_type,
        original_path=source.original,
        extracted_path=final_target,
    )
    json_path, csv_path = save_manifest(manifest, Path(manifests_dir))
    _print_summary(manifest, json_path, csv_path)
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Parse command line arguments and run ingestion."""

    parser = argparse.ArgumentParser(description="Ingest an OSIPI challenge submission")
    parser.add_argument("--input", required=True, help="Submission folder, .zip file, or GitHub repository URL")
    parser.add_argument(
        "--challenge",
        choices=sorted(CHALLENGE_TYPES.keys()) + ["unknown"],
        help="Override automatic challenge detection",
    )
    args = parser.parse_args(argv)

    try:
        ingest_submission(args.input, challenge=args.challenge)
    except (FileNotFoundError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    return 0


def _normalize_challenge_override(challenge: str | None) -> str | None:
    """Validate a manually provided challenge type, if the user gives one."""

    if challenge is None:
        return None
    normalized = challenge.lower()
    allowed = set(CHALLENGE_TYPES) | {"unknown"}
    if normalized not in allowed:
        raise ValueError(f"Unsupported challenge type '{challenge}'. Expected one of: {', '.join(sorted(allowed))}")
    return normalized


def _prepare_destination(destination: Path) -> None:
    """Create a clean destination folder."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)


def _cleanup_empty_staging(staging_root: Path) -> None:
    """Remove the staging folder if it is empty."""

    try:
        staging_root.rmdir()
    except OSError:
        pass


def _print_summary(manifest: Manifest, json_path: Path, csv_path: Path) -> None:
    """Print a short ingestion summary for the user."""

    print(f"Ingested submission: {manifest.submission_id}")
    print(f"Challenge type: {manifest.challenge_type}")
    print(f"Extracted path: {manifest.extracted_path}")
    print(f"Files found: {manifest.file_count}")
    print(f"NIfTI files: {len(manifest.nifti_files)}")
    print(f"Metadata files: {len(manifest.metadata_files)}")
    print(f"Code files: {len(manifest.code_files)}")
    print(f"Docker files: {len(manifest.docker_files)}")
    print(f"README files: {len(manifest.readme_files)}")
    print(f"Manifest JSON: {json_path}")
    print(f"Manifest CSV: {csv_path}")


if __name__ == "__main__":
    raise SystemExit(main())
