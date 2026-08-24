"""Normalized submission artifact, identity, and classification tests."""

from __future__ import annotations

import gzip
import struct
from pathlib import Path

import pytest

from osipi_pipeline.ingestion.artifact_classifier import classify, detect_map_type
from osipi_pipeline.ingestion.identity_parser import (
    parse_directory_identity,
    parse_filename_identity,
    resolve_identity,
)
from osipi_pipeline.ingestion.manifest import build_manifest
from osipi_pipeline.ingestion.models import SubmissionArtifact
from osipi_pipeline.ingestion.nifti_header import read_ndim


# ── Fixtures ──────────────────────────────────────────────────────────────

def _nifti_bytes(ndim: int) -> bytes:
    """A minimal but genuine NIfTI-1 header declaring ``ndim`` dimensions."""
    header = bytearray(352)
    struct.pack_into("<i", header, 0, 348)          # sizeof_hdr
    struct.pack_into("<h", header, 40, ndim)        # dim[0]
    for index in range(ndim):
        struct.pack_into("<h", header, 42 + index * 2, 2)
    header[344:348] = b"n+1\x00"                    # magic
    return bytes(header)


def _write_nifti(path: Path, ndim: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _nifti_bytes(ndim)
    if path.name.endswith(".gz"):
        path.write_bytes(gzip.compress(payload))
    else:
        path.write_bytes(payload)
    return path


def _manifest(root: Path, challenge: str = "dce"):
    return build_manifest(
        submission_id="sub", challenge_type=challenge,
        original_path=root, extracted_path=root,
    )


def _by_path(manifest, suffix: str) -> SubmissionArtifact:
    matches = [a for a in manifest.artifacts if a.path.endswith(suffix)]
    assert matches, f"no artifact ending {suffix!r}"
    return matches[0]


# ── Model ─────────────────────────────────────────────────────────────────

def test_artifact_is_immutable() -> None:
    artifact = SubmissionArtifact(path="a.nii.gz", role="parameter_map")
    with pytest.raises(Exception):
        artifact.path = "b.nii.gz"       # type: ignore[misc]


def test_artifact_serializes_to_json_safe_values() -> None:
    import json

    artifact = SubmissionArtifact(
        path="Synthetic/P1/Ktrans.nii.gz", role="parameter_map",
        challenge="dce", dataset="synthetic", participant="1",
        repeat="1", site="2", map_type="ktrans", dimensions=3,
    )
    payload = artifact.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["map_type"] == "ktrans"


def test_non_map_roles_carry_no_map_type() -> None:
    artifact = SubmissionArtifact(path="methods.docx", role="methods")
    assert artifact.map_type is None
    assert artifact.dimensions is None


# ── Directory-first identity ──────────────────────────────────────────────

@pytest.mark.parametrize("path,expected", [
    ("Synthetic/Participant001/Site1/Repeat1/Ktrans.nii.gz",
     {"dataset": "synthetic", "participant": "1", "repeat": "1", "site": "1"}),
    ("Clinical/Participant005/Repeat2/Ktrans.nii.gz",
     {"dataset": "clinical", "participant": "5", "repeat": "2", "site": None}),
    ("synthetic/sub-003/site-2/visit-1/vp.nii.gz",
     {"dataset": "synthetic", "participant": "3", "repeat": "1", "site": "2"}),
    ("clinical/patient-004/retest/Kep.nii.gz",
     {"dataset": "clinical", "participant": "4", "repeat": "retest", "site": None}),
    ("Synthetic/P001/Site03/Scan02/Ktrans.nii.gz",
     {"dataset": "synthetic", "participant": "1", "repeat": "2", "site": "3"}),
])
def test_directory_identity(path: str, expected: dict) -> None:
    resolved, conflicts = resolve_identity(path, challenge="dce")
    assert resolved == expected
    assert conflicts == []


def test_directory_parsing_is_case_insensitive() -> None:
    upper, _ = resolve_identity("SYNTHETIC/PARTICIPANT001/REPEAT1/x.nii.gz",
                                challenge="dce")
    assert upper["dataset"] == "synthetic"
    assert upper["participant"] == "1"


def test_textual_repeat_labels_are_kept_as_strings() -> None:
    resolved, _ = resolve_identity("clinical/patient-004/retest/Kep.nii.gz",
                                   challenge="dce")
    assert resolved["repeat"] == "retest"


def test_unrelated_directories_are_not_treated_as_identity() -> None:
    """A conservative parser must not read 'processed' as a participant."""
    found = parse_directory_identity(
        ("processed", "results", "outputs", "Ktrans.nii.gz"), challenge="dce")
    assert found == {}


def test_unknown_dataset_name_is_not_coerced() -> None:
    """'phantom' is not configured for DCE and must not become synthetic."""
    resolved, _ = resolve_identity("phantom/Participant001/Ktrans.nii.gz",
                                   challenge="dce")
    assert resolved["dataset"] is None
    assert resolved["participant"] == "1"


def test_flat_legacy_submission_has_no_identity() -> None:
    resolved, conflicts = resolve_identity("Ktrans.nii.gz", challenge="dce")
    assert resolved == {"dataset": None, "participant": None,
                        "repeat": None, "site": None}
    assert conflicts == []


# ── Configured filename fallback ──────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Synthetic_P001_Visit1.nii.gz",
     {"dataset": "synthetic", "participant": "1", "repeat": "1"}),
    ("Clinical_P005_Visit2.nii",
     {"dataset": "clinical", "participant": "5", "repeat": "2"}),
    ("Synthetic_P001_Visit2_Site3.nii.gz",
     {"dataset": "synthetic", "participant": "1", "repeat": "2", "site": "3"}),
])
def test_configured_filename_patterns(name: str, expected: dict) -> None:
    assert parse_filename_identity(name, challenge="dce") == expected


