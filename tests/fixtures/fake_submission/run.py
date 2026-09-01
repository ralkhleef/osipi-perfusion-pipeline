"""Fake submission run.py for integration testing.

Writes two fake output files to /output:
  - fake_output.nii    , fake NIfTI (minimal header bytes)
  - metadata.json      , execution metadata

Usage:
    python3 run.py --output /output
"""

import argparse
import json
import struct
import sys
from pathlib import Path


def write_fake_nifti(path: Path) -> None:
    """Write a minimal valid NIfTI-1 file (348-byte header + 1 voxel)."""
    # NIfTI-1 header: 348 bytes
    # Key fields: sizeof_hdr=348, dim[0]=3, pixdim, magic="n+1\0"
    header = bytearray(348)
    struct.pack_into("<i", header, 0, 348)          # sizeof_hdr
    struct.pack_into("<h", header, 40, 3)           # dim[0] = 3 (3-D)
    struct.pack_into("<h", header, 42, 1)           # dim[1]
    struct.pack_into("<h", header, 44, 1)           # dim[2]
    struct.pack_into("<h", header, 46, 1)           # dim[3]
    struct.pack_into("<h", header, 70, 16)          # datatype = float32
    struct.pack_into("<h", header, 72, 32)          # bitpix
    struct.pack_into("<f", header, 108, 1.0)        # pixdim[1]
    struct.pack_into("<f", header, 112, 1.0)        # pixdim[2]
    struct.pack_into("<f", header, 116, 1.0)        # pixdim[3]
    struct.pack_into("<f", header, 108, 352.0)      # vox_offset (after header)
    header[344:348] = b"n+1\x00"                    # magic
    # 4-byte extension block (all zeros = no extension)
    extension = b"\x00\x00\x00\x00"
    # 1 float32 voxel
    voxel = struct.pack("<f", 42.0)
    path.write_bytes(bytes(header) + extension + voxel)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/output", help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Write fake NIfTI file
    nifti_path = out_dir / "fake_output.nii"
    write_fake_nifti(nifti_path)
    print(f"Wrote NIfTI: {nifti_path}")

    # Write metadata JSON
    meta_path = out_dir / "metadata.json"
    meta_path.write_text(
        json.dumps({"status": "ok", "voxel_count": 1}, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote metadata: {meta_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
