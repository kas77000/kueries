#!/usr/bin/env python3
"""
=============================================================================
luld_shortsell_check.py

Audits our algo engine's CHILD SPLITS against each market's limit up/down and
short sell rules over a date range, and reports anomalies with enough context
to reproduce them.

  python scripts/luld_shortsell_check/luld_shortsell_check.py \
      --start 2026-07-01 --end 2026-07-31 --country CN --out-dir out

Talks to TWO kdb processes over PyKX, both HISTORICAL.  Set the two constants
below before first use.  pykx is imported lazily inside connect(), so the
self-test runs on a machine with no kdb, no pykx and no q licence:

  python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test

WHAT IS CHECKED

  LULD    JP KR MY TH CN TW IN     every split must sit inside the band, and
                                   inside the client's own limit
  Short   HK JP KR MY TH           only the five markets whose rule our sheet
   sell                            states.  CN, TW and IN are researched but
                                   NOT enforced - they report RULE_UNKNOWN,
                                   because a short sell finding is a
                                   compliance assertion and secondary sources
                                   on markets that rewrite these rules under
                                   stress are not a basis for making one.

  Indonesia is out of scope entirely: no derivable band, no stated rule.

WHERE THE BAND COMES FROM, best first

  override      --band-file, a CSV of known bands
  target_oms    the engine's own band - INDIA AND KOREA ONLY, where it is
                always populated.  It is inconsistent elsewhere.
  observed      qatt: a locked or one sided book IS the band
  computed      previous close from qatt netChange, else target_stock, then
                the market's rule (+/-30%, +/-10%, the TSE step table, or the
                Chinese board from the symbol prefix)

Every band carries band_src and band_conf, and both travel onto every finding.
A computed band the session contradicts is DISCARDED rather than reported -
except in Japan, where the cause is known (limits expand overnight after a
limit close) and the observed extreme is used instead.

THE GUIDING ASYMMETRY.  Where a band or a rule must be guessed, guess in the
direction that UNDER-reports.  A missed violation is a gap someone can close
later.  A fabricated violation against a legal price is what makes people stop
reading the report, and then the gaps stop mattering because nothing is read
at all.

Design, and why each judgement call went the way it did:
docs/superpowers/specs/2026-08-19-luld-shortsell-check-design.md
=============================================================================
"""

from __future__ import annotations

import argparse
import csv as _csv
import datetime as dt
import math
import os
import sys
from collections import defaultdict
from typing import NamedTuple, Optional

import numpy as np

# -----------------------------------------------------------------------------
# CONNECTIONS.  Edit these.  Both are the HISTORICAL processes, not the realtime
# ones - qatt in particular exists in both flavours and only the historical one
# carries a date column.
#
# Both are open processes, so host and port is the whole of it - connect() takes
# no credentials.
# -----------------------------------------------------------------------------

ORDER_SERVER = "CHANGEME:5010"   # target, target_state, target_stock, workorder, target_oms
QATT_SERVER = "CHANGEME:5011"    # qatt

_PLACEHOLDER = "CHANGEME"


# =============================================================================
# MARKET TABLE
# =============================================================================

class Market(NamedTuple):
    code: str
    band_rule: Optional[str]   # None | "jp_step" | "pct" | "cn_board" | "oms_only"
    band_pct: Optional[float]
    ss_rule: Optional[str]     # None | "always_ask" | "uptick" | "ltp_plus_tick"
    band_from_oms: bool


# ss_rule is None for CN, TW and IN ON PURPOSE.  Their real rules are known -
# China is an uptick against the latest trade, Taiwan is a floor at the previous
# close conditional on a 3.5% fall, and India has no price rule at all - but
# they are researched rather than confirmed, so they are counted as unverifiable
# instead of being enforced.  Enabling one is a line here plus its fixtures.
MARKETS = {
    "HK": Market("HK", None,       None, "always_ask",    False),
    "JP": Market("JP", "jp_step",  None, "uptick",        False),
    "KR": Market("KR", "pct",      30.0, "uptick",        True),
    "MY": Market("MY", "pct",      30.0, "uptick",        False),
    "TH": Market("TH", "pct",      30.0, "ltp_plus_tick", False),
    "CN": Market("CN", "cn_board", None, None,            False),
    "TW": Market("TW", "pct",      10.0, None,            False),
    "IN": Market("IN", "oms_only", None, None,            True),
}

EXCLUDED_COUNTRIES = ("ID",)          # Indonesia - see the module docstring
BAND_FROM_TARGET_OMS = ("IN", "KR")   # the only two where target_oms is reliable

SHORTSELL_SIDE = "sellshort"          # confirmed value of target.side

# Markets where the +/-1 tick offset from the unfavourable band is configurable,
# so a split sitting one tick off the band is a config reading, not a breach.
OFFSET_MARKETS = ("CN", "JP")
OFFSET_WINDOW_TICKS = 3

# Markets where a contradicted band has a KNOWN cause, so the observed extreme
# beats no band at all.  Japan's limits expand overnight after a limit close.
WIDEN_ON_CONTRADICTION = ("JP",)

# ChiNext moved from +/-10% to +/-20% on 2020-08-24.  Held as a date rather than
# a bare number so an audit range straddling it is right on both sides.
CHINEXT_20PCT_FROM = dt.date(2020, 8, 24)

# TSE daily price limit (seigen nehaba).  Below 1000 the table is irregular and
# listed; from 1000 up it repeats x10 per decade, so it is generated.
_JP_LOW = ((100, 30), (200, 50), (500, 80), (700, 100), (1000, 150))
_JP_MANTISSA = ((1.5, 300), (2, 400), (3, 500), (5, 700), (7, 1000), (10, 1500))
_JP_CAP = 10_000_000


def jp_limit_width(base: float) -> float:
    """The +/- yen daily limit for a TSE base price (kijun nedan)."""
    if base is None or base <= 0:
        return 0.0
    for hi, width in _JP_LOW:
        if base < hi:
            return float(width)
    k = int(math.floor(math.log10(base))) - 3
    m = base / (10.0 ** (k + 3))
    if m >= 10:              # float error on an exact decade boundary
        m, k = m / 10.0, k + 1
    for hi, width in _JP_MANTISSA:
        if m < hi:
            return float(min(width * (10 ** k), _JP_CAP))
    return float(_JP_CAP)


def _cn_digits(sym: str) -> str:
    head = (sym or "").split(".", 1)[0]
    return head if head.isdigit() else ""


def cn_band_pct(sym: str, trade_date: dt.date) -> Optional[float]:
    """+/- percent band for a Chinese symbol, from its board.  None if unknown.

    ST / *ST names are +/-5%, but that status lives in the stock NAME rather
    than the code, so they are indistinguishable here and come back 10.0 -
    deliberately twice as wide as the truth, which misses violations rather
    than inventing them.  They also self-correct on any day they actually pin,
    because the observed price then contradicts the computed band.
    """
    d = _cn_digits(sym)
    if len(d) != 6:
        return None
    p = d[:3]
    if p in ("600", "601", "603", "605"):
        return 10.0
    if p in ("688", "689"):
        return 20.0
    if p in ("300", "301"):
        return 20.0 if trade_date >= CHINEXT_20PCT_FROM else 10.0
    if p in ("000", "001", "002", "003"):
        return 10.0
    if p in ("900", "200"):
        return 10.0
    if p in ("430", "920") or d[0] == "8":
        return 30.0
    return None


# =============================================================================
# TICKS
# =============================================================================

_LADDER_MIN_POINTS = 50
_LADDER_BUCKETS = 12
_LADDER_PURITY = 0.80     # share of gaps in a bucket that must be the modal gap
_SNAP_EPS = 1e-6


def round_inward(price: float, base: float, tick: float) -> float:
    """Snap price to the tick grid, moving TOWARD base.

    An upper band rounds down and a lower band rounds up, so a band is never
    reported wider than the rule allows.  Values already on the grid are left
    alone - binary floats make price/tick land a hair under the integer, and
    without the snap a legal band edge would be rounded a tick inward.
    """
    if tick is None or tick <= 0 or price is None or price <= 0:
        return price
    n = price / tick
    r = round(n)
    if abs(n - r) < _SNAP_EPS:
        return round(r * tick, 10)
    stepped = math.floor(n) if price > base else math.ceil(n)
    return round(stepped * tick, 10)


def recover_tick_ladder(prices) -> list:
    """Recover a tick ladder from prices observed on stocks sharing one tsid.

    tsid is opaque - the table it indexes lives in mbref, which we cannot reach
    - but every stock carrying the same one shares a ladder by construction, so
    the ladder can be read back out of the prices themselves.

    Returns [(from_price, tick), ...] ascending, or [] when the data does not
    look like a ladder.  Refusing is the point: rounding against a ragged grid
    is worse than falling back to the scalar ticksize.
    """
    p = np.unique(np.asarray(prices, dtype=float))
    p = p[np.isfinite(p) & (p > 0)]
    if p.size < _LADDER_MIN_POINTS:
        return []
    edges = np.geomspace(p[0], p[-1] * (1 + 1e-9), _LADDER_BUCKETS + 1)
    ladder, good, bad = [], 0, 0
    for i in range(_LADDER_BUCKETS):
        seg = p[(p >= edges[i]) & (p < edges[i + 1])]
        if seg.size < 8:
            continue
        gaps = np.diff(seg)
        gaps = np.round(gaps[gaps > 0], 8)
        if gaps.size < 5:
            continue
        vals, counts = np.unique(gaps, return_counts=True)
        if counts.max() / gaps.size < _LADDER_PURITY:
            bad += 1
            continue
        good += 1
        modal = float(vals[counts.argmax()])
        if not ladder or abs(ladder[-1][1] - modal) > 1e-12:
            ladder.append((float(edges[i]), modal))
    if not ladder or bad > good:
        return []
    return ladder


def tick_at(ladder: list, price: float, fallback: float) -> float:
    """Tick size at a price from a recovered ladder, else the fallback."""
    if not ladder:
        return fallback
    tick = ladder[0][1]
    for frm, t in ladder:
        if price >= frm:
            tick = t
        else:
            break
    return tick


# =============================================================================
# BANDS
# =============================================================================

class Band(NamedTuple):
    up: float
    dn: float
    src: str    # override | target_oms | observed | computed
    conf: str   # confirmed | assumed | widened_observed


def compute_band(base: float, country: str, sym: str,
                 trade_date: dt.date, tick: float) -> Optional[Band]:
    """The rule-derived band, or None where no rule applies or no base exists."""
    m = MARKETS.get(country)
    if m is None or base is None or base <= 0:
        return None
    rule = m.band_rule
    if rule is None or rule == "oms_only":
        return None
    if rule == "jp_step":
        w = jp_limit_width(base)
        if w <= 0:
            return None
        return Band(round_inward(base + w, base, tick),
                    round_inward(base - w, base, tick), "computed", "assumed")
    if rule == "pct":
        pct = m.band_pct
    elif rule == "cn_board":
        pct = cn_band_pct(sym, trade_date)
    else:
        return None
    if pct is None:
        return None
    return Band(round_inward(base * (1 + pct / 100.0), base, tick),
                round_inward(base * (1 - pct / 100.0), base, tick),
                "computed", "assumed")