def test_first_matching_pattern_wins() -> None:
    """The site-aware pattern is listed first and must take precedence."""
    parsed = parse_filename_identity("Synthetic_P001_Visit2_Site3.nii.gz",
                                     challenge="dce")
    assert parsed.get("site") == "3"


def test_challenge_pattern_does_not_leak_but_generic_tokens_still_parse() -> None:
    """DCE's dataset pattern must not leak into ASL; explicit ids remain useful."""
    assert parse_filename_identity("Synthetic_P001_Visit1.nii.gz", challenge="asl") == {
        "participant": "1",
        "repeat": "1",
    }


def test_filename_fills_only_gaps_left_by_directories() -> None:
    resolved, conflicts = resolve_identity(
        "Synthetic/Participant001/Synthetic_P001_Visit2_Site3.nii.gz",
        challenge="dce")
    assert resolved["participant"] == "1"     # from directory
    assert resolved["repeat"] == "2"          # from filename
    assert resolved["site"] == "3"            # from filename
    assert conflicts == []


# ── Conflicts ─────────────────────────────────────────────────────────────

def test_directory_identity_wins_over_conflicting_filename() -> None:
    resolved, conflicts = resolve_identity(
        "Synthetic/Participant001/Repeat1/Clinical_P002_Visit2.nii.gz",
        challenge="dce")
    assert resolved["dataset"] == "synthetic"
    assert resolved["participant"] == "1"
    assert resolved["repeat"] == "1"
    fields = {c.field for c in conflicts}
    assert fields == {"dataset", "participant", "repeat"}


def test_conflict_records_both_values() -> None:
    _, conflicts = resolve_identity(
        "Synthetic/Participant001/Repeat1/Clinical_P002_Visit2.nii.gz",
        challenge="dce")
    participant = next(c for c in conflicts if c.field == "participant")
    assert participant.directory_value == "1"
    assert participant.filename_value == "2"


def test_agreeing_sources_produce_no_diagnostic() -> None:
    _, conflicts = resolve_identity(
        "Synthetic/Participant001/Repeat1/Synthetic_P001_Visit1.nii.gz",
        challenge="dce")
    assert conflicts == []


