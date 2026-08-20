#!/usr/bin/env python3
"""
=============================================================================
short_sell_report.py

The Short-Sell Order Report, counting a REPLACED ORDER ONCE.

  python scripts/short_sell_report_v2/short_sell_report.py
  python scripts/short_sell_report_v2/short_sell_report.py --compare
  python scripts/short_sell_report_v2/short_sell_report.py --chains
  python scripts/short_sell_report_v2/short_sell_report.py --self-test

THE PROBLEM.  When an order is rejected and re-sent, the engine writes a NEW
id_target.  counting target rows, so one economic order becomes several and
its `size` is counted once per attempt.  Seen live on 2026-08-19: Thailand
read 3 orders / 81,000,000 / 0 executed.  It was ONE 27m order, rejected and
replaced twice, the last attempt cancelled.

WHY NOT JUST DROP THE REJECTS.  Because the rejections are the report.  The
fix has to collapse the attempts while keeping every rejection they produced.

THE CHAIN KEY: FIX TAG 9604.  The client puts its own order id in tag 9604 of
fixmsg, and a cancel-and-replace carries the SAME id - it is the client saying
"this is still that order", which is a fact rather than an inference.  (An
earlier version of this file grouped on the oes_oid prefix; that was a
convention, and conventions are what break silently.)

Chained on (date, tag 9604).  NOT on id_server: a trader can move an order to a
different order server mid-life, and the two halves are still one order.  How
often that happens is reported.

A target whose 9604 is empty cannot be chained to anything, so it stands alone -
keyed on its own server and id_target, which is what keeps two unrelated
untagged orders apart - and is counted exactly as counting targets would.

BOTH OF THE USER'S CHECKS ARE BUILT IN AND RUN EVERY TIME.

  1. IS THE TAG POPULATED for the universe we ask for?  Every run reports how
     many targets carry no 9604, and per market.  A high number does not
     invalidate the report - those orders are simply not chained - but it says
     how much of it the tag is actually doing.  It is also the first thing to
     look at if the parse ever breaks: if the separator in fixmsg is not one
     this knows about, EVERY target reads as missing and says so loudly.

  2. DOES IT MIX TWO DIFFERENT ORDERS?  A chain must agree on sym, side, algo
     and basket.  Any that does not is reported as MIXED, named field by field,
     and --chains prints it attempt by attempt.  That count must be zero.  None
     of those four is in the key ON PURPOSE: putting them in would make the key
     right by construction and silent, and the whole question is whether 9604
     is trustworthy on its own.

WHAT CHANGES, AND WHAT DOES NOT

  Orders       one per CHAIN, not one per target                       CHANGED
  Order qty    the chain's size, taken once                            CHANGED
  Executed     sum of `make` over every attempt's workorders        unchanged
  Rejections   every workorder row in state `rejected, all attempts  unchanged
  Completion   executed / order qty per market, mean across markets  unchanged

--compare puts the OLD counting - one order per target - beside the chained
numbers over a single fetch, so what the chaining did can be seen without
running anything twice.

THE CHAIN'S QUANTITY is the LARGEST any attempt asked for - CHAIN_QTY="max".
A replace can come back for only the unfilled remainder, and it can also grow
the order; "first" is wrong in the second case and "last" in the first, and
only "max" survives both.  Both are still available.  Any chain that fills more
than its quantity is reported whatever the setting, which is the tripwire if
this reasoning is incomplete.

  --compare runs BOTH rollups over one fetch and prints them side by side.
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

# scripts/lib holds the page this is drawn on and the mailer.  Added to the path
# rather than installed, so this still runs as
# `python scripts/short_sell_report/short_sell_report.py` from the repo root.
# Copy scripts/lib alongside this folder if you move it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.report_page import (                                    # noqa: E402
    BLUE, COL_W, DASH, GREEN, INK, INK2, INK3, L, R, RED,
    barchart, figure, fmt_int, fmt_pct0, fmt_pct1, footer as _footer, heading,
    hline, kpis as _kpis_row, log, save as _save, table as _table_rows,
    vbarchart)

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

# What the mail says.  Just this - the report is the attachment, and a body
# that restates it is a second copy to keep in step and one more thing to
# render wrong in somebody's client.
EMAIL_SIGNATURE = "Best Regards,\n\nKhalife"

# What quantity a chain asked for.  Executed is summed over EVERY attempt, so
# this decides what those fills are measured against - and the attempts are not
# all the same KIND of thing:
#
#   a REPLACEMENT supersedes the one before it.  Three sends of 27m that never
#     traded are one 27m order, not 81m - that is the whole reason v2 exists.
#   a TOP UP is extra quantity on an order that already finished.  Sizes
#     900, 1700, 2500 filling 3,600 in total are 5,100 asked for, not 2,500.
#
# Both are real and they pull opposite ways, so no single "take the Nth size"
# rule works.  "asked" reads it off the fills instead:
#
#     asked = (what every attempt filled) + (what the LAST one still had to do)
#
#                        sizes            fills        executed   asked
#   top ups        900, 1700, 2500   900, 1700, 1000      3,600   5,100
#   reject x3      27m, 27m, 27m           0, 0, 0            0     27m
#   remainder          100, 70             30, 70          100     100
#
# It cannot print over 100%: qty minus executed IS the last attempt's residual,
# which is never negative.  The others are kept for comparison:
#
#   "sum"    every size added up.  Right for top ups, and puts a rejected and
#            replaced order straight back to the un-chained number.
#   "max"    the largest attempt.  Right for replacements, over 100% on top ups.
#   "first" / "last"   the original, or the order as it finally stood.
CHAIN_QTY = "asked"

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

TITLE = "Short-Sell Order Report"

# =============================================================================
# Q
#
# The query, plus what the chain needs: fixmsg is already there for the Japan
# exclusion and carries tag 9604; oes_oid, basket, side and algo are for the
# consistency checks, and time orders a chain's attempts.  Nothing else differs
# - same suffix filter, same side filter, same refusal to group anything.
# =============================================================================

Q_SESSION = """
{[hist;d;sfx;sside]
  sside:`$sside;
  et:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; sym:0#`; size:0#0i;
         fixmsg:0#`; oes_oid:0#`; basket:0#`; side:0#`; algo:0#`;
         t_start:0#0Nt; t_end:0#0Nt; time:0#0Nt);
  ew:([] date:0#0Nd; id_server:0#0i; id_work:0#0i; id_target:0#0i; make:0#0i;
         state:0#`);

  t:$[hist;
      select date,id_server,id_target,sym,size,fixmsg,oes_oid,basket,side,
          algo,t_start,t_end,time
        from target where date=d, side=sside, any (upper sym) like/: sfx;
      update date:0Nd from select id_server,id_target,sym,size,fixmsg,oes_oid,
          basket,side,algo,t_start,t_end,time
        from target where side=sside, any (upper sym) like/: sfx];
  if[0=count t; :(et;ew)];

  ids:exec distinct id_target from t;
  w:$[hist;
      select date,id_server,id_work,id_target,make,state
        from workorder where date=d, id_target in ids;
      update date:0Nd from select id_server,id_work,id_target,make,state
        from workorder where id_target in ids];
  (t;w)
  }
"""


def fetch(handle, hist: bool, d: Optional[dt.date]):
    sfx = [p.encode() for p in SYM_PATTERNS]
    t, w = handle(Q_SESSION, hist, d if d is not None else _UNUSED_DATE,
                  sfx, SHORTSELL_SIDE.encode())
    return t.pd().to_dict("records"), w.pd().to_dict("records")

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
    sfx = [p.encode() for p in SYM_PATTERNS]
    t, w = handle(Q_SESSION, hist, d if d is not None else _UNUSED_DATE,
                  sfx, SHORTSELL_SIDE.encode())
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
# CHAINS
# =============================================================================

# THE SEPARATOR IS A SEMICOLON in this feed.  From a real fixmsg:
#
#   ...;16589=108223;9604=104642494_SG_HK_PORTAL_LIV_20260819162013;17717=...
#
# SOH and pipe are accepted too, since a stored copy may be rewritten either
# way and neither appears inside a value here.
#
# A CARET IS NOT A SEPARATOR, even though it looks like one.  It is used INSIDE
# values all over this feed - `SILK_FLOW^1008649713^TargetPart=30^SharedTempl^^`
# and `9012=274=1^275=1` are both one field - so splitting on it would carve
# values into pieces.  Nor is a space, for the same reason.
#
# If the real separator is none of these, fix_tag finds nothing and EVERY target
# reads as having no 9604 - which the run reports in the first line of output
# rather than quietly failing to chain anything.
_FIX_SEPS = "\x01;|\n\r"
CLIENT_ID_TAG = "9604"


def fix_tag(fixmsg, tag=CLIENT_ID_TAG) -> str:
    """The value of one FIX tag in a fixmsg, or "" if it is not there.

    Split into fields first and compare the whole tag, rather than searching for
    "9604=": that would also match 19604= and 9604X=, and a client id taken from
    the wrong tag is worse than no client id at all.
    """
    txt = _s(fixmsg)
    if not txt:
        return ""
    field = ""
    for ch in txt:
        if ch in _FIX_SEPS:
            k, sep, val = field.partition("=")
            if sep and k.strip() == tag:
                return val.strip()
            field = ""
        else:
            field += ch
    k, sep, val = field.partition("=")
    return val.strip() if sep and k.strip() == tag else ""


class Attempt(NamedTuple):
    """One target row: one send of an order."""
    key: tuple                 # (date, id_server, id_target) - the parent key
    date: Optional[dt.date]
    country: str
    sym: str
    size: int
    client_id: str             # FIX tag 9604 - "" when the client sent none
    oes_oid: str               # not the key, just context for --chains
    basket: str
    side: str
    algo: str
    t_start: float             # the order's live window, seconds
    t_end: float
    seq: float                 # target `time`, to find the last attempt
    id_target: int

    @property
    def chain_key(self) -> tuple:
        """What makes an order: the client's own id for it.

        id_server is NOT in it - a trader can move an order to another order
        server and it is still the same order, which is exactly the case a
        server in the key would split back apart.

        A target with no 9604 keys on its own server and id_target instead, so
        it stands alone.  Grouping the un-tagged ones together would merge every
        unrelated order the client did not label, which is the one mistake here
        that would be invisible - and id_target alone is not unique across
        servers, hence both.
        """
        if not self.client_id:
            return (self.date, "", self.key[1], self.id_target)
        return (self.date, self.client_id)


class Chain(NamedTuple):
    """One order, however many times it was sent."""
    chain_key: tuple
    date: Optional[dt.date]
    country: str
    sym: str
    side: str
    basket: str
    algo: str
    client_id: str
    size: int
    attempts: tuple            # every Attempt, in order

    @property
    def n(self) -> int:
        return len(self.attempts)

    @property
    def keys(self) -> set:
        return {a.key for a in self.attempts}

    def disagrees_on(self) -> list:
        """Which of sym, side, algo, basket the attempts do not agree on.

        Empty is what it should be.  Anything in it means one 9604 covered two
        different orders and this chain is wrong.
        """
        return [f for f in ("sym", "side", "algo", "basket")
                if len({getattr(a, f) for a in self.attempts}) > 1]


def to_attempts(records) -> tuple:
    """(attempts, restricted_dropped).  out of scope
    markets and Japan's restricted names are dropped before anything counts."""
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
        idt = _i(r.get("id_target"))
        out.append(Attempt(
            key=(d, _i(r.get("id_server")), idt), date=d, country=country,
            sym=sym, size=abs(_i(r.get("size"))),
            client_id=fix_tag(r.get("fixmsg")),
            oes_oid=_s(r.get("oes_oid")), basket=_s(r.get("basket")),
            side=_s(r.get("side")) or SHORTSELL_SIDE,
            algo=_s(r.get("algo")),
            t_start=_t(r.get("t_start")), t_end=_t(r.get("t_end")),
            seq=_t(r.get("time")), id_target=idt))
    return out, dropped


def _t(v) -> float:
    """A q time as seconds since midnight, to order a chain's attempts."""
    if v is None:
        return 0.0
    if isinstance(v, dt.timedelta):
        return v.total_seconds()
    if isinstance(v, dt.time):
        return (v.hour * 3600 + v.minute * 60 + v.second
                + v.microsecond / 1e6)
    try:
        import pandas as pd
        if pd.isna(v):
            return 0.0
        return pd.Timedelta(v).total_seconds()
    except Exception:
        return 0.0


def attempt_fills(splits) -> dict:
    """What each ATTEMPT executed, keyed on the target it belongs to."""
    out = {}
    for sp in splits:
        out[sp.key] = out.get(sp.key, 0) + sp.make
    return out


def to_chains(attempts, qty=None, splits=()) -> list:
    """Collapse attempts into orders on the client's id.

    Ordered by (time, id_target) so "the last attempt" is the last one SENT,
    with the id as the tie break - two attempts can share a timestamp, and the
    id is monotonic where the clock is only nearly so.

    splits is only needed for qty="asked", which reads the quantity off what
    each attempt actually did.  Passing none makes "asked" fall back to the
    last attempt's size, which is what it degenerates to when nothing filled.
    """
    qty = qty or CHAIN_QTY
    fills = attempt_fills(splits)
    groups = {}
    for a in attempts:
        groups.setdefault(a.chain_key, []).append(a)

    out = []
    for k, got in groups.items():
        got = sorted(got, key=lambda a: (a.seq, a.id_target))
        last = got[-1]
        if qty == "asked":
            #  every attempt's fills, plus whatever the last one still had left
            #  to do.  A superseded attempt contributes only what it traded, so
            #  a replacement is not counted twice; a top up contributes its
            #  whole size, because it filled it.
            done = [fills.get(a.key, 0) for a in got]
            size = sum(done) + max(0, last.size - done[-1])
        elif qty == "sum":
            size = sum(a.size for a in got)
        elif qty == "max":
            size = max(a.size for a in got)
        elif qty == "first":
            size = got[0].size
        else:
            size = last.size
        out.append(Chain(chain_key=k, date=last.date, country=last.country,
                         sym=last.sym, side=last.side, basket=last.basket,
                         algo=last.algo, client_id=last.client_id,
                         size=size, attempts=tuple(got)))
    return sorted(out, key=lambda c: (c.attempts[0].seq, c.attempts[0].id_target))


# =============================================================================
# THE TWO CHECKS
#
# Is the tag populated for the universe we ask for, and does it ever mix two
# different orders.  Both run on every report rather than being something to
# remember to look at.
# =============================================================================

class ChainStats(NamedTuple):
    attempts: int
    chains: int
    multi: int                 # chains of more than one attempt
    longest: int
    no_id: int                 # attempts with no tag 9604
    no_id_by_market: dict
    mixed: list                # chains disagreeing on sym/side/algo/basket
    mixed_size: list           # chains whose attempts disagree on size
    multi_server: list         # chains spanning more than one order server


def chain_stats(attempts, chs) -> ChainStats:
    no_id = [a for a in attempts if not a.client_id]
    by_mkt = {}
    for a in no_id:
        by_mkt[a.country] = by_mkt.get(a.country, 0) + 1
    return ChainStats(
        attempts=len(attempts), chains=len(chs),
        multi=sum(1 for c in chs if c.n > 1),
        longest=max([c.n for c in chs], default=0),
        no_id=len(no_id), no_id_by_market=by_mkt,
        mixed=[c for c in chs if c.disagrees_on()],
        mixed_size=[c for c in chs if len({a.size for a in c.attempts}) > 1],
        multi_server=[c for c in chs
                      if len({a.key[1] for a in c.attempts}) > 1])


def chain_fills(chs, splits) -> dict:
    """Executed quantity per chain, from the same workorder rows the page uses."""
    owner = {}
    for c in chs:
        for k in c.keys:
            owner[k] = c.chain_key
    out = {}
    for sp in splits:
        ck = owner.get(sp.key)
        if ck is not None:
            out[ck] = out.get(ck, 0) + sp.make
    return out


def over_filled(chs, splits) -> list:
    """Chains that executed MORE than their quantity - a completion over 100%.

    Under CHAIN_QTY="max" this should be empty: fills are summed over attempts
    and no attempt can fill more than the largest one asked for.  If it is not
    empty, that reasoning is incomplete and the page is overstating completion
    somewhere, so it says so rather than printing 143% and leaving it there.
    """
    fills = chain_fills(chs, splits)
    return [(c, fills.get(c.chain_key, 0)) for c in chs
            if c.size > 0 and fills.get(c.chain_key, 0) > c.size]


# An order that never produced a workorder is ambiguous, and the two readings
# are opposites:
#
#   the client pulled it        - it was cancelled seconds after it arrived and
#                                 we never had a chance.  Not our failure, and
#                                 its quantity arguably does not belong in a
#                                 completion percentage at all.
#   we never sent anything      - it sat there for hours and the algo generated
#                                 nothing.  Very much our failure, and exactly
#                                 what a completion report exists to surface.
#
# Nothing in the row itself says which.  How LONG it was live does: a lifetime
# of seconds is the first, of hours the second.  So this measures rather than
# decides, and the split is printed on every run.
QUICK_CANCEL_SECS = 60.0


def no_workorder(chs, splits) -> tuple:
    """(quick, slow) - chains that never produced a single workorder, split by
    whether they lived long enough for that to be our fault."""
    had = {sp.key for sp in splits}
    none = [c for c in chs if not (c.keys & had)]
    quick = [c for c in none
             if (c.attempts[-1].t_end - c.attempts[0].t_start)
             <= QUICK_CANCEL_SECS]
    slow = [c for c in none if c not in quick]
    return quick, slow


def report_no_workorder(quick, slow) -> None:
    """Report them; do not remove them.

    Dropping the quick ones from completion is defensible - there was nothing
    to complete - but it is not free: an order that vanishes from a report is
    an order nobody counts, and the SLOW ones are a finding rather than noise.
    Until the split below shows which case dominates, both stay in and both are
    disclosed.
    """
    if not (quick or slow):
        return
    qn = sum(c.size for c in quick)
    sn = sum(c.size for c in slow)
    log(f"  {len(quick) + len(slow):,} orders never produced a workorder "
        f"({qn + sn:,} qty), and are IN the numbers above:")
    if quick:
        log(f"      {len(quick):,} died within {QUICK_CANCEL_SECS:.0f}s "
            f"({qn:,} qty) - pulled before we had a chance")
    if slow:
        worst = max(slow, key=lambda c: c.attempts[-1].t_end
                    - c.attempts[0].t_start)
        mins = (worst.attempts[-1].t_end - worst.attempts[0].t_start) / 60.0
        log(f"      {len(slow):,} lived longer ({sn:,} qty) - WE sent nothing, "
            f"longest {mins:.0f} min on {worst.sym}. These are a finding, not "
            f"noise, and are why none of this is dropped automatically")


def report_stats(st: ChainStats, quiet=False):
    log(f"  chains: {st.attempts:,} targets -> {st.chains:,} order"
        f"{'' if st.chains == 1 else 's'} "
        f"({st.multi:,} chained, longest {st.longest})")

    #  CHECK 1 - is tag 9604 populated for the universe we asked for
    if st.no_id == st.attempts and st.attempts:
        log(f"  WARNING: NOT ONE of {st.attempts:,} targets carries tag "
            f"{CLIENT_ID_TAG}. Either the client sends none, or fixmsg uses a "
            f"separator fix_tag does not know - check one fixmsg by hand "
            f"before believing any of this. Nothing has been chained.")
    elif st.no_id:
        pct = 100.0 * st.no_id / max(st.attempts, 1)
        worst = ", ".join(f"{k} {v:,}" for k, v in
                          sorted(st.no_id_by_market.items(),
                                 key=lambda kv: -kv[1]))
        log(f"  {st.no_id:,} of {st.attempts:,} targets ({pct:.1f}%) carry no "
            f"tag {CLIENT_ID_TAG} and stand alone, as counting targets would: {worst}")
    else:
        log(f"  tag {CLIENT_ID_TAG} is populated on every target")

    #  CHECK 2 - does one id ever cover two different orders
    if st.mixed:
        fields = sorted({f for c in st.mixed for f in c.disagrees_on()})
        one = len(st.mixed) == 1
        log(f"  WARNING: {len(st.mixed):,} chain{'' if one else 's'} "
            f"{'disagrees' if one else 'disagree'} on {', '.join(fields)} - "
            f"a {CLIENT_ID_TAG} is covering more than one order and these "
            f"numbers are WRONG.  --chains lists them")
    else:
        log(f"  no chain mixes sym, side, algo or basket")

    if st.multi_server:
        one = len(st.multi_server) == 1
        log(f"  {len(st.multi_server):,} chain{'' if one else 's'} "
            f"{'spans' if one else 'span'} more than one "
            f"order server - a trader moved the order.  Not an error; keying "
            f"on id_server would have split these back apart")

    if st.mixed_size:
        which = {"asked": "what they filled plus the last residual",
                 "sum": "all of them added up", "max": "the largest",
                 "first": "the first", "last": "the last"}.get(
                     CHAIN_QTY, CHAIN_QTY)
        log(f"  {len(st.mixed_size):,} chains have attempts of differing size "
            f"- a replace resized the order, or topped it up; "
            f"CHAIN_QTY={CHAIN_QTY!r} takes {which}. --chains")
    return st


def over_filled_attempts(attempts, splits) -> list:
    """Individual targets that executed more than their own size.

    THIS is the tripwire under CHAIN_QTY="asked", and it has to exist, because
    under that rule the chain level check CANNOT fire: asked is defined as the
    fills plus the last residual, so quantity minus executed is that residual
    and is never negative.  A rule that makes its own check vacuous needs
    another one, and this is it - a target filling more than it asked for is a
    data question, and it is the only thing left that could put a completion
    over 100% honestly.
    """
    fills = attempt_fills(splits)
    return [(a, fills.get(a.key, 0)) for a in attempts
            if a.size > 0 and fills.get(a.key, 0) > a.size]


def report_over_filled_attempts(over) -> None:
    if not over:
        return
    log(f"  WARNING: {len(over):,} individual target"
        f"{'' if len(over) == 1 else 's'} executed MORE than their own size. "
        f"That is not a grouping question - a workorder is filling more than "
        f"the target it belongs to:")
    for a, made in over[:10]:
        log(f"      id_target {a.id_target}  {a.sym}  size {a.size:,}  "
            f"executed {made:,}  ({100.0 * made / a.size:.0f}%)")


def unchain(chs, over) -> list:
    """Explode the over-filled chains back into one order per attempt.

    `over` is what over_filled() returns: [(chain, executed), ...].

    The escape hatch: a chain that still executes more than it asked for has
    been grouped wrongly, whatever the reason, and one order per target is
    exactly what counting targets would have said - a number that is defensible even
    when it is not ideal.  Better a chain we could not explain counted the old
    way than a completion of 144% on the page.
    """
    bad = {c.chain_key for c, _made in over}
    out = [c for c in chs if c.chain_key not in bad]
    for c in chs:
        if c.chain_key not in bad:
            continue
        for a in c.attempts:
            out.append(Chain(chain_key=(a.date, "", a.key[1], a.id_target),
                             date=a.date, country=a.country, sym=a.sym,
                             side=a.side, basket=a.basket, algo=a.algo,
                             client_id=a.client_id, size=a.size,
                             attempts=(a,)))
    return sorted(out, key=lambda c: (c.attempts[0].seq,
                                      c.attempts[0].id_target))


def report_unchained(over, still=()) -> None:
    """Say what was un-chained, and be honest about what that could not fix."""
    if not over:
        return
    n = sum(c.n for c, _made in over)
    log(f"  {len(over):,} chain{'' if len(over) == 1 else 's'} above have been "
        f"UN-CHAINED into their {n:,} targets and counted the way v1 counts "
        f"them, so the page does not read over 100%. Those are the ones to "
        f"look at with --chains")
    if still:
        log(f"  WARNING: {len(still):,} of them STILL execute more than their "
            f"own size as single targets, so this is not a grouping problem - "
            f"a workorder is filling more than the target it belongs to:")
        for c, made in still[:5]:
            log(f"      id_target {c.attempts[0].id_target}  {c.sym}  "
                f"size {c.size:,}  executed {made:,}")


def report_over_filled(over) -> None:
    if not over:
        return
    log(f"  WARNING: {len(over):,} chain{'' if len(over) == 1 else 's'} "
        f"executed MORE than the quantity taken for them, so completion is "
        f"over 100% there. With CHAIN_QTY={CHAIN_QTY!r} that should be "
        f"impossible - check these before believing the page:")
    for c, made in over[:10]:
        log(f"      {CLIENT_ID_TAG}={c.client_id or '(none)'}  {c.sym}  "
            f"qty {c.size:,}  executed {made:,}  "
            f"({100.0 * made / c.size:.0f}%)  sizes "
            f"{sorted({a.size for a in c.attempts})}")


def dump_untagged(attempts, limit=200) -> int:
    """The targets carrying no tag 9604.

    They are counted on the page - each stands alone, exactly as counting targets would -
    but until now there was no way to see WHICH they were, and "6.6% untagged"
    is not something anyone can act on.
    """
    got = [a for a in attempts if not a.client_id]
    if not got:
        print(f"every target carries tag {CLIENT_ID_TAG}")
        return 0
    by_mkt = {}
    for a in got:
        by_mkt[a.country] = by_mkt.get(a.country, 0) + 1
    print(f"{len(got):,} of {len(attempts):,} targets carry no tag "
          f"{CLIENT_ID_TAG} "
          f"({100.0 * len(got) / max(len(attempts), 1):.1f}%)")
    print("  " + ", ".join(f"{MARKET_NAME.get(k, k)} {n:,}"
                           for k, n in sorted(by_mkt.items(),
                                              key=lambda kv: -kv[1])))
    print(f"\neach stands alone and is counted exactly as counting targets would"
          + (f"; showing the first {limit}" if len(got) > limit else ""))
    print(f"\n  {'market':<11}{'sym':<16}{'id_target':>12}  {'size':>14}  "
          f"{'algo':<10}{'basket':<12}oes_oid")
    for a in sorted(got, key=lambda x: -x.size)[:limit]:
        print(f"  {a.country:<11}{a.sym:<16}{a.id_target:>12}  {a.size:>14,}  "
              f"{a.algo or '-':<10}{a.basket or '-':<12}{a.oes_oid or '-'}")
    return 0


def dump_chains(chs, limit=40):
    """The chained orders, and any chain that mixes - both for eyeballing
    against the engine."""
    st = chain_stats([a for c in chs for a in c.attempts], chs)

    multi = [c for c in chs if c.n > 1]
    if not multi:
        print("no chained orders: every target stands alone")
    else:
        print(f"{len(multi):,} chained orders"
              + (f", showing the first {limit}" if len(multi) > limit else ""))
        for c in multi[:limit]:
            bad = c.disagrees_on()
            flag = (f"   <-- MIXED on {', '.join(bad)}" if bad else
                    "   <-- sizes differ across attempts"
                    if len({a.size for a in c.attempts}) > 1 else "")
            print(f"\n  {CLIENT_ID_TAG}={c.client_id or '(none)'}  {c.sym}  "
                  f"{c.country}  {c.side}  {c.algo or '-'}  "
                  f"basket {c.basket or '-'}  -> qty {c.size:,}{flag}")
            for a in c.attempts:
                print(f"      id_target {a.id_target:<12} size {a.size:>14,}  "
                      f"t {a.seq:>9.0f}  oes_oid {a.oes_oid or '-'}")

    if st.mixed:
        print(f"\n{len(st.mixed):,} chains cover more than one order - tag "
              f"{CLIENT_ID_TAG} is NOT safe on its own here:")
        for c in st.mixed[:limit]:
            print(f"\n  {CLIENT_ID_TAG}={c.client_id}  disagrees on "
                  f"{', '.join(c.disagrees_on())}")
            for a in c.attempts:
                print(f"      id_target {a.id_target:<12} {a.sym:<14} "
                      f"{a.side:<10} {a.algo or '-':<10} "
                      f"basket {a.basket or '-':<10} size {a.size:>14,}")
    return 0


# =============================================================================
# THE NUMBERS
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


def totals(rows) -> Totals:
    """Summed executed over summed order qty - the real overall fill rate.

    It was briefly the mean of the market percentages, because a replaced and
    rejected order was counted once per attempt and 81m of quantity that never
    existed dragged the ratio to 12.3%.  Chaining fixes that at its source, so
    the honest aggregate is honest again.
    """
    ex = sum(r.executed for r in rows)
    qty = sum(r.order_qty for r in rows)
    return Totals(sum(r.orders for r in rows), qty, ex,
                  sum(r.rejections for r in rows), _completion(ex, qty))


def by_market(chs, splits) -> list:
    orders = {c: 0 for c in MARKET_CODES}
    qty = {c: 0 for c in MARKET_CODES}
    made = {c: 0 for c in MARKET_CODES}
    rej = {c: 0 for c in MARKET_CODES}
    for c in chs:
        orders[c.country] += 1
        qty[c.country] += c.size
    for s in splits:
        made[s.country] += s.make
        if s.rejected:
            rej[s.country] += 1
    return [Row(m.code, m.name, orders[m.code], qty[m.code],
                made[m.code], rej[m.code]) for m in MARKETS]


def by_market_targets(attempts, splits) -> list:
    """The OLD counting, one order per target - what --compare puts beside the
    chained numbers.

    The comparison is the whole reason to trust the chaining: if the two columns
    differ only where chains exist, the chaining is doing what it says.
    """
    orders = {c: 0 for c in MARKET_CODES}
    qty = {c: 0 for c in MARKET_CODES}
    made = {c: 0 for c in MARKET_CODES}
    rej = {c: 0 for c in MARKET_CODES}
    for a in attempts:
        orders[a.country] += 1
        qty[a.country] += a.size
    for s in splits:
        made[s.country] += s.make
        if s.rejected:
            rej[s.country] += 1
    return [Row(m.code, m.name, orders[m.code], qty[m.code],
                made[m.code], rej[m.code]) for m in MARKETS]


def by_day(chs, splits) -> list:
    """One DayRow per date; completion is that day's executed over that day's
    order qty, the same measure the headline uses."""
    days = {}

    def slot(d):
        return days.setdefault(d, [0, 0, 0, 0])

    for c in chs:
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


# =============================================================================
# COMPARE
# =============================================================================

def _pct(v) -> str:
    """A percentage for the CONSOLE.  fmt_pct1's em dash is right on the page
    and wrong here: a Windows console in cp1252 either garbles it or raises,
    and a diagnostic that crashes on a market with no orders is no use."""
    return "-" if v is None else f"{v:.1f}%"


def compare_lines(v1_rows, v2_rows) -> list:
    """v1 beside v2, per market and in total.  One fetch, two rollups, so any
    difference is the counting and nothing else."""
    t1, t2 = totals(v1_rows), totals(v2_rows)
    out = [
        f"{'':<12}{'orders':>18}{'order qty':>28}{'completion':>22}",
        f"{'':<12}{'v1':>8}{'v2':>10}{'v1':>14}{'v2':>14}{'v1':>11}{'v2':>11}",
        "-" * 80,
    ]
    for a, b in zip(v1_rows, v2_rows):
        out.append(
            f"{a.name:<12}{a.orders:>8,}{b.orders:>10,}"
            f"{a.order_qty:>14,}{b.order_qty:>14,}"
            f"{_pct(a.completion):>11}{_pct(b.completion):>11}")
    out.append("-" * 80)
    out.append(
        f"{'TOTAL':<12}{t1.orders:>8,}{t2.orders:>10,}"
        f"{t1.order_qty:>14,}{t2.order_qty:>14,}"
        f"{_pct(t1.completion):>11}{_pct(t2.completion):>11}")
    out.append("")
    out.append(f"executed and rejections are identical by construction: "
               f"{fmt_int(t1.executed)} / {fmt_int(t1.rejections)}")
    return out

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


def _table(fig, rows, y_top, row_h):
    """The per market table.  A zero rejection count stays muted, so the eye is
    only pulled to the counts that are not zero."""
    cells = [[(r.name, INK, "normal"),
              (fmt_int(r.orders), INK, "normal"),
              (fmt_int(r.order_qty), INK, "normal"),
              (fmt_int(r.executed), INK, "normal"),
              (fmt_pct1(r.completion), INK, "normal"),
              (fmt_int(r.rejections),
               RED if r.rejections else INK3,
               "bold" if r.rejections else "normal")]
             for r in rows]
    return _table_rows(fig, TABLE_COLS, cells, y_top, row_h)


def _kpis(fig, tot):
    """The three headline figures.  Colour carries the same meaning it carries
    everywhere else on the page: green completion, red rejections."""
    _kpis_row(fig, [(fmt_int(tot.orders), "Short-sell orders", INK),
                    (fmt_pct1(tot.completion), "Overall completion", GREEN),
                    (fmt_int(tot.rejections), "Rejections", RED)],
              Y_KPI_VALUE, Y_KPI_LABEL)


def _sorted_pairs(rows, key):
    """Chart order: biggest first, ties keeping MARKETS order.  Python's sort is
    stable, so the fixed market order is the tie break for free."""
    return sorted(rows, key=key, reverse=True)


def draw(rows, tot, subtitle, footer, days=None):
    """The whole page.  Pure: takes rollups, returns a figure.

    short_sell_report_v2 draws on this too, and reaches the title through the
    TITLE global rather than through an argument - this file is the one that
    gets EDITED for the servers and the mail, so a copy of it in the wild is
    often not the copy in git, and v2 must not need a particular signature.
    """
    monthly = days is not None

    fig = figure()
    heading(fig, TITLE, subtitle, Y_TITLE, Y_SUBTITLE, Y_RULE_TOP)
    _kpis(fig, tot)

    row_h = 0.030 if monthly else 0.040
    _table(fig, rows, Y_TABLE_TOP, row_h)

    # ---- completion and rejections by market -------------------------------
    comp = _sorted_pairs(rows, key=lambda r: (r.completion or 0.0))
    rej = _sorted_pairs(rows, key=lambda r: r.rejections)

    mkt_y0, mkt_rect_h, mkt_title_y = MKT_BAND["monthly" if monthly else "daily"]
    half = 0.405

    barchart(fig, (L, mkt_y0, half, mkt_rect_h), "Completion by market",
             [r.name for r in comp],
             [(r.completion or 0.0) for r in comp],
             [fmt_pct0(r.completion) for r in comp],
             BLUE, vmax=100.0, fs=8.0, title_y=mkt_title_y)
    barchart(fig, (R - half, mkt_y0, half, mkt_rect_h), "Rejections by market",
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
        vbarchart(fig, (L, cy, COL_W, ch), "Completion by day",
                  labels,
                  [(d.completion or 0.0) for d in days] or [0.0],
                  [fmt_pct0(d.completion) for d in days] or [DASH],
                  BLUE, vmax=100.0, fs=day_fs, title_y=cty)
        vbarchart(fig, (L, ry, COL_W, rh), "Rejections by day",
                  labels,
                  [float(d.rejections) for d in days] or [0.0],
                  [fmt_int(d.rejections) for d in days] or ["0"],
                  RED, fs=day_fs, title_y=rty)

    # ---- notes and footer ---------------------------------------------------
    _footer(fig, footer, Y_RULE_BOTTOM, Y_FOOTER)
    return fig


def save(fig, out_dir: Path, stem: str):
    return _save(fig, out_dir, stem, dpi=DPI)

# =============================================================================
# EMAIL
#
# The settings live in the EMAIL block at the top of this file.
# =============================================================================

def _mailer():
    try:
        from lib import mailer
    except ImportError as e:
        raise SystemExit(
            f"EMAIL_TO is set but scripts/lib/mailer.py will not import "
            f"({e}).  It sits beside this script's folder; copy scripts/lib "
            f"too if you moved this one.")
    return mailer

def _cfg(name, default):
    return getattr(v1, name, default)


def email_configured() -> bool:
    return bool(_cfg("EMAIL_TO", []) or _cfg("EMAIL_CC", [])
                or _cfg("EMAIL_BCC", []))


def mail_report(when, files) -> None:
    """Send the report: the PDF, and a body that is just the sign-off."""
    m = _mailer()
    pdf = next((q for q in files if q.suffix == ".pdf"), None)
    sender = _cfg("EMAIL_FROM", "")
    if not sender:
        raise SystemExit(
            "EMAIL_TO is set but EMAIL_FROM is empty. Both live in the EMAIL "
            "block near the top of short_sell_report.py, which this shares.")
    if pdf is None:
        raise SystemExit("nothing to attach: no PDF was written")

    msg = m.build_message(m.Mail(
        subject=f"{TITLE} v2 - {when}", sender=sender,
        to=_cfg("EMAIL_TO", []), cc=_cfg("EMAIL_CC", []),
        bcc=_cfg("EMAIL_BCC", []),
        text=_cfg("EMAIL_SIGNATURE", "Best Regards,"),
        attachments=[pdf]))
    smtp = m.Smtp(host=_cfg("SMTP_HOST", ""), port=_cfg("SMTP_PORT", 0),
                  timeout=_cfg("SMTP_TIMEOUT", 30))
    log("  email:")
    log(m.describe(msg))
    rcpt = m.send(msg, smtp, dry_run=_cfg("EMAIL_DRY_RUN", False))
    if _cfg("EMAIL_DRY_RUN", False):
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
    _check_server(pl.endpoint, pl.endpoint_name)
    log(f"short_sell_report_v2  {'historical' if pl.hist else 'realtime'}  "
        f"{pl.endpoint}")
    h = connect(pl.endpoint)

    attempts, splits, dropped, traded = [], [], 0, 0
    for d in pl.dates:
        if not args.quiet and d is not None:
            log(f"  {d} ...")
        pr, wr = fetch(h, pl.hist, d)
        att, drop = to_attempts(pr)
        dropped += drop
        if not att:
            continue
        traded += 1
        attempts.extend(att)
        splits.extend(to_splits(wr, att))       # keyed on the target rows

    if args.no_tag:
        return dump_untagged(attempts)

    chs = to_chains(attempts, args.chain_qty, splits)
    over = over_filled(chs, splits)
    if over and not args.keep_over:
        #  the escape hatch: whatever grouped these was wrong, so count them the
        #  way v1 counts them rather than print a completion over 100%
        chs = unchain(chs, over)
    st = report_stats(chain_stats(attempts, chs), args.quiet)
    report_over_filled(over)
    if over and not args.keep_over:
        report_unchained(over, over_filled(chs, splits))
    report_over_filled_attempts(over_filled_attempts(attempts, splits))
    report_no_workorder(*no_workorder(chs, splits))
    if args.chains:
        return dump_chains(chs)

    rows = by_market(chs, splits)
    tot = totals(rows)
    days = by_day(chs, splits) if pl.monthly else None

    if args.compare:
        v1_rows = by_market_targets(attempts, splits)
        print("\n".join(compare_lines(v1_rows, rows)))
        return 0

    log(f"  {tot.orders:,} short-sell orders, {tot.rejections:,} rejections"
        + (f", {dropped:,} restricted JP orders excluded" if dropped else ""))

    subtitle = f"By market  ·  {pl.when}"
    foot = (f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  "
            + ("historical" if pl.hist else "real-time snapshot")
            + f"  ·  {st.attempts:,} targets chained into {st.chains:,} orders"
            + (f", {st.no_id:,} untagged" if st.no_id else ""))
    if dropped:
        foot += f"  ·  {dropped:,} restricted JP excluded"

    fig = draw(rows, tot, subtitle, foot, days)
    files = save(fig, Path(args.out_dir), pl.stem.replace(
        "short_sell_report", "short_sell_report_v2"))
    if email_configured():
        mail_report(pl.when, files)
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


def demo_session(d=None):
    """(attempts, splits) for one made up session.  Deterministic.

    Includes what the page is FOR: a Thai order rejected and replaced twice
    under one client id, and a Japanese one topped up twice.
    """
    pr, wr = [], []
    for i in range(1, 110):                      # Hong Kong, one send each
        pr.append(_a(i, "HK", 457_633, f"CLI-HK-{i:04d}", t=i, d=d))
        wr.append(_c(i, i, 242_901, "filled", d=d))
    for i in range(239):
        wr.append(_c(90_000 + i, 1 + (i % 109), 0, "rejected", d=d))
    for i in range(1, 542):                      # Japan
        pr.append(_a(1000 + i, "JP", 9_903, f"CLI-JP-{i:04d}", t=i, d=d))
        wr.append(_c(1000 + i, 1000 + i, 8_285, "filled", d=d))
    for i in range(3):
        wr.append(_c(91_000 + i, 1001 + (i % 541), 0, "rejected", d=d))
    for i in range(1, 83):                       # Korea
        pr.append(_a(2000 + i, "KR", 12_567, f"CLI-KR-{i:04d}", t=i, d=d))
        wr.append(_c(2000 + i, 2000 + i, 4_933, "done", d=d))
    for i in range(152):
        wr.append(_c(92_000 + i, 2001 + (i % 82), 0, "rejected", d=d))

    #  ONE Thai order, sent three times under one client id, never traded
    for k, idt in enumerate((3001, 3002, 3003)):
        pr.append(_a(idt, "TH", 27_000_000, "CLI-TH-0001", t=k, d=d))
        wr.append(_c(idt, idt, 0, "rejected" if k < 2 else "cxl", d=d))
    #  and one Japanese order topped up twice
    for k, (idt, size, made) in enumerate(((3101, 900, 900),
                                           (3102, 1700, 1700),
                                           (3103, 2500, 1000))):
        pr.append(_a(idt, "JP", size, "CLI-JP-TOPUP", t=k, d=d))
        wr.append(_c(idt, idt, made, "filled", d=d))

    attempts, _dropped = to_attempts(pr)
    return attempts, to_splits(wr, attempts)


def demo_month(year=2026, month=7):
    attempts, splits = [], []
    for i, d in enumerate(month_dates(year, month)):
        a, w = demo_session(d)
        take = 40 + (i * 7) % 400
        keep = a[:take]
        keys = {x.key for x in keep}
        attempts.extend(keep)
        splits.extend([x for x in w if x.key in keys])
    return attempts, splits


def demo(out_dir) -> int:
    """Draw both layouts from made up numbers.  No kdb, no pykx."""
    out = Path(out_dir)
    stamp = f"Generated {dt.datetime.now():%Y-%m-%d %H:%M}  ·  {DEMO_STAMP}"

    attempts, splits = demo_session()
    chs = to_chains(attempts, CHAIN_QTY, splits)
    rows = by_market(chs, splits)
    log("demo: daily layout")
    report_stats(chain_stats(attempts, chs))
    save(draw(rows, totals(rows), "By market  ·  2026-07-24 18:37  ·  SAMPLE",
              stamp), out, "short_sell_report_SAMPLE_daily")

    attempts, splits = demo_month()
    chs = to_chains(attempts, CHAIN_QTY, splits)
    rows = by_market(chs, splits)
    days = by_day(chs, splits)
    log(f"demo: monthly layout, {len(days)} trading days")
    save(draw(rows, totals(rows), "By market  ·  July 2026  ·  SAMPLE", stamp,
              days), out, "short_sell_report_SAMPLE_monthly")
    log("  these are made up numbers - do not circulate them as a report")
    return 0


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

def _a(idt, country, size, cid, basket="B1", side="sellshort",
       algo="vwap", t=0.0, live=3600.0, d=None, srv=1, sym=None, extra=""):
    """One target row, as q returns it.  cid goes into fixmsg as tag 9604, the
    way the client actually sends it - so the fixture exercises the PARSE, not
    just the grouping."""
    r = _p(idt, country, size, d=d, srv=srv)
    if sym:
        r["sym"] = sym
    #  built the way the real feed does: semicolon separated, with a caret
    #  bearing field beside it so the parser is exercised against both
    fix = "8=FIX.4.2;35=D;9012=274=1^275=1;16589=108223;"
    if cid:
        fix += f"{CLIENT_ID_TAG}={cid};"
    r.update({"fixmsg": fix + extra + "17717=7280001184;59=0",
              "t_start": dt.timedelta(seconds=t),
              "t_end": dt.timedelta(seconds=t + live),
              "oes_oid": f"OID.{idt}", "basket": basket, "side": side,
              "algo": algo, "time": dt.timedelta(seconds=t)})
    return r

def self_test() -> int:
    import contextlib as _ctx
    import io as _i2
    ok = True

    def printed_err(fn, *a):
        """report_* write to stderr, via log()."""
        buf = _i2.StringIO()
        with _ctx.redirect_stderr(buf):
            fn(*a)
        return buf.getvalue()

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("short_sell_report_v2 --self-test\n\nreading tag 9604 out of fixmsg")
    SOH = "\x01"
    check("a normal SOH separated message",
          fix_tag(f"8=FIX.4.2{SOH}35=D{SOH}9604=ABC123{SOH}59=0"), "ABC123")
    check("the tag at the very end, with no trailing separator",
          fix_tag(f"35=D{SOH}9604=ABC123"), "ABC123")
    check("the tag first", fix_tag(f"9604=ABC123{SOH}35=D"), "ABC123")
    check("pipe separated, as logs rewrite it",
          fix_tag("8=FIX.4.2|9604=ABC123|59=0"), "ABC123")
    check("semicolon separated", fix_tag("35=D;9604=ABC123;59=0"), "ABC123")
    #  A REAL fixmsg from this feed, semicolons throughout.  A caret is NOT a
    #  separator here - it appears inside values - so a value carrying one must
    #  come back whole.
    REAL = ("35=D;9012=274=1^275=1;16589=108223;"
            "9604=104642494_SG_HK_PORTAL_LIV_20260819162013;"
            "17717=7280001184;16500=system;40=1;16505=GAM.MK")
    check("the real message shape",
          fix_tag(REAL), "104642494_SG_HK_PORTAL_LIV_20260819162013")
    check("a caret inside a value does NOT split it",
          fix_tag(REAL, tag="9012"), "274=1^275=1")
    check("a caret-joined value keeps its carets",
          fix_tag("35=D;1008649713=SILK_FLOW^TargetPart=30^SharedTempl^^;59=0",
                  tag="1008649713"), "SILK_FLOW^TargetPart=30^SharedTempl^^")
    check("the second message carries the SAME id, which is the whole point",
          fix_tag(REAL) == fix_tag(REAL.replace("16589=108223",
                                                "16589=108543")), True)
    check("a value containing a space survives",
          fix_tag(f"9604=ABC 123{SOH}59=0"), "ABC 123")
    check("a value containing an = survives",
          fix_tag(f"9604=A=B{SOH}59=0"), "A=B")
    check("absent is empty", fix_tag(f"35=D{SOH}59=0"), "")
    check("present but empty is empty", fix_tag(f"9604={SOH}59=0"), "")
    check("no fixmsg at all is empty", fix_tag(""), "")
    check("None is empty, not a crash", fix_tag(None), "")

    #  the traps: a tag that merely CONTAINS 9604 must not be read as it
    check("19604 is not 9604", fix_tag(f"19604=WRONG{SOH}59=0"), "")
    check("96040 is not 9604", fix_tag(f"96040=WRONG{SOH}59=0"), "")
    check("and 9604 inside a VALUE is not 9604",
          fix_tag(f"58=see 9604=WRONG{SOH}59=0"), "")
    check("the right tag still wins beside a decoy",
          fix_tag(f"19604=WRONG{SOH}9604=RIGHT{SOH}59=0"), "RIGHT")
    check("another tag can be read too",
          fix_tag(f"9604=A{SOH}RSHO=1{SOH}", tag="RSHO"), "1")

    print("\nchaining on the client's id")
    #  three sends of one 27m Thai order, then a different order
    att, _ = to_attempts([
        _a(1, "TH", 27_000_000, "CLI-0001", t=1),
        _a(2, "TH", 27_000_000, "CLI-0001", t=2),
        _a(3, "TH", 27_000_000, "CLI-0001", t=3),
        _a(4, "TH", 5_000_000, "CLI-0002", t=4)])
    chs = to_chains(att)
    check("four targets", len(att), 4)
    check("the id was parsed off fixmsg", att[0].client_id, "CLI-0001")
    check("two orders", len(chs), 2)
    check("the chain holds all three attempts", chs[0].n, 3)
    check("and its quantity is counted ONCE", chs[0].size, 27_000_000)
    check("the other order is untouched", chs[1].size, 5_000_000)
    check("total qty is 32m, not 86m",
          sum(c.size for c in chs), 32_000_000)

    print("\nwhat the key is, and is not")
    check("a different basket does NOT split one client id",
          len(to_chains(to_attempts([
              _a(1, "TH", 100, "CLI-1", basket="B1", t=1),
              _a(2, "TH", 100, "CLI-1", basket="B2", t=2)])[0])), 1)
    check("nor does a different sym - it is REPORTED instead",
          len(to_chains(to_attempts([
              _a(1, "TH", 100, "CLI-1", sym="A.TB", t=1),
              _a(2, "TH", 100, "CLI-1", sym="B.TB", t=2)])[0])), 1)
    #  a trader can move an order to another order server; the two halves are
    #  still one order, which is why id_server is not in the key
    moved, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1, srv=1),
                            _a(2, "TH", 100, "CLI-1", t=2, srv=7)])
    mv = to_chains(moved)
    check("a trader moving the order server does NOT split it", len(mv), 1)
    check("and the run says it happened",
          len(chain_stats(moved, mv).multi_server), 1)
    check("which is not an error", len(chain_stats(moved, mv).mixed), 0)
    check("two untagged targets on different servers stay apart",
          len(to_chains(to_attempts([_a(1, "TH", 100, "", srv=1),
                                     _a(1, "TH", 100, "", srv=7)])[0])), 2)

    check("a different day IS a different order",
          len(to_chains(to_attempts([
              _a(1, "TH", 100, "CLI-1", d=dt.date(2026, 7, 1)),
              _a(2, "TH", 100, "CLI-1", d=dt.date(2026, 7, 2))])[0])), 2)

    print("\ntargets the client did not label")
    blank, _ = to_attempts([_a(1, "TH", 100, ""), _a(2, "TH", 700, ""),
                            _a(3, "TH", 300, "CLI-9")])
    bc = to_chains(blank)
    check("two untagged targets do NOT become one order", len(bc), 3)
    check("each keeps its own quantity",
          sorted(c.size for c in bc), [100, 300, 700])
    check("which is exactly what v1 would have said",
          sum(c.size for c in bc), 1100)

    print("\nwhich attempt sets the quantity")
    grew, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1),
                           _a(2, "TH", 250, "CLI-1", t=2)])
    check("first takes the original order",
          to_chains(grew, "first")[0].size, 100)
    check("last takes the order as it finally stood",
          to_chains(grew, "last")[0].size, 250)
    check("max takes the largest attempt",
          to_chains(grew, "max")[0].size, 250)
    shrank, _ = to_attempts([_a(1, "TH", 250, "CLI-1", t=1),
                             _a(2, "TH", 100, "CLI-1", t=2)])
    check("where a replace shrank it, the three differ",
          tuple(to_chains(shrank, q)[0].size
                for q in ("first", "last", "max")), (250, 100, 250))
    check("asked is the default", CHAIN_QTY, "asked")
    out_of_order, _ = to_attempts([_a(2, "TH", 100, "CLI-1", t=9),
                                   _a(1, "TH", 250, "CLI-1", t=1)])
    check("the last attempt is the last one SENT, not the first row seen",
          to_chains(out_of_order, "last")[0].size, 100)
    same_time, _ = to_attempts([_a(7, "TH", 100, "CLI-1", t=5),
                                _a(9, "TH", 300, "CLI-1", t=5)])
    check("a tied timestamp falls back to id_target",
          to_chains(same_time, "last")[0].size, 300)

    #  THE CASE THAT DECIDES IT.  Executed is summed over EVERY attempt, so the
    #  quantity has to be one that no combination of fills can exceed.
    print("\nwhat quantity a chain asked for")
    #  THE TWO KINDS OF ATTEMPT, and they pull opposite ways.
    #
    #  a TOP UP: 6103.JP off the live run - sizes 900, 1700, 2500 executing
    #  3,600 in total.  900 and 1700 finished; the last did 1,000 of 2,500.
    top, _ = to_attempts([_a(1, "JP", 900, "VFMAA4246", t=1),
                          _a(2, "JP", 1700, "VFMAA4246", t=2),
                          _a(3, "JP", 2500, "VFMAA4246", t=3)])
    tsp = to_splits([_c(1, 1, 900, "filled"),
                        _c(2, 2, 1700, "filled"),
                        _c(3, 3, 1000, "filled")], top)
    check("asked adds the top ups up",
          to_chains(top, "asked", tsp)[0].size, 5100)
    check("sum agrees here", to_chains(top, "sum", tsp)[0].size, 5100)
    check("max does NOT - this is the 144% on the live run",
          to_chains(top, "max", tsp)[0].size, 2500)

    #  a REPLACEMENT: Thailand, three sends of 27m that never traded
    rep, _ = to_attempts([_a(i, "TH", 27_000_000, "CLI-TH", t=i)
                          for i in (1, 2, 3)])
    rsp = to_splits([], rep)
    check("asked counts a rejected-and-replaced order ONCE",
          to_chains(rep, "asked", rsp)[0].size, 27_000_000)
    check("sum puts it straight back to v1's 81m",
          to_chains(rep, "sum", rsp)[0].size, 81_000_000)
    check("max is right here", to_chains(rep, "max", rsp)[0].size, 27_000_000)
    check("so only asked is right in BOTH",
          [q for q in ("asked", "sum", "max", "first", "last")
           if to_chains(top, q, tsp)[0].size == 5100
           and to_chains(rep, q, rsp)[0].size == 27_000_000], ["asked"])

    #  a remainder replace: 100 filling 30, replaced by 70 filling 70
    rem, _ = to_attempts([_a(1, "TH", 100, "CLI-R", t=1),
                          _a(2, "TH", 70, "CLI-R", t=2)])
    rmsp = to_splits([_c(1, 1, 30, "filled"), _c(2, 2, 70, "filled")],
                        rem)
    check("and on a remainder replace it is the original size",
          to_chains(rem, "asked", rmsp)[0].size, 100)
    check("a single attempt is just its size",
          to_chains(to_attempts([_a(1, "TH", 500, "CLI-S")])[0], "asked",
                    to_splits([_c(1, 1, 200, "filled")],
                                 to_attempts([_a(1, "TH", 500, "CLI-S")])[0])
                    )[0].size, 500)
    check("with nothing filled at all it is the last size",
          to_chains(rem, "asked", [])[0].size, 70)

    print("\ncompletion can never exceed 100%")
    #  partial fill, then a replace for the remainder: 30 of 100, then 70 of 70
    part, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1),
                           _a(2, "TH", 70, "CLI-1", t=2)])
    psp = to_splits([_c(1, 1, 30, "filled"), _c(2, 2, 70, "filled")],
                       part)
    for q, bad in (("first", 0), ("last", 1), ("max", 0)):
        check(f"remainder replace, CHAIN_QTY={q!r}: "
              f"{'OVER 100%' if bad else 'within 100%'}",
              len(over_filled(to_chains(part, q), psp)), bad)
    #  the other direction: the client GREW the order on the replace
    grow, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1),
                           _a(2, "TH", 150, "CLI-1", t=2)])
    gsp = to_splits([_c(1, 1, 100, "filled"), _c(2, 2, 50, "filled")],
                       grow)
    for q, bad in (("first", 1), ("last", 0), ("max", 0)):
        check(f"grown order, CHAIN_QTY={q!r}: "
              f"{'OVER 100%' if bad else 'within 100%'}",
              len(over_filled(to_chains(grow, q), gsp)), bad)
    check("so only max is safe in BOTH directions",
          [q for q in ("first", "last", "max")
           if not over_filled(to_chains(part, q), psp)
           and not over_filled(to_chains(grow, q), gsp)], ["max"])
    check("and the detector says by how much",
          [(round(100.0 * made / c.size), c.size)
           for c, made in over_filled(to_chains(part, "last"), psp)],
          [(143, 70)])
    check("a chain that fills exactly its quantity is not flagged",
          over_filled(to_chains(part, "max"), psp), [])
    check("nor is one that fills less",
          over_filled(to_chains(part, "max"),
                      to_splits([_c(1, 1, 10, "filled")], part)), [])
    #  asked is safe in every one of these BY CONSTRUCTION: quantity minus
    #  executed IS the last attempt's residual, which is never negative
    for name, at, sp in (("top ups", top, tsp), ("replacement", rep, rsp),
                         ("remainder", rem, rmsp), ("grown", grow, gsp),
                         ("partial", part, psp)):
        check(f"asked never exceeds 100% - {name}",
              over_filled(to_chains(at, "asked", sp), sp), [])

    print("\nand if one still does, it is un-chained")
    #  two targets of 100 each filling 80: the CHAIN over fills under max, but
    #  neither target does on its own - which is what un-chaining is for
    odd, _ = to_attempts([_a(1, "TH", 100, "CLI-X", t=1),
                          _a(2, "TH", 100, "CLI-X", t=2)])
    osp = to_splits([_c(1, 1, 80, "filled"), _c(2, 2, 80, "filled")],
                       odd)
    oc = to_chains(odd, "max", osp)
    over_odd = over_filled(oc, osp)
    check("the chain is over filled", len(over_odd), 1)
    check("at 160%", round(100 * over_odd[0][1] / over_odd[0][0].size), 160)
    un = unchain(oc, over_odd)
    check("un-chaining turns it back into its targets", len(un), 2)
    check("each with its own size", sorted(c.size for c in un), [100, 100])
    check("and nothing reads over 100% any more", over_filled(un, osp), [])
    check("which is exactly what v1 would have said",
          sum(c.size for c in un), sum(a.size for a in odd))
    check("a chain that is fine is left alone",
          len(unchain(to_chains(top, "asked", tsp), [])), 1)
    said_un = printed_err(report_unchained, over_odd, [])
    check("and the run says it happened", "UN-CHAINED" in said_un, True)
    check("naming how many targets", "2 targets" in said_un, True)

    print("\nthe tripwire that still works under asked")
    #  asked is defined as fills plus the last residual, so the CHAIN level
    #  check can never fire under it.  A rule that makes its own check vacuous
    #  needs another one, at the level where the anomaly actually is.
    solo, _ = to_attempts([_a(1, "TH", 100, "CLI-Y", t=1)])
    ssp = to_splits([_c(1, 1, 400, "filled")], solo)
    check("under asked the chain level check cannot fire, by construction",
          over_filled(to_chains(solo, "asked", ssp), ssp), [])
    check("but the target DID fill four times its size",
          [(a.id_target, made) for a, made in
           over_filled_attempts(solo, ssp)], [(1, 400)])
    said_att = printed_err(report_over_filled_attempts,
                           over_filled_attempts(solo, ssp))
    check("and the run says so, plainly",
          "not a grouping question" in said_att, True)
    check("naming the target and the percentage",
          ("id_target 1" in said_att and "400%" in said_att), True)
    check("a healthy session trips nothing",
          over_filled_attempts(top, tsp), [])
    check("nor does one where a target exactly fills",
          over_filled_attempts(*(lambda a: (a, to_splits(
              [_c(1, 1, 100, "filled")], a)))(
                  to_attempts([_a(1, "TH", 100, "CLI-Z")])[0])), [])
    check("it is independent of CHAIN_QTY - the anomaly is per target",
          all(len(over_filled_attempts(solo, ssp)) == 1
              for _q in ("asked", "sum", "max", "first", "last")), True)

    print("\norders that never produced a workorder")
    #  three orders: one pulled in 5s, one that sat for an hour with nothing
    #  sent, and one that worked normally
    nw, _ = to_attempts([_a(1, "TH", 1000, "CLI-A", t=1, live=5.0),
                         _a(2, "TH", 2000, "CLI-B", t=2, live=3600.0),
                         _a(3, "TH", 3000, "CLI-C", t=3, live=3600.0)])
    nwc = to_chains(nw)
    nws = to_splits([_c(1, 3, 1500, "filled")], nw)
    quick, slow = no_workorder(nwc, nws)
    check("the one pulled in seconds is found",
          [c.client_id for c in quick], ["CLI-A"])
    check("the one we sat on is found separately",
          [c.client_id for c in slow], ["CLI-B"])
    check("and the one that worked is neither",
          "CLI-C" not in [c.client_id for c in quick + slow], True)
    #  a REJECTED workorder is still a workorder: we sent something and the
    #  venue said no, which is the opposite of never having sent anything
    rej_sp = to_splits([_c(9, 2, 0, "rejected"),
                           _c(1, 3, 1500, "filled")], nw)
    check("a rejected workorder counts as having produced one",
          [c.client_id for c in no_workorder(nwc, rej_sp)[1]], [])
    check("leaving only the one pulled in seconds",
          [c.client_id for c in no_workorder(nwc, rej_sp)[0]], ["CLI-A"])

    th = [r for r in by_market(nwc, nws) if r.code == "TH"][0]
    check("all three are still IN the rollup - nothing is dropped", th.orders, 3)
    check("with their quantity", th.order_qty, 6000)
    check("and the two that did nothing drag completion, as they should",
          round(th.completion, 1), 25.0)

    said_nw = printed_err(report_no_workorder, quick, slow)
    check("the run discloses them", "never produced a workorder" in said_nw,
          True)
    check("says they are IN the numbers", "are IN the numbers" in said_nw, True)
    check("separates the pulled from the neglected",
          ("pulled before we had a chance" in said_nw
           and "WE sent nothing" in said_nw), True)
    check("and names the worst one", "XT.TB" in said_nw, True)
    check("a session with none says nothing at all",
          printed_err(report_no_workorder, [], []), "")

    print("\nseeing the untagged targets")

    def printed(fn, *a):
        buf = _i2.StringIO()
        with _ctx.redirect_stdout(buf):
            fn(*a)
        return buf.getvalue()

    ut = printed(dump_untagged, blank)
    check("--no-tag says how many and what share",
          "2 of 3 targets carry no tag 9604" in ut, True)
    check("and breaks it down by market", "Thailand 2" in ut, True)
    check("and lists them by id_target",
          all(str(a.id_target) in ut for a in blank if not a.client_id), True)
    check("largest first, so the ones that matter are at the top",
          ut.index("700") < ut.index("100"), True)
    check("a fully tagged session says so instead",
          "every target carries tag 9604" in printed(dump_untagged, att), True)

    print("\ncheck 1 - is tag 9604 populated")
    st = chain_stats(att, chs)
    check("it counts the attempts", st.attempts, 4)
    check("and the orders", st.chains, 2)
    check("and how many collapsed", st.multi, 1)
    check("and the longest chain", st.longest, 3)
    check("nothing missing in a fully tagged fixture", st.no_id, 0)
    stb = chain_stats(blank, bc)
    check("it counts the targets carrying no id", stb.no_id, 2)
    check("and says which market they were in", stb.no_id_by_market, {"TH": 2})
    none_tagged, _ = to_attempts([_a(1, "TH", 100, ""), _a(2, "JP", 100, "")])
    stn = chain_stats(none_tagged, to_chains(none_tagged))
    check("all of them missing is its own case", stn.no_id, stn.attempts)
    check("counted per market", stn.no_id_by_market, {"TH": 1, "JP": 1})

    print("\ncheck 2 - does one id cover two different orders")
    check("a clean chain disagrees on nothing", chs[0].disagrees_on(), [])
    for field, kw in (("sym", dict(sym="OTHER.TB")), ("side", dict(side="buy")),
                      ("algo", dict(algo="twap")),
                      ("basket", dict(basket="OTHER"))):
        mixed, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1),
                                _a(2, "TH", 100, "CLI-1", t=2, **kw)])
        mc = to_chains(mixed)
        check(f"a chain disagreeing on {field} is named",
              mc[0].disagrees_on(), [field])
        check(f"and reported as mixed", len(chain_stats(mixed, mc).mixed), 1)
    two_at_once, _ = to_attempts([
        _a(1, "TH", 100, "CLI-1", t=1),
        _a(2, "TH", 100, "CLI-1", t=2, sym="OTHER.TB", algo="twap")])
    check("both fields are named when both differ",
          to_chains(two_at_once)[0].disagrees_on(), ["sym", "algo"])
    check("a clean fixture reports no mixing", st.mixed, [])
    check("the size-mismatch line names every CHAIN_QTY without blowing up",
          all("differing size" in printed_err(
              lambda: report_stats(chain_stats(top, to_chains(top, q, tsp))))
              for q in ("asked", "sum", "max", "first", "last")), True)

    #  the two branches of each check are exclusive - a run that printed both
    #  the warning and the all-clear would be worse than one that printed
    #  neither, and an if/else is exactly what a line edit breaks
    import contextlib
    import io as _io

    def said(attempts, chains):
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            report_stats(chain_stats(attempts, chains))
        return buf.getvalue()

    clean = said(att, chs)
    check("a clean run says the tag is populated",
          "populated on every target" in clean, True)
    check("and does not warn", "WARNING" in clean, False)
    dirty = said(two_at_once, to_chains(two_at_once))
    check("a mixed run warns", "WARNING" in dirty, True)
    check("and does NOT also give the all clear",
          "no chain mixes" in dirty, False)
    untagged = said(none_tagged, to_chains(none_tagged))
    check("a wholly untagged run says so in the strongest terms",
          "NOT ONE of" in untagged, True)
    check("and does not claim the tag is populated",
          "populated on every target" in untagged, False)
    st3 = chain_stats(shrank, to_chains(shrank))
    check("a chain whose attempts disagree on SIZE is separate - a replace "
          "may legitimately resize", (len(st3.mixed), len(st3.mixed_size)),
          (0, 1))

    print("\nthe rollup")
    #  the live Thailand case, with its rejections
    att2, _ = to_attempts([
        _a(1, "TH", 27_000_000, "CLI-0001", t=1),
        _a(2, "TH", 27_000_000, "CLI-0001", t=2),
        _a(3, "TH", 27_000_000, "CLI-0001", t=3)])
    sp = to_splits([_c(10, 1, 0, "rejected"), _c(11, 2, 0, "rejected"),
                       _c(12, 3, 0, "cxl")], att2)
    chs2 = to_chains(att2)
    rows2 = by_market(chs2, sp)
    th = [r for r in rows2 if r.code == "TH"][0]
    check("one order, not three", th.orders, 1)
    check("27m, not 81m", th.order_qty, 27_000_000)
    check("EVERY rejection is still counted", th.rejections, 2)
    check("nothing executed", th.executed, 0)
    check("so completion is still zero", th.completion, 0.0)

    v1_rows = by_market_targets(att2, sp)
    v1_th = [r for r in v1_rows if r.code == "TH"][0]
    check("v1 over the same data still says three orders", v1_th.orders, 3)
    check("and 81m", v1_th.order_qty, 81_000_000)
    check("with the same rejections - that is the whole point",
          v1_th.rejections, th.rejections)

    print("\ncompare")
    lines = compare_lines(v1_rows, rows2)
    #  two header lines, a rule, the markets, a rule, the total, a blank and
    #  the note about what cannot differ
    check("it prints a row per market plus a total",
          len(lines), len(MARKETS) + 7)
    check("with both order counts on the Thailand row",
          [x for x in lines if x.startswith("Thailand")][0].split()[1:3],
          ["3", "1"])
    check("and says what cannot differ",
          "identical by construction" in lines[-1], True)
    check("a market with no orders prints an ASCII dash, not an em dash",
          [x for x in lines if x.startswith("Korea")][0].strip().endswith("-"),
          True)
    check("so the whole table survives a cp1252 console",
          all(x.encode("cp1252", "strict") is not None for x in lines), True)

    print("\nthe headline is the real fill rate")
    #  the day this whole thread started from
    mrows = [Row("HK", "Hong Kong", 572, 21_324_695, 10_879_995, 146),
             Row("JP", "Japan", 290, 2_594_047, 1_971_647, 53),
             Row("KR", "Korea", 56, 105_590, 40_557, 54),
             Row("MY", "Malaysia", 3, 202_900, 86_900, 9),
             Row("TH", "Thailand", 3, 81_000_000, 0, 7)]
    mt = totals(mrows)
    check("the market percentages",
          [round(r.completion, 1) for r in mrows], [51.0, 76.0, 38.4, 42.8, 0.0])
    check("the headline is summed executed over summed order qty",
          round(mt.completion, 6),
          round(100.0 * mt.executed / mt.order_qty, 6))
    check("the mean of the market rows would have said 41.7 instead",
          round(sum(r.completion for r in mrows) / len(mrows), 1), 41.7)
    check("a market with no orders contributes nothing either way",
          totals(mrows + [Row("XX", "Nowhere", 0, 0, 0, 0)]).completion,
          mt.completion)
    check("and all-empty has no headline at all",
          totals([Row("XX", "Nowhere", 0, 0, 0, 0)]).completion, None)
    check("size DOES decide it, which is the point: a big market at 10% "
          "beside a small one at 90% is 10.8, not 50",
          round(totals([Row("A", "A", 100, 1_000_000, 100_000, 0),
                        Row("B", "B", 1, 10_000, 9_000, 0)]).completion, 1),
          10.8)

    #  the headline above is only trustworthy because Thailand's 3 targets are
    #  ONE order.  With the double count still in it read 12.3%.
    check("with Thailand counted three times it was 12.3",
          round(mt.completion, 1), 12.3)
    chained = [r if r.code != "TH" else Row("TH", "Thailand", 1, 27_000_000,
                                            0, 7) for r in mrows]
    check("counted once, the same ratio is 25.3",
          round(totals(chained).completion, 1), 25.3)


    print("\nthe page")
    try:
        import matplotlib      # noqa: F401
    except ImportError:
        print("  ..    matplotlib not installed, rendering skipped")
        print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
        return 0 if ok else 1
    import io
    fig = draw(rows2, totals(rows2), "By market  ·  x", "Generated  ·  x")
    buf = io.BytesIO()
    fig.savefig(buf, format="pdf")
    check("the page renders", buf.getvalue()[:5], b"%PDF-")

    #  by_day needs dated rows - the realtime side has none, and skipping them
    #  is what keeps a realtime run from inventing a day
    day = dt.date(2026, 7, 1)
    dat, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1, d=day),
                          _a(2, "TH", 100, "CLI-1", t=2, d=day)])
    dsp = to_splits([_c(1, 2, 40, "filled", d=day)], dat)
    d1 = by_day(to_chains(dat), dsp)
    check("the day series works over chains too", len(d1), 1)
    check("one order that day, not two", d1[0].orders, 1)
    check("counted once", d1[0].order_qty, 100)
    check("with the day's completion off the chain", round(d1[0].completion), 40)
    check("undated rows are skipped, not invented", len(by_day(chs2, sp)), 0)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1

