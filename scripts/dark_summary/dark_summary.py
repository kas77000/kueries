#!/usr/bin/env python3
"""
=============================================================================
dark_summary.py - what we executed in DARK venues, by venue

  of the shares we got done in dark venues, where did they go, and what were
  they worth in USD.

  python scripts/dark_summary/dark_summary.py                    # today, live
  python scripts/dark_summary/dark_summary.py --date 2026-07-01  # one session
  python scripts/dark_summary/dark_summary.py --monthly 2026-07  # a month
  python scripts/dark_summary/dark_summary.py --monthly 2026-07 --csv --raw

  python scripts/dark_summary/dark_summary.py --self-test        # no kdb
  python scripts/dark_summary/dark_summary.py --demo             # sample page

PERCENTAGES ARE SHARES OF THE DARK BOOK, NEVER OF THE DAY'S TRADING.  Nothing
here looks at a lit venue, so this report cannot answer "how much did we do in
the dark" - only "of what we did in the dark, where did it go".

ONE SERVER.  queries/dark_summary/dark_summary.q reads workorder and
target_stock and nothing else, so there is no quote server in this file and no
FX rate to fetch - fxlast rides along on target_stock.

THE QUERY IS NOT COPIED IN HERE.  queries/dark_summary/dark_summary.q is sent
to the server as it stands and darkSummary is called once per date, so this
script, kmonitor/dark_summary/ and a bare q session cannot drift apart.  Both
tables carry `date` on the RDB as well as the HDB, which is what lets the one
function serve the live server and the historical one unchanged.

A VENUE IS DARK WHEN ITS NAME CONTAINS DARK OR DRK.  That match IS the
classification, not an approximation of it - see the query.

NAMES CANNOT BE ADDED UP, and this report does not pretend otherwise.
darkSummary counts distinct syms per venue per day, and a name traded on two
days, or in two venues, is one name in both.  So the column is distinct names
on a single day, name-days over a range - the header says which - and the
TOTAL row is a dash either way, because there is no way to add it correctly.

pykx is imported lazily, so everything off the wire runs on a machine with no
kdb, no pykx and no q licence.
=============================================================================
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import sys
from pathlib import Path
from typing import NamedTuple, Optional

# scripts/lib holds the page these are drawn on.  Added to the path rather
# than installed, so this still runs as
# `python scripts/dark_summary/dark_summary.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.local_config import apply_local                        # noqa: E402
from lib.q_lint import balanced, reserved_used                  # noqa: E402
from lib.report_page import (                                   # noqa: E402
    DASH, INK, INK2, INK3, L, R, figure, fmt_int, footer, heading, kpis, log,
    save, table)

# -----------------------------------------------------------------------------
# CONNECTIONS.  Edit these, or put them in a local_settings.py beside this
# script - see scripts/lib/README.md.
#
# TWO endpoints, not four: this report never opens qatt.  The realtime server
# answers for today, the historical one for a date or a month.
# -----------------------------------------------------------------------------

ORDER_SERVER_RT = "CHANGEME:5012"     # realtime   - workorder, target_stock
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical - the same two, plus `date`

_PLACEHOLDER = "CHANGEME"

OUT_DIR = Path(__file__).resolve().parent / "out"
DPI = 200

QUERY_FILE = (Path(__file__).resolve().parents[2]
              / "queries" / "dark_summary" / "dark_summary.q")

# How many venues get a row of their own before the tail is folded into one.
# A long tail of venues taking a fraction of a percent each is noise on a page
# and detail in a CSV, which is where --csv and --raw put all of them.
TOP_VENUES = 20

# -----------------------------------------------------------------------------
# EMAIL.  Edit these, or put them in local_settings.py.  No command line
# arguments, by design: who gets this report is part of what the report IS,
# not of one run of it - a distribution list that lives in whatever someone
# last typed is a list that quietly loses people.
#
# EMAIL_TO empty means DO NOT SEND.  That is the whole switch; there is no
# separate enable flag to leave in the wrong position.
# -----------------------------------------------------------------------------

EMAIL_TO = []                  # ["desk@example.com"]
EMAIL_CC = []
EMAIL_BCC = []
EMAIL_FROM = ""                # "algo-reports@example.com"

SMTP_HOST = ""                 # "mail.example.com"
SMTP_PORT = 0                  # 0 -> 25
SMTP_TIMEOUT = 30              # seconds

EMAIL_DRY_RUN = False

EMAIL_SIGNATURE = "Best Regards,\n\nKhalife"

# -----------------------------------------------------------------------------
# Anything above can be overridden from a local_settings.py beside this script,
# which git ignores - so the servers survive a pull and this file never has to
# be edited.
# -----------------------------------------------------------------------------

apply_local(globals(), __file__)

TITLE = "Dark Venue Execution"

Y_TITLE, Y_SUBTITLE, Y_RULE_TOP = 0.955, 0.931, 0.9185
Y_KPI_VALUE, Y_KPI_LABEL = 0.884, 0.860
Y_TABLE_TOP, ROW_H = 0.760, 0.028
Y_RULE_BOTTOM, Y_FOOTER = 0.066, 0.048


def venue_cols(multi_day: bool) -> tuple:
    """The table's columns.  The names header is the one thing that changes:
    over a range that column is name-days, and calling it Names would be a
    number nobody could reconcile against anything."""
    return (
        ("Venue", 0.30, False),
        ("Orders", 0.11, True),
        ("Name-days" if multi_day else "Names", 0.11, True),
        ("Shares", 0.16, True),
        ("Notional (USD)", 0.18, True),
        ("% of dark", 0.14, True),
    )


# =============================================================================
# CONNECTION
# =============================================================================

def _check_server(endpoint: str, which: str):
    if _PLACEHOLDER in endpoint:
        raise SystemExit(
            f"{which} is still set to {_PLACEHOLDER}. Put the real one in a "
            f"local_settings.py beside {Path(__file__).name}, or edit the "
            f"constants at the top of it.")


def connect(endpoint: str):
    """Open a PyKX connection.  Host and port; the processes are open.

    pykx is imported here rather than at module level so --self-test, --demo
    and everything else off the wire run without it."""
    try:
        import pykx as kx
    except ImportError:
        raise SystemExit("pykx is not installed.  pip install pykx")
    host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected host:port, got {endpoint!r}")
    return kx.SyncQConnection(host=host, port=int(port))


def load_query(handle):
    """Send queries/dark_summary/dark_summary.q as it stands."""
    handle(QUERY_FILE.read_text(encoding="utf-8"))


def server_today(handle) -> dt.date:
    """The date on the SERVER, for the live run.

    Not this machine's.  The plant's clock runs ahead of UTC, so either side of
    midnight here the two are different days and a local date would ask the
    live server for a session it does not hold."""
    return handle(".z.D").py()


#  The columns darkSummary returns when it has rows.  When a day has no dark
#  fills at all the query returns early with its own empty workorder slice,
#  which carries none of these - so this is the test for "no rows", and it is
#  the untested edge kmonitor/dark_summary/README.md warns about.
RESULT_COLS = ("venue", "orders", "syms", "shares", "notional_usd")


def fetch_day(handle, d: dt.date) -> list:
    r = handle("darkSummary", d).pd()
    if not len(r) or not all(c in r.columns for c in RESULT_COLS):
        return []
    return r.to_dict("records")


# =============================================================================
# READING WHAT CAME BACK
# =============================================================================

def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _i(v) -> int:
    """A q int or long null reaches pandas as NaN or as a sentinel; both mean
    no quantity, and both would otherwise poison a sum."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if n in (-2147483648, -9223372036854775808) else n


