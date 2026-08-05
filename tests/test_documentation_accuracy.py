"""The documentation site must describe the code that exists.

Documentation goes stale silently. A renamed config key, a removed issue code
or a changed Docker flag leaves the page looking authoritative while telling a
reader something untrue — and the reader has no way to tell. This project has
already shipped a documentation site that instructed people to edit four Python
constants that had been deleted, so the failure mode is not hypothetical.

Every check here reads the *real* source of truth — the loaded YAML schema, the
validation module, `docker-compose.yml` — and fails if the page disagrees.
These are cheap tests that catch an expensive class of mistake.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

PAGES = sorted(DOCS.glob("*.html"))
TEXT = "\n".join(p.read_text() for p in PAGES)


def code_spans(pattern: str) -> set[str]:
    """Everything the docs wrapped in <code> matching ``pattern``."""
    return set(re.findall(rf"<code>({pattern})</code>", TEXT))


# ── The pages exist at all ────────────────────────────────────────────────

def test_the_documentation_site_is_present() -> None:
    assert PAGES, "no documentation pages found"
    assert (DOCS / "index.html").exists()


# ── Configuration keys ────────────────────────────────────────────────────

def test_every_challenge_key_the_docs_name_is_accepted_by_the_schema() -> None:
    """Nothing that looks like a config key may be absent from the schema.

    Matching a fixed list of known keys would be useless here: renaming a key
    in the documentation would simply stop it matching, and the check would
    pass on a page that had become wrong. So this scans for anything shaped
    like a key and requires the schema to recognise it.
    """
    from osipi_pipeline.config import rules

    # snake_case identifiers, which is how every key in this project is
    # written, plus camelCase — the schema has none, so a camelCase key in the
    # documentation is always a mistake, and catching it closes the case where
    # only some occurrences of a key were renamed.
    # Keys are often written as a path — challenges.<id>.required_maps — so
    # each dotted segment is checked, not the span as a whole. Discarding the
    # whole span let a rename inside a path slip through unseen.
    candidates: set[str] = set()
    for span in code_spans(r"[a-zA-Z0-9_.&;<>-]+"):
        if "/" in span:
            continue
        for segment in span.split("."):
            segment = re.sub(r"&lt;.*?&gt;|<.*?>", "", segment).strip()
            if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+|[a-z]+[A-Z][A-Za-z]*",
                            segment):
                candidates.add(segment)
    known = (
        rules._CHALLENGE_KEYS
        | rules._ARTIFACT_TYPE_KEYS
        | rules._DATASET_KEYS
        | {"map_types", "artifact_types", "challenges", "run_config",
           "package_id", "challenge_type", "map_type", "entry_point",
           "call_mode", "timeout_seconds", "scoring_package",
           "private_path_parts", "structural_subdirs", "grouped_statistics"}
        # Issue codes are upper case; environment variables are handled
        # separately. Anything left must be a real configuration key.
    )
    unknown = candidates - known
    assert not unknown, (
        f"documented as configuration but unknown to the schema: {sorted(unknown)}")


def test_the_schema_keys_are_actually_documented() -> None:
    """The other direction: a key the docs stop naming has gone missing.

    Catches a rename in the documentation, which the check above cannot see
    because the renamed word simply stops resembling anything real.
    """
    # Substring, not an exact <code> match: keys are often written in context
    # as challenges.<id>.required_maps rather than alone.
    for key in ("required_maps", "optional_maps", "required_artifacts",
                "datasets", "filename_identity_patterns"):
        assert key in TEXT, f"the docs no longer name {key!r}"


def test_documented_top_level_blocks_exist_in_the_live_config() -> None:
    live = yaml.safe_load((ROOT / "config" / "validation_rules.yaml").read_text())
    for block in ("map_types", "artifact_types", "challenges"):
        if f"<code>{block}</code>" in TEXT:
            assert block in live, f"docs describe {block}, which the config lacks"


def test_documented_map_type_fields_exist_on_a_real_map() -> None:
    """`display`, `units`, `dimensions`, `patterns` must be real field names."""
    from osipi_pipeline.config.rules import map_type_specs

    ktrans = map_type_specs()["ktrans"]
    for field in ("display", "units", "dimensions", "patterns"):
        if f"<code>{field}</code>" in TEXT:
            assert field in ktrans, f"docs describe map field {field!r}, absent from config"


def test_documented_dataset_fields_are_the_real_ones() -> None:
    from osipi_pipeline.config.rules import datasets_by_challenge

    synthetic = datasets_by_challenge()["dce"]["synthetic"]
    for field in ("participants", "repeats", "sites"):
        assert field in synthetic
        assert field in TEXT, f"the docs no longer mention {field}"


# ── Issue codes ───────────────────────────────────────────────────────────

def test_every_issue_code_in_the_docs_is_raised_by_the_code() -> None:
    """A code that no longer exists would send a reader hunting for nothing."""
    documented = {c for c in code_spans(r"[A-Z][A-Z_]{5,}") if c.isupper()}
    # KNOWN_CHALLENGE_TYPES is discussed as a runtime value, not an issue code.
    documented -= {"KNOWN_CHALLENGE_TYPES", "PYTHONPATH", "HOST_SUBMISSIONS_DIR",
                   "HOST_OUTPUTS_DIR", "HOST_REFERENCE_DATA_DIR", "PWD"}
    assert documented, "the docs list no issue codes"

    source = "\n".join(
        p.read_text() for p in (ROOT / "src").rglob("*.py")
    ) + "\n".join(p.read_text() for p in (ROOT / "backend").rglob("*.py"))

    missing = [code for code in documented if code not in source]
    assert not missing, f"documented issue codes not raised anywhere: {sorted(missing)}"


def test_the_completeness_codes_are_all_documented() -> None:
    """The reverse direction: a new code should reach the documentation."""
    completeness = (ROOT / "src" / "osipi_pipeline" / "validation" / "completeness.py").read_text()
    raised = set(re.findall(r'code="([A-Z_]{6,})"', completeness))
    raised |= set(re.findall(r'"([A-Z_]{6,})"', completeness)) & {
        "REQUIRED_MAP_MISSING", "REQUIRED_ARTIFACT_MISSING", "MAP_DIMENSION_MISMATCH",
        "ARTIFACT_DIMENSION_MISMATCH", "DUPLICATE_PARAMETER_MAP",
        "DUPLICATE_REQUIRED_ARTIFACT", "DUPLICATE_METHODS_DOCUMENT",
        "INCOMPLETE_ARTIFACT_IDENTITY", "DATASET_COUNT_MISMATCH",
        "IDENTITY_CONFLICT", "UNKNOWN_DATASET",
    }
    undocumented = [code for code in raised if code not in TEXT]
    assert not undocumented, f"issue codes missing from the docs: {sorted(undocumented)}"


# ── Worked examples actually work ─────────────────────────────────────────

def _code_blocks(label: str) -> list[str]:
    """The <pre> contents of every code block carrying ``label``."""
    pattern = (rf"<span>{re.escape(label)}</span>.*?<pre><code>(.*?)</code></pre>")
    blocks = re.findall(pattern, TEXT, re.S)
    return [
        b.replace("&lt;", "<").replace("&gt;", ">")
         .replace("&amp;", "&").replace("&quot;", '"')
        for b in blocks
    ]


def test_every_yaml_example_parses() -> None:
    blocks = _code_blocks("config/validation_rules.yaml")
    assert blocks, "no YAML examples found in the documentation"
    for block in blocks:
        yaml.safe_load(block)   # raises if the example is malformed


def test_yaml_examples_use_keys_the_schema_accepts() -> None:
    """An example with an invented key would be copied into a real config."""
    from osipi_pipeline.config import rules

    for block in _code_blocks("config/validation_rules.yaml"):
        parsed = yaml.safe_load(block) or {}
        for challenge, spec in (parsed.get("challenges") or {}).items():
            unknown = set(spec or {}) - rules._CHALLENGE_KEYS
            assert not unknown, f"example challenge {challenge!r} uses {sorted(unknown)}"
        for map_id, spec in (parsed.get("map_types") or {}).items():
            unknown = set(spec or {}) - {"display", "label", "units",
                                         "dimensions", "patterns"}
            assert not unknown, f"example map {map_id!r} uses {sorted(unknown)}"


def test_the_manifest_example_carries_every_required_field() -> None:
    """A reader copying the example must end up with a package that installs."""
    blocks = _code_blocks("manifest.json")
    assert blocks, "no manifest example found"
    manifest = json.loads(blocks[0])

    service = (ROOT / "backend" / "services" / "scoring_package_service.py").read_text()
    required = set(re.findall(r'REQUIRED_FIELDS\s*=\s*\(([^)]*)\)', service))
    if required:
        names = set(re.findall(r'"(\w+)"', required.pop()))
        missing = names - set(manifest)
        assert not missing, f"manifest example is missing {sorted(missing)}"
    else:
        # Fall back to the fields the shipped demo package declares.
        demo = json.loads((ROOT / "data" / "sample_submissions" /
                           "demo_scoring_package" / "manifest.json").read_text())
        for key in ("package_id", "challenge_type", "entry_point", "metrics"):
            assert key in manifest, f"manifest example omits {key!r}"
            assert key in demo


# ── Docker instructions match the compose file ────────────────────────────

def test_documented_docker_run_matches_the_compose_service() -> None:
    """The two must not drift; a missing mount breaks the Run step silently."""
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    service = compose["services"]["osipi-backend"]

    run_blocks = _code_blocks("Run")
    assert run_blocks, "no docker run example found"
    command = run_blocks[0]

    for variable in service["environment"]:
        assert variable in command, f"docker run example omits {variable}"
    for volume in service["volumes"]:
        container_path = volume.split(":")[1]
        assert container_path in command, f"docker run example omits {container_path}"
    assert service["ports"][0] in command


def test_the_documented_port_is_the_one_the_app_serves() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "8000" in dockerfile
    assert "localhost:8000" in TEXT


# ── Cited paths exist ─────────────────────────────────────────────────────

def test_every_repository_path_the_docs_cite_exists() -> None:
    cited = code_spans(r"[\w./-]+\.(?:py|yaml|json|js)")
    # Paths inside examples refer to a user's submission, not this repository.
    cited -= {"run_config.json", "manifest.json", "scoring.py", "index.html"}
    missing = [c for c in cited if not (ROOT / c).exists()]
    assert not missing, f"documented paths that do not exist: {sorted(missing)}"


# ── Claims that were wrong once and must not return ───────────────────────

@pytest.mark.parametrize("phrase", [
    "EXPECTED_MAPS",
    "MAP_TYPE_PATTERNS",
    "PROVIDERS[",
    "configuration is code-based",
    "not a feature claimed",
    "roadmap recommendation",
])
def test_retired_claims_do_not_return(phrase: str) -> None:
    """These described constants that were deleted, or work already shipped.

    The published site once told mentors that runtime YAML configuration was a
    future recommendation, months after it shipped. Pinning the exact phrases
    keeps that particular regression from recurring.
    """
    assert phrase not in TEXT, f"the documentation reintroduced {phrase!r}"


def test_the_docs_still_explain_the_real_configuration_file() -> None:
    assert "validation_rules.yaml" in TEXT
    assert "manifest.json" in TEXT
    assert "Scoring Setup" in TEXT


def test_statistical_conventions_are_stated_accurately() -> None:
    """The docs must describe the SD and CoV the code actually computes."""
    from osipi_pipeline.scoring.descriptive_statistics import METHODOLOGY

    assert "ddof=0" in METHODOLOGY["standard_deviation"]
    assert "ddof=0" in TEXT, "the docs no longer state the SD convention"
    # The code divides by the absolute arithmetic mean; the docs must agree.
    assert "absolute" in METHODOLOGY["coefficient_of_variation"]
    assert re.search(r"SD\s*÷\s*\|mean\||absolute\s+arithmetic\s+mean", TEXT), \
        "the docs no longer state the CoV convention"


def test_the_scope_limit_is_stated() -> None:
    """ROI statistics must never be presented as repeatability."""
    assert "not repeatability" in TEXT or "not repeatability, reproducibility" in TEXT


# ── Structural integrity of the site ──────────────────────────────────────

def test_no_page_has_a_dangling_internal_anchor() -> None:
    for page in PAGES:
        html = page.read_text()
        ids = set(re.findall(r'\bid="([\w-]+)"', html))
        for anchor in re.findall(r'href="#([\w-]+)"', html):
            assert anchor in ids, f"{page.name}: #{anchor} has no target"


def test_no_page_has_duplicate_ids() -> None:
    for page in PAGES:
        found = re.findall(r'\bid="([\w-]+)"', page.read_text())
        duplicates = {i for i in found if found.count(i) > 1}
        assert not duplicates, f"{page.name}: duplicate ids {sorted(duplicates)}"


def test_cross_page_links_resolve_to_a_real_file_and_id() -> None:
    for page in PAGES:
        for target, anchor in re.findall(r'href="([\w-]+\.html)(?:#([\w-]+))?"',
                                         page.read_text()):
            other = DOCS / target
            assert other.exists(), f"{page.name} links to missing {target}"
            if anchor:
                assert f'id="{anchor}"' in other.read_text(), \
                    f"{page.name} links to {target}#{anchor}, which has no target"


def test_every_local_asset_exists() -> None:
    for page in PAGES:
        for asset in re.findall(r'(?:src|href)="(?!https?:|#|mailto:)([^"]+)"',
                                page.read_text()):
            path = DOCS / asset.split("#")[0]
            assert path.exists(), f"{page.name} references missing asset {asset}"
