#!/usr/bin/env python3
"""
=============================================================================
luld_orders.py - orders that worked a stock while it was at a limit

STEP 1 of a rebuild.  What this answers, and only this:

  of the orders whose stock was limit up or limit down while the order was
  live, how much did we ask for and how much got done, by region.

BOTH SIDES COUNT.  A limit up is favourable to a seller and a limit down to a
buyer, but an unfavourable limit is not an excuse: a market order can be
marketable into one.  Which side it was is therefore not a filter here, and
nothing is dropped for being on the wrong side of the band.

A LIMIT PERIOD COMES FROM THE BOOK.  A stock at its limit stops quoting two
sided - it locks (bid = ask) or goes one sided (one side empty, the other
carrying the band).  That is the whole test, and it is the same expression
queries/limit_up_down/limit_up_down.q uses:

    lim: ((qbid=qask)&0<qbid) | ((0=qbid)&0<qask) | ((0=qask)&0<qbid)

Contiguous runs of it are one period each.  The boundaries are the NORMAL
ticks either side of a run, never a gap threshold: two limit periods with two
sided quoting between them are genuinely two periods, and a threshold would
have to guess.  A run counts only if it lasted at least --min-mins, which is
limit_up_down.q's `lookback` doing the job it does there - keeping a two tick
blip from reading as a limit.  Unlike that script, every period in the session
is found, not only the one in force at .z.T: a snapshot answers "what is
pinned right now", and a report of a past day needs the periods that resolved
before the bell as much as the ones that did not.

WHAT IS NOT HERE YET, on purpose:

  no chaining.  The engine writes a NEW id_target every time an order is
    re-sent, so three sends of 27m read here as three orders and 81m asked.
    The run PRINTS how many targets share a FIX tag 9604 so the size of it is
    visible, and does nothing about it.  Step 2.
  no marketable window, no split check.  Whether a split was actually on the
    book during the period is the question this is being built towards.
  no rejections, no email, no findings table.

pykx is imported lazily, so the analytics and the whole rendering path run on
a machine with no kdb, no pykx and no q licence:

  python scripts/luld_orders/luld_orders.py --self-test
  python scripts/luld_orders/luld_orders.py --demo
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
# `python scripts/luld_orders/luld_orders.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.local_config import apply_local                        # noqa: E402
from lib.order_chains import CLIENT_ID_TAG, fix_tag             # noqa: E402
from lib.q_lint import (                                        # noqa: E402
    balanced, groups_in_q, reserved_used, uncast_symbols)
from lib.report_page import (                                   # noqa: E402
    DASH, GREEN, INK, INK2, INK3, L, RED, figure, fmt_hm, fmt_int, fmt_pct1,
    footer, heading, kpis, log, save, table)

# -----------------------------------------------------------------------------
# CONNECTIONS.  Edit these, or put them in a local_settings.py beside this
# script - see scripts/lib/README.md.
#
# Four endpoints: the order server and the quote server, each realtime and
# historical.  All open processes - host and port is the whole of it.
#
# The order server and qatt stamp their times on the SAME CLOCK, so an order's
# window is compared against a limit period with no conversion.
# -----------------------------------------------------------------------------

ORDER_SERVER_RT = "CHANGEME:5012"     # realtime   - target and workorder
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical - the same two, plus `date`
QATT_SERVER_RT = "CHANGEME:5013"      # realtime   - qatt
QATT_SERVER_HIST = "CHANGEME:5011"    # historical - qatt, plus `date`

_PLACEHOLDER = "CHANGEME"

OUT_DIR = Path(__file__).resolve().parent / "out"
DPI = 200

# How long a run of locked or one sided quoting has to last before it is a
# limit period rather than a print.  limit_up_down.q takes this as `lookback`.
MIN_LIMIT_MINS = 20.0

# -----------------------------------------------------------------------------
# Anything above can be overridden from a local_settings.py beside this script,
# which git ignores - so the servers survive a pull and this file never has to
# be edited.
# -----------------------------------------------------------------------------

apply_local(globals(), __file__)


# =============================================================================
# SCOPE
# =============================================================================

class Region(NamedTuple):
    code: str
    name: str
    suffixes: tuple            # what the sym ends with on the feed


# Fixed order.  The table always prints all eight, whether or not they traded -
# a region absent from the data is otherwise indistinguishable from one nobody
# remembered to ask about.
#
# China and India carry several suffixes each, so the lookup is many-to-one.
# Hong Kong, Australia, Singapore and New Zealand are NOT here: they have no
# daily price limit, so there is no limit to be up or down against.  A
# whitelist, so a new venue is out until someone puts it in.
REGIONS = (
    Region("JP", "Japan", (".JP",)),
    Region("KR", "Korea", (".KS",)),
    Region("MY", "Malaysia", (".MK",)),
    Region("TH", "Thailand", (".TB",)),
    Region("ID", "Indonesia", (".IJ",)),
    Region("CN", "China", (".CH", ".C1", ".C2")),
    Region("TW", "Taiwan", (".TT",)),
    Region("IN", "India", (".IN", ".IS")),
)

REGION_CODES = tuple(r.code for r in REGIONS)
REGION_NAME = {r.code: r.name for r in REGIONS}
SUFFIX_REGION = {s: r.code for r in REGIONS for s in r.suffixes}
#  the patterns q filters on are BUILT FROM the table Python maps back with, so
#  the two cannot drift apart
SYM_PATTERNS = tuple("*" + s for r in REGIONS for s in r.suffixes)


def region_of(sym) -> Optional[str]:
    """The region a sym belongs to, from its suffix.  None when it is not one
    of the eight - which is how everything else on the book stays out."""
    s = _s(sym).upper()
    i = s.rfind(".")
    return SUFFIX_REGION.get(s[i:]) if i > 0 else None


# =============================================================================
# QUERIES
#
# Lambdas, sent over the handle and run there.  Nothing is grouped in q: a
# target row is one send and a workorder row is one child, and every sum and
# count happens in Python where --self-test can prove it.
# =============================================================================

Q_ORDERS = """
{[hist;d;sfx]
  et:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; sym:0#`; side:0#`;
         sidesign:0#0i; size:0#0i; otype:0#`; basket:0#`; limit_price:0#0n;
         t_gen:0#0Nt; t_start:0#0Nt; t_end:0#0Nt; fixmsg:0#`;
         fxlast:0#0n; adjclose:0#0n; orgclose:0#0n);
  ew:([] date:0#0Nd; id_server:0#0i; id_work:0#0i; id_target:0#0i; make:0#0i;
         t_gen:0#0Nt; t_off_market:0#0Nt; avg_fill_price:0#0n;
         t_transmit:0#0Nt; transmit_bidprice:0#0n; transmit_askprice:0#0n);
  es:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; time:0#0Nt; state:0#`);

  / parents.  Every side: a limit up is favourable to a seller and a limit down
  / to a buyer, and an unfavourable one can still be marketable, so nothing is
  / filtered away on side here.
  t:$[hist;
      select date,id_server,id_target,sym,side,sidesign,size,otype,basket,
          limit_price,t_gen,t_start,t_end,fixmsg
        from target where date=d, any (upper sym) like/: sfx;
      update date:0Nd from select id_server,id_target,sym,side,sidesign,size,
          otype,basket,limit_price,t_gen,t_start,t_end,fixmsg
        from target where any (upper sym) like/: sfx];
  if[0=count t; :(et;ew;es)];

  ids:exec distinct id_target from t;

  / the previous close, which is what says which BAND a locked book is at.  A
  / stock at its limit does not always go one sided - it can lock, bid = ask,
  / and then the side that went missing cannot answer the question because
  / neither did.  target_stock carries the close per target, so this is a
  / lookup on the key the row already has rather than a second symbology.
  x:$[hist;
      `date`id_server`id_target xkey select date,id_server,id_target,fxlast,
          adjclose,orgclose from target_stock where date=d, id_target in ids;
      `id_server`id_target xkey select id_server,id_target,fxlast,adjclose,
          orgclose from target_stock where id_target in ids];
  t:t lj x;

  / children.  make is what the child executed, whatever state it ended in.
  / t_gen is when the split was CREATED and t_off_market when it left the
  / book.  t_transmit and t_oes_send say when we SENT it, a third question.
  / avg_fill_price is what the child really paid, and the transmit quote is
  / the market the order actually met - both are what put it in USD.
  w:$[hist;
      select date,id_server,id_work,id_target,make,t_gen,t_off_market,
          avg_fill_price,t_transmit,transmit_bidprice,transmit_askprice
        from workorder where date=d, id_target in ids;
      update date:0Nd from select id_server,id_work,id_target,make,t_gen,
          t_off_market,avg_fill_price,t_transmit,transmit_bidprice,
          transmit_askprice
        from workorder where id_target in ids];
  / every state row, ungrouped.  Which one is LAST is decided in Python,
  / where the self test can prove it - grouping it down to one row per order
  / here would hand back an answer with no way to see what it stood for.
  st:$[hist;
      select date,id_server,id_target,time,state from target_state
        where date=d, id_target in ids;
      update date:0Nd from select id_server,id_target,time,state
        from target_state where id_target in ids];
  (t;w;st)
  }
"""

Q_LIMITS = """
{[hist;d;syms]
  / `$ IS LOAD BEARING.  syms arrives as a list of CHAR VECTORS - PyKX sends
  / bytes that way - and `sym in syms` against a symbol column with char
  / vectors on the right matches NOTHING.  Not an error: every day would come
  / back with no limit periods, no orders in scope, and a page of zeros that
  / reads exactly like a calm market.
  syms:`$syms;
  ep:([] sym:0#`; grp:0#0j; start:0#0Nt; end:0#0Nt; price:0#0n; ticks:0#0j;
         noask:0#0j; nobid:0#0j; net:0#0n);
  / rows with nothing on either side are trade prints or pre-open gaps - they
  / would read as one sided and break a run in two
  / netChange comes off the last TRADED price, so it is 0 or null on exactly
  / the stocks being hunted - it is a last resort, never the first answer
  q:$[hist;
      select time,sym,qbid:0^qbid,qask:0^qask,netChange:0^netChange from qatt
        where date=d, sym in syms, (0<0^qbid)|0<0^qask;
      select time,sym,qbid:0^qbid,qask:0^qask,netChange:0^netChange from qatt
        where sym in syms, (0<0^qbid)|0<0^qask];
  if[0=count q; :ep];
  q:`sym`time xasc q;
  / locked, or one sided with the band on the side that is left
  q:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from q;
  / contiguous runs.  The NORMAL ticks between two runs are what separate them
  q:update grp:sums differ lim by sym from q;
  / noask and nobid are the evidence for WHICH limit it was, counted across the
  / whole run rather than read off one tick: at limit up nobody will offer, so
  / the ask goes missing, and at limit down nobody bids
  0!select start:first time, end:last time,
      price:last ?[0=qbid;qask;qbid], ticks:count i,
      noask:sum 0=qask, nobid:sum 0=qbid, net:last netChange
    by sym,grp from q where lim
  }
"""


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
    and everything else off the wire run without it.
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
    t, w, st = handle(Q_ORDERS, hist,
                      d if d is not None else _UNUSED_DATE, sfx)
    return (t.pd().to_dict("records"), w.pd().to_dict("records"),
            st.pd().to_dict("records"))


def fetch_limits(handle, hist: bool, d: Optional[dt.date], syms):
    if not syms:
        return []
    r = handle(Q_LIMITS, hist, d if d is not None else _UNUSED_DATE,
               [s.encode() for s in syms])
    return r.pd().to_dict("records")


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
    try:
        if v is None:
            return 0
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if n == -2147483648 else n


def _f(v) -> float:
    """A q float.  Null and not-a-number both come back 0.0, which every
    caller here reads as "no reference price", never as a price of zero."""
    try:
        if v is None:
            return 0.0
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f else f          # NaN is q's null float through pandas


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
    except Exception:                              # noqa: BLE001
        return None


def _ms(v) -> Optional[int]:
    """A q time as milliseconds since midnight.  None where it is missing."""
    if v is None:
        return None
    if isinstance(v, dt.timedelta):        # pandas hands a q time back as this
        return int(v.total_seconds() * 1000)
    if isinstance(v, dt.time):
        return ((v.hour * 3600 + v.minute * 60 + v.second) * 1000
                + v.microsecond // 1000)
    try:
        import pandas as pd
        if pd.isna(v):
            return None
        return int(pd.Timedelta(v).total_seconds() * 1000)
    except Exception:                              # noqa: BLE001
        return None


class Order(NamedTuple):
    """ONE TARGET ROW - one send.

    Not chained: an order the engine re-sent is several of these, and step 1
    counts them as they stand.  client_id is carried only so the run can say
    how many targets share one, which is the size of what chaining would
    change.
    """
    key: tuple                 # (date, id_server, id_target)
    date: Optional[dt.date]
    region: str
    sym: str
    side: str
    otype: str                 # `market`, `limit`, ... - as the feed sends it
    basket: str                # what the order was sent as part of, if any
    size: int                  # SHARES
    sidesign: int              # +1 buy, -1 sell
    limit_price: float         # 0.0 on a market order
    fx: float                  # target_stock.fxlast, local -> USD
    t_gen: Optional[int]       # when the order was CREATED, ms since midnight
    t_start: Optional[int]     # ms since midnight
    t_end: Optional[int]
    client_id: str             # FIX tag 9604, "" when the client sent none
    id_target: int
    ref: float                 # the previous close a band is measured from


UP, DOWN, UNKNOWN = "up", "down", "unknown"


def direction_of(noask: int, nobid: int, price: float = 0.0,
                 ref: float = 0.0, net: float = 0.0) -> str:
    """Which limit a period was at.  The cascade is limit_up_down.q's.

    A stock at its limit does NOT always go one sided.  It can LOCK - bid =
    ask, both present - and then no side went missing and the book cannot
    answer on its own.  So three questions, in order of how much they can be
    trusted:

      1. WHICH SIDE WENT AWAY.  At limit up nobody will offer, so the ask goes
         missing; at limit down nobody bids.  Counted across the whole run
         rather than read off one tick, because a pinned book flickers.
      2. WHERE THE PRICE SITS against the previous close.  This is what settles
         a locked book: a band above the close is limit up, below it is down.
         `ref` is target_stock's adjclose, or orgclose where that is null.
      3. netChange, and only then.  It comes off the last TRADED price, so it
         is 0 or null on exactly the stocks being hunted - a last resort, and
         the q script says the same.

    UNKNOWN is what is left when all three decline, and it is a real answer:
    a locked stock with no close on file cannot be called, and saying so beats
    guessing.  Nothing is DROPPED for it - direction is reported, never used to
    decide whether an order is in scope.
    """
    if noask > nobid:
        return UP
    if nobid > noask:
        return DOWN
    if ref > 0 and price > 0:
        if price > ref:
            return UP
        if price < ref:
            return DOWN
    if net > 0:
        return UP
    if net < 0:
        return DOWN
    return UNKNOWN


def pct_from_close(price: float, ref: float) -> Optional[float]:
    """How far the band sits from the previous close.

    Reported, not filtered on.  limit_up_down.q calls this the sanity check -
    a genuine limit sits AT the band, so something locked at +0.1% is a locked
    market and not a limit - but the bands differ enough across APAC that
    turning it into a threshold needs reference data this does not have.
    """
    if ref <= 0 or price <= 0:
        return None
    return 100.0 * (price - ref) / ref


class Limit(NamedTuple):
    """ONE LIMIT PERIOD on one stock.  Its direction is reported, not filtered
    on: an unfavourable limit can still be marketable."""
    sym: str
    date: Optional[dt.date]
    start: int
    end: int
    price: float
    ticks: int
    noask: int = 0
    nobid: int = 0
    net: float = 0.0

    @property
    def minutes(self) -> float:
        return (self.end - self.start) / 60_000.0

    @property
    def locked(self) -> bool:
        """Neither side went missing, so the book alone cannot call it."""
        return self.noask == self.nobid

    def direction(self, ref: float = 0.0) -> str:
        """A method rather than a property: a locked period cannot be called
        without the stock's previous close, and the period does not carry it -
        it belongs to the stock, and arrives with the order."""
        return direction_of(self.noask, self.nobid, self.price, ref, self.net)


def to_orders(records) -> list:
    out = []
    for r in records:
        sym = _s(r.get("sym"))
        region = region_of(sym)
        if region is None:
            continue
        out.append(Order(
            key=(_d(r.get("date")), _i(r.get("id_server")),
                 _i(r.get("id_target"))),
            date=_d(r.get("date")), region=region, sym=sym,
            side=_s(r.get("side")), otype=_s(r.get("otype")),
            basket=_s(r.get("basket")), size=_i(r.get("size")),
            sidesign=_i(r.get("sidesign")),
            limit_price=_f(r.get("limit_price")),
            fx=_f(r.get("fxlast")), t_gen=_ms(r.get("t_gen")),
            t_start=_ms(r.get("t_start")), t_end=_ms(r.get("t_end")),
            client_id=fix_tag(r.get("fixmsg")),
            id_target=_i(r.get("id_target")),
            #  adjclose first, orgclose where it is null - q's orgclose^adjclose
            ref=_f(r.get("adjclose")) or _f(r.get("orgclose"))))
    return out


class Splits(NamedTuple):
    """What one order's children add up to.

    n is EVERY workorder row under the target, whatever became of it - a
    split that was rejected is still a split the engine made, and a count
    that quietly left those out would say the order tried less than it did.
    """
    n: int = 0
    made: int = 0                      # SHARES
    first_gen: Optional[int] = None    # earliest CREATED, ms since midnight
    last_off: Optional[int] = None     # latest OFF the book
    filled_local: float = 0.0          # sum of make * what it really paid
    quote: tuple = ()                  # (t_transmit, bid, ask), earliest

    def add(self, make, gen, off, px=0.0, tx=None, bid=0.0,
            ask=0.0) -> "Splits":
        """One more child.  A missing time cannot move a bound: a split
        that never reached the market has no t_off_market, and taking that
        as an end would date the order's last child to midnight.

        The quote kept is the EARLIEST child's that carried one - the market
        the order actually met.  A child with no fill price adds nothing to
        filled_local rather than adding its shares at zero.
        """
        q = self.quote
        if (bid > 0 or ask > 0) and (not q or (tx is not None
                                               and tx < q[0])):
            q = (tx if tx is not None else 0, bid, ask)
        return Splits(
            self.n + 1, self.made + make,
            gen if self.first_gen is None else (
                self.first_gen if gen is None else min(self.first_gen, gen)),
            off if self.last_off is None else (
                self.last_off if off is None else max(self.last_off, off)),
            self.filled_local + (make * px if px > 0 else 0.0), q)


def splits_by_order(records, orders) -> dict:
    """{order key: Splits}.

    Keyed back onto the orders that survived the region filter, so a workorder
    whose parent is not in scope cannot contribute quantity to a region that
    has no order in it.
    """
    known = {o.key for o in orders}
    out = {}
    for r in records:
        key = (_d(r.get("date")), _i(r.get("id_server")),
               _i(r.get("id_target")))
        if key not in known:
            continue
        out[key] = out.get(key, Splits()).add(
            abs(_i(r.get("make"))), _ms(r.get("t_gen")),
            _ms(r.get("t_off_market")), _f(r.get("avg_fill_price")),
            _ms(r.get("t_transmit")), _f(r.get("transmit_bidprice")),
            _f(r.get("transmit_askprice")))
    return out


def made_of(splits, key) -> int:
    """What an order executed.  No children at all is 0, not missing."""
    got = splits.get(key)
    return got.made if got else 0


class LastState(NamedTuple):
    at: Optional[int]          # ms since midnight
    state: str


def last_state_by_order(records, orders) -> dict:
    """{order key: the LAST target_state row it had}.

    Last by time, decided here rather than in the query: `last state by
    id_target` in q hands back one row per order with no way to see what it
    stood for, and this is the evidence a whole order gets dropped on.

    A row with no time cannot be the last one - it cannot be ordered at all,
    and treating it as midnight would make it the FIRST.
    """
    known = {o.key for o in orders}
    out = {}
    for r in records:
        key = (_d(r.get("date")), _i(r.get("id_server")),
               _i(r.get("id_target")))
        if key not in known:
            continue
        at = _ms(r.get("time"))
        if at is None:
            continue
        got = out.get(key)
        if got is None or at >= got.at:
            out[key] = LastState(at, _s(r.get("state")))
    return out


def died_before_starting(o, last) -> bool:
    """Was this order over before its own start time?

    An order cancelled before t_start never worked: nothing of it was ever
    going to reach the book, so counting it as an order that sat through a
    limit is counting a false positive.  The test is the LAST target_state
    row against t_start - if the order's life ended before it began, it did
    not begin.

    NOT DROPPED ON MISSING EVIDENCE.  No state row and no t_start both mean
    the question cannot be answered, and an order silently removed for want
    of a row in another table is worse than one that should not be there:
    the first is invisible, the second shows up in the raw file.
    """
    if last is None or last.at is None or o.t_start is None:
        return False
    return last.at < o.t_start


def drop_dead_on_arrival(orders, states) -> tuple:
    """(the orders that had a life, the ones that were over first)."""
    keep, dead = [], []
    for o in orders:
        (dead if died_before_starting(o, states.get(o.key)) else
         keep).append(o)
    return keep, dead


def to_limits(records, d=None, min_mins=MIN_LIMIT_MINS) -> list:
    """Limit periods long enough to count.

    A period is a FLOOR: a pinned stock often stops quoting altogether, so it
    ends at the last tick that PROVED it and never later.  Under-reporting is
    the chosen direction - a window this cannot prove is not one it claims.
    """
    out = []
    for r in records:
        start, end = _ms(r.get("start")), _ms(r.get("end"))
        if start is None or end is None or end <= start:
            continue
        try:
            price = float(r.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        lim = Limit(sym=_s(r.get("sym")), date=_d(r.get("date")) or d,
                    start=start, end=end, price=price,
                    ticks=_i(r.get("ticks")), noask=_i(r.get("noask")),
                    nobid=_i(r.get("nobid")), net=_f(r.get("net")))
        if lim.minutes < min_mins:
            continue
        out.append(lim)
    return out


# =============================================================================
# WHICH ORDERS THE LIMIT TOUCHED
# =============================================================================

def overlap(a0, a1, b0, b1) -> bool:
    """Do [a0,a1] and [b0,b1] share any time at all?

    A missing order start or end means the order was live from the open or is
    still live, so an absent bound cannot rule an overlap out.
    """
    lo = 0 if a0 is None else a0
    hi = 24 * 3_600_000 if a1 is None else a1
    return lo < b1 and hi > b0


def limits_by_sym(limits) -> dict:
    out = {}
    for w in limits:
        out.setdefault((w.date, w.sym), []).append(w)
    return out


def touched(orders, limits) -> tuple:
    """(orders a limit period overlapped, {order key: its periods}).

    An order that finished before its stock went to the limit is not a LULD
    order, however dramatic the stock's afternoon was.
    """
    index = limits_by_sym(limits)
    keep, hits = [], {}
    for o in orders:
        got = [w for w in index.get((o.date, o.sym), ())
               if overlap(o.t_start, o.t_end, w.start, w.end)]
        if not got:
            continue
        keep.append(o)
        hits[o.key] = got
    return keep, hits


# =============================================================================
# ONE RECORD PER LINE
# =============================================================================

# =============================================================================
# NOTIONAL
#
# `size` is a share count, so putting an order in USD needs a price - and the
# UNFILLED part of an order never traded at one.  The ladder, best first, is
# short_sell_report's:
#
#   limit_price   a limit order is worth what the client said it was worth
#   the quote     a market order has no limit, so it is valued at the side we
#                 would actually have traded - the BID for a sell, the ASK
#                 for a buy - as at the moment the FIRST child was sent
#   ref close     adjclose, else orgclose.  The fallback for an order that
#                 never produced a child at all, which on this page is not a
#                 rare branch: an order that sent nothing is the thing the
#                 report exists to find
#
# EXECUTED is not priced this way at all: it is the sum of make * the child's
# own avg_fill_price, which is what those shares really cost.  So notional
# completion is NOT share completion - it cannot be, because the two sides
# traded at different prices.
#
# Everything is multiplied by target_stock.fxlast, local -> USD.  An order
# with no price or no fx contributes NOTHING and is COUNTED: a notional that
# quietly omits a market is worse than one that admits a gap.
# =============================================================================

def order_price(o, sp) -> tuple:
    """(price in local currency, where it came from) for one order."""
    if o.limit_price > 0:
        return o.limit_price, "limit"
    if sp.quote:
        _tx, bid, ask = sp.quote
        #  sell into the bid, buy from the ask - the side we would have hit
        px = bid if o.sidesign < 0 else ask
        if px > 0:
            return px, "quote"
    if o.ref > 0:
        return o.ref, "close"
    return 0.0, "none"


def favourable_for(side: str, direction: str) -> Optional[bool]:
    """Was the band on the side we could have traded into?

    SELLING into a limit UP, or BUYING into a limit DOWN: there is a queue
    resting at the band and we are the other side of it.

    None where it cannot be said - a direction the book could not call, an
    order live through periods that went both ways, or a side the feed did
    not send.  None is NOT False: an order nobody can classify is not an
    order that was on the wrong side, and counting it as one would put a
    number on the page that no line supports.

    Adverse is not an excuse and is never a filter.  A market order is
    marketable into a band on either side, which is the whole reason both
    are counted and shown next to each other.
    """
    sd = (side or "").strip().lower()
    if direction == UP and sd == "sell":
        return True
    if direction == DOWN and sd == "buy":
        return True
    if direction in (UP, DOWN) and sd in ("buy", "sell"):
        return False
    return None


class Line(NamedTuple):
    """One order in scope, with everything already worked out.

    The region table, the order listing and the raw file are all built from
    these, so the count on the page and the line under it cannot disagree
    about whether an order was favourable or whether it finished.
    """
    o: Order
    sp: Splits
    periods: tuple
    direction: str
    favourable: Optional[bool]
    price: float = 0.0         # local currency, from the ladder
    price_source: str = "none"

    @property
    def executed(self) -> int:
        """SHARES."""
        return self.sp.made

    @property
    def priced(self) -> bool:
        return self.price > 0 and self.o.fx > 0

    @property
    def ordered_usd(self) -> float:
        return self.o.size * self.price * self.o.fx if self.priced else 0.0

    @property
    def executed_usd(self) -> float:
        """What the fills really cost, never the ladder's price."""
        return self.sp.filled_local * self.o.fx if self.o.fx > 0 else 0.0

    @property
    def completion(self) -> Optional[float]:
        """NOTIONAL completion, so the percentage is of the columns it
        sits beside.  Not the share completion: the two sides traded at
        different prices, so they are not the same number."""
        return _completion(self.executed_usd, self.ordered_usd)

    @property
    def share_completion(self) -> Optional[float]:
        return _completion(self.executed, self.o.size)

    @property
    def unfilled(self) -> int:
        return max(0, self.o.size - self.executed)

    @property
    def incomplete(self) -> bool:
        """Did it fail to finish?  SHARES, not notional and not a
        percentage: an order showing 100.0% on a rounded figure still has
        quantity left, and whether it finished is a question about the order
        rather than about what the shares were worth."""
        return self.o.size > 0 and self.executed < self.o.size


def to_lines(orders, splits, hits) -> list:
    """One Line per order, in the order the report counted them."""
    out = []
    for o in orders:
        got = tuple(sorted(hits.get(o.key, ()), key=lambda w: w.start))
        d = line_direction(got, o.ref)
        sp = splits.get(o.key, Splits())
        px, src = order_price(o, sp)
        out.append(Line(o, sp, got, d, favourable_for(o.side, d), px, src))
    return out


# =============================================================================
# ROLLUP
# =============================================================================

def _completion(executed: int, order_qty: int) -> Optional[float]:
    """No order quantity is not 0% - it is nothing to measure against."""
    if order_qty <= 0:
        return None
    return 100.0 * executed / order_qty


class Row(NamedTuple):
    code: str
    name: str
    orders: int
    ordered_usd: float
    executed_usd: float
    fav_short: int = 0        # favourable band, and still did not finish
    adv_short: int = 0        # adverse band, and still did not finish
    unpriced: int = 0         # no price or no fx - contribute NOTHING

    @property
    def completion(self) -> Optional[float]:
        return _completion(self.executed_usd, self.ordered_usd)


class Totals(NamedTuple):
    orders: int
    ordered_usd: float
    executed_usd: float
    fav_short: int = 0
    adv_short: int = 0
    unpriced: int = 0

    @property
    def completion(self) -> Optional[float]:
        return _completion(self.executed_usd, self.ordered_usd)


def by_region(lines) -> list:
    """One Row per region, always all eight, always in REGIONS order.

    An order's quantity and its fills are counted into the SAME region - the
    one its own sym says - so a row's two halves are always the same orders'.
    """
    n = {c: 0 for c in REGION_CODES}
    ordered = {c: 0.0 for c in REGION_CODES}
    made = {c: 0.0 for c in REGION_CODES}
    fav = {c: 0 for c in REGION_CODES}
    adv = {c: 0 for c in REGION_CODES}
    nopx = {c: 0 for c in REGION_CODES}
    for ln in lines:
        c = ln.o.region
        n[c] += 1
        ordered[c] += ln.ordered_usd
        made[c] += ln.executed_usd
        if not ln.priced:
            nopx[c] += 1
        if ln.incomplete and ln.favourable is True:
            fav[c] += 1
        elif ln.incomplete and ln.favourable is False:
            adv[c] += 1
    return [Row(r.code, r.name, n[r.code], ordered[r.code], made[r.code],
                fav[r.code], adv[r.code], nopx[r.code]) for r in REGIONS]


def totals(rows) -> Totals:
    """The headline.  Completion is summed executed over summed order qty, so
    it is the same ratio the rows are, not an average of percentages."""
    return Totals(sum(r.orders for r in rows),
                  sum(r.ordered_usd for r in rows),
                  sum(r.executed_usd for r in rows),
                  sum(r.fav_short for r in rows),
                  sum(r.adv_short for r in rows),
                  sum(r.unpriced for r in rows))


def shared_ids(orders) -> tuple:
    """(targets carrying no 9604, targets sharing one with another target).

    Not acted on: it is the size of what chaining would change, printed so
    nobody reads these numbers as order counts when they are send counts.
    """
    seen = {}
    for o in orders:
        if not o.client_id:
            continue
        k = (o.date, o.client_id)
        seen[k] = seen.get(k, 0) + 1
    no_id = sum(1 for o in orders if not o.client_id)
    shared = sum(v for v in seen.values() if v > 1)
    return no_id, shared


# =============================================================================
# THE PAGE
# =============================================================================

TITLE = "Orders at a Limit"

Y_TITLE, Y_SUBTITLE, Y_RULE_TOP = 0.955, 0.931, 0.9185
Y_RULE_BOTTOM, Y_FOOTER = 0.066, 0.048

#  "Short" is the trading sense of the word - the order came up short of
#  what it asked for - and it counts ORDERS, not quantity.  Favourable and
#  adverse sit next to each other because neither is the whole story: the
#  first is the one we could have traded into, the second is still a
#  question whenever the order was marketable anyway.
REGION_COLS = (
    ("Region", 0.15, False),
    ("Orders", 0.08, True),
    ("Notional Ordered (USD)", 0.20, True),
    ("Notional Executed (USD)", 0.21, True),
    ("Completion", 0.12, True),
    ("Short, fav.", 0.12, True),
    ("Short, adv.", 0.12, True),
)


def _row_cells(r):
    return [(r.name, INK, "normal"),
            (fmt_int(r.orders), INK if r.orders else INK3, "normal"),
            (fmt_usd(r.ordered_usd), INK if r.orders else INK3, "normal"),
            (fmt_usd(r.executed_usd), INK if r.orders else INK3, "normal"),
            (fmt_pct1(r.completion), INK, "bold"),
            (fmt_int(r.fav_short), RED if r.fav_short else INK3,
             "bold" if r.fav_short else "normal"),
            (fmt_int(r.adv_short), INK if r.adv_short else INK3, "normal")]


def draw(rows, tot, subtitle, foot, note=""):
    """The one page: what was asked for and what got done, by region."""
    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)

    kpis(fig, [(fmt_int(tot.orders), "Orders at a limit", INK),
               (fmt_pct1(tot.completion), "Overall completion", GREEN)],
         0.860, 0.836)

    fig.text(L, 0.788, "Orders whose stock was limit up OR limit down while "
                       "the order was live. Both sides count: an unfavourable "
                       "limit can still be marketable.",
             fontsize=8, color=INK2, va="baseline")
    fig.text(L, 0.772,
             "Notional is USD. Ordered is valued at the order's own limit "
             "price, else the quote its first child met, else the previous "
             "close.",
             fontsize=8, color=INK2, va="baseline")
    fig.text(L, 0.756,
             "Executed is what the fills really paid, so completion is the "
             "notional one — not the share completion, which is a different "
             "number.",
             fontsize=8, color=INK2, va="baseline")

    y = table(fig, REGION_COLS, [_row_cells(r) for r in rows], 0.730, 0.030,
              fs=9, head_fs=8.5)
    #  the total sits under the rows, on the same column edges, with no second
    #  header band over it
    fig.text(L, y - 0.026, "Total", fontsize=9, fontweight="bold", color=INK,
             va="baseline")
    _total_line(fig, tot, y - 0.026)

    fig.text(L, y - 0.062,
             "Short, fav. — orders that did not fully execute while the "
             "band was on the side we could trade into (selling into a limit "
             "up, buying into a limit down).",
             fontsize=7.5, color=INK2, va="baseline")
    fig.text(L, y - 0.080,
             "Short, adv. — the same on the other side. Not an excuse: a "
             "market order is marketable into a band either way. Orders the "
             "book could not call are in neither.",
             fontsize=7.5, color=INK2, va="baseline")
    if note:
        fig.text(L, y - 0.104, note, fontsize=7.5, color=INK3,
                 va="baseline")

    footer(fig, foot, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def _total_line(fig, tot, y):
    """The total row, drawn on REGION_COLS' own edges so it cannot drift out of
    line with the table above it."""
    from lib.report_page import R
    x = L
    for (label, frac, right), text in zip(
            REGION_COLS,
            ["", fmt_int(tot.orders), fmt_usd(tot.ordered_usd),
             fmt_usd(tot.executed_usd), fmt_pct1(tot.completion),
             fmt_int(tot.fav_short), fmt_int(tot.adv_short)]):
        w = frac * (R - L)
        if text:
            fig.text(x + w - 0.008, y, text, ha="right", va="baseline",
                     fontsize=9, fontweight="bold", color=INK)
        x += w


#  The listing.  Chosen for someone who has to decide whether a line is a
#  problem: what it was, how much of it got done, what the book was doing,
#  and how long the two overlapped.  Everything else is in --raw.
ORDER_LIST_COLS = (
    ("Region", 0.09, False),
    ("Symbol", 0.11, False),
    ("Target id", 0.08, True),
    ("Side", 0.045, False),
    ("Type", 0.055, False),
    ("Ordered (USD)", 0.115, True),
    ("Executed (USD)", 0.12, True),
    ("Completion", 0.09, True),
    ("Limit", 0.065, False),
    ("Limit window", 0.13, False),
    ("Mins", 0.05, True),
    ("Splits", 0.05, True),
)

ROWS_PER_LIST_PAGE = 28


def _list_cells(ln):
    w = ln.periods
    fav = ln.favourable
    #  the two that matter are read together: how little got done, and
    #  whether the band was on our side while it did not
    comp_ink = RED if (ln.incomplete and fav is True) else INK
    return [
        (REGION_NAME[ln.o.region], INK, "normal"),
        (ln.o.sym, INK, "normal"),
        #  an id, not a quantity: no thousands separator, because 1,105 is
        #  not what anyone types into a query
        (str(ln.o.id_target), INK2, "normal"),
        (ln.o.side or DASH, INK, "normal"),
        (ln.o.otype or DASH, INK2, "normal"),
        (fmt_usd(ln.ordered_usd), INK, "normal"),
        (fmt_usd(ln.executed_usd), INK, "normal"),
        (fmt_pct1(ln.completion), comp_ink, "bold"),
        (_dir_label(ln), INK if fav is True else INK2, "normal"),
        (f"{fmt_hm(w[0].start)}–{fmt_hm(w[-1].end)}" if w else DASH,
         INK2, "normal"),
        (f"{overlap_mins(ln.o, w):.0f}", INK, "normal"),
        (fmt_int(ln.sp.n), INK if ln.sp.n else RED,
         "normal" if ln.sp.n else "bold"),
    ]


def _dir_label(ln) -> str:
    """up / down, and a star when that was the side we could trade into.

    One column rather than two: the direction alone does not say whether it
    was ours to take without the side beside it, and a reader should not
    have to do that join by eye on every row.
    """
    if not ln.periods:
        return DASH
    return ln.direction + (" *" if ln.favourable is True else "")


def list_order(lines) -> list:
    """Worst first: the ones we could have traded into and did not
    finish, biggest quantity missed at the top."""
    return sorted(lines,
                  key=lambda ln: (ln.incomplete and ln.favourable is True,
                                  ln.unfilled, ln.o.size),
                  reverse=True)


def draw_orders(lines, subtitle, foot, page=1, pages=1):
    """One page of the order listing."""
    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)
    head = "Every order at a limit"
    if pages > 1:
        head += f"   ({page} of {pages})"
    fig.text(L, 0.893, head, fontsize=12, fontweight="bold", color=INK,
             va="baseline")
    fig.text(L, 0.874,
             "Sorted by quantity missed, the ones we could have traded into "
             "first — a star on the limit marks those.",
             fontsize=8, color=INK2, va="baseline")
    fig.text(L, 0.858,
             "Mins is how long the order and the limit overlapped. Splits is "
             "how many children the order sent: zero means it never tried.",
             fontsize=8, color=INK2, va="baseline")
    table(fig, ORDER_LIST_COLS, [_list_cells(ln) for ln in lines], 0.833,
          0.0245, fs=7.5, head_fs=7.5)
    footer(fig, foot, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def pages_for(rows, tot, subtitle, foot, note="", lines=()):
    """The summary, then every order that made it."""
    out = [draw(rows, tot, subtitle, foot, note)]
    got = list_order(lines)
    n = max(1, -(-len(got) // ROWS_PER_LIST_PAGE)) if got else 0
    for i in range(n):
        chunk = got[i * ROWS_PER_LIST_PAGE:(i + 1) * ROWS_PER_LIST_PAGE]
        out.append(draw_orders(chunk, subtitle, foot, i + 1, n))
    return out


# =============================================================================
# CSV
# =============================================================================

CSV_HEADER = ("region", "orders", "notional_ordered_usd",
              "notional_executed_usd", "completion_pct",
              "short_favourable", "short_adverse", "unpriced_orders")


def csv_rows(rows, tot) -> list:
    """The table, as data.  The SAME Rows the page draws, so the two cannot
    disagree - a CSV re-derived from the source would be a second answer to
    keep in step."""
    def one(name, r):
        #  full precision here, not the page's 79.2m: this is the file
        #  somebody adds up
        return [name, r.orders, round(r.ordered_usd, 2),
                round(r.executed_usd, 2),
                "" if r.completion is None else f"{r.completion:.1f}",
                r.fav_short, r.adv_short, r.unpriced]
    out = [one(r.name, r) for r in rows]
    out.append(one("Total", tot))
    return out


def write_csv(rows, tot, out_dir, stem) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stem}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        w.writerows(csv_rows(rows, tot))
    log(f"  wrote {path}")
    return path


# =============================================================================
# RAW
#
# The lines the page is made of.  Not a second answer: summing order_qty and
# executed over this file reproduces the table exactly, and a check holds it to
# that.  One row per ORDER, the same unit the page counts - a row per limit
# period would read more raw and add up to more than the report.
# =============================================================================

#  Three blocks, left to right: OURS, then the MARKET's, then the two put
#  together.  A reader going along a line meets what we did first, what the
#  book was doing second, and only then anything that needed both - and a
#  column's block says which side to go and check when it looks wrong.
ORDER_COLS = (
    "date", "region", "sym", "side", "otype", "id_server", "id_target",  # noqa: E501
    #  order_qty and executed stay in SHARES; the money rides beside them
    "tag_9604", "basket", "order_qty", "executed", "completion_pct",
    "ordered_usd", "executed_usd", "notional_completion_pct", "price",
    "price_source", "fxlast", "t_gen", "order_start", "order_end", "splits",
    "split_first_gen", "split_last_off",
)

LIMIT_COLS = (
    "limit_periods", "limit_first_start", "limit_last_end", "limit_mins",
    "limit_price", "ref_close", "pct_from_close", "limit_dir", "limit_noask",
    "limit_nobid", "limit_net", "limit_locked",
)

#  the only column that needs both, and the one the next step is about
BOTH_COLS = ("overlap_mins",)

RAW_HEADER = ORDER_COLS + LIMIT_COLS + BOTH_COLS


def line_direction(periods, ref: float = 0.0) -> str:
    """One direction for a line that may cover several periods.

    `mixed` when they disagree - a stock that was limit down in the morning and
    limit up in the afternoon is not one or the other, and picking the first
    would quietly say it was.
    """
    if not periods:
        return ""
    got = {w.direction(ref) for w in periods}
    if len(got) == 1:
        return got.pop()
    return "mixed"


def fmt_usd(v) -> str:
    """A USD notional, in the unit the number is actually in.

    A trading day runs from thousands to hundreds of millions, and
    79,241,883 is a number nobody reads - 79.2m is.  Zero prints as a dash:
    it means the orders could not be valued, not that they were worth
    nothing.  Same formatter as short_sell_report, deliberately.
    """
    v = float(v or 0.0)
    if v <= 0:
        return DASH
    for cut, suf in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if v >= cut:
            return f"{v / cut:,.1f}{suf}"
    return f"{v:,.0f}"


def _pct(v) -> str:
    return "" if v is None else f"{v:.2f}"


def _hms(ms) -> str:
    """A time as HH:MM:SS - what someone types back into a q query."""
    if ms is None:
        return ""
    ms = max(0, int(ms))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}"


def overlap_mins(o, periods) -> float:
    """How long the order and the limit actually coexisted, in minutes.

    Summed over the periods it touched, each clipped to the order's own window.
    An order still open is taken to the end of the day, the same way `overlap`
    treats it - a missing bound cannot shorten what it cannot rule out.
    """
    lo = 0 if o.t_start is None else o.t_start
    hi = 24 * 3_600_000 if o.t_end is None else o.t_end
    total = 0
    for w in periods:
        total += max(0, min(hi, w.end) - max(lo, w.start))
    return total / 60_000.0


def raw_rows(lines) -> list:
    """One row per order in scope, in the order the report counted them."""
    out = []
    for ln in lines:
        o, sp, got = ln.o, ln.sp, ln.periods
        ex = ln.executed
        comp = ln.share_completion
        out.append([
            #  ORDER_COLS - what we did
            o.date.isoformat() if o.date else "",
            REGION_NAME[o.region], o.sym, o.side, o.otype, o.key[1],
            o.id_target, o.client_id, o.basket, o.size, ex,
            "" if comp is None else f"{comp:.1f}",
            round(ln.ordered_usd, 2) if ln.ordered_usd else "",
            round(ln.executed_usd, 2) if ln.executed_usd else "",
            _pct(ln.completion), f"{ln.price:g}" if ln.price else "",
            ln.price_source, f"{o.fx:g}" if o.fx else "",
            _hms(o.t_gen), _hms(o.t_start), _hms(o.t_end),
            sp.n, _hms(sp.first_gen), _hms(sp.last_off),
            #  LIMIT_COLS - what the book was doing
            len(got),
            _hms(got[0].start) if got else "",
            _hms(got[-1].end) if got else "",
            f"{sum(w.minutes for w in got):.1f}" if got else "",
            f"{got[0].price:g}" if got else "",
            f"{o.ref:g}" if o.ref else "",
            _pct(pct_from_close(got[0].price, o.ref)) if got else "",
            ln.direction,
            sum(w.noask for w in got), sum(w.nobid for w in got),
            f"{got[0].net:g}" if got and got[0].net else "",
            "yes" if got and all(w.locked for w in got) else "no",
            #  BOTH_COLS - what needed the two of them
            f"{overlap_mins(o, got):.1f}",
        ])
    return out


def write_raw(lines, out_dir, stem) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stem}_raw.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(RAW_HEADER)
        w.writerows(raw_rows(lines))
    log(f"  wrote {path}")
    return path


# =============================================================================
# PLAN
# =============================================================================

class Plan(NamedTuple):
    hist: bool
    monthly: bool
    order_server: str
    qatt_server: str
    server_names: tuple
    dates: list
    stem: str
    subtitle: str


def month_dates(year, month) -> list:
    n = calendar.monthrange(year, month)[1]
    return [dt.date(year, month, d) for d in range(1, n + 1)
            if dt.date(year, month, d).weekday() < 5]


def parse_month(s: str) -> tuple:
    y, _, mo = s.partition("-")
    if not (y.isdigit() and mo.isdigit()):
        raise SystemExit(f"--monthly wants YYYY-MM, got {s!r}")
    return int(y), int(mo)


def plan(monthly, date, now=None) -> Plan:
    now = now or dt.datetime.now()
    hist = monthly is not None or date is not None
    order = ORDER_SERVER_HIST if hist else ORDER_SERVER_RT
    qatt = QATT_SERVER_HIST if hist else QATT_SERVER_RT
    names = (("ORDER_SERVER_HIST", "QATT_SERVER_HIST") if hist
             else ("ORDER_SERVER_RT", "QATT_SERVER_RT"))
    if monthly is not None:
        y, mo = parse_month(monthly)
        return Plan(True, True, order, qatt, names, month_dates(y, mo),
                    f"luld_orders_{y:04d}-{mo:02d}",
                    f"By region  ·  {calendar.month_name[mo]} {y}")
    if date is not None:
        return Plan(True, False, order, qatt, names, [date],
                    f"luld_orders_{date:%Y-%m-%d}", f"By region  ·  {date}")
    return Plan(False, False, order, qatt, names, [None],
                f"luld_orders_{now:%Y-%m-%d}",
                f"By region  ·  {now:%Y-%m-%d %H:%M}")


# =============================================================================
# RUN
# =============================================================================

def run(args) -> int:
    pl = plan(args.monthly, args.date)
    _check_server(pl.order_server, pl.server_names[0])
    _check_server(pl.qatt_server, pl.server_names[1])

    log(f"luld_orders  {'historical' if pl.hist else 'realtime'}  "
        f"orders {pl.order_server}  quotes {pl.qatt_server}")
    oh = connect(pl.order_server)
    qh = connect(pl.qatt_server)

    orders, executed, hits = [], {}, {}
    seen, cancelled, no_limit_days = 0, 0, 0
    for d in pl.dates:
        if not args.quiet and d is not None:
            log(f"  {d} ...")
        pr, wr, sr = fetch_orders(oh, pl.hist, d)
        day = to_orders(pr)
        if not day:
            continue
        seen += len(day)
        day, dead = drop_dead_on_arrival(day, last_state_by_order(sr, day))
        cancelled += len(dead)
        if not day:
            continue
        syms = sorted({o.sym for o in day})
        lims = to_limits(fetch_limits(qh, pl.hist, d, syms), d, args.min_mins)
        if not lims:
            #  a day with no limit period anywhere is possible; a run of them
            #  means the quote query is matching nothing rather than the market
            #  being calm
            no_limit_days += 1
            continue
        kept, day_hits = touched(day, lims)
        if not kept:
            continue
        orders.extend(kept)
        hits.update(day_hits)
        executed.update(splits_by_order(wr, kept))

    lines = to_lines(orders, executed, hits)
    rows = by_region(lines)
    tot = totals(rows)

    if tot.unpriced:
        log(f"  {tot.unpriced:,} of {tot.orders:,} orders could not be valued "
            f"- no price or no fxlast - and contribute NOTHING to the notional "
            f"columns")
    if cancelled:
        log(f"  {cancelled:,} of {seen:,} orders were over before their own "
            f"t_start - cancelled before they worked - and are out")
    log(f"  {seen - cancelled:,} orders in scope, {tot.orders:,} of them were "
        f"live while their stock was at a limit")
    if seen and not tot.orders:
        log(f"  WARNING: {seen:,} orders were in scope and NOT ONE was live "
            f"through a limit period. Check {pl.qatt_server} has the syms, and "
            f"that --min-mins {args.min_mins:g} is not filtering them all out.")
    if no_limit_days:
        log(f"  {no_limit_days} of {len(pl.dates)} days had no limit period at "
            f"all")
    no_id, shared = shared_ids(orders)
    note = ""
    if shared or no_id:
        log(f"  NOT CHAINED: {shared:,} of {tot.orders:,} targets share a "
            f"{CLIENT_ID_TAG} with another, {no_id:,} carry none. A re-sent "
            f"order is counted once per send.")
        note = (f"Not chained: {shared:,} of {tot.orders:,} targets share a "
                f"FIX {CLIENT_ID_TAG} with another, so a re-sent order is "
                f"counted once per send.")

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    figs = pages_for(rows, tot, pl.subtitle, f"Generated {stamp}", note,
                     lines)
    if len(figs) > 2:
        log(f"  the order listing runs to {len(figs) - 1} pages")
    save(figs, args.out_dir, pl.stem, dpi=DPI)
    if args.csv:
        write_csv(rows, tot, args.out_dir, pl.stem)
    if args.raw:
        write_raw(lines, args.out_dir, pl.stem)
    return 0


# =============================================================================
# DEMO - synthetic, deterministic, no kdb
# =============================================================================

def _t(idt, region, size, d=None, srv=1, sym=None, side="sell",
       otype="limit", basket="B1", limit_price=0.0, fxlast=1.0,
       t_gen=9 * 3_600_000, t_start=9 * 3_600_000 + 1_800_000,
       t_end=15 * 3_600_000, cid=None, adjclose=100.0, orgclose=None):
    """One target row.  cid goes into fixmsg as tag 9604 the way the client
    really sends it, so the fixture exercises the parse too."""
    sfx = dict((r.code, r.suffixes[0]) for r in REGIONS).get(region,
                                                             "." + region)
    cid = f"CLI-{idt}" if cid is None else cid
    fix = "8=FIX.4.2;35=D;9012=274=1^275=1;"
    if cid:
        fix += f"{CLIENT_ID_TAG}={cid};"
    return {"date": d, "id_server": srv, "id_target": idt,
            "sym": sym or f"{1000 + idt}{sfx}", "side": side,
            "sidesign": -1 if side == "sell" else 1, "size": size,
            "otype": otype, "basket": basket, "limit_price": limit_price,
            "fxlast": fxlast,
            #  None is a real value here: a target still working has no t_end
            "t_gen": _td(t_gen),
            "t_start": _td(t_start), "t_end": _td(t_end),
            "fixmsg": fix + "59=0",
            "adjclose": adjclose, "orgclose": orgclose}


def _td(ms):
    return None if ms is None else dt.timedelta(milliseconds=ms)


def _wo(idw, idt, make, d=None, srv=1, gen=None, off=None, fill=0.0,
        tx=None, bid=0.0, ask=0.0):
    """gen is when the split was CREATED, off when it left the book.  Both
    are None on a split that never reached the market, which is a real row.
    """
    return {"date": d, "id_server": srv, "id_work": idw, "id_target": idt,
            "make": make, "t_gen": _td(gen), "t_off_market": _td(off),
            "avg_fill_price": fill, "t_transmit": _td(tx),
            "transmit_bidprice": bid, "transmit_askprice": ask}


def _ts(idt, at, state="cancelled", d=None, srv=1):
    """One target_state row - one thing that happened to the order."""
    return {"date": d, "id_server": srv, "id_target": idt,
            "time": _td(at), "state": state}


def _lim(sym, start, end, price=100.0, ticks=50, d=None, noask=50, nobid=0,
         net=0.0):
    """Default is a limit UP: the ask is the side that went missing.

    noask=nobid is a LOCKED period - bid = ask, neither side away - which is
    what price against the close, and then net, are there to settle.
    """
    return {"sym": sym, "date": d, "start": dt.timedelta(milliseconds=start),
            "end": dt.timedelta(milliseconds=end), "price": price,
            "ticks": ticks, "noask": noask, "nobid": nobid, "net": net}


#  a plausible local -> USD rate per region, so the sample is not eight
#  markets all priced as if they were dollars
FX = {"JP": 0.0067, "KR": 0.00072, "MY": 0.22, "TH": 0.028, "ID": 0.000062,
      "CN": 0.14, "TW": 0.031, "IN": 0.012}


def demo_session(d=None):
    """(orders, executed, rows, totals) for one made up session."""
    H = 3_600_000
    tr, wr, lims, sr = [], [], [], []
    k = 0
    shape = (("JP", 26, 0.72), ("KR", 19, 0.41), ("CN", 22, 0.55),
             ("TW", 11, 0.63), ("TH", 7, 0.38), ("MY", 5, 0.51),
             ("ID", 4, 0.29), ("IN", 9, 0.66))
    for region, n, fill in shape:
        for _i2 in range(n):
            k += 1
            size = 20_000 + ((k * 7919) % 400) * 500
            px = 10.0 + (k % 400) / 4.0
            #  the band, and whether we were on the side that could trade
            #  into it - both, so the page has each to show
            up, down = ((50, 0), (0, 50), (0, 0))[k % 3]
            #  the close sits below the band here, so a LOCKED run reads as up
            went_up = up >= down
            fav = k % 2 == 0
            side = ("sell" if fav else "buy") if went_up else (
                "buy" if fav else "sell")
            #  market orders among them: marketable into a limit whichever way
            #  the band went, which is why otype is on the line
            otype = "market" if k % 4 == 0 else "limit"
            #  a limit order is worth what the client said; a market order
            #  has to be valued off the quote its first child met
            tr.append(_t(k, region, size, d=d, side=side, otype=otype,
                         limit_price=px if otype == "limit" else 0.0,
                         adjclose=px * 0.9, fxlast=FX.get(region, 1.0)))
            sym = tr[-1]["sym"]
            start = 11 * H + (k % 90) * 60_000
            end = start + (25 + (k % 40)) * 60_000     # all over --min-mins
            #  a spread of directions, including LOCKED runs the book cannot
            #  call from the sides alone - all stay in scope, which is the point
            lims.append(_lim(sym, start, end, price=px, d=d, noask=up,
                             nobid=down))
            #  one to three children, created inside the limit and coming
            #  off the book after it - the times the raw file reports
            done = int(size * fill)
            n_sp = 1 + (k % 3)
            for j in range(n_sp):
                wr.append(_wo(1000 * k + j, k, done // n_sp, d=d,
                              gen=start + j * 60_000,
                              off=start + (j + 1) * 300_000,
                              fill=px * 0.999, tx=start + j * 60_000,
                              bid=px * 0.999, ask=px * 1.001))
    #  an order CANCELLED BEFORE IT STARTED: its last state row is earlier
    #  than its own t_start, so it never worked and is not one of ours
    k += 1
    tr.append(_t(k, "JP", 500_000, d=d, t_gen=9 * H, t_start=10 * H))
    sr.append(_ts(k, 9 * H + 60_000, "cancelled", d=d))
    lims.append(_lim(tr[-1]["sym"], 11 * H, 12 * H, d=d))
    #  an order that sat through a limit and NEVER SENT A CHILD.  0 splits,
    #  no times, and the case this is all being built towards
    k += 1
    tr.append(_t(k, "TW", 75_000, d=d))
    lims.append(_lim(tr[-1]["sym"], 11 * H, 12 * H, d=d))
    #  a LOCKED stock with no close on file, which stays unknown
    #  in scope anyway - unknown is an answer, not a reason to drop an order
    k += 1
    tr.append(_t(k, "JP", 120_000, d=d, adjclose=None, orgclose=None))
    lims.append(_lim(tr[-1]["sym"], 11 * H, 12 * H, d=d, noask=0, nobid=0,
                     net=0.0))
    wr.append(_wo(k, k, 60_000, d=d, gen=11 * H + 60_000, off=12 * H))
    #  one stock that only blipped: under the minimum, so its order is out
    k += 1
    tr.append(_t(k, "JP", 99_000, d=d))
    lims.append(_lim(tr[-1]["sym"], 12 * H, 12 * H + 120_000, d=d))
    wr.append(_wo(k, k, 99_000, d=d))
    #  one order that finished before its stock ever went to the limit
    k += 1
    tr.append(_t(k, "KR", 88_000, d=d, t_start=9 * H, t_end=10 * H))
    lims.append(_lim(tr[-1]["sym"], 13 * H, 14 * H, d=d))
    wr.append(_wo(k, k, 88_000, d=d))

    orders = to_orders(tr)
    orders, _dead = drop_dead_on_arrival(orders,
                                         last_state_by_order(sr, orders))
    kept, hits = touched(orders, to_limits(lims, d))
    ex = splits_by_order(wr, kept)
    lines = to_lines(kept, ex, hits)
    rows = by_region(lines)
    return lines, rows, totals(rows)


def demo(out_dir, want_csv=True) -> int:
    lines, rows, tot = demo_session()
    figs = pages_for(rows, tot, "By region  ·  SAMPLE",
                     "SAMPLE - synthetic data, not from kdb", "", lines)
    save(figs, out_dir, "luld_orders_SAMPLE", dpi=DPI)
    if want_csv:
        write_csv(rows, tot, out_dir, "luld_orders_SAMPLE")
        write_raw(lines, out_dir, "luld_orders_SAMPLE")
    log("  these are made up numbers - do not circulate them as a report")
    return 0


# =============================================================================
# SELF TEST - runs with no kdb, no pykx and no q licence
# =============================================================================

def self_test() -> int:
    import io
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    H = 3_600_000
    print("luld_orders --self-test\n")

    print("the q, without a q")
    for nm, src, args in (("Q_ORDERS", Q_ORDERS, ("sfx",)),
                          ("Q_LIMITS", Q_LIMITS, ("syms",))):
        check(f"{nm}: no q reserved word as a name", reserved_used(src), [])
        check(f"{nm}: brackets balance", balanced(src), True)
    check("the queries group nothing - Python does the sums",
          groups_in_q(Q_ORDERS), [])
    check("symbol args are cast or used with like",
          uncast_symbols(Q_LIMITS, ("syms",)), [])
    check("the reserved word check would still catch one",
          reserved_used("{[d;ss] ss:1}"), ["ss"])

    print("\nwhich stocks are ours")
    check("Japan", region_of("7203.JP"), "JP")
    check("Korea", region_of("005930.KS"), "KR")
    check("China takes three suffixes",
          [region_of(s) for s in ("600519.CH", "000001.C1", "300750.C2")],
          ["CN", "CN", "CN"])
    check("India takes two",
          [region_of(s) for s in ("RELIANCE.IN", "TCS.IS")], ["IN", "IN"])
    check("Hong Kong has no daily limit and is out of scope",
          region_of("0700.HK"), None)
    check("so is anything with no suffix at all", region_of("AAPL"), None)
    check("the q patterns come from the same table",
          sorted(SYM_PATTERNS)[:3], ["*.C1", "*.C2", "*.CH"])

    print("\na limit period has to last")
    lims = to_limits([_lim("7203.JP", 11 * H, 11 * H + 1_200_000),
                      _lim("6103.JP", 11 * H, 11 * H + 60_000)],
                     min_mins=20.0)
    check("twenty minutes counts", [w.sym for w in lims], ["7203.JP"])
    check("one minute is a print, not a period", len(lims), 1)
    check("and the minimum is a setting, not a constant",
          len(to_limits([_lim("6103.JP", 11 * H, 11 * H + 60_000)],
                        min_mins=0.5)), 1)
    check("a period with no width at all is not one",
          len(to_limits([_lim("6103.JP", 11 * H, 11 * H)], min_mins=0.0)), 0)
    check("minutes are the window, not the tick count",
          to_limits([_lim("7203.JP", 11 * H, 11 * H + 1_800_000)],
                    min_mins=20.0)[0].minutes, 30.0)

    print("\nover before it began")
    #  an order cancelled before its own t_start never worked, so counting
    #  it as one that sat through a limit is counting a false positive
    dd = to_orders([_t(1, "JP", 1000, t_gen=9 * H, t_start=10 * H),
                    _t(2, "JP", 1000, t_gen=9 * H, t_start=10 * H)])
    st = last_state_by_order([_ts(1, 9 * H + 60_000, "cancelled"),
                              _ts(2, 14 * H, "done")], dd)
    check("the last state row is the last one by TIME, not by arrival",
          st[dd[1].key].at, 14 * H)
    check("and it carries what it was", st[dd[1].key].state, "done")
    keep, dead = drop_dead_on_arrival(dd, st)
    check("an order whose life ended before it began is out",
          [o.id_target for o in dead], [1])
    check("one that outlived its start is in",
          [o.id_target for o in keep], [2])
    #  the boundary: a state row AT t_start is not before it
    at = to_orders([_t(1, "JP", 1000, t_start=10 * H)])
    check("exactly at t_start is not before it",
          drop_dead_on_arrival(at, last_state_by_order(
              [_ts(1, 10 * H, "cancelled")], at))[1], [])
    #  never dropped on missing evidence
    check("no state row at all cannot answer the question, so it stays",
          len(drop_dead_on_arrival(at, {})[0]), 1)
    check("nor can a state row with no time",
          len(drop_dead_on_arrival(at, last_state_by_order(
              [_ts(1, None, "cancelled")], at))[0]), 1)
    nos = to_orders([_t(1, "JP", 1000, t_start=None)])
    check("nor an order with no start time to compare against",
          len(drop_dead_on_arrival(nos, last_state_by_order(
              [_ts(1, 10 * H, "cancelled")], nos))[0]), 1)
    check("a state row belonging to another order is not evidence here",
          last_state_by_order([_ts(99, 9 * H, "cancelled")], at), {})

    print("\nwhich orders the limit touched")
    ords = to_orders([_t(1, "JP", 1000, t_start=9 * H, t_end=15 * H),
                      _t(2, "JP", 1000, t_start=9 * H, t_end=10 * H)])
    ws = to_limits([_lim(ords[0].sym, 11 * H, 12 * H),
                    _lim(ords[1].sym, 11 * H, 12 * H)])
    kept, hits = touched(ords, ws)
    check("an order live through the limit is in",
          [o.id_target for o in kept], [1])
    check("one that finished before it is not", len(kept), 1)
    check("and the period is carried with it", len(hits[kept[0].key]), 1)
    check("a stock with no limit period brings nothing",
          len(touched(ords, [])[0]), 0)
    check("an order still open cannot be ruled out by its missing end",
          overlap(9 * H, None, 13 * H, 14 * H), True)
    check("nor one with no start", overlap(None, 15 * H, 9 * H, 10 * H), True)
    check("touching at the edge only is not an overlap",
          overlap(9 * H, 11 * H, 11 * H, 12 * H), False)

    print("\nboth sides count")
    two = to_orders([_t(1, "JP", 1000, side="sell", t_start=9 * H,
                        t_end=15 * H),
                     _t(2, "JP", 1000, side="buy", t_start=9 * H,
                        t_end=15 * H)])
    tw = to_limits([_lim(two[0].sym, 11 * H, 12 * H),
                    _lim(two[1].sym, 11 * H, 12 * H)])
    check("a seller and a buyer are both in scope, whatever the band was",
          len(touched(two, tw)[0]), 2)

    print("\nthe rollup")
    ro = to_orders([_t(1, "JP", 1000), _t(2, "JP", 3000), _t(3, "KR", 2000)])
    ex = splits_by_order([_wo(11, 1, 400, fill=50.0),
                          _wo(12, 1, 300, fill=50.0),
                          _wo(13, 3, 500, fill=50.0)], ro)
    check("a target's children are added up", ex[ro[0].key].made, 700)
    check("and counted", ex[ro[0].key].n, 2)
    rol = to_lines(ro, ex, {})
    rows = {r.code: r for r in by_region(rol)}
    check("orders are counted per region", rows["JP"].orders, 2)
    check("notional ordered is summed per region",
          rows["JP"].ordered_usd, 4000 * 100.0)
    check("and so is what the fills really paid",
          rows["JP"].executed_usd, 700 * 50.0)
    check("completion is the NOTIONAL one, off the columns beside it",
          round(rows["JP"].completion, 4),
          round(100.0 * (700 * 50.0) / (4000 * 100.0), 4))
    check("a region with no order shows no percentage, not 0%",
          rows["TH"].completion, None)
    check("all eight regions are always there", len(by_region(rol)), 8)
    check("and always in the same order",
          [r.code for r in by_region(rol)], list(REGION_CODES))
    tot = totals(by_region(rol))
    check("the total is the sum of the rows", (tot.orders, tot.ordered_usd),
          (3, 6000 * 100.0))
    check("and its completion is notional weighted, not a mean of the rows",
          round(tot.completion, 4),
          round(100.0 * tot.executed_usd / tot.ordered_usd, 4))

    print("\nfills cannot land in a region with no order")
    #  the fault that made the old report print Korea 161.9%: quantity counted
    #  off one grouping, fills off another
    mix = to_orders([_t(1, "JP", 1000)])
    mex = splits_by_order([_wo(11, 1, 500, fill=10.0),
                           _wo(12, 99, 5000, fill=10.0)], mix)
    mrows = by_region(to_lines(mix, mex, {}))
    check("a workorder whose parent is out of scope is dropped",
          sum(r.executed_usd for r in mrows), 5000.0)
    check("no region executes what it had no order for",
          [r.code for r in mrows if r.executed_usd and not r.orders], [])
    check("and no row completes more than it asked for",
          [r.code for r in mrows
           if r.completion is not None and r.completion > 100.0], [])

    print("\nnot chained, and saying so")
    sh = to_orders([_t(1, "JP", 100, cid="ONE"), _t(2, "JP", 100, cid="ONE"),
                    _t(3, "JP", 100, cid="OTHER"), _t(4, "JP", 100, cid="")])
    check("targets sharing a 9604 are counted", shared_ids(sh), (1, 2))
    check("a re-sent order is still several orders here", len(sh), 4)
    check("the tag is read off the fixmsg the client really sends",
          sh[0].client_id, "ONE")

    print("\nthe page")
    dlines, drows, dtot = demo_session()
    check("the demo has orders in every region",
          [r.code for r in drows if not r.orders], [])
    #  103 shaped orders, plus the locked one with no close on file and the
    #  one that never sent a child.  OUT: the two-minute blip, the order
    #  that finished before its limit, and the one cancelled before it
    #  started - which sat through a limit and would otherwise be counted
    check("every order that met a limit is counted, and only those",
          dtot.orders, 103 + 2)
    check("the columns add up to the full width",
          round(sum(c[1] for c in REGION_COLS), 6), 1.0)
    check("the page shows what was asked, what was done, and what came short",
          [c[0] for c in REGION_COLS],
          ["Region", "Orders", "Notional Ordered (USD)",
           "Notional Executed (USD)", "Completion", "Short, fav.",
           "Short, adv."])
    check("the listing columns add up to the full width",
          round(sum(c[1] for c in ORDER_LIST_COLS), 6), 1.0)
    tid = ORDER_LIST_COLS.index(("Target id", 0.08, True))
    idcell = _list_cells(to_lines(
        to_orders([_t(1_105_432, "JP", 1000)]), {}, {})[0])[tid][0]
    check("a target id is an id, not a quantity - no thousands separator",
          idcell, "1105432")
    figs = pages_for(drows, dtot, "By region  ·  x", "Generated  ·  x")
    check("the summary alone is one page", len(figs), 1)
    withl = pages_for(drows, dtot, "x", "y", "", dlines)
    check("and the listing paginates behind it",
          len(withl), 1 + -(-dtot.orders // ROWS_PER_LIST_PAGE))
    check("every order in scope is on one of those pages, none dropped",
          len(list_order(dlines)), dtot.orders)
    buf = io.BytesIO()
    figs[0].savefig(buf, format="pdf")
    check("and it renders", buf.getvalue()[:5], b"%PDF-")

    print("\nthe csv")
    cr = csv_rows(drows, dtot)
    check("a line per region, plus the total", len(cr), 9)
    check("the header names every column", len(CSV_HEADER), len(cr[0]))
    check("the last line is the total", cr[-1][0], "Total")
    check("the csv is the SAME numbers the page drew",
          [cr[0][1], cr[0][2], cr[0][3]],
          [drows[0].orders, round(drows[0].ordered_usd, 2),
           round(drows[0].executed_usd, 2)])
    check("and it carries the two short columns too",
          [cr[0][5], cr[0][6]], [drows[0].fav_short, drows[0].adv_short])
    check("full precision in the file, not the page's 79.2m",
          cr[0][2], round(drows[0].ordered_usd, 2))
    check("a percentage with nothing to measure is empty, not 0.0",
          csv_rows([Row("TH", "Thailand", 0, 0.0, 0.0)],
                   Totals(0, 0.0, 0.0))[0][4], "")
    check("the demo has orders on both sides of the band",
          (dtot.fav_short > 0, dtot.adv_short > 0), (True, True))

    print("\nwhich limit it was")
    check("no ask means nobody will offer: limit up", direction_of(60, 0), UP)
    check("no bid means limit down", direction_of(0, 60), DOWN)
    check("it is the run that decides, not the last tick",
          direction_of(59, 1), UP)
    #  a LOCKED book - bid = ask, neither side away - is a real limit, and the
    #  side that went missing cannot answer it because neither did
    check("locked, and the band is above the close: limit up",
          direction_of(0, 0, price=1250.0, ref=1000.0), UP)
    check("locked, and below it: limit down",
          direction_of(0, 0, price=750.0, ref=1000.0), DOWN)
    check("the close outranks netChange, which comes off the last trade",
          direction_of(0, 0, price=750.0, ref=1000.0, net=5.0), DOWN)
    check("netChange only when there is no close on file",
          direction_of(0, 0, price=750.0, ref=0.0, net=5.0), UP)
    check("and negative is down", direction_of(0, 0, net=-5.0), DOWN)
    check("locked with no close and no netChange cannot be called",
          direction_of(0, 0), UNKNOWN)
    check("a price exactly at the close says nothing either",
          direction_of(0, 0, price=1000.0, ref=1000.0), UNKNOWN)
    check("one side missing still wins over the close, whatever it says",
          direction_of(0, 60, price=1250.0, ref=1000.0), DOWN)
    check("a period carries its own direction",
          to_limits([_lim("7203.JP", 11 * H, 12 * H, noask=0,
                          nobid=60)])[0].direction(), DOWN)
    check("and knows whether it was locked",
          [to_limits([_lim("a", 11 * H, 12 * H, noask=0, nobid=0)])[0].locked,
           to_limits([_lim("a", 11 * H, 12 * H, noask=9, nobid=0)])[0].locked],
          [True, False])
    check("how far the band sat from the close",
          round(pct_from_close(1100.0, 1000.0), 2), 10.0)
    check("no close on file is not 0%, it is nothing to measure against",
          pct_from_close(1100.0, 0.0), None)
    check("t_gen is on the order, and is when it was CREATED",
          to_orders([_t(1, "JP", 1, t_gen=9 * H)])[0].t_gen, 9 * H)
    check("the close arrives on the ORDER, adjclose first",
          to_orders([_t(1, "JP", 1, adjclose=12.5, orgclose=99.0)])[0].ref,
          12.5)
    check("orgclose where adjclose is null",
          to_orders([_t(1, "JP", 1, adjclose=None, orgclose=99.0)])[0].ref,
          99.0)
    check("and neither is no reference at all, never a price of zero",
          to_orders([_t(1, "JP", 1, adjclose=None, orgclose=None)])[0].ref,
          0.0)
    #  unknown is REPORTED, never dropped - the old report threw these away
    unk = to_orders([_t(1, "JP", 1000, t_start=9 * H, t_end=15 * H)])
    ul = to_limits([_lim(unk[0].sym, 11 * H, 12 * H, noask=0, nobid=0)])
    check("a period the book cannot call still puts its order in scope",
          len(touched(unk, ul)[0]), 1)
    check("one line, several periods, one answer",
          line_direction(to_limits([_lim("a", 10 * H, 11 * H, noask=9),
                                    _lim("a", 12 * H, 13 * H, noask=9)])), UP)
    check("and `mixed` when they disagree, never the first one silently",
          line_direction(to_limits([_lim("a", 10 * H, 11 * H, noask=9,
                                         nobid=0),
                                    _lim("a", 12 * H, 13 * H, noask=0,
                                         nobid=9)])), "mixed")
    check("a locked line is called with the order's own close",
          line_direction(to_limits([_lim("a", 10 * H, 11 * H, price=1250.0,
                                         noask=0, nobid=0)]), 1000.0), UP)
    check("no period at all is empty, not a direction", line_direction([]), "")

    print("\nthe order type")
    check("it comes off the target as the feed sends it",
          to_orders([_t(1, "JP", 100, otype="market")])[0].otype, "market")
    check("and so does the basket it was sent as part of",
          to_orders([_t(1, "JP", 100, basket="ASIA-OPEN")])[0].basket,
          "ASIA-OPEN")
    check("an order in no basket carries none, not a placeholder",
          to_orders([_t(1, "JP", 100, basket="")])[0].basket, "")
    check("a limit order says so",
          to_orders([_t(1, "JP", 100, otype="limit")])[0].otype, "limit")
    check("an unset one is empty, not guessed at",
          to_orders([_t(1, "JP", 100, otype="")])[0].otype, "")
    check("nothing is filtered on it - a market order and a limit order are "
          "both in scope",
          len(touched(to_orders([_t(1, "JP", 100, otype="market"),
                                 _t(2, "JP", 100, otype="limit")]),
                      to_limits([_lim("1001.JP", 11 * H, 12 * H),
                                 _lim("1002.JP", 11 * H, 12 * H)]))[0]), 2)

    print("\nthe children, counted and timed")
    sp = splits_by_order(
        [_wo(11, 1, 100, gen=10 * H, off=11 * H),
         _wo(12, 1, 200, gen=9 * H, off=13 * H),
         _wo(13, 1, 300, gen=12 * H, off=12 * H + 60_000)],
        to_orders([_t(1, "JP", 1000)]))
    got = sp[(None, 1, 1)]
    check("every child under the target is counted", got.n, 3)
    check("and what they executed is summed", got.made, 600)
    check("the FIRST created, whatever order they arrived in",
          got.first_gen, 9 * H)
    check("and the LAST off the book", got.last_off, 13 * H)
    #  a split that never reached the market has no t_off_market, and
    #  taking that as an end would date the order to midnight
    none = splits_by_order([_wo(11, 1, 0, gen=10 * H, off=None),
                            _wo(12, 1, 50, gen=11 * H, off=12 * H)],
                           to_orders([_t(1, "JP", 1000)]))[(None, 1, 1)]
    check("a missing time cannot move a bound", none.last_off, 12 * H)
    check("but the split still counts as one the engine made", none.n, 2)
    only = splits_by_order([_wo(11, 1, 0, gen=None, off=None)],
                           to_orders([_t(1, "JP", 1000)]))[(None, 1, 1)]
    check("a child with no times at all leaves them empty, not zero",
          (only.first_gen, only.last_off), (None, None))
    check("an order with no children at all executed nothing",
          made_of({}, (None, 1, 1)), 0)

    print("\nthe raw rows")
    rr = raw_rows(dlines)
    check("a line per order in scope, and no more", len(rr), dtot.orders)
    check("the header names every column", len(RAW_HEADER), len(rr[0]))
    check("ours first, the market's second, both last",
          RAW_HEADER, ORDER_COLS + LIMIT_COLS + BOTH_COLS)
    check("nothing of the market's has drifted into ours",
          [c for c in ORDER_COLS if c.startswith(("limit_", "ref_", "pct_"))],
          [])
    check("nor the other way",
          [c for c in LIMIT_COLS
           if c.startswith(("split", "order_", "t_gen", "tag_"))], [])
    check("the basket is ours, and sits with the order",
          [c for c in ORDER_COLS if c == "basket"], ["basket"])
    check("what the order did stays with the order",
          ORDER_COLS[-3:], ("splits", "split_first_gen", "split_last_off"))
    check("and the only column needing both of them is the last one",
          (BOTH_COLS, RAW_HEADER[-1]), (("overlap_mins",), "overlap_mins"))
    qi = RAW_HEADER.index("ordered_usd")
    ei = RAW_HEADER.index("executed_usd")
    #  each line is rounded to the cent, the page rounds the sum, so they
    #  agree to within the rounding and not to the last digit
    check("the raw file adds back up to the page - notional ordered",
          abs(sum(r[qi] or 0 for r in rr) - dtot.ordered_usd) < 1.0, True)
    check("and notional executed",
          abs(sum(r[ei] or 0 for r in rr) - dtot.executed_usd) < 1.0, True)
    ri = RAW_HEADER.index("region")
    check("region by region too",
          abs(sum(r[qi] or 0 for r in rr if r[ri] == "Japan")
              - {x.code: x for x in drows}["JP"].ordered_usd) < 1.0, True)
    check("and the SHARES are still on the line beside the money",
          [RAW_HEADER.index("order_qty") < qi,
           RAW_HEADER.index("executed") < ei], [True, True])
    check("every line names a limit period, or it would not be a line",
          [r for r in rr if not r[RAW_HEADER.index("limit_periods")]], [])
    si = RAW_HEADER.index("splits")
    check("the split count is on the line",
          sorted({r[si] for r in rr}), [0, 1, 2, 3])
    check("an order that never sent one is 0, and still a line",
          len([r for r in rr if r[si] == 0]), 1)
    check("with no times to show for it",
          [r[si + 1] for r in rr if r[si] == 0], [""])
    check("and the ones that did are timed",
          [r for r in rr if r[si] and not r[si + 1]], [])
    oi = RAW_HEADER.index("otype")
    check("every line says what kind of order it was",
          sorted({r[oi] for r in rr}), ["limit", "market"])
    check("and market orders carry their quantity like any other",
          sum(r[qi] for r in rr if r[oi] == "market") > 0, True)
    di = RAW_HEADER.index("limit_dir")
    check("every line says which limit it was, unknown included",
          sorted({r[di] for r in rr}), [DOWN, UNKNOWN, UP])
    check("and none of them is blank",
          [r for r in rr if not r[di]], [])
    check("the tick counts that decided it are on the line too",
          [RAW_HEADER[di + 1], RAW_HEADER[di + 2]],
          ["limit_noask", "limit_nobid"])
    #  the demo's unknown line is the stock with no close on file, so it
    #  cannot be priced either - it keeps its SHARES and contributes no money
    qsh = RAW_HEADER.index("order_qty")
    check("an unknown direction does not cost the line its quantity",
          sum(r[qsh] for r in rr if r[di] == UNKNOWN) > 0, True)
    check("and an order that cannot be valued contributes no notional",
          [r[qi] for r in rr if not r[RAW_HEADER.index("fxlast")]
           or not r[RAW_HEADER.index("price")]], [""])
    check("which the page counts rather than hides", dtot.unpriced, 1)
    #  one order, one period, overlapping by half an hour of the period's hour
    ro = to_orders([_t(1, "JP", 1000, t_start=11 * H + 1_800_000,
                       t_end=15 * H)])
    rw = to_limits([_lim(ro[0].sym, 11 * H, 12 * H)], min_mins=0.0)
    rk, rh = touched(ro, rw)
    one = raw_rows(to_lines(rk, splits_by_order([_wo(9, 1, 250)], rk),
                            rh))[0]
    check("the limit period is 60 minutes",
          one[RAW_HEADER.index("limit_mins")], "60.0")
    check("but the order was only live for the second half of it",
          one[RAW_HEADER.index("overlap_mins")], "30.0")
    check("times are HH:MM:SS, ready to type back into a query",
          one[RAW_HEADER.index("limit_first_start")], "11:00:00")
    check("the order's own t_gen is on the line, beside its window",
          [RAW_HEADER[RAW_HEADER.index("t_gen") + i] for i in (0, 1, 2)],
          ["t_gen", "order_start", "order_end"])
    check("and it is the target's t_gen, not the first child's",
          one[RAW_HEADER.index("t_gen")], "09:00:00")
    check("an order still open overlaps to the end of the day, not to zero",
          overlap_mins(to_orders([_t(1, "JP", 1, t_start=11 * H,
                                     t_end=None)])[0],
                       [Limit("x", None, 23 * H, 24 * H, 1.0, 5)]), 60.0)
    check("a period the order never saw contributes nothing",
          overlap_mins(to_orders([_t(1, "JP", 1, t_start=9 * H,
                                     t_end=10 * H)])[0],
                       [Limit("x", None, 11 * H, 12 * H, 1.0, 5)]), 0.0)

    print("\nthe plan")
    now = dt.datetime(2026, 7, 24, 18, 37)
    check("no flags is realtime", plan(None, None, now).hist, False)
    check("and reads the realtime servers",
          plan(None, None, now).order_server, ORDER_SERVER_RT)
    check("--date is historical", plan(None, dt.date(2026, 7, 1), now).hist,
          True)
    check("and reads the historical ones",
          plan(None, dt.date(2026, 7, 1), now).qatt_server, QATT_SERVER_HIST)
    check("--monthly is every weekday of the month",
          len(plan("2026-07", None, now).dates), 23)
    check("weekends are not sessions",
          [d.weekday() for d in plan("2026-07", None, now).dates
           if d.weekday() > 4], [])
    check("the file is named for what it covers",
          plan("2026-07", None, now).stem, "luld_orders_2026-07")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# =============================================================================
# CLI
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Orders at a Limit - order quantity, executed and "
                    "completion by region, for orders whose stock was limit "
                    "up or limit down while they were live. Servers are "
                    "configured at the top of this file, or in a "
                    "local_settings.py beside it.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--monthly", metavar="YYYY-MM",
                   help="a whole month off the HISTORICAL servers")
    p.add_argument("--date", type=dt.date.fromisoformat, metavar="YYYY-MM-DD",
                   help="one past session off the HISTORICAL servers")
    p.add_argument("--min-mins", type=float, default=MIN_LIMIT_MINS,
                   metavar="N",
                   help="how long a run of locked or one sided quoting has to "
                        "last to count as a limit period")
    p.add_argument("--csv", action="store_true",
                   help="also write the table as CSV beside the PDF")
    p.add_argument("--raw", action="store_true",
                   help="also write the rows the table is made of: one line "
                        "per order, with the limit period it was live "
                        "through, as <stem>_raw.csv")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--demo", action="store_true",
                   help="render a sample off synthetic data, no kdb needed")
    p.add_argument("--self-test", action="store_true",
                   help="check the analytics and the q, no kdb needed")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.demo:
        return demo(args.out_dir)
    if args.monthly and args.date:
        raise SystemExit("--monthly and --date are different questions")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
