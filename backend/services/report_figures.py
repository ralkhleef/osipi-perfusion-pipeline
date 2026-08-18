"""Small data figures for OSIPI reports, rendered to both SVG and PDF.

A report about data quality that contains no graphics makes the reader do
the comparison arithmetic themselves. The module draws Bland-Altman and
identity plots, the standard method-comparison figures, from statistics
the scorer already stores, so no voxel data has to be re-read.

Axes are scaled to the observed range rather than forced to zero. That is
safe for a scatter, where no baseline is implied; it would not be for a bar
chart, which is why bars are not used here.

Styling follows the rest of the report: hairline axes, no gridlines, no
fills, no chartjunk. Colour encodes series and nothing else.

The geometry is computed once into a list of primitive dicts, then handed
to one of two thin renderers, :func:`to_svg` for the HTML report and
:func:`to_drawing` for the ReportLab PDF, so the two outputs cannot drift.
Every builder returns ``None`` when there is nothing worth plotting, and
callers are expected to skip the figure in that case.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from services.report_branding import BRAND, SANS_STACK

# Three series styles that remain distinct in greyscale.
SERIES = {
    "primary":   BRAND["ink"],
    "secondary": BRAND["subtle"],
    "accent":    BRAND["logo_plum"],
}

LEGEND_H = 15.0
PAD_R = 10.0


def _nums(values):
    return [float(v) for v in values
            if isinstance(v, (int, float)) and not isinstance(v, bool)]


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """Pick round tick values spanning ``lo``..``hi``."""
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(1, count)
    mag = 10 ** _floor_log10(raw)
    for mult in (1, 2, 2.5, 5, 10):
        step = mag * mult
        if step >= raw:
            break
    start = step * int(lo / step)
    if start > lo:
        start -= step
    ticks, value = [], start
    while value <= hi + step * 0.5:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _floor_log10(x: float) -> int:
    import math
    return int(math.floor(math.log10(x))) if x > 0 else 0








def _xy_plot(points: Sequence[Mapping[str, Any]],
             *, width: float, height: float,
             x_label: str, y_label: str,
             reference: str = "none",
             band: tuple[float, float] | None = None,
             band_label: str = "",
             legend: Sequence[tuple[str, str]] = ()) -> dict | None:
    """A scatter with two real axes.

    ``reference`` draws either the ``identity`` line (y = x, for
    submitted-vs-reference) or a ``zero`` line (for Bland-Altman). ``band``
    is an optional pair of horizontal limits drawn dashed, the 95% limits
    of agreement. ``legend`` is ``[(name, marker style), ...]``.
    """
    xs = _nums(p.get("x") for p in points)
    ys = _nums(p.get("y") for p in points)
    if not xs or not ys:
        return None

    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    if band:
        lo_y, hi_y = min(lo_y, band[0]), max(hi_y, band[1])
    if reference == "identity":
        # A y = x line only reads correctly on a shared, square scale.
        lo_x = lo_y = min(lo_x, lo_y)
        hi_x = hi_y = max(hi_x, hi_y)
    if reference == "zero":
        lo_y, hi_y = min(lo_y, 0.0), max(hi_y, 0.0)

    def _pad(lo, hi):
        if hi == lo:
            step = abs(hi) * 0.1 or 1.0
            return lo - step, hi + step
        margin = (hi - lo) * 0.12
        return lo - margin, hi + margin

    lo_x, hi_x = _pad(lo_x, hi_x)
    lo_y, hi_y = _pad(lo_y, hi_y)
    xt, yt = _nice_ticks(lo_x, hi_x, 4), _nice_ticks(lo_y, hi_y, 4)
    lo_x, hi_x = min(lo_x, xt[0]), max(hi_x, xt[-1])
    lo_y, hi_y = min(lo_y, yt[0]), max(hi_y, yt[-1])

    # Reserve a row above the plot for the y-axis label. Drawing it beside
    # the axis (anchored "end" at x0) pushed long labels such as
    # "submitted mean (ml/100g/min)" off the left edge of the figure, so it
    # sits above the axis instead, which also avoids rotated text, which
    # ReportLab's String cannot do.
    gutter, bottom = 42.0, 28.0
    top = (LEGEND_H if legend else 0.0) + 12.0
    x0, x1 = gutter, width - PAD_R
    y0, y1 = bottom, height - top

    def sx(v):
        return x0 + (v - lo_x) / (hi_x - lo_x) * (x1 - x0)

    def sy(v):
        return y0 + (v - lo_y) / (hi_y - lo_y) * (y1 - y0)

    p: list[dict] = []
    lx = x0
    for name, style in legend:
        p.append({"t": "marker", "x": lx + 3, "y": height - 6, "style": style})
        p.append({"t": "text", "x": lx + 10, "y": height - 9, "s": name,
                  "size": 6.4, "color": BRAND["muted"], "anchor": "start"})
        lx += 10 + len(name) * 6.4 * 0.58 + 14

    # Axes: two hairlines, no frame, no gridlines.
    p.append({"t": "line", "x1": x0, "y1": y0, "x2": x1, "y2": y0,
              "w": 0.5, "color": BRAND["rule"]})
    p.append({"t": "line", "x1": x0, "y1": y0, "x2": x0, "y2": y1,
              "w": 0.5, "color": BRAND["rule"]})
    for t in xt:
        if lo_x <= t <= hi_x:
            p.append({"t": "line", "x1": sx(t), "y1": y0, "x2": sx(t), "y2": y0 - 3,
                      "w": 0.5, "color": BRAND["hairline"]})
            p.append({"t": "text", "x": sx(t), "y": y0 - 11, "s": f"{t:g}",
                      "size": 6.2, "color": BRAND["muted"], "anchor": "middle"})
    for t in yt:
        if lo_y <= t <= hi_y:
            p.append({"t": "line", "x1": x0, "y1": sy(t), "x2": x0 - 3, "y2": sy(t),
                      "w": 0.5, "color": BRAND["hairline"]})
            p.append({"t": "text", "x": x0 - 6, "y": sy(t) - 2.2, "s": f"{t:g}",
                      "size": 6.2, "color": BRAND["muted"], "anchor": "end"})
    p.append({"t": "text", "x": x1, "y": 3, "s": x_label,
              "size": 6.4, "color": BRAND["subtle"], "anchor": "end"})
    p.append({"t": "text", "x": 0, "y": y1 + 4, "s": y_label,
              "size": 6.4, "color": BRAND["subtle"], "anchor": "start"})

    if reference == "identity":
        lo = max(lo_x, lo_y)
        hi = min(hi_x, hi_y)
        p.append({"t": "line", "x1": sx(lo), "y1": sy(lo),
                  "x2": sx(hi), "y2": sy(hi),
                  "w": 0.6, "color": BRAND["subtle"], "dash": (2, 2)})
    elif reference == "zero" and lo_y <= 0 <= hi_y:
        p.append({"t": "line", "x1": x0, "y1": sy(0.0), "x2": x1, "y2": sy(0.0),
                  "w": 0.6, "color": BRAND["subtle"]})

    if band:
        for edge in band:
            if lo_y <= edge <= hi_y:
                p.append({"t": "line", "x1": x0, "y1": sy(edge),
                          "x2": x1, "y2": sy(edge),
                          "w": 0.5, "color": BRAND["logo_plum"], "dash": (3, 2)})
        if band_label and lo_y <= band[1] <= hi_y:
            p.append({"t": "text", "x": x1, "y": sy(band[1]) + 4.5, "s": band_label,
                      "size": 6.0, "color": BRAND["logo_plum"], "anchor": "end"})

    for point in points:
        x, y = point.get("x"), point.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            continue
        if isinstance(x, bool) or isinstance(y, bool):
            continue
        p.append({"t": "marker", "x": sx(float(x)), "y": sy(float(y)),
                  "style": point.get("style", "solid")})

    return {"width": width, "height": height, "prims": p}


def bland_altman_figure(points: Sequence[Mapping[str, Any]],
                        *, units: str = "map units",
                        width: float = 300.0,
                        height: float = 178.0) -> dict | None:
    """Bland-Altman: mean level against submitted-minus-reference bias.

    Limits of agreement are the pooled bias ± 1.96 SD, taken from the
    ``standard_deviation_error`` the scorer already stores per region, so no
    voxel data has to be re-read to draw this.
    """
    usable = [p for p in points
              if isinstance(p.get("bias"), (int, float))
              and isinstance(p.get("mean_level"), (int, float))]
    if not usable:
        return None
    biases = _nums(p["bias"] for p in usable)
    sds = _nums(p.get("sd") for p in usable)
    band = None
    band_label = ""
    if biases and sds:
        mean_bias = sum(biases) / len(biases)
        pooled_sd = sum(sds) / len(sds)
        band = (mean_bias - 1.96 * pooled_sd, mean_bias + 1.96 * pooled_sd)
        band_label = "95% limits of agreement"
    fig = _xy_plot(
        [{"x": p["mean_level"], "y": p["bias"], "style": p.get("style", "solid")}
         for p in usable],
        width=width, height=height,
        x_label=f"mean of submitted and reference ({units})",
        y_label=f"bias ({units})",
        reference="zero", band=band, band_label=band_label,
        legend=[("Whole image", "solid"), ("ROI", "hollow")],
    )
    if fig is not None and band:
        # Surfaced so captions can quote the interval and tests can check the
        # arithmetic directly rather than inferring it from pixel positions.
        fig["mean_bias"] = mean_bias
        fig["limits"] = band
    return fig


def identity_figure(points: Sequence[Mapping[str, Any]],
                    *, units: str = "map units",
                    width: float = 300.0,
                    height: float = 178.0) -> dict | None:
    """Mean submitted against mean reference, with the y = x line."""
    usable = [p for p in points
              if isinstance(p.get("mean_submitted"), (int, float))
              and isinstance(p.get("mean_reference"), (int, float))]
    if not usable:
        return None
    return _xy_plot(
        [{"x": p["mean_reference"], "y": p["mean_submitted"],
          "style": p.get("style", "solid")} for p in usable],
        width=width, height=height,
        x_label=f"reference mean ({units})",
        y_label=f"submitted mean ({units})",
        reference="identity",
        legend=[("Whole image", "solid"), ("ROI", "hollow")],
    )


# ── Renderers ─────────────────────────────────────────────────────────────
# Primitives use y-up coordinates (ReportLab's convention); the SVG renderer
# flips y so both outputs describe the same geometry.

def _marker_svg(x: float, y: float, style: str) -> str:
    if style == "hollow":
        return (f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.6" fill="none" '
                f'stroke="{SERIES["primary"]}" stroke-width="0.9"/>')
    color = SERIES["accent"] if style == "accent" else SERIES["primary"]
    if style == "accent":
        return (f'<rect x="{x - 2.3:.2f}" y="{y - 2.3:.2f}" width="4.6" '
                f'height="4.6" fill="{color}"/>')
    return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.6" fill="{color}"/>'


def to_svg(fig: Mapping[str, Any]) -> str:
    """Render a primitive bundle as an inline SVG fragment."""
    w, h = fig["width"], fig["height"]
    out = [
        f'<svg class="fig-svg" viewBox="0 0 {w:.0f} {h:.0f}" width="100%" '
        f'height="{h:.0f}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Report figure">'
    ]
    for p in fig["prims"]:
        kind = p["t"]
        if kind == "line":
            dash = p.get("dash")
            dash_attr = (f' stroke-dasharray="{dash[0]},{dash[1]}"' if dash else "")
            out.append(
                f'<line x1="{p["x1"]:.2f}" y1="{h - p["y1"]:.2f}" '
                f'x2="{p["x2"]:.2f}" y2="{h - p["y2"]:.2f}" '
                f'stroke="{p["color"]}" stroke-width="{p["w"]}"{dash_attr}/>')
        elif kind == "marker":
            out.append(_marker_svg(p["x"], h - p["y"], p["style"]))
        elif kind == "text":
            anchor = {"start": "start", "middle": "middle",
                      "end": "end"}[p["anchor"]]
            text = (str(p["s"]).replace("&", "&amp;")
                    .replace("<", "&lt;").replace(">", "&gt;"))
            out.append(
                f'<text x="{p["x"]:.2f}" y="{h - p["y"]:.2f}" '
                f'font-family="{SANS_STACK}" font-size="{p["size"]}" '
                f'fill="{p["color"]}" text-anchor="{anchor}">{text}</text>')
    out.append("</svg>")
    return "".join(out)


def to_drawing(fig: Mapping[str, Any]):
    """Render a primitive bundle as a ReportLab ``Drawing`` flowable."""
    from reportlab.graphics.shapes import Circle, Drawing, Line, Rect, String
    from reportlab.lib import colors

    d = Drawing(fig["width"], fig["height"])
    for p in fig["prims"]:
        kind = p["t"]
        if kind == "line":
            line = Line(p["x1"], p["y1"], p["x2"], p["y2"],
                        strokeColor=colors.HexColor(p["color"]),
                        strokeWidth=p["w"])
            if p.get("dash"):
                line.strokeDashArray = list(p["dash"])
            d.add(line)
        elif kind == "marker":
            style = p["style"]
            if style == "hollow":
                d.add(Circle(p["x"], p["y"], 2.6, fillColor=None,
                             strokeColor=colors.HexColor(SERIES["primary"]),
                             strokeWidth=0.9))
            elif style == "accent":
                d.add(Rect(p["x"] - 2.3, p["y"] - 2.3, 4.6, 4.6,
                           fillColor=colors.HexColor(SERIES["accent"]),
                           strokeColor=None))
            else:
                d.add(Circle(p["x"], p["y"], 2.6,
                             fillColor=colors.HexColor(SERIES["primary"]),
                             strokeColor=None))
        elif kind == "text":
            d.add(String(p["x"], p["y"], str(p["s"]),
                         fontName="Helvetica", fontSize=p["size"],
                         fillColor=colors.HexColor(p["color"]),
                         textAnchor=p["anchor"]))
    return d
