"""Small data objects used by submission ingestion."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

# Artifact roles. `parameter_map` and the configured artifact roles
# (`fitted_signal`, `methods`) are the DCE-2026 additions; the rest mirror
# the legacy manifest categories so every file can be represented.
ROLE_PARAMETER_MAP = "parameter_map"
ROLE_METADATA = "metadata"
ROLE_CODE = "code"
ROLE_README = "readme"
ROLE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class SubmissionArtifact:
    """One submitted file, with whatever identity could be resolved.

    Identity fields are ``None`` when they could not be determined — a flat
    legacy submission has no participant or site, and inventing one would be
    worse than admitting the gap. Nothing in Phase 2 enforces completeness;
    this record only states what was found.

    ``map_type`` is set only for ``role == "parameter_map"``. A 4-D fitted
    signal is deliberately not a parameter map, so it carries ``None``.
    """

    path: str
    role: str
    challenge: str | None = None
    dataset: str | None = None
    participant: str | None = None
    repeat: str | None = None
    site: str | None = None
    map_type: str | None = None
    artifact_type: str | None = None
    dimensions: int | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-safe mapping with stable key order."""
        return asdict(self)


@dataclass(frozen=True)
class IdentityConflict:
    """Recorded when directory and filename identity disagree.

    Directory wins (see ``identity_parser``); this exists so the disagreement
    is visible rather than silently resolved. Phase 2 does not fail an upload
    on a conflict.
    """

    path: str
    field: str
    directory_value: str
    filename_value: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
    files: list[dict[str, object]] = field(default_factory=list)
    directories: list[dict[str, object]] = field(default_factory=list)
    config_fingerprint: str = ""
    # Additive. The legacy list fields above are unchanged and remain the
    # source for existing callers; `artifacts` is the normalized view that
    # DCE processing will build on.
    artifacts: tuple[SubmissionArtifact, ...] = ()
    identity_conflicts: tuple[IdentityConflict, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Convert the manifest into a plain dictionary for JSON and CSV."""

        return asdict(self)
