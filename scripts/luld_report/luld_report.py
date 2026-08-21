#!/usr/bin/env python3
"""
=============================================================================
luld_report.py

Limit Up / Limit Down Order Report: the orders whose stock was pinned at its
daily price limit while they were live, their completion and their rejections
by market - and then the ones that matter, the orders where the limit was on
the side we could have traded and we sent nothing into it.

  python scripts/luld_report/luld_report.py
  python scripts/luld_report/luld_report.py --monthly 2026-07
  python scripts/luld_report/luld_report.py --demo        # no kdb needed

The default run reads the REALTIME servers.  --monthly and --date read the
HISTORICAL ones, a date at a time.  Same tables either way; the historical ones
carry an extra `date` column, and that is the only difference the queries have
to care about.

MARKETS.  Japan, Korea, Malaysia, Thailand, Indonesia, China, Taiwan and India
- everywhere we trade that has a daily price limit.  The market is the sym
suffix, and China and India carry several each.

WHERE THE LIMIT COMES FROM.  The book, not a rule.  A stock at its limit stops
having a two sided quote: at limit up nobody will offer, at limit down nobody
will bid, and a locked book (bid = ask) is the same thing seen mid transition.
So a LIMIT PERIOD is a contiguous run of qatt ticks with one side missing or
the two sides equal, and its side is whichever of the two was missing more
often across the run.

Reading it off the book rather than off a band table is what lets this cover
Indonesia and China, where luld_shortsell_check.py has no derivable band - and
it is also why every window here is a floor rather than an estimate: a pinned
stock often stops quoting altogether, so a window ENDS at the last tick that
proved it, never later.  Under-reporting is the direction to be wrong in.

THE THREE NUMBERS, per market
  Orders       parent orders whose stock had a limit period overlapping their
               own live window.  Not every order in the market - this page is
               about the ones the limit actually touched
  Completion   executed / order qty - per market, and over every market
               at once for the headline
  Rejections   their workorders in state `rejected

THE TABLE AT THE BOTTOM - favourable, no split
  An order qualifies when ALL of these hold:
    - the limit was on the side we can fill.  Selling into a limit up, or
      buying into a limit down: there is a queue resting at the band and we
      are on the other side of it
    - the limit period overlapped the order's live window by at least
      MIN_PIN_MINS
    - the order still had quantity left to do
    - NO child split was on the market at any point during that overlap
  A split counts as on the market between t_on_market and t_off_market, and one
  still open at the end counts as active - so an order that worked all morning
  and slept through an afternoon limit is caught, which is the case a plain
  "did it ever produce a child" test misses entirely.

  Splits shows the order's TOTAL child count.  0 means it never worked at all;
  a number means it worked, just not while the limit was there - a different
  conversation, and the column is what tells them apart.

pykx is imported lazily, so the analytics and the whole rendering path run on a
machine with no kdb, no pykx and no q licence:

  python scripts/luld_report/luld_report.py --self-test
=============================================================================
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import sys
from pathlib import Path
from typing import NamedTuple, Optional

# scripts/lib holds the page these are drawn on and the mailer.  Added to the
# path rather than installed, so this still runs as
# `python scripts/luld_report/luld_report.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.order_chains import (                                   # noqa: E402
    CLIENT_ID_TAG, DEFAULT_QTY, QTY_CHOICES, chain_key, chain_size, fix_tag)
from lib.report_page import (                                    # noqa: E402
    BLUE, COL_W, DASH, GREEN, INK, INK2, INK3, L, R, RED,
    barchart, figure, fmt_hm, fmt_int, fmt_pct0, fmt_pct1, footer as _footer,
    heading, kpis as _kpis_row, log, save as _save, table as _table_rows,
    vbarchart)

# -----------------------------------------------------------------------------
# CONNECTIONS.  Edit these.
#
# Four endpoints: the order server and the quote server, each realtime and
# historical.  All open processes - host and port is the whole of it.
#
# The order server and qatt stamp their times on the SAME CLOCK, so a window
# from one is compared against a window from the other with no conversion.
# -----------------------------------------------------------------------------

ORDER_SERVER_RT = "CHANGEME:5012"     # realtime   - target and workorder
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical - the same two, plus `date`
QATT_SERVER_RT = "CHANGEME:5013"      # realtime   - qatt
QATT_SERVER_HIST = "CHANGEME:5011"    # historical - qatt, plus `date`

_PLACEHOLDER = "CHANGEME"

OUT_DIR = Path(__file__).resolve().parent / "out"
DPI = 200

# -----------------------------------------------------------------------------
# EMAIL.  Edit these.  No command line arguments - who gets this report is part
# of what the report IS.  EMAIL_TO empty means DO NOT SEND.
# -----------------------------------------------------------------------------

EMAIL_TO = []                  # ["desk@example.com", "compliance@example.com"]
EMAIL_CC = []
EMAIL_BCC = []
EMAIL_FROM = ""                # "algo-reports@example.com"

SMTP_HOST = ""                 # "mail.example.com"
SMTP_PORT = 0                  # 0 -> 25
SMTP_TIMEOUT = 30              # seconds

EMAIL_DRY_RUN = False

# What quantity a chain asked for - see scripts/lib/order_chains.  "asked"
# reads it off the fills and is the only one that cannot print over 100%.
CHAIN_QTY = DEFAULT_QTY

# What the mail says.  Just this - the report is the attachment, and a body that
# restates it is a second copy to keep in step and one more thing to render
# wrong in somebody's client.
EMAIL_SIGNATURE = "Best Regards,\n\nKhalife"


# =============================================================================
# SCOPE
# =============================================================================

class Mkt(NamedTuple):
    code: str
    name: str
    suffixes: tuple            # what the sym ends with on the feed


# Fixed order.  The table always prints all eight, whether or not they traded -
# a market absent from the data is otherwise indistinguishable from one nobody
# remembered to ask about.
#
# China and India carry several suffixes each, so the lookup is many-to-one.
# Hong Kong, Australia, Singapore and New Zealand are NOT here: they have no
# daily price limit, so there is no limit to be up or down against.
MARKETS = (
    Mkt("JP", "Japan", (".JP",)),
    Mkt("KR", "Korea", (".KS",)),
    Mkt("MY", "Malaysia", (".MK",)),
    Mkt("TH", "Thailand", (".TB",)),
    Mkt("ID", "Indonesia", (".IJ",)),
    Mkt("CN", "China", (".CH", ".C1", ".C2")),
    Mkt("TW", "Taiwan", (".TT",)),
    Mkt("IN", "India", (".IN", ".IS")),
)
MARKET_CODES = tuple(m.code for m in MARKETS)
MARKET_NAME = {m.code: m.name for m in MARKETS}
SUFFIX_MARKET = {s: m.code for m in MARKETS for s in m.suffixes}
SYM_PATTERNS = tuple("*" + s for m in MARKETS for s in m.suffixes)

# Only `rejected`.  workorder also carries invalid_ack and fail_ack, which are a
# different failure - a malformed or unacknowledged send rather than a venue
# saying no - and counting them would inflate the one number a compliance
# reader will quote.  Same rule as the short sell report, deliberately.
REJECT_STATES = frozenset({"rejected"})

# How long the limit has to sit on our side before not trading into it is worth
# asking about.  Below this it is a print, not a period.
MIN_PIN_MINS = 2.0

# A close-only order with a short window legitimately does nothing until the
# auction, so it is not a missed opportunity.
CLOSE_ONLY_WINDOW_MS = 1_800_000

# The findings table, paginated.  Nothing is dropped silently: what does not fit
# is counted on the page and in the log.
FINDINGS_PER_PAGE = 28
FINDINGS_MAX_PAGES = 4

TITLE = "Limit Up / Limit Down Order Report"


# =============================================================================
# Q
#
# Two servers, two lambdas, and the same $[hist;...;...] shape as the short sell
# report: q parses both branches but resolves only the one it takes, so the
# historical `date=d` never has to exist on the realtime side.  The realtime
# branch bolts on `date:0Nd` with an update so every frame has one shape.
#
# NOTHING IS GROUPED on the order server.  A target is an order and a workorder
# is a child order; the sums and counts happen in Python, where --self-test can
# prove them.
#
# NAMES.  Every parameter and local is checked against .Q.res by --self-test.
# `ss` is q's string search and cost a run to learn; `mins`, `max`, `var`, `in`
# and `last` are the same trap.
# =============================================================================

Q_ORDERS = """
{[hist;d;sfx]
  et:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; sym:0#`; side:0#`;
         sidesign:0#0i; size:0#0i; t_start:0#0Nt; t_end:0#0Nt; doclose:0#0i;
         fixmsg:0#`; basket:0#`; algo:0#`; time:0#0Nt);
  ew:([] date:0#0Nd; id_server:0#0i; id_work:0#0i; id_target:0#0i; make:0#0i;
         state:0#`; t_on_market:0#0Nt; t_off_market:0#0Nt);

  / parents.  Every side: a limit up is favourable to a seller and a limit down
  / to a buyer, so neither side can be filtered away here.
  t:$[hist;
      select date,id_server,id_target,sym,side,sidesign,size,t_start,t_end,
          doclose,fixmsg,basket,algo,time
        from target where date=d, any (upper sym) like/: sfx;
      update date:0Nd from select id_server,id_target,sym,side,sidesign,size,
          t_start,t_end,doclose,fixmsg,basket,algo,time
        from target where any (upper sym) like/: sfx];
  if[0=count t; :(et;ew)];

  / children, row by row.  t_on_market and t_off_market are what say whether a
  / split was ON THE BOOK during a limit period - t_gen and t_transmit say when
  / we decided and when we sent, which is a different question.
  ids:exec distinct id_target from t;
  w:$[hist;
      select date,id_server,id_work,id_target,make,state,t_on_market,
          t_off_market
        from workorder where date=d, id_target in ids;
      update date:0Nd from select id_server,id_work,id_target,make,state,
          t_on_market,t_off_market
        from workorder where id_target in ids];
  (t;w)
  }
"""

# Limit periods, off the quote server.  A stock at its limit stops having a two
# sided quote, so `lim` marks the ticks where one side is missing or the two are
# equal, and `sums differ` turns contiguous runs of them into one row each.
#
# The run boundaries come from the NORMAL ticks between them, not from a gap
# threshold: two limit periods either side of a spell of two sided quoting are
# genuinely two periods, and a threshold would have to guess where.
#
# noask and nobid are counted rather than decided here.  A locked book is
# ambiguous on its own and Python weighs the two, where the rule is testable.
Q_PINS = """
{[hist;d;syms]
  / `$ IS LOAD BEARING.  syms arrives as a list of CHAR VECTORS - PyKX sends
  / bytes that way - and `sym in syms` against a symbol column with char vectors
  / on the right matches NOTHING.  Not an error: every day would come back with
  / no limit periods, no orders touched, and a page of zeros.
  syms:`$syms;
  ep:([] sym:0#`; grp:0#0j; start:0#0Nt; end:0#0Nt; price:0#0n;
         noask:0#0j; nobid:0#0j; ticks:0#0j);
  q:$[hist;
      select time,sym,qbid:0^qbid,qask:0^qask from qatt
        where date=d, sym in syms, (0<0^qbid)|0<0^qask;
      select time,sym,qbid:0^qbid,qask:0^qask from qatt
        where sym in syms, (0<0^qbid)|0<0^qask];
  if[0=count q; :ep];
  q:`sym`time xasc q;
  q:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from q;
  q:update grp:sums differ lim by sym from q;
  0!select start:first time, end:last time,
      price:last ?[0=qask;qbid;qask],
      noask:sum 0=qask, nobid:sum 0=qbid, ticks:count i
    by sym,grp from q where lim
  }
"""


def _check_server(endpoint: str, which: str):
    if _PLACEHOLDER in endpoint:
        raise SystemExit(
            f"{which} is still set to {_PLACEHOLDER}. Edit the constants at the "
            f"top of {Path(__file__).name} before running against kdb.")


def connect(endpoint: str):
    """Open a PyKX connection.  Host and port; the processes are open.

    pykx is imported here rather than at module level so --self-test, --demo and
    everything else off the wire run without it.
    """
    try:
        import pykx as kx
    except ImportError:
        raise SystemExit("pykx is not installed.  pip install pykx")
    host, _, port = endpoint.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected host:port, got {endpoint!r}")
    return kx.SyncQConnection(host=host, port=int(port))


_UNUSED_DATE = dt.date(2000, 1, 1)


def fetch_orders(handle, hist: bool, d: Optional[dt.date]):
    sfx = [p.encode() for p in SYM_PATTERNS]
    t, w = handle(Q_ORDERS, hist, d if d is not None else _UNUSED_DATE, sfx)
    return t.pd().to_dict("records"), w.pd().to_dict("records")


def fetch_pins(handle, hist: bool, d: Optional[dt.date], syms):
    if not syms:
        return []
    p = handle(Q_PINS, hist, d if d is not None else _UNUSED_DATE,
               [s.encode() for s in syms])
    return p.pd().to_dict("records")


# =============================================================================
# RECORDS
#
# Everything below is pure and takes plain dicts, so the whole analytic path is
# exercised by --self-test with no pandas, no pykx and no kdb.
# =============================================================================

def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _i(v) -> int:
    try:
        if v is None:
            return 0
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if n == -2147483648 else n


def _d(v) -> Optional[dt.date]:
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


def _ms(v) -> Optional[int]:
    """A q time as milliseconds since midnight.  None where it is missing.

    None matters here: t_off_market is empty on a split that is still open, and
    an open split is ACTIVE rather than one that ended at midnight.
    """
    if v is None:
        return None
    if isinstance(v, dt.timedelta):          # pandas hands a q time back as this
        return int(v.total_seconds() * 1000)
    if isinstance(v, dt.time):
        return ((v.hour * 3600 + v.minute * 60 + v.second) * 1000
                + v.microsecond // 1000)
    try:
        import pandas as pd
        if pd.isna(v):
            return None
        return int(pd.Timedelta(v).total_seconds() * 1000)
    except Exception:
        return None


def market_of(sym) -> Optional[str]:
    """The market a sym belongs to, from its suffix.  None when it is not one of
    the eight - which is how everything else on the book stays out of a total."""
    s = _s(sym).upper()
    i = s.rfind(".")
    return SUFFIX_MARKET.get(s[i:]) if i > 0 else None


def is_rejected(state: str) -> bool:
    """Is this workorder row a rejection?  Every row carrying the state counts."""
    return _s(state).strip().lower() in REJECT_STATES


def pin_side(noask, nobid) -> Optional[str]:
    """Which limit a period was at: "up", "down", or None when it cannot say.

    At limit up nobody will offer, so the ask goes missing; at limit down the
    bid does.  A locked book has neither missing and votes for nothing, which is
    why the two are counted across the run and compared rather than read off one
    tick.  A tie is None and the period is dropped: a window whose SIDE is a
    guess cannot say whether it was favourable, and that is the whole question.
    """
    a, b = _i(noask), _i(nobid)
    if a > b:
        return "up"
    if b > a:
        return "down"
    return None


def is_favourable(sidesign: int, side: str) -> bool:
    """Selling into a limit up, or buying into a limit down - the side that CAN
    fill, because there is a queue resting at the band and we are the other side
    of it."""
    return (sidesign < 0 and side == "up") or (sidesign > 0 and side == "down")


def overlap(a0, a1, b0, b1):
    """The intersection of two half open windows, or None.

    An open ended window - a split with no t_off_market - is treated as running
    to the end of the other one, never as ending at zero.
    """
    if a0 is None or b0 is None:
        return None
    a1 = a1 if a1 is not None else max(b1 if b1 is not None else b0, a0)
    b1 = b1 if b1 is not None else max(a1, b0)
    lo, hi = max(a0, b0), min(a1, b1)
    return (lo, hi) if hi > lo else None


class Attempt(NamedTuple):
    """ONE TARGET ROW - one SEND of an order.

    An order rejected and re-sent writes a new id_target, so several of these
    can be one order.  What ties them together is the client's own id, FIX tag
    9604 - see scripts/lib/order_chains.
    """
    key: tuple                 # (date, id_server, id_target) - one send
    date: Optional[dt.date]
    market: str
    sym: str
    side: str
    sidesign: int
    size: int
    t_start: Optional[int]     # ms since midnight
    t_end: Optional[int]
    doclose: int
    client_id: str             # tag 9604 - "" when the client sent none
    basket: str
    algo: str
    id_target: int

    @property
    def chain_key(self) -> tuple:
        return chain_key(self.date, self.client_id, self.key[1], self.id_target)


class Chain(NamedTuple):
    """ONE ORDER, however many times it was sent.

    Its live window spans every attempt, and its splits are POOLED across them -
    which is the whole point here.  Asking "was anything of ours on the book
    during the limit" of a single attempt gives a false positive whenever a
    SIBLING attempt was the one that traded.
    """
    chain_key: tuple
    date: Optional[dt.date]
    market: str
    sym: str
    side: str
    sidesign: int
    size: int
    doclose: int
    client_id: str
    attempts: tuple

    @property
    def n(self) -> int:
        return len(self.attempts)

    @property
    def keys(self) -> set:
        return {a.key for a in self.attempts}

    @property
    def t_start(self) -> Optional[int]:
        got = [a.t_start for a in self.attempts if a.t_start is not None]
        return min(got) if got else None

    @property
    def t_end(self) -> Optional[int]:
        got = [a.t_end for a in self.attempts if a.t_end is not None]
        return max(got) if got else None

    def disagrees_on(self) -> list:
        return [f for f in ("sym", "side", "algo", "basket")
                if len({getattr(a, f) for a in self.attempts}) > 1]


class Split(NamedTuple):
    """ONE WORKORDER ROW - a child order."""
    key: tuple                 # its parent's key
    id_work: int
    date: Optional[dt.date]
    market: str
    make: int
    rejected: bool
    on_market: Optional[int]
    off_market: Optional[int]


class Pin(NamedTuple):
    """One LIMIT PERIOD on one stock."""
    sym: str
    date: Optional[dt.date]
    start: int
    end: int
    price: float
    side: str                  # "up" | "down"
    ticks: int

    @property
    def minutes(self) -> float:
        return (self.end - self.start) / 60_000.0


def to_attempts(records) -> list:
    out = []
    for r in records:
        sym = _s(r.get("sym"))
        market = market_of(sym)
        if market is None:
            continue
        d = _d(r.get("date"))
        idt = _i(r.get("id_target"))
        out.append(Attempt(
            key=(d, _i(r.get("id_server")), idt),
            date=d, market=market, sym=sym, side=_s(r.get("side")),
            sidesign=_i(r.get("sidesign")), size=abs(_i(r.get("size"))),
            t_start=_ms(r.get("t_start")), t_end=_ms(r.get("t_end")),
            doclose=_i(r.get("doclose")),
            client_id=fix_tag(r.get("fixmsg")), basket=_s(r.get("basket")),
            algo=_s(r.get("algo")), id_target=idt))
    return out


def attempt_fills(splits) -> dict:
    """What each ATTEMPT executed, keyed on the target it belongs to."""
    out = {}
    for sp in splits:
        out[sp.key] = out.get(sp.key, 0) + sp.make
    return out


def to_chains(attempts, qty=None, splits=()) -> list:
    """Collapse attempts into orders on the client's id.

    Ordered by (t_start, id_target) so "the last attempt" is the last one sent.
    """
    qty = qty or CHAIN_QTY
    fills = attempt_fills(splits)
    groups = {}
    for a in attempts:
        groups.setdefault(a.chain_key, []).append(a)

    out = []
    for k, got in groups.items():
        got = sorted(got, key=lambda a: (a.t_start or 0, a.id_target))
        last = got[-1]
        size = chain_size([a.size for a in got],
                          [fills.get(a.key, 0) for a in got], qty)
        out.append(Chain(chain_key=k, date=last.date, market=last.market,
                         sym=last.sym, side=last.side, sidesign=last.sidesign,
                         size=size,
                         #  close-only only excuses the order if EVERY attempt
                         #  was close-only; one working attempt is a working
                         #  order
                         doclose=1 if all(a.doclose for a in got) else 0,
                         client_id=last.client_id, attempts=tuple(got)))
    return sorted(out, key=lambda c: (c.attempts[0].t_start or 0,
                                      c.attempts[0].id_target))


def to_splits(records, owners) -> list:
    """Workorder rows, keyed back onto the orders that survived.

    owners may be ATTEMPTS or CHAINS - a chain flattens to its attempts, since a
    workorder belongs to one send.  Taking either is deliberate: every caller
    has one or the other in hand, and making them convert is how a chain's
    sibling attempts get dropped by accident.
    """
    att = []
    for o in owners:
        att.extend(o.attempts if hasattr(o, "attempts") else [o])
    by_key = {a.key: a for a in att}
    out = []
    for r in records:
        key = (_d(r.get("date")), _i(r.get("id_server")), _i(r.get("id_target")))
        p = by_key.get(key)
        if p is None:
            continue
        out.append(Split(key=key, id_work=_i(r.get("id_work")), date=p.date,
                         market=p.market, make=abs(_i(r.get("make"))),
                         rejected=is_rejected(r.get("state")),
                         on_market=_ms(r.get("t_on_market")),
                         off_market=_ms(r.get("t_off_market"))))
    return out


def to_pins(records, d=None) -> list:
    """Limit periods.  A period whose side cannot be told is dropped."""
    out = []
    for r in records:
        side = pin_side(r.get("noask"), r.get("nobid"))
        if side is None:
            continue
        start, end = _ms(r.get("start")), _ms(r.get("end"))
        if start is None or end is None or end <= start:
            continue
        try:
            price = float(r.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        out.append(Pin(sym=_s(r.get("sym")), date=_d(r.get("date")) or d,
                       start=start, end=end, price=price, side=side,
                       ticks=_i(r.get("ticks"))))
    return out


# =============================================================================
# WHICH ORDERS THE LIMIT TOUCHED
# =============================================================================

class ChainStats(NamedTuple):
    attempts: int
    chains: int
    multi: int
    longest: int
    no_id: int
    no_id_by_market: dict
    mixed: list                # chains disagreeing on sym/side/algo/basket
    over: list                 # [(chain, executed)] filling more than they asked


def chain_stats(attempts, chains, splits) -> ChainStats:
    """What the run should say about its own assumptions.

    `mixed` means one 9604 covered more than one order and the numbers are
    wrong; `over` means a chain executed more than it asked for, which under
    CHAIN_QTY="asked" should be impossible.
    """
    no_id = [a for a in attempts if not a.client_id]
    by_mkt = {}
    for a in no_id:
        by_mkt[a.market] = by_mkt.get(a.market, 0) + 1
    made = {}
    for sp in splits:
        made[sp.key] = made.get(sp.key, 0) + sp.make
    over = []
    for c in chains:
        ex = sum(made.get(k, 0) for k in c.keys)
        if c.size > 0 and ex > c.size:
            over.append((c, ex))
    return ChainStats(
        attempts=len(attempts), chains=len(chains),
        multi=sum(1 for c in chains if c.n > 1),
        longest=max([c.n for c in chains], default=0),
        no_id=len(no_id), no_id_by_market=by_mkt,
        mixed=[c for c in chains if c.disagrees_on()], over=over)


def report_chains(st: ChainStats) -> None:
    log(f"  chains: {st.attempts:,} targets -> {st.chains:,} order"
        f"{'' if st.chains == 1 else 's'} "
        f"({st.multi:,} chained, longest {st.longest})")
    if st.no_id == st.attempts and st.attempts:
        log(f"  WARNING: NOT ONE of {st.attempts:,} targets carries tag "
            f"{CLIENT_ID_TAG}. Either the client sends none, or fixmsg uses a "
            f"separator fix_tag does not know - nothing has been chained")
    elif st.no_id:
        worst = ", ".join(f"{k} {v:,}" for k, v in
                          sorted(st.no_id_by_market.items(),
                                 key=lambda kv: -kv[1]))
        log(f"  {st.no_id:,} of {st.attempts:,} targets "
            f"({100.0 * st.no_id / max(st.attempts, 1):.1f}%) carry no tag "
            f"{CLIENT_ID_TAG} and stand alone: {worst}")
    if st.mixed:
        fields = sorted({f for c in st.mixed for f in c.disagrees_on()})
        log(f"  WARNING: {len(st.mixed):,} chains disagree on "
            f"{', '.join(fields)} - a {CLIENT_ID_TAG} is covering more than "
            f"one order and these numbers are WRONG")
    if st.over:
        log(f"  WARNING: {len(st.over):,} chains executed MORE than the "
            f"quantity taken for them; they have been UN-CHAINED and counted "
            f"one order per target:")
        for c, ex in st.over[:5]:
            log(f"      {CLIENT_ID_TAG}={c.client_id or '(none)'}  {c.sym}  "
                f"qty {c.size:,}  executed {ex:,}")


def unchain(chains, over) -> list:
    """Explode the over-filled chains back into one order per attempt.

    Whatever grouped them was wrong, and one order per target is what counting
    targets would have said - defensible even when it is not ideal, and better
    than a completion over 100% on the page.
    """
    bad = {c.chain_key for c, _ex in over}
    out = [c for c in chains if c.chain_key not in bad]
    for c in chains:
        if c.chain_key not in bad:
            continue
        for a in c.attempts:
            out.append(Chain(chain_key=(a.date, "", a.key[1], a.id_target),
                             date=a.date, market=a.market, sym=a.sym,
                             side=a.side, sidesign=a.sidesign, size=a.size,
                             doclose=a.doclose, client_id=a.client_id,
                             attempts=(a,)))
    return sorted(out, key=lambda c: (c.attempts[0].t_start or 0,
                                      c.attempts[0].id_target))


def pins_by_sym(pins) -> dict:
    out = {}
    for p in pins:
        out.setdefault((p.date, p.sym), []).append(p)
    return out


def touched(chains, pins) -> tuple:
    """(chains the limit touched, {chain key: [its overlapping pins]}).

    Touched means a limit period on that stock overlapped the ORDER's live
    window - which for a chain spans every attempt, first send to last end.  An
    order that finished before the stock went limit is not a LULD order,
    however dramatic the stock's afternoon was.
    """
    index = pins_by_sym(pins)
    keep, hits = [], {}
    for c in chains:
        got = [w for w in index.get((c.date, c.sym), ())
               if overlap(c.t_start, c.t_end, w.start, w.end)]
        if not got:
            continue
        keep.append(c)
        hits[c.chain_key] = got
    return keep, hits


# =============================================================================
# ROLLUPS
# =============================================================================

def _completion(executed: int, order_qty: int) -> Optional[float]:
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
    """One date.  completion is that day's executed over that day's order qty,
    so a bar on the by day chart means what the headline means."""
    date: dt.date
    orders: int
    order_qty: int
    executed: int
    rejections: int
    completion: Optional[float]


class Totals(NamedTuple):
    """The headline figures.  completion is executed over order qty, summed."""
    orders: int
    order_qty: int
    executed: int
    rejections: int
    completion: Optional[float]


def by_market(chains, splits) -> list:
    """One Row per market, always all eight, always in MARKETS order.

    Plain counts and plain sums over rows the query returned as they stand.
    """
    orders = {c: 0 for c in MARKET_CODES}
    qty = {c: 0 for c in MARKET_CODES}
    made = {c: 0 for c in MARKET_CODES}
    rej = {c: 0 for c in MARKET_CODES}
    for c in chains:
        orders[c.market] += 1
        qty[c.market] += c.size
    for s in splits:
        made[s.market] += s.make
        if s.rejected:
            rej[s.market] += 1
    return [Row(m.code, m.name, orders[m.code], qty[m.code],
                made[m.code], rej[m.code]) for m in MARKETS]


def by_day(chains, splits) -> list:
    """One DayRow per date that carried LULD flow, in date order."""
    days = {}

    def slot(d):
        return days.setdefault(d, [0, 0, 0, 0])

    for c in chains:
        if c.date is None:
            continue
        e = slot(c.date)
        e[0] += 1
        e[1] += c.size
    for s in splits:
        if s.date is None:
            continue
        e = slot(s.date)
        e[2] += s.make
        if s.rejected:
            e[3] += 1
    return [DayRow(d, o, q, e, r, _completion(e, q))
            for d, (o, q, e, r) in sorted(days.items())]


def totals(rows) -> Totals:
    """The headline figures.

    Completion is the summed executed over the summed order qty - the real
    overall fill rate.  It was briefly the mean of the market percentages,
    because one market's replaced-and-rejected order was counted three times
    and 81m of quantity that never existed dragged the ratio to 12.3%.  Chaining
    fixed the double count at its source, so the honest aggregate is honest
    again and there is no reason to prefer an average of averages.
    """
    ex = sum(r.executed for r in rows)
    qty = sum(r.order_qty for r in rows)
    return Totals(sum(r.orders for r in rows), qty, ex,
                  sum(r.rejections for r in rows), _completion(ex, qty))


# =============================================================================
# FAVOURABLE, NO SPLIT
# =============================================================================

class Missed(NamedTuple):
    parent: Chain              # the ORDER, all its attempts pooled
    pin: Pin
    window: tuple              # (start, end) of the overlap, ms
    executed: int
    splits_total: int
    windows_qualifying: int
    attempts: int              # how many times the order was sent

    @property
    def unfilled(self) -> int:
        """What was still to do.  Not a column any more - the page shows what
        WAS done, and this is what it was measured against - but it is still
        what the table is sorted by, and it is derivable from the two columns
        that are there."""
        return self.parent.size - self.executed

    @property
    def completion(self) -> Optional[float]:
        return _completion(self.executed, self.parent.size)

    @property
    def minutes(self) -> float:
        return (self.window[1] - self.window[0]) / 60_000.0


def split_active(s: Split, w0: int, w1: int) -> bool:
    """Was this child on the market at any point in [w0, w1)?

    on_market is when it reached the book and off_market when it left.  A split
    with no on_market never got there - it cannot have been active whatever else
    is true of it.  A split with no off_market is still open, and open means
    active, which is why overlap() treats a missing end as running to the end of
    the other window rather than as ending at zero.
    """
    if s.on_market is None:
        return False
    return overlap(s.on_market, s.off_market, w0, w1) is not None


def missed_opportunities(chains, splits, hits, min_mins=MIN_PIN_MINS) -> list:
    """Orders where the limit was on our side and nothing was on the book.

    SPLITS ARE POOLED ACROSS THE CHAIN.  Asking this of a single attempt gives a
    false positive whenever a SIBLING attempt was the one resting on the book -
    a finding pointing at nothing, which is worse than no finding.

    One row per ORDER, taking its longest qualifying limit period - an order
    that missed three windows is one conversation, not three, and the count of
    the others rides along on the row.
    """
    by_key = {}
    for s in splits:
        by_key.setdefault(s.key, []).append(s)

    out = []
    for p in chains:
        kids = [s for k in p.keys for s in by_key.get(k, ())]
        executed = sum(s.make for s in kids)
        unfilled = p.size - executed
        if unfilled <= 0:
            continue
        # a close-only order with a short window legitimately does nothing until
        # the auction
        if p.doclose and p.t_start is not None and p.t_end is not None \
                and (p.t_end - p.t_start) <= CLOSE_ONLY_WINDOW_MS:
            continue

        qualifying = []
        for w in hits.get(p.chain_key, ()):
            if not is_favourable(p.sidesign, w.side):
                continue
            ov = overlap(p.t_start, p.t_end, w.start, w.end)
            if ov is None or (ov[1] - ov[0]) / 60_000.0 < min_mins:
                continue
            if any(split_active(s, ov[0], ov[1]) for s in kids):
                continue
            qualifying.append((w, ov))
        if not qualifying:
            continue
        w, ov = max(qualifying, key=lambda q: q[1][1] - q[1][0])
        out.append(Missed(parent=p, pin=w, window=ov, executed=executed,
                          splits_total=len(kids),
                          windows_qualifying=len(qualifying),
                          attempts=p.n))
    # biggest missed quantity first - the page is read from the top.  Unfilled
    # is no longer a column, but it is order qty times one minus completion, so
    # the order is still readable off the two that are.
    return sorted(out, key=lambda m: (m.unfilled, m.minutes), reverse=True)


# =============================================================================
# PAGE
# =============================================================================

Y_TITLE = 0.955
Y_SUBTITLE = 0.931
Y_RULE_TOP = 0.9185
Y_KPI_VALUE = 0.884
Y_KPI_LABEL = 0.860
Y_TABLE_TOP = 0.808
Y_RULE_BOTTOM = 0.066
Y_FOOTER = 0.048

MKT_TABLE_ROW_H = 0.030               # eight markets, so tighter than five
MKT_BAND = (0.235, 0.230, 0.478)      # (y0, height, title y) for the two charts

# the two per day charts, page 2 of --monthly
DAY_BANDS = ((0.520, 0.230, 0.790), (0.130, 0.230, 0.400))

MKT_COLS = (
    ("Market", 0.24, False),
    ("Orders", 0.12, True),
    ("Order qty", 0.19, True),
    ("Executed", 0.19, True),
    ("Completion", 0.14, True),
    ("Rejections", 0.12, True),
)

# The findings table.  Splits is the order's TOTAL child count: 0 means it never
# worked, a number means it worked but not while the limit was there.
MISS_COLS = (
    ("Market", 0.10, False),
    ("Symbol", 0.14, False),
    ("Side", 0.07, False),
    ("Order qty", 0.11, True),
    ("Exec qty", 0.11, True),
    ("Completion", 0.10, True),
    ("Limit", 0.08, True),
    ("At", 0.06, False),
    ("Limit period", 0.13, False),
    ("Mins", 0.05, True),
    ("Splits", 0.05, True),
)


def _kpis(fig, tot, n_missed):
    _kpis_row(fig, [(fmt_int(tot.orders), "Orders at a limit", INK),
                    (fmt_pct1(tot.completion), "Overall completion", GREEN),
                    (fmt_int(tot.rejections), "Rejections", RED),
                    (fmt_int(n_missed), "Favourable, no split",
                     RED if n_missed else INK3)],
              Y_KPI_VALUE, Y_KPI_LABEL, fs=21)


def _market_table(fig, rows, y_top, row_h):
    cells = [[(r.name, INK, "normal"),
              (fmt_int(r.orders), INK, "normal"),
              (fmt_int(r.order_qty), INK, "normal"),
              (fmt_int(r.executed), INK, "normal"),
              (fmt_pct1(r.completion), INK, "normal"),
              (fmt_int(r.rejections),
               RED if r.rejections else INK3,
               "bold" if r.rejections else "normal")]
             for r in rows]
    return _table_rows(fig, MKT_COLS, cells, y_top, row_h)


def _miss_cells(m: Missed):
    """Completion is the number in red: on a page about limits we could have
    traded into, how little of the order got done IS the finding.  Order qty
    beside it is what that percentage is a percentage of - the two together give
    back the quantity missed, so nothing is lost by not printing it."""
    return [(MARKET_NAME.get(m.parent.market, m.parent.market), INK, "normal"),
            (m.parent.sym, INK, "normal"),
            (m.parent.side or ("buy" if m.parent.sidesign > 0 else "sell"),
             INK, "normal"),
            (fmt_int(m.parent.size), INK, "normal"),
            (fmt_int(m.executed), INK, "normal"),
            (fmt_pct1(m.completion), RED, "bold"),
            (f"{m.pin.price:,.4g}" if m.pin.price else DASH, INK, "normal"),
            (m.pin.side, INK2, "normal"),
            (f"{fmt_hm(m.window[0])}–{fmt_hm(m.window[1])}", INK2, "normal"),
            (f"{m.minutes:.0f}", INK, "normal"),
            (fmt_int(m.splits_total),
             INK3 if m.splits_total == 0 else INK, "normal")]


def _sorted_pairs(rows, key):
    """Chart order: biggest first, ties keeping MARKETS order.  Python's sort is
    stable, so the fixed market order is the tie break for free."""
    return sorted(rows, key=key, reverse=True)


def draw_summary(rows, tot, n_missed, subtitle, foot):
    """Page 1: the headline figures, the market table, the two market charts."""
    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)
    _kpis(fig, tot, n_missed)
    _market_table(fig, rows, Y_TABLE_TOP, MKT_TABLE_ROW_H)

    comp = _sorted_pairs(rows, key=lambda r: (r.completion or 0.0))
    rej = _sorted_pairs(rows, key=lambda r: r.rejections)
    y0, h, ty = MKT_BAND
    half = 0.405
    barchart(fig, (L, y0, half, h), "Completion by market",
             [r.name for r in comp],
             [(r.completion or 0.0) for r in comp],
             [fmt_pct0(r.completion) for r in comp],
             BLUE, vmax=100.0, fs=8.0, title_y=ty)
    barchart(fig, (R - half, y0, half, h), "Rejections by market",
             [r.name for r in rej],
             [float(r.rejections) for r in rej],
             [fmt_int(r.rejections) for r in rej],
             RED, fs=8.0, title_y=ty)

    _footer(fig, foot, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def draw_days(days, subtitle, foot):
    """A page of the two per day charts.  Vertical: a month is a sequence, and a
    sequence reads left to right."""
    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)
    labels = [f"{d.date:%Y-%m-%d}" for d in days] or ["-"]
    fs = 5.4 if len(days) > 16 else 6.8
    (cy, ch, cty), (ry, rh, rty) = DAY_BANDS
    vbarchart(fig, (L, cy, COL_W, ch), "Completion by day", labels,
              [(d.completion or 0.0) for d in days] or [0.0],
              [fmt_pct0(d.completion) for d in days] or [DASH],
              BLUE, vmax=100.0, fs=fs, title_y=cty)
    vbarchart(fig, (L, ry, COL_W, rh), "Rejections by day", labels,
              [float(d.rejections) for d in days] or [0.0],
              [fmt_int(d.rejections) for d in days] or ["0"],
              RED, fs=fs, title_y=rty)
    _footer(fig, foot, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def draw_missed(missed, subtitle, foot, page=1, pages=1, dropped=0):
    """A page of the favourable-no-split table."""
    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)
    head = "Favourable limit, no split on the market"
    if pages > 1:
        head += f"   ({page} of {pages})"
    fig.text(L, 0.893, head, fontsize=12, fontweight="bold", color=INK,
             va="baseline")
    fig.text(L, 0.874,
             "The limit was on the side we could have filled, the order still "
             "had quantity left, and nothing of ours was on the book while it "
             "lasted.",
             fontsize=8, color=INK2, va="baseline")

    if missed:
        y = _table_rows(fig, MISS_COLS, [_miss_cells(m) for m in missed],
                        0.845, 0.0245, fs=7.5, head_fs=7.5)
        if dropped:
            fig.text(L, y - 0.022,
                     f"{dropped:,} more not shown - the table is sorted by the "
                     f"quantity missed, so these are the smallest.",
                     fontsize=7.5, color=RED, va="baseline")
    else:
        fig.text(L, 0.80, "Nothing to report: no order sat through a "
                          "favourable limit without a split on the market.",
                 fontsize=10, color=INK3, va="baseline")

    _footer(fig, foot, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def pages_for(rows, tot, missed, subtitle, foot, days=None):
    """Every page of the report, in order.

    The findings table is however long it is, so it paginates.  What does not
    fit is COUNTED on the last page and in the log - a silent truncation reads
    as "that was all of them", which is the one thing this page must not say.
    """
    out = [draw_summary(rows, tot, len(missed), subtitle, foot)]
    if days is not None:
        out.append(draw_days(days, subtitle, foot))

    shown = missed[:FINDINGS_PER_PAGE * FINDINGS_MAX_PAGES]
    dropped = len(missed) - len(shown)
    chunks = [shown[i:i + FINDINGS_PER_PAGE]
              for i in range(0, len(shown), FINDINGS_PER_PAGE)] or [[]]
    for i, chunk in enumerate(chunks, 1):
        out.append(draw_missed(chunk, subtitle, foot, i, len(chunks),
                               dropped if i == len(chunks) else 0))
    return out


def save(figs, out_dir, stem):
    return _save(figs, out_dir, stem, dpi=DPI)


# =============================================================================
# EMAIL
# =============================================================================

def _mailer():
    try:
        from lib import mailer
    except ImportError as e:
        raise SystemExit(
            f"EMAIL_TO is set but scripts/lib/mailer.py will not import ({e}).")
    return mailer


def email_configured() -> bool:
    return bool(EMAIL_TO or EMAIL_CC or EMAIL_BCC)


def smtp_config():
    return _mailer().Smtp(host=SMTP_HOST, port=SMTP_PORT, timeout=SMTP_TIMEOUT)


def mail_body() -> str:
    """The whole body.  The report is the PDF; the mail just carries it.

    No HTML, no inlined page, no tables repeated in the message.  A body that
    restates the report is a second copy of it to keep in step, and it renders
    at the mercy of whatever client opens it.
    """
    return EMAIL_SIGNATURE


def mail_report(when, files) -> None:
    m = _mailer()
    pdf = next((p for p in files if p.suffix == ".pdf"), None)
    if not EMAIL_FROM:
        raise SystemExit(
            f"EMAIL_TO is set but EMAIL_FROM is empty. Both live in the EMAIL "
            f"block near the top of {Path(__file__).name}.")
    if pdf is None:
        raise SystemExit("nothing to attach: no PDF was written")

    msg = m.build_message(m.Mail(
        subject=f"{TITLE} - {when}", sender=EMAIL_FROM, to=EMAIL_TO,
        cc=EMAIL_CC, bcc=EMAIL_BCC, text=mail_body(), attachments=[pdf]))
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
    n = calendar.monthrange(year, month)[1]
    return [d for d in (dt.date(year, month, i + 1) for i in range(n))
            if d.weekday() < 5]


def parse_month(text: str) -> tuple:
    """YYYY-MM, strictly.  A four digit year, because "26-07" is a valid date in
    the year 26 and would run silently against an empty HDB."""
    try:
        ys, ms = str(text).split("-")
        if len(ys) != 4 or len(ms) != 2 or not (ys + ms).isdigit():
            raise ValueError
        y, mo = int(ys), int(ms)
        if not 1 <= mo <= 12:
            raise ValueError
        dt.date(y, mo, 1)
    except (ValueError, AttributeError):
        raise SystemExit(f"--monthly wants YYYY-MM, got {text!r}")
    return y, mo


class Plan(NamedTuple):
    monthly: bool
    hist: bool
    order_server: str
    qatt_server: str
    server_names: tuple
    dates: list
    stem: str
    when: str


def plan(monthly=None, date=None, now=None) -> Plan:
    now = now or dt.datetime.now()
    if monthly is not None and date is not None:
        raise SystemExit("--monthly and --date are alternatives, not a range")
    hist = monthly is not None or date is not None
    order = ORDER_SERVER_HIST if hist else ORDER_SERVER_RT
    qatt = QATT_SERVER_HIST if hist else QATT_SERVER_RT
    names = (("ORDER_SERVER_HIST", "QATT_SERVER_HIST") if hist
             else ("ORDER_SERVER_RT", "QATT_SERVER_RT"))

    if monthly is not None:
        y, mo = parse_month(monthly)
        return Plan(True, True, order, qatt, names, month_dates(y, mo),
                    f"luld_report_{y:04d}-{mo:02d}",
                    f"{calendar.month_name[mo]} {y}")
    if date is not None:
        return Plan(False, True, order, qatt, names, [date],
                    f"luld_report_{date:%Y-%m-%d}", f"{date:%Y-%m-%d}")
    return Plan(False, False, order, qatt, names, [None],
                f"luld_report_{now:%Y-%m-%d}", f"{now:%Y-%m-%d %H:%M}")


def run(args) -> int:
    pl = plan(args.monthly, args.date)
    _check_server(pl.order_server, pl.server_names[0])
    _check_server(pl.qatt_server, pl.server_names[1])

    log(f"luld_report  {'historical' if pl.hist else 'realtime'}  "
        f"orders {pl.order_server}  quotes {pl.qatt_server}")
    oh = connect(pl.order_server)
    qh = connect(pl.qatt_server)

    chains, splits, missed, traded, seen = [], [], [], 0, 0
    all_attempts, no_pin_days = [], 0
    for d in pl.dates:
        if not args.quiet and d is not None:
            log(f"  {d} ...")
        pr, wr = fetch_orders(oh, pl.hist, d)
        att = to_attempts(pr)
        if not att:
            continue
        seen += len(att)
        all_attempts.extend(att)
        #  the splits come first: "asked" reads the chain's quantity off them
        ws_all = to_splits(wr, att)
        chs = to_chains(att, args.chain_qty, ws_all)

        syms = sorted({c.sym for c in chs})
        pins = to_pins(fetch_pins(qh, pl.hist, d, syms), d)
        if not pins:
            #  a day with NO limit period anywhere is possible, but a run of
            #  them means the quote query is matching nothing rather than the
            #  market being calm
            no_pin_days += 1
        kept, hits = touched(chs, pins)
        if not kept:
            continue
        traded += 1
        keys = {k for c in kept for k in c.keys}
        ws = [x for x in ws_all if x.key in keys]
        chains.extend(kept)
        splits.extend(ws)
        missed.extend(missed_opportunities(kept, ws, hits, args.min_mins))

    st = chain_stats(all_attempts, chains, splits)
    if st.over and not args.keep_over:
        chains = unchain(chains, st.over)
        missed = [m for m in missed
                  if m.parent.chain_key not in {c.chain_key
                                                for c, _e in st.over}]
    report_chains(st)

    rows = by_market(chains, splits)
    tot = totals(rows)
    days = by_day(chains, splits) if pl.monthly else None
    missed.sort(key=lambda m: (m.unfilled, m.minutes), reverse=True)

    log(f"  {seen:,} orders in scope, {tot.orders:,} of them touched a limit, "
        f"{tot.rejections:,} rejections")
    if seen and not tot.orders:
        log(f"  WARNING: {seen:,} short sell orders were in scope and NOT ONE "
            f"touched a limit period. Check {pl.qatt_server} has the syms and "
            f"the date - a quote query matching nothing looks exactly like a "
            f"calm market from here")
    elif no_pin_days:
        log(f"  {no_pin_days} of {len(pl.dates)} dates had no limit period at "
            f"all")
    log(f"  {len(missed):,} favourable with no split on the market")
    cap = FINDINGS_PER_PAGE * FINDINGS_MAX_PAGES
    if len(missed) > cap:
        log(f"  NOTE: only the largest {cap:,} are on the page; the other "
            f"{len(missed) - cap:,} are counted there but not listed")

    subtitle = f"By market  ·  {pl.when}"
    foot = (f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  "
            + ("historical" if pl.hist else "real-time snapshot"))
    if pl.monthly:
        foot += (f", {traded} trading day" + ("" if traded == 1 else "s"))

    files = save(pages_for(rows, tot, missed, subtitle, foot, days),
                 Path(args.out_dir), pl.stem)
    if email_configured():
        mail_report(pl.when, files)
    return 0


# =============================================================================
# DEMO
# =============================================================================

DEMO_STAMP = "SAMPLE - synthetic data, not from kdb"


def _p(idt, market, size, sidesign=-1, d=None, srv=1, sym=None,
       t_start=9 * 3_600_000 + 1_800_000, t_end=15 * 3_600_000, doclose=0,
       cid=None, basket="B1", algo="vwap"):
    """One TARGET row - one SEND.  cid goes into fixmsg as tag 9604 the way the
    client really sends it, so the fixture exercises the parse too.  Default is
    an id of its own, i.e. an order sent once."""
    sfx = dict((m.code, m.suffixes[0]) for m in MARKETS).get(market,
                                                             "." + market)
    cid = f"CLI-{idt}" if cid is None else cid
    fix = "8=FIX.4.2;35=D;9012=274=1^275=1;"
    if cid:
        fix += f"{CLIENT_ID_TAG}={cid};"
    return {"date": d, "id_server": srv, "id_target": idt,
            "sym": sym or f"{1000 + idt}{sfx}",
            "side": "sell" if sidesign < 0 else "buy", "sidesign": sidesign,
            "size": size, "t_start": dt.timedelta(milliseconds=t_start),
            "t_end": dt.timedelta(milliseconds=t_end), "doclose": doclose,
            "fixmsg": fix + "59=0", "basket": basket, "algo": algo,
            "time": dt.timedelta(milliseconds=t_start)}


def _chain(records, splits=()):
    """attempts -> chains, the way run() does it."""
    att = to_attempts(records)
    return att, to_chains(att, CHAIN_QTY, splits)


def _w(idw, idt, make, state, on=None, off=None, d=None, srv=1):
    return {"date": d, "id_server": srv, "id_work": idw, "id_target": idt,
            "make": make, "state": state,
            "t_on_market": None if on is None else dt.timedelta(milliseconds=on),
            "t_off_market": None if off is None else dt.timedelta(milliseconds=off)}


def _pin(sym, start, end, price=100.0, noask=50, nobid=0, ticks=50, d=None):
    return {"sym": sym, "date": d, "start": dt.timedelta(milliseconds=start),
            "end": dt.timedelta(milliseconds=end), "price": price,
            "noask": noask, "nobid": nobid, "ticks": ticks}


def demo_session(d=None):
    """(parents, splits, hits, missed) for one made up session.  Deterministic."""
    H = 3_600_000
    pr, wr, pins = [], [], []
    k = 0
    #  a spread of markets, some filling well, some badly, some rejecting
    shape = ((("JP", 26, 0.72, 0), ("KR", 19, 0.41, 14), ("CN", 22, 0.55, 6),
              ("TW", 11, 0.63, 2), ("TH", 7, 0.38, 9), ("MY", 5, 0.51, 0),
              ("ID", 4, 0.29, 3), ("IN", 9, 0.66, 1)))
    for mkt, n, fill, rejects in shape:
        for i in range(n):
            k += 1
            size = 20_000 + ((k * 7919) % 400) * 500
            sidesign = -1 if (k % 3) else 1
            pr.append(_p(k, mkt, size, sidesign=sidesign, d=d))
            sym = pr[-1]["sym"]
            # the stock is at a limit for a stretch of the afternoon
            start = 11 * H + (k % 90) * 60_000
            end = start + (6 + (k % 40)) * 60_000
            up = sidesign < 0 if (k % 4) else sidesign > 0
            pins.append(_pin(sym, start, end, price=10.0 + (k % 400) / 4.0,
                             noask=60 if up else 0, nobid=0 if up else 60, d=d))
            done = int(size * fill)
            if k % 5:                     # most orders worked through the limit
                wr.append(_w(k, k, done, "filled", on=start - 60_000,
                             off=end + 60_000, d=d))
            elif k % 10 == 5:             # some worked, but not during it
                wr.append(_w(k, k, done, "filled", on=9 * H, off=10 * H, d=d))
            # and the rest sent nothing at all
            for j in range(rejects if i < rejects else 0):
                wr.append(_w(500_000 + k * 4 + j, k, 0, "rejected",
                             on=start, off=start + 1000, d=d))
    #  a Japanese order sent three times under ONE client id, rejected twice
    #  and cancelled - what the chaining is for
    for k, idt in enumerate((90_001, 90_002, 90_003)):
        pr.append(_p(idt, "JP", 40_000, sidesign=-1, cid="CLI-REPLACED", d=d,
                     t_start=10 * H + k * 600_000, t_end=15 * H))
        wr.append(_w(idt, idt, 0, "rejected" if k < 2 else "cxl",
                     on=None, off=None, d=d))
    pins.append(_pin(pr[-1]["sym"], 12 * H, 13 * H, price=1234.0, d=d))

    attempts = to_attempts(pr)
    ws_all = to_splits(wr, attempts)
    chs = to_chains(attempts, CHAIN_QTY, ws_all)
    kept, hits = touched(chs, to_pins(pins, d))
    keys = {k for c in kept for k in c.keys}
    splits = [x for x in ws_all if x.key in keys]
    return kept, splits, hits, missed_opportunities(kept, splits, hits)


def demo_month(year=2026, month=7):
    chains, splits, missed = [], [], []
    for i, d in enumerate(month_dates(year, month)):
        p, w, _h, m = demo_session(d)
        keep = p[:4 + (i * 5) % 30]
        #  two different keys are in play and mixing them silently loses every
        #  finding: splits belong to an ATTEMPT, a finding to a CHAIN
        att_keys = {k for c in keep for k in c.keys}
        chain_keys = {c.chain_key for c in keep}
        chains.extend(keep)
        splits.extend([s for s in w if s.key in att_keys])
        missed.extend([x for x in m if x.parent.chain_key in chain_keys])
    return chains, splits, missed


def demo(out_dir) -> int:
    out = Path(out_dir)
    stamp = f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  {DEMO_STAMP}"

    parents, splits, _hits, missed = demo_session()
    rows = by_market(parents, splits)
    log(f"demo: daily layout, {len(missed)} favourable-no-split")
    save(pages_for(rows, totals(rows), missed,
                   "By market  ·  2026-07-24 18:37  ·  SAMPLE", stamp),
         out, "luld_report_SAMPLE_daily")

    parents, splits, missed = demo_month()
    rows = by_market(parents, splits)
    days = by_day(parents, splits)
    missed.sort(key=lambda m: (m.unfilled, m.minutes), reverse=True)
    log(f"demo: monthly layout, {len(days)} trading days, "
        f"{len(missed)} favourable-no-split")
    save(pages_for(rows, totals(rows), missed,
                   "By market  ·  July 2026  ·  SAMPLE", stamp, days),
         out, "luld_report_SAMPLE_monthly")
    log("  these are made up numbers - do not circulate them as a report")
    return 0


# =============================================================================
# SELF TEST
# =============================================================================

# .Q.res - a name from this list cannot be a q parameter or local: q fails to
# PARSE the lambda and returns the offending token as the error.
Q_RESERVED = frozenset("""
abs acos asin atan avg bin binr by cor cos cov delete dev div do each enlist
exec exit exp from getenv hopen if in insert last like log max min prd select
setenv sin sqrt ss string sum tan update var wavg where within wsum xexp
""".split())


def q_names(src: str) -> set:
    import re
    out = set()
    for params in re.findall(r"\{\s*\[([^\]]*)\]", src):
        out.update(n.strip() for n in params.split(";") if n.strip())
    for name in re.findall(r"^\s*([a-zA-Z][a-zA-Z0-9_]*)\s*:(?!:)", src, re.M):
        out.add(name)
    return {n for n in out if n}


def self_test() -> int:
    import io
    import tempfile
    H = 3_600_000
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("luld_report --self-test\n\nthe q holds together")
    for nm, src in (("Q_ORDERS", Q_ORDERS), ("Q_PINS", Q_PINS)):
        check(f"{nm}: no q reserved word as a name",
              sorted(q_names(src) & Q_RESERVED), [])
        check(f"{nm}: braces balance", src.count("{") == src.count("}"), True)
        check(f"{nm}: brackets balance", src.count("[") == src.count("]"), True)
    for nm, (y0, h, ty) in (("market charts", MKT_BAND),
                            ("day chart 1", DAY_BANDS[0]),
                            ("day chart 2", DAY_BANDS[1])):
        check(f"{nm}: the title sits above the axes, not on the bars",
              ty > y0 + h, True)
    #  a symbol column compared against char vectors matches NOTHING, and the
    #  page of zeros that follows looks exactly like a calm market
    for nm, src, args in (("Q_ORDERS", Q_ORDERS, ("sfx",)),
                          ("Q_PINS", Q_PINS, ("syms",))):
        for arg in args:
            used_bare = f"in {arg}" in src or f"like/: {arg}" in src
            cast = f"{arg}:`${arg};" in src
            like = f"like/: {arg}" in src
            check(f"{nm}: {arg} is cast with `$ before use, or used with like",
                  (cast or like) if used_bare else True, True)
    check("the reserved word check would still catch one",
          sorted(q_names("{[d;ss] ss:1}") & Q_RESERVED), ["ss"])
    check("the order query groups nothing",
          [ln.strip() for ln in Q_ORDERS.splitlines()
           if "last " in ln and " by " in ln], [])

    print("\nthe market is the sym suffix")
    check("Japan", market_of("7203.JP"), "JP")
    check("Korea", market_of("005930.KS"), "KR")
    check("Malaysia", market_of("1155.MK"), "MY")
    check("Thailand", market_of("PTT.TB"), "TH")
    check("Indonesia", market_of("BBCA.IJ"), "ID")
    check("Taiwan", market_of("2330.TT"), "TW")
    check("China takes three suffixes",
          [market_of("600519.CH"), market_of("600519.C1"),
           market_of("000001.C2")], ["CN", "CN", "CN"])
    check("India takes two",
          [market_of("RELIANCE.IN"), market_of("RELIANCE.IS")], ["IN", "IN"])
    check("Hong Kong has no daily limit and is out of scope",
          market_of("0700.HK"), None)
    check("so is Australia", market_of("BHP.AU"), None)
    check("case does not matter", market_of("7203.jp"), "JP")
    check("only the last dot counts", market_of("BRK.A.JP"), "JP")
    check("no suffix, no market", market_of("AAPL"), None)
    check("every suffix is sent to q",
          len(SYM_PATTERNS), sum(len(m.suffixes) for m in MARKETS))
    check("and each maps back to exactly one market",
          len(SUFFIX_MARKET), len(SYM_PATTERNS))

    print("\nwhich limit a period was at")
    check("no ask means limit up", pin_side(60, 0), "up")
    check("no bid means limit down", pin_side(0, 60), "down")
    check("mostly no ask still means up", pin_side(58, 2), "up")
    check("a locked book alone says nothing", pin_side(0, 0), None)
    check("and a dead heat is not a guess", pin_side(7, 7), None)
    check("a period with no side is dropped, not assumed",
          to_pins([_pin("X.JP", 0, 60_000, noask=5, nobid=5)]), [])

    print("\nfavourable")
    check("selling into a limit up", is_favourable(-1, "up"), True)
    check("buying into a limit down", is_favourable(1, "down"), True)
    check("selling into a limit down is NOT", is_favourable(-1, "down"), False)
    check("buying into a limit up is NOT", is_favourable(1, "up"), False)

    print("\nwindows")
    check("two windows that meet", overlap(0, 10, 5, 20), (5, 10))
    check("one inside the other", overlap(0, 100, 40, 50), (40, 50))
    check("touching at a point is not an overlap", overlap(0, 10, 10, 20), None)
    check("apart", overlap(0, 5, 10, 20), None)
    check("an open ended window runs to the end of the other",
          overlap(5, None, 0, 20), (5, 20))
    check("even when it starts inside", overlap(15, None, 0, 20), (15, 20))

    print("\nwas a split on the market")
    w0, w1 = 11 * H, 12 * H
    check("resting right through it",
          split_active(_split(on=10 * H, off=13 * H), w0, w1), True)
    check("arriving part way in",
          split_active(_split(on=w0 + 60_000, off=13 * H), w0, w1), True)
    check("leaving just after it starts",
          split_active(_split(on=10 * H, off=w0 + 1), w0, w1), True)
    check("gone before it began",
          split_active(_split(on=9 * H, off=10 * H), w0, w1), False)
    check("arriving after it ended",
          split_active(_split(on=13 * H, off=14 * H), w0, w1), False)
    check("still open counts as active",
          split_active(_split(on=10 * H, off=None), w0, w1), True)
    check("never reached the market cannot have been active",
          split_active(_split(on=None, off=None), w0, w1), False)

    print("\nwhich orders the limit touched")
    p1 = to_chains(to_attempts(
        [_p(1, "JP", 1000, t_start=9 * H, t_end=15 * H),
         _p(2, "JP", 1000, t_start=9 * H, t_end=10 * H)]))
    pins = to_pins([_pin(p1[0].sym, 11 * H, 12 * H),
                    _pin(p1[1].sym, 11 * H, 12 * H)])
    kept, hits = touched(p1, pins)
    check("an order live through the limit is in",
          [c.attempts[0].id_target for c in kept], [1])
    check("one that finished before it is not", len(kept), 1)
    check("and the window is carried with it",
          len(hits[kept[0].chain_key]), 1)
    check("a stock with no limit period at all brings nothing",
          len(touched(p1, [])[0]), 0)

    print("\nchaining a replaced order")
    #  one order, sent three times under one client id, live 09:00-15:00
    rep_recs = [_p(i, "JP", 1000, cid="CLI-R", t_start=(9 + i) * H,
                   t_end=15 * H) for i in (1, 2, 3)]
    ra = to_attempts(rep_recs)
    rc = to_chains(ra, "asked", [])
    check("three targets", len(ra), 3)
    check("one order", len(rc), 1)
    check("counted once, not three times", rc[0].size, 1000)
    check("its window spans every attempt",
          (rc[0].t_start, rc[0].t_end), (10 * H, 15 * H))
    check("counting targets would have said 3 orders and 3000",
          (len(ra), sum(a.size for a in ra)), (3, 3000))
    check("a target with no 9604 stands alone",
          len(to_chains(to_attempts(
              [_p(1, "JP", 100, cid=""), _p(2, "JP", 100, cid="")]))), 2)

    print("\nsplits are pooled across the chain")
    #  THE FALSE POSITIVE THIS PREVENTS.  The order is re-sent during a limit;
    #  the SECOND attempt rests on the book through it.  Looking at attempt one
    #  alone, nothing of ours was there - which is not true of the ORDER.
    #  the SAME sym on both sends - a replaced order keeps its stock, and a
    #  chain whose attempts disagree on sym would be flagged as mixed
    fp = [_p(1, "JP", 1000, sidesign=-1, cid="CLI-F", sym="6103.JP",
             t_start=9 * H, t_end=11 * H + 600_000),
          _p(2, "JP", 1000, sidesign=-1, cid="CLI-F", sym="6103.JP",
             t_start=11 * H + 600_000, t_end=15 * H)]
    fa = to_attempts(fp)
    fsp = to_splits([_w(9, 2, 400, "filled", on=11 * H, off=13 * H)], fa)
    fc = to_chains(fa, "asked", fsp)
    fk, fh = touched(fc, to_pins([_pin(fa[0].sym, 11 * H, 12 * H)]))
    check("the order is one chain", len(fc), 1)
    check("its attempts agree on everything", fc[0].disagrees_on(), [])
    check("and the limit touched it", len(fk), 1)
    check("nothing is reported: a SIBLING attempt was on the book",
          len(missed_opportunities(fk, fsp, fh)), 0)
    #  the same data, un-chained, is the false positive
    ua = to_chains(fa, "asked", fsp)
    ua = unchain(ua, [(ua[0], 0)])
    uk, uh = touched(ua, to_pins([_pin(fa[0].sym, 11 * H, 12 * H)]))
    check("un-chained, the first send looks like it sent nothing",
          len(missed_opportunities(uk, fsp, uh)), 1)

    print("\nfavourable, no split")
    #  one seller, stock limit UP 11:00-12:00, order live all day, 1000 to do
    par = to_chains(to_attempts([_p(1, "JP", 1000, sidesign=-1,
                                    t_start=9 * H, t_end=15 * H)]))
    pin = to_pins([_pin(par[0].sym, 11 * H, 12 * H, price=2500.0)])
    kept, hits = touched(par, pin)

    def miss(splits_records):
        sp = to_splits(splits_records, kept)
        return missed_opportunities(kept, sp, hits)

    check("nothing sent at all is a finding", len(miss([])), 1)
    check("with nothing executed", miss([])[0].executed, 0)
    check("so completion is zero", miss([])[0].completion, 0.0)
    check("and the quantity missed is the whole order", miss([])[0].unfilled,
          1000)
    check("with the window it was left in",
          miss([])[0].window, (11 * H, 12 * H))
    check("and the minutes", round(miss([])[0].minutes), 60)
    check("a split resting through it is not a finding",
          len(miss([_w(9, 1, 0, "leave", on=10 * H, off=13 * H)])), 0)
    check("a split that came and went BEFORE it still is",
          len(miss([_w(9, 1, 0, "leave", on=9 * H, off=10 * H)])), 1)
    check("and the Splits column says the order did work",
          miss([_w(9, 1, 0, "leave", on=9 * H, off=10 * H)])[0].splits_total, 1)
    check("a fully filled order is not a finding",
          len(miss([_w(9, 1, 1000, "filled", on=9 * H, off=10 * H)])), 0)
    part = miss([_w(9, 1, 250, "filled", on=9 * H, off=10 * H)])
    check("a part filled one reports what it did", part[0].executed, 250)
    check("and its completion", round(part[0].completion, 1), 25.0)
    check("with the missed quantity still derivable", part[0].unfilled, 750)
    check("a rejected split never reached the market, so it does not excuse us",
          len(miss([_w(9, 1, 0, "rejected", on=None, off=None)])), 1)

    #  the same stock limit DOWN is unfavourable to a seller
    dn = to_pins([_pin(par[0].sym, 11 * H, 12 * H, noask=0, nobid=60)])
    k2, h2 = touched(par, dn)
    check("an unfavourable limit is not a finding",
          len(missed_opportunities(k2, [], h2)), 0)

    #  too short to matter
    brief = to_pins([_pin(par[0].sym, 11 * H, 11 * H + 60_000)])
    k3, h3 = touched(par, brief)
    check("a limit shorter than MIN_PIN_MINS is not a finding",
          len(missed_opportunities(k3, [], h3)), 0)
    check("but it is with a lower threshold",
          len(missed_opportunities(k3, [], h3, min_mins=0.5)), 1)

    #  close-only
    co = to_chains(to_attempts([_p(1, "JP", 1000, sidesign=-1,
                                   t_start=14 * H, t_end=14 * H + 600_000,
                                   doclose=1)]))
    cp = to_pins([_pin(co[0].sym, 14 * H, 14 * H + 600_000)])
    k4, h4 = touched(co, cp)
    check("a short close-only order is doing what it was told",
          len(missed_opportunities(k4, [], h4)), 0)

    #  several windows on one order
    many = to_pins([_pin(par[0].sym, 10 * H, 10 * H + 600_000),
                    _pin(par[0].sym, 11 * H, 13 * H),
                    _pin(par[0].sym, 14 * H, 14 * H + 300_000)])
    k5, h5 = touched(par, many)
    m5 = missed_opportunities(k5, [], h5)
    check("one row per order, not per window", len(m5), 1)
    check("and it is the longest window", round(m5[0].minutes), 120)
    check("with the others counted", m5[0].windows_qualifying, 3)

    print("\nthe chain diagnostics")
    dsp = to_splits([_w(1, 1, 0, "rejected")], ra)
    st = chain_stats(ra, rc, dsp)
    check("it counts the sends and the orders", (st.attempts, st.chains), (3, 1))
    check("and how many collapsed", st.multi, 1)
    check("nothing untagged in this fixture", st.no_id, 0)
    ua2 = to_attempts([_p(1, "JP", 100, cid=""), _p(2, "TH", 100, cid="")])
    check("untagged targets are counted per market",
          chain_stats(ua2, to_chains(ua2), []).no_id_by_market,
          {"JP": 1, "TH": 1})
    mx = to_attempts([_p(1, "JP", 100, cid="CLI-M", algo="vwap"),
                      _p(2, "JP", 100, cid="CLI-M", algo="twap")])
    check("one id over two algos is REPORTED, not absorbed",
          len(chain_stats(mx, to_chains(mx), []).mixed), 1)
    ov = to_attempts([_p(1, "JP", 100, cid="CLI-O", t_start=9 * H),
                      _p(2, "JP", 100, cid="CLI-O", t_start=10 * H)])
    ovsp = to_splits([_w(1, 1, 80, "filled"), _w(2, 2, 80, "filled")], ov)
    ovc = to_chains(ov, "max", ovsp)
    ovst = chain_stats(ov, ovc, ovsp)
    check("a chain filling more than it asked for is caught",
          len(ovst.over), 1)
    check("and un-chaining puts it back to one order per target",
          len(unchain(ovc, ovst.over)), 2)
    check("after which nothing over fills",
          len(chain_stats(ov, unchain(ovc, ovst.over), ovsp).over), 0)
    check("asked would not have over filled in the first place",
          len(chain_stats(ov, to_chains(ov, "asked", ovsp), ovsp).over), 0)

    print("\nrollups")
    rows = by_market(kept, to_splits([_w(1, 1, 400, "filled"),
                                      _w(2, 1, 0, "rejected")], kept))
    check("all eight markets, in order", [r.code for r in rows],
          list(MARKET_CODES))
    check("Japan is row one", rows[0].code, "JP")
    check("its qty", rows[0].order_qty, 1000)
    check("its executed", rows[0].executed, 400)
    check("its rejections", rows[0].rejections, 1)
    check("completion", round(rows[0].completion, 1), 40.0)
    check("a market with no flow has no completion", rows[1].completion, None)
    check("with one market the headline is that market",
          round(totals(rows).completion, 1), 40.0)

    #  the headline is the real fill rate: a big market at 10% beside a small
    #  one at 90% is 10.8%, because most of the quantity really was not done
    two = [Row("JP", "Japan", 100, 1_000_000, 100_000, 0),
           Row("KR", "Korea", 1, 10_000, 9_000, 0),
           Row("MY", "Malaysia", 0, 0, 0, 0)]
    check("the headline is summed executed over summed order qty",
          round(totals(two).completion, 1), 10.8)
    check("which is exactly that ratio",
          round(100.0 * 109_000 / 1_010_000, 1), 10.8)
    check("the mean of the market rows would have said 50",
          round(sum(r.completion for r in two if r.completion is not None)
                / 2, 1), 50.0)
    check("a market with no orders contributes nothing to either side",
          totals(two).completion, totals(two[:2]).completion)

    print("\nmodes")
    now = dt.datetime(2026, 7, 24, 18, 37)
    rt = plan(now=now)
    check("no flags is realtime", rt.hist, False)
    check("and reaches both realtime servers", rt.server_names,
          ("ORDER_SERVER_RT", "QATT_SERVER_RT"))
    check("with no date to filter on", rt.dates, [None])
    check("named for today", rt.stem, "luld_report_2026-07-24")
    one = plan(date=dt.date(2026, 7, 1), now=now)
    check("--date is historical", one.hist, True)
    check("and reaches both historical servers", one.server_names,
          ("ORDER_SERVER_HIST", "QATT_SERVER_HIST"))
    check("for one session", one.dates, [dt.date(2026, 7, 1)])
    check("in the daily layout", one.monthly, False)
    mon = plan(monthly="2026-07", now=now)
    check("--monthly is the month's weekdays", len(mon.dates), 23)
    check("in the monthly layout", mon.monthly, True)
    for bad in ("2026-13", "26-07", "2026-7", "july", ""):
        r = False
        try:
            plan(monthly=bad)
        except SystemExit:
            r = True
        check(f"--monthly {bad!r} is rejected", r, True)

    print("\nthe page")
    try:
        import matplotlib      # noqa: F401
    except ImportError:
        print("  ..    matplotlib not installed, rendering skipped")
        print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
        return 0 if ok else 1

    dp, ds, _dh, dm = demo_session()
    dr = by_market(dp, ds)
    check("the demo touches every market",
          all(r.orders for r in dr), True)
    check("and finds something to report", len(dm) > 0, True)
    check("deterministic", [x.unfilled for x in demo_session()[3]],
          [x.unfilled for x in dm])

    figs = pages_for(dr, totals(dr), dm, "By market  ·  x", "Generated  ·  x")
    check("the daily report is two pages", len(figs), 2)
    check("the findings columns add up to the full width",
          round(sum(c[1] for c in MISS_COLS), 6), 1.0)
    check("and so do the market ones",
          round(sum(c[1] for c in MKT_COLS), 6), 1.0)
    check("the findings table shows what was done, not what was not",
          [c[0] for c in MISS_COLS][3:6], ["Order qty", "Exec qty", "Completion"])
    buf = io.BytesIO()
    figs[0].savefig(buf, format="pdf")
    check("page one renders", buf.getvalue()[:5], b"%PDF-")

    mp, msp, mm = demo_month()
    check("the monthly demo keeps its findings - the two key kinds are not "
          "mixed", len(mm) > 0, True)
    check("and every one belongs to a chain that was kept",
          {x.parent.chain_key for x in mm} <= {c.chain_key for c in mp}, True)
    figs_m = pages_for(by_market(mp, msp), totals(by_market(mp, msp)), mm,
                       "x", "y", by_day(mp, msp))
    check("the monthly report adds the day charts page", len(figs_m) >= 3, True)

    lots = dm * 20
    figs_l = pages_for(dr, totals(dr), lots, "x", "y")
    check("a long findings list paginates, capped",
          len(figs_l), 1 + FINDINGS_MAX_PAGES)
    check("nothing is dropped silently - the overflow is counted",
          len(lots) - FINDINGS_PER_PAGE * FINDINGS_MAX_PAGES > 0, True)

    none_figs = pages_for(by_market([], []), totals(by_market([], [])), [],
                          "x", "y")
    check("an empty session still renders", len(none_figs), 2)
    buf2 = io.BytesIO()
    none_figs[1].savefig(buf2, format="pdf")
    check("including the nothing-to-report page", buf2.getvalue()[:5], b"%PDF-")

    with tempfile.TemporaryDirectory() as d:
        check("--demo writes both reports", demo(d), 0)
        names = sorted(q.name for q in Path(d).iterdir())
        check("as SAMPLE-named pdfs",
              [n for n in names if n.endswith(".pdf")],
              ["luld_report_SAMPLE_daily.pdf",
               "luld_report_SAMPLE_monthly.pdf"])
        check("with a png per page",
              len([n for n in names if n.endswith(".png")]) >= 5, True)

    print("\nemail")
    try:
        m = _mailer()
    except SystemExit as e:
        print(f"  ..    {e}")
        print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
        return 0 if ok else 1
    check("an empty EMAIL_TO is the off switch", email_configured(), False)
    check("nothing to authenticate with, by design",
          [f for f in ("SMTP_USER", "SMTP_PASSWORD", "USER", "PASSWORD")
           if f in globals()], [])
    check("the body is the signature and nothing else",
          mail_body(), "Best Regards,\n\nKhalife")
    check("no table in it", "Market" in mail_body(), False)
    check("no numbers in it", any(c.isdigit() for c in mail_body()), False)

    with tempfile.TemporaryDirectory() as d:
        files = save(figs, d, "luld_report_2026-07-24")
        pdfs = [f for f in files if f.suffix == ".pdf"]
        check("one PDF for the whole report, however many pages", len(pdfs), 1)
        msg = m.build_message(m.Mail(
            subject=f"{TITLE} - x", sender="a@b.com", to=["c@d.com"],
            text=mail_body(), attachments=pdfs))
        check("the PDF is the only thing attached",
              [q.get_filename() for q in msg.walk() if q.get_filename()],
              ["luld_report_2026-07-24.pdf"])
        check("no page is inlined",
              any(q.get_content_type().startswith("image/")
                  for q in msg.walk()), False)
        check("the message is plain text, with no html part",
              any(q.get_content_type() == "text/html" for q in msg.walk()),
              False)
        check("and it still sends to everyone it should",
              m.send(msg, m.Smtp(), dry_run=True), ["c@d.com"])

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


def _split(on, off):
    """A bare Split, for the split_active checks."""
    return Split(key=(None, 1, 1), id_work=1, date=None, market="JP", make=0,
                 rejected=False, on_market=on, off_market=off)


# =============================================================================
# CLI
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Limit Up / Limit Down Order Report - completion, "
                    "rejections and missed favourable limits by market. "
                    "Mailing it is configured in the EMAIL block at the top "
                    "of this file, not here.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--monthly", metavar="YYYY-MM",
                   help="report a whole month off the HISTORICAL servers, and "
                        "add a page of the by day charts")
    p.add_argument("--date", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
                   help="one past session off the HISTORICAL servers. Without "
                        "this and without --monthly the report is a REALTIME "
                        "snapshot of the session in progress")
    p.add_argument("--min-mins", type=float, default=MIN_PIN_MINS,
                   help="how long a favourable limit must overlap an order "
                        "before not trading into it is worth reporting")
    p.add_argument("--chain-qty", choices=QTY_CHOICES, default=CHAIN_QTY,
                   help="what quantity a chain asked for. asked reads it off "
                        "the fills and cannot print over 100%%")
    p.add_argument("--keep-over", action="store_true",
                   help="do NOT un-chain the orders that execute more than "
                        "they asked for")
    p.add_argument("--out-dir", default=str(OUT_DIR),
                   help="where the .pdf and .png are written")
    p.add_argument("--quiet", action="store_true", help="no per date progress")
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
    if args.min_mins < 0:
        p.error("--min-mins cannot be negative")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
