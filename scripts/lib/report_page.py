#!/usr/bin/env python3
"""
=============================================================================
report_page.py

The drawing toolkit these reports are made of: an A4 portrait page, a palette,
a hand drawn table, a KPI row, and horizontal and vertical bar charts.  It
knows nothing about orders, markets or kdb - a caller passes text and numbers
and gets marks on a page.

  from lib.report_page import (SURFACE, INK, RED, figure, hline, kpis, save,
                               table, barchart, vbarchart)

  fig = figure()
  fig.text(L, 0.955, "My Report", fontsize=19, fontweight="bold", color=INK)
  hline(fig, 0.9185)
  kpis(fig, [("732", "Orders", INK), ("55.7%", "Completion", GREEN)], 0.884)
  table(fig, COLS, rows, y_top=0.808, row_h=0.040)
  barchart(fig, (L, 0.195, 0.405, 0.265), "By market", labels, values, texts,
           BLUE, vmax=100.0)
  save(fig, out_dir, "my_report")

WHY IT IS A LIBRARY.  The second report wanted the same page as the first, and
two copies of a layout drift the moment one of them is corrected.  Everything
in here is the part that is genuinely the same; the layout - where the bands
sit and how tall the rows are - stays in each report, because that is the part
that legitimately differs.

NOT A GRID.  The page is a document rather than a plot: a title block, a rule,
a KPI row, a table, then charts.  Only the bars live in an axes.  Positions are
figure fractions, which is why every caller reads like a layout rather than
like a chain of subplot calls.

  python scripts/lib/report_page.py --self-test
=============================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

# =============================================================================
# PALETTE
#
# Taken UNCHANGED from the data-viz reference palette, which documents its own
# validation.  The two series hues are used one per chart, so hue is chart
# identity rather than series identity and no within-chart separation is at
# stake:
#
#   BLUE   categorical slot 1
#   RED    status `critical` - 4.68:1 on this surface, and deliberately not the
#          categorical red, so it never reads as "series 8"
#
# Light only.  These pages get printed and pasted into documents, where a
# themed surface is a liability rather than a feature.
# =============================================================================

BLUE = "#2a78d6"        # the "how much" series
RED = "#d03b3b"         # the "how bad" series, and counts that are not zero
GREEN = "#006300"       # a good headline figure (success text)
SURFACE = "#ffffff"
INK = "#0b0b0b"         # primary
INK2 = "#52514e"        # secondary
INK3 = "#898781"        # muted - axis and category labels
RULE = "#e1e0d9"        # hairline
BASELINE = "#c3c2b7"    # chart baseline
HEADER_BG = "#3a3835"   # table header band
HEADER_FG = "#ffffff"

# For a STACKED mark, where the segments sit against each other and hue is
# series identity rather than chart identity.  Slots 1-4 of the reference
# palette in their published order, which is what the adjacent-pair CVD numbers
# are quoted for.  Four is the cap here: the fifth slot puts yellow beside
# orange, and that pair fails the all-pairs floors.
STACK = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")

FONTS = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]

# =============================================================================
# GEOMETRY.  A4 portrait, and the margins every band is measured from.
# =============================================================================

PAGE_W, PAGE_H = 8.27, 11.69          # inches
L, R = 0.075, 0.925                   # left and right margins, figure fraction
COL_W = R - L

H_TABLE_HEAD = 0.026                  # the dark header band
BAR_R_IN = 0.035                      # rounded data end, inches (~4px at 100dpi)
BAR_FRAC = 0.58                       # bar thickness as a fraction of the pitch

DASH = "—"


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


# =============================================================================
# FORMATTING
# =============================================================================

def fmt_int(n) -> str:
    return f"{int(n):,}"


def fmt_pct1(v) -> str:
    """One decimal, or an em dash.  None never prints as 0% - that would claim
    a measured zero where there was nothing to measure."""
    return DASH if v is None else f"{v:.1f}%"


def fmt_pct0(v) -> str:
    return "0%" if v is None else f"{v:.0f}%"


def fmt_hm(ms) -> str:
    """Milliseconds since midnight as HH:MM.  The page never shows seconds: at
    the resolution these reports are read, they are noise."""
    if ms is None:
        return DASH
    ms = int(ms)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}"


# =============================================================================
# CANVAS
# =============================================================================

def mpl():
    """matplotlib, imported here so callers stay importable without it."""
    try:
        import matplotlib
    except ImportError:
        raise SystemExit("drawing the report needs matplotlib.  "
                         "pip install matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = FONTS
    matplotlib.rcParams["pdf.fonttype"] = 42      # embed TrueType, keep text real
    return plt


def figure():
    """One blank A4 portrait page."""
    plt = mpl()
    fig = plt.figure(figsize=(PAGE_W, PAGE_H), facecolor=SURFACE)
    fig.patch.set_facecolor(SURFACE)
    return fig


def hline(fig, y, x0=L, x1=R, color=RULE, lw=0.8):
    from matplotlib.lines import Line2D
    fig.add_artist(Line2D([x0, x1], [y, y], transform=fig.transFigure,
                          color=color, linewidth=lw, zorder=1))


def rect(fig, x, y, w, h, color, zorder=1):
    from matplotlib.patches import Rectangle
    fig.add_artist(Rectangle((x, y), w, h, transform=fig.transFigure,
                             facecolor=color, edgecolor="none", zorder=zorder))


def heading(fig, title, subtitle, y_title=0.955, y_subtitle=0.931, y_rule=0.9185):
    fig.text(L, y_title, title, fontsize=19, fontweight="bold", color=INK,
             va="baseline")
    if subtitle:
        fig.text(L, y_subtitle, subtitle, fontsize=9.5, color=INK2,
                 va="baseline")
    if y_rule:
        hline(fig, y_rule)


def footer(fig, text, y_rule=0.066, y_text=0.048):
    if y_rule:
        hline(fig, y_rule)
    fig.text(L, y_text, text, fontsize=7.5, color=INK3, va="baseline")


def kpis(fig, items, y_value, y_label=None, fs=24):
    """A row of headline figures, evenly spaced across the column.

    items is [(value, label, colour)].  Colour carries whatever meaning it
    carries on the rest of the page; nothing here decides that.
    """
    if y_label is None:
        y_label = y_value - 0.024
    for i, (value, label, colour) in enumerate(items):
        x = L + i * (COL_W / max(len(items), 1))
        fig.text(x, y_value, value, fontsize=fs, fontweight="bold",
                 color=colour, va="baseline")
        fig.text(x, y_label, label, fontsize=9, color=INK2, va="baseline")


# =============================================================================
# TABLE
# =============================================================================

def table(fig, cols, rows, y_top, row_h, fs=9, head_fs=8.5, x0=L, x1=R):
    """A hand drawn table: dark header band, hairline separated rows.

    cols is [(label, width as a fraction of the span, right_aligned)].
    rows is [[(text, colour, weight), ...]] - one tuple per column, so a caller
    decides per CELL what is emphasised.  Returns the y of the last hairline,
    so a caller can put something underneath without measuring again.

    No zebra striping and no vertical rules: with right aligned figures the
    columns already read as columns, and every line not carrying information is
    a line competing with the ones that do.
    """
    span = x1 - x0
    head_y = y_top - H_TABLE_HEAD
    rect(fig, x0, head_y, span, H_TABLE_HEAD, HEADER_BG, zorder=2)

    x, edges = x0, []
    for label, frac, right in cols:
        w = frac * span
        edges.append((x, w, right))
        tx = x + w - 0.008 if right else x + 0.010
        fig.text(tx, head_y + H_TABLE_HEAD / 2.0, label,
                 ha="right" if right else "left", va="center",
                 fontsize=head_fs, fontweight="bold", color=HEADER_FG, zorder=3)
        x += w

    y = head_y
    for cells in rows:
        y -= row_h
        for (cx, cw, right), cell in zip(edges, cells):
            txt, colour, weight = cell if len(cell) == 3 else (*cell, "normal")
            tx = cx + cw - 0.008 if right else cx + 0.010
            fig.text(tx, y + row_h / 2.0, txt,
                     ha="right" if right else "left", va="center",
                     fontsize=fs, color=colour, fontweight=weight)
        hline(fig, y, x0, x1)
    return y


# =============================================================================
# MARKS
# =============================================================================

def _rounded_bar(ax, y0, h, w, color, rx, ry):
    """A bar with a square baseline end and a rounded data end.

    rx and ry are given separately because the axes is not square: a single
    radius in data units would draw an ellipse.  Both are clamped so a very
    short bar degrades to a rectangle instead of folding in on itself.
    """
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path
    if w <= 0:
        return
    rx = min(rx, w * 0.5)
    ry = min(ry, h * 0.5)
    y1 = y0 + h
    verts = [(0.0, y0), (w - rx, y0), (w, y0), (w, y0 + ry), (w, y1 - ry),
             (w, y1), (w - rx, y1), (0.0, y1), (0.0, y0)]
    codes = [Path.MOVETO, Path.LINETO, Path.CURVE3, Path.CURVE3, Path.LINETO,
             Path.CURVE3, Path.CURVE3, Path.LINETO, Path.CLOSEPOLY]
    ax.add_patch(PathPatch(Path(verts, codes), facecolor=color,
                           edgecolor="none", zorder=3))


def _rounded_vbar(ax, x0, w, h, color, rx, ry):
    """A column with a square foot on the baseline and a rounded top."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path
    if h <= 0:
        return
    rx = min(rx, w * 0.5)
    ry = min(ry, h * 0.5)
    x1 = x0 + w
    verts = [(x0, 0.0), (x0, h - ry), (x0, h), (x0 + rx, h), (x1 - rx, h),
             (x1, h), (x1, h - ry), (x1, 0.0), (x0, 0.0)]
    codes = [Path.MOVETO, Path.LINETO, Path.CURVE3, Path.CURVE3, Path.LINETO,
             Path.CURVE3, Path.CURVE3, Path.LINETO, Path.CLOSEPOLY]
    ax.add_patch(PathPatch(Path(verts, codes), facecolor=color,
                           edgecolor="none", zorder=3))


