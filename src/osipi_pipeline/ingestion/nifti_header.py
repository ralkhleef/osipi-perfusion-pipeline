"""Read NIfTI dimensionality from a NIfTI-1 or NIfTI-2 header alone.

A DCE-2026 synthetic submission may hold thousands of volumes, so the
manifest must not load voxel data to learn that a file is 3-D. This reads
the 348-byte header and stops.

For ``.nii.gz`` it streams through :mod:`gzip` and reads only the first 348
bytes, rather than decompressing the whole file into memory as the scoring
fallback does, the difference is a few hundred bytes against hundreds of
megabytes per volume.

Returns ``None`` on any problem. A malformed header is a validation concern
and is already reported by the validation layer; it must not break manifest
building.
"""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

_HEADER_BYTES = 348
_NIFTI1_SIZEOF_HDR = 348
_NIFTI2_SIZEOF_HDR = 540


def read_ndim(path: Path) -> int | None:
    """Number of dimensions declared by a NIfTI header, or ``None``.

    ``3`` for a parameter map, ``4`` for a modelled signal-time series.
    """
    try:
        if str(path).lower().endswith(".gz"):
            with gzip.open(path, "rb") as handle:
                raw = handle.read(_HEADER_BYTES)
        else:
            with open(path, "rb") as handle:
                raw = handle.read(_HEADER_BYTES)
    except (OSError, EOFError, gzip.BadGzipFile, Exception):
        return None

    if raw is None or len(raw) < _HEADER_BYTES:
        return None

    # sizeof_hdr identifies both the format and byte order. NIfTI-1 stores
    # dim[0] as int16 at byte 40; NIfTI-2 stores it as int64 at byte 16.
    try:
        little_size = struct.unpack("<i", raw[0:4])[0]
        big_size = struct.unpack(">i", raw[0:4])[0]
        if little_size in {_NIFTI1_SIZEOF_HDR, _NIFTI2_SIZEOF_HDR}:
            endian, header_size = "<", little_size
        elif big_size in {_NIFTI1_SIZEOF_HDR, _NIFTI2_SIZEOF_HDR}:
            endian, header_size = ">", big_size
        else:
            return None
        if header_size == _NIFTI1_SIZEOF_HDR:
            ndim = int(struct.unpack(endian + "h", raw[40:42])[0])
        else:
            ndim = int(struct.unpack(endian + "q", raw[16:24])[0])
    except struct.error:
        return None

    # dim[0] outside 1..7 means the header is not trustworthy.
    if ndim < 1 or ndim > 7:
        return None
    return ndim
