"""
Create minimal but fully valid test submissions for the OSIPI pipeline.

Generates two submissions:
  data/extracted/test_asl_submission/   — ASL challenge (cbf.nii.gz, att.nii.gz)
  data/extracted/test_dce_submission/   — DCE challenge (ktrans.nii.gz, kep.nii.gz, vp.nii.gz)

Each NIfTI file is a real, nibabel-readable 3-D volume (10×10×10 float32).
Run from the project root:
  python3 create_test_submission.py
or with the venv:
  .venv/bin/python3 create_test_submission.py
"""

import gzip
import json
import struct
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Minimal NIfTI-1 writer (no nibabel required)
# ---------------------------------------------------------------------------

def _nifti1_header(nx: int, ny: int, nz: int) -> bytes:
    """Return a 352-byte NIfTI-1 header + 4-byte extension block."""
    # NIfTI-1 header is exactly 348 bytes (see nifti1.h)
    # We pack field by field matching the C struct layout.

    sizeof_hdr   = 348
    data_type    = b'\x00' * 10
    db_name      = b'\x00' * 18
    extents      = 0
    session_err  = 0
    regular      = ord('r')
    dim_info     = 0

    # dim[0]=3 means 3-D; dim[1..3] = nx, ny, nz; rest = 1
    dim = (3, nx, ny, nz, 1, 1, 1, 1)

    intent_p1 = intent_p2 = intent_p3 = 0.0
    intent_code = 0
    datatype    = 16    # float32
    bitpix      = 32
    slice_start = 0
    pixdim      = (1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0)
    vox_offset  = 352.0  # data starts right after header+extension
    scl_slope   = 0.0
    scl_inter   = 0.0
    slice_end   = 0
    slice_code  = 0
    xyzt_units  = 2      # mm
    cal_max     = 0.0
    cal_min     = 0.0
    slice_dur   = 0.0
    toffset     = 0.0
    glmax       = 0
    glmin       = 0
    descrip     = b'test submission\x00' + b'\x00' * 64  # 80 bytes
    aux_file    = b'\x00' * 24
    qform_code  = 1
    sform_code  = 1
    quatern_b   = quatern_c = quatern_d = 0.0
    qoffset_x   = qoffset_y = qoffset_z = 0.0
    srow_x      = (1.0, 0.0, 0.0, 0.0)
    srow_y      = (0.0, 1.0, 0.0, 0.0)
    srow_z      = (0.0, 0.0, 1.0, 0.0)
    intent_name = b'\x00' * 16
    magic       = b'n+1\x00'

    hdr = struct.pack(
        '<i',    sizeof_hdr)      +  \
        data_type                 +  \
        db_name                   +  \
        struct.pack('<ihbb',
            extents, session_err, regular, dim_info) + \
        struct.pack('<8h', *dim)  +  \
        struct.pack('<3f', intent_p1, intent_p2, intent_p3) + \
        struct.pack('<hhh', intent_code, datatype, bitpix) + \
        struct.pack('<h', slice_start)  + \
        struct.pack('<8f', *pixdim)     + \
        struct.pack('<fff', vox_offset, scl_slope, scl_inter) + \
        struct.pack('<h', slice_end)    + \
        struct.pack('<bb', slice_code, xyzt_units) + \
        struct.pack('<4f', cal_max, cal_min, slice_dur, toffset) + \
        struct.pack('<2i', glmax, glmin) + \
        descrip   + aux_file     +  \
        struct.pack('<hh', qform_code, sform_code) + \
        struct.pack('<6f', quatern_b, quatern_c, quatern_d,
                           qoffset_x, qoffset_y, qoffset_z) + \
        struct.pack('<4f', *srow_x)  + \
        struct.pack('<4f', *srow_y)  + \
        struct.pack('<4f', *srow_z)  + \
        intent_name + magic

    assert len(hdr) == 348, f"Header is {len(hdr)} bytes, expected 348"

    # 4-byte extension block (all zeros = no extensions)
    ext_block = b'\x00\x00\x00\x00'
    return hdr + ext_block


def _float32_data(nx: int, ny: int, nz: int, value: float = 1.0) -> bytes:
    """Return nx*ny*nz little-endian float32 values."""
    return struct.pack(f'<{nx*ny*nz}f', *([value] * (nx * ny * nz)))


