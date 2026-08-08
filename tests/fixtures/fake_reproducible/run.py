"""Fake reproducible submission, writes synthetic perfusion maps to /output.

This script is used for integration testing of the reproducible execution
workflow.  It does NOT require real input data; it generates small dummy
NIfTI files (4×4×4 voxels) and writes them to /output so the pipeline can
validate post-execution output collection.

Expected outputs (DCE challenge):
  ktrans.nii.gz, kep.nii.gz, vp.nii.gz
"""

import gzip
import os
import struct

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")


def _nifti1_header(shape=(4, 4, 4)):
    """Return a minimal NIfTI-1 header (348 bytes) for float32 data."""
    hdr = bytearray(348)
    struct.pack_into("<i",  hdr,   0, 348)       # sizeof_hdr
    struct.pack_into("<H",  hdr,  40, 3)          # dim[0] = 3 dims
    struct.pack_into("<H",  hdr,  42, shape[0])   # dim[1]
    struct.pack_into("<H",  hdr,  44, shape[1])   # dim[2]
    struct.pack_into("<H",  hdr,  46, shape[2])   # dim[3]
    struct.pack_into("<H",  hdr,  70, 16)         # datatype = float32
    struct.pack_into("<H",  hdr,  72, 32)         # bitpix = 32
    struct.pack_into("<f",  hdr,  76, 1.0)        # pixdim[0]
    struct.pack_into("<f",  hdr,  80, 2.0)        # pixdim[1]
    struct.pack_into("<f",  hdr,  84, 2.0)        # pixdim[2]
    struct.pack_into("<f",  hdr,  88, 2.0)        # pixdim[3]
    struct.pack_into("<f",  hdr, 108, 352.0)      # vox_offset
    struct.pack_into("<f",  hdr, 112, 1.0)        # scl_slope
    struct.pack_into("<f",  hdr, 116, 0.0)        # scl_inter
    hdr[344:347] = b"ni1"                         # magic
    return bytes(hdr)


def write_nifti(path, value, shape=(4, 4, 4)):
    """Write a constant-valued float32 NIfTI-1 file (gzip-compressed)."""
    n_vox = shape[0] * shape[1] * shape[2]
    header = _nifti1_header(shape)
    # 4-byte extension code (required by NIfTI-1 spec)
    extension = struct.pack("<i", 0)
    data = struct.pack(f"<{n_vox}f", *([value] * n_vox))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wb") as fh:
        fh.write(header)
        fh.write(extension)
        fh.write(data)
    print(f"  Written: {path} ({os.path.getsize(path)} bytes)")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"Writing synthetic DCE perfusion maps to {OUTPUT_DIR}")
    write_nifti(os.path.join(OUTPUT_DIR, "ktrans.nii.gz"), value=0.15)
    write_nifti(os.path.join(OUTPUT_DIR, "kep.nii.gz"),    value=0.42)
    write_nifti(os.path.join(OUTPUT_DIR, "vp.nii.gz"),     value=0.05)
    print("Done.")


if __name__ == "__main__":
    main()
