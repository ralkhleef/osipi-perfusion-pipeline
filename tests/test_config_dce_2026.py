"""Schema tests for configured DCE, ASL, and DSC requirements."""

from __future__ import annotations

from pathlib import Path

import pytest

from osipi_pipeline.config import rules as config_rules

from tests.test_config_rules import (  # noqa: E402
    _assert_config_error,
    _base_rules,
    _load_temp_config,
)


def _dce_rules() -> dict:
    """Base config extended with the DCE-2026 sections."""
    rules = _base_rules()
    rules["map_types"]["ve"] = {
        "display": "ve",
        "label": "Extravascular extracellular volume fraction",
        "units": None,
        "dimensions": 3,
        "patterns": ["ve", "v_e"],
    }
    for map_id in ("ktrans", "kep", "vp"):
        rules["map_types"][map_id]["dimensions"] = 3
    rules["artifact_types"] = {
        "modelled_st": {
            "role": "fitted_signal",
            "dimensions": 4,
            "suffixes": [".nii", ".nii.gz"],
            "patterns": ["modelled_st", "modeled_st"],
        },
        "measured_st": {
            "role": "measured_signal",
            "dimensions": 4,
            "suffixes": [".nii", ".nii.gz"],
            "patterns": ["measured_st"],
        },
        "methods": {
            "role": "methods",
            "suffixes": [".docx", ".txt"],
            "patterns": ["methods", "methodology"],
        },
    }
    rules["challenges"]["dce"].update({
        "required_maps": ["ktrans"],
        "optional_maps": ["vp", "ve", "kep"],
        "required_artifacts": ["modelled_st", "methods"],
        "analysis": {
            "roi_descriptive": {
                "enabled": True,
                "map_types": ["ktrans"],
            },
            "signal_rss": {
                "enabled": True,
                "modelled_artifact": "modelled_st",
                "measured_artifact": "measured_st",
            },
        },
        "datasets": {
            "synthetic": {"participants": None, "repeats": 2, "sites": 3},
            "clinical": {"participants": 5, "repeats": 2, "sites": 1},
        },
    })
    return rules


def _loaded(tmp_path, monkeypatch, rules=None):
    """Load a config and return the accessors' view of it."""
    _load_temp_config(tmp_path, monkeypatch, rules or _dce_rules())
    monkeypatch.setattr(config_rules, "VALIDATION_RULES_PATH",
                        tmp_path / "validation_rules.yaml")
    monkeypatch.setattr(config_rules, "SETTINGS_PATH", tmp_path / "settings.yaml")
    config_rules.clear_config_cache()
    return config_rules


# ── Valid configuration ───────────────────────────────────────────────────

def test_required_and_optional_maps_load(tmp_path: Path, monkeypatch) -> None:
    cfg = _loaded(tmp_path, monkeypatch)
    assert cfg.required_maps_by_challenge()["dce"] == ("ktrans",)
    assert cfg.optional_maps_by_challenge()["dce"] == ("vp", "ve", "kep")


def test_ve_is_an_optional_dce_map(tmp_path: Path, monkeypatch) -> None:
    """ve already existed in map_types but was missing from DCE."""
    cfg = _loaded(tmp_path, monkeypatch)
    assert "ve" in cfg.optional_maps_by_challenge()["dce"]


def test_dce_parameter_maps_declare_three_dimensions(tmp_path: Path, monkeypatch) -> None:
    cfg = _loaded(tmp_path, monkeypatch)
    specs = cfg.map_type_specs()
    for map_id in ("ktrans", "vp", "ve", "kep"):
        assert specs[map_id]["dimensions"] == 3, map_id


def test_artifact_types_load_with_role_and_dimensions(tmp_path: Path, monkeypatch) -> None:
    cfg = _loaded(tmp_path, monkeypatch)
    artifacts = cfg.artifact_type_specs()
    assert artifacts["modelled_st"]["role"] == "fitted_signal"
    assert artifacts["modelled_st"]["dimensions"] == 4
    # A methods document has no dimensionality; the field stays absent.
    assert "dimensions" not in artifacts["methods"]
    assert ".docx" in artifacts["methods"]["suffixes"]


def test_artifact_specs_are_copies_not_cached_internals(tmp_path: Path, monkeypatch) -> None:
    """Mutating the returned specs must not corrupt the cached config."""
    cfg = _loaded(tmp_path, monkeypatch)
    cfg.artifact_type_specs()["modelled_st"]["role"] = "mutated"
    assert cfg.artifact_type_specs()["modelled_st"]["role"] == "fitted_signal"