def _f(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f          # NaN


# =============================================================================
# THE NUMBERS
# =============================================================================

class Row(NamedTuple):
    venue: str
    orders: int
    names: int
    shares: int
    notional_usd: float
    pct: Optional[float]                 # share of the dark book, or None


class Totals(NamedTuple):
    orders: int
    shares: int
    notional_usd: float
    venues: int


def to_rows(per_day) -> list:
    """One row per venue, summed across the dates, ordered by notional.

    THE PERCENTAGE IS RECOMPUTED, never summed.  Over a range it is each
    venue's share of the WHOLE range - which is the honest answer to where the
    flow went, and is not the same as averaging the daily shares: a venue that
    took everything on one quiet day and nothing since reads small here, as it
    should.  Same convention as kmonitor/dark_summary/.

    per_day is [(date, [record, ...]), ...] - the raw darkSummary rows.
    """
    agg: dict = {}
    for _, recs in per_day:
        for rec in recs:
            v = _s(rec.get("venue"))
            cur = agg.setdefault(v, [0, 0, 0, 0.0])
            cur[0] += _i(rec.get("orders"))
            cur[1] += _i(rec.get("syms"))
            cur[2] += _i(rec.get("shares"))
            cur[3] += _f(rec.get("notional_usd"))
    tot = sum(c[3] for c in agg.values())
    rows = [Row(v, c[0], c[1], c[2], c[3],
                (100.0 * c[3] / tot) if tot > 0 else None)
            for v, c in agg.items()]
    #  by notional, then by name so two venues worth the same never swap
    #  places between two runs of the same day
    return sorted(rows, key=lambda r: (-r.notional_usd, r.venue))


def fold_tail(rows: list, top: int) -> list:
    """The top venues by notional, with the rest as one row.

    The tail is FOLDED, not dropped: its notional still counts, so the
    percentages on the page still add to 100 and the total still reconciles.
    --csv and --raw carry every venue."""
    if top <= 0 or len(rows) <= top:
        return list(rows)
    head, tail = rows[:top], rows[top:]
    return head + [Row(
        f"Other ({len(tail)} venues)",
        sum(r.orders for r in tail),
        sum(r.names for r in tail),
        sum(r.shares for r in tail),
        sum(r.notional_usd for r in tail),
        sum(r.pct for r in tail if r.pct is not None) or None,
    )]


def totals(rows: list) -> Totals:
    """Everything that CAN be added.  names is not here on purpose - see the
    note at the top of this file."""
    return Totals(sum(r.orders for r in rows),
                  sum(r.shares for r in rows),
                  sum(r.notional_usd for r in rows),
                  len(rows))


# =============================================================================
# FORMATTING
# =============================================================================

def fmt_usd(v) -> str:
    """A USD notional in the unit the number is actually in.  Zero prints as a
    dash: it means the fills could not be valued, not that they were worth
    nothing.  Same formatter as luld_orders and short_sell_report."""
    v = float(v or 0.0)
    if v <= 0:
        return DASH
    for cut, suf in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if v >= cut:
            return f"{v / cut:,.1f}{suf}"
    return f"{v:,.0f}"


def fmt_shares(n) -> str:
    n = _i(n)
    if n <= 0:
        return DASH
    for cut, suf in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if n >= cut:
            return f"{n / cut:,.1f}{suf}"
    return f"{n:,.0f}"


def fmt_pct2(v) -> str:
    """Two decimals, matching darkSummary's own pct_notional.  None is a dash:
    it means there was nothing to take a share OF."""
    return DASH if v is None else f"{v:.2f}%"


# =============================================================================
# THE PAGE
# =============================================================================

def _row_cells(r: Row) -> list:
    return [(r.venue, INK, "normal"),
            (fmt_int(r.orders), INK if r.orders else INK3, "normal"),
            (fmt_int(r.names), INK if r.names else INK3, "normal"),
            (fmt_shares(r.shares), INK, "normal"),
            (fmt_usd(r.notional_usd), INK, "normal"),
            (fmt_pct2(r.pct), INK, "bold")]


def draw(rows, tot: Totals, subtitle: str, foot: str, multi_day: bool,
         note: str = ""):
    """The one page: where our dark flow went."""
    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)

    kpis(fig, [(fmt_usd(tot.notional_usd), "Dark notional (USD)", INK),
               (fmt_shares(tot.shares), "Shares executed", INK),
               (fmt_int(tot.venues), "Venues", INK)],
         Y_KPI_VALUE, Y_KPI_LABEL)

    #  ONE SENTENCE PER LINE, and short ones.  The page is proportional type
    #  with a hard right margin and nothing wraps: a line that runs long is
    #  silently CLIPPED, which is worse than no note at all.  overflowing()
    #  measures it, and --self-test fails on it.
    for i, line in enumerate((
            "Shares we EXECUTED in dark venues, by venue. A venue is dark "
            "when its name contains DARK or DRK.",
            "Notional is the fill price in USD, at the parent order's own "
            "fxlast, so it is what those shares really cost.",
            "% of dark is a share of the DARK book, never of the day's "
            "trading - nothing here looks at a lit venue.")):
        fig.text(L, 0.816 - 0.016 * i, line, fontsize=8, color=INK2,
                 va="baseline")

    cols = venue_cols(multi_day)
    y = table(fig, cols, [_row_cells(r) for r in rows], Y_TABLE_TOP, ROW_H)

    #  the total sits under the rows, on the same column edges, with no second
    #  header band over it
    _total_line(fig, cols, tot, y - 0.024)

    notes = [("Name-days, not names: a stock traded on two days counts twice."
              if multi_day else
              "Names is distinct stocks in that venue.")
             + " It cannot be added across venues either, so the total is a "
               "dash."]
    if multi_day:
        notes.append("% of dark is each venue's share of the WHOLE range, not "
                     "an average of its daily shares - a venue big on one "
                     "quiet day and quiet since reads small.")
    for i, line in enumerate(notes):
        fig.text(L, y - 0.056 - 0.018 * i, line, fontsize=7.5, color=INK2,
                 va="baseline")
    if note:
        fig.text(L, y - 0.056 - 0.018 * len(notes) - 0.008, note,
                 fontsize=7.5, color=INK3, va="baseline")

    footer(fig, foot, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def overflowing(fig) -> list:
    """Every line on the page that runs past the right margin.

    MEASURED, NOT COUNTED IN CHARACTERS.  The page is proportional type, so
    the only way to know a sentence fits is to render it and ask where it
    ended - and a clipped note is invisible in a self-test that only checks
    the file is not empty.  Returns the offending text, so a failure names
    the line to shorten."""
    fig.canvas.draw()
    r = fig.canvas.get_renderer()
    inv = fig.transFigure.inverted()
    out = []
    for t in fig.texts:
        bb = t.get_window_extent(r).transformed(inv)
        if bb.x1 > R + 0.001 or bb.x0 < L - 0.001:
            out.append(t.get_text()[:70])
    return out


def _total_line(fig, cols, tot: Totals, y):
    """The total row, drawn on the table's own column edges so it cannot drift
    out of line with the rows above it."""
    x = L
    span = R - L
    texts = ["Total", fmt_int(tot.orders), DASH, fmt_shares(tot.shares),
             fmt_usd(tot.notional_usd), "100.00%" if tot.notional_usd else DASH]
    for (label, frac, right), text in zip(cols, texts):
        w = frac * span
        tx = x + w - 0.008 if right else x + 0.010
        fig.text(tx, y, text, ha="right" if right else "left", va="baseline",
                 fontsize=9, fontweight="bold", color=INK)
        x += w


# =============================================================================
# CSV
# =============================================================================

CSV_COLS = ["venue", "orders", "names", "shares", "notional_usd",
            "pct_of_dark"]


def csv_rows(rows, tot: Totals) -> list:
    """Every venue, unfolded, at full precision - the page rounds, this does
    not.  The total's names cell is empty rather than 0: there is no correct
    number to put there."""
    out = [[r.venue, r.orders, r.names, r.shares,
            round(r.notional_usd, 2),
            "" if r.pct is None else round(r.pct, 4)] for r in rows]
    out.append(["Total", tot.orders, "", tot.shares,
                round(tot.notional_usd, 2),
                100.0 if tot.notional_usd else ""])
    return out


def write_csv(rows, tot: Totals, out_dir, stem) -> Path:
    p = Path(out_dir) / f"{stem}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLS)
        w.writerows(csv_rows(rows, tot))
    log(f"  wrote {p}")
    return p


RAW_COLS = ["date", "venue", "orders", "names", "shares", "notional_usd",
            "pct_of_that_day"]


def raw_rows(per_day) -> list:
    """VENUE BY DAY - one line per venue per date, which is the thing the page
    sums away and nothing else in this repo produces.  pct here is that day's
    own share, straight off darkSummary, NOT a share of the range."""
    out = []
    for d, recs in per_day:
        for rec in sorted(recs, key=lambda r: -_f(r.get("notional_usd"))):
            out.append([str(d), _s(rec.get("venue")), _i(rec.get("orders")),
                        _i(rec.get("syms")), _i(rec.get("shares")),
                        round(_f(rec.get("notional_usd")), 2),
                        round(_f(rec.get("pct_notional")), 4)])
    return out


def write_raw(per_day, out_dir, stem) -> Path:
    p = Path(out_dir) / f"{stem}_raw.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RAW_COLS)
        w.writerows(raw_rows(per_day))
    log(f"  wrote {p}")
    return p


# =============================================================================
# WHAT TO ASK, AND OF WHICH SERVER
# =============================================================================

class Plan(NamedTuple):
    hist: bool
    monthly: bool
    server: str
    server_name: str
    dates: list                          # [None] until the live date is read
    stem: str
    subtitle: str
    when: str


def parse_month(s: str):
    try:
        y, m = s.split("-")
        y, m = int(y), int(m)
        if not 1 <= m <= 12:
            raise ValueError
    except (ValueError, AttributeError):
        raise SystemExit(f"--monthly wants YYYY-MM, not {s!r}")
    return y, m


def month_dates(y: int, m: int) -> list:
    """Every calendar day of the month.  Weekends and holidays come back
    empty, which costs one round trip each and needs no exchange calendar -
    and a calendar that is wrong is worse than a query that returns nothing."""
    n = calendar.monthrange(y, m)[1]
    return [dt.date(y, m, i + 1) for i in range(n)]


def plan(monthly, date, now=None) -> Plan:
    now = now or dt.datetime.now()
    hist = monthly is not None or date is not None
    server = ORDER_SERVER_HIST if hist else ORDER_SERVER_RT
    name = "ORDER_SERVER_HIST" if hist else "ORDER_SERVER_RT"
    if monthly is not None:
        y, m = parse_month(monthly)
        return Plan(True, True, server, name, month_dates(y, m),
                    f"dark_summary_{y:04d}-{m:02d}",
                    f"By venue  ·  {calendar.month_name[m]} {y}",
                    f"{calendar.month_name[m]} {y}")
    if date is not None:
        return Plan(True, False, server, name, [date],
                    f"dark_summary_{date:%Y-%m-%d}",
                    f"By venue  ·  {date}", f"{date}")
    #  live: the date is a placeholder until the server is asked for its own,
    #  and relabel() replaces all three labels with what it says
    return Plan(False, False, server, name, [None],
                f"dark_summary_{now:%Y-%m-%d}",
                f"By venue  ·  {now:%Y-%m-%d %H:%M}", f"{now:%Y-%m-%d %H:%M}")


def relabel(pl: Plan, d: dt.date, now=None) -> Plan:
    """The live plan, once the server has said what day it is."""
    now = now or dt.datetime.now()
    return pl._replace(dates=[d], stem=f"dark_summary_{d:%Y-%m-%d}",
                       subtitle=f"By venue  ·  {d}, {now:%H:%M} so far",
                       when=f"{d} {now:%H:%M}")


# =============================================================================
# RUN
# =============================================================================

def run(args) -> int:
    pl = plan(args.monthly, args.date)
    _check_server(pl.server, pl.server_name)

    log(f"dark_summary  {'historical' if pl.hist else 'realtime'}  "
        f"orders {pl.server}")
    h = connect(pl.server)
    load_query(h)
    if not pl.hist:
        pl = relabel(pl, server_today(h))
        log(f"  the server says it is {pl.dates[0]}")

    per_day, empty = [], 0
    for d in pl.dates:
        recs = fetch_day(h, d)
        if not recs:
            empty += 1
            continue
        if not args.quiet:
            log(f"  {d}  {len(recs):>3} venues")
        per_day.append((d, recs))

    if not per_day:
        log(f"  no dark fills at all over {pl.when}.  A venue is dark when its "
            f"name contains DARK or DRK - if that is not this server's "
            f"vocabulary, nothing here can match.")
        return 1

    every = to_rows(per_day)
    rows = fold_tail(every, args.top)
    tot = totals(every)
    note = ""
    if len(every) > len(rows):
        note = (f"{len(every) - len(rows)} venues below the top {args.top} by "
                f"notional are folded into one row. --csv lists them all.")
    if pl.monthly and empty:
        note = (note + "  " if note else "") + (
            f"{empty} of {len(pl.dates)} days had no dark fills.")

    log(f"  {len(every)} venues, {fmt_shares(tot.shares)} shares, "
        f"{fmt_usd(tot.notional_usd)} over {len(per_day)} day"
        f"{'' if len(per_day) == 1 else 's'}")

    fig = draw(rows, tot, pl.subtitle,
               f"{TITLE}  ·  {pl.when}  ·  {pl.server}", pl.monthly, note)
    files = save(fig, args.out_dir, pl.stem, DPI)
    if args.csv:
        files.append(write_csv(every, tot, args.out_dir, pl.stem))
    if args.raw:
        files.append(write_raw(per_day, args.out_dir, pl.stem))

    if args.no_email:
        log("  --no-email: not sent")
    elif email_configured():
        mail_report(pl.when, files)
    return 0


# =============================================================================
# EMAIL
# =============================================================================

def _cfg(name, default):
    """A setting, or its default.  Reads the module globals rather than naming
    each constant, so a local_settings.py that overrides one is picked up
    without this having to know which."""
    return globals().get(name, default)


def _mailer():
    try:
        from lib import mailer
    except ImportError as e:                     # noqa: BLE001
        raise SystemExit(
            f"EMAIL_TO is set but scripts/lib/mailer.py will not import "
            f"({e}).  It sits beside this script's folder; copy scripts/lib "
            f"too if you moved this one.")
    return mailer


def email_configured() -> bool:
    """No recipients is the off switch, and it is the whole switch."""
    return bool(_cfg("EMAIL_TO", []) or _cfg("EMAIL_CC", [])
                or _cfg("EMAIL_BCC", []))


def mail_body() -> str:
    """The sign-off, and nothing else.  The report is the ATTACHMENT: a body
    that restates the table is a second copy of the numbers to keep in step."""
    return _cfg("EMAIL_SIGNATURE", "Best Regards,")


def mail_report(when, files) -> None:
    m = _mailer()
    pdf = next((q for q in files if q.suffix == ".pdf"), None)
    sender = _cfg("EMAIL_FROM", "")
    if not sender:
        raise SystemExit(
            "EMAIL_TO is set but EMAIL_FROM is empty. Both live in the EMAIL "
            "block near the top of dark_summary.py, or in a local_settings.py "
            "beside it.")
    if pdf is None:
        raise SystemExit("nothing to attach: no PDF was written")

    msg = m.build_message(m.Mail(
        subject=f"{TITLE} - {when}", sender=sender,
        to=_cfg("EMAIL_TO", []), cc=_cfg("EMAIL_CC", []),
        bcc=_cfg("EMAIL_BCC", []),
        text=mail_body(), attachments=[pdf]))
    smtp = m.Smtp(host=_cfg("SMTP_HOST", ""), port=_cfg("SMTP_PORT", 0),
                  timeout=_cfg("SMTP_TIMEOUT", 30))
    log("  email:")
    log(m.describe(msg))
    dry = _cfg("EMAIL_DRY_RUN", False)
    rcpt = m.send(msg, smtp, dry_run=dry)
    n = len(rcpt)
    if dry:
        log(f"  EMAIL_DRY_RUN: NOT sent, {n} recipient"
            f"{'' if n == 1 else 's'} would have been")
    else:
        log(f"  sent to {n} recipient{'' if n == 1 else 's'} via "
            f"{smtp.host}:{smtp.resolved_port()}")


# =============================================================================
# DEMO
# =============================================================================

def _fake_day(d: dt.date, scale: float = 1.0) -> list:
    """One day of darkSummary rows: a couple of venues carrying most of it and
    a tail, which is the shape a real dark book has."""
    base = [("UBS-DARK", 41, 26, 3_100_000, 18_400_000.0),
            ("CS-CROSSFINDER-DRK", 33, 22, 2_400_000, 12_900_000.0),
            ("MS-POOL-DARK", 21, 15, 1_050_000, 6_200_000.0),
            ("JPM-DRK", 12, 9, 480_000, 2_700_000.0),
            ("BARX-DARK", 7, 6, 190_000, 980_000.0),
            ("LIQUIDNET-DARK", 3, 3, 90_000, 410_000.0)]
    tot = sum(b[4] for b in base) * scale
    return [{"venue": v.encode(), "orders": o, "syms": s,
             "shares": int(sh * scale), "notional_usd": n * scale,
             "pct_notional": round(100.0 * n * scale / tot, 2)}
            for v, o, s, sh, n in base]


def _fake_month(n=3) -> list:
    return [(dt.date(2026, 7, 1 + i), _fake_day(dt.date(2026, 7, 1 + i),
                                                1.0 + 0.3 * i))
            for i in range(n)]


def demo(out_dir) -> int:
    per_day = _fake_month(3)
    every = to_rows(per_day)
    rows = fold_tail(every, TOP_VENUES)
    tot = totals(every)
    fig = draw(rows, tot, "By venue  ·  July 2026  (DEMO, synthetic data)",
               f"{TITLE}  ·  DEMO  ·  no server was contacted", True,
               "Synthetic data. Nothing here came off a server.")
    save(fig, out_dir, "dark_summary_demo", DPI)
    return 0


# =============================================================================
# SELF TEST.  No kdb, no server.
# =============================================================================

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"\n          got {got!r}, want {want!r}"))

    print("dark_summary --self-test\n\nthe q it sends")
    check("the query file is where this expects it", QUERY_FILE.exists(), True)
    src = QUERY_FILE.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("/"))
    check("its braces and brackets balance", balanced(code), True)
    check("no q reserved word is used as a name", reserved_used(code), [])
    check("darkSummary is what it defines", "darkSummary:{" in code, True)
    check("and it takes one date", "darkSummary:{[dt]" in code, True)
    #  the classification lives in the q and nowhere else - if this ever moves,
    #  the note on the page and in the README is wrong
    check("dark is still DARK or DRK", '("*DARK*";"*DRK*")' in code, True)
    check("every column this script reads is selected by it",
          all(c in code for c in RESULT_COLS), True)

    print("\nwhich server, and which dates")
    now = dt.datetime(2026, 7, 15, 14, 30)
    p_live = plan(None, None, now)
    check("no argument is the realtime server", p_live.server_name,
          "ORDER_SERVER_RT")
    check("and it asks for one day", len(p_live.dates), 1)
    check("which is not known until the server says",
          p_live.dates[0], None)
    p_day = plan(None, dt.date(2026, 7, 1), now)
    check("a date is the historical server", p_day.server_name,
          "ORDER_SERVER_HIST")
    check("for that one day", p_day.dates, [dt.date(2026, 7, 1)])
    check("stem names the day", p_day.stem, "dark_summary_2026-07-01")
    p_mo = plan("2026-07", None, now)
    check("a month is historical too", p_mo.server_name, "ORDER_SERVER_HIST")
    check("with every calendar day in it", len(p_mo.dates), 31)
    check("weekends included - no exchange calendar is guessed at",
          sorted({d.isoweekday() for d in p_mo.dates}), [1, 2, 3, 4, 5, 6, 7])
    check("and it is a month, which changes the names header", p_mo.monthly,
          True)
    check("a February leap year is 29 days", len(plan("2024-02", None).dates),
          29)
    check("relabel takes the day off the server",
          relabel(p_live, dt.date(2026, 7, 16), now).dates,
          [dt.date(2026, 7, 16)])
    check("and renames the file with it",
          relabel(p_live, dt.date(2026, 7, 16), now).stem,
          "dark_summary_2026-07-16")

    print("\nadding the days up")
    one = [(dt.date(2026, 7, 1), _fake_day(dt.date(2026, 7, 1)))]
    r1 = to_rows(one)
    check("one row per venue", len(r1), 6)
    check("ordered by notional, biggest first", r1[0].venue, "UBS-DARK")
    check("a symbol's bytes become text", isinstance(r1[0].venue, str), True)
    check("the percentages add to 100",
          round(sum(r.pct for r in r1), 6), 100.0)
    two = _fake_month(2)
    r2 = to_rows(two)
    check("two days sum the shares",
          r2[0].shares,
          sum(d[1][0]["shares"] for d in two))
    check("and the notional",
          round(r2[0].notional_usd, 2),
          round(sum(d[1][0]["notional_usd"] for d in two), 2))
    check("the percentage is RECOMPUTED over the range, not summed",
          round(sum(r.pct for r in r2), 6), 100.0)
    #  the same venue on both days, scaled - so a summed pct would exceed 100
    check("summing the daily percentages would have been wrong",
          round(sum(_f(rec["pct_notional"])
                    for _, recs in two for rec in recs), 1), 200.0)
    check("a day with no dark fills contributes nothing",
          to_rows([(dt.date(2026, 7, 1), [])]), [])
    check("and neither does no days at all", to_rows([]), [])
    check("nothing to take a share of leaves the percentage unmeasured",
          to_rows([(dt.date(2026, 7, 1),
                    [{"venue": b"X", "orders": 1, "syms": 1, "shares": 0,
                      "notional_usd": 0.0}])])[0].pct, None)

    print("\nthe totals, and the one that cannot be added")
    t = totals(r1)
    check("orders add", t.orders, sum(r.orders for r in r1))
    check("shares add", t.shares, sum(r.shares for r in r1))
    check("venues is a count of the rows", t.venues, 6)
    check("names is NOT in the totals at all",
          "names" in Totals._fields, False)
    check("and the total row prints it as a dash",
          csv_rows(r1, t)[-1][2], "")

    print("\nfolding the tail")
    f = fold_tail(r1, 3)
    check("the top venues keep their own rows", len(f), 4)
    check("the last one is the fold", f[-1].venue, "Other (3 venues)")
    check("nothing is lost from the notional",
          round(sum(r.notional_usd for r in f), 2),
          round(sum(r.notional_usd for r in r1), 2))
    check("nor from the shares",
          sum(r.shares for r in f), sum(r.shares for r in r1))
    check("so the page still adds to 100",
          round(sum(r.pct for r in f), 6), 100.0)
    check("a top bigger than the list folds nothing", fold_tail(r1, 99), r1)
    check("and so does no limit at all", fold_tail(r1, 0), r1)

    print("\nwhat the CSVs carry")
    check("the table CSV has every venue plus a total",
          len(csv_rows(r1, t)), 7)
    check("at full precision, not the page's rounding",
          csv_rows(r1, t)[0][4], round(r1[0].notional_usd, 2))
    raw = raw_rows(two)
    check("the raw CSV is venue BY DAY", len(raw), 12)
    check("with the date on every line", raw[0][0], "2026-07-01")
    check("and the second day too", raw[-1][0], "2026-07-02")
    check("its percentage is that day's own, not the range's",
          raw[0][6], round(_f(two[0][1][0]["pct_notional"]), 4))
    check("the raw columns say so in their names",
          RAW_COLS[-1], "pct_of_that_day")

    print("\nreading the numbers")
    check("18.4m reads as 18.4m", fmt_usd(18_400_000), "18.4m")
    check("and 1.2bn as 1.2bn", fmt_usd(1_234_000_000), "1.2bn")
    check("nothing valued is a dash, not a zero", fmt_usd(0), DASH)
    check("shares use the same scale", fmt_shares(3_100_000), "3.1m")
    check("two decimals on a percentage", fmt_pct2(12.3456), "12.35%")
    check("an unmeasured percentage is a dash", fmt_pct2(None), DASH)
    check("a q null share count does not read as a quantity",
          _i(-2147483648), 0)
    check("nor does a NaN notional", _f(float("nan")), 0.0)

    print("\nthe page")
    check("a range says name-days", venue_cols(True)[2][0], "Name-days")
    check("one day says names", venue_cols(False)[2][0], "Names")
    check("the columns fill the width exactly",
          round(sum(c[1] for c in venue_cols(True)), 6), 1.0)
    with tempfile.TemporaryDirectory() as d:
        fig = draw(fold_tail(r1, TOP_VENUES), t, "sub", "foot", False)
        #  nothing on the page may run past the margin - see overflowing()
        check("no line runs off the page, one day", overflowing(fig), [])
        wide = draw(fold_tail(r1, TOP_VENUES), t,
                    "By venue  ·  September 2026", f"{TITLE}  ·  foot", True,
                    "12 venues below the top 20 by notional are folded into "
                    "one row. --csv lists them all.  4 of 30 days had no dark "
                    "fills.")
        check("nor over a range, with every note on it", overflowing(wide), [])
        files = save(fig, d, "dark_summary_test")
        check("one PDF for the page", len([f for f in files
                                           if f.suffix == ".pdf"]), 1)
        check("and it is not an empty file",
              all(f.stat().st_size > 5000 for f in files), True)
        #  20 venues is the default cap, and the page has to hold them
        big = to_rows([(dt.date(2026, 7, 1),
                        [{"venue": f"V{i:02d}-DARK".encode(), "orders": i + 1,
                          "syms": i, "shares": 1000 * (i + 1),
                          "notional_usd": 1000.0 * (30 - i)}
                         for i in range(30)])])
        fig2 = draw(fold_tail(big, TOP_VENUES), totals(big), "sub", "foot",
                    True, "note")
        check("a full page of venues still draws", fig2 is not None, True)
        check("and still fits inside the margins", overflowing(fig2), [])
        check("a long venue name does not push the table out",
              overflowing(draw(
                  [Row("SOME-VERY-LONG-VENUE-NAME-DARK-POOL-EU", 1, 1, 1,
                       1.0, 100.0)],
                  Totals(1, 1, 1.0, 1), "sub", "foot", False)), [])
        check("with the tail folded into one row",
              len(fold_tail(big, TOP_VENUES)), TOP_VENUES + 1)
        check("the demo renders", demo(d), 0)

    print("\nemail")
    check("no recipients means do not send", email_configured(), False)
    globals()["EMAIL_BCC"] = ["x@y.com"]
    check("a bcc alone still counts as configured", email_configured(), True)
    globals()["EMAIL_BCC"] = []
    check("and putting it back turns it off again", email_configured(), False)
    check("nothing to authenticate with, by design",
          [f for f in ("SMTP_USER", "SMTP_PASSWORD", "PASSWORD")
           if f in globals()], [])
    check("the body is the signature and nothing else",
          mail_body(), "Best Regards,\n\nKhalife")
    check("no table in it", "Venue" in mail_body(), False)
    check("no numbers in it", any(c.isdigit() for c in mail_body()), False)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# =============================================================================