# =============================================================================
# CLI
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Short-Sell Order Report v2 - the same report, counting a "
                    "rejected-and-replaced order once. Servers and email are "
                    "configured in short_sell_report.py, which this imports.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--monthly", metavar="YYYY-MM")
    p.add_argument("--date", type=dt.date.fromisoformat, metavar="YYYY-MM-DD")
    p.add_argument("--chain-qty",
                   choices=("asked", "sum", "max", "first", "last"),
                   default=CHAIN_QTY,
                   help="what quantity a chain asked for. asked reads it off "
                        "the fills and cannot print over 100%%")
    p.add_argument("--keep-over", action="store_true",
                   help="do NOT un-chain the orders that still execute more "
                        "than they asked for - leave them chained, and let the "
                        "page read over 100%%")
    p.add_argument("--compare", action="store_true",
                   help="print v1 and v2 side by side over ONE fetch and exit, "
                        "so any difference is the counting and nothing else")
    p.add_argument("--chains", action="store_true",
                   help="list the chained orders and their attempts, and exit "
                        "- the way to check tag 9604 against the engine")
    p.add_argument("--no-tag", action="store_true",
                   help="list the targets carrying NO tag 9604, and exit - "
                        "they are counted as counting targets would, and this is how "
                        "to see which they are")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--quiet", action="store_true")
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
