"""Data models for validation results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

@dataclass(frozen=True)
class ValidationIssue:
    """One validation problem or warning found in a submission."""

    severity: str
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert the issue into a plain dictionary for JSON output."""

        return asdict(self)

@dataclass(frozen=True)
class ValidationResult:
    """The full validation summary for one ingested submission."""

    submission_path: str
    challenge_type: str
    passed: bool
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]
    checked_at: str
    # Each entry is one NiftiFileResult dict produced by nifti_validator.
    # Empty list means no NIfTI files were inspected (e.g. early-exit paths).
    nifti_summary: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Convert the result into a plain dictionary for JSON output."""

        data = asdict(self)
        data["errors"] = [issue.to_dict() for issue in self.errors]
        data["warnings"] = [issue.to_dict() for issue in self.warnings]
        # nifti_summary is already list[dict]; asdict deep-copies it in place.
        return data
