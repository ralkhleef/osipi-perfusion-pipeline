# Data Ingestion Commands

```bash
cd ~/Desktop/osipi-perfusion-pipeline
source .venv/bin/activate
```

```bash
PYTHONPATH=src python -m pytest -q
```

```bash
rm -rf submissions/incoming/demo_ingestion
rm -rf submissions/extracted/asl/demo_ingestion
```

```bash
mkdir -p submissions/incoming/demo_ingestion
```

```bash
echo "fake nifti placeholder" > submissions/incoming/demo_ingestion/CBF.nii.gz
echo "fake nifti placeholder" > submissions/incoming/demo_ingestion/ATT.nii.gz
echo '{"team":"demo_ingestion","challenge":"asl"}' > submissions/incoming/demo_ingestion/metadata.json
echo "# Demo ingestion submission" > submissions/incoming/demo_ingestion/README.md
echo "print('demo run')" > submissions/incoming/demo_ingestion/run.py
```

```bash
PYTHONPATH=src python -m osipi_pipeline.ingestion.ingest \
  --input submissions/incoming/demo_ingestion \
  --challenge asl
```

```bash
find submissions/extracted -maxdepth 4 -type f | head -20
```

```bash
find data/outputs/manifests -type f | tail
```

```bash
cat data/outputs/manifests/asl_demo_ingestion_manifest.json
```

```bash
PYTHONPATH=src python -m osipi_pipeline.validation.validate \
  --input submissions/incoming/demo_ingestion \
  --challenge asl
```

```bash
rm -rf submissions/incoming/demo_ingestion
rm -rf submissions/extracted/asl/demo_ingestion
```

```bash
git status
```
