"""The documentation site must describe the code that exists.

Documentation goes stale silently. A renamed config key, a removed issue code
or a changed Docker flag leaves the page looking authoritative while telling a
reader something untrue, and the reader has no way to tell. This project has
already shipped a documentation site that instructed people to edit four Python
constants that had been deleted, so the failure mode is not hypothetical.

Every check here reads the *real* source of truth, the loaded YAML schema, the
validation module, `docker-compose.yml`, and fails if the page disagrees.
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

#: Everything in docs/ that GitHub Pages serves as readable text. Anything
#: deployed can be read by someone, so anything deployed has to be true.
PUBLISHED = sorted(p for p in DOCS.rglob("*")
                   if p.suffix in {".html", ".md"} and p.is_file())


def without_comments(html: str) -> str:
    """The markup a browser acts on, with HTML comments removed.

    Commented-out markup is not a reference. A worked example of a link left
    in a comment for whoever restores it later must not make the checks below
    demand that the file exists, or insist the page still links to it.
    """
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


#: Page text with commented-out markup removed. Use this for anything that
#: asks "does the site actually point at this?"; use TEXT for prose checks.
VISIBLE = "\n".join(without_comments(p.read_text()) for p in PAGES)


def code_spans(pattern: str) -> set[str]:
    """Everything the docs wrapped in <code> matching ``pattern``."""
    return set(re.findall(rf"<code>({pattern})</code>", TEXT))


# ── The pages exist at all ────────────────────────────────────────────────

def test_the_documentation_site_is_present() -> None:
    assert PAGES, "no documentation pages found"
    assert (DOCS / "index.html").exists()


def test_configuration_page_has_the_safe_update_handoff() -> None:
    html = (DOCS / "configuration.html").read_text(encoding="utf-8").lower()
    for phrase in (
        "1. changing challenge requirements",
        "2. adding or updating scoring",
        "3. adding private reference data",
        "4. safe updates",
        "save as new version",
        "previous active configuration unchanged",
        "official osipi challenge ranking is not currently configured",
    ):
        assert phrase in html, f"configuration handoff is missing: {phrase!r}"


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
    # written, plus camelCase, the schema has none, so a camelCase key in the
    # documentation is always a mistake, and catching it closes the case where
    # only some occurrences of a key were renamed.
    # Keys are often written as a path, challenges.<id>.required_maps, so
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
                | rules._ROI_DESCRIPTIVE_KEYS
            | set(rules.validation_rules().get("artifact_types", {}))
            | set(rules.validation_rules().get("map_types", {}))
            | set(rules.validation_rules().get("challenges", {}))
            | {"map_types", "artifact_types", "challenges", "run_config",
               "package_id", "package_version", "challenge_type", "map_type", "entry_point",
               "call_mode", "timeout_seconds", "scoring_package", "required_inputs",
               "required_assets", "requirements_file",
               "private_path_parts", "structural_subdirs", "grouped_statistics"}
        # `osipi_cwd` is a *value* of call_mode, not a key, and only looks like
        # one because it is snake_case. It is pinned against the code that
        # implements it in test_the_documented_call_modes_are_implemented.
            | {"osipi_cwd", "dce_accuracy_v1_0", "dce_accuracy_v1_1"}
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
    cited -= {
        "run_config.json", "manifest.json", "metrics.json", "results.json",
        "scoring.py", "index.html",
    }
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

    Checked across everything in ``docs/``, not only the HTML. Restricting it
    to the pages is how a narration script describing a ``PROVIDERS`` registry
    that no longer exists stayed published: it was in the deployed directory,
    it was linked from a page, and no check ever read it.
    """
    for path in PUBLISHED:
        assert phrase not in path.read_text(), \
            f"{path.name} reintroduced {phrase!r}"


# ── Nothing published may be a stand-in for work not yet done ─────────────

