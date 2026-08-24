#!/usr/bin/env python3
"""
=============================================================================
dark_routed_executed.py

Our DARK activity split into what we ROUTED and what actually EXECUTED, per
venue, and the two pie charts the report draws from it.

  Routed %      share of dark notional we SENT to each venue
  Executed %    share of dark notional that came BACK from each venue
  Fill Rate     executed / routed, money weighted, per venue

The gap between the two is the point.  A venue taking 47% of the flow and
returning 88% of the fills is a different venue from one doing 6% and 4%, and
that is exactly what the two pies side by side show.

Talks to ONE kdb process over PyKX - the HISTORICAL order server, holding
workorder and target_stock.  No quotes are needed here, so qatt is not
involved.  host:port is a constant below rather than an argument; set it once,
before first use.

  python scripts/dark_routed_executed/dark_routed_executed.py \
      --start 2026-04-01 --end 2026-06-30 --country AU --out-dir out

PyKX runs in unlicensed mode - SyncQConnection against a remote process needs
no q licence and no QHOME, because all q evaluation happens on the server.
pykx is imported lazily inside connect(), so --self-test runs anywhere.

  python scripts/dark_routed_executed/dark_routed_executed.py --self-test

RELATION TO THE OTHER TWO

  queries/dark_summary/dark_routed_executed.q   the same split, one date, no
      country filter and no grouping.  This script is that query run over a
      range, with the venue sheet applied and the pies drawn.

  scripts/reversion_liquidity                   tables 3.1 and 3.3.  Its
      %Notional and this script's Executed % measure almost the same thing off
      two different columns - fillsize*fillprice per execution there,
      make*avg_fill_price per child order here - so they agree to a rounding,
      not to the bit.  The report shows Centrepoint at 88.6 in table 3.1 and
      88.5 in the Executed pie for exactly that reason.

PERCENTAGES ARE COMPUTED ONCE, AT THE END, from accumulated notionals.  A mean
of daily percentages is a different and wrong number - it weights a quiet
Tuesday the same as a heavy Thursday.  See test_percentages_come_from_totals().
=============================================================================
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# scripts/lib holds local_config, which reads the settings file beside this
# script.  Added to the path rather than installed, so this still runs as
# `python scripts/dark_routed_executed/dark_routed_executed.py` from the repo
# root.  Copy scripts/lib alongside this folder if you move it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.local_config import apply_local                        # noqa: E402

# -----------------------------------------------------------------------------
# CONNECTION.  Edit this, or put it in a local_settings.py beside this script -
# see scripts/lib/README.md.  The HISTORICAL order server, not the realtime one.
#
# It is an open process, so host and port is the whole of it - connect() takes
# no credentials.
# -----------------------------------------------------------------------------

ORDER_SERVER = "CHANGEME:5010"

_PLACEHOLDER = "CHANGEME"


# -----------------------------------------------------------------------------
# THE VENUE SHEET.  Edit this.
#
# (country, kdb venue) -> (name for the table, short name for the pies)
#
# Several kdb symbols can be one pool, and both the table and the pies name the
# pool: the report's Routed pie has ONE Ctrpnt slice at 46.6 where our
# workorder table says CENTREPOINT_DARK for one route into it and
# CENTREPOINT_CITI_DARK for another.
#
# Keyed on the country too, because the sheet is: JPMAP_DARK is JPMX in JP and
# HK, while in AU the same pool is reached as JPMAP_MF_DARK.
#
# The SECOND name is what labels a pie slice, because a slice has no room for
# "Centrepoint".  The first is what the table prints.  Keep this table in step
# with the copy in scripts/reversion_liquidity/reversion_liquidity.py - each
# script folder stands on its own, so the sheet lives in both.
#
# The sheet also has a "type" column, "Midpoint dark" on every row.  This
# script only ever sees dark venues, so it is not a key.
#
# A pair that is NOT here keeps its raw kdb symbol as its row label, and is
# named on stdout just above the table.  Nothing is dropped and
# nothing is merged into the wrong pool by guessing.  It is still subject to
# --other-below like any venue, so a thin stray one lands in Other on the pie -
# the table and the stdout notice are where it is always visible.
# -----------------------------------------------------------------------------

VENUE_GROUPS = {
    ("JP", "CITI_DARK"):             ("Citi",        "Citi"),
    ("JP", "DAIWA_DARK"):            ("Daiwa",       "DAIWA"),
    ("JP", "JPMAP_DARK"):            ("JPMX",        "JPMX"),
    ("JP", "LNAL_DARK"):             ("LNAL",        "Liqnet"),
    ("JP", "MS_DARK"):               ("MS Pool",     "MSPL"),
    ("JP", "NOM_DARK"):              ("Nomura",      "Nomura"),
    ("JP", "POSITNOW_DARK"):         ("Posit",       "Posit"),

    ("HK", "CITI_DARK"):             ("Citi",        "Citi"),
    ("HK", "CLSA_DARK"):             ("CLSA",        "CLSA"),
    ("HK", "INSTINET_DARK"):         ("Instinet",    "Instnet"),
    ("HK", "JPMAP_DARK"):            ("JPMX",        "JPMX"),
    ("HK", "MS_DARK"):               ("MS Pool",     "MSPL"),
    ("HK", "POSITNOW_DARK"):         ("Posit",       "Posit"),

    # the published pie labels this slice Ctrpnt; the sheet says CentrePt and
    # the sheet is what we follow
    ("AU", "CENTREPOINT_CITI_DARK"): ("Centrepoint", "CentrePt"),
    ("AU", "CENTREPOINT_DARK"):      ("Centrepoint", "CentrePt"),
    ("AU", "CLSA_DARK"):             ("CLSA",        "CLSA"),
    ("AU", "JPMAP_MF_DARK"):         ("JPMX",        "JPMX"),
    ("AU", "MS_DARK"):               ("MS Pool",     "MSPL"),
    ("AU", "POSITNOW_DARK"):         ("Posit",       "Posit"),
}

# -----------------------------------------------------------------------------
# Anything above can be overridden from a local_settings.py beside this script,
# which git ignores - so the server survives a pull and this file never has to
# be edited.  See scripts/lib/README.md.
#
# It sits here, ABOVE SHORT_NAMES, so a locally replaced VENUE_GROUPS is the one
# the pie labels are built from too - a derived name below this line cannot go
# stale against a sheet set from outside.
# -----------------------------------------------------------------------------

apply_local(globals(), __file__)

# display name -> pie label.  Built from the sheet, so the two can never drift.
SHORT_NAMES = {name: short for name, short in VENUE_GROUPS.values()}

GROUP_COL = "venue_group"
OTHER = "Other"


# -----------------------------------------------------------------------------
# q source.  Sent as text + typed args, the same contract as sending a lambda
# over a raw handle - dates and country codes travel as q values, never
# interpolated into the text.
#
# This is queries/dark_summary/dark_routed_executed.q with three changes:
#
#   - a country filter, so a range can be cut the way the report cuts it
#   - grouped by country as well as venue, because the venue sheet is keyed on
#     the pair
#   - an ij rather than an lj onto target_stock, so a child order whose parent
#     is not in the requested country is DROPPED rather than kept with a null
#     fx.  With no --country the two are the same join.
#
# NO make>0 filter anywhere: the children that never filled are exactly what
# makes routed differ from executed, and dropping them would collapse the two
# pies onto each other.
#
# workorder is reduced to one row per id_work with `last` before anything is
# joined to it.  If workorder already holds one row per child order that is a
# no-op; if it ever holds a row per state change, orders_routed would otherwise
# count state changes.
# -----------------------------------------------------------------------------

Q_ROUTED_EXECUTED = """
{[d;ctry]
  dk:("*DARK*";"*DRK*");
  w:select date,id_server,id_work,id_target,venue,size,price,make,
      avg_fill_price,transmit_lastprice
    from workorder
    where date=d, any (upper venue) like/: dk;
  w:0!select last id_target, last venue, last size, last price, last make,
      last avg_fill_price, last transmit_lastprice
    by date,id_server,id_work from w;
  ids:exec distinct id_target from w;
  x:select date,id_server,id_target,fxlast,country
    from target_stock where date=d, id_target in ids;
  x:$[0=count ctry; x; select from x where country=`$ctry];
  x:`date`id_server`id_target xkey x;
  w:w ij x;
  / an order that never filled has no fill price, so ROUTED is valued at the
  / price the child was sent with, falling back to the last trade at transmit
  / time for market and pegged orders that carry no usable limit
  w:update px_routed:transmit_lastprice^?[price>0;price;0n] from w;
  / fxlast is local -> USD
  w:update
      notional_routed:size*px_routed*fxlast,
      notional_executed:make*avg_fill_price*fxlast
    from w;
  0!select
      orders_routed:count i,
      orders_filled:sum make>0,
      shares_routed:sum size,
      shares_executed:sum make,
      notional_routed:sum notional_routed,
      notional_executed:sum notional_executed
    by country,venue from w
 }
"""

# Where a day's rows go, stage by stage, for --diagnose.  An empty report is
# almost always one of these dropping to zero.
Q_DIAG = """
{[d;ctry]
  dk:("*DARK*";"*DRK*");
  a:count select from workorder where date=d;
  w:select date,id_server,id_work,id_target,venue,make
    from workorder where date=d, any (upper venue) like/: dk;
  b:count w;
  c:count select from w where make>0;
  ids:exec distinct id_target from w;
  x:select date,id_server,id_target,country from target_stock
    where date=d, id_target in ids;
  e:count x;
  f:$[0=count ctry; e; count select from x where country=`$ctry];
  ([] stage:`workorder_rows`dark_venue_rows`of_those_filled`stock_rows`after_country;
      n:(a;b;c;e;f))
 }
"""

# The country values actually present, so a filter that matched nothing can be
# compared against what was there to match.
Q_COUNTRIES = """
{[d]
  w:select date,id_server,id_target from workorder
    where date=d, any (upper venue) like/: ("*DARK*";"*DRK*");
  ids:exec distinct id_target from w;
  `n xdesc 0!select n:count i by country from target_stock
    where date=d, id_target in ids
 }
"""

# Every column is a plain sum, so folding a day in is one frame addition and
# memory stays flat whether you ask for a day or a quarter.
ACC = [
    "orders_routed", "orders_filled",
    "shares_routed", "shares_executed",
    "notional_routed", "notional_executed",
]


# -----------------------------------------------------------------------------
# kdb IO
# -----------------------------------------------------------------------------

def parse_hostport(s):
    """'host:port' -> ('host', port).  Raises on anything else."""
    if ":" not in s:
        raise ValueError(f"expected host:port, got {s!r}")
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {s!r}")
    return host, int(port)


def connect(hostport):
    """Open a PyKX connection on a host and a port; the server is open, so there
    is nothing to log in with.  pykx is imported here, not at module level, so
    the pure-python half of this file stays importable without it."""
    if hostport.startswith(_PLACEHOLDER):
        raise SystemExit(
            f"{hostport!r} is still the placeholder.  Set ORDER_SERVER in a "
            f"local_settings.py beside this script, or near the top of "
            f"{__file__}."
        )
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Only IPC is needed here, so unlicensed mode is enough - no q "
            "licence and no QHOME required."
        )
    host, port = parse_hostport(hostport)
    return pykx.SyncQConnection(host=host, port=port)


def _to_pandas(tbl):
    """PyKX table -> DataFrame, with symbol columns normalised to str.

    PyKX hands symbols back as bytes in some versions and str in others.  Left
    alone that difference turns up much later as a groupby that splits one
    venue into two, so it is flattened here at the boundary."""
    df = tbl.pd()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: v.decode() if isinstance(v, bytes) else v)
    return df


def fetch_day(ho, day, country):
    """One date's routed/executed roll, one row per (country, venue)."""
    return _to_pandas(ho(Q_ROUTED_EXECUTED, day, country))


# -----------------------------------------------------------------------------
# Applying the venue sheet
# -----------------------------------------------------------------------------

def venue_labels(df):
    """The display group for every row of the roll.

    A (country, venue) pair the sheet does not carry keeps its raw kdb symbol,
    so a venue nobody has added yet is still its own slice - visible, in the
    right total, and obviously un-prettified - rather than vanishing or landing
    in somebody else's pool.  unmapped_venues() names them.

    A frame with no country column maps on ("", venue), which no sheet row
    matches, so it falls through to the raw name.  That is what lets the
    synthetic frames in the self-test stay independent of the sheet."""
    venue = df["venue"].astype(str)
    country = (df["country"].astype(str) if "country" in df.columns
               else pd.Series("", index=df.index, dtype=object))
    labels = [VENUE_GROUPS.get((c, v), (v, v))[0] for c, v in zip(country, venue)]
    return pd.Series(labels, index=df.index, name=GROUP_COL)


def unmapped_venues(df):
    """The (country, venue) pairs in this frame the sheet does not carry.

    Collected per day and reported once, so a quarter of dates does not print
    the same missing venue ninety times."""
    if len(df) == 0 or "venue" not in df.columns:
        return set()
    venue = df["venue"].astype(str)
    country = (df["country"].astype(str) if "country" in df.columns
               else pd.Series("", index=df.index, dtype=object))
    return {p for p in zip(country, venue) if p not in VENUE_GROUPS}


def pie_label(group):
    """The short name a pie slice is labelled with.

    An unmapped venue has no short name, so it keeps its kdb symbol - long and
    ALL_CAPS next to CentrePt and MSPL, which is a legible way to be told the
    sheet needs a line adding."""
    return SHORT_NAMES.get(group, group)


# -----------------------------------------------------------------------------
# Accumulation
# -----------------------------------------------------------------------------

def aggregate(df):
    """One day's roll folded onto the group (index = the sheet's name).

    q returns this one row per (country, venue), so a group built out of two
    symbols arrives as TWO rows and they are summed here.  Indexing on the
    group instead would keep whichever row landed last and silently drop the
    other venue's whole notional."""
    if len(df) == 0:
        return pd.DataFrame(columns=ACC, dtype=float)
    out = df.assign(**{GROUP_COL: venue_labels(df)})
    cols = [c for c in ACC if c in out.columns]
    out = out.groupby(GROUP_COL, dropna=False)[cols].sum()
    return out.reindex(columns=ACC).astype(float)


def fold(acc, day):
    """Add a day's per group sums into the accumulator."""
    if day is None or len(day) == 0:
        return acc
    if acc is None:
        return day.copy()
    return acc.add(day, fill_value=0.0)


def _safe_div(a, b):
    """a/b, NaN where b is zero or missing - never inf, never a warning."""
    b = pd.Series(b, dtype=float)
    return pd.Series(np.where((b != 0) & b.notna(), a / b.where(b != 0), np.nan),
                     index=b.index)


# -----------------------------------------------------------------------------
# The table
# -----------------------------------------------------------------------------

def build_table(acc):
    """Routed against executed, per group.

    Every percentage divides the ACCUMULATED totals.  Averaging daily
    percentages would weight a quiet Tuesday the same as a heavy Thursday, and
    the two answers differ by a lot more than rounding whenever the venue mix
    moves during the range."""
    routed = acc["notional_routed"]
    executed = acc["notional_executed"]
    out = pd.DataFrame(index=acc.index)
    out["Routed %"] = 100.0 * routed / routed.sum() if routed.sum() else np.nan
    out["Executed %"] = 100.0 * executed / executed.sum() if executed.sum() else np.nan
    out["Fill Rate"] = _safe_div(100.0 * executed, routed)
    out["Orders"] = acc["orders_routed"]
    out["Filled"] = acc["orders_filled"]
    out["Routed $m"] = routed / 1e6
    out["Executed $m"] = executed / 1e6
    out.index.name = "Venue"
    # heaviest first, the way the q sorts it
    return out.sort_values("Routed $m", ascending=False)


TABLE_FMT = (
    ("Routed %", "{:.1f}"), ("Executed %", "{:.1f}"), ("Fill Rate", "{:.1f}"),
    ("Orders", "{:,.0f}"), ("Filled", "{:,.0f}"),
    ("Routed $m", "{:,.1f}"), ("Executed $m", "{:,.1f}"),
)


def format_table(t, fmt):
    """Numeric frame -> every cell a string, NaN as blank."""
    cols = [c for c, _ in fmt]
    d = pd.DataFrame(index=t.index)
    for c, spec in fmt:
        d[c] = t[c].map(lambda v, s=spec: "" if pd.isna(v) else s.format(v))
    return d[cols]


# -----------------------------------------------------------------------------
# The pies
# -----------------------------------------------------------------------------

def pie_series(pct, other_below):
    """One pie's slices: short codes, small shares rolled into Other.

    The report's Executed pie is Ctrpnt 88.5, Other 7.2, MSPL 4.3 - and that
    7.2 is exactly CLSA 1.7 + JPMX 2.6 + Posit 2.9 out of table 3.1.  The 3.0
    default reproduces it: MS Pool at 4.3 stands, the three below it roll up,
    and every slice of the Routed pie (smallest 6.1) is left alone.

    Rolled up BEFORE rounding, so Other is the true remainder rather than a sum
    of three already-rounded numbers.  Sorted descending with Other in its
    natural place, which is where the report puts it."""
    pct = pd.Series(pct, dtype=float).dropna()
    keep, rest = pct[pct >= other_below], pct[pct < other_below]
    rows = [(pie_label(k), float(v)) for k, v in keep.items()]
    if len(rest):
        rows.append((OTHER, float(rest.sum())))
    rows.sort(key=lambda r: -r[1])
    return [(n, round(v, 1)) for n, v in rows]


def write_pie_csv(path, rows):
    """The two-column format latex_pie/pie_slide.py reads, so the pies can be
    redrawn or hand-edited without going back to kdb."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "percentage"])
        for name, value in rows:
            w.writerow([name, f"{value:g}"])


# Office pastel palette, sampled from the original slide and then extended.
# Same values as latex_pie/pie_slide.py, so a pie drawn here and one drawn
# there are the same picture.
PALETTE = [
    "#9FDCC9",  # teal
    "#F2EDA0",  # yellow
    "#C0BCE8",  # lavender
    "#EE7B6B",  # red
    "#8AB8DE",  # blue
    "#F3C48E",  # orange
    "#A8D08D",  # green
    "#D9A7C7",  # pink
    "#BFBFBF",  # grey
]

EDGE = "#5A5A5A"       # wedge outline
INK = "#3F3F3F"        # label / title text
LEADER = "#7F7F7F"     # leader-line grey

LABEL_FONTSIZE = 11
MIN_LABEL_GAP = 0.17   # data units; stops labels stacking on top of each other


def build_color_map(*series_list):
    """One colour per slice name, shared across both pies.

    Assigned by name rather than by position, so a venue appearing in both pies
    keeps its colour in both - which is the only reason the pair can be read
    side by side at all."""
    colors = {}
    for rows in series_list:
        for name, _ in rows:
            if name not in colors:
                colors[name] = PALETTE[len(colors) % len(PALETTE)]
    return colors


def place_labels(wedges):
    """Radial label positions, then a de-collision pass down each side."""
    from math import cos, radians, sin
    placed = []
    for index, wedge in enumerate(wedges):
        ang = (wedge.theta1 + wedge.theta2) / 2.0
        x, y = cos(radians(ang)), sin(radians(ang))
        side = 1 if x >= 0 else -1
        tx = x * 1.35
        if abs(tx) < 1.15:            # keep the text clear of the pie itself
            tx = side * 1.15
        placed.append({"i": index, "tip": (x, y), "tx": tx, "ty": y * 1.30,
                       "side": side})

    for side in (1, -1):
        column = sorted((p for p in placed if p["side"] == side),
                        key=lambda p: -p["ty"])
        for above, current in zip(column, column[1:]):
            if current["ty"] > above["ty"] - MIN_LABEL_GAP:
                current["ty"] = above["ty"] - MIN_LABEL_GAP
        # if the column overflowed the bottom, push the whole run back up
        if column and column[-1]["ty"] < -1.42:
            shift = -1.42 - column[-1]["ty"]
            for p in column:
                p["ty"] += shift

    return {p["i"]: p for p in placed}


def office_pie(ax, rows, color_map, title, startangle):
    """One Office-style pie with outside 'Name,Value' leader labels."""
    values = [value for _, value in rows]
    colors = [color_map[name] for name, _ in rows]
    labels = [f"{name},{value:g}" for name, value in rows]

    wedges, _ = ax.pie(
        values,
        colors=colors,
        startangle=startangle,
        counterclock=False,                      # Office pies always run clockwise
        wedgeprops=dict(edgecolor=EDGE, linewidth=0.8),
        radius=1.0,
    )

    positions = place_labels(wedges)
    for index, label in enumerate(labels):
        spot = positions[index]
        ax.annotate(
            label,
            xy=spot["tip"],                                  # tip touches the arc
            xytext=(spot["tx"], spot["ty"]),
            ha="left" if spot["side"] > 0 else "right",
            va="center",
            fontsize=LABEL_FONTSIZE,
            color=INK,
            annotation_clip=False,
            arrowprops=dict(arrowstyle="-", color=LEADER, linewidth=0.8,
                            shrinkA=0, shrinkB=2),
        )

    if title:
        ax.set_title(title, fontsize=15, fontweight="bold", color=INK, pad=18)
    ax.set(aspect="equal")
    ax.set_xlim(-1.9, 1.9)
    ax.set_ylim(-1.5, 1.5)


def write_pies(png_path, routed_rows, executed_rows, startangle=90.0):
    """Both pies on one canvas, written as .png and as .pdf beside it.

    The .pdf is the one to put in a document - it is vector, so it stays sharp
    at any size.  matplotlib is imported here, not at module level, so the
    script still runs and still self-tests on a machine without it."""
    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError:
        raise SystemExit("drawing the pies needs matplotlib.  pip install matplotlib")

    # a bare Figure with an explicit canvas: no pyplot, so no backend is
    # selected and nothing tries to find a display
    fig = Figure(figsize=(13.33, 5.6), dpi=200)
    FigureCanvasAgg(fig)
    fig.patch.set_facecolor("white")
    for ax in (fig.add_subplot(1, 2, 1), fig.add_subplot(1, 2, 2)):
        ax.set_axis_off()
    axes = fig.axes

    color_map = build_color_map(routed_rows, executed_rows)
    office_pie(axes[0], routed_rows, color_map, "Routed %", startangle)
    office_pie(axes[1], executed_rows, color_map, "Executed %", startangle)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.04)

    import os
    fig.savefig(png_path, facecolor="white")
    pdf_path = os.path.splitext(png_path)[0] + ".pdf"
    fig.savefig(pdf_path, facecolor="white")
    return png_path, pdf_path


# -----------------------------------------------------------------------------
# Progress.  Goes to stderr and is flushed line by line, so a long range says
# what it is doing WHILE it does it and the report on stdout stays pipeable.
# -----------------------------------------------------------------------------

QUIET = False


def log(msg=""):
    if not QUIET:
        print(msg, file=sys.stderr, flush=True)


def _hms(secs):
    return f"{secs:.1f}s" if secs < 60 else f"{int(secs)//60}m {int(secs)%60:02d}s"


def daterange(d0, d1):
    d = d0
    while d <= d1:
        yield d
        d += dt.timedelta(days=1)


def diagnose(ho, day, ctry, country_label):
    """Where one date's rows disappear, stage by stage."""
    print(f"diagnosing {day}\n")
    funnel = _to_pandas(ho(Q_DIAG, day, ctry))
    width = max(len(str(s)) for s in funnel["stage"])
    prev = None
    for _, r in funnel.iterrows():
        n = int(r["n"])
        share = "" if not prev else f"   {100.0 * n / prev:5.1f}% of previous"
        gone = "   <- everything dropped here" if n == 0 and prev else ""
        print(f"  {str(r['stage']):<{width}}  {n:>12,}{share}{gone}")
        prev = n
    print()
    if int(funnel["n"].iloc[0]) == 0:
        print(f"  no workorder rows at all on {day} - a non-trading date, or a "
              f"date the HDB does not hold.\n  Re-run --diagnose with a --start "
              f"you know traded before reading anything into the rest.")
        return 0
    ctry_rows = _to_pandas(ho(Q_COUNTRIES, day))
    if len(ctry_rows) == 0:
        print("  no stock rows for that date, so no countries to compare against")
    else:
        print(f"  countries on {day}, by dark parent orders:")
        for _, r in ctry_rows.head(20).iterrows():
            got = str(r["country"])
            mine = "   <- your --country" if country_label and got == country_label else ""
            print(f"    {got:<12} {int(r['n']):>8,}{mine}")
        if country_label and country_label not in [str(v) for v in ctry_rows["country"]]:
            print(f"\n  --country {country_label} is not among them, which is why "
                  f"the range came back empty.")
    return 0


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

def run(args):
    global QUIET
    QUIET = args.quiet

    days = list(daterange(args.start, args.end))
    log(f"dark_routed_executed  {args.start} to {args.end}  ({len(days)} dates)"
        + (f", country {args.country}" if args.country else ", all countries"))
    log(f"  order server  {ORDER_SERVER} ...")
    ho = connect(ORDER_SERVER)
    # BYTES, not str: PyKX sends a python str as a q symbol, and the q casts
    # with `$, which is a 'type error on a symbol.  b"" is an empty char
    # vector, so `0=count ctry` still selects every country.
    country = (args.country or "").encode()

    if args.diagnose:
        return diagnose(ho, days[0], country, args.country)

    log("")
    acc, unmapped = None, set()
    n_ok = n_empty = n_failed = 0
    t_run = time.perf_counter()
    for i, day in enumerate(days, start=1):
        t0 = time.perf_counter()
        tag = f"  [{i:>3}/{len(days)}] {day}"
        try:
            roll = fetch_day(ho, day, country)
        except Exception as exc:                      # noqa: BLE001
            n_failed += 1
            log(f"{tag}  FAILED - {exc}")
            continue
        took = time.perf_counter() - t0
        if len(roll) == 0:
            n_empty += 1
            log(f"{tag}  no dark child orders   {took:5.1f}s")
            continue
        n_ok += 1
        unmapped |= unmapped_venues(roll)
        acc = fold(acc, aggregate(roll))
        log(f"{tag}  {len(roll):>5,} venue rows   {took:5.1f}s")

    log("")
    log(f"  {len(days)} dates in {_hms(time.perf_counter() - t_run)}: {n_ok} with "
        f"rows, {n_empty} empty, {n_failed} failed")

    if acc is None or len(acc) == 0:
        raise SystemExit(
            f"\nno dark child orders across {len(days)} dates"
            + (f" for country {args.country}" if args.country else "")
            + (f", and {n_failed} date(s) errored - see above" if n_failed else "")
            + "\nrun the same command with --diagnose to see which filter empties it.")

    table = build_table(acc)
    routed_rows = pie_series(table["Routed %"], args.other_below)
    executed_rows = pie_series(table["Executed %"], args.other_below)

    print(f"\nDark routed vs executed {args.start} to {args.end}"
          + (f", country {args.country}" if args.country else ""))
    if unmapped:
        print(f"\n  {len(unmapped)} venue(s) are not in VENUE_GROUPS, so they keep "
              f"their raw kdb name below.\n  Add them to the sheet near the top "
              f"of this script to group them:")
        for c, v in sorted(unmapped):
            print(f'    ("{c}", "{v}"):')
    print()
    print(format_table(table, TABLE_FMT).to_string())
    print(f"\n  Routed $m {table['Routed $m'].sum():,.1f}   "
          f"Executed $m {table['Executed $m'].sum():,.1f}   "
          f"overall fill rate {100.0 * acc['notional_executed'].sum() / acc['notional_routed'].sum():.1f}%")
    print(f"\nPie slices, anything under {args.other_below:g}% rolled into {OTHER}\n")
    for title, rows in (("Routed %", routed_rows), ("Executed %", executed_rows)):
        print(f"  {title}: " + ", ".join(f"{n},{v:g}" for n, v in rows))

    if args.out_dir:
        import os
        os.makedirs(args.out_dir, exist_ok=True)
        written = []
        p = os.path.join(args.out_dir, "dark_routed_executed.csv")
        table.to_csv(p)
        written.append(p)
        for name, rows in (("routed.csv", routed_rows), ("executed.csv", executed_rows)):
            p = os.path.join(args.out_dir, name)
            write_pie_csv(p, rows)
            written.append(p)
        if not args.no_pies:
            written.extend(write_pies(os.path.join(args.out_dir, "pies.png"),
                                      routed_rows, executed_rows))
        print()
        for p in written:
            print(f"written to {p}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Dark routed vs executed by venue, with the two pies",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start", type=dt.date.fromisoformat)
    p.add_argument("--end", type=dt.date.fromisoformat)
    p.add_argument("--country", default="", help="target_stock country, e.g. AU; blank for all")
    p.add_argument("--other-below", type=float, default=3.0,
                   help="pie slices under this percentage roll into one Other "
                        "slice; 0 keeps every venue as its own slice")
    p.add_argument("--out-dir", help="write the table, the two pie CSVs and "
                                     "pies.png/.pdf here")
    p.add_argument("--no-pies", action="store_true",
                   help="write the CSVs but do not draw; use on a machine "
                        "without matplotlib")
    p.add_argument("--diagnose", action="store_true",
                   help="query the FIRST date only and show where its rows are "
                        "lost, stage by stage; use when a range reports nothing")
    p.add_argument("--quiet", action="store_true",
                   help="no per-date progress on stderr; the report still prints")
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in tests; needs no kdb connection")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    missing = [n for n in ("start", "end") if getattr(args, n) is None]
    if missing:
        p.error("required unless --self-test: " + ", ".join("--" + m.replace("_", "-")
                                                            for m in missing))
    return run(args)


# -----------------------------------------------------------------------------
# Built-in tests.  Everything except the three q constants is pure python and
# is checked here, so the script can be verified on a machine with no kdb.
# -----------------------------------------------------------------------------

def _synth_roll(venues, country=None, scale=1.0):
    """One day's roll, one row per venue, with plausible fill rates."""
    n = len(venues)
    df = pd.DataFrame({
        "venue": list(venues),
        "orders_routed": np.arange(10, 10 + n, dtype=float) * scale,
        "orders_filled": np.arange(4, 4 + n, dtype=float) * scale,
        "shares_routed": np.arange(1000, 1000 + n, dtype=float) * scale,
        "shares_executed": np.arange(300, 300 + n, dtype=float) * scale,
        "notional_routed": np.arange(1, 1 + n, dtype=float) * 1e6 * scale,
        "notional_executed": np.arange(1, 1 + n, dtype=float) * 3e5 * scale,
    })
    if country is not None:
        df["country"] = country
    return df


def test_venue_sheet_is_consistent():
    """The sheet is hand typed off a screenshot, so its shape is checked here
    rather than trusted.

    ONE SHORT CODE PER NAME is the one that matters beyond tidiness: a pool
    spelled two ways would draw as two slices of a pie that has one."""
    short_of = {}
    for key, val in VENUE_GROUPS.items():
        assert isinstance(key, tuple) and len(key) == 2, key
        country, venue = key
        assert country.isalpha() and country == country.upper(), key
        assert venue == venue.upper(), key
        # this script only ever sees venues matching this, so a typo here is a
        # row that can never match anything
        assert ("DARK" in venue) or ("DRK" in venue), key
        assert len(val) == 2, (key, val)
        name, short = val
        assert name and short, (key, val)
        assert short_of.setdefault(name, short) == short, (
            f"{name} has two short codes: {short_of[name]} and {short}")
    # and the sheet must agree with the copy the other script carries
    assert SHORT_NAMES["Centrepoint"] == "CentrePt"
    assert SHORT_NAMES["MS Pool"] == "MSPL"


def test_venues_in_one_group_become_one_row():
    """CENTREPOINT_DARK and CENTREPOINT_CITI_DARK are two routes into one pool,
    and the report draws ONE Ctrpnt slice.  Both notionals have to be in it."""
    roll = _synth_roll(["CENTREPOINT_DARK", "CENTREPOINT_CITI_DARK"], country="AU")
    acc = aggregate(roll)
    assert list(acc.index) == ["Centrepoint"], list(acc.index)
    assert acc.loc["Centrepoint", "notional_routed"] == 3e6      # 1m + 2m
    assert acc.loc["Centrepoint", "orders_routed"] == 21.0       # 10 + 11
    table = build_table(acc)
    assert abs(table.loc["Centrepoint", "Routed %"] - 100.0) < 1e-12


def test_the_sheet_is_keyed_on_country():
    """JPMAP_DARK is JPMX in JP and HK; in AU the same pool is JPMAP_MF_DARK.
    Neither spelling maps in the other's country, which is why the key is a
    pair rather than a venue name."""
    roll = pd.concat([_synth_roll(["JPMAP_DARK"], country="JP"),
                      _synth_roll(["JPMAP_DARK"], country="HK"),
                      _synth_roll(["JPMAP_MF_DARK"], country="AU")],
                     ignore_index=True)
    assert list(aggregate(roll).index) == ["JPMX"]
    crossed = pd.DataFrame({"country": ["JP", "AU"],
                            "venue": ["JPMAP_MF_DARK", "JPMAP_DARK"]})
    assert list(venue_labels(crossed)) == ["JPMAP_MF_DARK", "JPMAP_DARK"]
    assert unmapped_venues(crossed) == {("JP", "JPMAP_MF_DARK"),
                                        ("AU", "JPMAP_DARK")}


def test_unmapped_venue_keeps_its_kdb_name():
    """A venue the sheet has not caught up with must stay visible under its own
    symbol.  Dropping it would take it out of the denominator too, so every
    other slice would quietly grow."""
    roll = _synth_roll(["BRAND_NEW_DARK"], country="AU")
    assert list(aggregate(roll).index) == ["BRAND_NEW_DARK"]
    assert unmapped_venues(roll) == {("AU", "BRAND_NEW_DARK")}
    assert unmapped_venues(_synth_roll(["MS_DARK"], country="AU")) == set()
    assert list(aggregate(_synth_roll(["MS_DARK"], country="AU")).index) == ["MS Pool"]
    # having no short name, it labels a slice with its own kdb symbol
    assert pie_label("BRAND_NEW_DARK") == "BRAND_NEW_DARK"


def test_unmapped_venue_is_not_exempt_from_other():
    """An unmapped venue is a venue.  It rolls into Other below the threshold
    like any other thin one - the table above the pies and the notice above
    that are what always name it, not a sliver on a chart."""
    pct = pd.Series({"Centrepoint": 88.5, "MS Pool": 8.0, "BRAND_NEW_DARK": 1.5,
                     "CLSA": 2.0})
    rows = dict(pie_series(pct, 3.0))
    assert "BRAND_NEW_DARK" not in rows, rows
    assert rows[OTHER] == 3.5, rows                       # 1.5 + 2.0
    # above the threshold it stands on its own, under its kdb name
    assert "BRAND_NEW_DARK" in dict(pie_series(pct, 1.0))


def test_percentages_come_from_totals():
    """Percentages must divide the accumulated notionals, never average the
    daily ones.

    Two days: on day one A is all of the flow, on day two B is nine tenths of
    a ten times bigger day.  The mean of the daily shares would put A at 50%;
    the truth is that A took 1m of an 11m fortnight, which is 9.1%."""
    day1 = pd.DataFrame({"venue": ["A_DARK"], "country": ["AU"],
                         "orders_routed": [1.0], "orders_filled": [1.0],
                         "shares_routed": [1.0], "shares_executed": [1.0],
                         "notional_routed": [1e6], "notional_executed": [1e6]})
    day2 = pd.DataFrame({"venue": ["B_DARK"], "country": ["AU"],
                         "orders_routed": [1.0], "orders_filled": [1.0],
                         "shares_routed": [1.0], "shares_executed": [1.0],
                         "notional_routed": [10e6], "notional_executed": [10e6]})
    acc = fold(fold(None, aggregate(day1)), aggregate(day2))
    table = build_table(acc)
    assert abs(table.loc["A_DARK", "Routed %"] - 100.0 / 11.0) < 1e-9
    assert abs(table.loc["B_DARK", "Routed %"] - 1000.0 / 11.0) < 1e-9
    # the wrong answer, pinned so the difference is not mistaken for rounding
    assert abs(table.loc["A_DARK", "Routed %"] - 50.0) > 40.0


def test_chunking_is_exact():
    """Folding day by day must give the same answer as one pass.  This is the
    property the whole accumulator design rests on."""
    days = [_synth_roll(["MS_DARK", "CLSA_DARK", "CENTREPOINT_DARK",
                         "CENTREPOINT_CITI_DARK"], country="AU", scale=s)
            for s in (1.0, 2.5, 0.3, 7.0)]
    chunked = None
    for d in days:
        chunked = fold(chunked, aggregate(d))
    one_pass = aggregate(pd.concat(days, ignore_index=True))
    chunked, one_pass = chunked.sort_index(), one_pass.sort_index()
    assert list(chunked.index) == list(one_pass.index)
    for c in ACC:
        assert np.allclose(chunked[c], one_pass[c], rtol=1e-12, atol=1e-6), c


def test_fill_rate_is_money_weighted():
    """Fill Rate is executed notional over routed notional, per group - not the
    share of orders that filled, which is a different number whenever one venue
    gets the big orders."""
    roll = pd.DataFrame({"venue": ["MS_DARK"], "country": ["AU"],
                         "orders_routed": [100.0], "orders_filled": [1.0],
                         "shares_routed": [1.0], "shares_executed": [1.0],
                         "notional_routed": [1000.0], "notional_executed": [80.0]})
    table = build_table(aggregate(roll))
    assert abs(table.loc["MS Pool", "Fill Rate"] - 8.0) < 1e-12
    # a venue that routed nothing valuable gets a blank, not an infinity
    zero = roll.assign(notional_routed=[0.0])
    assert pd.isna(build_table(aggregate(zero)).loc["MS Pool", "Fill Rate"])


def test_other_reproduces_the_published_pie():
    """The published Executed pie is Ctrpnt 88.5, Other 7.2, MSPL 4.3, and its
    Other is exactly table 3.1's CLSA 1.7 + JPMX 2.6 + Posit 2.9.  That is what
    the 3.0 default has to reproduce - and it must leave the Routed pie, whose
    smallest slice is 6.1, with all five venues showing."""
    executed = pd.Series({"Centrepoint": 88.5, "CLSA": 1.7, "JPMX": 2.6,
                          "MS Pool": 4.3, "Posit": 2.9})
    assert pie_series(executed, 3.0) == [("CentrePt", 88.5), (OTHER, 7.2),
                                         ("MSPL", 4.3)]
    routed = pd.Series({"Centrepoint": 46.6, "CLSA": 16.6, "JPMX": 15.7,
                        "Posit": 15.1, "MS Pool": 6.1})
    assert [n for n, _ in pie_series(routed, 3.0)] == ["CentrePt", "CLSA",
                                                       "JPMX", "Posit", "MSPL"]
    # 0 keeps every venue, however thin
    assert len(pie_series(executed, 0.0)) == 5
    assert OTHER not in [n for n, _ in pie_series(executed, 0.0)]


def test_other_is_summed_before_rounding():
    """Three slices of 0.04 are 0.1 together, not 0.0 three times over."""
    tiny = pd.Series({"CLSA": 0.04, "Posit": 0.04, "JPMX": 0.04, "MS Pool": 99.88})
    rows = dict(pie_series(tiny, 3.0))
    assert rows[OTHER] == 0.1, rows


def test_pie_csv_matches_the_latex_pie_format():
    """The CSVs have to drop straight into latex_pie/pie_slide.py, which reads
    'name,percentage' rows and skips anything whose second field is not a
    number.  Read back the way that script reads them."""
    import os
    import tempfile
    rows = pie_series(pd.Series({"Centrepoint": 88.5, "CLSA": 1.7, "JPMX": 2.6,
                                 "MS Pool": 4.3, "Posit": 2.9}), 3.0)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "executed.csv")
        write_pie_csv(p, rows)
        text = open(p, encoding="utf-8").read()
        back = []
        for fields in csv.reader(open(p, newline="", encoding="utf-8-sig")):
            fields = [f.strip() for f in fields if f.strip() != ""]
            if len(fields) < 2:
                continue
            try:
                back.append((fields[0], float(fields[1])))
            except ValueError:
                continue                       # the header row
    assert text.splitlines()[0] == "name,percentage", text.splitlines()[0]
    assert back == rows, (back, rows)


def test_pies_are_written():
    """End to end: two real image files with both pies on them."""
    try:
        import matplotlib                                      # noqa: F401
    except ImportError:
        print("        (matplotlib not installed, skipped)")
        return
    import os
    import tempfile
    routed = pie_series(pd.Series({"Centrepoint": 46.6, "CLSA": 16.6,
                                   "JPMX": 15.7, "Posit": 15.1,
                                   "MS Pool": 6.1}), 3.0)
    executed = pie_series(pd.Series({"Centrepoint": 88.5, "CLSA": 1.7,
                                     "JPMX": 2.6, "MS Pool": 4.3,
                                     "Posit": 2.9}), 3.0)
    with tempfile.TemporaryDirectory() as d:
        png, pdf = write_pies(os.path.join(d, "pies.png"), routed, executed)
        blob = open(pdf, "rb").read()
        assert os.path.getsize(png) > 10_000, os.path.getsize(png)
    assert blob[:5] == b"%PDF-", blob[:16]
    # every slice of both pies must be in one shared colour map, and no two
    # slices may share a colour - that map by NAME is what makes CentrePt the
    # same colour on the left as on the right
    cmap = build_color_map(routed, executed)
    names = {n for n, _ in routed} | {n for n, _ in executed}
    assert names <= set(cmap), names - set(cmap)
    assert len(set(cmap.values())) == len(cmap), cmap


def test_table_formats_like_the_report():
    acc = aggregate(_synth_roll(["MS_DARK", "CLSA_DARK", "POSITNOW_DARK"],
                                country="AU"))
    d = format_table(build_table(acc), TABLE_FMT)
    assert list(d.columns) == [c for c, _ in TABLE_FMT]
    assert len(d["Routed %"].iloc[0].split(".")[1]) == 1        # 1dp, as published
    # a NaN must render blank, never the string 'nan'
    t = build_table(acc)
    t.loc[t.index[0], "Fill Rate"] = np.nan
    assert format_table(t, TABLE_FMT)["Fill Rate"].iloc[0] == ""


def test_country_reaches_q_as_chars():
    """The country filter must arrive as a char vector, never a str.

    PyKX sends a python str as a q SYMBOL, and the q casts it with `$, which is
    a 'type error on a symbol - so a str here fails on every single date rather
    than on the first thing anyone would look at."""
    sent = []

    class Result:                      # what _to_pandas expects back
        def pd(self):
            return pd.DataFrame()

    class Handle:
        def __call__(self, qsql, *args):
            sent.append(args)
            return Result()

    fetch_day(Handle(), dt.date(2026, 4, 1), "AU".encode())
    assert sent, "fetch_day sent nothing"
    for args in sent:
        assert isinstance(args[1], bytes), (
            f"country reached q as {type(args[1]).__name__}, which PyKX "
            f"converts to a symbol; send bytes so it arrives as chars")


def test_parse_hostport():
    assert parse_hostport("h:5010") == ("h", 5010)
    for bad in ("h", "h:", ":5010", "h:abc"):
        try:
            parse_hostport(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


def test_server_constant():
    """Once edited, the connection constant must parse as host:port.

    Worth a test because this script is written on a machine with no kdb and
    run on one that has it: a typo here would otherwise surface as a connection
    failure on the far side, long after the edit.  Still holding the
    placeholder is fine - connect() catches that with its own message."""
    if ORDER_SERVER.startswith(_PLACEHOLDER):
        print("        (ORDER_SERVER not set yet)")
        return
    try:
        parse_hostport(ORDER_SERVER)
    except ValueError as exc:
        raise AssertionError(f"ORDER_SERVER={ORDER_SERVER!r}: {exc}")


def self_test():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main() or 0)


# =============================================================================
# WHERE THE JUDGEMENT CALLS ARE
#
# 1. ROUTED IS VALUED AT THE PRICE THE CHILD WAS SENT WITH, falling back to the
#    last trade at transmit time for market and pegged orders that carry no
#    usable limit.  workorder also carries limit_target, limit_candidate,
#    transmit_bidprice and transmit_askprice if you would rather value it
#    another way - the px_routed line in the q is the only place that decides.
#
# 2. NO make>0 FILTER.  The children that never filled are exactly what makes
#    routed differ from executed; filtering them would collapse the two pies
#    onto each other and make Fill Rate 100% everywhere.
#
# 3. AN ij ONTO target_stock, not the lj the .q uses.  With --country set, a
#    child whose parent is in another country has to be dropped, not kept with
#    a null fx that silently zeroes its notional.  With no --country the two
#    joins are the same.
#
# 4. FILL RATE IS MONEY WEIGHTED - executed notional over routed notional.
#    orders_filled/orders_routed is the order weighted version and is in the
#    table beside it as Orders and Filled; the two diverge whenever one venue
#    is getting the big orders.
#
# 5. PERCENTAGES DIVIDE THE ACCUMULATED TOTALS, once, at the end.  A mean of
#    daily percentages weights a quiet Tuesday like a heavy Thursday, and the
#    difference is not a rounding.  test_percentages_come_from_totals pins it.
#
# 6. --other-below DEFAULTS TO 3.0 because that is what reproduces the report:
#    it rolls CLSA, JPMX and Posit into the 7.2 Other slice of the Executed pie
#    while leaving all five slices of the Routed pie standing.  It is a display
#    threshold only - the table above the pies always shows every venue, and
#    Other never enters a calculation.
#
# 7. AN UNMAPPED VENUE KEEPS ITS KDB SYMBOL rather than being dropped.  Dropped
#    it would leave the denominator, so every other row and slice would quietly
#    grow.  Where it is always visible is the TABLE and the notice above it;
#    --other-below still applies to it on the pie, so a thin stray venue rolls
#    into Other there like any other thin venue.  Exempting it would put an
#    ALL_CAPS sliver on a chart to say something the notice already says.
# =============================================================================
