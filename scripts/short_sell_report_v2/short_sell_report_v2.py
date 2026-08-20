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

THE CHAIN KEY.  oes_primoid is always empty, so the link is the oes_oid STEM -
the id with its last dot component removed, which is the attempt number:

    SCB-R.TB.APPD2.1w519.2p5   ->  stem SCB-R.TB.APPD2.1w519   attempt 2p5
    SCB-R.TB.APPD2.1w519.3p1   ->  stem SCB-R.TB.APPD2.1w519   attempt 3p1

which is the q the user already had:  {"." sv -1 _ "." vs string x} each oes_oid

Chained on (date, id_server, STEM, BASKET).

Two orders in different baskets can share a stem, so the stem alone is not an
order - the basket is what makes it one.  Side is NOT in the key: this whole
report is one side, so it could never separate two rows here.

THE RULE IS TESTED RATHER THAN INSURED AGAINST.  The key is the user's, exactly
as given, and the run then checks what that key produced:

  split stems   stems shared by more than one order, which the basket pulled
                apart.  Informational - it is the measure of how load bearing
                the basket is.  Zero means the stem was unique anyway.
  MIXED CHAINS  a chain holding more than one sym, or more than one side.  A
                WARNING: it means stem + basket was not enough after all, and
                those orders have been merged when they should not have been.
                It must be zero, and --chains lists them.

Adding sym to the key would make that warning impossible to trigger, which
would hide the answer rather than give it.

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

VALIDATE BEFORE TRUSTING IT.  Two things are assumed and neither is proven:

  1. that the stem and the basket together are an order.  Every run reports
     MIXED CHAINS - chains that ended up holding more than one sym or side -
     which must be zero, and split stems, which is how much work the basket is
     doing.  --chains lists both.
  2. that the chain's quantity is the LAST attempt's size.  CHAIN_QTY = "max"
     if a replace can shrink an order to its unfilled remainder, in which case
     summing fills across attempts against the last size would overstate
     completion.  Every run reports how many chains had attempts of differing
     size - if that is zero, the choice does not matter.

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

# Which attempt's size is the chain's quantity.
#   "last"  the order as it finally stood.  Right when a replace re-sends the
#           same quantity, which is what a reject-and-replace does.
#   "max"   the largest attempt.  Right if a replace can come back for only the
#           unfilled remainder, where fills from earlier attempts would
#           otherwise be measured against a smaller order.
# Every run reports how many chains had attempts of differing size.  While that
# is zero the two are identical and this does not matter.
CHAIN_QTY = "last"


# =============================================================================
# Q
#
# v1's query plus the three columns the chain key needs: oes_oid, basket and
# time.  Nothing else differs - same suffix filter, same side filter, same
# refusal to group anything.
# =============================================================================