def test_no_page_embeds_media_that_is_not_in_the_repository() -> None:
    """A <video> whose file is absent renders as a dead player, not as a gap.

    The site shipped a walkthrough recording that taught a scoring registry
    the code does not have. Deleting the file is not enough on its own, the
    element referring to it has to go with it.
    """
    for page in PAGES:
        html = page.read_text()
        for source in re.findall(r'<(?:video|iframe|source|audio)[^>]*src="([^"]+)"',
                                 html):
            if source.startswith(("http://", "https://")):
                continue
            assert (DOCS / source).exists(), \
                f"{page.name} embeds {source}, which is not in the repository"


def test_the_example_report_is_a_real_one_the_code_produced() -> None:
    """The published example must be output, not a hand-written mock-up.

    A mocked-up report is the most convincing kind of wrong thing to publish:
    it looks exactly like the real deliverable and is under no obligation to
    match it. Pinning structure the generator emits means a hand-edited stand-in
    would have to reproduce the generator to pass, at which point it is real.
    """
    pdf = (DOCS / "downloads" / "example-report-blinded.pdf").read_bytes()
    assert pdf.startswith(b"%PDF"), "the example report is not a PDF"

    # The generator writes uncompressed pages precisely so page text can be
    # read back like this. If that ever changes, this check would pass
    # vacuously, so the marker assertions have to fail first and loudly.
    text = pdf.decode("latin-1", errors="ignore")
    # Structure the generator emits, plus the blinding label. The report used
    # to carry the sentence "Team and contact details were withheld"; the
    # redesign replaced it with the "Blinded report" header, so the label is
    # what to pin now.
    for marker in ("Submission review report", "Submission 1", "Blinded report"):
        assert marker in text, (
            f"the example report lacks {marker!r}; is pageCompression enabled?")
    for leaked in ("Team Gamma", "gamma@example.org", "DCE_Test_Clean"):
        assert leaked not in text, f"the published example leaks {leaked!r}"


def test_the_published_example_pdf_is_portrait_throughout() -> None:
    """The file offered for download is the one people judge the tool by."""
    pdf = (DOCS / "downloads" / "example-report-blinded.pdf").read_bytes()
    boxes = re.findall(rb"/MediaBox\s*\[\s*([\d.\s-]+?)\]", pdf)
    assert boxes, "no page boxes found in the example report"
    sizes = set()
    for box in boxes:
        x0, y0, x1, y1 = (float(v) for v in box.split())
        sizes.add((round(x1 - x0), round(y1 - y0)))
    assert len(sizes) == 1, f"the published example has mixed page sizes: {sorted(sizes)}"
    width, height = sizes.pop()
    assert height > width, f"the published example is landscape ({width} x {height})"


def test_the_published_example_csv_matches_what_the_page_claims() -> None:
    """The page says 32 rows, one per scan and region. It should be true."""
    csv_path = DOCS / "downloads" / "example-roi-statistics.csv"
    lines = [line for line in csv_path.read_text().splitlines() if line.strip()]
    header, rows = lines[0], lines[1:]

    for column in ("roi_median", "roi_within_scan_sd", "roi_within_scan_cov",
                   "voxel_count", "units", "status"):
        assert column in header, f"the example CSV has no {column} column"
    assert len(rows) == 32, f"the example CSV has {len(rows)} rows, the page says 32"
    assert "32 rows" in (DOCS / "examples.html").read_text()

    for leaked in ("Team Gamma", "gamma@example.org"):
        assert leaked not in csv_path.read_text(), f"the example CSV leaks {leaked!r}"


def _offered_downloads() -> list[str]:
    """Local files any page offers for download. Remote links are not ours."""
    found: list[str] = []
    for page in PAGES:
        found += [
            target for target in
            re.findall(r'class="docs-download" href="([^"]+)"',
                       without_comments(page.read_text()))
            if not target.startswith(("http://", "https://"))
        ]
    return found


def test_every_download_the_page_offers_exists_and_is_not_empty() -> None:
    offered = _offered_downloads()
    assert offered, "no downloads are offered anywhere on the site"
    for target in offered:
        path = DOCS / target
        assert path.exists(), f"offered for download but missing: {target}"
        assert path.stat().st_size > 1024, f"offered download is empty: {target}"


