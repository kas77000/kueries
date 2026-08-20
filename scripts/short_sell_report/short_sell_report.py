#!/usr/bin/env python3
"""
=============================================================================
short_sell_report.py

Short-Sell Order Report: every short sell parent order of the session, its
overall completion and its rejected child splits, summarised by market, as a
one page PDF (and the same page as a PNG).

  python scripts/short_sell_report/short_sell_report.py
  python scripts/short_sell_report/short_sell_report.py --monthly 2026-07

The default run reads the REALTIME order server, so the report is a snapshot
of the session as it stands.  --monthly reads the HISTORICAL one, a date at a
time, and adds two per day charts.  The two servers hold the same tables; the
historical ones carry an extra `date` column, and that is the only difference
the queries have to care about - see Q_SESSION.

MARKETS.  Hong Kong, Japan, Korea, Malaysia and Thailand, always all five and
always in that order, so a market with no short sell flow reads as a zero
rather than as a missing row.  Anything else on the book is out of scope.

The market is the SYM SUFFIX - .HK .JP .KS .MK .TB - so the report reads two
tables and joins nothing: target for the parents, workorder for their children.

JAPAN.  Restricted names are excluded: a parent whose fixmsg carries RSHO=1 is
dropped before anything is counted, so it appears in neither the order count,
the quantities, nor the rejections.

WHAT THE THREE NUMBERS MEAN

  Orders       parent short sell orders - target rows with side=`sellshort.
               A target IS an order, so this is a row count
  Completion   executed / order qty, quantity weighted.  Order qty is the sum
               of `size` over the targets in that market - `size` IS the
               order's quantity, taken as it stands; executed is the sum of
               workorder `make` - a workorder is a child order and `make` is
               what it executed, whatever state it ended in.  The headline figure is the same ratio
               taken over all five markets at once, NOT the average of the
               five percentages - a market with 500 orders should not weigh
               the same as one with 5.
  Rejections   workorder rows whose state is `rejected`.  Counted per child
               order rather than per parent, which is why one order can
               contribute several, and why a market can show more rejections
               than it has orders.

pykx is imported lazily inside connect(), so the analytics and the whole
rendering path run on a machine with no kdb, no pykx and no q licence:

  python scripts/short_sell_report/short_sell_report.py --self-test

Everything between the query and the page is a pure function over plain
records, so --self-test exercises the market rollup, the day series, the Japan
exclusion and the page itself without a connection.
=============================================================================
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import fnmatch
import sys
from pathlib import Path
from typing import NamedTuple, Optional

# scripts/lib holds the pieces that are not about this report - the mailer, for
# one.  Added to the path rather than installed, so the script still runs as
# `python scripts/short_sell_report/short_sell_report.py` from the repo root.
# Copy scripts/lib alongside this folder if you move it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# -----------------------------------------------------------------------------
# CONNECTIONS.  Edit these.
#
# Two flavours of the SAME order server.  The default report reads the realtime
# one; --monthly and --date read the historical one.  Both are open processes -
# host and port is the whole of it, the same way the relay needs no login.
# -----------------------------------------------------------------------------

ORDER_SERVER_RT = "CHANGEME:5012"     # realtime   - target and workorder
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical - the same two, plus `date`

_PLACEHOLDER = "CHANGEME"

OUT_DIR = Path(__file__).resolve().parent / "out"
DPI = 200

# -----------------------------------------------------------------------------
# EMAIL.  Edit these.  No command line arguments, by design: who gets this
# report is part of what the report IS, not of one run of it - a distribution
# list that lives in whatever someone last typed is a list that quietly loses
# people.
#
# EMAIL_TO empty means DO NOT SEND.  That is the whole switch; there is no
# separate enable flag to leave in the wrong position.
# -----------------------------------------------------------------------------

EMAIL_TO = []                  # ["desk@example.com", "compliance@example.com"]
EMAIL_CC = []
EMAIL_BCC = []
EMAIL_FROM = ""                # "algo-reports@example.com"

# The relay takes mail from the host this runs on, so there is nothing to
# authenticate with: host, port and timeout is the whole of it.
SMTP_HOST = ""                 # "mail.example.com"
SMTP_PORT = 0                  # 0 -> 25
SMTP_TIMEOUT = 30              # seconds

# True builds the message and reports who it would go to without opening a
# socket - the way to check a new recipient list.
EMAIL_DRY_RUN = False


# =============================================================================
# SCOPE
# =============================================================================

class Mkt(NamedTuple):
    code: str
    name: str
    suffix: str     # what the sym ends with on the feed


# Fixed order.  The table always prints all five, in this order, whether or not
# they traded - a market that is simply absent from the data is indistinguishable
# from a market we forgot to ask about, and the zero row removes the question.
#
# The suffix IS the market.  Neither target nor workorder carries a country
# column, and going to target_stock for one would add a table and a join to a
# report that otherwise needs two.  The suffixes below are the feed's, not
# Bloomberg's or Reuters' - .JP rather than .T for Japan, .KS for Korea, .MK for
# Malaysia, .TB for Thailand.
MARKETS = (
    Mkt("HK", "Hong Kong", ".HK"),
    Mkt("JP", "Japan", ".JP"),
    Mkt("KR", "Korea", ".KS"),
    Mkt("MY", "Malaysia", ".MK"),
    Mkt("TH", "Thailand", ".TB"),
)
MARKET_CODES = tuple(m.code for m in MARKETS)
MARKET_NAME = {m.code: m.name for m in MARKETS}
SUFFIX_MARKET = {m.suffix: m.code for m in MARKETS}
SYM_PATTERNS = tuple("*" + m.suffix for m in MARKETS)

SHORTSELL_SIDE = "sellshort"          # confirmed value of target.side

# Only `rejected`.  workorder also carries invalid_ack and fail_ack, which are
# failures of a different kind - a malformed or unacknowledged send rather than
# a venue saying no - and counting them here would inflate the one number on
# this page that a compliance reader will quote.
REJECT_STATES = frozenset({"rejected"})

# Japan restricted names.  The flag rides on the parent's fixmsg; matched
# case-insensitively against the uppercased value, so a lower case tag still
# excludes rather than silently passing through.
RESTRICTED_MARKETS = ("JP",)
RESTRICTED_FIXMSG = "*RSHO=1*"


# =============================================================================
# PALETTE
#
# Taken UNCHANGED from the data-viz reference palette, which documents its own
# validation.  Two charts, one series each, so hue is chart identity rather than
# series identity and no within-chart separation is at stake:
#
#   completion   categorical slot 1, blue #2a78d6
#   rejections   status `critical`, red #d03b3b - 4.68:1 on this surface, and
#                deliberately not the categorical red, so it never reads as
#                "series 8"
#
# Light only.  This page is a PDF that gets printed and pasted into documents;
# a themed surface would be a liability rather than a feature.
# =============================================================================

BLUE = "#2a78d6"        # completion bars
RED = "#d03b3b"         # rejection bars, rejection counts, the rejection KPI
GREEN = "#006300"       # the completion KPI (success text)
SURFACE = "#ffffff"
INK = "#0b0b0b"         # primary
INK2 = "#52514e"        # secondary
INK3 = "#898781"        # muted - axis and category labels
RULE = "#e1e0d9"        # hairline
BASELINE = "#c3c2b7"    # chart baseline
HEADER_BG = "#3a3835"   # table header band
HEADER_FG = "#ffffff"

FONTS = ["Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans"]

TITLE = "Short-Sell Order Report"


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


# =============================================================================
# Q
#
# One lambda, both flavours.  The realtime tables have no `date` column at all,
# so a query written for the historical side does not merely return the wrong
# rows there - it throws.  The two extractions therefore sit inside $[hist;...],
# which q parses whole but only resolves the taken branch of, and the realtime
# branch bolts on `date:0Nd` with an update, so that everything downstream - the
# grouping, the join keys, the frame the Python sees - has one shape.  update
# rather than a bare atom in the select list: it is unambiguous on an empty
# result, where the atom would have nothing to take its length from.
#
# Sent as a serialized lambda with typed arguments; nothing is interpolated.
# `sfx` arrives as a list of CHAR VECTORS ((b"*.HK";b"*.JP";...)) built from
# MARKETS, so the patterns q filters on and the suffixes Python maps back cannot
# drift apart.  `sside` is a char vector for the same reason - PyKX turns a str
# into a q symbol, which is not what `like` and `$` want.
#
# NAMES.  Every parameter and local in here is checked against the q reserved
# words by --self-test.  `sside` is called that because `ss` is q's string
# search: naming a parameter after it is a PARSE error, which does not surface
# until the lambda reaches a real q process and comes back as `QError: ss`.
#
# What comes back is deliberately raw: one row per parent and one row per
# workorder, with sym, fixmsg and state carried through untouched.  The rules
# that decide what counts - which market a sym is, the Japan exclusion, what a
# rejection is - live in Python, where --self-test can prove them.  The only
# thing q decides is which syms to fetch, which is a volume question rather than
# a judgement, and it decides that from the same suffixes Python maps.
# =============================================================================

Q_SESSION = """
{[hist;d;sfx;sside]
  sside:`$sside;
  et:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; sym:0#`; size:0#0i;
         fixmsg:0#`);
  ew:([] date:0#0Nd; id_server:0#0i; id_work:0#0i; id_target:0#0i; make:0#0i;
         state:0#`);

  / parents.  The sym suffix is the market filter, so target_stock is not read
  / at all - two tables, no join.  upper, because the filter must not depend on
  / how the feed cased the suffix.
  /
  / NOTHING IS GROUPED.  A target IS an order and `size` IS its quantity, so
  / the rows come back as they are; the market total is the sum of the sizes of
  / the targets in that market, and that sum happens in Python.  Same for
  / workorder below.  Every `last ... by` this query used to carry was guarding
  / against a row multiplication that does not happen, at the price of hiding
  / one that would.
  t:$[hist;
      select date,id_server,id_target,sym,size,fixmsg
        from target where date=d, side=sside, any (upper sym) like/: sfx;
      update date:0Nd from select id_server,id_target,sym,size,fixmsg
        from target where side=sside, any (upper sym) like/: sfx];
  if[0=count t; :(et;ew)];

  / children.  Every workorder of those targets, ROW BY ROW and deliberately not
  / grouped: a workorder IS a child order, its executed quantity is `make`, and
  / a rejection is a row whose state is `rejected.  Both roll up as a plain sum
  / and a plain count over these rows.
  ids:exec distinct id_target from t;
  w:$[hist;
      select date,id_server,id_work,id_target,make,state
        from workorder where date=d, id_target in ids;
      update date:0Nd from select id_server,id_work,id_target,make,state
        from workorder where id_target in ids];
  (t;w)
  }
"""


# .Q.res - the q reserved words.  A name from this list cannot be a parameter
# or a local: q fails to PARSE the lambda and returns the offending token as the
# error, so the whole query dies on a name rather than on anything it does.
# Nothing here can catch that at runtime without a q process, hence the check in
# --self-test.
Q_RESERVED = frozenset("""
abs acos asin atan avg bin binr by cor cos cov delete dev div do each enlist
exec exit exp from getenv hopen if in insert last like log max min prd select
setenv sin sqrt ss string sum tan update var wavg where within wsum xexp
""".split())


def q_names(src: str) -> set:
    """Every parameter and local assigned in a q lambda, for the reserved word
    check.  Deliberately crude - it over-collects rather than under-collects,
    because a name it misses is a name nothing is checking."""
    import re
    out = set()
    for params in re.findall(r"\{\s*\[([^\]]*)\]", src):
        out.update(n.strip() for n in params.split(";") if n.strip())
    for name in re.findall(r"^\s*([a-zA-Z][a-zA-Z0-9_]*)\s*:(?!:)", src, re.M):
        out.add(name)
    return {n for n in out if n}


def _check_server(endpoint: str, which: str):
    if _PLACEHOLDER in endpoint:
        raise SystemExit(
            f"{which} is still set to {_PLACEHOLDER}. Edit the constants at the "
            f"top of {Path(__file__).name} before running against kdb.")


def connect(endpoint: str):
    """Open a PyKX connection.  Host and port; the processes are open.

    pykx is imported here rather than at module level so --self-test, --demo and
    everything else off the wire run on a machine that has neither pykx nor a q
    licence.
    """
    try:
        import pykx as kx
    except ImportError:
        raise SystemExit("pykx is not installed.  pip install pykx")
    host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected host:port, got {endpoint!r}")
    return kx.SyncQConnection(host=host, port=int(port))


# A date is always sent, even to the realtime server, so the argument keeps its
# type.  The realtime branch never looks at it.
_UNUSED_DATE = dt.date(2000, 1, 1)


def fetch(handle, hist: bool, d: Optional[dt.date]):
    """(parent records, child records) for one session.  d is None on realtime."""
    sfx = [p.encode() for p in SYM_PATTERNS]
    t, w = handle(Q_SESSION, hist, d if d is not None else _UNUSED_DATE, sfx,
                  SHORTSELL_SIDE.encode())
    return t.pd().to_dict("records"), w.pd().to_dict("records")


# =============================================================================
# RECORDS
#
# Everything below this line is pure and takes plain dicts, so the whole
# analytic path is exercised by --self-test with no pandas, no pykx and no kdb.
# =============================================================================

def _s(v) -> str:
    """A q symbol as a str.  PyKX hands these back as bytes or as numpy bytes."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _i(v) -> int:
    """A q int as an int.  A null (0Ni) or a missing value reads as 0."""
    try:
        if v is None:
            return 0
        n = int(v)
    except (TypeError, ValueError):
        return 0
    # 0Ni round-trips as INT_MIN; a size or a fill of that magnitude is a null.
    return 0 if n == -2147483648 else n


def _d(v) -> Optional[dt.date]:
    """A q date as a datetime.date.  A null date (the realtime side) is None."""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        import pandas as pd
        if pd.isna(v):
            return None
        return pd.Timestamp(v).date()
    except Exception:
        return None


def market_of(sym) -> Optional[str]:
    """The market a sym belongs to, from its suffix.  None when it is not one of
    the five - which is how everything else on the book stays out of a total."""
    s = _s(sym).upper()
    i = s.rfind(".")
    return SUFFIX_MARKET.get(s[i:]) if i > 0 else None


def is_restricted(country: str, fixmsg: str) -> bool:
    """Is this parent a restricted name we must leave out entirely?

    Japan only, and driven by the parent's own fixmsg rather than by anything
    derived, because the flag is what the order was actually sent with.
    """
    if country not in RESTRICTED_MARKETS:
        return False
    return fnmatch.fnmatchcase(_s(fixmsg).upper(), RESTRICTED_FIXMSG)


def is_rejected(state: str) -> bool:
    """Is this workorder ROW a rejection?

    Every row carrying the state counts.  Not "did the split end up rejected" -
    a split rejected and then cancelled ended up cancelled, and the rejection
    still happened.
    """
    return _s(state).strip().lower() in REJECT_STATES


class Parent(NamedTuple):
    key: tuple              # (date, id_server, id_target) - unique per order
    date: Optional[dt.date]
    country: str
    sym: str
    size: int


class Split(NamedTuple):
    """ONE WORKORDER ROW, not one child order.  A split the engine wrote three
    times is three of these; id_work is what identifies the split itself.

    Both figures the page takes from workorder are per row, so nothing here is
    ever reduced to one entry per id_work.
    """
    key: tuple              # its parent's key
    id_work: int
    date: Optional[dt.date]
    country: str
    make: int
    rejected: bool


def to_parents(records) -> tuple:
    """(parents, restricted_dropped).  Out of scope markets and restricted names
    are dropped here, before anything is counted."""
    out, dropped = [], 0
    for r in records:
        sym = _s(r.get("sym"))
        country = market_of(sym)
        if country is None:
            continue
        if is_restricted(country, r.get("fixmsg")):
            dropped += 1
            continue
        d = _d(r.get("date"))
        out.append(Parent(
            key=(d, _i(r.get("id_server")), _i(r.get("id_target"))),
            date=d, country=country, sym=sym, size=abs(_i(r.get("size")))))
    return out, dropped


def to_splits(records, parents) -> list:
    """Workorder rows, keyed back onto the parents that survived.

    A row whose parent was dropped is dropped with it - that is what makes the
    Japan exclusion complete rather than cosmetic.  A row with no parent at all
    is dropped too: it cannot be attributed to a market.
    """
    by_key = {p.key: p for p in parents}
    out = []
    for r in records:
        key = (_d(r.get("date")), _i(r.get("id_server")), _i(r.get("id_target")))
        p = by_key.get(key)
        if p is None:
            continue
        out.append(Split(key=key, id_work=_i(r.get("id_work")), date=p.date,
                         country=p.country, make=abs(_i(r.get("make"))),
                         rejected=is_rejected(r.get("state"))))
    return out


# =============================================================================
# ROLLUPS
# =============================================================================

def _completion(executed: int, order_qty: int) -> Optional[float]:
    """Executed as a percentage of order qty, or None where there is nothing to
    divide by.  None prints as an em dash; it never prints as 0%, which would
    claim we sent quantity and filled none of it."""
    if order_qty <= 0:
        return None
    return 100.0 * executed / order_qty


class Row(NamedTuple):
    code: str
    name: str
    orders: int
    order_qty: int
    executed: int
    rejections: int

    @property
    def completion(self) -> Optional[float]:
        return _completion(self.executed, self.order_qty)


class DayRow(NamedTuple):
    date: dt.date
    orders: int
    order_qty: int
    executed: int
    rejections: int

    @property
    def completion(self) -> Optional[float]:
        return _completion(self.executed, self.order_qty)


class Totals(NamedTuple):
    orders: int
    order_qty: int
    executed: int
    rejections: int

    @property
    def completion(self) -> Optional[float]:
        return _completion(self.executed, self.order_qty)


def by_market(parents, splits) -> list:
    """One Row per market, always all five, always in MARKETS order.

    Every figure is a plain count or a plain sum over rows the query returned
    as they stand.  A target is an order and `size` is its quantity, so a
    market's order qty is the sum of its targets' sizes; a workorder is a child
    order, so a market's executed is the sum of their `make` and its rejections
    the count of those in state `rejected.  Nothing is grouped anywhere.
    """
    orders = {c: 0 for c in MARKET_CODES}
    qty = {c: 0 for c in MARKET_CODES}
    made = {c: 0 for c in MARKET_CODES}
    rej = {c: 0 for c in MARKET_CODES}
    for p in parents:
        orders[p.country] += 1
        qty[p.country] += p.size
    for s in splits:
        made[s.country] += s.make
        if s.rejected:
            rej[s.country] += 1
    return [Row(m.code, m.name, orders[m.code], qty[m.code],
                made[m.code], rej[m.code]) for m in MARKETS]


def by_day(parents, splits) -> list:
    """One DayRow per date that carried short sell flow, in date order.

    All five markets folded together: the per day charts answer "how did the
    month go", and splitting them by market there would be a different report.
    """
    days = {}

    def slot(d):
        return days.setdefault(d, [0, 0, 0, 0])   # orders, qty, made, rej

    for p in parents:
        if p.date is None:
            continue
        s = slot(p.date)
        s[0] += 1
        s[1] += p.size
    for f in splits:
        if f.date is None:
            continue
        e = slot(f.date)
        e[2] += f.make
        if f.rejected:
            e[3] += 1
    return [DayRow(d, *days[d]) for d in sorted(days)]


def totals(rows) -> Totals:
    """The headline figures.  Completion is re-derived from the summed
    quantities, so it is quantity weighted rather than an average of five
    percentages - one market with 500 orders must not weigh the same as one
    with 5."""
    return Totals(sum(r.orders for r in rows), sum(r.order_qty for r in rows),
                  sum(r.executed for r in rows), sum(r.rejections for r in rows))


# =============================================================================
# FORMATTING
# =============================================================================

DASH = "—"


def fmt_int(n) -> str:
    return f"{int(n):,}"


def fmt_pct1(v) -> str:
    return DASH if v is None else f"{v:.1f}%"


def fmt_pct0(v) -> str:
    return "0%" if v is None else f"{v:.0f}%"


# =============================================================================
# PAGE
#
# One A4 portrait page, laid out by hand in figure fractions.  Not a GridSpec:
# the page is a document rather than a grid of plots - a title block, a rule, a
# KPI row, a hand drawn table, then charts - and the only thing that lives in an
# axes is the bars.
#
# The two layouts differ in the table's row pitch and in what sits below it.
# --monthly buys the room for the two per day charts by tightening the table,
# which is the one block whose height is arbitrary.
# =============================================================================

PAGE_W, PAGE_H = 8.27, 11.69          # A4 portrait, inches
L, R = 0.075, 0.925                   # left and right margins, figure fraction
COL_W = R - L

Y_TITLE = 0.955
Y_SUBTITLE = 0.931
Y_RULE_TOP = 0.9185
Y_KPI_VALUE = 0.884
Y_KPI_LABEL = 0.860
Y_TABLE_TOP = 0.808
H_TABLE_HEAD = 0.026

# The footer is the only thing under the charts.  What the columns are counted
# from is written down in the README and in this file, not on the page - the
# page is read by people who already know, and the definitions were pushing the
# monthly layout around for no one's benefit.
Y_RULE_BOTTOM = 0.066
Y_FOOTER = 0.048

# Where the two by market charts sit, and how tall they are.  --monthly gives up
# most of that band, and some of the table's row pitch, to the per day pair.
MKT_BAND = {                      # (y0, height, title y)
    "daily": (0.195, 0.265, 0.478),
    "monthly": (0.470, 0.118, 0.598),
}
# The per day band stops clear of the notes: its baseline hairline is drawn 0.008
# below y0, and a baseline running through a line of text is the one collision
# this layout is actually prone to.
# The day charts are VERTICAL and stacked, each the full column width: 23 dates
# want the long side of the page, and turned on their side under the baseline
# they want about 0.045 of the page height beneath each plot.  (y0, height,
# title y) for the completion chart and then the rejections one.
DAY_BANDS = (
    (0.330, 0.100, 0.443),
    (0.152, 0.100, 0.265),
)

# (column label, width as a fraction of COL_W, right aligned)
TABLE_COLS = (
    ("Market", 0.27, False),
    ("Orders", 0.12, True),
    ("Order qty", 0.19, True),
    ("Executed", 0.19, True),
    ("Completion", 0.13, True),
    ("Rejections", 0.10, True),
)

BAR_R_IN = 0.035      # rounded data end, inches (~4px at 100dpi)
BAR_FRAC = 0.58       # bar height as a fraction of the row pitch - thin marks


def _mpl():
    """matplotlib, imported here so the module still imports without it."""
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


def _hline(fig, y, x0=L, x1=R, color=RULE, lw=0.8):
    from matplotlib.lines import Line2D
    fig.add_artist(Line2D([x0, x1], [y, y], transform=fig.transFigure,
                          color=color, linewidth=lw, zorder=1))


def _rect(fig, x, y, w, h, color, zorder=1):
    from matplotlib.patches import Rectangle
    fig.add_artist(Rectangle((x, y), w, h, transform=fig.transFigure,
                             facecolor=color, edgecolor="none", zorder=zorder))


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
    """A column with a square foot on the baseline and a rounded top.

    The mirror of _rounded_bar.  rx and ry are separate for the same reason: a
    single radius in data units would draw an ellipse on a non-square axes.
    """
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


def _vbarchart(fig, rect, title, labels, values, texts, color,
               vmax=None, fs=5.4, title_y=None):
    """One VERTICAL bar chart: columns left to right, dates turned on their side
    under the baseline, values above each column.

    This is the form the day series takes.  A month is a sequence, and a
    sequence reads left to right - the horizontal form used for the five markets
    would put time on the vertical axis, which is the wrong axis for it.
    """
    x0, y0, w, h = rect
    ax = fig.add_axes(rect)
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
    _hline(fig, y0, x0, x0 + w, color=BASELINE, lw=0.8)
    return ax


def _barchart(fig, rect, title, labels, values, texts, color,
              vmax=None, fs=8.0, title_y=None, gutter=0.38, head=1.26):
    """One horizontal bar chart: category gutter, bars, direct value labels.

    No axes, no grid, no ticks.  Every bar is labelled - with at most a handful
    of categories, or with a date series that is read like a table, the label is
    the value channel and an axis would only repeat it less precisely.

    gutter is how much room the category labels get, and head how far past the
    longest bar the value labels may run, both as multiples of the scale.  They
    are arguments because a full date needs about half again the gutter a market
    name does, and a bar squeezed to buy that room is the wrong trade.
    """
    x0, y0, w, h = rect
    ax = fig.add_axes(rect)
    ax.set_axis_off()
    ax.patch.set_alpha(0.0)

    n = max(len(values), 1)
    top = vmax if vmax is not None else max([abs(v) for v in values] + [0.0])
    if not top:
        top = 1.0
    # gutter for the category labels, headroom for the value labels
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
    _hline(fig, y0 - 0.008, x0, x0 + w, color=BASELINE, lw=0.8)
    return ax


def _table(fig, rows, y_top, row_h):
    """The per market table, drawn by hand.

    A dark header band and hairline separated rows, matching the page this
    reproduces.  Numbers are right aligned on tabular figures so the columns
    line up; a zero rejection count stays muted so the eye is only pulled to
    the counts that are not zero.
    """
    head_y = y_top - H_TABLE_HEAD
    _rect(fig, L, head_y, COL_W, H_TABLE_HEAD, HEADER_BG, zorder=2)

    x = L
    edges = []
    for label, frac, right in TABLE_COLS:
        w = frac * COL_W
        edges.append((x, w, right))
        tx = x + w - 0.008 if right else x + 0.010
        fig.text(tx, head_y + H_TABLE_HEAD / 2.0, label,
                 ha="right" if right else "left", va="center",
                 fontsize=8.5, fontweight="bold", color=HEADER_FG, zorder=3)
        x += w

    y = head_y
    for r in rows:
        y -= row_h
        cells = (
            (r.name, INK, "normal"),
            (fmt_int(r.orders), INK, "normal"),
            (fmt_int(r.order_qty), INK, "normal"),
            (fmt_int(r.executed), INK, "normal"),
            (fmt_pct1(r.completion), INK, "normal"),
            (fmt_int(r.rejections),
             RED if r.rejections else INK3,
             "bold" if r.rejections else "normal"),
        )
        for (cx, cw, right), (txt, colour, weight) in zip(edges, cells):
            tx = cx + cw - 0.008 if right else cx + 0.010
            fig.text(tx, y + row_h / 2.0, txt,
                     ha="right" if right else "left", va="center",
                     fontsize=9, color=colour, fontweight=weight)
        _hline(fig, y, L, R)
    return y


def _kpis(fig, tot):
    """The three headline figures.  Colour carries the same meaning it carries
    everywhere else on the page: green completion, red rejections."""
    items = (
        (fmt_int(tot.orders), "Short-sell orders", INK),
        (fmt_pct1(tot.completion), "Overall completion", GREEN),
        (fmt_int(tot.rejections), "Rejections", RED),
    )
    for i, (value, label, colour) in enumerate(items):
        x = L + i * (COL_W / 3.0)
        fig.text(x, Y_KPI_VALUE, value, fontsize=24, fontweight="bold",
                 color=colour, va="baseline")
        fig.text(x, Y_KPI_LABEL, label, fontsize=9, color=INK2, va="baseline")


def _sorted_pairs(rows, key):
    """Chart order: biggest first, ties keeping MARKETS order.  Python's sort is
    stable, so the fixed market order is the tie break for free."""
    return sorted(rows, key=key, reverse=True)


def draw(rows, tot, subtitle, footer, days=None):
    """The whole page.  Pure: takes rollups, returns a figure."""
    plt = _mpl()
    monthly = days is not None

    fig = plt.figure(figsize=(PAGE_W, PAGE_H), facecolor=SURFACE)
    fig.patch.set_facecolor(SURFACE)

    fig.text(L, Y_TITLE, TITLE, fontsize=19, fontweight="bold", color=INK,
             va="baseline")
    fig.text(L, Y_SUBTITLE, subtitle, fontsize=9.5, color=INK2, va="baseline")
    _hline(fig, Y_RULE_TOP)
    _kpis(fig, tot)

    row_h = 0.030 if monthly else 0.040
    _table(fig, rows, Y_TABLE_TOP, row_h)

    # ---- completion and rejections by market -------------------------------
    comp = _sorted_pairs(rows, key=lambda r: (r.completion or 0.0))
    rej = _sorted_pairs(rows, key=lambda r: r.rejections)

    mkt_y0, mkt_rect_h, mkt_title_y = MKT_BAND["monthly" if monthly else "daily"]
    half = 0.405

    _barchart(fig, (L, mkt_y0, half, mkt_rect_h), "Completion by market",
              [r.name for r in comp],
              [(r.completion or 0.0) for r in comp],
              [fmt_pct0(r.completion) for r in comp],
              BLUE, vmax=100.0, fs=8.0, title_y=mkt_title_y)
    _barchart(fig, (R - half, mkt_y0, half, mkt_rect_h), "Rejections by market",
              [r.name for r in rej],
              [float(r.rejections) for r in rej],
              [fmt_int(r.rejections) for r in rej],
              RED, fs=8.0, title_y=mkt_title_y)

    # ---- completion and rejections by day ----------------------------------
    if monthly:
        # Chronological, never sorted by value: a date series read out of order
        # is a different chart that happens to use the same numbers.
        # The whole date, not the day of month.  These pages get read on their
        # own, months after the fact, and "17" is only a date if you still have
        # the subtitle in view.
        labels = [f"{d.date:%Y-%m-%d}" for d in days] or ["-"]
        day_fs = 5.4 if len(days) > 16 else 6.8
        (cy, ch, cty), (ry, rh, rty) = DAY_BANDS
        _vbarchart(fig, (L, cy, COL_W, ch), "Completion by day",
                   labels,
                   [(d.completion or 0.0) for d in days] or [0.0],
                   [fmt_pct0(d.completion) for d in days] or [DASH],
                   BLUE, vmax=100.0, fs=day_fs, title_y=cty)
        _vbarchart(fig, (L, ry, COL_W, rh), "Rejections by day",
                   labels,
                   [float(d.rejections) for d in days] or [0.0],
                   [fmt_int(d.rejections) for d in days] or ["0"],
                   RED, fs=day_fs, title_y=rty)

    # ---- notes and footer ---------------------------------------------------
    _hline(fig, Y_RULE_BOTTOM)
    fig.text(L, Y_FOOTER, footer, fontsize=7.5, color=INK3, va="baseline")
    return fig


def save(fig, out_dir: Path, stem: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for ext in ("pdf", "png"):
        p = out_dir / f"{stem}.{ext}"
        fig.savefig(p, dpi=DPI, facecolor=SURFACE)
        written.append(p)
        log(f"  wrote {p}")
    return written


# =============================================================================
# EMAIL
#
# The mailer itself is scripts/lib/mailer.py and knows nothing about this
# report.  What lives here is only the part that IS about this report: what the
# subject says, and what the body says when the recipient's client will not show
# the attachment.
#
# The body repeats the page rather than pointing at it.  A report that arrives
# as "see attached" is a report most people do not open, and the three numbers
# that matter fit in a preview pane.
# =============================================================================

TABLE_HEADERS = tuple(c[0] for c in TABLE_COLS)
TABLE_ALIGN = tuple("r" if c[2] else "l" for c in TABLE_COLS)


def _mailer():
    try:
        from lib import mailer
    except ImportError as e:
        raise SystemExit(
            f"EMAIL_TO is set but scripts/lib/mailer.py will not import "
            f"({e}).  It sits beside this script's folder; copy scripts/lib "
            f"too if you moved this one.")
    return mailer


def table_cells(rows) -> list:
    """The per market table as text, in the same order the page prints it."""
    return [[r.name, fmt_int(r.orders), fmt_int(r.order_qty),
             fmt_int(r.executed), fmt_pct1(r.completion), fmt_int(r.rejections)]
            for r in rows]


def mail_bodies(rows, tot, subtitle, footer, png_cid=None) -> tuple:
    """(plain text body, html body).  Pure - no files, no network."""
    m = _mailer()
    cells = table_cells(rows)

    headline = (f"{fmt_int(tot.orders)} short-sell orders   ·   "
                f"{fmt_pct1(tot.completion)} overall completion   ·   "
                f"{fmt_int(tot.rejections)} rejections")
    text = "\n".join([
        TITLE, subtitle, "", headline, "",
        m.text_table(TABLE_HEADERS, cells, TABLE_ALIGN), "",
        footer, ""])

    # Inline styles only; mail clients strip <style> blocks.
    colours = [[None] * 5 + [RED if r.rejections else INK3] for r in rows]
    kpis = "".join(
        f'<td style="padding:0 34px 0 0"><div style="font-size:26px;'
        f'font-weight:700;color:{c}">{m.esc(v)}</div>'
        f'<div style="font-size:12px;color:{INK2};padding-top:2px">'
        f'{m.esc(l)}</div></td>'
        for v, l, c in ((fmt_int(tot.orders), "Short-sell orders", INK),
                        (fmt_pct1(tot.completion), "Overall completion", GREEN),
                        (fmt_int(tot.rejections), "Rejections", RED)))
    img = (f'<div style="padding-top:22px"><img src="cid:{png_cid}" '
           f'style="width:100%;max-width:660px" alt="{m.esc(TITLE)}"></div>'
           if png_cid else "")
    html = (
        f'<div style="font:14px system-ui,-apple-system,Segoe UI,Arial,'
        f'sans-serif;color:{INK};max-width:700px">'
        f'<div style="font-size:22px;font-weight:700">{m.esc(TITLE)}</div>'
        f'<div style="font-size:13px;color:{INK2};padding-top:3px">'
        f'{m.esc(subtitle)}</div>'
        f'<hr style="border:0;border-top:1px solid {RULE};margin:12px 0 16px">'
        f'<table cellspacing="0" cellpadding="0"><tr>{kpis}</tr></table>'
        f'<div style="padding-top:20px">'
        f'{m.html_table(TABLE_HEADERS, cells, TABLE_ALIGN, colours)}</div>'
        f'{img}'
        f'<hr style="border:0;border-top:1px solid {RULE};margin:22px 0 8px">'
        f'<div style="font-size:11px;color:{INK3}">{m.esc(footer)}</div>'
        f'</div>')
    return text, html


def email_configured() -> bool:
    """Is there anyone to send to?  An empty EMAIL_TO is the off switch."""
    return bool(EMAIL_TO or EMAIL_CC or EMAIL_BCC)


def smtp_config():
    """The SMTP settings.  Host, port and timeout - there is nothing else."""
    return _mailer().Smtp(host=SMTP_HOST, port=SMTP_PORT,
                          timeout=SMTP_TIMEOUT)


def mail_report(rows, tot, subtitle, footer, when, files) -> None:
    """Build the message and send it, per the EMAIL block at the top.

    Raises rather than warning: a report nobody received is only harmless if
    somebody knows it was not received.
    """
    m = _mailer()
    pdf = next((p for p in files if p.suffix == ".pdf"), None)
    png = next((p for p in files if p.suffix == ".png"), None)

    if not EMAIL_FROM:
        raise SystemExit(
            f"EMAIL_TO is set but EMAIL_FROM is empty. Both live in the EMAIL "
            f"block near the top of {Path(__file__).name}.")

    cid = "report-page" if png else None
    text, html = mail_bodies(rows, tot, subtitle, footer, png_cid=cid)
    msg = m.build_message(m.Mail(
        subject=f"{TITLE} - {when}",
        sender=EMAIL_FROM,
        to=EMAIL_TO,
        cc=EMAIL_CC,
        bcc=EMAIL_BCC,
        text=text,
        html=html,
        inline_images=[(cid, png)] if png else (),
        attachments=[pdf] if pdf else ()))

    smtp = smtp_config()
    log("  email:")
    log(m.describe(msg))
    rcpt = m.send(msg, smtp, dry_run=EMAIL_DRY_RUN)
    if EMAIL_DRY_RUN:
        log(f"  EMAIL_DRY_RUN: NOT sent, {len(rcpt)} recipient"
            f"{'' if len(rcpt) == 1 else 's'} would have been")
    else:
        log(f"  sent to {len(rcpt)} recipient{'' if len(rcpt) == 1 else 's'} "
            f"via {smtp.host}:{smtp.resolved_port()}")


# =============================================================================
# RUN
# =============================================================================

def month_dates(year: int, month: int):
    """Every weekday of the month.  Weekends are skipped rather than queried -
    none of the five markets trades on one - but holidays are not, because a
    holiday calendar we would have to maintain is a worse failure mode than a
    handful of queries that return nothing."""
    n = calendar.monthrange(year, month)[1]
    return [d for d in (dt.date(year, month, i + 1) for i in range(n))
            if d.weekday() < 5]


def parse_month(text: str) -> tuple:
    """YYYY-MM, strictly.

    The year has to be four digits: "26-07" is a perfectly valid date in the
    year 26, so a typed-short year would otherwise run silently and return an
    empty report rather than an error.
    """
    try:
        ys, ms = str(text).split("-")
        if len(ys) != 4 or len(ms) != 2 or not (ys + ms).isdigit():
            raise ValueError
        y, m = int(ys), int(ms)
        if not 1 <= m <= 12:
            raise ValueError
        dt.date(y, m, 1)
    except (ValueError, AttributeError):
        raise SystemExit(f"--monthly wants YYYY-MM, got {text!r}")
    return y, m


class Plan(NamedTuple):
    """Which server, which dates, and what the page calls itself.

    Three modes, and this is the only place that tells them apart:

      (nothing)            REALTIME, the session in progress.  dates is [None]
                           - the realtime tables have no date to filter on.
      --date 2026-07-01    HISTORICAL, that one session, in the daily layout.
      --monthly 2026-07    HISTORICAL, every weekday of the month, and the two
                           per day charts.

    Pure, so --self-test covers the routing without a connection - which server
    a mode reaches for is not something to find out by running it against the
    wrong one.
    """
    monthly: bool
    hist: bool
    endpoint: str
    endpoint_name: str
    dates: list            # [None] on realtime
    stem: str
    when: str


def plan(monthly=None, date=None, now=None) -> Plan:
    now = now or dt.datetime.now()
    if monthly is not None and date is not None:
        raise SystemExit("--monthly and --date are alternatives, not a range")
    hist = monthly is not None or date is not None
    endpoint_name = "ORDER_SERVER_HIST" if hist else "ORDER_SERVER_RT"
    endpoint = ORDER_SERVER_HIST if hist else ORDER_SERVER_RT

    if monthly is not None:
        year, month = parse_month(monthly)
        return Plan(True, True, endpoint, endpoint_name, month_dates(year, month),
                    f"short_sell_report_{year:04d}-{month:02d}",
                    f"{calendar.month_name[month]} {year}")
    if date is not None:
        return Plan(False, True, endpoint, endpoint_name, [date],
                    f"short_sell_report_{date:%Y-%m-%d}", f"{date:%Y-%m-%d}")
    return Plan(False, False, endpoint, endpoint_name, [None],
                f"short_sell_report_{now:%Y-%m-%d}", f"{now:%Y-%m-%d %H:%M}")


def run(args) -> int:
    pl = plan(args.monthly, args.date)
    monthly, hist, dates, stem = pl.monthly, pl.hist, pl.dates, pl.stem
    subtitle_when = pl.when
    _check_server(pl.endpoint, pl.endpoint_name)

    log(f"short_sell_report  {'historical' if hist else 'realtime'}  "
        f"{pl.endpoint}")
    h = connect(pl.endpoint)

    parents, splits, dropped, traded = [], [], 0, 0
    for d in dates:
        if not args.quiet and d is not None:
            log(f"  {d} ...")
        pr, cr = fetch(h, hist, d)
        ps, drop = to_parents(pr)
        ws = to_splits(cr, ps)
        dropped += drop
        if ps:
            traded += 1
        parents.extend(ps)
        splits.extend(ws)

    rows = by_market(parents, splits)
    tot = totals(rows)
    days = by_day(parents, splits) if monthly else None

    log(f"  {tot.orders:,} short-sell orders, {tot.rejections:,} rejections"
        + (f", {dropped:,} restricted JP orders excluded" if dropped else ""))
    if monthly:
        log(f"  {traded} of {len(dates)} weekdays carried short-sell flow")

    if monthly:
        subtitle = f"By market  ·  {subtitle_when}"
        footer = (f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  "
                  f"historical, {traded} trading day"
                  f"{'' if traded == 1 else 's'}")
    else:
        subtitle = f"By market  ·  {subtitle_when}"
        footer = (f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  "
                  + ("historical, one session" if hist
                     else "real-time snapshot"))
    if dropped:
        footer += f"  ·  {dropped:,} restricted JP order" \
                  f"{'' if dropped == 1 else 's'} excluded"

    files = save(draw(rows, tot, subtitle, footer, days),
                 Path(args.out_dir), stem)
    if email_configured():
        mail_report(rows, tot, subtitle, footer, subtitle_when, files)
    return 0


# =============================================================================
# DEMO
#
# --demo draws both layouts from made up numbers, with no connection, so the
# page can be looked at and argued about before anyone points it at kdb.
#
# It is stamped SAMPLE in the subtitle, in the footer and in the file name, and
# the numbers are deliberately the ones in the README rather than anything
# plausible-and-new.  A report that looks real and is not is worse than no
# preview at all - these go into compliance folders.
# =============================================================================

DEMO_STAMP = "SAMPLE - synthetic data, not from kdb"


def demo_session():
    """(parents, splits) for one made up session.  Deterministic."""
    pr, cr = [], []
    #  the shape of a real day: Japan many small orders, Hong Kong fewer and
    #  larger, Korea small and badly filled, Malaysia and Thailand quiet
    for i in range(1, 110):
        pr.append(_p(i, "HK", 457_633))
        cr.append(_c(i, i, 242_901, "filled"))
    for i in range(239):
        cr.append(_c(90_000 + i, 1 + (i % 109), 0, "rejected"))
    for i in range(1, 542):
        pr.append(_p(1000 + i, "JP", 9_903))
        cr.append(_c(1000 + i, 1000 + i, 8_285, "filled"))
    for i in range(3):
        cr.append(_c(91_000 + i, 1001 + (i % 541), 0, "rejected"))
    for i in range(1, 83):
        pr.append(_p(2000 + i, "KR", 12_567))
        cr.append(_c(2000 + i, 2000 + i, 4_933, "done"))
    for i in range(152):
        cr.append(_c(92_000 + i, 2001 + (i % 82), 0, "rejected"))
    parents, _ = to_parents(pr)
    return parents, to_splits(cr, parents)


def demo_month(year=2026, month=7):
    """(parents, splits) for a made up month.  Deterministic - no random, so the
    preview is the same page every time and a layout change is the only thing
    that can move it."""
    pr, cr = [], []
    k = 0
    for i, d in enumerate(month_dates(year, month)):
        n = 8 + (i * 7) % 33                       # 8..40 orders that day
        for j in range(n):
            k += 1
            mkt = MARKET_CODES[(i + j) % len(MARKET_CODES)]
            size = 1000 + ((i * 37 + j * 911) % 900) * 1000
            pr.append(_p(k, mkt, size, d=d))
            cr.append(_c(k, k, int(size * (0.15 + ((i * 13 + j) % 60) / 100.0)),
                         "filled", d=d))
            for r in range((i + j) % 4):
                cr.append(_c(500_000 + k * 4 + r, k, 0, "rejected", d=d))
    parents, _ = to_parents(pr)
    return parents, to_splits(cr, parents)


def demo(out_dir) -> int:
    """Write both layouts from made up numbers.  No kdb, no pykx."""
    out = Path(out_dir)
    stamp = f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  {DEMO_STAMP}"

    parents, splits = demo_session()
    rows = by_market(parents, splits)
    log("demo: daily layout")
    save(draw(rows, totals(rows), f"By market  ·  2026-07-24 18:37  ·  SAMPLE",
              stamp),
         out, "short_sell_report_SAMPLE_daily")

    parents, splits = demo_month()
    rows = by_market(parents, splits)
    days = by_day(parents, splits)
    log(f"demo: monthly layout, {len(days)} trading days")
    save(draw(rows, totals(rows), "By market  ·  July 2026  ·  SAMPLE",
              stamp, days=days),
         out, "short_sell_report_SAMPLE_monthly")
    log("  these are made up numbers - do not circulate them as a report")
    return 0


# =============================================================================
# SELF TEST
# =============================================================================

def _p(idt, country, size, fixmsg="", d=None, srv=1, sym=None):    # noqa: E302
    """One TARGET row.  country is spelled as a market code for readability and
    turned into the sym suffix the real feed carries."""
    if sym is None:
        sym = "X" + MARKET_NAME.get(country, country)[:1] + dict(
            (m.code, m.suffix) for m in MARKETS).get(country, "." + country)
    return {"date": d, "id_server": srv, "id_target": idt, "sym": sym,
            "size": size, "fixmsg": fixmsg}


def _c(idw, idt, make, state, d=None, srv=1):
    """ONE WORKORDER ROW.  Several of these may share an id_work - which is the
    whole point of counting per row rather than per split."""
    return {"date": d, "id_server": srv, "id_work": idw, "id_target": idt,
            "make": make, "state": state}


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("short_sell_report --self-test")

    # -- the page this reproduces, rebuilt from records -----------------------
    print("\nmarket rollup")
    pr = ([_p(i, "HK", 457_633) for i in range(1, 110)]
          + [_p(1000 + i, "JP", 9_903) for i in range(1, 542)]
          + [_p(2000 + i, "KR", 12_567) for i in range(1, 83)])
    cr = ([_c(i, i, 242_901, "filled") for i in range(1, 110)]
          + [_c(90_000 + i, 1 + (i % 109), 0, "rejected") for i in range(239)]
          + [_c(1000 + i, 1000 + i, 8_285, "filled") for i in range(1, 542)]
          + [_c(91_000 + i, 1001 + (i % 541), 0, "rejected") for i in range(3)]
          + [_c(2000 + i, 2000 + i, 4_933, "done") for i in range(1, 83)]
          + [_c(92_000 + i, 2001 + (i % 82), 0, "rejected") for i in range(152)])
    parents, dropped = to_parents(pr)
    splits = to_splits(cr, parents)
    rows = by_market(parents, splits)
    tot = totals(rows)

    check("all five markets, in order", [r.code for r in rows],
          list(MARKET_CODES))
    check("HK orders", rows[0].orders, 109)
    check("JP orders", rows[1].orders, 541)
    check("KR orders", rows[2].orders, 82)
    check("MY is a zero row, not a missing row", rows[3].orders, 0)
    check("HK order qty", rows[0].order_qty, 49_881_997)
    check("HK executed", rows[0].executed, 26_476_209)
    check("HK rejections (per workorder row, not per order)",
          rows[0].rejections, 239)
    check("JP rejections", rows[1].rejections, 3)
    check("KR rejections", rows[2].rejections, 152)
    check("total orders", tot.orders, 732)
    check("total rejections", tot.rejections, 394)
    check("headline completion is quantity weighted",
          round(tot.completion, 1), 55.7)
    check("HK completion", round(rows[0].completion, 1), 53.1)
    check("a market with no flow has no completion", rows[3].completion, None)
    check("no restricted orders in this fixture", dropped, 0)

    # -- Japan restricted ------------------------------------------------------
    print("\nJapan restricted names")
    check("RSHO=1 is restricted", is_restricted("JP", "RSHO=1"), True)
    check("embedded in a longer fixmsg",
          is_restricted("JP", "8=FIX.4.2|114=Y|RSHO=1|59=0"), True)
    check("lower case still excludes", is_restricted("JP", "rsho=1"), True)
    check("RSHO=0 is not restricted", is_restricted("JP", "RSHO=0"), False)
    check("no fixmsg is not restricted", is_restricted("JP", ""), False)
    check("the rule is Japan only", is_restricted("HK", "RSHO=1"), False)

    pr2 = [_p(1, "JP", 100, "RSHO=1"), _p(2, "JP", 300, "111=X|RSHO=1"),
           _p(3, "JP", 500, "RSHO=0"), _p(4, "HK", 700, "RSHO=1")]
    cr2 = [_c(11, 1, 100, "filled"), _c(12, 1, 0, "rejected"),
           _c(13, 3, 250, "filled"), _c(14, 3, 0, "rejected"),
           _c(15, 4, 700, "filled")]
    p2, drop2 = to_parents(pr2)
    c2 = to_splits(cr2, p2)
    r2 = by_market(p2, c2)
    check("two JP parents dropped", drop2, 2)
    check("their quantity is gone too", r2[1].order_qty, 500)
    check("their fills are gone", r2[1].executed, 250)
    check("their rejections are gone", r2[1].rejections, 1)
    check("HK is untouched by the JP rule", r2[0].order_qty, 700)

    # -- how a rejection is counted -------------------------------------------
    print("\nrejections are counted per workorder row")
    #  one order, five child orders:
    #    101  rejected             make   0   -> 1 rejection
    #    102  rejected             make   0   -> 1 rejection
    #    103  filled               make 300
    #    104  cxl, part filled     make 200   -> make is the executed quantity
    #                                            whatever state it ended in
    #    105  leave, nothing done  make   0
    pr7 = [_p(1, "HK", 1000)]
    cr7 = [_c(101, 1, 0, "rejected"), _c(102, 1, 0, "rejected"),
           _c(103, 1, 300, "filled"), _c(104, 1, 200, "cxl"),
           _c(105, 1, 0, "leave")]
    p7, _ = to_parents(pr7)
    s7 = to_splits(cr7, p7)
    r7 = by_market(p7, s7)
    check("one entry per workorder", len(s7), 5)
    check("rejections are the workorders in state `rejected", r7[0].rejections, 2)
    check("executed is the sum of make", r7[0].executed, 500)
    check("a cancelled child still contributes what it executed",
          sum(x.make for x in s7 if x.id_work == 104), 200)
    check("a state alone executes nothing - only make says",
          sum(x.make for x in s7 if x.id_work == 105), 0)
    check("a rejected child executes nothing",
          sum(x.make for x in s7 if x.rejected), 0)
    check("completion is executed over parent size",
          round(r7[0].completion, 1), 50.0)
    check("nothing is grouped by id_work",
          by_market(p7, to_splits(
              [_c(301, 1, 0, "rejected"), _c(301, 1, 0, "rejected")],
              p7))[0].rejections, 2)

    # -- scope and state -------------------------------------------------------
    print("\nthe q holds together")
    names = q_names(Q_SESSION)
    check("the query's own names are found", "sside" in names and "t" in names,
          True)
    check("none of them is a q reserved word - `ss` cost a run to learn",
          sorted(names & Q_RESERVED), [])
    check("and the check would catch one if it came back",
          sorted(q_names("{[hist;ss] ss:1; t:2}") & Q_RESERVED), ["ss"])
    check("the side is sent as a char vector, not a symbol",
          isinstance(SHORTSELL_SIDE.encode(), bytes), True)
    check("the query groups nothing - both tables come back row by row",
          [ln.strip() for ln in Q_SESSION.splitlines()
           if "last " in ln and " by " in ln], [])
    check("the lambda's braces balance",
          Q_SESSION.count("{") == Q_SESSION.count("}"), True)
    check("and its brackets do",
          Q_SESSION.count("[") == Q_SESSION.count("]"), True)

    print("\nthe market is the sym suffix")
    check("Hong Kong", market_of("0700.HK"), "HK")
    check("Japan", market_of("7203.JP"), "JP")
    check("Korea", market_of("005930.KS"), "KR")
    check("Malaysia", market_of("1155.MK"), "MY")
    check("Thailand", market_of("PTT.TB"), "TH")
    check("case does not matter", market_of("0700.hk"), "HK")
    check("only the LAST dot is the suffix",
          market_of("BRK.A.HK"), "HK")
    check("another market is not one of ours", market_of("BHP.AU"), None)
    check("Tokyo's Reuters suffix is NOT what this feed uses",
          market_of("7203.T"), None)
    check("a sym with no suffix has no market", market_of("AAPL"), None)
    check("nor does a bare suffix", market_of(".HK"), None)
    check("nor an empty sym", market_of(""), None)
    check("the patterns sent to q come from the same table",
          list(SYM_PATTERNS), ["*.HK", "*.JP", "*.KS", "*.MK", "*.TB"])

    print("\nscope and state")
    p3, _ = to_parents([_p(1, "AU", 100), _p(2, "CN", 100), _p(3, "TH", 100)])
    check("out of scope markets never enter", [p.country for p in p3], ["TH"])
    check("`rejected` is a rejection", is_rejected("rejected"), True)
    check("case and whitespace do not matter", is_rejected(" Rejected "), True)
    check("invalid_ack is NOT a rejection", is_rejected("invalid_ack"), False)
    check("fail_ack is NOT a rejection", is_rejected("fail_ack"), False)
    check("cxlrej is NOT a rejection", is_rejected("cxlrej"), False)
    check("a filled split is not a rejection", is_rejected("filled"), False)

    p4, _ = to_parents([_p(1, "HK", 100)])
    c4 = to_splits([_c(10, 1, 50, "filled"), _c(11, 99, 50, "filled")], p4)
    check("an orphan child is dropped", len(c4), 1)

    check("a null q int reads as zero", _i(-2147483648), 0)
    check("a symbol comes back as text", _s(b"sellshort"), "sellshort")

    # -- the day series --------------------------------------------------------
    print("\nday series")
    d1, d2, d3 = dt.date(2026, 7, 1), dt.date(2026, 7, 2), dt.date(2026, 7, 3)
    pr5 = [_p(1, "HK", 1000, d=d1), _p(1, "HK", 4000, d=d2),
           _p(2, "JP", 1000, d=d2), _p(1, "KR", 1000, d=d3)]
    cr5 = [_c(1, 1, 500, "filled", d=d1),
           _c(1, 1, 1000, "filled", d=d2), _c(2, 1, 0, "rejected", d=d2),
           _c(3, 2, 500, "filled", d=d2), _c(4, 2, 0, "rejected", d=d2),
           _c(1, 1, 0, "rejected", d=d3)]
    #  the same id_work on two dates is two splits, not one - the date is part
    #  of the key, so d1's fill is not overwritten by d2's
    p5, _ = to_parents(pr5)
    c5 = to_splits(cr5, p5)
    days = by_day(p5, c5)
    check("one row per traded date", [d.date for d in days], [d1, d2, d3])
    check("an id_target repeated across dates is two orders",
          [d.orders for d in days], [1, 2, 1])
    check("day completion", [round(d.completion, 1) for d in days],
          [50.0, 30.0, 0.0])
    check("day rejections", [d.rejections for d in days], [0, 2, 1])
    check("the month total matches the day series",
          totals(by_market(p5, c5)).executed, sum(d.executed for d in days))
    check("and so do the rejections",
          totals(by_market(p5, c5)).rejections, sum(d.rejections for d in days))

    check("weekends are not queried",
          len(month_dates(2026, 7)), 23)
    check("month_dates covers the whole month",
          (month_dates(2026, 7)[0], month_dates(2026, 7)[-1]),
          (dt.date(2026, 7, 1), dt.date(2026, 7, 31)))

    # -- the page --------------------------------------------------------------
    print("\npage")
    try:
        import matplotlib      # noqa: F401
    except ImportError:
        print("  ..    matplotlib not installed, rendering skipped")
        return 0 if ok else 1

    import io
    fig = draw(rows, tot, "By market  ·  2026-07-24 18:37",
               "Generated 2026-07-24 18:37  ·  real-time snapshot")
    buf = io.BytesIO()
    fig.savefig(buf, format="pdf", facecolor=SURFACE)
    check("the daily page renders as a PDF", buf.getvalue()[:5], b"%PDF-")
    check("and is not an empty one", len(buf.getvalue()) > 5000, True)

    p6, _ = to_parents([_p(i, "HK", 1000, d=d) for d in month_dates(2026, 7)
                        for i in range(1, 4)])
    c6 = to_splits([_c(i, i, 400 + 10 * i, "filled", d=d)
                      for d in month_dates(2026, 7) for i in range(1, 4)]
                     + [_c(50 + i, i, 0, "rejected", d=d)
                        for d in month_dates(2026, 7) for i in range(1, 3)], p6)
    r6, t6 = by_market(p6, c6), totals(by_market(p6, c6))
    days6 = by_day(p6, c6)
    check("23 day rows for a 23 weekday month", len(days6), 23)
    fig2 = draw(r6, t6, "By market  ·  July 2026",
                "Generated 2026-08-01 09:00  ·  historical, 23 trading days",
                days=days6)
    buf2 = io.BytesIO()
    fig2.savefig(buf2, format="pdf", facecolor=SURFACE)
    check("the monthly page renders as a PDF", buf2.getvalue()[:5], b"%PDF-")
    check("and is bigger than the daily one, having two more charts",
          len(buf2.getvalue()) > len(buf.getvalue()), True)

    fig3 = draw(by_market([], []), totals(by_market([], [])),
                "By market  ·  2026-07-24 18:37", "Generated  ·  x")
    buf3 = io.BytesIO()
    fig3.savefig(buf3, format="pdf", facecolor=SURFACE)
    check("an empty session still renders", buf3.getvalue()[:5], b"%PDF-")

    # -- the email -------------------------------------------------------------
    print("\nemail")
    try:
        m = _mailer()
    except SystemExit as e:
        print(f"  ..    {e}")
        print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
        return 0 if ok else 1

    sub = "By market  ·  2026-07-24 18:37"
    foot = "Generated 2026-07-24 18:37  ·  real-time snapshot"
    text, html = mail_bodies(rows, tot, sub, foot, png_cid="report-page")
    check("the text body carries the headline", "55.7%" in text, True)
    check("and every market, zero rows included",
          all(r.name in text for r in rows), True)
    check("and the rejection counts", "394" in text and "239" in text, True)
    check("the html body references the inlined page",
          'src="cid:report-page"' in html, True)
    check("the html colours the rejections red", RED in html, True)
    check("the html body has no <style> block clients would strip",
          "<style" in html, False)

    # the EMAIL block is module state, so the checks set it and put it back
    print("\nemail is configured in the file, not on the command line")
    check("an empty EMAIL_TO is the off switch", email_configured(), False)
    with _email_config(EMAIL_TO=["desk@example.com, compliance@example.com"],
                       EMAIL_CC=["risk@example.com"],
                       EMAIL_FROM="algo-reports@example.com",
                       SMTP_HOST="mail.example.com", SMTP_PORT=2525,
                       SMTP_TIMEOUT=30, EMAIL_DRY_RUN=True):
        check("filling EMAIL_TO turns it on", email_configured(), True)
        check("the port is taken from the config",
              smtp_config().resolved_port(), 2525)
        check("the timeout too", smtp_config().timeout, 30)

        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            files = save(fig, out, "short_sell_report_2026-07-24")
            check("save writes a pdf and a png",
                  [p.name for p in files],
                  ["short_sell_report_2026-07-24.pdf",
                   "short_sell_report_2026-07-24.png"])
            mail_report(rows, tot, sub, foot, "2026-07-24", files)
            msg = m.build_message(m.Mail(
                subject=f"{TITLE} - 2026-07-24", sender=EMAIL_FROM,
                to=EMAIL_TO, cc=EMAIL_CC, text=text, html=html,
                inline_images=[("report-page", files[1])],
                attachments=[files[0]]))
            check("the pdf is attached and the png inlined",
                  [p.get_filename() for p in msg.walk() if p.get_filename()],
                  ["short_sell_report_2026-07-24.png",
                   "short_sell_report_2026-07-24.pdf"])
            check("a pasted recipient list is split", m.recipients(msg),
                  ["desk@example.com", "compliance@example.com",
                   "risk@example.com"])
            check("EMAIL_DRY_RUN opens no socket",
                  m.send(msg, m.Smtp(), dry_run=True), m.recipients(msg))
    check("and the config goes back afterwards", email_configured(), False)

    with _email_config(EMAIL_TO=["desk@example.com"], EMAIL_FROM=""):
        r = False
        try:
            mail_report(rows, tot, sub, foot, "x", [])
        except SystemExit as e:
            r = "EMAIL_FROM" in str(e)
        check("EMAIL_TO with no EMAIL_FROM says so, naming the block", r, True)

    check("there is nothing to authenticate with anywhere, by design",
          [f for f in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_PASSWORD_ENV",
                       "SMTP_STARTTLS", "USER", "PASSWORD") if f in globals()],
          [])
    check("port 0 means 25", m.Smtp(host="x").resolved_port(), 25)

    check("there are no email flags left on the command line",
          _cli_error(["--email", "a@b.com"]), True)
    check("--monthly with --date is rejected",
          _cli_error(["--monthly", "2026-07", "--date", "2026-07-01"]), True)
    check("a future --date is rejected",
          _cli_error(["--date", "2999-01-01"]), True)

    # -- the preview -----------------------------------------------------------
    print("\ndemo")
    dp, ds = demo_session()
    dr = by_market(dp, ds)
    dt_ = totals(dr)
    check("the daily demo is the page in the README",
          (dt_.orders, dt_.rejections, round(dt_.completion, 1)),
          (732, 394, 55.7))
    check("and is deterministic", totals(by_market(*demo_session())).executed,
          dt_.executed)
    mp, ms = demo_month()
    md = by_day(mp, ms)
    check("the monthly demo covers every weekday", len(md), 23)
    check("with all five markets on it",
          all(r.orders for r in by_market(mp, ms)), True)
    check("and is deterministic too",
          [d.rejections for d in by_day(*demo_month())],
          [d.rejections for d in md])
    with tempfile.TemporaryDirectory() as d:
        check("--demo writes four files", demo(d), 0)
        names = sorted(q.name for q in Path(d).iterdir())
        check("named so nobody mistakes them for a real report", names,
              ["short_sell_report_SAMPLE_daily.pdf",
               "short_sell_report_SAMPLE_daily.png",
               "short_sell_report_SAMPLE_monthly.pdf",
               "short_sell_report_SAMPLE_monthly.png"])

    # -- which mode reaches which server ---------------------------------------
    print("\nmodes")
    now = dt.datetime(2026, 7, 24, 18, 37)
    rt = plan(now=now)
    check("no flags is realtime", rt.hist, False)
    check("and reaches the realtime server", rt.endpoint_name,
          "ORDER_SERVER_RT")
    check("with no date to filter on", rt.dates, [None])
    check("named for today", rt.stem, "short_sell_report_2026-07-24")
    check("and stamped to the minute", rt.when, "2026-07-24 18:37")
    check("not the monthly layout", rt.monthly, False)

    one = plan(date=dt.date(2026, 7, 1), now=now)
    check("--date is historical", one.hist, True)
    check("and reaches the historical server", one.endpoint_name,
          "ORDER_SERVER_HIST")
    check("for exactly one session", one.dates, [dt.date(2026, 7, 1)])
    check("named for that date, not today", one.stem,
          "short_sell_report_2026-07-01")
    check("subtitled with it", one.when, "2026-07-01")
    check("in the DAILY layout - no per day charts for one day",
          one.monthly, False)

    mon = plan(monthly="2026-07", now=now)
    check("--monthly is historical", mon.hist, True)
    check("and reaches the historical server", mon.endpoint_name,
          "ORDER_SERVER_HIST")
    check("over every weekday of the month", len(mon.dates), 23)
    check("named for the month", mon.stem, "short_sell_report_2026-07")
    check("subtitled with it", mon.when, "July 2026")
    check("in the monthly layout", mon.monthly, True)

    raises_ok = False
    try:
        plan(monthly="2026-07", date=dt.date(2026, 7, 1))
    except SystemExit:
        raises_ok = True
    check("the two are mutually exclusive in plan() too", raises_ok, True)
    for bad in ("2026-13", "2026", "july", "26-07", "2026-7", "2026-00",
                "", "2026-07-01"):
        r = False
        try:
            plan(monthly=bad)
        except SystemExit:
            r = True
        check(f"--monthly {bad!r} is rejected", r, True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


class _email_config:
    """Set the EMAIL block for the duration of a block of checks, and put it
    back afterwards.  Module constants rather than arguments is the point of
    this design; the tests have to reach them the same way a person editing the
    file does."""

    def __init__(self, **kw):
        self.kw = kw
        self.old = {}

    def __enter__(self):
        g = globals()
        for k, v in self.kw.items():
            self.old[k] = g[k]
            g[k] = v
        return self

    def __exit__(self, *exc):
        globals().update(self.old)
        return False


def _cli_error(argv) -> bool:
    """Did argparse reject this command line?  Used by the self-test, which must
    not be able to reach run() and therefore kdb."""
    import contextlib
    import io as _io
    try:
        with contextlib.redirect_stderr(_io.StringIO()):
            main(argv)
    except SystemExit as e:
        return e.code not in (0, None)
    return False


# =============================================================================
# CLI
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Short-Sell Order Report - completion and rejections by "
                    "market, as a one page PDF and PNG. Mailing it is "
                    "configured in the EMAIL block at the top of this file, "
                    "not here.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--monthly", metavar="YYYY-MM",
                   help="report a whole month off the HISTORICAL server, and "
                        "add the completion and rejections by day charts")
    p.add_argument("--date", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
                   help="one past session off the HISTORICAL server. Without "
                        "this and without --monthly the report is a REALTIME "
                        "snapshot of the session in progress")
    p.add_argument("--out-dir", default=str(OUT_DIR),
                   help="where the .pdf and .png are written. Any path the "
                        "machine can reach, a network share included")
    p.add_argument("--quiet", action="store_true",
                   help="no per date progress")
    p.add_argument("--self-test", action="store_true",
                   help="run the offline checks and exit - no kdb needed")
    p.add_argument("--demo", action="store_true",
                   help="draw both layouts from MADE UP numbers and exit, so "
                        "the page can be looked at before it is pointed at "
                        "kdb - no connection needed. Stamped SAMPLE")

    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.demo:
        return demo(args.out_dir)
    if args.monthly and args.date:
        p.error("--monthly and --date are alternatives, not a range")
    if args.date and args.date > dt.date.today():
        p.error(f"--date {args.date} is in the future")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
