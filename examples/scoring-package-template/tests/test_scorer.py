from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scorer import find_niftis


def test_find_niftis_ignores_non_nifti_files(tmp_path: Path) -> None:
    (tmp_path / "Ktrans.nii.gz").touch()
    (tmp_path / "methods.txt").touch()
    assert [path.name for path in find_niftis(tmp_path)] == ["Ktrans.nii.gz"]
