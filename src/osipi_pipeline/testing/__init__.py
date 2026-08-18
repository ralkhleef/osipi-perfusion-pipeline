"""Fixture builders shared by tests and demo scripts without importing pytest."""

from osipi_pipeline.testing.synthetic import (
    CLINICAL_SCANS,
    DCE_MAP_NAMES,
    SYNTHETIC_SCANS,
    VOLUME_SHAPE,
    VOLUME_VALUES,
    build_dce_submission,
    nifti_bytes,
    write_nifti,
    zip_directory,
)

__all__ = [
    "CLINICAL_SCANS",
    "DCE_MAP_NAMES",
    "SYNTHETIC_SCANS",
    "VOLUME_SHAPE",
    "VOLUME_VALUES",
    "build_dce_submission",
    "nifti_bytes",
    "write_nifti",
    "zip_directory",
]