def barchart(fig, rect_, title, labels, values, texts, color,
             vmax=None, fs=8.0, title_y=None, gutter=0.38, head=1.26):
    """One HORIZONTAL bar chart: category gutter, bars, direct value labels.

    No axes, no grid, no ticks.  Every bar is labelled - with a handful of
    categories, or with a series read like a table, the label is the value
    channel and an axis would only repeat it less precisely.

    gutter is how much room the category labels get, and head how far past the
    longest bar the value labels may run, both as multiples of the scale.  They
    are arguments because a long label needs half again the gutter a short one
    does, and a bar squeezed to buy that room is the wrong trade.
    """
    x0, y0, w, h = rect_
    ax = fig.add_axes(rect_)
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)

    n = max(len(values), 1)
    top = vmax if vmax is not None else max([abs(v) for v in values] + [0.0])
    if not top:
        top = 1.0
    ax.set_xlim(-gutter * top, head * top)
    ax.set_ylim(n, 0)                       # first category at the top

    # inches per data unit, so the corner radius is round rather than elliptical
    span_x = (gutter + head) * top
    rx = BAR_R_IN * span_x / (w * PAGE_W)
    ry = BAR_R_IN * n / (h * PAGE_H)

    pad = 0.022 * top
    for i, (lab, v, txt) in enumerate(zip(labels, values, texts)):
        yb = i + (1.0 - BAR_FRAC) / 2.0
        _rounded_bar(ax, yb, BAR_FRAC, max(v, 0.0), color, rx, ry)
        ax.text(-pad, i + 0.5, lab, ha="right", va="center",
                fontsize=fs, color=INK3)
        ax.text(max(v, 0.0) + pad, i + 0.5, txt, ha="left", va="center",
                fontsize=fs, color=INK, fontweight="bold")

    if title_y is None:
        title_y = y0 + h + 0.014
    fig.text(x0, title_y, title, fontsize=10.5, fontweight="bold", color=INK)
    hline(fig, y0 - 0.008, x0, x0 + w, color=BASELINE, lw=0.8)
    return ax


