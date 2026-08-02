# Phase 5 (Presentation) — GSoC Work Product Website

Build a public, static work-product website for the OSIPI Perfusion Submission
Pipeline. This is a presentation phase. It does not change the pipeline.

---

## Hosting Requirements

This website is not intended to replace the local application.

The perfusion submission pipeline is designed to run locally because it processes
NIfTI imaging data, reference masks, and potentially Docker-based execution.

The public website should therefore serve as a GSoC work-product website and
project showcase, not a cloud-hosted processing service.

The website must be completely static and compatible with GitHub Pages.

Do not redesign the pipeline into a hosted SaaS application.
Do not create a public upload service for NIfTI files.
Do not require a running FastAPI backend for visitors.

The website should instead:

* Present the project professionally.
* Explain the problem and solution.
* Demonstrate the workflow.
* Show screenshots.
* Embed or link a demo video.
* Display sample validation, scoring, ROI statistics, HTML reports, PDF reports,
  and exports using pre-generated example data.
* Link to the GitHub repository, documentation, releases, and installation guide.
* Explain that the complete pipeline runs locally after installation.

If interactive functionality is included, it must operate entirely from static
example JSON data bundled with the website.

No server-side computation should occur on GitHub Pages.

---

## Scope

Build a single-page (or lightly multi-section) static site that a GSoC reviewer
can open cold and, within a few minutes, understand: what problem OSIPI has,
what the pipeline does, how it works, that it demonstrably works, and how to run
it themselves.

### Required sections

1. **Header / hero** — project title, one-sentence description, the OSIPI
   lockup (reuse `frontend/assets/logo-lockup.png`), and primary links:
   GitHub repository, installation guide, demo video, reports.
2. **Problem statement** — why OSIPI challenge submissions need automated,
   reproducible validation and scoring. Grounded in the actual challenge
   structure (DCE-2026 datasets, participants, repeats, sites), not generic
   filler.
3. **Solution overview** — the pipeline's stages: ingestion → identity
   resolution → validation → completeness → execution → scoring → ROI
   descriptive statistics → reports and exports.
4. **Architecture diagram** — inline SVG or a static image. No diagram
   libraries pulled from a CDN.
5. **Workflow walkthrough** — the wizard steps a user actually goes through,
   with screenshots.
6. **Live examples from static data** — validation issues, scoring metrics,
   ROI Ktrans statistics table, rendered from bundled JSON.
7. **Reports and exports** — a linked example HTML report, an example PDF
   report, and example CSV exports.
8. **Demo video** — embedded, or linked if the file is too large to commit.
9. **Installation and local use** — the exact commands, plus a clear statement
   that the full pipeline runs locally, not on this page.
10. **Project status and future work** — implemented vs. not yet implemented,
    stated honestly (see "Honesty requirements").

### Content sourcing

Take the technical content from the repository, not from assumption:

* `README.md`, `docs/setupinstruct.md`, `docs/PROJECT_STRUCTURE.md`,
  `docs/configuration.md`, `docs/ingestion_notes.md`,
  `docs/validation_notes.md`, `docs/execution_notes.md`
* `config/validation_rules.yaml` for the real challenge/dataset structure
* `src/osipi_pipeline/scoring/descriptive_statistics.py` for the exact
  statistical definitions and the `METHODOLOGY` text
* `backend/services/report_branding.py` for the palette and typography, so the
  website matches the reports

### Example data

Generate the bundled example JSON from the real code path — do not hand-write
plausible-looking numbers. Add a script (e.g. `scripts/build_site_examples.py`)
that runs the existing pipeline over a small generated submission and writes the
JSON and report files into `docs/assets/`. The example values on the site must
be reproducible by running that script.

Statistics displayed on the site must match the pipeline's own conventions:
population SD (`ddof=0`), CoV as `SD / abs(mean)` rendered as a percentage,
unavailable values shown as unavailable rather than as `0`.

---

## Design

Match the report design language already established in
`backend/services/report_branding.py`: the journal register — serif headings,
thin rules, restrained colour, `booktabs`-style tables (horizontal rules only,
no zebra striping, no filled cells), status shown as a small coloured dot.

* Use only web-safe font stacks already defined in the branding module.
  No webfont CDNs.