# CLI
# =============================================================================

def build_parser():
    p = argparse.ArgumentParser(
        description="Dark venue execution by venue - shares and USD notional. "
                    "Servers are configured at the top of this file, or in a "
                    "local_settings.py beside it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--monthly", metavar="YYYY-MM",
                   help="a whole month off the HISTORICAL server")
    p.add_argument("--date", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
                   help="one past session off the HISTORICAL server")
    p.add_argument("--top", type=int, default=TOP_VENUES, metavar="N",
                   help="venues with a row of their own; the rest fold into "
                        "one. 0 shows every venue")
    p.add_argument("--csv", action="store_true",
                   help="also write the table as CSV beside the PDF, every "
                        "venue and at full precision")
    p.add_argument("--raw", action="store_true",
                   help="also write one line per venue PER DAY, as "
                        "<stem>_raw.csv")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--demo", action="store_true",
                   help="render a sample off synthetic data, no kdb needed")
    p.add_argument("--self-test", action="store_true",
                   help="check the analytics and the q, no kdb needed")
    p.add_argument("--no-email", action="store_true",
                   help="write the report but do not send it, whatever "
                        "EMAIL_TO says")
    p.add_argument("--quiet", action="store_true")
    return p


def main(argv=None) -> int:
    p = build_parser()
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