Q_SESSION = """
{[hist;d;sfx;sside]
  sside:`$sside;
  et:([] date:0#0Nd; id_server:0#0i; id_target:0#0i; sym:0#`; size:0#0i;
         fixmsg:0#`; oes_oid:0#`; basket:0#`; side:0#`; time:0#0Nt);
  ew:([] date:0#0Nd; id_server:0#0i; id_work:0#0i; id_target:0#0i; make:0#0i;
         state:0#`);

  t:$[hist;
      select date,id_server,id_target,sym,size,fixmsg,oes_oid,basket,side,
          time
        from target where date=d, side=sside, any (upper sym) like/: sfx;
      update date:0Nd from select id_server,id_target,sym,size,fixmsg,oes_oid,
          basket,side,time
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

def oid_stem(oes_oid) -> str:
    """The oes_oid with its last dot component - the attempt - removed.

    `SCB-R.TB.APPD2.1w519.2p5` -> `SCB-R.TB.APPD2.1w519`.  An id with no dot has
    no attempt to strip and is its own stem, which makes it a chain of one - the
    safe reading, since it can only ever fail to collapse something.
    """
    s = v1._s(oes_oid)
    i = s.rfind(".")
    return s[:i] if i > 0 else s


class Attempt(NamedTuple):
    """One target row: one send of an order."""
    key: tuple                 # (date, id_server, id_target) - v1's parent key
    date: Optional[dt.date]
    country: str
    sym: str
    size: int
    stem: str
    basket: str
    side: str
    seq: float                 # target `time`, to find the last attempt
    id_target: int

    @property
    def chain_key(self) -> tuple:
        """What makes an order: the oes_oid prefix and the basket.

        The stem is not enough on its own - two orders in different baskets can
        share one.  sym and side are deliberately NOT here: keeping them out is
        what lets a chain holding two of either be DETECTED, which is the check
        that this key is right.
        """
        return (self.date, self.key[1], self.stem, self.basket)


class Chain(NamedTuple):
    """One order, however many times it was sent."""
    chain_key: tuple
    date: Optional[dt.date]
    country: str
    sym: str
    side: str
    basket: str
    size: int
    attempts: tuple            # every Attempt, in order

    @property
    def n(self) -> int:
        return len(self.attempts)

    @property
    def keys(self) -> set:
        return {a.key for a in self.attempts}


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
            stem=oid_stem(r.get("oes_oid")), basket=v1._s(r.get("basket")),
            side=v1._s(r.get("side")) or v1.SHORTSELL_SIDE,
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
    """Collapse attempts into orders.

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
        size = max(a.size for a in got) if qty == "max" else last.size
        out.append(Chain(chain_key=k, date=last.date, country=last.country,
                         sym=last.sym, side=last.side, basket=last.basket,
                         size=size, attempts=tuple(got)))
    return sorted(out, key=lambda c: (c.attempts[0].seq, c.attempts[0].id_target))


# =============================================================================
# VALIDATION
#
# Two assumptions, both reported on every run rather than trusted:
# the stem groups one order and nothing else, and the chain's quantity is the
# last attempt's size.
# =============================================================================

class ChainStats(NamedTuple):
    attempts: int
    chains: int
    multi: int                 # chains of more than one attempt
    longest: int
    mixed: list                # chains holding >1 sym or >1 side - a FAILURE
    split_stems: list          # [(stem, [chains])] - stems shared by >1 order
    mixed_size: list           # chains whose attempts disagree on size
    no_oid: int                # attempts with no oes_oid at all


def chain_stats(attempts, chs) -> ChainStats:
    """What the run should say about its own assumptions.

    `mixed` is the one that matters: a chain holding more than one sym or side
    means stem + basket merged two orders that are not the same order, and the
    numbers are wrong.  It is only detectable BECAUSE sym and side are kept out
    of the key - putting them in would make the key silently right-looking.

    `split_stems` is the other direction and is not an error: stems that belong
    to more than one order, which the basket pulled apart.  It says how much
    work the basket is doing.
    """
    by_stem = {}
    for c in chs:
        by_stem.setdefault(c.chain_key[2], []).append(c)
    return ChainStats(
        attempts=len(attempts), chains=len(chs),
        multi=sum(1 for c in chs if c.n > 1),
        longest=max([c.n for c in chs], default=0),
        mixed=[c for c in chs
               if len({a.sym for a in c.attempts}) > 1
               or len({a.side for a in c.attempts}) > 1],
        split_stems=[(k, v) for k, v in by_stem.items() if len(v) > 1 and k],
        mixed_size=[c for c in chs if len({a.size for a in c.attempts}) > 1],
        no_oid=sum(1 for a in attempts if not a.stem))


def report_stats(st: ChainStats, quiet=False):
    log(f"  chains: {st.attempts:,} targets -> {st.chains:,} orders "
        f"({st.multi:,} chained, longest {st.longest})")
    if st.no_oid:
        log(f"  NOTE: {st.no_oid:,} targets have no oes_oid - each is its own "
            f"chain, so they are counted exactly as v1 counts them")
    if st.mixed:
        log(f"  WARNING: {len(st.mixed):,} chain"
            f"{'' if len(st.mixed) == 1 else 's'} hold more than one sym or "
            f"side. stem + basket has merged orders that are NOT the same "
            f"order and these numbers are WRONG.  --chains lists them")
    if st.split_stems:
        n = sum(len(v) for _k, v in st.split_stems)
        log(f"  {len(st.split_stems):,} oes_oid stem"
            f"{'' if len(st.split_stems) == 1 else 's'} shared by {n:,} "
            f"different orders - the basket kept them apart; a stem-only key "
            f"would have merged them.  --chains lists them")
    if st.mixed_size:
        log(f"  NOTE: {len(st.mixed_size):,} chains have attempts of differing "
            f"size; CHAIN_QTY={CHAIN_QTY!r} takes the "
            f"{'largest' if CHAIN_QTY == 'max' else 'last'}. --chains")
    return st


