"""Shared visual identity for OSIPI reports (HTML and PDF)."""

from __future__ import annotations

import base64
import io
import re
from functools import lru_cache
from pathlib import Path

from services.path_config import FRONTEND_DIR

LOGO_SVG_PATH = FRONTEND_DIR / "assets" / "logo.svg"

LOGO_LOCKUP_PATH = FRONTEND_DIR / "assets" / "logo-lockup.png"

# ── Palette ───────────────────────────────────────────────────────────────
# Compact report palette.
BRAND = {
    # Ink
    "ink":        "#16161a",   # body text, strong rules
    "ink_soft":   "#33353d",   # table cell text
    "muted":      "#6b6e7a",   # captions, deck lines
    "subtle":     "#83868f",   # section labels, column heads
    # Light table and section rules.
    "rule":       "#4a4d57",   # table top/bottom rule, masthead rule
    "hairline":   "#c9ccd3",   # section underline
    "faint":      "#eceef1",   # row separators in dense tables
    "paper":      "#ffffff",
    # Logo: used only for the mark itself
    "logo_plum":  "#830087",
    "logo_coral": "#fe575f",
    "ok":         "#3b6d11",
    "warn":       "#854f0b",
    "bad":        "#a32d2d",
    "neutral":    "#6b6e7a",
}

# Near-white status backgrounds for callers that need a full tone set.
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
    """Classify a status string as ``ok`` / ``warn`` / ``bad`` / ``neutral``."""
    text = str(value or "").strip().lower()
    if not text:
        return "neutral"
    for needle, tone in _TONE_RULES:
        if needle in text:
            return tone
    return "neutral"


@lru_cache(maxsize=1)
def _logo_layers() -> tuple[object, object] | None:
    """Decode the colour and mask PNGs embedded in the logo SVG."""
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
    """Composite the logo into a transparent PNG ``width`` pixels wide."""
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
    """Return the logo as a ``data:image/png;base64,...`` URI, or ``None``."""
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
    """Write the PDF-sized logo to a temp file and return its path."""
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