def test_required_artifacts_resolve(tmp_path: Path, monkeypatch) -> None:
    cfg = _loaded(tmp_path, monkeypatch)
    assert cfg.required_artifacts_by_challenge()["dce"] == ("modelled_st", "methods")


def test_synthetic_participant_count_may_be_null(tmp_path: Path, monkeypatch) -> None:
    """OSIPI has not finalised N; null must survive as None, not become 0."""
    cfg = _loaded(tmp_path, monkeypatch)
    synthetic = cfg.datasets_by_challenge()["dce"]["synthetic"]
    assert synthetic["participants"] is None
    assert synthetic["repeats"] == 2
    assert synthetic["sites"] == 3


def test_clinical_dataset_counts_load(tmp_path: Path, monkeypatch) -> None:
    cfg = _loaded(tmp_path, monkeypatch)
    clinical = cfg.datasets_by_challenge()["dce"]["clinical"]
    assert clinical == {"participants": 5, "repeats": 2, "sites": 1}


def test_dataset_names_are_not_restricted_to_synthetic_and_clinical(
    tmp_path: Path, monkeypatch
) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["datasets"]["phantom"] = {
        "participants": 3, "repeats": 1, "sites": 1,
    }
    cfg = _loaded(tmp_path, monkeypatch, rules)
    assert "phantom" in cfg.datasets_by_challenge()["dce"]


# ── Backward compatibility ────────────────────────────────────────────────

def test_config_without_any_new_sections_still_loads(tmp_path: Path, monkeypatch) -> None:
    """An older config has no artifact_types and no new challenge fields."""
    cfg = _loaded(tmp_path, monkeypatch, _base_rules())
    assert cfg.artifact_type_specs() == {}
    assert cfg.required_maps_by_challenge()["dce"] == ()
    assert cfg.optional_maps_by_challenge()["dce"] == ()
    assert cfg.required_artifacts_by_challenge()["dce"] == ()
    assert cfg.datasets_by_challenge()["dce"] == {}


def test_expected_maps_is_unchanged_by_the_new_fields(tmp_path: Path, monkeypatch) -> None:
    """required_maps must not migrate, replace, or reorder expected_maps."""
    cfg = _loaded(tmp_path, monkeypatch)
    assert cfg.expected_maps_by_challenge()["dce"] == ("ktrans", "kep", "vp")


def test_dce_analysis_enablement_is_configuration_driven() -> None:
    from osipi_pipeline.config import rules as cfg

    cfg.clear_config_cache()
    analysis = cfg.analysis_by_challenge()["dce"]
    assert analysis["roi_descriptive"] == {
        "enabled": True,
        "map_types": ["ktrans"],
        "report_metrics": [
            "mean", "median", "standard_deviation", "range",
            "coefficient_of_variation",
        ],
    }
    assert analysis["signal_rss"] == {
        "enabled": True,
        "modelled_artifact": "modelled_st",
        "measured_artifact": "measured_st",
    }
    # ASL now enables the same 4-D fitted-model comparison as DCE: participants
    # submit what they fitted to obtain CBF and ATT, and it is compared against
    # the ground-truth 4-D ASL signal. The analysis was always generic; only
    # the configuration block is new.
    assert cfg.analysis_by_challenge()["asl"] == {
        "roi_descriptive": {
            "enabled": True,
            "map_types": ["cbf", "att"],
            "report_metrics": [
                "mean", "median", "standard_deviation", "range",
                "coefficient_of_variation",
            ],
        },
        "signal_rss": {
            "enabled": True,
            "modelled_artifact": "modelled_st",
            "measured_artifact": "measured_st",
        },
    }
    # DSC carries the same descriptive statistics. Mean, median, SD, range
    # and CoV inside a supplied mask need no definition from the challenge
    # leads, unlike accuracy, deviance or a pass threshold, so they are set
    # provisionally rather than leaving DSC reporting nothing.
    assert cfg.analysis_by_challenge()["dsc"] == {
        "roi_descriptive": {
            "enabled": True,
            "map_types": ["cbv", "cbf", "mtt"],
            "report_metrics": [
                "mean", "median", "standard_deviation", "range",
                "coefficient_of_variation",
            ],
        },
    }


def test_asl_and_dsc_declare_the_maps_they_exist_to_collect() -> None:
    """ASL and DSC declare their required parameter maps."""
    config_rules.clear_config_cache()

    required = config_rules.required_maps_by_challenge()
    assert set(required["asl"]) == {"cbf", "att"}
    assert set(required["dsc"]) == {"cbv", "cbf", "mtt"}

    # Nothing beyond the maps is mandatory until the challenge leads say so.
    artifacts = config_rules.required_artifacts_by_challenge()
    assert artifacts["asl"] == () and artifacts["dsc"] == ()