def stacked_barchart(fig, rect_, title, labels, series, colors=None,
                     fs=8.0, title_y=None, gutter=0.38, head=1.30,
                     legend_y=None):
    """A HORIZONTAL bar chart whose bars are split into segments.

    series is [(name, [value per label]), ...].  The row total is labelled at
    the end of the bar; the segments are not labelled individually, because at
    this size the numbers would collide - the legend and the total are what the
    reader needs, and the exact split is in the terminal.

    A 2px surface gap separates the segments, per the mark spec, so two
    adjacent colours never read as one block.
    """
    x0, y0, w, h = rect_
    ax = fig.add_axes(rect_)
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)
    colors = colors or STACK

    n = max(len(labels), 1)
    totals = [sum(vals[i] for _nm, vals in series) for i in range(len(labels))]
    top = max(totals + [0.0]) or 1.0
    ax.set_xlim(-gutter * top, head * top)
    ax.set_ylim(n, 0)

    span_x = (gutter + head) * top
    rx = BAR_R_IN * span_x / (w * PAGE_W)
    ry = BAR_R_IN * n / (h * PAGE_H)
    pad = 0.022 * top

    from matplotlib.patches import Rectangle
    for i, lab in enumerate(labels):
        yb = i + (1.0 - BAR_FRAC) / 2.0
        run = 0.0
        parts = [(k, vals[i]) for k, (_nm, vals) in enumerate(series)
                 if vals[i] > 0]
        for j, (k, v) in enumerate(parts):
            last = j == len(parts) - 1
            if last:
                #  only the outermost segment gets the rounded data end
                _rounded_bar(ax, yb, BAR_FRAC, run + v, colors[k % len(colors)],
                             rx, ry)
                #  redraw the ones before it on top, so the rounding shows
                #  only at the very end of the bar
                back = 0.0
                for k2, v2 in parts[:-1]:
                    ax.add_patch(Rectangle(
                        (back, yb), v2, BAR_FRAC,
                        facecolor=colors[k2 % len(colors)], edgecolor=SURFACE,
                        linewidth=2, zorder=4))
                    back += v2
            run += v
        ax.text(-pad, i + 0.5, lab, ha="right", va="center", fontsize=fs,
                color=INK3)
        ax.text(totals[i] + pad, i + 0.5, fmt_int(totals[i]), ha="left",
                va="center", fontsize=fs, color=INK, fontweight="bold")

    if title_y is None:
        title_y = y0 + h + 0.014
    fig.text(x0, title_y, title, fontsize=10.5, fontweight="bold", color=INK)

    #  a legend is not optional with more than one series: identity must never
    #  be carried by colour alone
    if legend_y is None:
        legend_y = y0 - 0.024
    lx = x0
    for k, (name, _vals) in enumerate(series):
        rect(fig, lx, legend_y, 0.011, 0.008, colors[k % len(colors)], zorder=3)
        fig.text(lx + 0.016, legend_y, name, fontsize=7.5, color=INK2,
                 va="baseline")
        lx += 0.020 + 0.0088 * len(name)
    hline(fig, y0 - 0.008, x0, x0 + w, color=BASELINE, lw=0.8)
    return ax