def reconcile_band(computed: Optional[Band], pin, session_high, session_low,
                   country: str, tick: float) -> Optional[Band]:
    """Grade a computed band against what the market actually did.

    None means the band is contradicted with no known cause and has been
    discarded.  That is deliberate: reporting "no band for 40 names" is worth
    more than 40 fabricated violations.
    """
    if computed is None:
        return None
    tol = tick if tick and tick > 0 else 0.0
    if pin and pin > 0:
        if abs(pin - computed.up) <= tol or abs(pin - computed.dn) <= tol:
            return computed._replace(conf="confirmed")
    escaped_up = session_high is not None and session_high > computed.up + tol
    escaped_dn = session_low is not None and 0 < session_low < computed.dn - tol
    pin_outside = bool(pin) and pin > 0 and (
        pin > computed.up + tol or pin < computed.dn - tol)
    if escaped_up or escaped_dn or pin_outside:
        if country not in WIDEN_ON_CONTRADICTION:
            return None
        up = max([v for v in (computed.up, session_high, pin) if v] or [computed.up])
        lows = [v for v in (computed.dn, session_low, pin) if v and v > 0]
        return Band(up, min(lows), "observed", "widened_observed")
    return computed


def _parse_date(s) -> dt.date:
    if isinstance(s, dt.date):
        return s
    parts = str(s).strip().replace(".", "-").split("-")
    return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))


def load_band_overrides(path) -> dict:
    """CSV of known bands that wins over every computed layer.

    Partial coverage is fine: a sym present uses it, a sym absent falls through
    to the normal chain.  This is how reference data we cannot derive - India's
    per-scrip circuit filters, China's ST names - becomes pluggable as it
    arrives rather than a precondition.

      date,sym,limit_up,limit_dn,source
      2026.07.16,600584.CH,41.83,34.23,exchange
    """
    out = {}
    if not path:
        return out
    with open(path, newline="") as fh:
        for row in _csv.DictReader(fh):
            try:
                up, dn = float(row["limit_up"]), float(row["limit_dn"])
            except (TypeError, ValueError, KeyError):
                continue
            if up <= 0 or dn <= 0 or dn > up:
                continue
            out[(_parse_date(row["date"]), row["sym"].strip())] = Band(
                up, dn, "override", "confirmed")
    return out


def resolve_band(sym, country, trade_date, base, tick, oms_up, oms_dn,
                 pin, pin_up, sess_high, sess_low, overrides) -> Optional[Band]:
    """The full chain: override, then target_oms where it is trusted, then the
    computed band reconciled against what the market did."""
    hit = overrides.get((trade_date, sym))
    if hit is not None:
        return hit
    if country in BAND_FROM_TARGET_OMS and oms_up and oms_dn \
            and oms_up > 0 and oms_dn > 0:
        return Band(float(oms_up), float(oms_dn), "target_oms", "confirmed")
    computed = compute_band(base, country, sym, trade_date, tick)
    if computed is None:
        return None
    return reconcile_band(computed, pin, sess_high, sess_low, country, tick)


# =============================================================================
# Q SOURCES.  Sent as text with TYPED arguments; nothing is interpolated.
# ctry arrives as a CHAR VECTOR (b"CN"), never a python str - PyKX turns a str
# into a q symbol and `$ on a symbol is a 'type error.
# =============================================================================

# Parents, their state, their stock reference, and their child splits, for one
# date.  workorder is reduced to one row per id_work with `last` BEFORE any join.
# If it already holds one row per child that grouping is free; if it ever holds a
# row per state change, it is the difference between a correct split count and a
# silently multiplied one.
Q_ORDERS = """
{[d;ctry;exctry]
  t:select date,id_server,id_target,sym,side,sidesign,size,otype,limit_price,
      t_start,t_end,algo,doclose
    from target where date=d;
  ids:exec distinct id_target from t;
  x:select date,id_server,id_target,country,ticksize,tsid,orgclose,adjclose,
      fxlast,ipo
    from target_stock where date=d, id_target in ids;
  x:select from x where not country in exctry;
  x:$[0=count ctry; x; select from x where country=`$ctry];
  t:t ij `date`id_server`id_target xkey x;
  ids:exec distinct id_target from t;
  s:select state:last state, leave:last leave, make:last make
    by date,id_server,id_target
    from target_state where date=d, id_target in ids;
  t:t lj `date`id_server`id_target xkey 0!s;
  w:select date,id_server,id_work,id_target,sym,side,size,otype,price,
      limit_target,venue,venuetype,state,count_send,count_chaseprice,make,
      t_gen,t_transmit,t_on_market,t_off_market,
      transmit_bidprice,transmit_askprice,transmit_lastprice
    from workorder where date=d, id_target in ids;
  w:0!select last id_target, last sym, last side, last size, last otype,
      last price, last limit_target, last venue, last venuetype, last state,
      last count_send, last count_chaseprice, last make, last t_gen,
      last t_transmit, last t_on_market, last t_off_market,
      last transmit_bidprice, last transmit_askprice, last transmit_lastprice
    by date,id_server,id_work from w;
  (t;w)
  }
"""

# Band evidence from qatt for one date and a list of syms.  One row per sym: the
# first tick carrying a usable netChange (which gives the previous close), the
# session extremes, and the pinned price if the book ever locked or went one
# sided.  Rows with nothing on either side are trade prints or pre-open gaps and
# would read as one sided, so they are dropped first.
Q_BAND = """
{[d;syms]
  q:select time,sym,price,qbid:0^qbid,qask:0^qask,netChange:0^netChange,
      pctChange:0^pctChange,highPrice,lowPrice
    from qatt where date=d, sym in syms, (0<0^qbid)|0<0^qask;
  q:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from q;
  b:select preClsTick:first price-netChange, preClsPct:first ?[pctChange<>0;
      price%1+0.01*pctChange; 0n]
    by sym from q where price>0, netChange<>0;
  e:select sessHigh:max highPrice, sessLow:min ?[lowPrice>0;lowPrice;0n],
      firstTime:first time, lastTime:last time
    by sym from q;
  p:select pinPrice:last ?[0=qask;qbid;qask], pinUp:last 0=qask,
      pinStart:first time, pinEnd:last time, pinTicks:count i
    by sym from q where lim;
  ((0!b) lj `sym xkey 0!e) lj `sym xkey 0!p
  }
"""

# The prevailing quote at each split's transmit time.  aj returns the last quote
# at or before the target time and preserves the left table's row order, so the
# columns concatenate straight back onto the splits.  Order and qatt share one
# clock, so nothing is converted.
Q_MKT = """
{[d;f]
  qt:`sym`time xasc select time,sym,qbid:0^qbid,qask:0^qask,
      lastPrice:0^lastPrice,trdTick:0^trdTick
    from qatt where date=d, sym in exec distinct sym from f;
  aj[`sym`time; `sym`time xasc select sym, time:t_transmit from f; qt]
  }
"""

# The engine's own band, for the two markets where it is reliable.  target_oms
# is a tickstream - many rows per id_target - so this takes the band PREVAILING
# at t_transmit rather than the last of the day, and only from rows that carry
# one.  FlexOrderStream writes limitup/limitdn only when it has a quote, so a
# zero means "not known at this instant", not "no band"; reading the last row
# blindly would return 0, and that 0 would then report itself as a missing guard.
Q_OMS_BAND = """
{[d;f]
  o:`id_target`t_algo xasc select id_target,t_algo,limitup,limitdn
    from target_oms where date=d, limitup>0, limitdn>0,
      id_target in exec distinct id_target from f;
  aj[`id_target`t_algo;
     `id_target`t_algo xasc select id_target, t_algo:t_transmit from f; o]
  }
"""


def _check_servers():
    bad = [n for n, v in (("ORDER_SERVER", ORDER_SERVER),
                          ("QATT_SERVER", QATT_SERVER)) if _PLACEHOLDER in v]
    if bad:
        raise SystemExit(
            f"{', '.join(bad)} still set to {_PLACEHOLDER}. Edit the constants "
            "at the top of this script before running against kdb.")


def connect(endpoint: str):
    """Open a PyKX connection.  Imported lazily so --self-test runs anywhere."""
    import pykx as kx
    host, port = endpoint.rsplit(":", 1)
    return kx.SyncQConnection(host=host, port=int(port))


# =============================================================================
# STATE
#
# From OrderStateType in the engine (ai3 src/com/kas/ai/OrderStateType.java).
# The two rule families fail through completely different states: short sell
# breaches come back REJECTED, LULD breaches usually come back as nothing at all
# - CLOSE_BAD_PRICE, a volatility stop, or silence.  That asymmetry is why state
# is carried as an axis and never used as a gate.
# =============================================================================

_STATE_CLASS = {}
for _s in ("rejected", "invalid_ack", "fail_ack"):
    _STATE_CLASS[_s] = "rejected"
for _s in ("close_bad_price", "close_take_outofmoney", "close_ioi_outofmoney",
           "close_invalid", "close_bad_size", "close_oddlot",
           "close_less_min_order_size"):
    _STATE_CLASS[_s] = "suppressed"
for _s in ("close_stock_halt", "close_order_halt", "close_lunch_hour",
           "stopped_volatility_tag262", "stopped_volatility_tag325"):
    _STATE_CLASS[_s] = "halted"
for _s in ("close_not_ack", "close_after_cutoff", "dest_down",
           "close_no_transmit", "fail_ord_status", "fail_transmit",
           "fail_sys_ack", "fail_no_ref", "failed"):
    _STATE_CLASS[_s] = "never_on_market"
for _s in ("created", "init", "scheduled", "activated", "intransmit",
           "transmitted", "acked", "leave", "cxl_pending", "cxlrej", "cxl",
           "filled", "done", "rpld", "expired", "cxlord_succeed", "closed"):
    _STATE_CLASS[_s] = "normal"


def classify_state(state) -> str:
    """Bucket a workorder state.  An unrecognised state is 'unknown', never
    'normal' - a state the engine grew since this was written must be visible
    rather than absorbed."""
    if not state:
        return "unknown"
    return _STATE_CLASS.get(str(state).strip().lower(), "unknown")


# =============================================================================
# RULES
# =============================================================================

class Split(NamedTuple):
    id_target: int
    id_work: int
    sym: str
    country: str
    side: str
    sidesign: int
    otype: str
    price: float
    size: int
    state: str
    t_transmit: int
    parent_limit: float
    tick: float
    q_bid: float
    q_ask: float
    q_last: float
    q_trdtick: int
    t_bid: float
    t_ask: float
    t_last: float


class Finding(NamedTuple):
    rule: str
    severity: str      # violation | deviation | opportunity | improvement
    sym: str
    id_target: int
    id_work: int
    expected: float
    delta_ticks: float
    reason: str


def _mkt(sp: Split, ref: str):
    """(bid, ask, last) from the chosen reference market.

    'qatt' is what the exchange published; 'transmit' is the algo's own snapshot.
    A split legal against one and illegal against the other is a stale-snapshot
    finding rather than a pricing bug, and that is the shape of the Hong Kong
    "market moves away" note.
    """
    return (sp.q_bid, sp.q_ask, sp.q_last) if ref == "qatt" else \
           (sp.t_bid, sp.t_ask, sp.t_last)


def _ticks(delta: float, tick: float) -> float:
    return delta / tick if tick and tick > 0 else 0.0


def _priced(sp: Split) -> bool:
    return sp.price is not None and sp.price > 0


