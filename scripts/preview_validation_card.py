"""Render the validation results card as a standalone HTML preview.

Uses the real ``frontend/styles.css`` and the exact markup
``renderValidationResults()`` builds in ``frontend/app.js``, so the preview
shows what the app shows rather than an approximation.

Writes a before/after pair: "before" truncates the stylesheet at the journal
register block, "after" uses the whole file. Same markup, same file, only the
appended rules differ.

    python3 scripts/preview_validation_card.py
    open data/outputs/ui_preview/validation_card_after.html
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STYLES = ROOT / "frontend" / "styles.css"
OUT_DIR = ROOT / "data" / "outputs" / "ui_preview"

# The marker that opens the appended block. Everything from here is "after".
MARKER = "   Journal register — issue lists, status chips, section headings"

# Real messages, taken verbatim from the validation run in CODE_WALKTHROUGH.md.
SCANS = [
    ("Participant1/Site1/Repeat1", ["Ktrans", "modelled_st", "ve", "vp"]),
    ("Participant1/Site1/Repeat2", ["Ktrans", "modelled_st", "ve", "vp"]),
]
ERRORS = [
    f"{scan}/{name}.nii.gz could not be assigned to a scan because dataset "
    f"could not be determined, so completeness cannot be evaluated."
    for scan, names in SCANS for name in names
] + [
    "Required artifact missing: methods document not found in the submission.",
]
WARNINGS = [
    "README or SOP file missing",
    "No run instructions found",
    "Duplicate filename in submission",
]
CHECKS = [
    "43 NIfTI files detected",
    "Code files present",
]


def _li(items: list[str], cls: str) -> str:
    return "".join(
        f'<li class="{cls}">{html.escape(text)}</li>' for text in items
    )


def _status_chip(label: str, state: str, tone: str) -> str:
    return (
        f'<span class="status-chip status-pill status-{state} '
        f'status-chip-{tone}">{html.escape(label)}</span>'
    )


def _help(text: str) -> str:
    return (
        f'<button type="button" class="help-tooltip" aria-label="More '
        f'information">?<span class="tooltip-text">{html.escape(text)}</span>'
        f"</button>"
    )


def card_markup() -> str:
    """The markup from frontend/app.js renderValidationResults()."""
    chips = f"""
        <div class="validation-detail-chips worklist-meta">
          <span class="validation-meta-with-help">{_status_chip("Errors", "error", "danger")}</span>
          <span class="validation-meta-with-help">Result maps provided {_help("This submission already includes result maps, so no processing run is needed.")}</span>
          <span class="validation-meta-with-help">{_status_chip("Cannot run", "error", "danger")} {_help("Runnable submissions include executable code. Result-only submissions skip execution and go directly to scoring.")}</span>
        </div>"""

    return f"""
    <div class="validation-detail-inner">
      {chips}
      <p class="vr-result-only-note">This submission already includes result maps. No processing run is needed.</p>
      <div class="vr-detail-nifti">Map count: <strong>43</strong></div>
      <div class="vp-section error-section" style="margin-top:8px">
        <div class="vp-section-heading">Blocking errors</div>
        <ul class="issue-list">{_li(ERRORS, "is-error")}</ul>
      </div>
      <div class="vp-section review-section" style="margin-top:8px">
        <div class="vp-section-heading">Items to review</div>
        <ul class="issue-list">{_li(WARNINGS, "review-item")}</ul>
      </div>
      <details class="tech-checks-toggle" style="margin-top:8px" open>
        <summary>Technical details</summary>
        <ul class="issue-list" style="margin-top:6px">{_li(CHECKS, "is-pass")}</ul>
      </details>
    </div>"""


def page(css: str, title: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{css}</style>
<style>
  body {{ background: #f3f4f6; padding: 28px; }}
  .preview-frame {{ max-width: 760px; margin: 0 auto; background: #fff;
    border: 1px solid #dde1e7; border-radius: 12px; padding: 22px 24px; }}
  .preview-title {{ font-size: 0.95rem; font-weight: 700; color: #1a1a1c; margin: 0; }}
  .preview-sub {{ font-size: 0.75rem; color: #6b6e7a; margin: 2px 0 16px; }}
  .preview-label {{ max-width: 760px; margin: 0 auto 10px; font: 600 0.7rem/1
    system-ui, sans-serif; letter-spacing: 0.12em; text-transform: uppercase;
    color: #6b6e7a; }}
</style></head>
<body>
  <p class="preview-label">{html.escape(title)}</p>
  <div class="preview-frame">
    <p class="preview-title">DCE Test team gamma Clinical</p>
    <p class="preview-sub">DCE · Mixed/Other · 43 maps · 3 items to review</p>
    {card_markup()}
  </div>
</body></html>
"""


def main() -> None:
    css = STYLES.read_text(encoding="utf-8")
    index = css.find(MARKER)
    if index == -1:
        raise SystemExit(
            "Journal register marker not found in styles.css — the preview "
            "cannot separate before from after."
        )
    # Rewind to the opening comment delimiter so "before" ends cleanly.
    before_css = css[: css.rfind("/*", 0, index)]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for name, sheet, title in (
        ("before", before_css, "Before"),
        ("after", css, "After"),
    ):
        path = OUT_DIR / f"validation_card_{name}.html"
        path.write_text(page(sheet, title), encoding="utf-8")
        written.append(path)

    for path in written:
        print(path)


if __name__ == "__main__":
    main()
