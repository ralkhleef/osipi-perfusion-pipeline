from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from osipi_pipeline.config.rules import required_maps_by_challenge


ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = ROOT / "scripts" / "make_example_scoring_package.py"
TRACKED_EXAMPLE = ROOT / "examples" / "demo-scoring-package"


def _load_generator():
    spec = importlib.util.spec_from_file_location("example_package_generator", GENERATOR_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_uses_configured_inputs_for_every_challenge(tmp_path: Path) -> None:
    generator = _load_generator()
    configured = required_maps_by_challenge()

    for challenge in ("asl", "dce", "dsc"):
        archive = generator.build(challenge, False, tmp_path / f"{challenge}.zip")
        with zipfile.ZipFile(archive) as package:
            manifest = json.loads(package.read("manifest.json"))
            scorer = package.read("scoring.py").decode("utf-8").lower()

        assert manifest["challenge_type"] == challenge
        assert manifest["required_inputs"] == list(configured[challenge])
        assert manifest["official"] is False
        assert "score" not in manifest["metrics"]
        assert "random" not in scorer
        assert "hashlib" not in scorer


def test_generator_rejects_a_challenge_missing_from_config(tmp_path: Path) -> None:
    generator = _load_generator()
    output = tmp_path / "unknown.zip"

    with pytest.raises(ValueError, match="Unknown challenge"):
        generator.build("unknown", False, output)

    assert not output.exists()


def test_tracked_dce_example_calculates_values_from_nifti_data(
    tmp_path: Path,
) -> None:
    submission = tmp_path / "submission"
    output = tmp_path / "output"
    submission.mkdir()

    nib.save(
        nib.Nifti1Image(
            np.array([[[-1.0, 1.0], [np.nan, 3.0]]], dtype=np.float32),
            np.eye(4),
        ),
        submission / "scan-1_ktrans.nii.gz",
    )
    nib.save(
        nib.Nifti1Image(np.full((1, 2, 2), 2.0, dtype=np.float32), np.eye(4)),
        submission / "scan-2_ktrans.nii.gz",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(TRACKED_EXAMPLE / "scoring.py"),
            "--submission-dir",
            str(submission),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads((output / "metrics.json").read_text())
    summary = result["summary"]
    assert summary["file_count"] == 2
    assert summary["readable_file_count"] == 2
    assert summary["mean_finite_percent"] == pytest.approx(87.5)
    assert summary["mean_negative_percent"] == pytest.approx(100 / 6)
    assert summary["mean_of_map_means"] == pytest.approx(1.5)
    assert summary["official_osipi_scoring"] is False

    manifest = json.loads((TRACKED_EXAMPLE / "manifest.json").read_text())
    assert set(manifest["metrics"]) <= set(summary)
    assert manifest["required_inputs"] == list(required_maps_by_challenge()["dce"])
    assert not {"demo_rmse", "demo_bias", "demo_cv", "demo_score"} & set(summary)


def test_generated_dsc_example_runs_on_dsc_maps(tmp_path: Path) -> None:
    generator = _load_generator()
    archive = generator.build("dsc", False, tmp_path / "dsc.zip")
    package_dir = tmp_path / "package"
    with zipfile.ZipFile(archive) as package:
        package.extractall(package_dir)

    submission = tmp_path / "dsc-submission"
    output = tmp_path / "dsc-output"
    submission.mkdir()
    for map_name, value in (("cbv", 1.0), ("cbf", 2.0), ("mtt", 3.0)):
        nib.save(
            nib.Nifti1Image(
                np.full((2, 2, 2), value, dtype=np.float32), np.eye(4)
            ),
            submission / f"scan-1_{map_name}.nii.gz",
        )

    completed = subprocess.run(
        [
            sys.executable,
            str(package_dir / "scoring.py"),
            "--submission-dir",
            str(submission),
            "--output-dir",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    summary = json.loads((output / "results.json").read_text())
    assert summary["file_count"] == 3
    assert summary["readable_file_count"] == 3
    assert summary["mean_finite_percent"] == pytest.approx(100.0)
    assert summary["mean_negative_percent"] == pytest.approx(0.0)
    assert summary["mean_of_map_means"] == pytest.approx(2.0)