def vbarchart(fig, rect_, title, labels, values, texts, color,
              vmax=None, fs=5.4, title_y=None):
    """One VERTICAL bar chart: columns left to right, labels turned on their
    side under the baseline, values above each column.

    This is the form a date series takes.  A month is a sequence, and a
    sequence reads left to right - the horizontal form would put time on the
    vertical axis, which is the wrong axis for it.
    """
    x0, y0, w, h = rect_
    ax = fig.add_axes(rect_)
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)

    n = max(len(values), 1)
    top = vmax if vmax is not None else max([abs(v) for v in values] + [0.0])
    if not top:
        top = 1.0
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_ylim(0.0, top * 1.16)          # headroom for the value labels

    rx = BAR_R_IN * n / (w * PAGE_W)
    ry = BAR_R_IN * (top * 1.16) / (h * PAGE_H)

    pad = 0.03 * top
    for i, (lab, v, txt) in enumerate(zip(labels, values, texts)):
        _rounded_vbar(ax, i - BAR_FRAC / 2.0, BAR_FRAC, max(v, 0.0),
                      color, rx, ry)
        ax.text(i, max(v, 0.0) + pad, txt, ha="center", va="bottom",
                fontsize=fs, color=INK, fontweight="bold")
        ax.text(i, -pad, lab, ha="right", va="center", rotation=90,
                rotation_mode="anchor", fontsize=fs, color=INK3)

    if title_y is None:
        title_y = y0 + h + 0.014
    fig.text(x0, title_y, title, fontsize=10.5, fontweight="bold", color=INK)
    hline(fig, y0, x0, x0 + w, color=BASELINE, lw=0.8)
    return ax


# =============================================================================
# OUTPUT
# =============================================================================