def test_asl_and_dsc_declare_no_dataset_grid() -> None:
    """Their layout has not been stated, so none is invented here.

    A dataset name is not inert. Ingestion reads these names to decide
    whether a top-level directory partitions one submission or separates two
    teams, so inventing a placeholder dataset would change how real archives
    are unpacked. The schema accepts null counts now, so a grid can be added
    as soon as one exists, without another schema change.
    """
    config_rules.clear_config_cache()
    grids = config_rules.datasets_by_challenge()
    assert grids["asl"] == {} and grids["dsc"] == {}


# ── Invalid configuration ─────────────────────────────────────────────────

def test_unknown_key_in_artifact_definition_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["artifact_types"]["methods"]["mystery"] = True
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "artifact_types.methods.mystery: unknown key")


def test_unknown_key_in_dataset_definition_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["datasets"]["clinical"]["scanners"] = 2
    _assert_config_error(tmp_path, monkeypatch, rules, "scanners: unknown key")


@pytest.mark.parametrize(
    ("analysis_name", "field"),
    [
        ("roi_descriptive", "map_types"),
        ("signal_rss", "modelled_artifact"),
        ("signal_rss", "measured_artifact"),
    ],
)
def test_enabled_analysis_requires_explicit_inputs(
    tmp_path: Path,
    monkeypatch,
    analysis_name: str,
    field: str,
) -> None:
    rules = _dce_rules()
    del rules["challenges"]["dce"]["analysis"][analysis_name][field]
    _assert_config_error(
        tmp_path,
        monkeypatch,
        rules,
        f"analysis.{analysis_name}.{field}: required when enabled is true",
    )


@pytest.mark.parametrize("analysis_name", ["roi_descriptive", "signal_rss"])
def test_disabled_analysis_may_omit_inputs(
    tmp_path: Path,
    monkeypatch,
    analysis_name: str,
) -> None:
    rules = _dce_rules()
    block = rules["challenges"]["dce"]["analysis"][analysis_name]
    block.clear()
    block["enabled"] = False
    loaded, _settings = _load_temp_config(tmp_path, monkeypatch, rules)
    assert loaded["challenges"]["dce"]["analysis"][analysis_name] == {"enabled": False}


def test_unknown_roi_report_metric_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["analysis"]["roi_descriptive"][
        "report_metrics"
    ] = ["mean", "invented_metric"]
    _assert_config_error(
        tmp_path, monkeypatch, rules,
        "report_metrics[1]: unknown ROI report metric 'invented_metric'",
    )


@pytest.mark.parametrize("dims", [0, -1, 1, 8])
def test_out_of_range_artifact_dimensions_are_rejected(
    tmp_path: Path, monkeypatch, dims: int
) -> None:
    rules = _dce_rules()
    rules["artifact_types"]["modelled_st"]["dimensions"] = dims
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "artifact_types.modelled_st.dimensions")


@pytest.mark.parametrize("dims", [0, -1])
def test_out_of_range_map_dimensions_are_rejected(
    tmp_path: Path, monkeypatch, dims: int
) -> None:
    rules = _dce_rules()
    rules["map_types"]["ktrans"]["dimensions"] = dims
    _assert_config_error(tmp_path, monkeypatch, rules, "map_types.ktrans.dimensions")


def test_non_string_artifact_suffix_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["artifact_types"]["methods"]["suffixes"] = [123]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "artifact_types.methods.suffixes")


def test_artifact_missing_required_field_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    del rules["artifact_types"]["methods"]["role"]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "artifact_types.methods.role: required field is missing")


def test_required_artifact_referencing_unknown_id_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["required_artifacts"] = ["modelled_st", "nonexistent"]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "unknown artifact id 'nonexistent'")


def test_required_map_referencing_unknown_id_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["required_maps"] = ["not_a_map"]
    _assert_config_error(tmp_path, monkeypatch, rules, "unknown map id 'not_a_map'")


def test_optional_map_referencing_unknown_id_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["optional_maps"] = ["not_a_map"]
    _assert_config_error(tmp_path, monkeypatch, rules, "unknown map id 'not_a_map'")


@pytest.mark.parametrize("field", ["participants", "repeats", "sites"])
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_dataset_counts_are_rejected(
    tmp_path: Path, monkeypatch, field: str, value: int
) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["datasets"]["clinical"][field] = value
    _assert_config_error(tmp_path, monkeypatch, rules,
                         f"datasets.clinical.{field}: must be greater than 0")


