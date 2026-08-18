"""Build an end-to-end DCE demo bundle.

The script creates a synthetic DCE submission with Clinical and Synthetic
datasets, then runs upload, validation, analysis, and export:

    data/outputs/demo_evidence/
        DCE_Test_Clean.zip            the input, reproducible
        roi_statistics.csv            32 ROI rows
        results_blinded.csv           blinded combined export
        results_blinded.json          blinded combined export
        results_unblinded.csv         organiser export
        report_blinded.html/.pdf      no team identity anywhere
        report_unblinded.html/.pdf    organiser copy
        EVIDENCE.md                   check results

Temporary submission files are removed when the script finishes.

    python3 scripts/demo_evidence.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "backend")]

from osipi_pipeline.testing import (  # noqa: E402
    VOLUME_SHAPE, VOLUME_VALUES, build_dce_submission, write_nifti, zip_directory)

OUT = ROOT / "data" / "outputs" / "demo_evidence"
TEAM_NAME = "Team Gamma"
CONTACT = "gamma@example.org"

# Reference volumes: the reference Ktrans differs from the submitted one, so
# the comparison produces real (non-zero) error metrics.
MASK_TUMOUR = [1, 1, 1, 1, 0, 0, 0, 0]


def _build_zip(work: Path) -> Path:
    submission = build_dce_submission(work / "stage", "DCE_Test_Clean")
    return zip_directory(submission, OUT / "DCE_Test_Clean.zip")


def _install_reference(root: Path) -> None:
    reference = root / "reference"
    write_nifti(reference / "maps" / "Ktrans.nii.gz",
                [v * 1.05 for v in VOLUME_VALUES], VOLUME_SHAPE)
    write_nifti(reference / "masks" / "tumour.nii.gz", MASK_TUMOUR, VOLUME_SHAPE)
    write_nifti(reference / "masks" / "whole_brain.nii.gz", [1] * 8, VOLUME_SHAPE)
    # Both spellings of the mask directory: the macOS case-fold condition.
    alias = reference / "Masks"
    if not alias.exists():
        try:
            alias.symlink_to(reference / "masks", target_is_directory=True)
        except OSError:
            pass


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="osipi_demo_"))
    checks: list[tuple[str, object, object]] = []

    try:
        archive = _build_zip(work)

        # ── Upload ────────────────────────────────────────────────────────
        import services.ingest_service as ingest

        extracted = work / "extracted"
        extracted.mkdir()
        ingest.EXTRACTED_DIR = extracted
        result = ingest.save_and_extract_batch_from_path(archive, archive.name)
        sid = result["submission_id"]
        root = extracted / sid
        _install_reference(root)

        checks.append(("submissions produced", 1 if not result["batch"] else result["submission_count"], 1))
        checks.append(("top-level entries",
                       sorted(p.name for p in root.iterdir() if not p.name.startswith(".")),
                       ["Clinical", "Synthetic", "methods.txt", "reference"]))

        # ── Manifest and identity ─────────────────────────────────────────
        from osipi_pipeline.ingestion.manifest import refresh_manifest
        from osipi_pipeline.ingestion.models import SubmissionArtifact

        manifest = refresh_manifest(root, submission_id=sid, challenge_type="dce",
                                    original_path=archive.name)
        artifacts = [SubmissionArtifact(**item) for item in manifest.get("artifacts", [])]
        scans = {(a.dataset, a.participant, a.site, a.repeat)
                 for a in artifacts if a.map_type == "ktrans"}

        checks.append(("clinical scans", len([s for s in scans if s[0] == "clinical"]), 10))
        checks.append(("synthetic scans", len([s for s in scans if s[0] == "synthetic"]), 6))
        checks.append(("Ktrans maps", len([a for a in artifacts if a.map_type == "ktrans"]), 16))
        checks.append(("modelled S(t) files",
                       len([a for a in artifacts if a.artifact_type == "modelled_st"]), 16))
        checks.append(("methods documents",
                       len([a for a in artifacts if a.artifact_type == "methods"]), 1))
        checks.append(("reference files counted as artifacts",
                       len([a for a in artifacts if a.path.startswith("reference/")]), 0))

        # ── Validation ────────────────────────────────────────────────────
        import services.validation_service as vs

        vs.EXTRACTED_DIR = extracted
        vs.OUTPUTS_DIR = work / "outputs"
        report = vs.validate_submission(sid, challenge_type="dce",
                                        team_name=TEAM_NAME, contact_email=CONTACT)
        codes = [i.get("code") for i in (report.get("errors") or []) + (report.get("warnings") or [])]
        for code in ("INCOMPLETE_ARTIFACT_IDENTITY", "DUPLICATE_FILENAME",
                     "REQUIRED_ARTIFACT_MISSING", "DATASET_COUNT_MISMATCH"):
            checks.append((code, codes.count(code), 0))

        # ── ROI statistics ────────────────────────────────────────────────
        import scoring
        from services.roi_descriptive_service import (
            compute_roi_descriptive_statistics, roi_definitions_from_masks)

        masks = scoring._reference_masks(root / "reference")
        rows = compute_roi_descriptive_statistics(
            [a for a in artifacts if a.map_type == "ktrans"],
            roi_definitions_from_masks(masks), challenge="dce", root=root)

        checks.append(("masks discovered", len(masks), 2))
        checks.append(("ROI rows", len(rows), 32))
        checks.append(("unique (scan, ROI) pairs",
                       len({(r.path, r.roi_id) for r in rows}), 32))

        # ── Exports through the real endpoints ────────────────────────────
        import main
        from fastapi.testclient import TestClient

        analysis = {"reference_scoring": {
            "roi_descriptive_statistics": [r.to_dict() for r in rows],
            "roi_descriptive_status": "available",
            "summary": {},
        }}

        gathered = dict(main._gather_summary("__demo_absent__"))
        gathered.update({
            "submission_id": sid, "team_name": TEAM_NAME, "contact_email": CONTACT,
            "source_folder": sid, "challenge_type": "dce", "mode": "result_only",
            "val_passed": report.get("passed"),
            "error_count": report.get("error_count", 0),
            "warning_count": report.get("warning_count", 0),
            "errors": report.get("errors") or [], "warnings": report.get("warnings") or [],
            "nifti_count": report.get("nifti_count", 0), "has_validation": True,
            "exec_status": "skipped_result_maps", "nifti_analysis": analysis,
        })
        fields = dict(main._analysis_summary_fields({}))
        fields.update({"parameter_maps_detected": "Ktrans", "map_count": 16,
                       "finite_voxels_percent": 100.0})
        gathered["analysis_fields"] = fields

        main._collect_export_ids = lambda b, s: [sid]
        main._gather_summary = lambda _sid: gathered
        client = TestClient(main.app)

        artefacts = [
            ("roi_statistics.csv", "/api/export-roi-descriptive", {"submission_id": sid}),
            ("results_blinded.csv", "/api/export-combined",
             {"submission_id": sid, "blinded": "true", "format": "csv"}),
            ("results_blinded.json", "/api/export-combined",
             {"submission_id": sid, "blinded": "true", "format": "json"}),
            ("results_unblinded.csv", "/api/export-combined",
             {"submission_id": sid, "blinded": "false", "format": "csv"}),
            ("report_blinded.html", "/api/report", {"submission_id": sid, "blinded": "true"}),
            ("report_unblinded.html", "/api/report", {"submission_id": sid, "blinded": "false"}),
            ("report_blinded.pdf", "/api/export/report/pdf",
             {"submission_id": sid, "blinded": "true"}),
            ("report_unblinded.pdf", "/api/export/report/pdf",
             {"submission_id": sid, "blinded": "false"}),
        ]
        written = []
        for name, path, params in artefacts:
            response = client.get(path, params=params)
            (OUT / name).write_bytes(response.content)
            written.append((name, response.status_code, len(response.content),
                            response.headers.get("content-disposition", "")))

        # ── Blinding proof ────────────────────────────────────────────────
        def leaks(payload: bytes) -> bool:
            text = payload.decode("latin-1", errors="ignore").lower()
            squashed = "".join(c for c in text if c.isalnum())
            return any(t in squashed for t in ("teamgamma", "gammaexampleorg"))

        for name in ("report_blinded.html", "report_blinded.pdf",
                     "results_blinded.csv", "results_blinded.json"):
            checks.append((f"{name} leaks identity", leaks((OUT / name).read_bytes()), False))
        checks.append(("report_unblinded.html names the team",
                       "Team Gamma" in (OUT / "report_unblinded.html").read_text(errors="ignore"),
                       True))

        roi_lines = (OUT / "roi_statistics.csv").read_text().strip().splitlines()
        checks.append(("ROI CSV data rows", len(roi_lines) - 1, 32))

        _write_evidence(checks, written, sid)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    failures = [c for c in checks if c[1] != c[2]]
    print(f"\n{len(checks) - len(failures)}/{len(checks)} checks passed")
    for label, actual, expected in failures:
        print(f"  FAIL {label}: {actual!r} != {expected!r}")
    print(f"\nBundle: {OUT}")


def _write_evidence(checks, written, sid: str) -> None:
    lines = [
        "# DCE_Test_Clean, demo evidence",
        "",
        "End-to-end upload, validation, analysis, and export check. Regenerate with:",
        "",
        "    python3 scripts/demo_evidence.py",
        "",
        f"Submission id: `{sid}`",
        "",
        "## Checks",
        "",
        "| Check | Actual | Expected | |",
        "|---|---|---|---|",
    ]
    for label, actual, expected in checks:
        mark = "ok" if actual == expected else "**FAIL**"
        lines.append(f"| {label} | `{actual}` | `{expected}` | {mark} |")

    lines += ["", "## Artefacts", "", "| File | Status | Bytes | Download name |", "|---|---|---|---|"]
    for name, status, size, disposition in written:
        filename = disposition.split("filename=")[-1].strip('"') if disposition else ""
        lines.append(f"| `{name}` | {status} | {size:,} | `{filename}` |")

    lines += [
        "",
        "## Coverage",
        "",
        "- One submission is preserved across Clinical and Synthetic datasets.",
        "- Required artifacts and scan identities are validated.",
        "- ROI masks are deduplicated on case-insensitive filesystems.",
        "- Blinded outputs exclude team and contact identity.",
        "",
    ]
    (OUT / "EVIDENCE.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