def test_no_offered_download_is_excluded_from_version_control() -> None:
    """Existing locally is not the same as being published.

    A .gitignore rule of `downloads/`, unanchored, from the standard Python
    template, meant for the pip cache, matched docs/downloads/ as well. The
    files sat on disk, the page linked to them, every other check passed, and
    they would have 404ed on the deployed site because they were never
    committed. Nothing but git can answer this question.
    """
    import shutil
    import subprocess

    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    targets = [str((DOCS / t).relative_to(ROOT)) for t in _offered_downloads()]
    assert targets
    result = subprocess.run(["git", "check-ignore", *targets],
                            cwd=ROOT, capture_output=True, text=True)
    ignored = [line for line in result.stdout.splitlines() if line.strip()]
    assert not ignored, f"offered for download but gitignored: {ignored}"


def test_each_screenshot_slot_names_the_file_that_replaces_it() -> None:
    """The slots are temporary. Each has to say what fills it.

    Without the filename recorded next to the slot, adding the screenshots
    later becomes guesswork about where each one goes.
    """
    page = (DOCS / "examples.html").read_text()
    figures = re.findall(r"<figure class=\"docs-screen\">(.*?)</figure>", page, re.S)
    assert figures, "no interface slots or images found"
    for figure in figures:
        if "<img" in figure:
            continue  # a real screenshot has replaced the slot
        assert re.search(r"<!--\s*assets/images/screens/[\w-]+\.png\s*-->", figure), \
            "an empty slot does not name the file that replaces it"
        assert "<figcaption>" in figure, "an empty slot has no caption"


