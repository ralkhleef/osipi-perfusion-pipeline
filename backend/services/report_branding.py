"""Shared visual identity for OSIPI reports (HTML and PDF).

The reports are typeset in a scientific-journal register rather than a
dashboard one, because the audience reads Radiology and Nature Methods all
day and that is the convention they trust. Concretely:

  * Rules, never boxes. Hierarchy comes from scale, weight, and case.
  * Tables follow LaTeX ``booktabs``: horizontal rules only, no vertical
    rules, no zebra striping, no fills.
  * Colour encodes data and nothing else. There is no decorative colour;
    the only hues in the document are the logo and three status inks.
  * Numerals are tabular and right-aligned so columns compare vertically.

Both renderers — the HTML report in ``backend/main.py`` and the ReportLab
PDF in ``services/pdf_report_service.py`` — import their palette, logo, and
status logic from here so the two stay identical.

The logo asset is ``frontend/assets/logo.svg``, which is really a thin SVG
wrapper around two embedded PNGs: a colour layer and a greyscale luminance
layer used as a mask. ReportLab cannot render SVG, so :func:`logo_png_bytes`
recombines those layers into one RGBA PNG. Results are cached per width, and
every entry point returns ``None`` rather than raising, so a missing or
unreadable logo degrades to a text-only masthead instead of breaking a report.
"""

from __future__ import annotations

import base64
import io
import re
from functools import lru_cache
from pathlib import Path

from services.path_config import FRONTEND_DIR

LOGO_SVG_PATH = FRONTEND_DIR / "assets" / "logo.svg"

# The official full lockup: mark + OSIPI wordmark + "Open Science Initiative
# for Perfusion Imaging". Used for the page-1 masthead. It is set on white
# rather than transparent, which is fine because report pages are white; the
# alternative (keying white out) would eat the coral's antialiased edges.
# The mark-only SVG is still used for running heads, where the tagline would
# be far too small to read.
LOGO_LOCKUP_PATH = FRONTEND_DIR / "assets" / "logo-lockup.png"

# ── Palette ───────────────────────────────────────────────────────────────
# Deliberately small. Black ink, three greys, two rule weights, three status
# inks, and the two logo colours. Anything beyond this is decoration.
BRAND = {
    # Ink
    "ink":        "#16161a",   # body text, strong rules
    "ink_soft":   "#33353d",   # table cell text
    "muted":      "#6b6e7a",   # captions, deck lines
    "subtle":     "#83868f",   # section labels, column heads
    # Rules. Kept deliberately light: booktabs calls for *thin* rules, and a
    # page of heavy black bars reads as noise rather than structure. The
    # strongest rule in the document is 0.7pt of `rule`, not solid black.
    "rule":       "#4a4d57",   # table top/bottom rule, masthead rule
    "hairline":   "#c9ccd3",   # section underline
    "faint":      "#eceef1",   # row separators in dense tables
    "paper":      "#ffffff",
    # Logo — used only for the mark itself
    "logo_plum":  "#830087",
    "logo_coral": "#fe575f",
    # Status inks. Dark enough to read as text on paper, and to survive a
    # monochrome print as distinguishable greys.
    "ok":         "#3b6d11",
    "warn":       "#854f0b",
    "bad":        "#a32d2d",
    "neutral":    "#6b6e7a",
}

# Tone backgrounds are intentionally near-paper. The journal treatment marks
# status with a coloured dot and coloured text, never a filled pill, but
# tone_colors() still needs a complete triple for any caller that wants one.
_TONE_BG = {
    "ok": "#f4f8ef", "warn": "#fdf8ee", "bad": "#fbf1f1", "neutral": "#f6f7f8",
}

# Phrases that map a human-readable status onto a tone. Order matters: the
# first substring found wins, so specific phrases precede general ones.
_TONE_RULES: tuple[tuple[str, str], ...] = (
    ("unable to continue", "bad"),
    ("blocking", "bad"),
    ("failed", "bad"),
    ("cannot run", "bad"),
    ("timed out", "bad"),
    ("error", "bad"),
    ("ready with limitations", "warn"),
    ("needs review", "warn"),
    ("partial", "warn"),
    ("warning", "warn"),
    ("mixed", "warn"),
    ("not available", "neutral"),
    ("not required", "neutral"),
    ("not scored", "neutral"),
    ("not run", "neutral"),
    ("complete", "ok"),
    ("available", "ok"),
    ("passed", "ok"),
    ("ready", "ok"),
    ("scored", "ok"),
)

# Typeface stacks.
#
# Every family here is one of ReportLab's base-14 fonts or a metric-compatible
# system equivalent, for two reasons:
#
#  1. Reports are standalone downloads. The web app pulls Inter from Google
#     Fonts, but a saved or emailed report has no network, so naming Inter
#     meant the file silently rendered in whatever the browser substituted —
#     a font "match" that only held on the developer's machine.
#  2. The HTML and PDF have to agree. Georgia/Inter in one and Times/Helvetica
#     in the other meant the same report looked like two different documents
#     depending on which format you opened.
#
# So: Times for display, Helvetica/Arial for data, Courier for labels. The
# mono is a deliberate nod to the typewriter face in the OSIPI lockup, and
# unlike a webfont it cannot fail to load.
SERIF_STACK = "'Times New Roman', Times, serif"
SANS_STACK = "Helvetica, Arial, 'Helvetica Neue', sans-serif"
MONO_STACK = "'Courier New', Courier, monospace"

