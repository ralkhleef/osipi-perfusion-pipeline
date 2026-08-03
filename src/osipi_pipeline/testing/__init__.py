"""Fixture builders shared by the test suite and the demo scripts.

Deliberately part of the library rather than of ``tests/``: a demo or
evidence script must not import from the test suite, because that drags in
``pytest`` and makes a production-side tool fail on a machine that has only
the runtime dependencies installed.

Nothing in this package imports ``pytest``.
"""

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
