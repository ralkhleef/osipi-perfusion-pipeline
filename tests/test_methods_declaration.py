"""A methods document is asked about, not demanded.

It used to be required outright for DCE: a submission without one failed
validation. The challenge leads have since said it is not needed for the runs
being done now. That is their call, so the blanket requirement is gone -- but
"not required" is easy to slide into "not tracked", and these tests hold the
line between the two.

What replaces the requirement:

* the upload form asks whether a methods document is included;
* answering no is recorded, blocks nothing, and puts a blank template in the
  submission for the team to complete;
* answering yes and sending none is an error, because that is a mismatch
  between what the submitter said and what arrived, not a policy question;
* not being asked is recorded as not being asked, and never reported as if the
  submitter had said no.

The case worth the most care is the inserted template. It is a file whose name
contains "methods" and whose content contains no methods, so anything that
counts files would count it. It must not be reported as a methods document
while it is blank, and it must be reported as one the moment somebody fills it
in -- which is why the test for both is here rather than left to inspection.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src", "backend"):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

nib = pytest.importorskip("nibabel")

from services import methods_declaration_service as declaration  # noqa: E402


# ── The answer itself ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["yes", "Yes", "YES", "true", "1", True, "y"])
def test_every_way_of_saying_yes_is_yes(raw) -> None:
    assert declaration.normalize(raw) == declaration.PROVIDED


@pytest.mark.parametrize("raw", ["no", "No", "false", "0", False, "n"])
def test_every_way_of_saying_no_is_no(raw) -> None:
    assert declaration.normalize(raw) == declaration.NOT_PROVIDED


@pytest.mark.parametrize("raw", [None, "", "   ", "maybe", "unknown", 42, []])
def test_an_answer_that_cannot_be_read_is_not_an_answer(raw) -> None:
    """Guessing which way an unreadable answer went is worse than recording none.

    Reading it as "no" would put words in the submitter's mouth; reading it as
    "yes" would raise an error they never earned.
    """
    assert declaration.normalize(raw) is declaration.UNDECLARED


def test_not_asked_is_not_the_same_as_answering_no() -> None:
    assert declaration.PROVIDED != declaration.UNDECLARED
    assert declaration.NOT_PROVIDED is not declaration.UNDECLARED


# ── Recording it ───────────────────────────────────────────────────────────

@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """Point the declaration store and submission folders at a scratch tree."""
    outputs = tmp_path / "outputs"
    extracted = tmp_path / "extracted"
    outputs.mkdir()
    extracted.mkdir()
    monkeypatch.setattr(declaration, "OUTPUTS_DIR", outputs)
    return tmp_path


def test_an_answer_survives_being_written_and_read_back(workspace) -> None:
    declaration.record("sub-1", "no")
    assert declaration.load("sub-1")["methods_document_declared"] == declaration.NOT_PROVIDED


def test_a_submission_nobody_answered_for_reads_as_undeclared(workspace) -> None:
    assert declaration.load("never-asked")["methods_document_declared"] is None


def test_a_corrupt_record_is_no_record_rather_than_a_no(workspace, monkeypatch) -> None:
    """An unreadable file is not a declaration.

    Reporting "not provided" from a corrupt record would be inventing an
    answer out of a disk error.
    """
    declaration.record("sub-1", "yes")
    declaration._declaration_file("sub-1").write_text("{ not json", encoding="utf-8")
    assert declaration.load("sub-1")["methods_document_declared"] is None


def test_the_record_is_kept_outside_the_submission(workspace) -> None:
    """A manifest rebuild rescans the submission folder and must not lose it."""
    declaration.record("sub-1", "no")
    stored = declaration._declaration_file("sub-1")
    assert stored.exists()
    assert "extracted" not in stored.parts


# ── The template is offered, never imposed ─────────────────────────────────

def test_the_template_says_it_is_blank_in_its_own_first_lines() -> None:
    """Whoever downloads it must be able to tell a finished document from an
    untouched one without opening this code."""
    body = declaration.template_text()
    assert "BLANK TEMPLATE" in body.upper()


def test_the_template_does_not_prescribe_what_a_methods_document_must_contain() -> None:
    """The prompts are a starting point. What is required is the leads' call,
    and the template has to say so rather than quietly becoming the rule."""
    body = declaration.template_text().lower()
    assert "not the challenge's requirements" in body
    assert "challenge leads decide" in body


def test_nothing_in_this_module_writes_into_a_submission() -> None:
    """The reversal, enforced rather than remembered.

    An earlier version dropped a blank template into any submission that
    declared no document. It was withdrawn: we do not know whether a team has a
    methods document of their own or wants ours, and a submission that arrives
    without one has to move through the pipeline untouched. If a write ever
    reappears here, this is where it surfaces.
    """
    source = (Path(declaration.__file__)).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]          # skip the module docstring
    for forbidden in ("write_text(", "write_bytes(", "copy(", "copy2(", "mkdir("):
        if forbidden == "mkdir(":
            # The declaration store is outside every submission; that one is
            # allowed and is the only directory this module creates.
            assert body.count(forbidden) == 1
            continue
        assert forbidden not in body.replace(
            'path.write_text(json.dumps(entry, indent=2), encoding="utf-8")', ""
        ), f"{forbidden} is back in the methods declaration service"

    public = {name for name in dir(declaration) if not name.startswith("_")}
    for banned in ("insert", "template_inserted", "placeholder"):
        assert not any(banned in name.lower() for name in public), (
            f"{banned!r} is back on this module's public surface"
        )


# ── Nothing requires one any more ──────────────────────────────────────────

def test_no_shipped_challenge_requires_a_methods_document() -> None:
    """The leads said it is not needed. This is the check that they were heard.

    If it becomes required again, the fix is one line of configuration, and
    this test is where the change announces itself.
    """
    pytest.importorskip("yaml")
    from osipi_pipeline.config.rules import required_artifacts_by_challenge

    configured = required_artifacts_by_challenge()
    assert configured, "no challenges are configured"
    for challenge, artifacts in sorted(configured.items()):
        assert "methods" not in artifacts, (
            f"{challenge} still demands a methods document outright"
        )


def test_the_methods_artifact_type_still_exists() -> None:
    """Relaxing the requirement must not delete the ability to detect one."""
    pytest.importorskip("yaml")
    from osipi_pipeline.config.rules import artifact_type_specs

    assert "methods" in artifact_type_specs()


# ── Through the real routes ────────────────────────────────────────────────

def _submission_zip() -> bytes:
    """A minimal DCE submission: one Ktrans map and one fitted curve."""
    buf = io.BytesIO()
    affine = np.diag([2.0, 2.0, 4.0, 1.0])
    with zipfile.ZipFile(buf, "w") as zf:
        for name, shape in (("Ktrans.nii.gz", (4, 4, 2)), ("Ct.nii.gz", (4, 4, 2, 3))):
            import tempfile
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / name
                nib.save(nib.Nifti1Image(np.ones(shape, np.float32), affine), str(path))
                zf.writestr(f"team/{name}", path.read_bytes())
    return buf.getvalue()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """The real FastAPI app, pointed at a scratch workspace."""
    pytest.importorskip("fastapi")
    import services.path_config as pc

    mapping = {
        "INCOMING_DIR": tmp_path / "incoming",
        "EXTRACTED_DIR": tmp_path / "extracted",
        "OUTPUTS_DIR": tmp_path / "outputs",
        "VALIDATION_SUBDIR": tmp_path / "outputs" / "validation",
        "PREVIEW_ROOT": tmp_path / "outputs" / "previews",
        "REFERENCE_DATA_DIR": tmp_path / "reference_data",
        "SCORING_DIR": tmp_path / "scoring",
        "SCORING_OUTPUTS_DIR": tmp_path / "score_out",
        "SCORING_RESULTS_DIR": tmp_path / "score_out",
    }
    for name, value in mapping.items():
        monkeypatch.setattr(pc, name, value, raising=False)
    for module in list(sys.modules.values()):
        for name, value in mapping.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value, raising=False)
    for directory in mapping.values():
        directory.mkdir(parents=True, exist_ok=True)

    from fastapi.testclient import TestClient
    import backend.main as app_module

    for name, value in mapping.items():
        if hasattr(app_module, name):
            monkeypatch.setattr(app_module, name, value, raising=False)
    with TestClient(app_module.app) as test_client:
        yield test_client, mapping


def _upload(client, answer=None):
    data = {"methods_document": answer} if answer is not None else {}
    response = client.post(
        "/api/upload-submission",
        files={"file": ("team.zip", _submission_zip(), "application/zip")},
        data=data,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body.get("submission_id") or (body.get("submissions") or [{}])[0]["submission_id"]


def test_the_template_can_be_downloaded_from_the_upload_screen(client) -> None:
    test_client, _ = client
    response = test_client.get("/api/methods-template")
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    assert "BLANK TEMPLATE" in response.text.upper()


def test_saying_no_uploads_and_validates_without_complaint(client) -> None:
    """The behaviour the challenge leads asked for, end to end."""
    test_client, mapping = client
    sid = _upload(test_client, "no")

    response = test_client.post("/api/validate",
                                json={"submission_id": sid, "challenge_type": "dce"})
    assert response.status_code == 200
    result = response.json()
    codes = [issue["code"] for issue in result["errors"] + result["warnings"]]
    assert "REQUIRED_ARTIFACT_MISSING" not in codes
    assert "METHODS_DOCUMENT_DECLARED_BUT_MISSING" not in codes
    assert result["methods_document"]["status"] == "not_provided"

    # The submission is exactly what was uploaded. Saying "no" must not add,
    # rename or rewrite anything in it.
    contents = sorted(p.name for p in (mapping["EXTRACTED_DIR"] / sid).rglob("*")
                      if p.is_file() and not p.name.startswith("."))
    assert contents == ["Ct.nii.gz", "Ktrans.nii.gz"], contents


def test_saying_yes_and_sending_none_is_an_error(client) -> None:
    """Not a policy judgement: they said one thing and sent another."""
    test_client, _ = client
    sid = _upload(test_client, "yes")

    result = test_client.post("/api/validate",
                              json={"submission_id": sid, "challenge_type": "dce"}).json()
    codes = [issue["code"] for issue in result["errors"]]
    assert "METHODS_DOCUMENT_DECLARED_BUT_MISSING" in codes
    assert result["methods_document"]["status"] == "declared_but_missing"


def test_not_being_asked_blocks_nothing_and_claims_nothing(client) -> None:
    test_client, mapping = client
    sid = _upload(test_client)

    result = test_client.post("/api/validate",
                              json={"submission_id": sid, "challenge_type": "dce"}).json()
    codes = [issue["code"] for issue in result["errors"]]
    assert "METHODS_DOCUMENT_DECLARED_BUT_MISSING" not in codes
    assert result["methods_document"]["status"] == "not_declared"


def test_a_submission_with_its_own_document_moves_on_without_the_template(client) -> None:
    """The team who wrote their own, or downloaded ours and filled it in.

    Either way it is their file, and the pipeline neither adds to it nor asks
    where it came from.
    """
    test_client, mapping = client
    sid = _upload(test_client, "yes")
    (mapping["EXTRACTED_DIR"] / sid / "methods.txt").write_text(
        "We fitted the extended Tofts model.\n", encoding="utf-8")

    result = test_client.post("/api/validate",
                              json={"submission_id": sid, "challenge_type": "dce"}).json()
    codes = [issue["code"] for issue in result["errors"]]
    assert "METHODS_DOCUMENT_DECLARED_BUT_MISSING" not in codes
    assert result["methods_document"]["status"] == "provided"
    assert result["methods_document"]["documents_found"] == ["methods.txt"]


def test_the_report_states_which_it_was(client) -> None:
    test_client, _ = client
    sid = _upload(test_client, "no")
    test_client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce"})

    response = test_client.get("/api/report",
                               params={"submission_id": sid, "blinded": "false"})
    assert response.status_code == 200
    # Both halves in one string, deliberately. "Not provided" on its own passes
    # against unrelated report text such as "Units: Not provided", which is how
    # this assertion first passed while the report said "Not recorded".
    assert "Methods document</span><strong>Not provided (declared by the submitter)" \
        in response.text