def check_luld_cap(sp: Split, band: Optional[Band]) -> Optional[Finding]:
    """Every child split must be capped by limit up/down."""
    if band is None or not _priced(sp):
        return None
    if sp.price > band.up:
        return Finding("LULD_CAP", "violation", sp.sym, sp.id_target, sp.id_work,
                       band.up, _ticks(sp.price - band.up, sp.tick),
                       f"split priced {sp.price} above limit up {band.up}")
    if sp.price < band.dn:
        return Finding("LULD_CAP", "violation", sp.sym, sp.id_target, sp.id_work,
                       band.dn, _ticks(band.dn - sp.price, sp.tick),
                       f"split priced {sp.price} below limit down {band.dn}")
    return None


def check_client_limit(sp: Split) -> Optional[Finding]:
    """The Japan note: without config the algo goes to limit up/down and stops
    following the client's limit.  Crossing a client limit is worse than
    touching a band, so this is a violation regardless of market."""
    if not _priced(sp) or not sp.parent_limit or sp.parent_limit <= 0:
        return None
    if sp.sidesign > 0 and sp.price > sp.parent_limit:
        d = sp.price - sp.parent_limit
    elif sp.sidesign < 0 and sp.price < sp.parent_limit:
        d = sp.parent_limit - sp.price
    else:
        return None
    return Finding("LULD_CLIENT_LIMIT", "violation", sp.sym, sp.id_target,
                   sp.id_work, sp.parent_limit, _ticks(d, sp.tick),
                   f"split priced {sp.price} through client limit {sp.parent_limit}")


def check_luld_offset(sp: Split, band: Optional[Band]) -> Optional[Finding]:
    """China and Japan can be configured to sit +/-1 tick off the UNFAVOURABLE
    band.  We cannot see the config, so this reports the observed offset as a
    deviation and lets the distribution speak: a market consistently 0 or
    consistently 1 is a setting, not hundreds of failures."""
    if band is None or not _priced(sp) or sp.country not in OFFSET_MARKETS:
        return None
    if sp.tick is None or sp.tick <= 0:
        return None
    edge = band.up if sp.sidesign > 0 else band.dn
    off = _ticks(sp.price - edge, sp.tick)
    if abs(off) > OFFSET_WINDOW_TICKS or abs(off) < 1e-9:
        return None
    return Finding("LULD_OFFSET", "deviation", sp.sym, sp.id_target, sp.id_work,
                   edge, off,
                   f"split priced {off:+.0f} ticks from the unfavourable band {edge}")


def check_ss_hk_ask(sp: Split, ref: str) -> Optional[Finding]:
    """Hong Kong: a short sell may not be priced below the best ask."""
    _, ask, _ = _mkt(sp, ref)
    if not _priced(sp):
        return Finding("SS_HK_ASK", "violation", sp.sym, sp.id_target, sp.id_work,
                       ask or 0.0, 0.0,
                       "market order short sell in HK cannot satisfy always-ask")
    if ask is None or ask <= 0:
        return None
    if sp.price < ask:
        return Finding("SS_HK_ASK", "violation", sp.sym, sp.id_target, sp.id_work,
                       ask, _ticks(ask - sp.price, sp.tick),
                       f"short sell priced {sp.price} below ask {ask} ({ref})")
    return None


def check_ss_uptick(sp: Split, ref: str) -> Optional[Finding]:
    """Japan, Korea, Malaysia: uptick (trend).  Equal to the last trade is
    allowed only on a zero-plus tick, which is what qatt.trdTick tells us."""
    _, _, last = _mkt(sp, ref)
    if not _priced(sp) or last is None or last <= 0:
        return None
    if sp.price > last or (sp.price == last and sp.q_trdtick > 0):
        return None
    return Finding("SS_UPTICK", "violation", sp.sym, sp.id_target, sp.id_work,
                   last, _ticks(last - sp.price, sp.tick),
                   f"short sell priced {sp.price} at/below last {last} "
                   f"on a non-uptick ({ref})")


def check_ss_th_ltp1(sp: Split, ref: str) -> Optional[Finding]:
    """Thailand: last traded price plus one tick.  Below the last trade is a
    breach; merely not being exactly LTP+1 is a config deviation."""
    _, _, last = _mkt(sp, ref)
    if not _priced(sp) or last is None or last <= 0 or not sp.tick or sp.tick <= 0:
        return None
    want = round(last + sp.tick, 10)
    if abs(sp.price - want) < 1e-9:
        return None
    sev = "violation" if sp.price < last else "deviation"
    return Finding("SS_TH_LTP1", sev, sp.sym, sp.id_target, sp.id_work, want,
                   _ticks(sp.price - want, sp.tick),
                   f"short sell priced {sp.price}, LTP+1 tick is {want} ({ref})")


def check_ss_kr_clamp(sp: Split, band: Optional[Band], ref: str) -> Optional[Finding]:
    """Korea: an uptick price that would exceed the band must be capped at the
    band, not sent through it.  This is the LULD-and-short-sell intersection."""
    if band is None or not _priced(sp) or sp.price <= band.up:
        return None
    return Finding("SS_KR_CLAMP", "violation", sp.sym, sp.id_target, sp.id_work,
                   band.up, _ticks(sp.price - band.up, sp.tick),
                   f"uptick price {sp.price} sent through limit up {band.up} "
                   f"instead of being capped ({ref})")


def check_ss_hk_chase(sp: Split, ask_after, resting_secs,
                      chase_ticks: int, chase_secs: int) -> Optional[Finding]:
    """Hong Kong DLP/DMA: an aggressive short sell that does not reprice or
    chase when the market moves away.  Needs the ask AFTER the split went on
    market, so it is separate from the at-transmit checks."""
    if not _priced(sp) or ask_after is None or ask_after <= 0:
        return None
    if resting_secs is None or resting_secs < chase_secs:
        return None
    if sp.tick is None or sp.tick <= 0:
        return None
    moved = _ticks(ask_after - sp.price, sp.tick)
    if moved < chase_ticks:
        return None
    return Finding("SS_HK_CHASE", "violation", sp.sym, sp.id_target, sp.id_work,
                   ask_after, moved,
                   f"short sell rested {resting_secs:.0f}s at {sp.price} while the "
                   f"ask moved {moved:.0f} ticks to {ask_after} without a reprice")


def merge_refs(qatt_findings: list, transmit_findings: list) -> list:
    """Combine the two reference markets into one finding per rule.

    Running both references and counting both would double every number.  The
    disagreement is the useful part, so it becomes a field instead:

      both            failed against the market AND against the algo's own
                      snapshot - a pricing bug
      qatt_only       legal against the snapshot the algo held, illegal against
                      what the exchange published - a STALE SNAPSHOT, which is
                      the shape of the Hong Kong "market moves away" note
      transmit_only   the algo's own view was worse than reality

    Returns [(Finding, ref_verdict), ...].  The qatt finding wins on content,
    because what the exchange published is what compliance cares about.
    """
    q = {f.rule: f for f in qatt_findings}
    t = {f.rule: f for f in transmit_findings}
    out = []
    for rule in sorted(set(q) | set(t)):
        if rule in q and rule in t:
            out.append((q[rule], "both"))
        elif rule in q:
            out.append((q[rule], "qatt_only"))
        else:
            out.append((t[rule], "transmit_only"))
    return out


def run_rules(sp: Split, band: Optional[Band], ref: str) -> list:
    """Every rule that applies to this split, for one reference market."""
    out = []
    for f in (check_luld_cap(sp, band), check_client_limit(sp),
              check_luld_offset(sp, band)):
        if f:
            out.append(f)
    if sp.side != SHORTSELL_SIDE:
        return out
    m = MARKETS.get(sp.country)
    if m is None or m.ss_rule is None:
        return out          # CN / TW / IN -> RULE_UNKNOWN, counted by the caller
    if m.ss_rule == "always_ask":
        f = check_ss_hk_ask(sp, ref)
    elif m.ss_rule == "uptick":
        f = check_ss_uptick(sp, ref)
    elif m.ss_rule == "ltp_plus_tick":
        f = check_ss_th_ltp1(sp, ref)
    else:
        f = None
    if f:
        out.append(f)
    if sp.country == "KR":
        f = check_ss_kr_clamp(sp, band, ref)
        if f:
            out.append(f)
    return out


# =============================================================================
# DETECTORS
#
# Why "no split" happens at all.  From FlexOrderStream.checkPriceFinal, a child
# order is not sent when the quote is stale, or when extremeMarketCondition
# reports a crossed/one-sided book on a lit venue; and ABSStrategy bails when
# midPrice is below ZEROPRICE.  A stock pinned limit up has no ask, so its mid
# is degenerate and its quote goes stale - the algo stops generating splits BY
# CONSTRUCTION.  On the side that cannot fill that is correct.  On the side that
# can, it is a queue of resting counterparties we never joined.
# =============================================================================

CHURN_MIN_SPLITS = 5
GUARD_INACTIVE_MIN_BREACHES = 3
CLOSE_ONLY_WINDOW_MS = 1_800_000
REJECT_CLUSTER_MIN = 5


class Pin(NamedTuple):
    sym: str
    side_pinned: str    # "up" | "down"
    start: int          # ms since midnight
    end: int
    price: float


class Parent(NamedTuple):
    id_target: int
    sym: str
    country: str
    sidesign: int
    state: str
    leave: int
    t_start: int
    t_end: int
    doclose: int
    halted: bool
    size: int
    fxlast: float


def is_favourable(sidesign: int, pinned: str) -> bool:
    """Selling into a limit up, or buying into a limit down - the side that CAN
    fill, because there is a queue resting at the band."""
    return (sidesign < 0 and pinned == "up") or (sidesign > 0 and pinned == "down")


def _pin_overlaps_parent(parent: Parent, pin: Pin) -> bool:
    return pin.start < parent.t_end and pin.end > parent.t_start


def _held_mins(parent: Parent, pin: Pin) -> float:
    return (min(pin.end, parent.t_end) - max(pin.start, parent.t_start)) / 60_000.0


def _no_split_guards_pass(parent: Parent, pin: Pin, pin_mins: int) -> bool:
    """Each guard is separate and each becomes a column on the output row, so a
    false positive can be diagnosed rather than argued about."""
    if str(parent.state).lower() != "activated":
        return False
    if parent.leave is None or parent.leave <= 0:
        return False
    if parent.halted:
        return False
    if parent.doclose and (parent.t_end - parent.t_start) <= CLOSE_ONLY_WINDOW_MS:
        return False
    if not _pin_overlaps_parent(parent, pin):
        return False
    return _held_mins(parent, pin) >= pin_mins


def detect_favourable_no_split(parent: Parent, pin: Pin, splits,
                               pin_mins: int) -> Optional[Finding]:
    """We could have traded at the band and sent nothing."""
    if not is_favourable(parent.sidesign, pin.side_pinned):
        return None
    if splits:
        return None
    if not _no_split_guards_pass(parent, pin, pin_mins):
        return None
    return Finding("LULD_FAVOURABLE_NO_SPLIT", "opportunity", parent.sym,
                   parent.id_target, 0, pin.price, 0.0,
                   f"{parent.leave} left, stock pinned limit {pin.side_pinned} at "
                   f"{pin.price} for {_held_mins(parent, pin):.0f} min on our "
                   f"fillable side, no child split generated")


