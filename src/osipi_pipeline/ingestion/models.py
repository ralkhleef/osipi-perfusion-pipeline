"""Small data objects used by submission ingestion."""

# TODO: This file defines simple data containers shared by ingestion code.
# TODO: Later, add models for validation results, scoring results, and reports.
# TODO: Keep these models easy to serialize so pipeline stages can pass data cleanly.

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Manifest:
    """A manifest is the inventory we write after ingesting a submission.

    It records where the submission came from, where it was copied or extracted,
    and which useful files were found.
    """

    submission_id: str
    challenge_type: str
    original_path: str
    extracted_path: str
    file_count: int
    nifti_files: list[str]
    metadata_files: list[str]
    code_files: list[str]
    docker_files: list[str]
    readme_files: list[str]
    timestamp: str

    def to_dict(self) -> dict[str, object]:
        """Convert the manifest into a plain dictionary for JSON and CSV."""

        return asdict(self)