# ── Map-type classification ───────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Ktrans.nii.gz", "ktrans"), ("vp.nii", "vp"),
    ("ve.nii.gz", "ve"), ("Kep.nii.gz", "kep"),
    ("sub-001_ktrans.nii.gz", "ktrans"), ("K_trans.nii.gz", "ktrans"),
])
def test_parameter_maps_are_detected(name: str, expected: str) -> None:
    assert detect_map_type(name) == expected


@pytest.mark.parametrize("name", [
    "curve.nii.gz",       # contains "ve"
    "archive.nii.gz",     # contains "ve"
    "developer.nii.gz",   # contains "ve" twice
    "vessel.nii.gz",      # starts with "ve"
    "developer_map.nii.gz",
    "average_signal.nii.gz",
])
def test_substring_lookalikes_do_not_match_a_map_type(name: str) -> None:
    """The legacy detector matches 've' inside 'curve'; this one must not."""
    assert detect_map_type(name) is None


def test_longest_pattern_wins_when_several_match() -> None:
    """'ktrans_ve.nii.gz' holds both tokens; resolution must be deterministic.

    Without a longest-first rule the answer would depend on dict ordering,
    which is exactly the kind of silent instability that makes a classifier
    untrustworthy.
    """
    assert detect_map_type("ktrans_ve.nii.gz") == "ktrans"
    assert detect_map_type("ve_ktrans.nii.gz") == "ktrans"


def test_vpcopy_txt_is_not_a_parameter_map() -> None:
    role, map_type, _ = classify("vpcopy.txt", is_nifti=False)
    assert role != "parameter_map"
    assert map_type is None


# ── Artifact classification ───────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "modelled_st.nii.gz", "modeled_st.nii.gz", "fitted_signal.nii.gz",
])
def test_modelled_signal_is_a_fitted_signal_not_a_map(name: str) -> None:
    role, map_type, artifact_type = classify(name, is_nifti=True)
    assert role == "fitted_signal"
    assert map_type is None
    assert artifact_type == "modelled_st"


@pytest.mark.parametrize("name", [
    "methods.docx", "methods.txt", "methodology.docx", "methodology.txt",
])
def test_methods_documents_are_recognized(name: str) -> None:
    role, map_type, artifact_type = classify(name)
    assert role == "methods"
    assert map_type is None
    assert artifact_type == "methods"


def test_pattern_and_suffix_must_both_agree() -> None:
    """A matching name with a non-configured suffix is not a methods doc."""
    role, _, _ = classify("methods.pdf")
    assert role != "methods"


def test_arbitrary_text_file_is_not_a_methods_document() -> None:
    role, _, _ = classify("notes.txt", is_metadata=False)
    assert role != "methods"


def test_readme_detection_keeps_priority() -> None:
    role, _, _ = classify("readme.txt", is_readme=True)
    assert role == "readme"


@pytest.mark.parametrize("name,flags,expected", [
    ("params.json", {"is_metadata": True}, "metadata"),
    ("run.py", {"is_code": True}, "code"),
    ("mystery.bin", {}, "unknown"),
])
def test_legacy_categories_still_classify(name, flags, expected) -> None:
    role, _, _ = classify(name, **flags)
    assert role == expected


@pytest.mark.parametrize("name,expected", [
    ("cbf.nii.gz", "cbf"), ("att.nii.gz", "att"),
    ("cbv.nii.gz", "cbv"), ("mtt.nii.gz", "mtt"),
])
def test_asl_and_dsc_map_types_still_classify(name, expected) -> None:
    assert detect_map_type(name) == expected


# ── NIfTI dimensions ──────────────────────────────────────────────────────

@pytest.mark.parametrize("filename,ndim", [
    ("Ktrans.nii.gz", 3), ("Ktrans.nii", 3),
    ("modelled_st.nii.gz", 4), ("modelled_st.nii", 4),
])
def test_header_dimensions_are_read(tmp_path: Path, filename: str, ndim: int) -> None:
    path = _write_nifti(tmp_path / filename, ndim)
    assert read_ndim(path) == ndim