def test_no_unreferenced_file_is_deployed() -> None:
    """Everything under docs/assets and docs/downloads must be linked.

    Removing the element that embedded a file is not the same as removing the
    file. Left behind, it is still published, still reachable at its URL, and
    invisible to every other check here because nothing links to it. That is
    how a walkthrough recording describing a scoring registry the code does
    not have stayed deployed after its section was deleted.

    Downloads are covered as well as media. An unpublished document taken off
    a page leaves a PDF nobody links to, which is the same failure wearing a
    different extension.
    """
    watched = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
               ".mp4", ".webm", ".mov", ".mp3", ".ico",
               ".pdf", ".csv", ".json", ".zip"}
    orphans = []
    for directory in (DOCS / "assets", DOCS / "downloads"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(DOCS).as_posix()
            if path.is_file() and path.suffix.lower() in watched and relative not in VISIBLE:
                orphans.append(relative)
    assert not orphans, f"deployed but referenced by no page: {orphans}"


def test_public_docs_images_require_explicit_release_review() -> None:
    """Do not publish derived submission/reference images by copying them into docs.

    GitHub Pages deploys the entire ``docs`` directory. Local submissions,
    reference maps, masks, and generated previews are intentionally ignored by
    Git, but copying one of their rendered PNGs into ``docs/assets/images``
    bypasses those protections. Keep the small public-image allowlist explicit
    so adding any new raster asset requires a deliberate review here.
    """
    approved = {
        "favicon.ico",
        "osipi-logo.png",
        "osipi-mark.png",
        "screens/workflow-export.png",
        "screens/workflow-qc-preview.png",
        "screens/workflow-review.png",
        "screens/workflow-run.png",
        "screens/workflow-upload.png",
        "screens/workflow-validate.png",
    }
    image_root = DOCS / "assets" / "images"
    published = {
        path.relative_to(image_root).as_posix()
        for path in image_root.rglob("*")
        if path.is_file()
    }
    assert published == approved, (
        "docs/assets/images changed without public-release review: "
        f"added={sorted(published - approved)}, removed={sorted(approved - published)}"
    )


def test_public_docs_contain_no_raw_medical_or_submission_archives() -> None:
    forbidden = {".nii", ".dcm", ".dicom", ".zip", ".tar", ".tgz"}
    leaked = []
    for path in DOCS.rglob("*"):
        if not path.is_file():
            continue
        lower = path.name.lower()
        if lower.endswith(".nii.gz") or path.suffix.lower() in forbidden:
            leaked.append(path.relative_to(DOCS).as_posix())
    assert not leaked, f"private/raw assets must not be published by GitHub Pages: {leaked}"


@pytest.mark.parametrize("phrase", [
    "Suggested file:",
    "Planned recordings",
    "coming soon",
    "Coming soon",
    "TODO",
    "to be recorded",
])
def test_no_placeholder_scaffolding_is_published(phrase: str) -> None:
    """Notes-to-self are for a branch, not for a published site.

    A reader cannot tell a plan from a description. A grid of recordings that
    do not exist reads, to a mentor, as a site that is broken.
    """
    for path in PUBLISHED:
        assert phrase not in path.read_text(), \
            f"{path.name} publishes the placeholder {phrase!r}"


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

def _nav_pages() -> dict[str, list[tuple[str, str]]]:
    """The PAGES table from nav.js, as {page file: [(anchor, label)]}.

    Parsed rather than executed: the sidebar is the one part of the site
    whose links live in JavaScript, so nothing else here can see them.
    """
    source = (DOCS / "assets" / "scripts" / "nav.js").read_text()
    pages: dict[str, list[tuple[str, str]]] = {}
    for block in re.findall(r"\{\s*id:.*?\n    \}", source, re.S):
        file_match = re.search(r'file:\s*"([^"]+)"', block)
        if not file_match:
            continue
        pages[file_match.group(1)] = re.findall(r'\["#([\w-]+)",\s*"([^"]+)"\]', block)
    return pages


def test_every_sidebar_link_points_at_a_heading_that_exists() -> None:
    """The sidebar is generated, so a renamed heading breaks it silently.

    Three links on the Configuration page pointed at sections that had been
    renamed. Clicking them did nothing, which is the kind of thing that makes
    a documentation site feel broken before a reader has read a word of it.

    The dangling-anchor check below cannot catch this: it reads hrefs written
    into the HTML, and these are written by nav.js at load.
    """
    pages = _nav_pages()
    assert pages, "no PAGES table found in nav.js"

    for filename, links in pages.items():
        page = DOCS / filename
        assert page.exists(), f"nav.js lists {filename}, which does not exist"
        ids = set(re.findall(r'\bid="([\w-]+)"', page.read_text()))
        dead = [anchor for anchor, _ in links if anchor not in ids]
        assert not dead, f"{filename}: sidebar links to missing sections {dead}"


def test_every_sidebar_label_matches_the_heading_it_points_at() -> None:
    """A sidebar reading one thing and the heading another is its own confusion."""
    for filename, links in _nav_pages().items():
        html = (DOCS / filename).read_text()
        titles = dict(re.findall(r'<h2 id="([\w-]+)"[^>]*data-toc-title="([^"]+)"', html))
        for anchor, label in links:
            if anchor in titles:
                assert titles[anchor] == label, (
                    f"{filename}#{anchor}: sidebar says {label!r}, "
                    f"the heading says {titles[anchor]!r}")


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
                                without_comments(page.read_text())):
            path = DOCS / asset.split("#")[0]
            assert path.exists(), f"{page.name} references missing asset {asset}"


# ── The getting-started instructions are runnable ─────────────────────────
#
# Someone arriving at this repository for the first time follows the Install
# page literally. A command naming a file that was renamed or deleted wastes
# their time and, worse, makes them doubt the rest of the page. These checks
# resolve every path the instructions tell a newcomer to type.

def _documented_paths(label: str) -> set[str]:
    """Repository-relative paths appearing in the code block ``label``."""
    found: set[str] = set()
    for block in _code_blocks(label):
        found |= set(re.findall(r"(?<![\w/.-])((?:[\w-]+/)*[\w-]+\.(?:py|js|txt))",
                                block))
    return found


