"""Read NIfTI dimensionality from the header alone.

A DCE-2026 synthetic submission may hold thousands of volumes, so the
manifest must not load voxel data to learn that a file is 3-D. This reads
the 348-byte header and stops.

For ``.nii.gz`` it streams through :mod:`gzip` and reads only the first 348
bytes, rather than decompressing the whole file into memory as the scoring
fallback does — the difference is a few hundred bytes against hundreds of
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
_SIZEOF_HDR = 348


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

    # sizeof_hdr identifies byte order; anything else is not NIfTI-1.
    try:
        if struct.unpack("<i", raw[0:4])[0] == _SIZEOF_HDR:
            endian = "<"
        elif struct.unpack(">i", raw[0:4])[0] == _SIZEOF_HDR:
            endian = ">"
        else:
            return None
        ndim = int(struct.unpack(endian + "h", raw[40:42])[0])
    except struct.error:
        return None

    # dim[0] outside 1..7 means the header is not trustworthy.
    if ndim < 1 or ndim > 7:
        return None
    return ndim