@pytest.mark.parametrize("filename", ["Ktrans.nii", "Ktrans.nii.gz"])
def test_nifti2_header_dimensions_are_read(tmp_path: Path, filename: str) -> None:
    """The fast manifest probe must support both NIfTI-1 and NIfTI-2."""

    header = bytearray(544)
    struct.pack_into("<i", header, 0, 540)
    struct.pack_into("<q", header, 16, 3)
    header[4:12] = b"n+2\x00\r\n\x1a\n"
    path = tmp_path / filename
    path.write_bytes(gzip.compress(bytes(header)) if filename.endswith(".gz") else bytes(header))

    assert read_ndim(path) == 3


def test_bids_style_filename_identity_is_resolved_without_challenge_pattern() -> None:
    identity = parse_filename_identity(
        "sub-001_acq-002_Perfmap_32float.nii.gz", challenge="asl"
    )

    assert identity == {"participant": "1", "repeat": "2"}


def test_broken_nifti_returns_none_and_does_not_raise(tmp_path: Path) -> None:
    broken = tmp_path / "broken.nii.gz"
    broken.write_bytes(b"definitely not a nifti")
    assert read_ndim(broken) is None


def test_truncated_nifti_returns_none(tmp_path: Path) -> None:
    short = tmp_path / "short.nii"
    short.write_bytes(b"\x00" * 40)
    assert read_ndim(short) is None


def test_broken_nifti_does_not_break_manifest_building(tmp_path: Path) -> None:
    (tmp_path / "Ktrans.nii.gz").write_bytes(b"not a nifti at all")
    manifest = _manifest(tmp_path)
    artifact = _by_path(manifest, "Ktrans.nii.gz")
    assert artifact.dimensions is None
    assert artifact.map_type == "ktrans"


# ── End-to-end manifest ───────────────────────────────────────────────────

def test_full_synthetic_layout_is_normalized(tmp_path: Path) -> None:
    base = tmp_path / "Synthetic" / "Participant001" / "Site1" / "Repeat1"
    _write_nifti(base / "Ktrans.nii.gz", 3)
    _write_nifti(base / "vp.nii.gz", 3)
    _write_nifti(base / "modelled_st.nii.gz", 4)
    (tmp_path / "methods.docx").write_bytes(b"methods")

    manifest = _manifest(tmp_path)

    ktrans = _by_path(manifest, "Ktrans.nii.gz")
    assert (ktrans.role, ktrans.map_type, ktrans.dimensions) == ("parameter_map", "ktrans", 3)
    assert (ktrans.dataset, ktrans.participant, ktrans.site, ktrans.repeat) == \
        ("synthetic", "1", "1", "1")

    signal = _by_path(manifest, "modelled_st.nii.gz")
    assert (signal.role, signal.map_type, signal.dimensions) == ("fitted_signal", None, 4)

    methods = _by_path(manifest, "methods.docx")
    assert (methods.role, methods.dimensions) == ("methods", None)


def test_clinical_layout_has_no_site(tmp_path: Path) -> None:
    base = tmp_path / "Clinical" / "Participant005" / "Repeat2"
    _write_nifti(base / "Ktrans.nii.gz", 3)
    artifact = _by_path(_manifest(tmp_path), "Ktrans.nii.gz")
    assert artifact.site is None
    assert (artifact.dataset, artifact.participant, artifact.repeat) == ("clinical", "5", "2")


def test_duplicate_artifacts_are_both_preserved(tmp_path: Path) -> None:
    base = tmp_path / "Synthetic" / "Participant001" / "Site1" / "Repeat1"
    _write_nifti(base / "Ktrans.nii.gz", 3)
    _write_nifti(base / "Ktrans_copy.nii.gz", 3)

    manifest = _manifest(tmp_path)
    ktrans = [a for a in manifest.artifacts if a.map_type == "ktrans"]
    assert len(ktrans) == 2, "a duplicate must not be silently dropped"
    identities = {(a.dataset, a.participant, a.site, a.repeat) for a in ktrans}
    assert len(identities) == 1, "duplicates share one identity"