# Matching ReportLab base-14 names, so the PDF uses the same three families.
PDF_SERIF = "Times-Roman"
PDF_SERIF_ITALIC = "Times-Italic"
PDF_SANS = "Helvetica"
PDF_MONO = "Courier"
PDF_MONO_BOLD = "Courier-Bold"


def status_tone(value: object) -> str:
    """Classify a status string as ``ok`` / ``warn`` / ``bad`` / ``neutral``.

    Unrecognised values fall back to ``neutral``, so a new status string can
    never render as a misleading green or red.
    """
    text = str(value or "").strip().lower()
    if not text:
        return "neutral"
    for needle, tone in _TONE_RULES:
        if needle in text:
            return tone
    return "neutral"


def tone_colors(tone: str) -> dict[str, str]:
    """Return the ``fg`` / ``bg`` triple for a tone name."""
    tone = tone if tone in _TONE_BG else "neutral"
    return {"fg": BRAND[tone], "bg": _TONE_BG[tone]}


@lru_cache(maxsize=1)
def _logo_layers() -> tuple[object, object] | None:
    """Decode the colour and mask PNGs embedded in the logo SVG.

    Returns ``None`` if the file is missing, malformed, or Pillow is
    unavailable. Cached because the base64 payloads total roughly 60 KB.
    """
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        markup = LOGO_SVG_PATH.read_text(encoding="utf-8")
    except Exception:
        return None
    payloads = re.findall(r"base64,([A-Za-z0-9+/=]+)", markup)
    if len(payloads) < 2:
        return None
    try:
        # Layer order in the SVG: [0] is the mask (inside <defs><mask>),
        # [1] is the visible colour artwork.
        mask = Image.open(io.BytesIO(base64.b64decode(payloads[0]))).convert("L")
        color = Image.open(io.BytesIO(base64.b64decode(payloads[1]))).convert("RGB")
    except Exception:
        return None
    if mask.size != color.size:
        mask = mask.resize(color.size)
    return color, mask


@lru_cache(maxsize=8)
def logo_png_bytes(width: int = 220) -> bytes | None:
    """Composite the logo into a transparent PNG ``width`` pixels wide.

    Alpha comes from the SVG's luminance mask. Transparent margins are
    trimmed so the mark sits flush against the masthead rule.
    """
    layers = _logo_layers()
    if layers is None:
        return None
    try:
        from PIL import Image

        color, mask = layers
        rgba = color.convert("RGBA")
        rgba.putalpha(mask)
        bbox = rgba.getbbox()
        if bbox:
            rgba = rgba.crop(bbox)
        width = max(16, int(width))
        height = max(1, round(rgba.height * width / rgba.width))
        rgba = rgba.resize((width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        rgba.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


@lru_cache(maxsize=8)
def logo_data_uri(width: int = 220) -> str | None:
    """Return the logo as a ``data:image/png;base64,...`` URI, or ``None``.

    HTML reports get downloaded and emailed as standalone files, so a
    ``/static/...`` reference would break the moment the file left the
    server. Embedding keeps the report self-contained.
    """
    png = logo_png_bytes(width)
    if png is None:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@lru_cache(maxsize=4)
def lockup_png_bytes(width: int = 900) -> bytes | None:
    """Return the full OSIPI lockup as PNG bytes at ``width`` pixels."""
    try:
        from PIL import Image

        if not LOGO_LOCKUP_PATH.exists():
            return None
        im = Image.open(LOGO_LOCKUP_PATH).convert("RGB")
        width = max(64, int(width))
        if im.width != width:
            im = im.resize((width, max(1, round(im.height * width / im.width))),
                           Image.LANCZOS)
        buffer = io.BytesIO()
        im.save(buffer, "PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        return None


@lru_cache(maxsize=4)
def lockup_data_uri(width: int = 900) -> str | None:
    """The lockup as a self-contained data URI for the HTML masthead."""
    png = lockup_png_bytes(width)
    if png is None:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@lru_cache(maxsize=1)
def lockup_aspect() -> float:
    """Width / height of the lockup, for sizing without loading it twice."""
    try:
        from PIL import Image

        with Image.open(LOGO_LOCKUP_PATH) as im:
            return im.width / im.height
    except Exception:
        return 4.72  # measured from the supplied artwork


@lru_cache(maxsize=1)
def lockup_reportlab_path() -> str | None:
    """Path to a PDF-resolution copy of the lockup, or ``None``."""
    png = lockup_png_bytes(1100)
    if png is None:
        return None
    try:
        import tempfile

        target = Path(tempfile.gettempdir()) / "osipi_report_lockup.png"
        if not target.exists() or target.stat().st_size != len(png):
            target.write_bytes(png)
        return str(target)
    except Exception:
        return None


@lru_cache(maxsize=1)
def logo_reportlab_path() -> str | None:
    """Write the PDF-sized logo to a temp file and return its path.

    ReportLab's ``drawImage`` takes a path; writing once and reusing avoids
    re-compositing the PNG on every page of every report.
    """
    png = logo_png_bytes(320)
    if png is None:
        return None
    try:
        import tempfile

        target = Path(tempfile.gettempdir()) / "osipi_report_logo.png"
        if not target.exists() or target.stat().st_size != len(png):
            target.write_bytes(png)
        return str(target)
    except Exception:
        return None
