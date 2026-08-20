#!/usr/bin/env python3
"""
=============================================================================
short_sell_report_v2.py

The Short-Sell Order Report, counting a REPLACED ORDER ONCE.

  python scripts/short_sell_report_v2/short_sell_report_v2.py
  python scripts/short_sell_report_v2/short_sell_report_v2.py --compare
  python scripts/short_sell_report_v2/short_sell_report_v2.py --chains
  python scripts/short_sell_report_v2/short_sell_report_v2.py --self-test

THE PROBLEM.  When an order is rejected and re-sent, the engine writes a NEW
id_target.  v1 counts target rows, so one economic order becomes several and
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
untagged orders apart - and is counted exactly as v1 counts it.

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

Everything else - the markets, the suffix routing, the Japan exclusion, what
counts as a rejection, the page, the mail - is IMPORTED FROM v1, not copied.
A second copy of a report is a report that drifts, and the point of this one
is that the ONLY difference is how orders are counted.

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
import datetime as dt
import sys
from pathlib import Path
from typing import NamedTuple, Optional

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parents[1]))                    # scripts/
sys.path.insert(0, str(_HERE.parents[1] / "short_sell_report"))

import short_sell_report as v1                               # noqa: E402
from lib.report_page import fmt_int, log                     # noqa: E402

# -----------------------------------------------------------------------------
# CONNECTIONS AND EMAIL.  v1's, deliberately: this reads the same servers and
# goes to the same people.  Edit them in short_sell_report.py, once.
# -----------------------------------------------------------------------------

OUT_DIR = _HERE.parent / "out"

TITLE = "Short-Sell Order Report"

# Which attempt's size is the chain's quantity.  Executed is summed over EVERY
# attempt's fills, so this decides what those fills are measured against.
#
#   "max"    the largest attempt asked for.  THE DEFAULT, and the only one that
#            cannot print a completion over 100%.
#   "first"  the original order.  Safe when a replace comes back for the
#            unfilled remainder; WRONG when a replace grows the order.
#   "last"   the order as it finally stood.  Safe when a replace grows it;
#            WRONG when it comes back for the remainder.
#
#   partial fill, replace for the remainder      replace GROWS the order
#     attempt 1  size 100  fills  30               attempt 1  size 100  fills 100
#     attempt 2  size  70  fills  70               attempt 2  size 150  fills  50
#     executed 100                                 executed 150
#       first 100%   last 143%   max 100%            first 150%   last 100%   max 100%
#
# Every run reports chains whose attempts differ in size, and separately any
# chain whose fills exceed its quantity - which should be impossible under
# "max" and is the tripwire if it is not.
CHAIN_QTY = "max"


# =============================================================================
# Q
#
# v1's query plus what the chain needs: fixmsg is already there for the Japan
# exclusion and carries tag 9604; oes_oid, basket, side and algo are for the
# consistency checks, and time orders a chain's attempts.  Nothing else differs
# - same suffix filter, same side filter, same refusal to group anything.
# =============================================================================

Q_SESSION = """
{[hist;d;sfx;sside]
  sside:`$sside;
  et:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; sym:0#`; size:0#0i;
         fixmsg:0#`; oes_oid:0#`; basket:0#`; side:0#`; algo:0#`;
         time:0#0Nt);
  ew:([] date:0#0Nd; id_server:0#0i; id_work:0#0i; id_target:0#0i; make:0#0i;
         state:0#`);

  t:$[hist;
      select date,id_server,id_target,sym,size,fixmsg,oes_oid,basket,side,
          algo,time
        from target where date=d, side=sside, any (upper sym) like/: sfx;
      update date:0Nd from select id_server,id_target,sym,size,fixmsg,oes_oid,
          basket,side,algo,time
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
    sfx = [p.encode() for p in v1.SYM_PATTERNS]
    t, w = handle(Q_SESSION, hist, d if d is not None else v1._UNUSED_DATE,
                  sfx, v1.SHORTSELL_SIDE.encode())
    return t.pd().to_dict("records"), w.pd().to_dict("records")


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
    txt = v1._s(fixmsg)
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
    key: tuple                 # (date, id_server, id_target) - v1's parent key
    date: Optional[dt.date]
    country: str
    sym: str
    size: int
    client_id: str             # FIX tag 9604 - "" when the client sent none
    oes_oid: str               # not the key, just context for --chains
    basket: str
    side: str
    algo: str
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
    """(attempts, restricted_dropped).  v1's rules, unchanged: out of scope
    markets and Japan's restricted names are dropped before anything counts."""
    out, dropped = [], 0
    for r in records:
        sym = v1._s(r.get("sym"))
        country = v1.market_of(sym)
        if country is None:
            continue
        if v1.is_restricted(country, r.get("fixmsg")):
            dropped += 1
            continue
        d = v1._d(r.get("date"))
        idt = v1._i(r.get("id_target"))
        out.append(Attempt(
            key=(d, v1._i(r.get("id_server")), idt), date=d, country=country,
            sym=sym, size=abs(v1._i(r.get("size"))),
            client_id=fix_tag(r.get("fixmsg")),
            oes_oid=v1._s(r.get("oes_oid")), basket=v1._s(r.get("basket")),
            side=v1._s(r.get("side")) or v1.SHORTSELL_SIDE,
            algo=v1._s(r.get("algo")),
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


def to_chains(attempts, qty=None) -> list:
    """Collapse attempts into orders on the client's id.

    Ordered by (time, id_target) so "the last attempt" is the last one SENT,
    with the id as the tie break - two attempts can share a timestamp, and the
    id is monotonic where the clock is only nearly so.
    """
    qty = qty or CHAIN_QTY
    groups = {}
    for a in attempts:
        groups.setdefault(a.chain_key, []).append(a)

    out = []
    for k, got in groups.items():
        got = sorted(got, key=lambda a: (a.seq, a.id_target))
        last = got[-1]
        if qty == "max":
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
            f"tag {CLIENT_ID_TAG} and stand alone, as v1 counts them: {worst}")
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
        which = {"max": "the largest", "first": "the first",
                 "last": "the last"}[CHAIN_QTY]
        log(f"  {len(st.mixed_size):,} chains have attempts of differing size "
            f"- a replace resized the order; CHAIN_QTY={CHAIN_QTY!r} takes "
            f"{which}. --chains")
    return st


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

    They are counted on the page - each stands alone, exactly as v1 counts it -
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
    print("  " + ", ".join(f"{v1.MARKET_NAME.get(k, k)} {n:,}"
                           for k, n in sorted(by_mkt.items(),
                                              key=lambda kv: -kv[1])))
    print(f"\neach stands alone and is counted exactly as v1 counts it"
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


def draw(rows, tot, subtitle, foot, days=None):
    """v1's page, under v2's title.

    The title is set by swapping v1's module global for the duration of the
    call, NOT by passing a keyword to v1.draw().  v1 is the file that has to be
    EDITED - the servers and the mail live in it - so a copy of it in the wild
    is very often not the copy in git, and v2 must not need a particular
    signature from it.  A module global has been there since the first version.

    This is also why nothing else here calls into v1 with keywords it has not
    always had.
    """
    old = getattr(v1, "TITLE", None)
    try:
        v1.TITLE = TITLE
        return v1.draw(rows, tot, subtitle, foot, days)
    finally:
        if old is not None:
            v1.TITLE = old


def by_market(chs, splits) -> list:
    orders = {c: 0 for c in v1.MARKET_CODES}
    qty = {c: 0 for c in v1.MARKET_CODES}
    made = {c: 0 for c in v1.MARKET_CODES}
    rej = {c: 0 for c in v1.MARKET_CODES}
    for c in chs:
        orders[c.country] += 1
        qty[c.country] += c.size
    for s in splits:
        made[s.country] += s.make
        if s.rejected:
            rej[s.country] += 1
    return [v1.Row(m.code, m.name, orders[m.code], qty[m.code],
                   made[m.code], rej[m.code]) for m in v1.MARKETS]


def by_day(chs, splits) -> list:
    """One DayRow per date, completion the mean of that day's markets - the same
    measure v1 uses, over chains instead of targets."""
    days, mkt = {}, {}

    def slot(d):
        mkt.setdefault(d, {})
        return days.setdefault(d, [0, 0, 0, 0])

    def cell(d, m):
        return mkt[d].setdefault(m, [0, 0])

    for c in chs:
        if c.date is None:
            continue
        e = slot(c.date)
        e[0] += 1
        e[1] += c.size
        cell(c.date, c.country)[0] += c.size
    for s in splits:
        if s.date is None:
            continue
        e = slot(s.date)
        e[2] += s.make
        if s.rejected:
            e[3] += 1
        cell(s.date, s.country)[1] += s.make
    return [v1.DayRow(d, days[d][0], days[d][1], days[d][2], days[d][3],
                      v1._mean(v1._completion(e, q)
                               for q, e in mkt[d].values()))
            for d in sorted(days)]


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
    t1, t2 = v1.totals(v1_rows), v1.totals(v2_rows)
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
# RUN
# =============================================================================

def run(args) -> int:
    pl = v1.plan(args.monthly, args.date)
    v1._check_server(pl.endpoint, pl.endpoint_name)
    log(f"short_sell_report_v2  {'historical' if pl.hist else 'realtime'}  "
        f"{pl.endpoint}")
    h = v1.connect(pl.endpoint)

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
        splits.extend(v1.to_splits(wr, att))       # keyed on the target rows

    if args.no_tag:
        return dump_untagged(attempts)

    chs = to_chains(attempts, args.chain_qty)
    st = report_stats(chain_stats(attempts, chs), args.quiet)
    report_over_filled(over_filled(chs, splits))
    if args.chains:
        return dump_chains(chs)

    rows = by_market(chs, splits)
    tot = v1.totals(rows)
    days = by_day(chs, splits) if pl.monthly else None

    if args.compare:
        v1_rows = v1.by_market(
            [v1.Parent(key=a.key, date=a.date, country=a.country, sym=a.sym,
                       size=a.size) for a in attempts], splits)
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
    files = v1.save(fig, Path(args.out_dir), pl.stem.replace(
        "short_sell_report", "short_sell_report_v2"))
    if v1.email_configured():
        v1.mail_report(pl.when, files)
    return 0


# =============================================================================
# SELF TEST
# =============================================================================

def _a(idt, country, size, cid, basket="B1", side="sellshort",
       algo="vwap", t=0.0, d=None, srv=1, sym=None, extra=""):
    """One target row, as q returns it.  cid goes into fixmsg as tag 9604, the
    way the client actually sends it - so the fixture exercises the PARSE, not
    just the grouping."""
    r = v1._p(idt, country, size, d=d, srv=srv)
    if sym:
        r["sym"] = sym
    #  built the way the real feed does: semicolon separated, with a caret
    #  bearing field beside it so the parser is exercised against both
    fix = "8=FIX.4.2;35=D;9012=274=1^275=1;16589=108223;"
    if cid:
        fix += f"{CLIENT_ID_TAG}={cid};"
    r.update({"fixmsg": fix + extra + "17717=7280001184;59=0",
              "oes_oid": f"OID.{idt}", "basket": basket, "side": side,
              "algo": algo, "time": dt.timedelta(seconds=t)})
    return r


def self_test() -> int:
    ok = True

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
    check("max is the default", CHAIN_QTY, "max")
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
    print("\ncompletion can never exceed 100%")
    #  partial fill, then a replace for the remainder: 30 of 100, then 70 of 70
    part, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1),
                           _a(2, "TH", 70, "CLI-1", t=2)])
    psp = v1.to_splits([v1._c(1, 1, 30, "filled"), v1._c(2, 2, 70, "filled")],
                       part)
    for q, bad in (("first", 0), ("last", 1), ("max", 0)):
        check(f"remainder replace, CHAIN_QTY={q!r}: "
              f"{'OVER 100%' if bad else 'within 100%'}",
              len(over_filled(to_chains(part, q), psp)), bad)
    #  the other direction: the client GREW the order on the replace
    grow, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1),
                           _a(2, "TH", 150, "CLI-1", t=2)])
    gsp = v1.to_splits([v1._c(1, 1, 100, "filled"), v1._c(2, 2, 50, "filled")],
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
                      v1.to_splits([v1._c(1, 1, 10, "filled")], part)), [])

    print("\nseeing the untagged targets")
    import contextlib as _c
    import io as _i2

    def printed(fn, *a):
        buf = _i2.StringIO()
        with _c.redirect_stdout(buf):
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
    sp = v1.to_splits([v1._c(10, 1, 0, "rejected"), v1._c(11, 2, 0, "rejected"),
                       v1._c(12, 3, 0, "cxl")], att2)
    chs2 = to_chains(att2)
    rows2 = by_market(chs2, sp)
    th = [r for r in rows2 if r.code == "TH"][0]
    check("one order, not three", th.orders, 1)
    check("27m, not 81m", th.order_qty, 27_000_000)
    check("EVERY rejection is still counted", th.rejections, 2)
    check("nothing executed", th.executed, 0)
    check("so completion is still zero", th.completion, 0.0)

    v1_rows = v1.by_market(
        [v1.Parent(key=a.key, date=a.date, country=a.country, sym=a.sym,
                   size=a.size) for a in att2], sp)
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
          len(lines), len(v1.MARKETS) + 7)
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

    print("\nreuse")
    for name in ("MARKETS", "market_of", "is_restricted", "is_rejected",
                 "to_splits", "totals", "draw", "save", "plan", "connect",
                 "mail_report", "Row", "DayRow"):
        check(f"{name} comes from v1, not a copy", hasattr(v1, name), True)
    check("v1's own checks still pass over its own fixture",
          v1.by_market(*(lambda p: (p, v1.to_splits([], p)))(
              v1.to_parents([v1._p(1, "HK", 100)])[0]))[0].orders, 1)

    print("\nthe page")
    try:
        import matplotlib      # noqa: F401
    except ImportError:
        print("  ..    matplotlib not installed, rendering skipped")
        print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
        return 0 if ok else 1
    import io
    fig = draw(rows2, v1.totals(rows2), "By market  ·  x", "Generated  ·  x")
    buf = io.BytesIO()
    fig.savefig(buf, format="pdf")
    check("it renders on v1's page", buf.getvalue()[:5], b"%PDF-")
    check("v1's own title is put back afterwards",
          v1.TITLE, "Short-Sell Order Report")

    #  A v1 IN THE WILD IS OFTEN NOT THE v1 IN GIT: it is the file that has to
    #  be edited for the servers and the mail.  Prove the page still draws
    #  against one whose draw() predates any keyword v2 might have wanted.
    real_draw = v1.draw

    def old_draw(rows_, tot_, subtitle_, footer_, days_=None):
        return real_draw(rows_, tot_, subtitle_, footer_, days_)

    try:
        v1.draw = old_draw
        buf_old = io.BytesIO()
        draw(rows2, v1.totals(rows2), "x", "y").savefig(buf_old, format="pdf")
        check("and against a v1 whose draw() takes no title at all",
              buf_old.getvalue()[:5], b"%PDF-")
    finally:
        v1.draw = real_draw
    check("with v1 left exactly as it was found", v1.draw, real_draw)

    #  by_day needs dated rows - the realtime side has none, and skipping them
    #  is what keeps a realtime run from inventing a day
    day = dt.date(2026, 7, 1)
    dat, _ = to_attempts([_a(1, "TH", 100, "CLI-1", t=1, d=day),
                          _a(2, "TH", 100, "CLI-1", t=2, d=day)])
    dsp = v1.to_splits([v1._c(1, 2, 40, "filled", d=day)], dat)
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
    p.add_argument("--chain-qty", choices=("max", "first", "last"),
                   default=CHAIN_QTY,
                   help="which attempt's size is the chain's quantity. max is "
                        "the only one that cannot print a completion over 100%%")
    p.add_argument("--compare", action="store_true",
                   help="print v1 and v2 side by side over ONE fetch and exit, "
                        "so any difference is the counting and nothing else")
    p.add_argument("--chains", action="store_true",
                   help="list the chained orders and their attempts, and exit "
                        "- the way to check tag 9604 against the engine")
    p.add_argument("--no-tag", action="store_true",
                   help="list the targets carrying NO tag 9604, and exit - "
                        "they are counted as v1 counts them, and this is how "
                        "to see which they are")
    p.add_argument("--out-dir", default=str(OUT_DIR))
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--self-test", action="store_true",
                   help="run the offline checks and exit - no kdb needed")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.monthly and args.date:
        p.error("--monthly and --date are alternatives, not a range")
    if args.date and args.date > dt.date.today():
        p.error(f"--date {args.date} is in the future")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
