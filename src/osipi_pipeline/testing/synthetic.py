"""Build small, valid NIfTI volumes and DCE-2026 submission trees.

Volumes are written with a hand-packed NIfTI-1 header rather than through
nibabel, so fixtures can be produced with no optional dependency installed and
so the on-disk bytes are exactly what the test intends — a fixture builder that
shares a library with the code under test can hide a bug in both.

The DCE grid matches ``config/validation_rules.yaml``:

    Clinical    5 participants x 2 repeats x 1 site  = 10 scans
    Synthetic   1 participant  x 2 repeats x 3 sites =  6 scans
"""

from __future__ import annotations

import gzip
import struct
import zipfile
from pathlib import Path
from typing import Iterable, Sequence

#: (participant, site, repeat) triples per dataset.
CLINICAL_SCANS: tuple[tuple[int, int, int], ...] = tuple(
    (participant, 1, repeat) for participant in range(1, 6) for repeat in range(1, 3)
)
SYNTHETIC_SCANS: tuple[tuple[int, int, int], ...] = tuple(
    (1, site, repeat) for site in range(1, 4) for repeat in range(1, 3)
)

#: Parameter maps written into every scan directory.
DCE_MAP_NAMES: tuple[str, ...] = ("Ktrans", "vp", "ve")

VOLUME_SHAPE: tuple[int, ...] = (2, 2, 2)
VOLUME_VALUES: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)

_HEADER_SIZE = 352
_DATATYPE_FLOAT32 = 16
_BITPIX_FLOAT32 = 32


def nifti_bytes(values: Sequence[float], shape: Sequence[int]) -> bytes:
    """A valid little-endian NIfTI-1 float32 volume, header included."""
    header = bytearray(_HEADER_SIZE)
    struct.pack_into("<i", header, 0, 348)                      # sizeof_hdr
    struct.pack_into("<h", header, 40, len(shape))              # dim[0]
    for index, size in enumerate(shape):
        struct.pack_into("<h", header, 42 + index * 2, size)    # dim[1..]
    struct.pack_into("<h", header, 70, _DATATYPE_FLOAT32)
    struct.pack_into("<h", header, 72, _BITPIX_FLOAT32)
    struct.pack_into("<f", header, 108, float(_HEADER_SIZE))    # vox_offset
    for index in range(1, len(shape) + 1):
        struct.pack_into("<f", header, 76 + index * 4, 1.0)     # pixdim
    struct.pack_into("<f", header, 112, 1.0)                    # scl_slope
    header[344:348] = b"n+1\x00"
    body = b"".join(struct.pack("<f", float(value)) for value in values)
    return bytes(header) + body


def write_nifti(
    path: Path,
    values: Sequence[float] = VOLUME_VALUES,
    shape: Sequence[int] = VOLUME_SHAPE,
) -> Path:
    """Write a volume, gzipping when the filename asks for it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = nifti_bytes(values, shape)
    path.write_bytes(gzip.compress(payload) if path.name.endswith(".gz") else payload)
    return path


def build_dce_submission(
    root: Path,
    name: str = "DCE_Test_Clean",
    *,
    datasets: Iterable[str] = ("Clinical", "Synthetic"),
    methods: bool = True,
) -> Path:
    """Create the submission layout DCE-2026 describes, under ``root``.

    Returns the submission directory. Each scan gets the parameter maps plus a
    4-D modelled signal; the methods document sits at the submission root,
    where it belongs to the submission as a whole rather than to one dataset.
    """
    scans_by_dataset = {"Clinical": CLINICAL_SCANS, "Synthetic": SYNTHETIC_SCANS}
    submission = root / name
    for dataset in datasets:
        for participant, site, repeat in scans_by_dataset[dataset]:
            scan = (submission / dataset / f"Participant{participant}"
                    / f"Site{site}" / f"Repeat{repeat}")
            for map_name in DCE_MAP_NAMES:
                write_nifti(scan / f"{map_name}.nii.gz")
            write_nifti(scan / "modelled_st.nii.gz",
                        VOLUME_VALUES * 2, (2, 2, 2, 2))
    if methods:
        (submission / "methods.txt").write_text(
            "Extended Tofts model, population AIF.\n", encoding="utf-8")
    return submission


def zip_directory(directory: Path, archive: Path) -> Path:
    """Zip ``directory`` so the archive contains it as its top-level folder."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(directory.parent))
    return archive
