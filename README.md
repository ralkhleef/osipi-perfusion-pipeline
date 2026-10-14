# OSIPI Perfusion Pipeline

A local web app for reviewing DCE, ASL, and DSC perfusion MRI challenge submissions.

**Upload → Review → Validate → Run → QC & Preview → Export**

The app accepts a ZIP, folder/files, public GitHub repository, or Zenodo record.
It validates submitted files, runs participant code in Docker when needed, and
creates QC, previews, reports, and structured exports. Additional analysis is
shown when compatible masks, references, or trusted analysis packages are
available.

Official OSIPI challenge ranking is not currently configured.

## Run locally

```bash
docker compose up --build
```

Open http://localhost:8000.

Stop the app with:

```bash
docker compose down
```

The `scripts/start/` and `scripts/stop/` folders also contain macOS, Linux, and
Windows launchers for reviewers who prefer not to type Docker commands.

## Testing with the DCE test data

The DCE test data contains three folders. Only `submission/` is uploaded in
the app. Copy the other two into `data/reference_data/` first:

| Folder in the test data | Where it goes | Uploaded in the app? |
|---|---|---|
| `hidden_ground_truth/` (`P01/ … P10/`) | copy its `P01 … P10` folders into `data/reference_data/maps/` | No |
| `shared_masks/` (`site_1/ … site_3/`) | copy its `site_*` folders into `data/reference_data/dce/masks/` | No |
| `submission/` | — | Yes |

After copying, the layout inside the repository is:

```text
osipi-perfusion-pipeline/
└── data/
    └── reference_data/
        ├── maps/
        │   ├── P01/site_1/scan_1/{Ct,Ktrans,vp}.nii.gz
        │   └── ...
        └── dce/
            └── masks/
                ├── site_1/*_mask.nii.gz
                ├── site_2/*_mask.nii.gz
                └── site_3/*_mask.nii.gz
```

Start the app:

- macOS: double-click `scripts/start/start.command`. If macOS says it cannot
  be opened, right-click it, choose *Open*, then *Open* again.
- Windows: double-click `scripts\start\start.bat`.
- Or run `docker compose up --build` from the project folder.

Docker Desktop must be installed and running. The first start downloads and
builds the image, which takes a few minutes. The app then opens at
http://localhost:8000. In the app:

1. Upload: enter a team name and email, choose *Select folder*, and select
   the `submission` folder.
2. Review: press *Validate*.
3. Validate: press *Continue*.
4. Run: press *Continue*.
5. QC & Preview: wait for "Ground truth is loaded", then press *Run analysis*.
6. Export: download the CSV, JSON, HTML or PDF results.

macOS `._*` and `__MACOSX` files are ignored, so they do not need to be
deleted. `data/reference_data/` is ignored by Git.

## Repository guide

| If you want to… | Go to… |
|---|---|
| Change challenge requirements | `config/validation_rules.yaml` or the Configuration Manager in the app |
| Change runtime limits or paths | `config/settings.yaml` |
| Change API routes or report exports | `backend/` |
| Change ingestion, validation, execution, or analysis logic | `src/osipi_pipeline/` |
| Change the web interface | `frontend/` |
| Add or update tests | `tests/` |
| Change the GitHub Pages site | `docs/` |
| Work with a custom analysis package | `examples/README.md` |
| Review pending scientific decisions | `notes/SCIENTIFIC_REQUIREMENTS_PENDING.md` |

The [code walkthrough](notes/CODE_WALKTHROUGH.md) explains how these areas connect.
More focused maintainer notes are available for
[configuration](notes/configuration.md),
[ingestion](notes/ingestion_notes.md),
[Docker execution](notes/execution_notes.md),
[updating scoring](notes/UPDATING_SCORING.md), and
[adding a metric](notes/ADDING_SCORING_METRICS.md).

## Run tests

Install the development dependencies once:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r backend/requirements.txt -r requirements-test.txt
```

Run the Python and frontend suites (the frontend suites are discovered, so
a new one needs no edit here):

```bash
OSIPI_REQUIRE_FULL_TESTS=1 PYTHONPATH=.:backend:src .venv/bin/pytest -q
for suite in tests/*_test.js; do node "$suite"; done
```

The tracked DCE example in `examples/demo-scoring-package/` calculates simple
map summaries from its input files. To make the same ready-to-upload example
for any configured challenge, run:

```bash
python3 scripts/make_example_scoring_package.py --challenge asl
python3 scripts/make_example_scoring_package.py --challenge dce
python3 scripts/make_example_scoring_package.py --challenge dsc
```

The generator reads required map names from `config/validation_rules.yaml`.
These examples do not contain an overall score, ranking, or acceptance limit.
Test inputs are in `tests/fixtures/`. Generated outputs, uploaded submissions,
private reference assets, and installed scoring packages are ignored by Git.

## More information

- [Documentation](https://ralkhleef.github.io/osipi-perfusion-pipeline/)
- [Installation](https://ralkhleef.github.io/osipi-perfusion-pipeline/install.html)
- [Configuration](https://ralkhleef.github.io/osipi-perfusion-pipeline/configuration.html)
- [GSoC work product](notes/GSoC_WORK_PRODUCT.md)