def test_the_documented_call_modes_are_implemented() -> None:
    """Both scoring call modes must be branches the service actually takes."""
    service = (ROOT / "backend" / "services" /
               "scoring_package_service.py").read_text()
    for mode in ("standard", "osipi_cwd"):
        assert f"<code>{mode}</code>" in TEXT, f"the docs no longer name {mode}"
        assert f'"{mode}"' in service, f"docs describe call_mode {mode!r}, absent from the code"


def test_the_documented_pip_targets_exist() -> None:
    """The one install step a newcomer runs must not name a missing file."""
    documented = _documented_paths("Python tests")
    assert documented, "no Python test instructions found"
    for path in documented:
        assert (ROOT / path).exists(), f"the docs pip-install missing {path}"


def test_the_documented_test_and_script_files_exist() -> None:
    for label in ("Frontend tests", "End-to-end demo"):
        documented = _documented_paths(label)
        assert documented, f"no commands found under {label!r}"
        for path in documented:
            assert (ROOT / path).exists(), f"{label} runs missing {path}"


def test_the_documented_frontend_suites_are_all_of_them() -> None:
    """A new suite nobody is told to run is a suite nobody runs."""
    on_disk = {f"tests/{p.name}" for p in (ROOT / "tests").glob("*_test.js")}
    documented = _documented_paths("Frontend tests")
    assert not on_disk - documented, \
        f"frontend suites the docs never mention: {sorted(on_disk - documented)}"


def test_the_documented_project_layout_is_real() -> None:
    """Every directory the layout table names must exist in a fresh clone."""
    for directory in re.findall(r"<td><code>([\w/]+)/</code></td>", TEXT):
        assert (ROOT / directory).is_dir(), \
            f"the layout table names {directory}/, which does not exist"


def test_the_documented_health_check_hits_a_real_endpoint() -> None:
    main = (ROOT / "backend" / "main.py").read_text()
    for path in re.findall(r"curl [^<\n]*localhost:8000(/[\w/-]+)", TEXT):
        assert f'"{path}"' in main, f"the docs curl {path}, which is not served"


def test_the_config_mount_the_instructions_depend_on_is_present() -> None:
    """The docs promise a rules edit needs only the Reload button.

    That is only true while the compose file mounts config/. Without the
    mount the container reads the copy baked into the image, so Reload
    re-reads the same stale file and the edit appears to do nothing, which
    is exactly the confusion the section exists to prevent.
    """
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    mounts = compose["services"]["osipi-backend"]["volumes"]
    assert "./config:/app/config" in mounts, \
        "compose no longer mounts writable config/, so the Configuration Manager cannot activate a version"
    assert "Reload rules" in TEXT, "the docs no longer name the button"


def test_the_reload_button_the_docs_promise_actually_exists() -> None:
    """Documentation is not allowed to invent an interface."""
    app_js = (ROOT / "frontend" / "app.js").read_text()
    index = (ROOT / "frontend" / "index.html").read_text()
    main = (ROOT / "backend" / "main.py").read_text()

    assert "Reload rules" in index, "the button the docs describe is not in the interface"
    assert "/api/config/reload" in app_js and '"/api/config/reload"' in main, \
        "the button is not wired to an endpoint"


def test_the_osipi_link_points_at_the_real_organisation() -> None:
    """OSIPI is at osipi.ismrm.org, not osipi.github.io.

    Every page footer and the shared navbar carry this link, so getting it
    wrong is wrong in seven places at once.
    """
    sources = PAGES + [DOCS / "assets" / "scripts" / "nav.js"]
    for path in sources:
        text = path.read_text()
        assert "osipi.github.io" not in text, f"{path.name} links to the wrong OSIPI site"
    assert "https://osipi.ismrm.org/" in TEXT, "the site no longer links to OSIPI"