def save(figs, out_dir, stem, dpi=200):
    """Write one figure, or several as the pages of one PDF, plus a PNG each.

    A list gives a multi page PDF and stem_p1.png, stem_p2.png ... - PNG has no
    concept of a page, and silently writing only the first one is how a second
    page goes unnoticed.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    many = isinstance(figs, (list, tuple))
    pages = list(figs) if many else [figs]
    written = []

    from matplotlib.backends.backend_pdf import PdfPages
    pdf_path = out / f"{stem}.pdf"
    with PdfPages(pdf_path) as pdf:
        for fig in pages:
            pdf.savefig(fig, facecolor=SURFACE)
    written.append(pdf_path)
    log(f"  wrote {pdf_path}")

    for i, fig in enumerate(pages, 1):
        name = f"{stem}_p{i}.png" if len(pages) > 1 else f"{stem}.png"
        p = out / name
        fig.savefig(p, dpi=dpi, facecolor=SURFACE)
        written.append(p)
        log(f"  wrote {p}")
    return written


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import io
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("report_page --self-test\n\nformatting")
    check("thousands", fmt_int(49881997), "49,881,997")
    check("a percentage keeps one decimal", fmt_pct1(53.0769), "53.1%")
    check("nothing to measure is an em dash, never 0%", fmt_pct1(None), DASH)
    check("chart labels round to whole percent", fmt_pct0(53.0769), "53%")
    check("but a chart shows 0% where the table shows a dash",
          fmt_pct0(None), "0%")
    check("a time is HH:MM", fmt_hm(9 * 3_600_000 + 31 * 60_000), "09:31")
    check("midnight", fmt_hm(0), "00:00")
    check("and no time is a dash", fmt_hm(None), DASH)

    print("\ngeometry")
    check("A4 portrait", (PAGE_W, PAGE_H), (8.27, 11.69))
    check("the column is what the margins leave", round(COL_W, 3), 0.85)

    print("\ndrawing")
    try:
        import matplotlib      # noqa: F401
    except ImportError:
        print("  ..    matplotlib not installed, rendering skipped")
        print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
        return 0 if ok else 1

    COLS = (("Market", 0.5, False), ("Orders", 0.5, True))
    rows = [[("Japan", INK, "normal"), ("541", INK, "normal")],
            [("Korea", INK, "normal"), ("82", RED, "bold")]]

    fig = figure()
    heading(fig, "A Report", "By market  ·  2026-07-24")
    kpis(fig, [("732", "Orders", INK), ("55.7%", "Completion", GREEN),
               ("394", "Rejections", RED)], 0.884)
    y = table(fig, COLS, rows, 0.808, 0.040)
    check("the table reports where it ended", round(y, 3), round(0.808 - 0.026 - 0.080, 3))
    barchart(fig, (L, 0.40, 0.405, 0.20), "Horizontal", ["a", "b"], [40.0, 90.0],
             ["40%", "90%"], BLUE, vmax=100.0)
    vbarchart(fig, (L, 0.12, COL_W, 0.14), "Vertical", ["2026-07-01", "x"],
              [3.0, 9.0], ["3", "9"], RED)
    footer(fig, "Generated  ·  a test")

    buf = io.BytesIO()
    fig.savefig(buf, format="pdf", facecolor=SURFACE)
    check("a page with every element renders", buf.getvalue()[:5], b"%PDF-")

    st = figure()
    stacked_barchart(st, (L, 0.40, 0.405, 0.20), "Stacked",
                     ["Hong Kong", "Japan"],
                     [("short sell", [152.0, 2.0]), ("open", [31.0, 0.0]),
                      ("close", [12.0, 1.0]), ("continuous", [44.0, 0.0])])
    b_st = io.BytesIO()
    st.savefig(b_st, format="pdf", facecolor=SURFACE)
    check("a stacked chart renders", b_st.getvalue()[:5], b"%PDF-")
    st0 = figure()
    stacked_barchart(st0, (L, 0.4, 0.405, 0.2), "All zero", ["a", "b"],
                     [("x", [0.0, 0.0]), ("y", [0.0, 0.0])])
    b0 = io.BytesIO()
    st0.savefig(b0, format="pdf", facecolor=SURFACE)
    check("and one whose stacks are all empty", b0.getvalue()[:5], b"%PDF-")
    check("the stack palette caps at four - the fifth slot puts yellow beside "
          "orange", len(STACK), 4)

    empty = figure()
    barchart(empty, (L, 0.4, 0.405, 0.2), "No data", [], [], [], BLUE)
    vbarchart(empty, (L, 0.1, COL_W, 0.2), "No data", [], [], [], RED)
    table(empty, COLS, [], 0.8, 0.04)
    buf2 = io.BytesIO()
    empty.savefig(buf2, format="pdf", facecolor=SURFACE)
    check("so does one with nothing in it", buf2.getvalue()[:5], b"%PDF-")

    zeros = figure()
    barchart(zeros, (L, 0.4, 0.405, 0.2), "All zero", ["a", "b"], [0.0, 0.0],
             ["0", "0"], BLUE)
    buf3 = io.BytesIO()
    zeros.savefig(buf3, format="pdf", facecolor=SURFACE)
    check("and one whose bars are all zero", buf3.getvalue()[:5], b"%PDF-")

    print("\noutput")
    with tempfile.TemporaryDirectory() as d:
        one = save(figure(), d, "one")
        check("a single figure is a pdf and a png",
              [p.name for p in one], ["one.pdf", "one.png"])
        two = save([figure(), figure()], d, "two")
        check("several become one pdf and a png per page",
              [p.name for p in two],
              ["two.pdf", "two_p1.png", "two_p2.png"])
        blob = open(Path(d) / "two.pdf", "rb").read()
        check("the pdf really has two pages, not one silently dropped",
              blob.count(b"/Type /Page") - blob.count(b"/Type /Pages"), 2)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
