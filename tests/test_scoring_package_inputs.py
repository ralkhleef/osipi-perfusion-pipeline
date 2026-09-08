from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


def test_required_inputs_filter_maps_from_other_niftis(tmp_path) -> None:
    from services.scoring_package_service import _required_input_paths

    scan = tmp_path / "P05" / "site_1" / "scan_1"
    scan.mkdir(parents=True)
    for name in ("Ktrans.nii.gz", "vp.nii.gz", "Ct.nii.gz"):
        (scan / name).write_bytes(b"placeholder")

    selected = _required_input_paths(
        tmp_path,
        {"challenge_type": "dce", "required_inputs": ["ktrans"]},
    )

    assert [source.name for source, _ in selected] == ["Ktrans.nii.gz"]
    assert selected[0][1] == Path("P05/site_1/scan_1/Ktrans.nii.gz")