def test_the_clone_url_is_the_repository_the_site_links_to() -> None:
    (clone,) = re.findall(r"git clone (\S+)", TEXT)
    linked = set(re.findall(r'href="(https://github\.com/[\w-]+/[\w-]+)"', TEXT))
    assert clone.removesuffix(".git") in linked, \
        f"the clone URL {clone} is not the repository the site links to"


# ── Hand-written components ───────────────────────────────────────────────

def test_numbered_steps_run_in_order_and_are_complete() -> None:
    """The step numbers are typed by hand, so they can silently repeat.

    A stepper reading 1, 2, 3, 3, 5, 6 looks fine at a glance and is wrong.
    The last step also has to be the one without a connector line, or the
    rail hangs past the final circle.
    """
    for page in PAGES:
        html = page.read_text()
        blocks = re.findall(r'<div class="docs-steps">(.*?)\n\s*</div>\s*$',
                            html, re.S | re.M)
        for block in re.findall(r'<div class="docs-steps">(.*?)</div>\s*(?:</section>|<p)',
                                html, re.S):
            numbers = [int(n) for n in
                       re.findall(r'<span class="docs-step-num">(\d+)</span>', block)]
            if not numbers:
                continue
            assert numbers == list(range(1, len(numbers) + 1)), \
                f"{page.name}: step numbers are {numbers}"

            steps = re.findall(r'<div class="docs-step">(.*?)</div>\s*</div>', block, re.S)
            assert len(steps) == len(numbers), \
                f"{page.name}: {len(numbers)} numbers but {len(steps)} steps"
            for step in steps:
                assert "docs-step-title" in step, f"{page.name}: a step has no title"

            lines = block.count('class="docs-step-line"')
            assert lines == len(numbers) - 1, (
                f"{page.name}: {len(numbers)} steps need {len(numbers) - 1} "
                f"connectors, found {lines}")
        assert blocks or True


def test_every_component_class_the_pages_use_is_styled() -> None:
    """A typo in a class name renders as unstyled markup, not as an error.

    Both sides need a word boundary. Substring matching says `.docs-step-num`
    is present when the stylesheet only defines `.docs-step-numbers`, and says
    a page uses `docs-step` when it only ever writes `docs-steps`.
    """
    css = (DOCS / "assets" / "stylesheets" / "site.css").read_text()

    used: set[str] = set()
    for attribute in re.findall(r'class="([^"]+)"', TEXT):
        used.update(attribute.split())

    for name in sorted(n for n in used if n.startswith("docs-")):
        assert re.search(rf"\.{re.escape(name)}(?![\w-])", css), \
            f"{name} is used by a page but has no style rule"


# ── Branding assets ───────────────────────────────────────────────────────

IMAGES = DOCS / "assets" / "images"


def _png_header(path: Path) -> tuple[int, int, int]:
    """(width, height, colour type) from the IHDR chunk.

    Read by hand rather than with Pillow: this is the only thing the tests
    need from an image, and it must not add a dependency to the suite.
    """
    import struct

    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height, data[25]


def test_the_mark_is_square_and_transparent() -> None:
    """A wordmark-shaped or white-backed icon looks wrong in a browser tab."""
    width, height, colour_type = _png_header(IMAGES / "osipi-mark.png")
    assert width == height, f"the mark is {width}x{height}, not square"
    assert colour_type == 6, "the mark has no alpha channel"


def test_the_wordmark_keeps_its_alpha_too() -> None:
    _, _, colour_type = _png_header(IMAGES / "osipi-logo.png")
    assert colour_type == 6, "the wordmark lost its transparency"


def test_every_page_uses_the_mark_as_its_icon_and_the_wordmark_as_its_brand() -> None:
    """The two are not interchangeable, and the site once used one for both.

    The wordmark reduced to a 16 px favicon is an illegible smear; the mark
    alone in the navbar drops the organisation's name from every page.
    """
    for page in PAGES:
        html = page.read_text()
        assert 'rel="icon" href="assets/images/favicon.ico"' in html, \
            f"{page.name} does not use the mark as its icon"
        assert 'src="assets/images/osipi-logo.png"' in html, \
            f"{page.name} lost the wordmark from its navbar"
        assert 'rel="icon" href="assets/images/osipi-logo.png"' not in html, \
            f"{page.name} reverted to the wordmark as a favicon"