* Use the existing palette constants; do not invent new colours.
* Responsive down to mobile width. Readable at 320px.
* Accessible: semantic headings in order, alt text on every image, sufficient
  contrast, keyboard-reachable interactive controls, `prefers-reduced-motion`
  respected.
* No gradient hero, no rounded "card" grid of emoji-topped feature boxes, no
  pill badges. It should read as a research work product, not a SaaS landing
  page.

---

## Constraints

Do not deploy or redesign the FastAPI backend.

Do not require Render, Railway, Fly.io, Vercel server functions, or any other
hosted backend.

Assume the final Work Product URL will be a GitHub Pages site generated from:

```
docs/
    index.html
    styles.css
    script.js
    assets/
```

Additional constraints:

* Do not modify the pipeline's scientific formulas, scoring, validation, or
  report generation to suit the website.
* Do not modify `frontend/` — the local application UI is unchanged by this
  phase.
* Do not break the existing `docs/*.md` documentation files; the new site files
  live alongside them.
* Do not commit large binaries. If the demo video exceeds a few MB, link it
  (GitHub Release asset or an external host) rather than committing it.
* Do not fabricate results, metrics, screenshots, or capabilities. Every number
  shown must come from a real run.
* Do not claim any feature is implemented that is not.

---

## Deliverables

Create:

* `docs/index.html`
* `docs/styles.css`
* `docs/script.js`
* `docs/assets/`
* `docs/assets/screenshots/`
* `docs/assets/reports/`
* `docs/assets/video/`
* `GSoC_WORK_PRODUCT.md`

The website must be immediately deployable through GitHub Pages by selecting:

* Branch: `main`
* Folder: `/docs`

No additional build tools, Node.js, React, Next.js, Vue, or bundlers should be
required.

### `GSoC_WORK_PRODUCT.md`

The canonical work-product summary submitted to GSoC. It must contain:

* Project title, contributor, mentors, organisation.
* A summary of the problem and what was built.
* The state of the work at submission: what is complete and merged, what is in
  review, what remains.
* Links to the repository, the deployed work-product site, the documentation,
  releases, and the demo video.
* A list of the significant commits or pull requests.
* Honest limitations and future work.

### Also add

* `docs/assets/data/*.json` — the bundled example data.
* `scripts/build_site_examples.py` — regenerates that data and the example
  reports from the real pipeline.
* A short note in `README.md` pointing to the published site.

---

## Honesty requirements

State plainly, on the site and in `GSoC_WORK_PRODUCT.md`:

* Which challenges are supported and to what depth (DCE-2026 vs. ASL/DSC).
* That ROI statistics are within-scan spatial descriptive statistics only —
  not repeatability, reproducibility, or inter-participant variability.
* That grouped statistics, accuracy, deviance, and RSS are not implemented.
* That statistical conventions remain subject to confirmation by OSIPI.
* That the site's example data is pre-generated, and no processing happens in
  the browser.

---

## Verification

Before reporting completion:

1. Serve `docs/` with `python -m http.server` from that directory and confirm
   every page, asset, screenshot, report, and link resolves — no 404s, no
   absolute local paths, no `file://` references.
2. Confirm the site renders and functions with the FastAPI backend stopped.
3. Confirm no network request leaves the site except to the explicitly allowed
   external links (GitHub, video host). Check the browser network panel.
4. Validate the HTML, and confirm `script.js` runs without console errors.
5. Confirm every path in the HTML/CSS/JS is relative, so the site works from a
   `username.github.io/repo/` subpath, not just from the domain root.
6. Confirm the example numbers on the page match the output of
   `scripts/build_site_examples.py`.
7. Run the full existing test suite (Python + frontend) and confirm nothing
   regressed.

## Completion report

Report:

1. Files created and modified.
2. Where each piece of displayed data came from, and the command that
   regenerates it.
3. Screenshot inventory and how each was captured.
4. Video handling — embedded or linked, and why.
5. Confirmation the site is fully static and works with the backend stopped.
6. Confirmation all paths are relative and GitHub Pages `/docs` deployment
   works unchanged.
7. Accessibility and responsive checks performed.
8. Full test suite results, before and after.
9. Any content on the site that could not be sourced from a real run, and why.
10. Remaining limitations.
11. Confirmation that no backend was deployed, no upload service was created,
    and no pipeline logic was modified.
