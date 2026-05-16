"""Data models for validation results."""

# TODO: This file stores validation result shapes shared by the validation CLI and tests.
# TODO: Later, add more detailed issue fields if deeper NIfTI or BIDS checks need them.
# TODO: These models help pass validation results to future scoring and reporting stages.

from __future__ import annotations

from dataclasses import asdict, dataclass


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

    def to_dict(self) -> dict[str, object]:
        """Convert the result into a plain dictionary for JSON output."""

        data = asdict(self)
        data["errors"] = [issue.to_dict() for issue in self.errors]
        data["warnings"] = [issue.to_dict() for issue in self.warnings]
        return data