def test_the_favicon_carries_the_small_sizes_that_matter() -> None:
    """Saving an ICO from an already-small image silently yields one entry."""
    data = (IMAGES / "favicon.ico").read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00", "favicon.ico is not an ICO"
    count = int.from_bytes(data[4:6], "little")
    # Each directory entry is 16 bytes; width 0 in an entry means 256.
    widths = {data[6 + 16 * i] or 256 for i in range(count)}
    assert {16, 32}<= widths, f"favicon.ico only carries {sorted(widths)}"


def test_the_documented_config_check_actually_runs() -> None:
    """Run the snippet the docs give for checking a config edit."""
    import subprocess
    import sys

    (block,) = _code_blocks("Check")
    snippet = re.sub(r'^\s*PYTHONPATH=src python3 -c "', "", block).rstrip('"\n ')
    result = subprocess.run(
        [sys.executable, "-c", snippet], cwd=ROOT, capture_output=True, text=True,
        env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"the documented check fails:\n{result.stderr}"
    assert "ktrans" in result.stdout


# ── Repository layout ─────────────────────────────────────────────────────
#
# docs/ is deployed by GitHub Pages and nothing else is. It used to hold both
# the published site and five maintainer notes, which meant prose written for
# maintainers was quietly being served to the public, and a relative link from
# a published page to one of those notes only worked by accident of them
# sharing a directory. The notes now live in notes/. These two tests keep the
# split honest and keep every link resolving.

def _tracked() -> list[str]:
    import subprocess
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return out.stdout.split()


def test_docs_holds_the_published_site_and_nothing_else() -> None:
    """Maintainer prose belongs in notes/, not in the deployed folder."""
    stray = sorted(p.relative_to(ROOT).as_posix() for p in DOCS.rglob("*.md"))
    assert not stray, (
        f"markdown in the deployed site folder: {stray}. "
        "Maintainer notes belong in notes/, which Pages does not serve."
    )


def test_every_relative_link_resolves() -> None:
    """A moved file with a stale link is worse than no link at all.

    Covers both directions that broke when the notes were moved: markdown
    links between documents, and hrefs from a published page. A published
    page cannot reach notes/ with a relative link, because Pages serves only
    docs/, so those have to be absolute repository URLs and are checked
    separately by ``test_every_repository_link_points_at_a_real_file``.
    """
    tracked = set(_tracked())
    broken: list[str] = []

    def check(source: Path, target: str) -> None:
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return
        resolved = (source.parent / target.split("#")[0]).resolve()
        try:
            relative = resolved.relative_to(ROOT).as_posix()
        except ValueError:
            broken.append(f"{source.relative_to(ROOT)} -> {target} (escapes the repository)")
            return
        # A link may legitimately point at a directory. git ls-files lists
        # files only, so a directory has to be matched by prefix.
        if relative in tracked:
            return
        if any(entry.startswith(f"{relative}/") for entry in tracked):
            return
        broken.append(f"{source.relative_to(ROOT)} -> {target}")

    for name in tracked:
        path = ROOT / name
        if name.endswith(".md"):
            for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
                check(path, target)
        elif name.startswith("docs/") and name.endswith(".html"):
            for target in re.findall(r'href="([^"]+)"', without_comments(path.read_text())):
                check(path, target)

    assert not broken, "links pointing at files that do not exist:\n  " + "\n  ".join(broken)


def test_every_repository_link_points_at_a_real_file() -> None:
    """Absolute blob links break silently: the page renders, the link 404s."""
    tracked = set(_tracked())
    missing = sorted({
        target for page in PAGES
        for target in re.findall(r"blob/main/([A-Za-z0-9_./-]+)", page.read_text())
        if target not in tracked
    })
    assert not missing, f"pages link to repository files that do not exist: {missing}"