def write_nifti_gz(path: Path, nx: int = 10, ny: int = 10, nz: int = 10,
                   value: float = 1.0) -> None:
    """Write a minimal valid .nii.gz file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = _nifti1_header(nx, ny, nz)
    data   = _float32_data(nx, ny, nz, value)
    with gzip.open(path, 'wb') as f:
        f.write(header + data)
    print(f"  wrote {path}  ({path.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# Submission builders
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent


def _write_readme(folder: Path, challenge: str) -> None:
    (folder / 'README.md').write_text(
        f"# Test {challenge.upper()} Submission\n\n"
        "Minimal synthetic submission for pipeline testing.\n\n"
        "## Contents\n"
        "- Parameter maps as `.nii.gz` volumes (10×10×10, float32, all voxels = 1.0)\n"
        "- `run.py` entry point\n"
        "- `Dockerfile`\n"
        "- `metadata.json`\n",
        encoding='utf-8',
    )


def _write_run_py(folder: Path, challenge: str) -> None:
    (folder / 'run.py').write_text(
        f'"""Entry point for the {challenge.upper()} challenge container."""\n\n'
        "if __name__ == '__main__':\n"
        "    print('Running test submission')\n",
        encoding='utf-8',
    )


def _write_dockerfile(folder: Path) -> None:
    (folder / 'Dockerfile').write_text(
        "FROM python:3.9-slim\n"
        "WORKDIR /app\n"
        "COPY . .\n"
        'CMD ["python3", "run.py"]\n',
        encoding='utf-8',
    )


def _write_metadata(folder: Path, challenge: str, maps: List[str]) -> None:
    meta = {
        "challenge_type": challenge,
        "team_name": "test-team",
        "parameter_maps": maps,
        "description": f"Minimal synthetic {challenge.upper()} test submission",
    }
    (folder / 'metadata.json').write_text(
        json.dumps(meta, indent=2), encoding='utf-8')


def create_asl_submission() -> None:
    folder = ROOT / 'data' / 'extracted' / 'test_asl_submission'
    print(f"\nCreating ASL test submission → {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    write_nifti_gz(folder / 'cbf.nii.gz',  value=60.0)   # CBF map (ml/100g/min)
    write_nifti_gz(folder / 'att.nii.gz',  value=1.2)    # ATT map (seconds)
    _write_readme(folder, 'asl')
    _write_run_py(folder, 'asl')
    _write_dockerfile(folder)
    _write_metadata(folder, 'asl', ['cbf', 'att'])
    print("  ASL submission ready ✓")


def create_dce_submission() -> None:
    folder = ROOT / 'data' / 'extracted' / 'test_dce_submission'
    print(f"\nCreating DCE test submission → {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    write_nifti_gz(folder / 'ktrans.nii.gz', value=0.15)  # Ktrans (min⁻¹)
    write_nifti_gz(folder / 'kep.nii.gz',    value=0.45)  # kep (min⁻¹)
    write_nifti_gz(folder / 'vp.nii.gz',     value=0.05)  # vp (fraction)
    _write_readme(folder, 'dce')
    _write_run_py(folder, 'dce')
    _write_dockerfile(folder)
    _write_metadata(folder, 'dce', ['ktrans', 'kep', 'vp'])
    print("  DCE submission ready ✓")


def create_dsc_submission() -> None:
    folder = ROOT / 'data' / 'extracted' / 'test_dsc_submission'
    print(f"\nCreating DSC test submission → {folder}")
    folder.mkdir(parents=True, exist_ok=True)
    write_nifti_gz(folder / 'cbv.nii.gz',  value=4.0)   # CBV (ml/100g)
    write_nifti_gz(folder / 'cbf.nii.gz',  value=50.0)  # CBF (ml/100g/min)
    write_nifti_gz(folder / 'mtt.nii.gz',  value=4.8)   # MTT (seconds)
    _write_readme(folder, 'dsc')
    _write_run_py(folder, 'dsc')
    _write_dockerfile(folder)
    _write_metadata(folder, 'dsc', ['cbv', 'cbf', 'mtt'])
    print("  DSC submission ready ✓")


if __name__ == '__main__':
    create_asl_submission()
    create_dce_submission()
    create_dsc_submission()
    print("\nAll test submissions created.")
    print("To validate them, run the app and point it at one of the folders above,")
    print("or use the CLI:")
    print("  .venv/bin/python3 -m osipi_pipeline.validation.validate \\")
    print("      --input data/extracted/test_asl_submission --challenge asl")
