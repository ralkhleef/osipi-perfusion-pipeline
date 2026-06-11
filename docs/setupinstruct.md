# Development Setup

## Requirements

- Python 3.9+
- Docker Desktop (for the execution step)

## Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
echo "fake nifti" > submissions/incoming/demo_ingestion/CBF.nii.gz
echo "fake nifti" > submissions/incoming/demo_ingestion/ATT.nii.gz
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