def dump_chains(chs, limit=40):
    """The multi attempt chains, and the stems that turned out to hold more than
    one order.  Both are for eyeballing against the engine."""
    st = chain_stats([a for c in chs for a in c.attempts], chs)

    multi = [c for c in chs if c.n > 1]
    if not multi:
        print("no chained orders: every target stands alone")
    else:
        print(f"{len(multi):,} chained orders"
              + (f", showing the first {limit}" if len(multi) > limit else ""))
        for c in multi[:limit]:
            if len({a.sym for a in c.attempts}) > 1 \
                    or len({a.side for a in c.attempts}) > 1:
                flag = "   <-- MIXED, stem + basket is not enough here"
            elif len({a.size for a in c.attempts}) > 1:
                flag = "   <-- sizes differ across attempts"
            else:
                flag = ""
            print(f"\n  {c.sym}  {c.country}  {c.side}  "
                  f"stem {c.chain_key[2]}  basket {c.basket or '-'}"
                  f"  -> qty {c.size:,}{flag}")
            for a in c.attempts:
                print(f"      id_target {a.id_target:<12} size {a.size:>14,}  "
                      f"t {a.seq:>9.0f}")

    if st.mixed:
        print(f"\n{len(st.mixed):,} chains hold more than one sym or side - "
              f"stem + basket MERGED orders that are not the same order:")
        for c in st.mixed[:limit]:
            print(f"\n  stem {c.chain_key[2]}  basket {c.basket or '-'}")
            for a in c.attempts:
                print(f"      id_target {a.id_target:<12} {a.sym:<14} "
                      f"{a.side:<10} size {a.size:>14,}")

    if st.split_stems:
        print(f"\n{len(st.split_stems):,} stems held more than one order - "
              f"this is what the basket is in the key FOR:")
        for stem, got in st.split_stems[:limit]:
            print(f"\n  stem {stem}")
            for c in got:
                print(f"      {c.sym:<14} {c.side:<10} basket "
                      f"{c.basket or '-':<10} qty {c.size:>14,}  "
                      f"{c.n} attempt{'' if c.n == 1 else 's'}")
    return 0


# =============================================================================
# ROLLUP
#
# The only arithmetic that differs from v1: orders and qty come from the
# chains, executed and rejections from the same workorder rows v1 uses.
# =============================================================================

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

    chs = to_chains(attempts, args.chain_qty)
    st = report_stats(chain_stats(attempts, chs), args.quiet)
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
            + f"  ·  {st.attempts:,} targets chained into {st.chains:,} orders")
    if dropped:
        foot += f"  ·  {dropped:,} restricted JP excluded"

    fig = v1.draw(rows, tot, subtitle, foot, days, title=TITLE)
    files = v1.save(fig, Path(args.out_dir), pl.stem.replace(
        "short_sell_report", "short_sell_report_v2"))
    if v1.email_configured():
        v1.mail_report(pl.when, files)
    return 0


# =============================================================================
# SELF TEST
# =============================================================================

def _a(idt, country, size, oid, basket="B1", side="sellshort", t=0.0,
       d=None, srv=1):
    """One target row, as q returns it."""
    r = v1._p(idt, country, size, d=d, srv=srv)
    r.update({"oes_oid": oid, "basket": basket, "side": side,
              "time": dt.timedelta(seconds=t)})
    return r


