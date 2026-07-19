# Development Setup

## Requirements

- Python 3.9+
- Docker Desktop (for the execution step)

## Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r backend/requirements.txt -r requirements-test.txt
```

## Run the backend server

```bash
cd backend
python3 -m uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000.

## Run tests

```bash
PYTHONPATH=src python3 -m pytest
```

## Create a test submission for ingestion

```bash
mkdir -p submissions/incoming/demo_ingestion
python3 - <<'PY'
import gzip
import struct
from pathlib import Path

def write_tiny_nifti(path: Path) -> None:
    shape = (2, 2, 1)
    header = bytearray(348)
    header[0:4] = (348).to_bytes(4, "little")
    header[344:348] = b"n+1\0"
    header[40:42] = len(shape).to_bytes(2, "little", signed=True)
    for i, size in enumerate(shape, start=1):
        header[40 + i * 2:42 + i * 2] = int(size).to_bytes(2, "little", signed=True)
    header[70:72] = (16).to_bytes(2, "little", signed=True)
    header[72:74] = (32).to_bytes(2, "little", signed=True)
    header[108:112] = struct.pack("<f", 352.0)
    values = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)
    path.write_bytes(gzip.compress(bytes(header) + b"\0\0\0\0" + values))

root = Path("submissions/incoming/demo_ingestion")
write_tiny_nifti(root / "CBF.nii.gz")
write_tiny_nifti(root / "ATT.nii.gz")
PY
echo '{"team":"demo","challenge":"asl"}' > submissions/incoming/demo_ingestion/metadata.json
echo "# Demo" > submissions/incoming/demo_ingestion/README.md
echo "print('run')" > submissions/incoming/demo_ingestion/run.py

PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest \
  --input submissions/incoming/demo_ingestion \
  --challenge asl
```

## Clean up test data

```bash
rm -rf submissions/incoming/demo_ingestion
rm -rf submissions/extracted/asl/demo_ingestion
```