def test_conflicts_are_recorded_on_the_manifest(tmp_path: Path) -> None:
    base = tmp_path / "Synthetic" / "Participant001" / "Repeat1"
    _write_nifti(base / "Clinical_P002_Visit2.nii.gz", 3)
    manifest = _manifest(tmp_path)
    assert manifest.identity_conflicts
    assert {c.field for c in manifest.identity_conflicts} >= {"participant"}


def test_every_file_becomes_an_artifact(tmp_path: Path) -> None:
    _write_nifti(tmp_path / "Ktrans.nii.gz", 3)
    (tmp_path / "readme.md").write_text("hi")
    (tmp_path / "params.json").write_text("{}")
    (tmp_path / "mystery.bin").write_bytes(b"\x00")
    manifest = _manifest(tmp_path)
    assert len(manifest.artifacts) == manifest.file_count == 4


# ── Backward compatibility ────────────────────────────────────────────────

def test_legacy_manifest_fields_are_unchanged(tmp_path: Path) -> None:
    _write_nifti(tmp_path / "Ktrans.nii.gz", 3)
    (tmp_path / "readme.md").write_text("hi")
    (tmp_path / "params.json").write_text("{}")

    manifest = _manifest(tmp_path)
    assert manifest.nifti_files == ["Ktrans.nii.gz"]
    assert manifest.readme_files == ["readme.md"]
    assert manifest.metadata_files == ["params.json"]
    assert manifest.file_count == 3


def test_manifest_serializes_with_artifacts(tmp_path: Path) -> None:
    import json

    _write_nifti(tmp_path / "Ktrans.nii.gz", 3)
    payload = _manifest(tmp_path).to_dict()
    round_tripped = json.loads(json.dumps(payload, default=str))
    assert round_tripped["artifacts"][0]["map_type"] == "ktrans"
    # Legacy keys survive serialization untouched.
    for key in ("nifti_files", "metadata_files", "code_files",
                "docker_files", "readme_files", "files", "directories"):
        assert key in round_tripped


def test_flat_submission_yields_null_identity(tmp_path: Path) -> None:
    _write_nifti(tmp_path / "Ktrans.nii.gz", 3)
    artifact = _by_path(_manifest(tmp_path), "Ktrans.nii.gz")
    assert (artifact.dataset, artifact.participant,
            artifact.repeat, artifact.site) == (None, None, None, None)
    assert artifact.map_type == "ktrans"


@pytest.mark.parametrize("filename,expected", [
    ("curve.nii.gz", ""),        # 've' is a substring but not a token
    ("archive.nii.gz", ""),
    ("developer.nii.gz", ""),
    ("vessel.nii.gz", ""),
    ("ve.nii.gz", "ve"),
    ("vp.nii.gz", "vp"),
    ("Ktrans.nii.gz", "ktrans"),
])
def test_legacy_map_field_agrees_with_normalized_artifact(
    tmp_path: Path, filename: str, expected: str
) -> None:
    """The legacy manifest field and the artifact must not disagree.

    Required-map enforcement reads the artifact, but both fields are visible
    to consumers; two different answers for one file is how the substring bug
    would quietly survive.
    """
    _write_nifti(tmp_path / filename, 3)
    manifest = _manifest(tmp_path)
    entry = next(e for e in manifest.files if e["relative_path"] == filename)
    artifact = _by_path(manifest, filename)
    assert entry["detected_parameter_map_id"] == expected
    assert (artifact.map_type or "") == expected


def test_asl_submission_still_classifies(tmp_path: Path) -> None:
    _write_nifti(tmp_path / "cbf.nii.gz", 3)
    _write_nifti(tmp_path / "att.nii.gz", 3)
    manifest = _manifest(tmp_path, challenge="asl")
    assert {a.map_type for a in manifest.artifacts} == {"cbf", "att"}
    assert all(a.role == "parameter_map" for a in manifest.artifacts)