def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("short_sell_report_v2 --self-test\n\nthe oes_oid stem")
    check("the attempt is the last dot component",
          oid_stem("SCB-R.TB.APPD2.1w519.2p5"), "SCB-R.TB.APPD2.1w519")
    check("a second attempt has the same stem",
          oid_stem("SCB-R.TB.APPD2.1w519.3p1"), "SCB-R.TB.APPD2.1w519")
    check("and the screenshot's other id",
          oid_stem("1.HK.APPD.1w51b.7"), "1.HK.APPD.1w51b")
    check("a different order does NOT share it",
          oid_stem("SCB-R.TB.APPD2.1w520.2p5"), "SCB-R.TB.APPD2.1w520")
    check("an id with no dot is its own chain", oid_stem("ABC"), "ABC")
    check("an empty id stays empty", oid_stem(""), "")
    check("it is the same rule as the q",
          oid_stem("a.b.c.d"), ".".join("a.b.c.d".split(".")[:-1]))

    print("\nchaining")
    #  three sends of one 27m Thai order, then a different order
    att, _ = to_attempts([
        _a(1, "TH", 27_000_000, "SCB-R.TB.APPD2.1w519.1p0", t=1),
        _a(2, "TH", 27_000_000, "SCB-R.TB.APPD2.1w519.2p5", t=2),
        _a(3, "TH", 27_000_000, "SCB-R.TB.APPD2.1w519.3p1", t=3),
        _a(4, "TH", 5_000_000, "SCB-R.TB.APPD2.1w520.1p0", t=4)])
    chs = to_chains(att)
    check("four targets", len(att), 4)
    check("two orders", len(chs), 2)
    check("the chain holds all three attempts", chs[0].n, 3)
    check("and its quantity is counted ONCE", chs[0].size, 27_000_000)
    check("the standalone order is untouched", chs[1].size, 5_000_000)
    check("total qty is 32m, not 86m",
          sum(c.size for c in chs), 32_000_000)

    print("\nwhat the key is made of")
    diff_side, _ = to_attempts([_a(1, "TH", 100, "X.TB.A.1.1p0", basket="B1"),
                               _a(2, "TH", 100, "X.TB.A.1.2p0", basket="B2")])
    check("a different basket is a different order",
          len(to_chains(diff_side)), 2)
    check("side is NOT in the key - this whole report is one side",
          len(to_chains(to_attempts([
              _a(1, "TH", 100, "X.TB.A.1.1p0", side="sellshort"),
              _a(2, "TH", 100, "X.TB.A.1.2p0", side="buy")])[0])), 1)
    two_days, _ = to_attempts([_a(1, "TH", 100, "X.TB.A.1.1p0",
                                  d=dt.date(2026, 7, 1)),
                               _a(2, "TH", 100, "X.TB.A.1.2p0",
                                  d=dt.date(2026, 7, 2))])
    check("and so is a different day", len(to_chains(two_days)), 2)

    print("\nwhich attempt sets the quantity")
    grew, _ = to_attempts([_a(1, "TH", 100, "X.TB.A.1.1p0", t=1),
                           _a(2, "TH", 250, "X.TB.A.1.2p0", t=2)])
    check("last takes the order as it finally stood",
          to_chains(grew, "last")[0].size, 250)
    check("max takes the largest attempt",
          to_chains(grew, "max")[0].size, 250)
    shrank, _ = to_attempts([_a(1, "TH", 250, "X.TB.A.1.1p0", t=1),
                             _a(2, "TH", 100, "X.TB.A.1.2p0", t=2)])
    check("where a replace shrank it, last and max differ",
          (to_chains(shrank, "last")[0].size, to_chains(shrank, "max")[0].size),
          (100, 250))
    out_of_order, _ = to_attempts([_a(2, "TH", 100, "X.TB.A.1.2p0", t=9),
                                   _a(1, "TH", 250, "X.TB.A.1.1p0", t=1)])
    check("the last attempt is the last one SENT, not the first row seen",
          to_chains(out_of_order, "last")[0].size, 100)
    same_time, _ = to_attempts([_a(7, "TH", 100, "X.TB.A.1.a", t=5),
                                _a(9, "TH", 300, "X.TB.A.1.b", t=5)])
    check("a tied timestamp falls back to id_target",
          to_chains(same_time, "last")[0].size, 300)

    print("\nvalidation")
    st = chain_stats(att, chs)
    check("it counts the attempts", st.attempts, 4)
    check("and the orders", st.chains, 2)
    check("and how many collapsed", st.multi, 1)
    check("and the longest chain", st.longest, 3)
    check("nothing shared, mixed or resized in a clean fixture",
          (st.mixed, st.split_stems, st.mixed_size), ([], [], []))

    #  THE CASE THE USER RAISED: one stem, two baskets, two real orders
    shared, _ = to_attempts([
        _a(1, "TH", 100, "X.TB.A.1.1p0", basket="ALPHA", t=1),
        _a(2, "TH", 100, "X.TB.A.1.2p0", basket="ALPHA", t=2),
        _a(3, "TH", 700, "X.TB.A.1.1p0", basket="BETA", t=1)])
    sh = to_chains(shared)
    check("a shared stem does NOT merge two baskets", len(sh), 2)
    check("each keeps its own quantity",
          sorted(c.size for c in sh), [100, 700])
    check("and the run says the stem was shared",
          len(chain_stats(shared, sh).split_stems), 1)
    check("naming which stem",
          chain_stats(shared, sh).split_stems[0][0], "X.TB.A.1")

    #  THE RULE IS TESTED, NOT INSURED AGAINST.  sym and side are kept OUT of
    #  the key on purpose, so that stem+basket merging two orders that are not
    #  the same order is something the run can SEE and complain about.  Putting
    #  them in would make the key look right by construction and say nothing.
    two_syms, _ = to_attempts([_a(1, "TH", 100, "X.TB.A.1.1p0", t=1),
                               _a(2, "TH", 100, "X.TB.A.1.2p0", t=2)])
    two_syms = [two_syms[0], two_syms[1]._replace(sym="OTHER.TB")]
    st2 = chain_stats(two_syms, to_chains(two_syms))
    check("two syms on one stem and basket DO merge - that is the rule",
          len(to_chains(two_syms)), 1)
    check("and the run warns that they should not have", len(st2.mixed), 1)

    two_sides, _ = to_attempts([
        _a(1, "TH", 100, "X.TB.A.1.1p0", side="sellshort", t=1),
        _a(2, "TH", 100, "X.TB.A.1.2p0", side="buy", t=2)])
    check("two sides likewise merge",
          len(to_chains(two_sides)), 1)
    check("and are likewise warned about",
          len(chain_stats(two_sides, to_chains(two_sides)).mixed), 1)
    check("a clean chain raises no warning", len(st.mixed), 0)
    st3 = chain_stats(shrank, to_chains(shrank))
    check("so is a chain whose attempts disagree on size",
          len(st3.mixed_size), 1)
    noid, _ = to_attempts([_a(1, "TH", 100, ""), _a(2, "TH", 100, "")])
    check("targets with no oes_oid are counted", chain_stats(noid,
          to_chains(noid)).no_oid, 2)

    print("\nthe rollup")
    #  the live Thailand case, with its rejections
    att2, _ = to_attempts([
        _a(1, "TH", 27_000_000, "SCB-R.TB.A.1.1p0", t=1),
        _a(2, "TH", 27_000_000, "SCB-R.TB.A.1.2p0", t=2),
        _a(3, "TH", 27_000_000, "SCB-R.TB.A.1.3p0", t=3)])
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
    fig = v1.draw(rows2, v1.totals(rows2), "By market  ·  x",
                  "Generated  ·  x", title=TITLE)
    buf = io.BytesIO()
    fig.savefig(buf, format="pdf")
    check("it renders on v1's page", buf.getvalue()[:5], b"%PDF-")

    #  by_day needs dated rows - the realtime side has none, and skipping them
    #  is what keeps a realtime run from inventing a day
    day = dt.date(2026, 7, 1)
    dat, _ = to_attempts([_a(1, "TH", 100, "X.TB.A.1.1p0", t=1, d=day),
                          _a(2, "TH", 100, "X.TB.A.1.2p0", t=2, d=day)])
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
    p.add_argument("--chain-qty", choices=("last", "max"), default=CHAIN_QTY,
                   help="which attempt's size is the chain's quantity")
    p.add_argument("--compare", action="store_true",
                   help="print v1 and v2 side by side over ONE fetch and exit, "
                        "so any difference is the counting and nothing else")
    p.add_argument("--chains", action="store_true",
                   help="list the chained orders and their attempts, and exit "
                        "- the way to check the stem against the engine")
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