def detect_favourable_passive(parent: Parent, pin: Pin, splits,
                              pin_mins: int) -> Optional[Finding]:
    """Splits exist, but every one of them sits BEHIND the band rather than at
    it - queueing behind the resting interest instead of joining it."""
    if not is_favourable(parent.sidesign, pin.side_pinned):
        return None
    if not splits or not _no_split_guards_pass(parent, pin, pin_mins):
        return None
    priced = [s for s in splits if _priced(s)]
    if not priced:
        return None
    if pin.side_pinned == "up":
        behind = [s for s in priced if s.price < pin.price]
    else:
        behind = [s for s in priced if s.price > pin.price]
    if len(behind) < len(priced):
        return None
    return Finding("LULD_FAVOURABLE_PASSIVE", "opportunity", parent.sym,
                   parent.id_target, 0, pin.price, 0.0,
                   f"all {len(priced)} splits priced behind the limit "
                   f"{pin.side_pinned} at {pin.price} on our fillable side")


def detect_unfavourable_churn(parent: Parent, pin: Pin, splits) -> Optional[Finding]:
    """Splits that cannot fill, sent into the band anyway - message traffic and
    count_send inflation."""
    if is_favourable(parent.sidesign, pin.side_pinned):
        return None
    n = len(splits)
    if n < CHURN_MIN_SPLITS or not _pin_overlaps_parent(parent, pin):
        return None
    return Finding("LULD_UNFAVOURABLE_CHURN", "improvement", parent.sym,
                   parent.id_target, 0, pin.price, 0.0,
                   f"{n} splits sent while pinned limit {pin.side_pinned} on the "
                   f"side that cannot fill")


def detect_blind_suppression(parent: Parent, pin: Pin, splits) -> Optional[Finding]:
    """The algo went blind BECAUSE of the limit rather than because of a price
    error: splits suppressed on the price path while the book was one sided."""
    if not _pin_overlaps_parent(parent, pin):
        return None
    blind = [s for s in splits if classify_state(s.state) == "suppressed"]
    if not blind:
        return None
    return Finding("LULD_BLIND_SUPPRESSION", "improvement", parent.sym,
                   parent.id_target, 0, pin.price, 0.0,
                   f"{len(blind)} splits suppressed on the price path while the "
                   f"book was pinned limit {pin.side_pinned} - the one sided "
                   f"quote is the cause, not a bad price")


def detect_guard_inactive(sym: str, cap_findings) -> Optional[Finding]:
    """A rollup of LULD_CAP, not a finding of its own.  Three or more breaches on
    one stock is the cap not being applied at all rather than missed once.  The
    constituent splits stay counted under LULD_CAP; this row is a stock count."""
    n = len(cap_findings)
    if n < GUARD_INACTIVE_MIN_BREACHES:
        return None
    f0 = cap_findings[0]
    return Finding("LULD_GUARD_INACTIVE", "violation", sym, f0.id_target, 0,
                   f0.expected, 0.0,
                   f"{n} splits priced through the band on one stock - the cap "
                   f"looks inactive rather than missed")


def detect_reject_cluster(country: str, venue: str, hour: int,
                          rejects) -> Optional[Finding]:
    """Rejected short sells clustered by market, venue and hour.  One reject is
    bad luck; a cluster is a rule mismatch."""
    n = len(rejects)
    if n < REJECT_CLUSTER_MIN:
        return None
    return Finding("SS_REJECT_CLUSTER", "violation", f"{country}/{venue}",
                   rejects[0].id_target, 0, 0.0, 0.0,
                   f"{n} short sell splits rejected on {venue} in hour {hour:02d} "
                   f"- a cluster is a rule mismatch, not bad luck")


# =============================================================================
# SCORING
# =============================================================================

_NOTIONAL_RULES = ("LULD_FAVOURABLE_NO_SPLIT", "LULD_FAVOURABLE_PASSIVE")


def impact_usd(finding: Finding, size, fxlast, price) -> float:
    """What the finding is worth in USD, so the report can sort by it.

    Returns 0.0 rather than a wrong number when fx or size is missing - an
    unsortable row beats a misleading one.
    """
    if not fxlast or fxlast <= 0 or not size or size <= 0:
        return 0.0
    if finding.rule in _NOTIONAL_RULES:
        return float(size) * float(price or 0.0) * float(fxlast)
    if finding.expected and price:
        return abs(float(price) - float(finding.expected)) * float(size) * float(fxlast)
    return 0.0


class Tally:
    """Per-market, per-rule counters.  Small enough to hold for a whole range."""

    def __init__(self):
        self.counts = defaultdict(lambda: defaultdict(int))
        self.seen_n = defaultdict(int)
        self.unverifiable_n = defaultdict(int)
        self.suppressed = defaultdict(set)
        self.excluded_n = defaultdict(int)
        self.band_conf = defaultdict(int)

    def add(self, country, rule, severity):
        self.counts[(country, rule)][severity] += 1

    def seen(self, country, rule, n=1):
        self.seen_n[(country, rule)] += n

    def unverifiable(self, country, n=1):
        self.unverifiable_n[country] += n

    def suppress(self, country, sym):
        self.suppressed[country].add(sym)

    def excluded(self, country, n=1):
        self.excluded_n[country] += n

    def band(self, country, conf):
        self.band_conf[(country, conf)] += 1


def scorecard(tally: Tally) -> str:
    """The rule table, recomputed from what we actually found.

    'unchecked' is deliberately not 'OK'.  A market reading clean because
    nothing was checkable must not look like one where everything passed.
    """
    hdr = (f"{'market':<7}{'rule':<26}{'checked':>9}{'viol':>7}{'dev':>7}"
           f"{'opp':>7}  status")
    rows = [hdr, "-" * len(hdr)]
    keys = sorted(set(list(tally.counts) + list(tally.seen_n)))
    for country, rule in keys:
        c = tally.counts[(country, rule)]
        seen = tally.seen_n[(country, rule)]
        v, d, o = c["violation"], c["deviation"], c["opportunity"] + c["improvement"]
        status = "NotOK" if v else ("check" if d or o else "OK")
        rows.append(f"{country:<7}{rule:<26}{seen:>9}{v:>7}{d:>7}{o:>7}  {status}")
    for country, n in sorted(tally.unverifiable_n.items()):
        rows.append(f"{country:<7}{'RULE_UNKNOWN':<26}{n:>9}{'-':>7}{'-':>7}"
                    f"{'-':>7}  unchecked (no confirmed rule)")
    for country, n in sorted(tally.excluded_n.items()):
        rows.append(f"{country:<7}{'excluded_market':<26}{n:>9}{'-':>7}{'-':>7}"
                    f"{'-':>7}  out of scope")
    return "\n".join(rows)


def suppression_footer(tally: Tally) -> str:
    """What was NOT checked, and why.  Read this before the findings: a clean
    cell because nothing was checkable is a different animal from a clean cell
    because everything passed."""
    out = ["", "suppressed and unchecked", "-" * 40]
    any_row = False
    for country in sorted(tally.suppressed):
        syms = tally.suppressed[country]
        if syms:
            any_row = True
            shown = ", ".join(sorted(syms)[:8])
            more = f" (+{len(syms) - 8} more)" if len(syms) > 8 else ""
            out.append(f"  {country}: {len(syms)} stocks with a contradicted band "
                       f"- LULD findings suppressed: {shown}{more}")
    confs = defaultdict(dict)
    for (country, conf), n in tally.band_conf.items():
        confs[country][conf] = n
    for country in sorted(confs):
        any_row = True
        parts = ", ".join(f"{k} {v}" for k, v in sorted(confs[country].items()))
        out.append(f"  {country}: band confidence - {parts}")
    for country, n in sorted(tally.unverifiable_n.items()):
        any_row = True
        out.append(f"  {country}: {n} short sell splits unchecked - no confirmed "
                   f"rule for this market")
    for country, n in sorted(tally.excluded_n.items()):
        any_row = True
        out.append(f"  {country}: {n} orders skipped - market out of scope")
    if not any_row:
        out.append("  nothing suppressed")
    return "\n".join(out)


# =============================================================================
# REPORT
# =============================================================================

WORKBOOK_COLS = ["date", "id_target", "id_work", "sym", "country", "side",
                 "state", "state_class", "t_transmit", "price_sent",
                 "expected_price", "delta_ticks", "band_up", "band_dn",
                 "band_src", "band_conf", "qbid", "qask", "lastPrice",
                 "trdTick", "transmit_bid", "transmit_ask", "transmit_last",
                 "ref_verdict", "severity", "confidence", "impact_usd", "reason"]