@pytest.mark.parametrize("field", ["participants", "repeats", "sites"])
def test_string_dataset_counts_are_rejected(
    tmp_path: Path, monkeypatch, field: str
) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["datasets"]["clinical"][field] = "five"
    _assert_config_error(tmp_path, monkeypatch, rules,
                         f"datasets.clinical.{field}: must be an integer")


@pytest.mark.parametrize("field", ["participants", "repeats", "sites"])
def test_any_count_may_be_left_undecided(tmp_path: Path, monkeypatch, field: str) -> None:
    """Null marks any unresolved dataset count without inventing a value."""
    rules = _dce_rules()
    rules["challenges"]["dce"]["datasets"]["clinical"][field] = None
    loaded, _settings = _load_temp_config(tmp_path, monkeypatch, rules)
    assert loaded["challenges"]["dce"]["datasets"]["clinical"][field] is None


@pytest.mark.parametrize("field", ["participants", "repeats", "sites"])
def test_an_omitted_count_is_still_rejected(tmp_path: Path, monkeypatch, field: str) -> None:
    """Undecided has to be written down. Leaving the key out is a mistake."""
    rules = _dce_rules()
    del rules["challenges"]["dce"]["datasets"]["clinical"][field]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         f"datasets.clinical.{field}: required field is missing")


def test_dataset_missing_a_required_count_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    del rules["challenges"]["dce"]["datasets"]["clinical"]["participants"]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "datasets.clinical.participants: required field is missing")


# Filename identity patterns

def test_identity_patterns_load_in_order(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = [
        r"^(?P<dataset>Synthetic)_P(?P<participant>\d+)_Visit(?P<repeat>\d+)_Site(?P<site>\d+)$",
        r"^(?P<dataset>Synthetic)_P(?P<participant>\d+)_Visit(?P<repeat>\d+)$",
    ]
    cfg = _loaded(tmp_path, monkeypatch, rules)
    patterns = cfg.filename_identity_patterns_by_challenge()["dce"]
    assert len(patterns) == 2
    assert "Site" in patterns[0], "declared order must be preserved"


def test_identity_patterns_are_not_lowercased(tmp_path: Path, monkeypatch) -> None:
    """Regexes are case-sensitive; lowercasing would change what they match."""
    cfg = _loaded(tmp_path, monkeypatch)
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = [
        r"^(?P<dataset>Synthetic)_P(?P<participant>\d+)$",
    ]
    cfg = _loaded(tmp_path, monkeypatch, rules)
    assert "Synthetic" in cfg.filename_identity_patterns_by_challenge()["dce"][0]


def test_challenge_without_identity_patterns_returns_empty(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _loaded(tmp_path, monkeypatch, _base_rules())
    assert cfg.filename_identity_patterns_by_challenge()["dce"] == ()


def test_invalid_regex_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = ["^(?P<dataset>["]
    _assert_config_error(tmp_path, monkeypatch, rules, "invalid regular expression")


def test_unknown_named_group_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = [
        r"^(?P<scanner>\d+)_(?P<participant>\d+)$",
    ]
    _assert_config_error(tmp_path, monkeypatch, rules, "unknown named group")


def test_pattern_without_any_identity_group_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = [r"^Synthetic_\d+$"]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "must capture at least one of")


def test_non_string_pattern_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = [123]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "filename_identity_patterns")


def test_empty_pattern_is_rejected(tmp_path: Path, monkeypatch) -> None:
    rules = _dce_rules()
    rules["challenges"]["dce"]["filename_identity_patterns"] = [""]
    _assert_config_error(tmp_path, monkeypatch, rules,
                         "filename_identity_patterns")


def test_repository_config_identity_patterns_compile() -> None:
    """The shipped DCE patterns must be usable, not merely well-formed."""
    import re

    config_rules.clear_config_cache()
    patterns = config_rules.filename_identity_patterns_by_challenge()["dce"]
    assert patterns, "DCE should ship identity patterns"
    compiled = [re.compile(p) for p in patterns]
    match = next(
        (m for m in (c.match("Synthetic_P001_Visit2_Site3") for c in compiled) if m),
        None,
    )
    assert match is not None
    assert match.group("site") == "3"


def test_duplicate_artifact_ids_are_rejected(tmp_path: Path, monkeypatch) -> None:
    """Normalisation-collision detection must cover the new section too."""
    rules = _dce_rules()
    rules["artifact_types"]["METHODS"] = dict(rules["artifact_types"]["methods"])
    _assert_config_error(tmp_path, monkeypatch, rules, "duplicate identifier")