def write_workbook(out_dir: str, findings_by_rule: dict) -> str:
    """One sheet per rule, every finding in full.  Cells hold numbers rather
    than rendered strings, so the workbook can be sorted and charted."""
    from openpyxl import Workbook
    os.makedirs(out_dir, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for rule, rows in sorted(findings_by_rule.items()):
        ws = wb.create_sheet(rule[:31])
        ws.append(WORKBOOK_COLS)
        for r in sorted(rows, key=lambda x: -(x.get("impact_usd") or 0.0)):
            ws.append([r.get(c) for c in WORKBOOK_COLS])
    path = os.path.join(out_dir, "report.xlsx")
    wb.save(path)
    return path


# =============================================================================
# RUN
# =============================================================================

ALL_RULES = ("LULD_CAP", "LULD_CLIENT_LIMIT", "LULD_OFFSET", "SS_HK_ASK",
             "SS_UPTICK", "SS_TH_LTP1", "SS_KR_CLAMP", "SS_HK_CHASE",
             "LULD_FAVOURABLE_NO_SPLIT", "LULD_FAVOURABLE_PASSIVE",
             "LULD_UNFAVOURABLE_CHURN", "LULD_BLIND_SUPPRESSION",
             "LULD_GUARD_INACTIVE", "SS_REJECT_CLUSTER")


def parse_checks(spec: str) -> set:
    """Which rules to run.  An unknown id is refused rather than ignored - a
    typo that silently disables a check is how a clean report gets believed."""
    if not spec or spec.strip().lower() == "all":
        return set(ALL_RULES)
    want = {c.strip().upper() for c in spec.split(",") if c.strip()}
    bad = want - set(ALL_RULES)
    if bad:
        known = ", ".join(ALL_RULES)
        raise SystemExit(f"unknown check(s): {', '.join(sorted(bad))}"
                         f"{os.linesep}known: {known}")
    return want


def daterange(start: dt.date, end: dt.date) -> list:
    if end < start:
        raise SystemExit(f"--end {end} is before --start {start}")
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def _f(v, default=0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if not np.isfinite(x) else x


def _i(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def build_splits(wo_rows, parents, ladders, quotes, bands) -> list:
    """Assemble Split records from the per-date frames.

    Kept separate from the IPC so the assembly is exercised by the self-test
    even though the queries themselves cannot be.
    """
    out = []
    for i, w in enumerate(wo_rows):
        idt = _i(w.get("id_target"))
        p = parents.get(idt)
        if p is None:
            continue
        q = quotes[i] if i < len(quotes) else {}
        tick = tick_at(ladders.get(p["tsid"], []), _f(w.get("price")),
                       _f(p.get("ticksize"), 0.0))
        out.append(Split(
            id_target=idt, id_work=_i(w.get("id_work")), sym=_s(w.get("sym")),
            country=_s(p.get("country")), side=_s(p.get("side")),
            sidesign=_i(p.get("sidesign")), otype=_s(w.get("otype")),
            price=_f(w.get("price")), size=_i(w.get("size")),
            state=_s(w.get("state")), t_transmit=_i(w.get("t_transmit")),
            parent_limit=_f(p.get("limit_price")), tick=tick,
            q_bid=_f(q.get("qbid")), q_ask=_f(q.get("qask")),
            q_last=_f(q.get("lastPrice")), q_trdtick=_i(q.get("trdTick")),
            t_bid=_f(w.get("transmit_bidprice")),
            t_ask=_f(w.get("transmit_askprice")),
            t_last=_f(w.get("transmit_lastprice"))))
    return out


def run(args) -> int:
    """Walk the range one date at a time, folding findings as we go."""
    import pandas as pd  # noqa: F401  (pykx returns frames)

    overrides = load_band_overrides(args.band_file)
    checks = parse_checks(args.checks)
    tally = Tally()
    findings_by_rule = defaultdict(list)
    dates = daterange(args.start, args.end)
    if args.diagnose:
        dates = dates[:1]

    oh = connect(ORDER_SERVER)
    qh = connect(QATT_SERVER)
    ctry = args.country.encode() if args.country else b""
    exctry = [c.encode() for c in EXCLUDED_COUNTRIES]

    try:
        for d in dates:
            if not args.quiet:
                print(f"  {d} ...", file=sys.stderr, flush=True)
            tgt, wo = oh(Q_ORDERS, d, ctry, exctry)
            tgt, wo = tgt.pd(), wo.pd()
            if tgt.empty:
                continue
            if args.diagnose:
                _diagnose(d, tgt, wo)
                continue

            syms = sorted(set(tgt["sym"].astype(str)))
            band_ev = qh(Q_BAND, d, syms).pd().set_index("sym", drop=False)
            quotes = qh(Q_MKT, d, wo).pd().to_dict("records") if not wo.empty else []
            oms = {}
            if any(c in BAND_FROM_TARGET_OMS for c in set(tgt["country"].astype(str))):
                omsdf = oh(Q_OMS_BAND, d, wo).pd() if not wo.empty else None
                if omsdf is not None and not omsdf.empty:
                    for rec in omsdf.to_dict("records"):
                        oms[_i(rec.get("id_target"))] = (
                            _f(rec.get("limitup")), _f(rec.get("limitdn")))

            parents, pins = {}, {}
            for rec in tgt.to_dict("records"):
                idt = _i(rec.get("id_target"))
                parents[idt] = rec
                sym = _s(rec.get("sym"))
                ev = band_ev.loc[sym].to_dict() if sym in band_ev.index else {}
                base = _f(ev.get("preClsTick")) or _f(rec.get("orgclose")) \
                    or _f(rec.get("adjclose"))
                oms_up, oms_dn = oms.get(idt, (0.0, 0.0))
                tick = _f(rec.get("ticksize"))
                band = resolve_band(
                    sym=sym, country=_s(rec.get("country")), trade_date=d,
                    base=base, tick=tick, oms_up=oms_up, oms_dn=oms_dn,
                    pin=_f(ev.get("pinPrice")) or None,
                    pin_up=bool(ev.get("pinUp")),
                    sess_high=_f(ev.get("sessHigh")) or None,
                    sess_low=_f(ev.get("sessLow")) or None,
                    overrides=overrides)
                rec["_band"] = band
                country = _s(rec.get("country"))
                if band is None and MARKETS.get(country, Market("", None, None, None, False)).band_rule:
                    tally.suppress(country, sym)
                elif band is not None:
                    tally.band(country, band.conf)
                if _f(ev.get("pinPrice")) > 0:
                    pins[sym] = Pin(sym, "up" if ev.get("pinUp") else "down",
                                    _i(ev.get("pinStart")), _i(ev.get("pinEnd")),
                                    _f(ev.get("pinPrice")))

            wo_rows = wo.to_dict("records") if not wo.empty else []
            splits = build_splits(wo_rows, parents, {}, quotes, None)
            by_parent = defaultdict(list)
            for sp in splits:
                by_parent[sp.id_target].append(sp)

            cap_by_sym = defaultdict(list)
            for sp in splits:
                p = parents.get(sp.id_target, {})
                band = p.get("_band")
                m = MARKETS.get(sp.country)
                if m is None:
                    tally.excluded(sp.country)
                    continue
                if sp.side == SHORTSELL_SIDE and m.ss_rule is None:
                    tally.unverifiable(sp.country)
                merged = merge_refs(run_rules(sp, band, "qatt"),
                                    run_rules(sp, band, "transmit"))
                for f, verdict in merged:
                    if f.rule not in checks:
                        continue
                    tally.seen(sp.country, f.rule)
                    tally.add(sp.country, f.rule, f.severity)
                    if f.rule == "LULD_CAP":
                        cap_by_sym[sp.sym].append(f)
                    findings_by_rule[f.rule].append(_row(d, sp, f, band, verdict, p))

            for sym, caps in cap_by_sym.items():
                g = detect_guard_inactive(sym, caps)
                if g and g.rule in checks:
                    c = next((s.country for s in splits if s.sym == sym), "")
                    tally.add(c, g.rule, g.severity)
                    findings_by_rule[g.rule].append(_row(d, None, g, None, "", {}))

            for idt, p in parents.items():
                sym = _s(p.get("sym"))
                pin = pins.get(sym)
                if pin is None:
                    continue
                par = Parent(idt, sym, _s(p.get("country")),
                             _i(p.get("sidesign")), _s(p.get("state")),
                             _i(p.get("leave")), _i(p.get("t_start")),
                             _i(p.get("t_end")), _i(p.get("doclose")), False,
                             _i(p.get("size")), _f(p.get("fxlast")))
                sp = by_parent.get(idt, [])
                for det in (detect_favourable_no_split(par, pin, sp, args.pin_mins),
                            detect_favourable_passive(par, pin, sp, args.pin_mins),
                            detect_unfavourable_churn(par, pin, sp),
                            detect_blind_suppression(par, pin, sp)):
                    if det and det.rule in checks:
                        tally.seen(par.country, det.rule)
                        tally.add(par.country, det.rule, det.severity)
                        findings_by_rule[det.rule].append(
                            _row(d, None, det, p.get("_band"), "", p))
    finally:
        for h in (oh, qh):
            try:
                h.close()
            except Exception:
                pass

    print(scorecard(tally))
    print(suppression_footer(tally))
    if args.out_dir and findings_by_rule:
        print(f"\nworkbook: {write_workbook(args.out_dir, findings_by_rule)}")
    return 0


def _row(d, sp: Optional[Split], f: Finding, band: Optional[Band],
         ref: str, parent: dict) -> dict:
    size = sp.size if sp else _i(parent.get("leave"))
    price = sp.price if sp else _f(parent.get("_pin_price"))
    return {
        "date": d, "id_target": f.id_target, "id_work": f.id_work,
        "sym": f.sym, "country": sp.country if sp else _s(parent.get("country")),
        "side": sp.side if sp else _s(parent.get("side")),
        "state": sp.state if sp else _s(parent.get("state")),
        "state_class": classify_state(sp.state if sp else parent.get("state")),
        "t_transmit": sp.t_transmit if sp else None,
        "price_sent": sp.price if sp else None,
        "expected_price": f.expected, "delta_ticks": f.delta_ticks,
        "band_up": band.up if band else None,
        "band_dn": band.dn if band else None,
        "band_src": band.src if band else None,
        "band_conf": band.conf if band else None,
        "qbid": sp.q_bid if sp else None, "qask": sp.q_ask if sp else None,
        "lastPrice": sp.q_last if sp else None,
        "trdTick": sp.q_trdtick if sp else None,
        "transmit_bid": sp.t_bid if sp else None,
        "transmit_ask": sp.t_ask if sp else None,
        "transmit_last": sp.t_last if sp else None,
        "ref_verdict": ref, "severity": f.severity,
        "confidence": band.conf if band else "none",
        "impact_usd": impact_usd(f, size, _f(parent.get("fxlast")), price),
        "reason": f.reason,
    }


def _diagnose(d, tgt, wo):
    """First date only.  Everything needed to tell a real empty result from a
    filter that silently matched nothing."""
    print(f"\ndiagnose {d}")
    print(f"  parents {len(tgt):>8}   splits {len(wo):>8}")
    print("\n  distinct target.side:")
    for v, n in tgt["side"].astype(str).value_counts().items():
        mark = "  <- short sells" if v == SHORTSELL_SIDE else ""
        print(f"    {v:<16}{n:>8}{mark}")
    print("\n  country x symbol suffix (China is .CH, not .CN):")
    suf = tgt["sym"].astype(str).str.rsplit(".", n=1).str[-1]
    for (c, s), n in tgt.groupby([tgt["country"].astype(str), suf]).size().items():
        print(f"    {c:<4} {s:<6}{n:>8}")
    print("\n  tsid x symbol prefix (a second opinion on the Chinese board):")
    pre = tgt["sym"].astype(str).str[:3]
    for (t, p), n in tgt.groupby([tgt["tsid"].astype(str), pre]).size().items():
        if n > 1:
            print(f"    {t:<10} {p:<6}{n:>8}")
    if not wo.empty:
        print("\n  workorder.state:")
        for v, n in wo["state"].astype(str).value_counts().head(20).items():
            print(f"    {v:<32}{classify_state(v):<18}{n:>8}")


# =============================================================================
# SELF TEST
# =============================================================================

def _split(**kw) -> Split:
    base = dict(id_target=1, id_work=2, sym="X.KS", country="KR", side="sell",
                sidesign=-1, otype="limit", price=100.0, size=1000,
                state="acked", t_transmit=0, parent_limit=0.0, tick=0.01,
                q_bid=99.0, q_ask=101.0, q_last=100.0, q_trdtick=0,
                t_bid=99.0, t_ask=101.0, t_last=100.0)
    base.update(kw)
    return Split(**base)


def _parent(**kw) -> Parent:
    base = dict(id_target=1, sym="7203.JT", country="JP", sidesign=-1,
                state="activated", leave=5000, t_start=0, t_end=30_000_000,
                doclose=0, halted=False, size=10000, fxlast=0.0068)
    base.update(kw)
    return Parent(**base)


def _pin_(**kw) -> Pin:
    base = dict(sym="7203.JT", side_pinned="up", start=10_000_000,
                end=20_000_000, price=1300.0)
    base.update(kw)
    return Pin(**base)


# --- market table -----------------------------------------------------------

def test_markets_cover_exactly_the_eight_in_scope():
    assert set(MARKETS) == {"HK", "JP", "KR", "MY", "TH", "CN", "TW", "IN"}
    assert "ID" not in MARKETS, "Indonesia is out of scope"


def test_short_sell_rules_only_on_confirmed_markets():
    checked = {c for c, m in MARKETS.items() if m.ss_rule is not None}
    assert checked == {"HK", "JP", "KR", "MY", "TH"}
    for c in ("CN", "TW", "IN"):
        assert MARKETS[c].ss_rule is None, f"{c} short sell must stay RULE_UNKNOWN"


def test_band_from_oms_is_two_markets():
    assert {c for c, m in MARKETS.items() if m.band_from_oms} == {"IN", "KR"}
    assert set(BAND_FROM_TARGET_OMS) == {"IN", "KR"}


def test_hk_has_no_band_rule():
    assert MARKETS["HK"].band_rule is None


# --- japan ------------------------------------------------------------------

def test_jp_step_table_low_end():
    assert jp_limit_width(50) == 30
    assert jp_limit_width(99.9) == 30
    assert jp_limit_width(100) == 50
    assert jp_limit_width(199) == 50
    assert jp_limit_width(200) == 80
    assert jp_limit_width(499) == 80
    assert jp_limit_width(500) == 100
    assert jp_limit_width(699) == 100
    assert jp_limit_width(700) == 150
    assert jp_limit_width(999) == 150


def test_jp_step_table_is_self_similar_per_decade():
    assert jp_limit_width(1000) == 300
    assert jp_limit_width(1500) == 400
    assert jp_limit_width(2000) == 500
    assert jp_limit_width(3000) == 700
    assert jp_limit_width(5000) == 1000
    assert jp_limit_width(7000) == 1500
    assert jp_limit_width(10000) == 3000
    assert jp_limit_width(20000) == 5000
    assert jp_limit_width(50000) == 10000
    assert jp_limit_width(100000) == 30000


def test_jp_step_table_caps_at_ten_million():
    assert jp_limit_width(50_000_000) == 10_000_000
    assert jp_limit_width(90_000_000) == 10_000_000
    assert jp_limit_width(500_000_000) == 10_000_000


def test_jp_step_table_is_monotonic():
    prev = 0.0
    for base in (1, 50, 100, 250, 600, 800, 1200, 1800, 2500, 4000, 6000,
                 8000, 12000, 18000, 25000, 40000, 120000, 1_000_000):
        w = jp_limit_width(base)
        assert w >= prev, f"width fell at base {base}"
        prev = w


# --- china ------------------------------------------------------------------

def test_cn_main_board_is_ten_percent():
    d = dt.date(2026, 7, 16)
    for sym in ("600584.CH", "601398.CH", "603288.CH", "605080.CH",
                "000001.CH", "001979.CH", "002415.CH", "003816.CH"):
        assert cn_band_pct(sym, d) == 10.0, sym


def test_cn_star_board_is_twenty_percent():
    d = dt.date(2026, 7, 16)
    assert cn_band_pct("688981.CH", d) == 20.0
    assert cn_band_pct("689009.CH", d) == 20.0


def test_cn_chinext_is_twenty_percent_only_after_the_2020_reform():
    assert cn_band_pct("300750.CH", dt.date(2026, 7, 16)) == 20.0
    assert cn_band_pct("301029.CH", dt.date(2026, 7, 16)) == 20.0
    assert cn_band_pct("300750.CH", dt.date(2020, 8, 24)) == 20.0
    assert cn_band_pct("300750.CH", dt.date(2020, 8, 21)) == 10.0


def test_cn_b_shares_and_beijing():
    d = dt.date(2026, 7, 16)
    assert cn_band_pct("900901.CH", d) == 10.0
    assert cn_band_pct("200011.CH", d) == 10.0
    assert cn_band_pct("430047.CH", d) == 30.0
    assert cn_band_pct("832000.CH", d) == 30.0
    assert cn_band_pct("871981.CH", d) == 30.0
    assert cn_band_pct("920002.CH", d) == 30.0


def test_cn_unknown_prefix_returns_none_rather_than_guessing():
    d = dt.date(2026, 7, 16)
    assert cn_band_pct("123456.CH", d) is None
    assert cn_band_pct("NOTANUM.CH", d) is None
    assert cn_band_pct("", d) is None


# --- ticks ------------------------------------------------------------------

def test_round_inward_never_widens_a_band():
    assert round_inward(130.0, 100.0, 0.01) == 130.0
    assert abs(round_inward(43.329, 33.33, 0.01) - 43.32) < 1e-9
    assert abs(round_inward(23.331, 33.33, 0.01) - 23.34) < 1e-9


def test_round_inward_is_idempotent_on_grid_values():
    for p in (10.0, 10.05, 99.99):
        assert abs(round_inward(p, 50.0, 0.01) - p) < 1e-9, p


def test_round_inward_handles_a_zero_tick():
    assert round_inward(43.329, 33.33, 0.0) == 43.329


def test_recover_tick_ladder_finds_a_two_step_grid():
    lo = np.arange(1.0, 2.0, 0.001)
    hi = np.arange(2.0, 3.0, 0.005)
    ladder = recover_tick_ladder(np.concatenate([lo, hi]))
    assert ladder, "expected a ladder"
    assert abs(tick_at(ladder, 1.5, 0.01) - 0.001) < 1e-9
    assert abs(tick_at(ladder, 2.5, 0.01) - 0.005) < 1e-9


def test_recover_tick_ladder_refuses_ragged_data():
    rng = np.random.default_rng(0)
    assert recover_tick_ladder(rng.uniform(1.0, 100.0, 500)) == []


def test_recover_tick_ladder_refuses_too_few_points():
    assert recover_tick_ladder(np.array([1.0, 1.01, 1.02])) == []


def test_tick_at_falls_back_when_ladder_is_empty():
    assert tick_at([], 42.0, 0.05) == 0.05


# --- bands ------------------------------------------------------------------

def test_compute_band_percent_markets():
    d = dt.date(2026, 7, 16)
    b = compute_band(100.0, "KR", "005930.KS", d, 0.01)
    assert b.up == 130.0 and b.dn == 70.0 and b.src == "computed"
    b = compute_band(100.0, "TW", "2330.TW", d, 0.01)
    assert b.up == 110.0 and b.dn == 90.0


def test_compute_band_japan_uses_the_step_table():
    b = compute_band(1000.0, "JP", "7203.JT", dt.date(2026, 7, 16), 1.0)
    assert b.up == 1300.0 and b.dn == 700.0


def test_compute_band_china_uses_the_board():
    d = dt.date(2026, 7, 16)
    assert compute_band(100.0, "CN", "600584.CH", d, 0.01).up == 110.0
    assert compute_band(100.0, "CN", "688981.CH", d, 0.01).up == 120.0


def test_compute_band_returns_none_where_no_rule_exists():
    d = dt.date(2026, 7, 16)
    assert compute_band(100.0, "HK", "0005.HK", d, 0.01) is None
    assert compute_band(100.0, "IN", "RELIANCE.IN", d, 0.05) is None
    assert compute_band(0.0, "KR", "005930.KS", d, 0.01) is None
    assert compute_band(100.0, "CN", "123456.CH", d, 0.01) is None


def test_compute_band_rounds_inward_so_it_never_exceeds_the_rule():
    b = compute_band(33.33, "KR", "X.KS", dt.date(2026, 7, 16), 0.01)
    assert b.up <= 33.33 * 1.30 + 1e-9
    assert b.dn >= 33.33 * 0.70 - 1e-9


def test_reconcile_confirms_when_a_pin_agrees():
    c = Band(130.0, 70.0, "computed", "assumed")
    r = reconcile_band(c, 130.0, 130.0, 95.0, "KR", 0.01)
    assert r.conf == "confirmed"


def test_reconcile_contradicts_and_discards_when_the_session_escapes():
    c = Band(130.0, 70.0, "computed", "assumed")
    assert reconcile_band(c, None, 145.0, 95.0, "KR", 0.01) is None


def test_reconcile_widens_rather_than_discards_for_japan():
    c = Band(1300.0, 700.0, "computed", "assumed")
    r = reconcile_band(c, None, 1500.0, 900.0, "JP", 1.0)
    assert r is not None, "Japan widens, it does not suppress"
    assert r.conf == "widened_observed" and r.up == 1500.0


def test_reconcile_leaves_an_untouched_band_assumed():
    c = Band(130.0, 70.0, "computed", "assumed")
    r = reconcile_band(c, None, 110.0, 95.0, "KR", 0.01)
    assert r.conf == "assumed" and r.up == 130.0


# --- state ------------------------------------------------------------------

def test_state_rejections_are_rejected():
    for s in ("rejected", "REJECTED", "invalid_ack", "fail_ack"):
        assert classify_state(s) == "rejected", s


def test_state_price_suppression_is_suppressed():
    for s in ("close_bad_price", "close_take_outofmoney", "close_ioi_outofmoney"):
        assert classify_state(s) == "suppressed", s


def test_state_halts_and_volatility_stops_are_halted():
    for s in ("close_stock_halt", "close_order_halt",
              "stopped_volatility_tag262", "stopped_volatility_tag325"):
        assert classify_state(s) == "halted", s


def test_state_never_on_market():
    for s in ("close_not_ack", "close_after_cutoff", "dest_down",
              "close_no_transmit", "fail_ord_status"):
        assert classify_state(s) == "never_on_market", s


def test_state_normal_lifecycle():
    for s in ("filled", "acked", "leave", "done", "cxl", "transmitted"):
        assert classify_state(s) == "normal", s


def test_state_unknown_is_not_silently_normal():
    assert classify_state("something_new_from_the_engine") == "unknown"
    assert classify_state("") == "unknown"
    assert classify_state(None) == "unknown"


# --- rules ------------------------------------------------------------------

def test_luld_cap_flags_a_split_above_the_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    f = check_luld_cap(_split(price=115.0), b)
    assert f is not None and f.rule == "LULD_CAP" and f.severity == "violation"
    assert abs(f.delta_ticks - 500.0) < 1e-6


def test_luld_cap_flags_a_split_below_the_band():
    b = Band(41.83, 34.23, "computed", "confirmed")
    f = check_luld_cap(_split(sym="600584.CH", country="CN", price=34.00), b)
    assert f is not None and f.rule == "LULD_CAP"


def test_luld_cap_passes_a_split_at_the_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    assert check_luld_cap(_split(price=110.0), b) is None
    assert check_luld_cap(_split(price=90.0), b) is None


def test_luld_cap_ignores_market_orders_and_missing_bands():
    b = Band(110.0, 90.0, "computed", "confirmed")
    assert check_luld_cap(_split(price=0.0, otype="market"), b) is None
    assert check_luld_cap(_split(price=115.0), None) is None


def test_client_limit_flags_a_buy_priced_through_the_parent_limit():
    f = check_client_limit(_split(side="buy", sidesign=1, price=105.0,
                                  parent_limit=100.0))
    assert f is not None and f.rule == "LULD_CLIENT_LIMIT"


def test_client_limit_flags_a_sell_priced_through_the_parent_limit():
    assert check_client_limit(_split(side="sell", sidesign=-1, price=95.0,
                                     parent_limit=100.0)) is not None


def test_client_limit_passes_at_the_limit_and_when_absent():
    assert check_client_limit(_split(side="buy", sidesign=1, price=100.0,
                                     parent_limit=100.0)) is None
    assert check_client_limit(_split(side="buy", sidesign=1, price=105.0,
                                     parent_limit=0.0)) is None


def test_luld_offset_reports_a_tick_off_the_unfavourable_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    f = check_luld_offset(_split(country="CN", sym="600584.CH", sidesign=-1,
                                 price=90.01), b)
    assert f is not None and f.rule == "LULD_OFFSET" and f.severity == "deviation"
    assert abs(f.delta_ticks - 1.0) < 1e-6


def test_luld_offset_is_silent_at_the_band_and_far_from_it():
    b = Band(110.0, 90.0, "computed", "confirmed")
    assert check_luld_offset(_split(country="CN", price=90.0), b) is None
    assert check_luld_offset(_split(country="CN", price=100.0), b) is None
    assert check_luld_offset(_split(country="KR", price=90.01), b) is None


def test_hk_short_sell_must_be_at_or_above_the_ask():
    sp = _split(country="HK", sym="0005.HK", price=100.5, q_ask=101.0)
    f = check_ss_hk_ask(sp, "qatt")
    assert f is not None and f.rule == "SS_HK_ASK"
    assert check_ss_hk_ask(_split(country="HK", price=101.0, q_ask=101.0),
                           "qatt") is None


def test_hk_market_order_short_sell_fails_by_construction():
    f = check_ss_hk_ask(_split(country="HK", price=0.0, otype="market"), "qatt")
    assert f is not None and "market" in f.reason.lower()


def test_uptick_requires_strictly_above_last_on_a_downtick():
    assert check_ss_uptick(_split(country="JP", price=100.0, q_last=100.0,
                                  q_trdtick=-1), "qatt") is not None
    assert check_ss_uptick(_split(country="JP", price=100.01, q_last=100.0,
                                  q_trdtick=-1), "qatt") is None


def test_uptick_allows_equal_to_last_on_a_zero_plus_tick():
    assert check_ss_uptick(_split(country="JP", price=100.0, q_last=100.0,
                                  q_trdtick=1), "qatt") is None


def test_uptick_judges_the_two_reference_markets_separately():
    # legal against the algo's own snapshot, illegal against what qatt published
    sp = _split(country="JP", price=100.5, q_last=101.0, t_last=100.0,
                q_trdtick=-1)
    assert check_ss_uptick(sp, "transmit") is None
    assert check_ss_uptick(sp, "qatt") is not None


def test_thailand_wants_ltp_plus_one_tick():
    sp = _split(country="TH", sym="PTT.TB", price=100.0, q_last=100.0, tick=0.25)
    f = check_ss_th_ltp1(sp, "qatt")
    assert f is not None and f.severity == "deviation"
    below = _split(country="TH", price=99.75, q_last=100.0, tick=0.25)
    assert check_ss_th_ltp1(below, "qatt").severity == "violation"
    ok = _split(country="TH", price=100.25, q_last=100.0, tick=0.25)
    assert check_ss_th_ltp1(ok, "qatt") is None


def test_korea_uptick_price_must_still_be_clamped_to_the_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    f = check_ss_kr_clamp(_split(country="KR", price=112.0, q_last=111.0,
                                 q_trdtick=1), b, "qatt")
    assert f is not None and f.rule == "SS_KR_CLAMP"
    assert abs(f.expected - 110.0) < 1e-9


def test_hk_chase_fires_when_the_ask_ran_away_and_we_sat_there():
    sp = _split(country="HK", sym="0005.HK", price=100.0, tick=0.05)
    f = check_ss_hk_chase(sp, ask_after=100.2, resting_secs=45,
                          chase_ticks=2, chase_secs=30)
    assert f is not None and f.rule == "SS_HK_CHASE"


def test_hk_chase_is_silent_when_it_repriced_in_time_or_barely_moved():
    sp = _split(country="HK", price=100.0, tick=0.05)
    assert check_ss_hk_chase(sp, 100.2, 10, 2, 30) is None
    assert check_ss_hk_chase(sp, 100.05, 45, 2, 30) is None


def test_merge_refs_counts_a_finding_once_not_twice():
    b = Band(110.0, 90.0, "computed", "confirmed")
    sp = _split(price=115.0)
    merged = merge_refs(run_rules(sp, b, "qatt"), run_rules(sp, b, "transmit"))
    caps = [(f, v) for f, v in merged if f.rule == "LULD_CAP"]
    assert len(caps) == 1, "LULD_CAP must not be counted once per reference"
    assert caps[0][1] == "both", "band breaches do not depend on the quote"


def test_merge_refs_names_a_stale_snapshot():
    # legal against the algo's own snapshot, illegal against what qatt published
    sp = _split(country="JP", side=SHORTSELL_SIDE, price=100.5, q_last=101.0,
                t_last=100.0, q_trdtick=-1)
    merged = merge_refs(run_rules(sp, None, "qatt"), run_rules(sp, None, "transmit"))
    ups = [(f, v) for f, v in merged if f.rule == "SS_UPTICK"]
    assert len(ups) == 1 and ups[0][1] == "qatt_only"


def test_merge_refs_names_the_reverse_case():
    sp = _split(country="JP", side=SHORTSELL_SIDE, price=100.5, q_last=100.0,
                t_last=101.0, q_trdtick=-1)
    merged = merge_refs(run_rules(sp, None, "qatt"), run_rules(sp, None, "transmit"))
    ups = [(f, v) for f, v in merged if f.rule == "SS_UPTICK"]
    assert len(ups) == 1 and ups[0][1] == "transmit_only"


def test_merge_refs_is_empty_when_both_references_are_clean():
    assert merge_refs([], []) == []


def test_run_rules_skips_short_sell_checks_on_unconfirmed_markets():
    b = Band(110.0, 90.0, "computed", "confirmed")
    for country, sym in (("CN", "600584.CH"), ("TW", "2330.TW"),
                         ("IN", "RELIANCE.IN")):
        sp = _split(country=country, sym=sym, side=SHORTSELL_SIDE, sidesign=-1,
                    price=95.0, q_ask=101.0, q_last=100.0, q_trdtick=-1)
        rules = {f.rule for f in run_rules(sp, b, "qatt")}
        assert not any(r.startswith("SS_") for r in rules), (country, rules)


def test_run_rules_runs_short_sell_checks_on_confirmed_markets():
    b = Band(110.0, 90.0, "computed", "confirmed")
    sp = _split(country="JP", sym="7203.JT", side=SHORTSELL_SIDE, sidesign=-1,
                price=100.0, q_last=100.0, q_trdtick=-1)
    assert "SS_UPTICK" in {f.rule for f in run_rules(sp, b, "qatt")}


# --- detectors --------------------------------------------------------------

def test_favourable_is_selling_into_a_limit_up():
    assert is_favourable(-1, "up") is True
    assert is_favourable(1, "up") is False
    assert is_favourable(1, "down") is True
    assert is_favourable(-1, "down") is False


def test_no_split_fires_when_we_could_have_sold_into_a_limit_up():
    f = detect_favourable_no_split(_parent(), _pin_(), [], 5)
    assert f is not None
    assert f.rule == "LULD_FAVOURABLE_NO_SPLIT" and f.severity == "opportunity"


def test_no_split_is_silent_when_splits_exist():
    assert detect_favourable_no_split(_parent(), _pin_(), [_split()], 5) is None


def test_no_split_guard_parent_must_be_activated():
    assert detect_favourable_no_split(_parent(state="scheduled"), _pin_(),
                                      [], 5) is None


def test_no_split_guard_needs_something_left_to_work():
    assert detect_favourable_no_split(_parent(leave=0), _pin_(), [], 5) is None


def test_no_split_guard_pin_must_outlast_pin_mins():
    short = _pin_(start=10_000_000, end=10_120_000)
    assert detect_favourable_no_split(_parent(), short, [], 5) is None


def test_no_split_guard_pin_must_fall_inside_the_parent_window():
    late = _pin_(start=40_000_000, end=45_000_000)
    assert detect_favourable_no_split(_parent(), late, [], 5) is None


def test_no_split_guard_skips_halted_and_close_only_parents():
    assert detect_favourable_no_split(_parent(halted=True), _pin_(), [], 5) is None
    assert detect_favourable_no_split(
        _parent(doclose=1, t_start=29_000_000, t_end=30_000_000),
        _pin_(), [], 5) is None


def test_no_split_is_silent_on_the_unfavourable_side():
    assert detect_favourable_no_split(_parent(sidesign=1), _pin_(), [], 5) is None


def test_favourable_passive_fires_when_every_split_hid_behind_the_band():
    sp = [_split(price=1290.0), _split(price=1295.0)]
    f = detect_favourable_passive(_parent(), _pin_(), sp, 5)
    assert f is not None and f.rule == "LULD_FAVOURABLE_PASSIVE"


def test_favourable_passive_is_silent_if_any_split_reached_the_band():
    sp = [_split(price=1290.0), _split(price=1300.0)]
    assert detect_favourable_passive(_parent(), _pin_(), sp, 5) is None


def test_churn_fires_when_we_keep_sending_into_a_wall():
    f = detect_unfavourable_churn(_parent(sidesign=1), _pin_(), [_split()] * 12)
    assert f is not None and f.rule == "LULD_UNFAVOURABLE_CHURN"
    assert f.severity == "improvement"


def test_churn_needs_more_than_a_couple_of_splits():
    assert detect_unfavourable_churn(_parent(sidesign=1), _pin_(),
                                     [_split()] * 2) is None


def test_blind_suppression_fires_on_price_path_kills_during_a_pin():
    sp = [_split(state="close_bad_price"), _split(state="acked")]
    f = detect_blind_suppression(_parent(), _pin_(), sp)
    assert f is not None and f.rule == "LULD_BLIND_SUPPRESSION"


def test_blind_suppression_is_silent_when_nothing_was_suppressed():
    assert detect_blind_suppression(_parent(), _pin_(), [_split()]) is None


def test_guard_inactive_needs_a_pattern_not_an_incident():
    one = [Finding("LULD_CAP", "violation", "X.CH", 1, 2, 10.0, 1.0, "")]
    assert detect_guard_inactive("X.CH", one) is None
    f = detect_guard_inactive("X.CH", one * 3)
    assert f is not None and f.rule == "LULD_GUARD_INACTIVE"


def test_reject_cluster_needs_a_cluster():
    r = [_split(state="rejected")]
    assert detect_reject_cluster("HK", "SEHK", 10, r * 2) is None
    f = detect_reject_cluster("HK", "SEHK", 10, r * 6)
    assert f is not None and f.rule == "SS_REJECT_CLUSTER"


# --- scoring and report -----------------------------------------------------

def test_impact_is_price_delta_times_size_in_usd():
    f = Finding("LULD_CAP", "violation", "X.JT", 1, 2, 1300.0, 5.0, "")
    assert abs(impact_usd(f, 1000, 0.0068, 1305.0) - 34.0) < 1e-6


def test_impact_for_a_no_split_is_the_unfilled_notional():
    f = Finding("LULD_FAVOURABLE_NO_SPLIT", "opportunity", "X.JT", 1, 0,
                1300.0, 0.0, "")
    assert abs(impact_usd(f, 5000, 0.0068, 1300.0) - 5000 * 1300.0 * 0.0068) < 1e-6


def test_impact_is_zero_without_an_fx_rate_rather_than_wrong():
    f = Finding("LULD_CAP", "violation", "X.JT", 1, 2, 1300.0, 5.0, "")
    assert impact_usd(f, 1000, 0.0, 1305.0) == 0.0


def test_tally_counts_by_market_and_severity():
    t = Tally()
    t.add("JP", "LULD_CAP", "violation")
    t.add("JP", "LULD_CAP", "violation")
    t.add("KR", "SS_TH_LTP1", "deviation")
    assert t.counts[("JP", "LULD_CAP")]["violation"] == 2
    assert t.counts[("KR", "SS_TH_LTP1")]["deviation"] == 1


def test_tally_tracks_unverifiable_separately_from_clean():
    t = Tally()
    t.unverifiable("CN")
    t.unverifiable("CN")
    assert t.unverifiable_n["CN"] == 2


def test_scorecard_marks_unchecked_markets_as_unchecked_not_ok():
    t = Tally()
    t.add("JP", "SS_UPTICK", "violation")
    t.unverifiable("TW")
    out = scorecard(t)
    assert "TW" in out and "unchecked" in out.lower() and "NotOK" in out


def test_scorecard_reports_a_clean_market_as_ok():
    t = Tally()
    t.seen("MY", "SS_UPTICK", 40)
    assert "OK" in scorecard(t)


def test_suppression_footer_says_so_when_nothing_was_suppressed():
    assert "nothing suppressed" in suppression_footer(Tally())


def test_suppression_footer_lists_contradicted_stocks():
    t = Tally()
    t.suppress("JP", "7203.JT")
    out = suppression_footer(t)
    assert "7203.JT" in out and "contradicted" in out


# --- overrides and the chain ------------------------------------------------

def test_band_overrides_load_and_key_on_date_and_sym():
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as fh:
        fh.write("date,sym,limit_up,limit_dn,source\n"
                 "2026.07.16,600584.CH,41.83,34.23,exchange\n"
                 "2026-07-17,7203.JT,1500,1100,exchange\n")
    try:
        ov = load_band_overrides(path)
    finally:
        os.unlink(path)
    b = ov[(dt.date(2026, 7, 16), "600584.CH")]
    assert b.up == 41.83 and b.dn == 34.23
    assert b.src == "override" and b.conf == "confirmed"
    assert (dt.date(2026, 7, 17), "7203.JT") in ov, "both date formats must parse"


def test_resolve_band_prefers_an_override_over_everything():
    ov = {(dt.date(2026, 7, 16), "X.KS"): Band(200.0, 50.0, "override", "confirmed")}
    b = resolve_band("X.KS", "KR", dt.date(2026, 7, 16), 100.0, 0.01,
                     111.0, 89.0, None, None, None, None, ov)
    assert b.src == "override" and b.up == 200.0


def test_resolve_band_uses_target_oms_for_korea():
    b = resolve_band("X.KS", "KR", dt.date(2026, 7, 16), 100.0, 0.01,
                     111.0, 89.0, None, None, None, None, {})
    assert b.src == "target_oms" and b.up == 111.0 and b.conf == "confirmed"


def test_resolve_band_ignores_target_oms_for_japan():
    b = resolve_band("7203.JT", "JP", dt.date(2026, 7, 16), 1000.0, 1.0,
                     9999.0, 1.0, None, None, None, None, {})
    assert b.src == "computed" and b.up == 1300.0


def test_resolve_band_ignores_a_nonpositive_oms_band():
    b = resolve_band("X.KS", "KR", dt.date(2026, 7, 16), 100.0, 0.01,
                     0.0, 0.0, None, None, None, None, {})
    assert b.src == "computed", "a zero oms band means 'unknown', not 'no band'"


def test_resolve_band_returns_none_for_india_without_oms():
    b = resolve_band("RELIANCE.IN", "IN", dt.date(2026, 7, 16), 100.0, 0.05,
                     0.0, 0.0, None, None, None, None, {})
    assert b is None, "India has no computable band"


# --- q sources and cli ------------------------------------------------------

def test_q_sources_are_lambdas_with_typed_parameters():
    for name, src in (("Q_ORDERS", Q_ORDERS), ("Q_BAND", Q_BAND),
                      ("Q_MKT", Q_MKT), ("Q_OMS_BAND", Q_OMS_BAND)):
        s = src.strip()
        assert s.startswith("{["), f"{name} must be a lambda taking named args"
        assert s.endswith("}"), f"{name} must be a closed lambda"


def test_q_sources_never_interpolate_python_values():
    for src in (Q_ORDERS, Q_BAND, Q_MKT, Q_OMS_BAND):
        assert "%s" not in src
        assert ".format(" not in src
        assert "+ str(" not in src


def test_q_sources_constrain_the_partition_column_first():
    flat = " ".join(Q_ORDERS.split())
    for tbl in ("target", "target_stock", "target_state", "workorder"):
        assert f"from {tbl} where date=d" in flat, tbl
    assert "from target_oms where date=d" in " ".join(Q_OMS_BAND.split())
    assert "from qatt where date=d" in " ".join(Q_BAND.split())


def test_q_orders_collapses_workorder_to_one_row_per_id_work():
    assert "by date,id_server,id_work from w" in " ".join(Q_ORDERS.split())


def test_q_orders_excludes_indonesia():
    assert "exctry" in Q_ORDERS
    assert EXCLUDED_COUNTRIES == ("ID",)


def test_check_servers_refuses_placeholders():
    try:
        _check_servers()
    except SystemExit as e:
        assert _PLACEHOLDER in str(e)
        return
    raise AssertionError("must refuse to run against a CHANGEME endpoint")


def test_parse_checks_defaults_to_everything():
    assert parse_checks("all") == set(ALL_RULES)
    assert parse_checks("") == set(ALL_RULES)


def test_parse_checks_selects_a_subset():
    assert parse_checks("luld_cap,ss_uptick") == {"LULD_CAP", "SS_UPTICK"}


def test_parse_checks_refuses_a_typo_rather_than_ignoring_it():
    try:
        parse_checks("LULD_CAPP")
    except SystemExit as e:
        assert "LULD_CAPP" in str(e)
        return
    raise AssertionError("a misspelled check must fail loudly, not disable itself")


def test_daterange_is_inclusive_of_both_ends():
    ds = daterange(dt.date(2026, 7, 16), dt.date(2026, 7, 18))
    assert ds == [dt.date(2026, 7, 16), dt.date(2026, 7, 17), dt.date(2026, 7, 18)]


def test_daterange_rejects_a_reversed_range():
    try:
        daterange(dt.date(2026, 7, 18), dt.date(2026, 7, 16))
    except SystemExit:
        return
    raise AssertionError("a reversed range must be refused, not silently empty")


def test_parser_defaults_match_the_spec():
    a = build_parser().parse_args(["--start", "2026-07-16", "--end", "2026-07-18"])
    assert a.pin_mins == 5
    assert a.chase_ticks == 2
    assert a.chase_secs == 30
    assert a.checks == "all"


def test_parser_accepts_the_documented_flags():
    a = build_parser().parse_args(
        ["--start", "2026-07-16", "--end", "2026-07-16", "--country", "CN",
         "--band-file", "b.csv", "--out-dir", "out", "--diagnose", "--quiet"])
    assert a.country == "CN" and a.band_file == "b.csv"
    assert a.out_dir == "out" and a.diagnose and a.quiet


# --- acceptance -------------------------------------------------------------

def test_acceptance_the_600584_case_from_the_notes():
    """1370265478 / 600584.CH, 16 July - a split generated BELOW limit down.

    600584 is SSE main board, so +/-10%.  A previous close of 38.03 puts limit
    down at 34.23; the split went out at 34.00.  If this does not flag, the
    script does not work.
    """
    d = dt.date(2026, 7, 16)
    band = compute_band(38.03, "CN", "600584.CH", d, 0.01)
    assert band is not None
    assert abs(band.dn - 34.23) < 0.02, f"limit down came out {band.dn}"
    sp = Split(id_target=1370265478, id_work=1, sym="600584.CH", country="CN",
               side="sell", sidesign=-1, otype="limit", price=34.00, size=1000,
               state="acked", t_transmit=0, parent_limit=0.0, tick=0.01,
               q_bid=34.10, q_ask=34.30, q_last=34.20, q_trdtick=0,
               t_bid=34.10, t_ask=34.30, t_last=34.20)
    caps = [f for f in run_rules(sp, band, "qatt") if f.rule == "LULD_CAP"]
    assert caps, "the known bad split must be flagged"
    assert caps[0].severity == "violation"
    assert "below limit down" in caps[0].reason


def run_self_test() -> int:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


# =============================================================================
# MAIN
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Audit child splits against LULD and short sell rules.")
    p.add_argument("--start", type=_parse_date, help="first date, YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, help="last date, inclusive")
    p.add_argument("--country", default="", help="restrict to one market")
    p.add_argument("--checks", default="all",
                   help="comma list of rule/detector ids, or 'all'")
    p.add_argument("--band-file", default="",
                   help="CSV of known bands, overriding every computed layer")
    p.add_argument("--pin-mins", type=int, default=5,
                   help="minimum pin minutes before the no-split family fires")
    p.add_argument("--chase-ticks", type=int, default=2,
                   help="SS_HK_CHASE: ticks the ask must move")
    p.add_argument("--chase-secs", type=int, default=30,
                   help="SS_HK_CHASE: seconds without a reprice")
    p.add_argument("--out-dir", default="", help="also write report.xlsx here")
    p.add_argument("--diagnose", action="store_true",
                   help="first date only; distinct values and stage row counts")
    p.add_argument("--quiet", action="store_true",
                   help="no per-date progress on stderr")
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in tests; needs no kdb connection")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return run_self_test()
    if not args.start or not args.end:
        raise SystemExit("--start and --end are required (or pass --self-test)")
    # validated before anything touches kdb, so a typo fails in a second rather
    # than after a connection and a day of queries
    parse_checks(args.checks)
    daterange(args.start, args.end)
    _check_servers()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())


# =============================================================================
# NOTES - the judgement calls, each with the one line that reverses it
#
# 1. A contradicted band is DISCARDED, not reported (reconcile_band returns
#    None).  A wrong band produces confident nonsense.  To report them anyway,
#    return `computed._replace(conf="contradicted")` instead of None - and read
#    the suppression footer first to see how many stocks that is.
#
# 2. Japan WIDENS instead of discarding, because there the cause is known:
#    limits expand overnight after a limit close.  WIDEN_ON_CONTRADICTION.
#
# 3. Config-dependent rules are DEVIATIONS, not violations - LULD_OFFSET,
#    SS_TH_LTP1 above the floor.  We cannot see the engine's config, so the
#    offset distribution is the evidence.  A market consistently 1 tick off is
#    a setting, not hundreds of failures.
#
# 4. Short sell checks run on FIVE markets only.  CN, TW and IN have real rules
#    - China an uptick against the latest trade, Taiwan a floor at the previous
#    close conditional on a 3.5% fall, India no price rule at all - but they are
#    researched rather than confirmed.  Set ss_rule in MARKETS to enable one.
#
# 5. target_oms is read for IN and KR only (BAND_FROM_TARGET_OMS).  It is
#    inconsistent elsewhere.  Widen the tuple only on the null rates --diagnose
#    prints, never on optimism.
#
# 6. Where a band must be guessed, it is guessed WIDE - Chinese ST names come
#    back 10% when they are really 5%.  That misses violations rather than
#    inventing them, and only the inventing kind makes the report ignorable.
#
# 7. Unfilled splits are IN SCOPE.  Two of the reported problems are about
#    splits that never filled; excluding them would hide the thing being asked
#    about.
#
# 8. Indonesia is excluded rather than carried as unverifiable.  A market that
#    can only ever emit RULE_UNKNOWN adds a row to every table and no
#    information.  Add it back in MARKETS plus a tier function.
# =============================================================================
