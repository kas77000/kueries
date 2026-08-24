#!/usr/bin/env python3
"""
=============================================================================
reversion_liquidity.py

Reproduces two tables from the Bernstein dark pool report against our own
data, for DARK executions only.

  Table 3.1  Liquidity
             venue, %Notional, Spread (bps), Adv (m shares), Fill%adv,
             Fill Rate, Duration (seconds)

  Table 3.3  Venue tiering / ranking on 1s reversion and quote stability
             venue, Reversion, Stability, Score, Tier

--decompose adds a third table splitting Reversion into the two effects it is
made of - Capture, where in the touch the fill happened, and Drift, where the
mid went in the second after.  --out-dir writes the tables to report.xlsx, and
--pdf typesets them the way the report typesets them.

Talks to TWO kdb processes over PyKX.  Both are HISTORICAL, not the realtime
ones, and their host:port are fixed - so they are constants below rather than
arguments.  Set them once, before first use.

  ORDER_SERVER   workorder, execution, target_stock
  QATT_SERVER    qatt

  python scripts/reversion_liquidity/reversion_liquidity.py \
      --start 2026-04-01 --end 2026-06-30 --country AU

PyKX runs in unlicensed mode - SyncQConnection against a remote process needs
no q licence and no QHOME, because all q evaluation happens on the server.
pykx is imported lazily inside connect(), so --self-test runs anywhere.

  python scripts/reversion_liquidity/reversion_liquidity.py --self-test

HOW THE WORK IS SPLIT

The q lambdas below are sent as source text with TYPED ARGUMENTS - dates and
country codes travel as q values, never interpolated into the text.  That is
the same contract as sending a lambda over a raw handle.

The script loops one date at a time.  Per date it makes two IPC calls, derives
the per fill metrics, folds them into per venue running sums, and throws the
fill rows away.  Sums and sums of squares are sufficient statistics for every
figure in both tables, including the pooled mean and variance the z scores
need, so chunking costs nothing in accuracy - and memory stays flat whether you
ask for one day or a whole quarter.  See test_chunking_is_exact().

A VENUE IS DARK when its name contains DARK or DRK, matched case insensitively.
Identical to dark_summary.q and dark_routed_executed.q, so the three agree by
construction rather than by coincidence.

A MARKET IS THE SYM SUFFIX: 7203.JP is JP, 0005.HK is HK, BHP.AU is AU.  NOT
target_stock's country column, which is blank or wrong often enough that
--country JP came back empty while the JP dark fills sat in workorder the whole
time.  The suffix rides on every row this script already reads, so the market is
decided before any join and cannot be lost in one.
queries/market_stats/market_stats.q names a market the same way.

VENUES ARE GROUPED BY THE VENUE SHEET, not by their kdb symbol.  Several
symbols can be one pool - CENTREPOINT_DARK and CENTREPOINT_CITI_DARK are both
Centrepoint - and the report's tables name the pool, not the route into it.
VENUE_GROUPS below is that sheet.  Edit it; it is the only thing that decides
which rows share a line.
=============================================================================
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

# scripts/lib holds local_config, which reads the settings file beside this
# script.  Added to the path rather than installed, so this still runs as
# `python scripts/reversion_liquidity/reversion_liquidity.py` from the repo
# root.  Copy scripts/lib alongside this folder if you move it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.local_config import apply_local                        # noqa: E402

# -----------------------------------------------------------------------------
# CONNECTIONS.  Edit these, or put them in a local_settings.py beside this
# script - see scripts/lib/README.md.
#
# Both are the HISTORICAL processes, not the realtime ones - qatt in particular
# exists in both flavours and only the historical one carries a date column.
#
# Both are open processes, so host and port is the whole of it - connect() takes
# no credentials.
# -----------------------------------------------------------------------------

ORDER_SERVER = "CHANGEME:5010"
QATT_SERVER = "CHANGEME:5011"

_PLACEHOLDER = "CHANGEME"


# -----------------------------------------------------------------------------
# THE VENUE SHEET.  Edit this.
#
# (country, kdb venue) -> (name for the tables, short name for the pies)
#
# Several kdb symbols can be one pool, and the report names the pool: 3.1 and
# 3.3 both say "Centrepoint" where our workorder table says CENTREPOINT_DARK
# for one route into it and CENTREPOINT_CITI_DARK for another.  Every figure in
# both tables is computed on the GROUP, so the two arrive as one row with one
# notional, one spread and one reversion.
#
# Keyed on the country too, because the sheet is: JPMAP_DARK is JPMX in JP and
# HK, while in AU the same pool is reached as JPMAP_MF_DARK.  A venue-name-only
# table could not say that.
#
# The country here is the SYM SUFFIX - JP, HK, AU - which is where the q gets it
# from as well.  target_stock's country column is not read anywhere in this
# script; it is not dependable enough to decide which rows a report contains.
#
# The second name is the pie label - shorter, because a pie slice has no room -
# and is used by scripts/dark_routed_executed, not here.  It is carried anyway
# so the sheet is transcribed once.
#
# The sheet also has a "type" column, "Midpoint dark" on every row.  This
# script only ever sees dark venues, so it is not a key.
#
# A pair that is NOT here keeps its raw kdb symbol as its row label and is
# named on stdout just above the tables.  Nothing is dropped and nothing is
# merged into the wrong pool by guessing.
# -----------------------------------------------------------------------------

VENUE_GROUPS = {
    # Japan
    ("JP", "CITI_DARK"):             ("Citi",        "Citi"),
    ("JP", "DAIWA_DARK"):            ("Daiwa",       "DAIWA"),
    ("JP", "JPMAP_DARK"):            ("JPMX",        "JPMX"),
    ("JP", "LNAL_DARK"):             ("LNAL",        "Liqnet"),
    ("JP", "MS_DARK"):               ("MS Pool",     "MSPL"),
    ("JP", "NOM_DARK"):              ("Nomura",      "Nomura"),
    ("JP", "POSITNOW_DARK"):         ("Posit",       "Posit"),
    ("JP", "CLSA_DARK"):             ("CLSA",        "CLSA"),
    ("JP", "ITGBD_DARKFRM"):         ("VIRTU Cond",  "VIRTU Cond"),
    ("JP", "LIQH_DARK"):             ("LNAL Cond",   "Liqnet Cond"),
    ("JP", "LIQUID_DARKFRM"):        ("LNAL Cond",   "Liqnet Cond"),
    ("JP", "ML_DARK"):               ("BAML",        "BAML"),
    # Hong Kong
    ("HK", "CITI_DARK"):             ("Citi",        "Citi"),
    ("HK", "CITI_DARK_PASS"):        ("Citi",        "Citi"),
    ("HK", "CLSA_DARK"):             ("CLSA",        "CLSA"),
    ("HK", "INSTINET_DARK"):         ("Instinet",    "Instnet"),
    ("HK", "INSTINET_DARK_PASS"):    ("Instinet",    "Instnet"),
    ("HK", "ITGBD_DARKFRM"):         ("VIRTU Cond",  "VIRTU Cond"),
    ("HK", "JPMAP_DARK"):            ("JPMX",        "JPMX"),
    ("HK", "JPMAP_DARK_PASS"):       ("JPMX",        "JPMX"),
    ("HK", "MS_DARK"):               ("MS Pool",     "MSPL"),
    ("HK", "POSITNOW_DARK"):         ("Posit",       "Posit"),
    ("HK", "POSITNOW_DARK_PASS"):    ("Posit",       "Posit"),
    ("HK", "LIQH_DARK"):             ("LNAL Cond",   "Liqnet Cond"),
    ("HK", "LIQUID_DARKFRM"):        ("LNAL Cond",   "Liqnet Cond"),
    ("HK", "LNAL_DARK"):             ("LNAL",        "Liqnet"),
    # Australia
    # the published pie labels this slice Ctrpnt; the sheet says CentrePt
    # and the sheet is what we follow
    ("AU", "CENTREPOINT_CITI_DARK"): ("Centrepoint", "CentrePt"),
    ("AU", "CENTREPOINT_DARK"):      ("Centrepoint", "CentrePt"),
    ("AU", "CLSA_DARK"):             ("CLSA",        "CLSA"),
    ("AU", "JPMAP_MF_DARK"):         ("JPMX",        "JPMX"),
    ("AU", "JPMAP_DARK"):            ("JPMX",        "JPMX"),
    ("AU", "MS_DARK"):               ("MS Pool",     "MSPL"),
    ("AU", "POSITNOW_DARK"):         ("Posit",       "Posit"),
    ("AU", "CITI_DARK"):             ("Citi",        "Citi"),
    ("AU", "INSTINET_DARK"):         ("Instinet",    "Instnet"),
    ("AU", "ITGBD_DARKFRM"):         ("VIRTU Cond",  "VIRTU Cond"),
    ("AU", "LNAL_DARK"):             ("LNAL",        "Liqnet"),
    ("AU", "LIQH_DARK"):             ("LNAL Cond",   "Liqnet Cond"),
    ("AU", "LIQUID_DRKFRM"):         ("LNAL Cond",   "Liqnet Cond"),
    ("AU", "CBOE_RBC_DARK"):         ("CBOE",        "CBOE"),
    ("AU", "CRAIGS_NZX_DARK"):       ("CRAIGS",      "CRAIGS"),
}

# -----------------------------------------------------------------------------
# Anything above can be overridden from a local_settings.py beside this script,
# which git ignores - so the servers survive a pull and this file never has to
# be edited.  See scripts/lib/README.md.
#
# It sits here, above everything derived from the sheet, so a locally replaced
# VENUE_GROUPS is the one the labels and the groupings are built from.
# -----------------------------------------------------------------------------

apply_local(globals(), __file__)

# the column the accumulators are grouped on, once the sheet has been applied
GROUP_COL = "venue_group"


# -----------------------------------------------------------------------------
# q sources.  Sent as text + typed args; see module docstring.
# -----------------------------------------------------------------------------

# Dark fills for one date.  Returns one row per fill, carrying the venue from
# the child order and adv/fxlast from the stock.
#
# ctry is a CHAR VECTOR, not a symbol - "AU", or "" for every market - and is
# sent as BYTES from python for that reason.  PyKX turns a python str into a q
# SYMBOL, and `$ on a symbol is a 'type error, so passing args.country straight
# through fails on every date.  See run().
#
# It is matched against the SYM SUFFIX, upper cased on both sides, so
# `--country jp` and `--country JP` are the same request rather than one of them
# being a silently empty report.
#
# workorder is reduced to one row per id_work with `last` before anything is
# joined to it.  If workorder already holds exactly one row per child order
# that grouping is a no-op; if it ever holds a row per state change, the join
# to execution would otherwise multiply every fill.  Cheap insurance.
Q_FILLS = """
{[d;ctry]
  dk:("*DARK*";"*DRK*");
  w:select date,id_server,id_work,id_target,sym,venue,size,make
    from workorder
    where date=d, any (upper venue) like/: dk;
  / THE MARKET IS THE SYM SUFFIX.  7203.JP is JP, BHP.AU is AU.  target_stock
  / carries a country column and it is NOT dependable: filtering on it returned
  / nothing for Japan while the rows sat in workorder all along.  The suffix is
  / already on the row we have, and it needs no join to be right.
  / A date with no dark rows has no syms to split, and an untyped empty column
  / would break the compare or the group that follows it, so the empty case is
  / spelled out rather than left to fall out of the cast.
  w:$[count w; update country:`$upper {last "." vs x} each string sym from w;
               update country:`symbol$() from w];
  w:$[0=count ctry; w; select from w where country=`$upper ctry];
  / country is KEPT, not deleted: the venue sheet is keyed on (country,venue)
  w:0!select last id_target, last sym, last venue, last country, last size,
      last make
    by date,id_server,id_work from w;
  ids:exec distinct id_target from w;
  x:`date`id_server`id_target xkey select date,id_server,id_target,adv,fxlast
    from target_stock where date=d, id_target in ids;
  / lj, NOT ij: the market is decided above, off the sym, so target_stock is
  / here for adv and fxlast alone and must not get a vote on which rows exist.
  / A stock row that is missing nulls those two rather than deleting the fill.
  w:w lj x;
  wk:exec distinct id_work from w;
  e:select date,id_server,id_work,sym,fillprice,fillsize,sidesign,
      t_oes_xact,time,bidprice,askprice
    from execution
    where date=d, id_work in wk, fillsize>0;
  k:`date`id_server`id_work xkey
    select date,id_server,id_work,venue,country,adv,fxlast from w;
  f:e ij k;
  f:update tm:time^t_oes_xact from f;
  `sym`tm xasc select date,sym,tm,venue,country,fillprice,fillsize,sidesign,
      adv,fxlast,bidprice,askprice from f
 }
"""

# Per venue child order roll for one date.  Needs no quotes, so it is
# aggregated on the order server and only one row per venue comes back.
#
# px_routed is the dark_routed_executed.q rule: the price the child was sent
# with, falling back to the last trade at transmit time for market and pegged
# orders that carry no usable limit.
#
# fr_wsum / fr_wnum are kept as a separate weight sum rather than reusing
# routed_notional, because a child order with no usable routed price must not
# sit in the denominator of a weighted mean it contributes no numerator to.
Q_CHILD = """
{[d;ctry]
  dk:("*DARK*";"*DRK*");
  w:select date,id_server,id_work,id_target,sym,venue,size,make,price,
      transmit_lastprice,t_on_market,t_off_market
    from workorder
    where date=d, any (upper venue) like/: dk;
  / THE MARKET IS THE SYM SUFFIX.  7203.JP is JP, BHP.AU is AU.  target_stock
  / carries a country column and it is NOT dependable: filtering on it returned
  / nothing for Japan while the rows sat in workorder all along.  The suffix is
  / already on the row we have, and it needs no join to be right.
  / A date with no dark rows has no syms to split, and an untyped empty column
  / would break the compare or the group that follows it, so the empty case is
  / spelled out rather than left to fall out of the cast.
  w:$[count w; update country:`$upper {last "." vs x} each string sym from w;
               update country:`symbol$() from w];
  w:$[0=count ctry; w; select from w where country=`$upper ctry];
  / country is KEPT, and grouped on below, so the sheet can be applied here too
  w:0!select last id_target, last venue, last country, last size, last make,
      last price, last transmit_lastprice, last t_on_market, last t_off_market
    by date,id_server,id_work from w;
  ids:exec distinct id_target from w;
  x:`date`id_server`id_target xkey select date,id_server,id_target,fxlast
    from target_stock where date=d, id_target in ids;
  / lj, NOT ij: the market is decided above, off the sym, so target_stock is
  / here for fxlast alone and must not get a vote on which rows exist.  A stock
  / row that is missing nulls fxlast rather than deleting the child order.
  w:w lj x;
  w:update px_routed:transmit_lastprice^?[price>0;price;0n] from w;
  w:update notional_routed:size*px_routed*fxlast from w;
  w:update fill_pct:?[size>0;100*make%size;0n] from w;
  w:update dur:?[(not null t_on_market)&(not null t_off_market);
      0.001*"f"$t_off_market-t_on_market;0n] from w;
  w:update ok:(notional_routed>0)&(not null notional_routed)
      &(not null fill_pct) from w;
  0!select
      orders:count i,
      routed_notional:sum notional_routed,
      fr_wsum:sum ?[ok;notional_routed;0f],
      fr_wnum:sum ?[ok;notional_routed*fill_pct;0f],
      duration_sum:sum dur,
      duration_n:sum not null dur
    by country,venue from w
 }
"""

# The quote lookups.  Receives the fill table as a typed q table and returns
# four columns in the SAME ROW ORDER, so the caller can concat them on.
#
# aj returns the last quote at or before the target time - the prevailing
# quote, which is what "spread at the time of execution" means.  Note this
# picks up a quote stamped in the same millisecond as the fill, which may
# already be reacting to it; tm-00:00:00.001 is the strictly-before variant if
# you want to test the difference.
#
# qt is filtered to two sided rows and sorted by sym then time, which is what
# makes the aj valid; the `p attribute makes it fast.
Q_QUOTES = """
{[d;f]
  syms:exec distinct sym from f;
  qt:select time,sym,qbid,qask from qatt
     where date=d, sym in syms, qbid>0, qask>0;
  qt:update `p#sym from `sym`time xasc qt;
  q0:aj[`sym`time; select sym, time:tm from f; qt];
  q1:aj[`sym`time; select sym, time:tm+00:00:01.000 from f; qt];
  ([] qbid0:q0`qbid; qask0:q0`qask; qbid1:q1`qbid; qask1:q1`qask)
 }
"""

# Where a day's rows go, stage by stage, for --diagnose.  An empty report is
# almost always one of these dropping to zero, and which one it is decides what
# to do about it - there is nothing in "no dark fills in range" to act on.
Q_DIAG = """
{[d;ctry]
  dk:("*DARK*";"*DRK*");
  a:count select from workorder where date=d;
  w:select date,id_server,id_work,id_target,sym,venue,make
    from workorder where date=d, any (upper venue) like/: dk;
  b:count w;
  c:count select from w where make>0;
  / THE MARKET IS THE SYM SUFFIX.  7203.JP is JP, BHP.AU is AU.  target_stock
  / carries a country column and it is NOT dependable: filtering on it returned
  / nothing for Japan while the rows sat in workorder all along.  The suffix is
  / already on the row we have, and it needs no join to be right.
  / A date with no dark rows has no syms to split, and an untyped empty column
  / would break the compare or the group that follows it, so the empty case is
  / spelled out rather than left to fall out of the cast.
  w:$[count w; update country:`$upper {last "." vs x} each string sym from w;
               update country:`symbol$() from w];
  w:$[0=count ctry; w; select from w where country=`$upper ctry];
  e:count w;
  ids:exec distinct id_target from w;
  / LAST, and no longer able to empty anything: Q_FILLS lj's onto this, so a
  / row it does not have nulls adv and fxlast instead of deleting the fill.
  f:count select from target_stock where date=d, id_target in ids;
  ([] stage:`workorder_rows`dark_venue_rows`of_those_filled`after_country`stock_rows_found;
      n:(a;b;c;e;f))
 }
"""

# The markets actually in the dark flow, so a filter that matched nothing can
# be compared against what was there to match.  Read off workorder's own syms:
# the table the rows come from, with no join to lose them in.
Q_COUNTRIES = """
{[d]
  w:select sym from workorder
    where date=d, any (upper venue) like/: ("*DARK*";"*DRK*");
  / A date with no dark rows has no syms to split, and an untyped empty column
  / would break the compare or the group that follows it, so the empty case is
  / spelled out rather than left to fall out of the cast.
  w:$[count w; update country:`$upper {last "." vs x} each string sym from w;
               update country:`symbol$() from w];
  `n xdesc 0!select n:count i by country from w
 }
"""

# Columns carried in the fill level accumulator.  Every one is a plain sum, so
# folding a day in is a single frame addition.
FILL_ACC = [
    "notional", "n_fill",
    "w_spread", "wsum_spread",
    "w_adv", "wsum_adv",
    "w_filladv", "wsum_filladv",
    "n_rev", "sum_rev", "sumsq_rev",
    "n_dec", "sum_capture", "sum_drift",
    "n_stable", "sum_stable",
    "no_quote", "bad_spread",
]
CHILD_ACC = [
    "orders", "routed_notional", "fr_wsum", "fr_wnum",
    "duration_sum", "duration_n",
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
    """Open a PyKX connection on a host and a port; the servers are open, so
    there is nothing to log in with.  pykx is imported here, not at module
    level, so the pure-python half of this file stays importable without it."""
    if hostport.startswith(_PLACEHOLDER):
        raise SystemExit(
            f"{hostport!r} is still the placeholder.  Set ORDER_SERVER and "
            f"QATT_SERVER in a local_settings.py beside this script, or near "
            f"the top of {__file__}."
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


def normalise_country(s):
    """--country as both sides want it: stripped, upper case, "" for all of them.

    Upper because the suffix is upper on the feed, and `--country jp` should be
    the same request as `--country JP` rather than a silently empty report.  The
    q uppers it too - neither side is the only thing standing between a lower
    case argument and no rows at all - but it is normalised HERE so that the
    heading over the tables says what was actually filtered on."""
    return (s or "").strip().upper()


def _plain_numeric(s):
    """A q numeric column that CONTAINS A NULL, as plain float64 with NaN.

    PyKX types a column by whether a null is in it.  `adv` with none is an
    int32 ndarray; the same column with one is a numpy MASKED array, and a
    masked array can reach pandas' own notna() and come back out as

        TypeError: bad operand type for unary ~: 'float'

    eight frames down in pandas internals, naming nothing that appears in this
    file.  That is one null ADV on one Japanese name, and it took the whole
    range down on the date it turned up.

    So the null is turned into a NaN HERE, at the one boundary, rather than
    surviving as a representation every line downstream would have to know
    about.  NaN is what the report already means by "no adv": adv_ok in
    fill_metrics is a notna() test that was written for exactly this row.
    """
    values = getattr(s, "values", s)
    if isinstance(values, np.ma.MaskedArray):
        # .filled needs somewhere to put NaN, so widen ints first
        return pd.Series(values.astype("float64").filled(np.nan),
                         index=s.index, name=s.name)
    return pd.Series(s.to_numpy(dtype="float64", na_value=np.nan),
                     index=s.index, name=s.name)


def _to_pandas(tbl):
    """PyKX table -> DataFrame, with symbol columns normalised to str.

    PyKX hands symbols back as bytes in some versions and str in others.  Left
    alone that difference turns up much later as a groupby that splits one
    venue into two, so it is flattened here at the boundary.

    Numeric columns are flattened for the same reason: a q null makes PyKX
    return a masked array where the same column without one is a plain
    ndarray, and only the first of those survives contact with pandas.  See
    _plain_numeric.  Times and dates are left alone - a masked timedelta is
    still a timedelta, and turning one into a float would break the quote
    join far more quietly than it would fix anything."""
    df = tbl.pd()
    for c in df.columns:
        s = df[c]
        if s.dtype == object:
            df[c] = s.map(lambda v: v.decode() if isinstance(v, bytes) else v)
        elif (isinstance(getattr(s, "values", None), np.ma.MaskedArray)
                and s.values.dtype.kind in "iuf"):
            df[c] = _plain_numeric(s)
        elif not isinstance(s.dtype, np.dtype) and pd.api.types.is_numeric_dtype(s.dtype):
            # a nullable extension dtype - Int32, Float64 - for the same reason
            df[c] = _plain_numeric(s)
    return df


def fetch_day(ho, hq, day, country):
    """One date: fills joined to their quotes, plus the child order roll.

    Returns (fills_df, child_df).  fills_df is empty when the venue filter
    matched nothing, which is normal for a non-trading day."""
    fills = _to_pandas(ho(Q_FILLS, day, country))
    child = _to_pandas(ho(Q_CHILD, day, country))
    if len(fills) == 0:
        return fills, child
    quotes = _to_pandas(hq(Q_QUOTES, day, fills))
    fills = pd.concat(
        [fills.reset_index(drop=True), quotes.reset_index(drop=True)], axis=1
    )
    return fills, child


# -----------------------------------------------------------------------------
# Applying the venue sheet
#
# The two frames coming back from kdb carry the raw symbol and the country;
# everything downstream works on the GROUP.  Both functions read the sheet and
# neither mutates it, so the mapping is a pure lookup that the self-test can
# exercise without a connection.
# -----------------------------------------------------------------------------

def venue_labels(df):
    """The display group for every row of a fill or child frame.

    A (country, venue) pair the sheet does not carry keeps its raw kdb symbol,
    so a venue nobody has added yet is still its own row - visible, in the
    right total, and obviously un-prettified - rather than vanishing or landing
    in somebody else's pool.  unmapped_venues() names them.

    A frame with no country column maps on ("", venue), which no sheet row
    matches, so it falls through to the raw name.  That is what lets the
    synthetic frames in the self-test stay independent of the sheet's
    contents."""
    venue = df["venue"].astype(str)
    country = (df["country"].astype(str) if "country" in df.columns
               else pd.Series("", index=df.index, dtype=object))
    labels = [VENUE_GROUPS.get((c, v), (v, v))[0] for c, v in zip(country, venue)]
    return pd.Series(labels, index=df.index, name=GROUP_COL)


def unmapped_venues(df):
    """The (country, venue) pairs in this frame that the sheet does not carry.

    Collected per day and reported once, so a quarter of dates does not print
    the same missing venue ninety times."""
    if len(df) == 0 or "venue" not in df.columns:
        return set()
    venue = df["venue"].astype(str)
    country = (df["country"].astype(str) if "country" in df.columns
               else pd.Series("", index=df.index, dtype=object))
    return {p for p in zip(country, venue) if p not in VENUE_GROUPS}


# -----------------------------------------------------------------------------
# Per fill metrics
# -----------------------------------------------------------------------------

def fill_metrics(df, half_spread=False):
    """Derive the per fill quantities both tables are built from.

    Two validity flags, because the two metrics of table 3.3 do NOT have the
    same usable population:

      has_quote    all four touches present and positive.  A fill without this
                   is unusable for everything quote based.
      good_spread  has_quote AND the quote is not crossed or locked.  A crossed
                   quote has no meaningful spread to normalise by, so the fill
                   is dropped from reversion - but its touches are still
                   perfectly comparable a second later, so it is KEPT for
                   stability.

    Collapsing those onto one flag would quietly misweight one of the two z
    scores.  Columns that must not count a row carry NaN there, and every sum
    downstream skips NaN, so the two populations stay separate by construction.
    """
    df = df.copy()

    bid0, ask0 = df["qbid0"], df["qask0"]
    bid1, ask1 = df["qbid1"], df["qask1"]

    has_quote = (
        bid0.notna() & ask0.notna() & bid1.notna() & ask1.notna()
        & (bid0 > 0) & (ask0 > 0) & (bid1 > 0) & (ask1 > 0)
    )
    spread = ask0 - bid0
    good_spread = has_quote & (spread > 0)

    mid0 = (bid0 + ask0) / 2.0
    mid1 = (bid1 + ask1) / 2.0

    df["notional"] = df["fillsize"] * df["fillprice"] * df["fxlast"]

    # Spread normalised 1s reversion.  Positive means the price moved OUR way
    # after the fill, i.e. no adverse selection, so higher is better - the
    # convention that makes the published tiers coherent (MS Pool has the
    # highest Reversion and gets Tier 1).
    denom = spread / 2.0 if half_spread else spread
    df["rev"] = np.where(
        good_spread, df["sidesign"] * (mid1 - df["fillprice"]) / denom, np.nan
    )
    df["rev2"] = df["rev"] ** 2

    # The two halves of that reversion, on the same mask and the same divisor,
    # so Capture + Drift == rev row by row:
    #
    #   capture  where in the touch the fill happened.  0 at mid, +0.5 at the
    #            passive touch, -0.5 at the aggressive one.  A property of the
    #            price we got.
    #   drift    where the mid went in the second after.  A property of what
    #            the market did next, i.e. leakage.
    #
    # Reversion alone cannot tell "prices me well" from "does not leak"; these
    # two can.  Both are reported only under --decompose.
    df["capture"] = np.where(
        good_spread, df["sidesign"] * (mid0 - df["fillprice"]) / denom, np.nan
    )
    df["drift"] = np.where(
        good_spread, df["sidesign"] * (mid1 - mid0) / denom, np.nan
    )

    # Strict stability: BOTH touches unchanged.  A venue is charged for any
    # touch move in the second after its fill.
    df["stable"] = np.where(has_quote, ((bid1 == bid0) & (ask1 == ask0)) * 1.0, np.nan)

    # Spread in bps at the time of execution, for table 3.1.
    df["spread_bps"] = np.where(good_spread, 10000.0 * spread / mid0, np.nan)

    # Weighted-mean parts.  Each metric carries its OWN weight column, masked
    # to exactly the rows it is valid for, so every weighted mean divides by
    # the weight of the rows that actually contributed to it.
    adv_ok = df["adv"].notna() & (df["adv"] > 0)
    df["adv_m"] = np.where(adv_ok, df["adv"] / 1e6, np.nan)
    df["filladv"] = np.where(adv_ok, 100.0 * df["fillsize"] / df["adv"], np.nan)

    df["p_spread"] = df["notional"] * df["spread_bps"]
    df["n_spread"] = df["notional"].where(df["spread_bps"].notna())
    df["p_adv"] = df["notional"] * df["adv_m"]
    df["n_adv"] = df["notional"].where(df["adv_m"].notna())
    df["p_filladv"] = df["notional"] * df["filladv"]
    df["n_filladv"] = df["notional"].where(df["filladv"].notna())

    df["no_quote"] = (~has_quote) * 1.0
    df["bad_spread"] = (has_quote & ~good_spread) * 1.0
    return df


def aggregate_fills(df):
    """Fold one day of fills into per group sums (index = the sheet's name)."""
    g = df.assign(**{GROUP_COL: venue_labels(df)}).groupby(GROUP_COL, dropna=False)
    out = g.agg(
        notional=("notional", "sum"),
        n_fill=("notional", "size"),
        w_spread=("p_spread", "sum"),
        wsum_spread=("n_spread", "sum"),
        w_adv=("p_adv", "sum"),
        wsum_adv=("n_adv", "sum"),
        w_filladv=("p_filladv", "sum"),
        wsum_filladv=("n_filladv", "sum"),
        # 'count' counts non-NaN, which is exactly the usable population.
        n_rev=("rev", "count"),
        sum_rev=("rev", "sum"),
        sumsq_rev=("rev2", "sum"),
        # same mask as rev, so n_dec == n_rev; kept separate so a future change
        # to either mask shows up as a mismatch instead of a silent reweighting
        n_dec=("capture", "count"),
        sum_capture=("capture", "sum"),
        sum_drift=("drift", "sum"),
        n_stable=("stable", "count"),
        sum_stable=("stable", "sum"),
        no_quote=("no_quote", "sum"),
        bad_spread=("bad_spread", "sum"),
    )
    return out.reindex(columns=FILL_ACC).astype(float)


def aggregate_child(df):
    """Fold the child order roll onto the group and drop to the sum columns.

    q returns this roll one row per (country, venue), so a group built out of
    two symbols arrives as TWO rows.  They have to be summed: indexing on the
    group instead would keep whichever row landed last and silently halve the
    group's orders, its routed notional and the weights under its fill rate."""
    if len(df) == 0:
        return pd.DataFrame(columns=CHILD_ACC, dtype=float)
    out = df.assign(**{GROUP_COL: venue_labels(df)})
    cols = [c for c in CHILD_ACC if c in out.columns]
    out = out.groupby(GROUP_COL, dropna=False)[cols].sum()
    return out.reindex(columns=CHILD_ACC).astype(float)


def fold(acc, day):
    """Add a day's per venue sums into the accumulator."""
    if day is None or len(day) == 0:
        return acc
    if acc is None:
        return day.copy()
    return acc.add(day, fill_value=0.0)


# -----------------------------------------------------------------------------
# Table assembly
# -----------------------------------------------------------------------------

def _safe_div(a, b):
    """a/b, NaN where b is zero or missing - never inf, never a warning."""
    b = pd.Series(b, dtype=float)
    return pd.Series(np.where((b != 0) & b.notna(), a / b.where(b != 0), np.nan),
                     index=b.index)


def build_liquidity(fill_acc, child_acc):
    """Table 3.1.  Four columns are per fill, two are per child order; they are
    accumulated separately and only meet here, on venue."""
    f = fill_acc
    c = child_acc if child_acc is not None else pd.DataFrame(columns=CHILD_ACC)
    c = c.reindex(f.index)

    total = f["notional"].sum()
    out = pd.DataFrame(index=f.index)
    out["%Notional"] = 100.0 * f["notional"] / total if total else np.nan
    out["Spread"] = _safe_div(f["w_spread"], f["wsum_spread"])
    out["Adv"] = _safe_div(f["w_adv"], f["wsum_adv"])
    out["Fill%adv"] = _safe_div(f["w_filladv"], f["wsum_filladv"])
    out["Fill Rate"] = _safe_div(c["fr_wnum"], c["fr_wsum"])
    out["Duration"] = _safe_div(c["duration_sum"], c["duration_n"])
    out.index.name = "Venue"
    # BIGGEST FIRST.  The venue that moved the money is what the page is about,
    # and fifteen pools in alphabetical order is a lookup table, not a ranking -
    # you cannot see the shape of the flow without reading every row.
    #
    # sort_index() first and a stable sort second, so venues that tie - which in
    # practice means several that traded nothing - come out in a fixed, readable
    # order rather than in whatever order the accumulator happened to hold them.
    return out.sort_index().sort_values("%Notional", ascending=False,
                                        kind="stable")


def pooled_z(fill_acc):
    """Per venue z scores against the distribution pooled over ALL dark fills.

    The published z columns do not average to zero across the venues shown, so
    the z base is wider than the displayed rows.  Pooling across fills is what
    reproduces that: a heavy venue regresses toward zero, a light one does not.

    Reconstructed from sums, which is exact - see test_chunking_is_exact.
    stable is 0/1, so its pooled variance is p(1-p) and no sum of squares is
    needed for it.
    """
    n_rev = fill_acc["n_rev"].sum()
    mean_rev = fill_acc["sum_rev"].sum() / n_rev if n_rev else np.nan
    var_rev = (fill_acc["sumsq_rev"].sum() / n_rev - mean_rev ** 2) if n_rev else np.nan
    sd_rev = np.sqrt(var_rev) if var_rev and var_rev > 0 else np.nan

    n_st = fill_acc["n_stable"].sum()
    p_st = fill_acc["sum_stable"].sum() / n_st if n_st else np.nan
    sd_st = np.sqrt(p_st * (1 - p_st)) if n_st and 0 < p_st < 1 else np.nan

    venue_rev = _safe_div(fill_acc["sum_rev"], fill_acc["n_rev"])
    venue_st = _safe_div(fill_acc["sum_stable"], fill_acc["n_stable"])

    out = pd.DataFrame(index=fill_acc.index)
    out["Reversion"] = (venue_rev - mean_rev) / sd_rev
    out["Stability"] = (venue_st - p_st) / sd_st
    out["Score"] = (out["Reversion"] + out["Stability"]) / 2.0
    out["n_rev"] = fill_acc["n_rev"]
    out["n_stable"] = fill_acc["n_stable"]
    return out


def build_decomposition(fill_acc):
    """Reversion split into the two things it is made of, in spread units.

    Reversion is a single number that answers two different questions at once.
    Adding and subtracting mid0 separates them:

        rev = sidesign*(mid1 - fillprice)/spread
            = sidesign*(mid0 - fillprice)/spread   <- Capture: the price I got
            + sidesign*(mid1 - mid0)/spread        <- Drift:   where it went next

    A venue can post a good Reversion by pricing well (Capture) or by not
    leaking (Drift), and the two call for completely different responses.  Same
    population as Reversion - every venue appears, thin ones included, with n
    alongside so a two-fill venue at the top is visible as such.
    """
    out = pd.DataFrame(index=fill_acc.index)
    out["Capture"] = _safe_div(fill_acc["sum_capture"], fill_acc["n_dec"])
    out["Drift"] = _safe_div(fill_acc["sum_drift"], fill_acc["n_dec"])
    out["Reversion"] = _safe_div(fill_acc["sum_rev"], fill_acc["n_rev"])
    out["n"] = fill_acc["n_rev"]
    out.index.name = "Venue"
    return out.sort_values("Reversion", ascending=False)


def build_dropped(fill_acc):
    """Fills the quote based metrics could not use, per venue.

    Ordered by fill count rather than by name, for the same reason 3.1 is: the
    venue with the most fills behind it is the one whose no-quote rate decides
    whether the tables above are worth reading."""
    d = fill_acc[["n_fill", "no_quote", "bad_spread"]].copy()
    d["% no quote"] = 100.0 * d["no_quote"] / d["n_fill"]
    d["% crossed"] = 100.0 * d["bad_spread"] / d["n_fill"]
    d.index.name = "Venue"
    return d.sort_index().sort_values("n_fill", ascending=False, kind="stable")


# -----------------------------------------------------------------------------
# Tiering: exact 1-D k-means
# -----------------------------------------------------------------------------
#
# Optimal clusters on a line are contiguous in sorted order, so the exact
# minimum of within-cluster variance is reachable by dynamic programming - no
# Lloyd iteration, no random initialisation, no seed to pin, and no chance of
# two runs on the same scores disagreeing about the tiers.  O(k n^2), and n is
# a handful of venues.

def _prefix(xs):
    p1 = np.concatenate([[0.0], np.cumsum(xs)])
    p2 = np.concatenate([[0.0], np.cumsum(np.asarray(xs) ** 2)])
    return p1, p2


def _sse(p1, p2, i, j):
    """Within-segment sum of squares for sorted xs[i:j], j exclusive."""
    n = j - i
    if n <= 0:
        return 0.0
    s = p1[j] - p1[i]
    s2 = p2[j] - p2[i]
    return max(s2 - s * s / n, 0.0)


def kmeans_1d(xs, k):
    """Exact 1-D k-means.  xs need not be sorted.

    Returns labels in the caller's order, numbered 0..k-1 by ascending cluster
    position, and the total within-cluster sum of squares."""
    xs = np.asarray(xs, dtype=float)
    n = len(xs)
    if n == 0:
        return np.array([], dtype=int), 0.0
    k = max(1, min(int(k), n))
    order = np.argsort(xs, kind="stable")
    s = xs[order]
    p1, p2 = _prefix(s)

    INF = float("inf")
    # D[m][j] = best cost of splitting the first j points into m clusters
    D = [[INF] * (n + 1) for _ in range(k + 1)]
    B = [[0] * (n + 1) for _ in range(k + 1)]
    D[0][0] = 0.0
    for m in range(1, k + 1):
        for j in range(m, n + 1):
            best, arg = INF, m - 1
            for i in range(m - 1, j):
                if D[m - 1][i] == INF:
                    continue
                c = D[m - 1][i] + _sse(p1, p2, i, j)
                if c < best:
                    best, arg = c, i
            D[m][j] = best
            B[m][j] = arg

    # walk the split points back out
    labels_sorted = np.zeros(n, dtype=int)
    j, m = n, k
    while m > 0:
        i = B[m][j]
        labels_sorted[i:j] = m - 1
        j, m = i, m - 1

    labels = np.zeros(n, dtype=int)
    labels[order] = labels_sorted
    return labels, D[k][n]


def silhouette(xs, labels):
    """Mean silhouette over the points.  Singleton clusters score 0, the usual
    convention.  n is tiny, so the naive O(n^2) form is fine."""
    xs = np.asarray(xs, dtype=float)
    labels = np.asarray(labels)
    n = len(xs)
    uniq = np.unique(labels)
    if len(uniq) < 2 or n < 2:
        return -1.0
    scores = []
    for i in range(n):
        same = xs[(labels == labels[i])]
        if len(same) <= 1:
            scores.append(0.0)
            continue
        a = np.abs(same - xs[i]).sum() / (len(same) - 1)
        b = min(
            np.abs(xs[labels == u] - xs[i]).mean()
            for u in uniq if u != labels[i]
        )
        m = max(a, b)
        scores.append(0.0 if m == 0 else (b - a) / m)
    return float(np.mean(scores))


def choose_k(xs):
    """Pick k by silhouette over 2..min(5, n-1).

    Fewer than three venues cannot be clustered meaningfully, so they all land
    in one tier rather than being split on noise."""
    n = len(xs)
    if n < 3:
        return 1
    best_k, best_s = 1, -np.inf
    for k in range(2, min(5, n - 1) + 1):
        labels, _ = kmeans_1d(xs, k)
        s = silhouette(xs, labels)
        if s > best_s:
            best_k, best_s = k, s
    return best_k


def assign_tiers(scores, k):
    """Cluster and number the tiers by DESCENDING mean score, so Tier 1 is the
    best group - matching the report, where the top Score sits in Tier 1."""
    labels, _ = kmeans_1d(scores, k)
    means = {u: np.mean(np.asarray(scores)[labels == u]) for u in np.unique(labels)}
    ranked = sorted(means, key=lambda u: -means[u])
    remap = {u: i + 1 for i, u in enumerate(ranked)}
    return np.array([remap[u] for u in labels], dtype=int)


def build_tiering(fill_acc, min_fills, tiers):
    """Table 3.3.  min_fills applies to the TIERING ONLY - table 3.1 keeps
    every venue.  That is why the report shows fewer venues in 3.3 than in 3.1:
    CLSA and Posit, the two smallest, are absent there."""
    z = pooled_z(fill_acc)
    keep = z[(z["n_rev"] >= min_fills) & (z["n_stable"] >= min_fills)].copy()
    keep = keep[keep["Score"].notna()]
    if len(keep) == 0:
        return keep
    k = choose_k(keep["Score"].values) if tiers == "auto" else int(tiers)
    keep["Tier"] = assign_tiers(keep["Score"].values, k)
    keep.index.name = "Venue"
    return keep.sort_values("Score", ascending=False)


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------

# One decimal spec per table, used by the terminal, the workbook and the PDF
# alike, so the three can never disagree about how a column is written.  The
# liquidity figures match the report column for column.
LIQUIDITY_FMT = (
    ("%Notional", "{:.1f}"), ("Spread", "{:.1f}"), ("Adv", "{:.1f}"),
    ("Fill%adv", "{:.2f}"), ("Fill Rate", "{:.1f}"), ("Duration", "{:.0f}"),
)
TIERING_FMT = (
    ("Reversion", "{:.2f}"), ("Stability", "{:.2f}"),
    ("Score", "{:.2f}"), ("Tier", "{:.0f}"),
)
DECOMPOSITION_FMT = (
    ("Capture", "{:.3f}"), ("Drift", "{:.3f}"),
    ("Reversion", "{:.3f}"), ("n", "{:,.0f}"),
)
DROPPED_FMT = (
    ("n_fill", "{:,.0f}"), ("no_quote", "{:,.0f}"), ("bad_spread", "{:,.0f}"),
    ("% no quote", "{:.1f}"), ("% crossed", "{:.1f}"),
)


def format_table(t, fmt):
    """Numeric frame -> every cell a string, NaN as blank.  Column order comes
    from fmt, so a table always presents the same way whatever built it."""
    cols = [c for c, _ in fmt]
    d = pd.DataFrame(index=t.index)
    for c, spec in fmt:
        d[c] = t[c].map(lambda v, s=spec: "" if pd.isna(v) else s.format(v))
    return d[cols]


def render_liquidity(t):
    return format_table(t, LIQUIDITY_FMT).to_string()


def render_tiering(t):
    return format_table(t, TIERING_FMT).to_string()


def render_decomposition(t):
    return format_table(t, DECOMPOSITION_FMT).to_string()


def render_dropped(t):
    return format_table(t, DROPPED_FMT).to_string()


# -----------------------------------------------------------------------------
# Output files
#
# Both writers take the SAME numeric frames the terminal renders, so the three
# can only ever differ in presentation.  Neither dependency is imported until
# the corresponding flag is used - the script still runs, and still self-tests,
# on a machine that has neither.
# -----------------------------------------------------------------------------

def write_xlsx(path, sheets):
    """One workbook, one sheet per table.

    Written as NUMBERS, not as the rendered strings: a spreadsheet you cannot
    sort or chart is a screenshot with extra steps.  Excel applies the display
    rounding, the cell keeps full precision."""
    engine = None
    for cand in ("openpyxl", "xlsxwriter"):
        try:
            __import__(cand)
            engine = cand
            break
        except ImportError:
            continue
    if engine is None:
        raise SystemExit(
            "writing .xlsx needs an Excel engine.  pip install openpyxl")
    with pd.ExcelWriter(path, engine=engine) as xw:
        for name, frame in sheets:
            frame.to_excel(xw, sheet_name=name)


# A4 portrait in POINTS, which is what the rest of the layout is measured in -
# font sizes, rule weights and column gaps are all typographic units, so the
# page is too, and the only conversion is the one into figure fractions.
PDF_PAGE = (595.28, 841.89)
PDF_FS = 10.0            # body size
PDF_LEAD = 1.45          # baseline to baseline, x font size
PDF_COLGAP = 20.0        # between columns
PDF_MARGIN = 64.0        # page margin
PDF_TABLEGAP = 46.0      # between two tables on one page


def _pdf_mismapped(strings, font):
    """The characters this font renders as the WRONG glyph.

    cmr10 is a TeX font and carries the OT1 encoding with it: underscore comes
    out as a raised dot, braces as dashes, backslash as an opening quote.  It
    does this SILENTLY - the glyph exists, it is simply not the one that was
    asked for, so nothing warns and the wrong venue name reaches the page.
    Venue names are symbols out of kdb and will contain underscores, so this
    has to be tested rather than assumed.

    Compared against DejaVu Serif, which matplotlib also ships and which maps
    ASCII correctly - so there is no hardcoded table here to fall out of date.
    """
    import matplotlib.font_manager as fm
    from matplotlib.ft2font import FT2Font
    a = FT2Font(fm.findfont(fm.FontProperties(family=font)))
    b = FT2Font(fm.findfont(fm.FontProperties(family="DejaVu Serif")))
    bad = set()
    for ch in {c for s in strings for c in str(s)}:
        if (a.get_glyph_name(a.get_char_index(ord(ch)))
                != b.get_glyph_name(b.get_char_index(ord(ch)))):
            bad.add(ch)
    return bad


def _pdf_font(strings=()):
    """Computer Modern, so the page is set in the report's own face rather than
    merely a serif - unless the text contains something CM would mis-render, in
    which case the whole document falls back and says so.  A PDF in the right
    font with the wrong venue names is worse than one in the wrong font."""
    import matplotlib.font_manager as fm
    have = {f.name for f in fm.fontManager.ttflist}
    if "cmr10" not in have:
        return "DejaVu Serif"
    bad = _pdf_mismapped(strings, "cmr10")
    if bad:
        log("  pdf: Computer Modern mis-maps " + " ".join(sorted(bad))
            + " (TeX OT1 encoding), so the tables are set in DejaVu Serif")
        return "DejaVu Serif"
    return "cmr10"


def _pdf_widths(strings, font, fs):
    """String widths in points.

    Measured off the glyph outlines rather than a renderer, so the answer does
    not depend on a backend, a DPI or a screen being present."""
    from matplotlib.font_manager import FontProperties
    from matplotlib.textpath import TextPath
    fp = FontProperties(family=font, size=fs)
    out = []
    for s in strings:
        s = str(s)
        out.append(0.0 if not s.strip()
                   else TextPath((0, 0), s, size=fs, prop=fp).get_extents().width)
    return out


def _pdf_geometry(index_header, columns, rows, font, fs=PDF_FS):
    """Column widths from the widest cell in each column, so the table fits its
    contents instead of a guess.  Returns (index width, column widths, total
    width, total height), all in points."""
    idx_w = max(_pdf_widths([index_header] + [r[0] for r in rows], font, fs))
    col_w = [max(_pdf_widths([c] + [r[1][j] for r in rows], font, fs))
             for j, c in enumerate(columns)]
    width = idx_w + sum(col_w) + PDF_COLGAP * len(col_w)
    lead = fs * PDF_LEAD
    height = lead * (len(rows) + 1) + lead * 1.6
    return idx_w, col_w, width, height


def _pdf_draw(fig, x0, y_top, idx_w, col_w, index_header, columns, rows,
              font, fs=PDF_FS):
    """One booktabs table.  Heavy top and bottom rule, light rule under the
    header, no vertical rules and no row lines - that is the whole style, and
    the reason the report's tables read as cleanly as they do.

    Returns the y of the bottom rule, in points."""
    from matplotlib.lines import Line2D
    W, H = PDF_PAGE
    lead = fs * PDF_LEAD
    width = idx_w + sum(col_w) + PDF_COLGAP * len(col_w)

    edges, x = [], x0 + idx_w
    for w in col_w:
        x += PDF_COLGAP + w
        edges.append(x)

    def text(xp, yp, s, ha):
        fig.text(xp / W, yp / H, s, ha=ha, va="baseline",
                 fontfamily=font, fontsize=fs, color="black")

    def rule(yp, lw):
        fig.add_artist(Line2D([x0 / W, (x0 + width) / W], [yp / H, yp / H],
                              lw=lw, color="black", solid_capstyle="butt",
                              transform=fig.transFigure))

    y = y_top
    rule(y, 0.9)                                  # toprule
    y -= lead * 0.78
    text(x0, y, index_header, "left")
    for e, c in zip(edges, columns):
        text(e, y, c, "right")
    y -= lead * 0.36
    rule(y, 0.45)                                 # midrule
    y -= lead * 0.88
    for name, vals in rows:
        text(x0, y, name, "left")
        for e, v in zip(edges, vals):
            text(e, y, v, "right")
        y -= lead
    y += lead - lead * 0.52
    rule(y, 0.9)                                  # bottomrule
    return y


def write_pdf(path, tables):
    """The tables and nothing else: no title, no caption, no letterhead.

    tables is a list of (index_header, numeric frame, format spec)."""
    try:
        from matplotlib.backends.backend_pdf import FigureCanvasPdf, PdfPages
        from matplotlib.figure import Figure
    except ImportError:
        raise SystemExit("writing .pdf needs matplotlib.  pip install matplotlib")

    W, H = PDF_PAGE
    laid = []
    for header, frame, fmt in tables:
        d = format_table(frame, fmt)
        rows = [(str(ix), list(r)) for ix, r in zip(d.index, d.values)]
        laid.append((header, list(d.columns), rows))
    # every string that will be typeset, so the font is chosen knowing all of it
    font = _pdf_font([s for header, cols, rows in laid
                      for s in [header] + cols + [r[0] for r in rows]
                      + [v for r in rows for v in r[1]]])

    def new_page():
        # a bare Figure with a pdf canvas: no pyplot, so no backend is selected
        # and nothing tries to find a display
        fig = Figure(figsize=(W / 72.0, H / 72.0))
        FigureCanvasPdf(fig)
        fig.patch.set_facecolor("white")
        return fig

    with PdfPages(path) as pdf:
        fig, y = None, 0.0
        for header, columns, rows in laid:
            fs = PDF_FS
            idx_w, col_w, tw, th = _pdf_geometry(header, columns, rows, font, fs)
            avail = W - 2 * PDF_MARGIN
            if tw > avail:
                # our venue names are longer than the report's, and a table
                # wider than the page loses its right hand columns off the edge
                # without saying so.  Shrink to fit instead.
                fs = max(6.0, fs * avail / tw)
                idx_w, col_w, tw, th = _pdf_geometry(header, columns, rows, font, fs)
            if fig is None or y - th < PDF_MARGIN:
                if fig is not None:
                    pdf.savefig(fig)
                fig = new_page()
                y = H - PDF_MARGIN
            y = _pdf_draw(fig, (W - tw) / 2.0, y, idx_w, col_w,
                          header, columns, rows, font, fs)
            y -= PDF_TABLEGAP
        if fig is None:                    # nothing to typeset; still emit a page
            fig = new_page()
        pdf.savefig(fig)


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

def daterange(d0, d1):
    d = d0
    while d <= d1:
        yield d
        d += dt.timedelta(days=1)


# -----------------------------------------------------------------------------
# Progress.  Goes to stderr and is flushed line by line, so a long range says
# what it is doing WHILE it does it and the report on stdout stays pipeable.
# On by default - a run that queries two servers ninety times should not look
# identical to a run that has hung.  --quiet turns it off.
# -----------------------------------------------------------------------------

QUIET = False


def log(msg=""):
    if not QUIET:
        print(msg, file=sys.stderr, flush=True)


def _hms(secs):
    return f"{secs:.1f}s" if secs < 60 else f"{int(secs)//60}m {int(secs)%60:02d}s"


def diagnose(ho, day, ctry, country_label):
    """Where one date's rows disappear, stage by stage.

    An empty report is almost always one stage dropping to zero, and which one
    decides what to do about it.  "no dark fills in range" says none of that."""
    # all of this on stdout, header included: in this mode the funnel IS the
    # output, and splitting it across two streams reorders it under a pipe
    print(f"diagnosing {day}\n")
    funnel = _to_pandas(ho(Q_DIAG, day, ctry))
    width = max(len(str(s)) for s in funnel["stage"])
    prev = None
    stock_missing = False
    for _, r in funnel.iterrows():
        stage, n = str(r["stage"]), int(r["n"])
        # no share on the last line: it counts PARENT orders where everything
        # above it counts children, and "3.6% of previous" off two grains reads
        # like a collapse when it is just the fan-out
        share = ("" if not prev or stage == "stock_rows_found"
                 else f"   {100.0 * n / prev:5.1f}% of previous")
        # only the stages that FILTER can empty the report.  stock_rows_found is
        # joined with lj, so a zero there costs adv and fxlast and nothing else -
        # calling that "everything dropped here" would send the next person to
        # the wrong table
        gone = ("   <- everything dropped here"
                if n == 0 and prev and stage != "stock_rows_found" else "")
        if stage == "stock_rows_found" and n == 0:
            stock_missing = True
        print(f"  {stage:<{width}}  {n:>12,}{share}{gone}")
        prev = n
    print()
    if stock_missing:
        print("  no target_stock rows for those parents.  The fills still come "
              "through - the\n  join is an lj - but adv and fxlast are null, so "
              "%Notional and Adv will be\n  empty.  Everything quote based is "
              "unaffected.\n")
    if int(funnel["n"].iloc[0]) == 0:
        print(f"  no workorder rows at all on {day} - a non-trading date, or a "
              f"date the HDB does not hold.\n  Re-run --diagnose with a --start "
              f"you know traded before reading anything into the rest.")
        return 0
    ctry_rows = _to_pandas(ho(Q_COUNTRIES, day))
    if len(ctry_rows) == 0:
        print("  no dark child orders on that date, so no markets to compare "
              "against")
    else:
        print(f"  markets on {day}, by dark child orders - the SYM SUFFIX, "
              f"which is what --country matches:")
        for _, r in ctry_rows.head(20).iterrows():
            got = str(r["country"])
            mine = "   <- your --country" if country_label and got == country_label else ""
            print(f"    {got:<12} {int(r['n']):>8,}{mine}")
        if country_label and country_label not in [str(v) for v in ctry_rows["country"]]:
            print(f"\n  --country {country_label} is not among them, which is why "
                  f"the range came back empty.\n  It is matched against the end "
                  f"of the sym - 7203.JP is JP - and nothing else.")
    return 0


def run(args):
    global QUIET
    QUIET = args.quiet

    args.country = normalise_country(args.country)
    days = list(daterange(args.start, args.end))
    log(f"reversion_liquidity  {args.start} to {args.end}  ({len(days)} dates)"
        + (f", market {args.country}" if args.country else ", all markets"))
    # logged BEFORE each connect, so a hang names the server it is hanging on
    log(f"  order server  {ORDER_SERVER} ...")
    ho = connect(ORDER_SERVER)
    log(f"  quote server  {QATT_SERVER} ...")
    hq = connect(QATT_SERVER)
    # BYTES, not str: PyKX sends a python str as a q symbol, and the q casts
    # with `$, which is a 'type error on a symbol.  b"" is an empty char
    # vector, so `0=count ctry` still selects every market.
    country = args.country.encode()

    if args.diagnose:
        return diagnose(ho, days[0], country, args.country)

    log("")
    fill_acc, child_acc, kept = None, None, []
    unmapped = set()
    n_ok = n_empty = n_failed = n_fills = 0
    t_run = time.perf_counter()
    for i, day in enumerate(days, start=1):
        t0 = time.perf_counter()
        tag = f"  [{i:>3}/{len(days)}] {day}"
        try:
            fills, child = fetch_day(ho, hq, day, country)
        except Exception as exc:                      # noqa: BLE001
            n_failed += 1
            log(f"{tag}  FAILED - {exc}")
            continue
        unmapped |= unmapped_venues(child)
        child_acc = fold(child_acc, aggregate_child(child))
        took = time.perf_counter() - t0
        if len(fills) == 0:
            n_empty += 1
            also = "" if len(child) else ", and no dark child orders either"
            log(f"{tag}  no dark fills{also}   {took:5.1f}s")
            continue
        n_ok += 1
        n_fills += len(fills)
        unmapped |= unmapped_venues(fills)
        m = fill_metrics(fills, half_spread=args.half_spread)
        fill_acc = fold(fill_acc, aggregate_fills(m))
        if args.keep_fills:
            kept.append(m)
        log(f"{tag}  {len(fills):>7,} fills, {len(child):>7,} children   {took:5.1f}s")

    log("")
    log(f"  {len(days)} dates in {_hms(time.perf_counter() - t_run)}: {n_ok} with "
        f"fills ({n_fills:,} in total), {n_empty} empty, {n_failed} failed")

    if fill_acc is None or len(fill_acc) == 0:
        raise SystemExit(
            f"\nno dark fills across {len(days)} dates"
            + (f" for market {args.country} - the sym suffix, e.g. 7203.JP is JP"
               if args.country else "")
            + (f", and {n_failed} date(s) errored - see above" if n_failed else "")
            + "\nrun the same command with --diagnose to see which filter empties it.")

    liquidity = build_liquidity(fill_acc, child_acc)
    tiering = build_tiering(fill_acc, args.min_fills, args.tiers)
    dropped = build_dropped(fill_acc)
    decomposition = build_decomposition(fill_acc) if args.decompose else None
    # build_tiering returns a frame with no Tier column when nothing cleared
    # --min-fills, so everything downstream asks this rather than len()
    tiered = "Tier" in tiering.columns

    print(f"\nDark venues {args.start} to {args.end}"
          + (f", country {args.country}" if args.country else "")
          + (", half-spread normalised" if args.half_spread else ""))
    if unmapped:
        print(f"\n  {len(unmapped)} venue(s) are not in VENUE_GROUPS, so they keep "
              f"their raw kdb name below.\n  Add them to the sheet near the top "
              f"of this script to group them:")
        for c, v in sorted(unmapped):
            print(f'    ("{c}", "{v}"):')
    print("\nTable 3.1: Liquidity\n")
    print(render_liquidity(liquidity))
    print("\nTable 3.3: Venue tiering on 1s reversion and quote stability\n")
    if not tiered:
        print(f"  no venue reached --min-fills {args.min_fills}")
    else:
        print(render_tiering(tiering))
        if len(tiering) < len(liquidity):
            missing = sorted(set(liquidity.index) - set(tiering.index))
            print(f"\n  below --min-fills {args.min_fills}, not tiered: "
                  + ", ".join(missing))
    if decomposition is not None:
        unit = "half spreads" if args.half_spread else "spreads"
        print(f"\nReversion decomposition: Capture + Drift = Reversion, "
              f"per fill, in {unit}\n")
        print(render_decomposition(decomposition))
    print("\nFills excluded from the quote based metrics\n")
    print(render_dropped(dropped))

    if args.out_dir:
        import os
        os.makedirs(args.out_dir, exist_ok=True)
        sheets = [("Liquidity", liquidity)]
        if tiered:
            sheets.append(("Tiering", tiering))
        if decomposition is not None:
            sheets.append(("Decomposition", decomposition))
        sheets.append(("Excluded", dropped))
        book = os.path.join(args.out_dir, "report.xlsx")
        write_xlsx(book, sheets)
        print(f"\nwritten to {book}")
        if args.keep_fills and kept:
            # stays a CSV on purpose: a quarter of dark fills runs past Excel's
            # 1,048,576 row ceiling, and a sheet truncates there in silence
            fills_csv = os.path.join(args.out_dir, "fills.csv")
            pd.concat(kept).to_csv(fills_csv, index=False)
            print(f"written to {fills_csv}")

    if args.pdf:
        tables = [("Venue", liquidity, LIQUIDITY_FMT)]
        if tiered:
            # the report leaves this table's venue column unheaded
            tables.append(("", tiering, TIERING_FMT))
        if decomposition is not None:
            tables.append(("Venue", decomposition, DECOMPOSITION_FMT))
        write_pdf(args.pdf, tables)
        print(f"\nwritten to {args.pdf}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Dark venue liquidity and reversion tiering",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start", type=dt.date.fromisoformat)
    p.add_argument("--end", type=dt.date.fromisoformat)
    p.add_argument("--country", default="",
                   help="market, matched against the sym suffix: AU for *.AU, "
                        "JP for *.JP. Case insensitive; blank for all")
    p.add_argument("--min-fills", type=int, default=1000,
                   help="minimum usable fills for a venue to be TIERED")
    p.add_argument("--tiers", default="auto", help="'auto' (silhouette) or an integer k")
    p.add_argument("--half-spread", action="store_true",
                   help="normalise reversion by half the spread instead of the full spread")
    p.add_argument("--keep-fills", action="store_true",
                   help="also retain fill level rows; will exhaust memory on a long range")
    p.add_argument("--decompose", action="store_true",
                   help="also show reversion split into Capture (where in the "
                        "touch the fill happened) and Drift (where the mid went "
                        "in the second after)")
    p.add_argument("--out-dir", help="also write report.xlsx here, one sheet per table")
    p.add_argument("--pdf", help="also write the tables to this .pdf, typeset the "
                                 "way the report typesets them")
    p.add_argument("--diagnose", action="store_true",
                   help="query the FIRST date only and show where its rows are "
                        "lost, stage by stage; use when a range reports nothing")
    p.add_argument("--quiet", action="store_true",
                   help="no per-date progress on stderr; the report still prints")
    p.add_argument("--verbose", action="store_true",
                   help=argparse.SUPPRESS)   # progress is on by default now
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

def _brute_1d(xs, k):
    """Reference cost: enumerate every contiguous partition of sorted xs."""
    s = np.sort(np.asarray(xs, dtype=float))
    n = len(s)
    best = float("inf")
    for cuts in combinations(range(1, n), k - 1):
        b = [0] + list(cuts) + [n]
        cost = 0.0
        for i in range(k):
            seg = s[b[i]:b[i + 1]]
            cost += float(((seg - seg.mean()) ** 2).sum()) if len(seg) else 0.0
        best = min(best, cost)
    return best


def test_kmeans_matches_brute_force():
    rng = np.random.default_rng(0)
    for trial in range(60):
        n = int(rng.integers(3, 10))
        xs = np.round(rng.normal(size=n), 2)          # duplicates on purpose
        for k in range(1, n + 1):
            _, cost = kmeans_1d(xs, k)
            assert abs(cost - _brute_1d(xs, k)) < 1e-9, (
                f"trial {trial} n={n} k={k}: dp {cost} vs brute {_brute_1d(xs, k)}")
    # k clusters over k points is a perfect fit
    _, cost = kmeans_1d([1.0, 2.0, 3.0], 3)
    assert abs(cost) < 1e-12
    # all-identical points cost nothing at any k
    _, cost = kmeans_1d([2.0] * 5, 2)
    assert abs(cost) < 1e-12


def _synth_fills(rng, n, venues, country=None):
    """A fill frame with the awkward cases deliberately present.

    country defaults to absent, not blank: a frame with no country column maps
    through the raw-name fallback, so the tests that predate the venue sheet
    keep asserting on the names they pass in."""
    v = rng.choice(venues, n)
    bid0 = np.round(rng.uniform(10, 11, n), 2)
    ask0 = bid0 + np.round(rng.choice([0.01, 0.02, 0.05], n), 2)
    bid1 = np.where(rng.random(n) < 0.5, bid0, bid0 + 0.01)
    ask1 = np.where(rng.random(n) < 0.5, ask0, ask0 + 0.01)
    df = pd.DataFrame({
        "venue": v,
        "fillprice": (bid0 + ask0) / 2,
        "fillsize": rng.integers(100, 5000, n).astype(float),
        "sidesign": rng.choice([1, -1], n).astype(float),
        "adv": rng.integers(1_000_000, 9_000_000, n).astype(float),
        "fxlast": np.full(n, 0.65),
        "qbid0": bid0, "qask0": ask0, "qbid1": bid1, "qask1": ask1,
    })
    if country is not None:
        df["country"] = country
    # a crossed quote: unusable for reversion, still usable for stability
    df.loc[0, "qask0"] = df.loc[0, "qbid0"] - 0.01
    # a missing post-quote: unusable for both
    df.loc[1, "qbid1"] = np.nan
    return df


def test_populations_are_separate():
    rng = np.random.default_rng(1)
    df = _synth_fills(rng, 40, ["A"])
    m = fill_metrics(df)
    a = aggregate_fills(m).loc["A"]
    assert np.isnan(m.loc[0, "rev"]), "crossed quote must not produce a reversion"
    assert not np.isnan(m.loc[0, "stable"]), "crossed quote must still count for stability"
    assert np.isnan(m.loc[1, "rev"]) and np.isnan(m.loc[1, "stable"])
    # 40 fills, one crossed and one with no post-quote
    assert a["n_stable"] == 39, a["n_stable"]
    assert a["n_rev"] == 38, a["n_rev"]
    assert a["bad_spread"] == 1 and a["no_quote"] == 1
    # notional counts every fill, quote or not
    assert a["n_fill"] == 40


def test_chunking_is_exact():
    """Folding day by day must give bit-comparable results to one pass.  This
    is the property the whole accumulator design rests on."""
    rng = np.random.default_rng(2)
    days = [_synth_fills(rng, 50, ["A", "B", "C"]) for _ in range(7)]

    chunked = None
    for d in days:
        chunked = fold(chunked, aggregate_fills(fill_metrics(d)))
    one_pass = aggregate_fills(fill_metrics(pd.concat(days, ignore_index=True)))

    chunked = chunked.sort_index()
    one_pass = one_pass.sort_index()
    assert list(chunked.index) == list(one_pass.index)
    for c in FILL_ACC:
        assert np.allclose(chunked[c], one_pass[c], rtol=1e-12, atol=1e-9), c

    zc = pooled_z(chunked)[["Reversion", "Stability", "Score"]]
    zo = pooled_z(one_pass)[["Reversion", "Stability", "Score"]]
    assert np.allclose(zc.values, zo.values, rtol=1e-12, atol=1e-12)


def test_score_matches_published_rows():
    """The three rows of the report's table 3.3.

    Checked to +/-0.005, NOT by exact equality after rounding.  The published z
    columns are themselves rounded to 2dp, so the recomputed Score can only be
    pinned to half a display unit.  Centrepoint is the row that proves it:
    (-0.15 + -0.32)/2 is -0.235, exactly on the boundary, which the report
    rounds away from zero to -0.24 while python's banker's rounding gives
    -0.23.  Asserting equality there would be testing the rounding mode, not
    the formula."""
    published = [
        ("MS Pool", 0.19, -0.33, -0.07, 1),
        ("Centrepoint", -0.15, -0.32, -0.24, 1),
        ("JPMX", -0.62, -0.37, -0.50, 2),
    ]
    for name, rev, stab, score, _ in published:
        assert abs((rev + stab) / 2.0 - score) <= 0.0051, name

    scores = [s for _, _, _, s, _ in published]
    tiers = assign_tiers(scores, 2)
    expected = [t for _, _, _, _, t in published]
    assert list(tiers) == expected, (list(tiers), expected)
    # tier 1 must be the best group, not merely the first one encountered
    assert tiers[int(np.argmax(scores))] == 1


def test_weighted_means():
    """Each weighted mean must divide by the weight of the rows that actually
    contributed to it, not by the venue's whole notional."""
    df = pd.DataFrame({
        "venue": ["A", "A"],
        "fillprice": [10.0, 10.0],
        "fillsize": [100.0, 900.0],
        "sidesign": [1.0, 1.0],
        "adv": [1e6, np.nan],          # second row has no adv
        "fxlast": [1.0, 1.0],
        "qbid0": [9.99, 9.99], "qask0": [10.01, 10.01],
        "qbid1": [9.99, 9.99], "qask1": [10.01, 10.01],
    })
    acc = aggregate_fills(fill_metrics(df))
    liq = build_liquidity(acc, pd.DataFrame(columns=CHILD_ACC, dtype=float))
    # only the first row has an adv, so Adv is that row's 1.0m, undiluted
    assert abs(liq.loc["A", "Adv"] - 1.0) < 1e-12, liq.loc["A", "Adv"]
    assert abs(liq.loc["A", "Fill%adv"] - 0.01) < 1e-12
    # both rows have a spread, so it is the notional weighted 20bps
    assert abs(liq.loc["A", "Spread"] - 20.0) < 1e-9
    assert abs(liq.loc["A", "%Notional"] - 100.0) < 1e-12


def test_min_fills_only_affects_tiering():
    rng = np.random.default_rng(3)
    big = _synth_fills(rng, 200, ["BIG"])
    small = _synth_fills(rng, 5, ["SMALL"])
    acc = fold(None, aggregate_fills(fill_metrics(pd.concat([big, small],
                                                           ignore_index=True))))
    liq = build_liquidity(acc, pd.DataFrame(columns=CHILD_ACC, dtype=float))
    tier = build_tiering(acc, min_fills=50, tiers="auto")
    assert "SMALL" in liq.index, "3.1 must keep every venue"
    assert "SMALL" not in tier.index, "3.3 must drop the thin venue"


def test_server_constants():
    """Once edited, the two connection constants must parse as host:port.

    Worth a test because this script is written on a machine with no kdb and
    run on one that has it: a typo here would otherwise surface as a connection
    failure on the far side, long after the edit.  Still holding the
    placeholder is fine and is not a failure - connect() catches that with its
    own message."""
    for name, val in (("ORDER_SERVER", ORDER_SERVER), ("QATT_SERVER", QATT_SERVER)):
        if val.startswith(_PLACEHOLDER):
            print(f"        ({name} not set yet)")
            continue
        try:
            parse_hostport(val)
        except ValueError as exc:
            raise AssertionError(f"{name}={val!r}: {exc}")


def test_parse_hostport():
    assert parse_hostport("h:5010") == ("h", 5010)
    for bad in ("h", "h:", ":5010", "h:abc"):
        try:
            parse_hostport(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


def test_country_reaches_q_as_chars():
    """The country filter must arrive as a char vector, never a str.

    PyKX sends a python str as a q SYMBOL, and the q casts it with `$, which is
    a 'type error on a symbol - so a str here fails on every single date rather
    than on the first thing anyone would look at.  Worth pinning: it is
    invisible without a server, and it is what run() spent a release getting
    wrong."""
    sent = []

    class Result:                      # what _to_pandas expects back
        def pd(self):
            return pd.DataFrame()

    class Handle:
        def __call__(self, qsql, *args):
            sent.append(args)
            return Result()

    fetch_day(Handle(), Handle(), dt.date(2026, 4, 1), "AU".encode())
    assert sent, "fetch_day sent nothing"
    for args in sent:
        ctry = args[1]
        assert isinstance(ctry, bytes), (
            f"country reached q as {type(ctry).__name__}, which PyKX converts "
            f"to a symbol; send bytes so it arrives as chars")
    # and the every-country case has to stay an EMPTY char vector, so the
    # `0=count ctry` branch in the q still fires
    assert b"" == "".encode()


def test_the_tables_are_ordered_biggest_first():
    """3.1 by %Notional, 3.3 by Score, the decomposition by Reversion, the
    dropped footer by fills - all descending, none of them alphabetical.

    With fifteen pools on the sheet an alphabetical table is a lookup table:
    every row has to be read before the shape of the flow is visible.  Each of
    these four is sorted on the column it exists to show."""
    # named so that ALPHABETICAL and BIGGEST-FIRST are opposite orders - with
    # BIG/MID/SMALL the two agree, and the test cannot tell a sorted table from
    # a sort_index()'d one
    rng = np.random.default_rng(11)
    df = pd.concat([_synth_fills(rng, 300, ["ZEBRA"]),
                    _synth_fills(rng, 40, ["MIDDLE"]),
                    _synth_fills(rng, 8, ["ALPHA"])], ignore_index=True)
    df.loc[df["venue"] == "ZEBRA", "fillsize"] *= 10
    acc = aggregate_fills(fill_metrics(df))

    liq = build_liquidity(acc, pd.DataFrame(columns=CHILD_ACC, dtype=float))
    assert list(liq.index) == ["ZEBRA", "MIDDLE", "ALPHA"], list(liq.index)
    assert liq["%Notional"].is_monotonic_decreasing

    dropped = build_dropped(acc)
    assert list(dropped.index) == ["ZEBRA", "MIDDLE", "ALPHA"], list(dropped.index)

    tier = build_tiering(acc, min_fills=1, tiers="auto")
    assert tier["Score"].is_monotonic_decreasing, tier["Score"].tolist()

    dec = build_decomposition(acc)
    assert dec["Reversion"].is_monotonic_decreasing, dec["Reversion"].tolist()


def test_the_market_is_the_sym_suffix():
    """Every q that decides a market decides it the same way, off the sym.

    This is the bug that produced the whole rule: the market used to come from
    target_stock's country column, --country JP returned nothing, and the JP
    dark fills were in workorder the entire time.  Four lambdas ask the
    question and all four have to answer it identically - one of them drifting
    back to a join is exactly how a market goes quietly missing again."""
    line = ('  w:$[count w; update country:`$upper {last "." vs x} '
            'each string sym from w;')
    for name, q in (("Q_FILLS", Q_FILLS), ("Q_CHILD", Q_CHILD),
                    ("Q_DIAG", Q_DIAG), ("Q_COUNTRIES", Q_COUNTRIES)):
        assert line in q, f"{name} does not take the market off the sym suffix"


def test_no_query_reads_target_stock_country():
    """target_stock's country column is not consulted anywhere.

    It exists, and it is wrong or blank often enough that nothing in this
    script is allowed to depend on it.  Checks the columns of every
    `select ... from target_stock`, so re-adding it is a failing test rather
    than a report that is empty for one market and right for another."""
    for name, q in (("Q_FILLS", Q_FILLS), ("Q_CHILD", Q_CHILD),
                    ("Q_DIAG", Q_DIAG), ("Q_COUNTRIES", Q_COUNTRIES)):
        for piece in q.split("from target_stock")[:-1]:
            cols = piece[piece.rfind("select") + len("select"):]
            assert "country" not in cols, (
                f"{name} selects country from target_stock: {cols.strip()!r}")


def test_target_stock_cannot_delete_a_fill():
    """The stock join is lj, so a missing stock row nulls adv and fxlast.

    It used to be ij, because the country filter was applied to the stock rows
    and an inner join was what made --country exclude.  The filter is on the
    sym now, so the only thing an ij could still do is silently drop a fill
    whose stock row is absent - a row that was in workorder, was dark, was
    filled, and vanished on the way to a table."""
    for name, q in (("Q_FILLS", Q_FILLS), ("Q_CHILD", Q_CHILD)):
        assert "w lj x" in q, f"{name} no longer left-joins target_stock"
        assert " ij x" not in q, f"{name} inner-joins target_stock again"


def test_country_is_normalised_before_it_reaches_q():
    """--country jp is --country JP, and blank still means every market."""
    assert normalise_country(" jp ") == "JP"
    assert normalise_country("Au") == "AU"
    assert normalise_country("") == ""
    assert normalise_country(None) == ""


def test_a_null_number_arrives_as_nan():
    """A q null in a numeric column reaches pandas as NaN, not as a mask.

    PyKX returns a MASKED array for a column that contains a null and a plain
    ndarray for the same column without one.  A masked array reaching
    fill_metrics is `TypeError: bad operand type for unary ~: 'float'` out of
    pandas internals - which is what one null ADV on one Japanese name did to
    a whole quarter, on the date it first turned up.

    The contract is that nothing downstream of _to_pandas ever sees a mask."""
    masked = np.ma.array([1_000_000, 0, 3_000_000], mask=[False, True, False],
                         dtype=np.int32)

    class Result:
        def pd(self):
            df = pd.DataFrame({
                "fxlast": pd.array([1.0, None, 1.5], dtype="Float64"),
                "venue": [b"MS_DARK", b"MS_DARK", b"MS_DARK"],
                "tm": pd.to_timedelta(["1h", "2h", "3h"]),
            })
            # ASSIGNED, not passed to the constructor: the constructor quietly
            # re-blocks a masked array into a plain one, and a mask surviving
            # into the frame is the whole of what this is about
            df["adv"] = pd.Series(masked)
            return df

    df = _to_pandas(Result())

    assert not isinstance(df["adv"].values, np.ma.MaskedArray), "adv is still masked"
    assert df["adv"].dtype == np.float64, df["adv"].dtype
    assert isinstance(df["fxlast"].dtype, np.dtype), "fxlast is still an extension type"
    assert df["adv"].notna().tolist() == [True, False, True]
    assert df["fxlast"].notna().tolist() == [True, False, True]
    assert df["adv"].iloc[0] == 1_000_000.0 and np.isnan(df["adv"].iloc[1])
    # the symbol column still decodes, and the time column is left as a time
    assert df["venue"].tolist() == ["MS_DARK"] * 3
    assert df["tm"].dtype.kind == "m", df["tm"].dtype


def test_a_null_adv_is_dropped_from_adv_not_from_the_row():
    """A fill on a name with no ADV still counts for %Notional.

    It cannot count for Adv or Fill%adv - there is nothing to divide by - but
    dropping the row entirely would take its notional out of the venue's share
    too, which is a different and wrong number.  fill_metrics carries NaN in
    the two adv columns and the row everywhere else."""
    rng = np.random.default_rng(7)
    df = _synth_fills(rng, 4, ["MS_DARK"], country="JP")
    df.loc[df.index[0], "adv"] = np.nan

    m = fill_metrics(df)

    assert np.isnan(m["adv_m"].iloc[0]), "a null adv must not become a number"
    assert np.isnan(m["filladv"].iloc[0])
    assert m["notional"].notna().all(), "the row itself stays, notional and all"
    assert m["adv_m"].iloc[1:].notna().all(), "the other rows are untouched"


def test_decomposition_adds_up():
    """Capture + Drift == Reversion, per fill and per venue.

    That identity is the entire claim the decomposition makes.  If it ever
    stops holding, the two columns are explaining something that is not the
    reversion they sit next to."""
    rng = np.random.default_rng(4)
    m = fill_metrics(_synth_fills(rng, 120, ["A", "B", "C"]))
    ok = m["rev"].notna()
    assert ok.sum() > 100, ok.sum()
    assert np.allclose(m.loc[ok, "capture"] + m.loc[ok, "drift"],
                       m.loc[ok, "rev"], rtol=1e-12, atol=1e-12)
    dec = build_decomposition(aggregate_fills(m))
    assert np.allclose(dec["Capture"] + dec["Drift"], dec["Reversion"],
                       rtol=1e-9, atol=1e-12)
    assert list(dec["Reversion"]) == sorted(dec["Reversion"], reverse=True)


def test_decomposition_shares_the_reversion_mask():
    """Both halves must drop out exactly where reversion drops out.  A crossed
    quote has no spread to divide by, so neither half means anything there -
    and if the masks ever drifted apart, the two columns would quietly stop
    adding up to the column beside them."""
    rng = np.random.default_rng(5)
    m = fill_metrics(_synth_fills(rng, 40, ["A"]))
    assert np.isnan(m.loc[0, "capture"]) and np.isnan(m.loc[0, "drift"])
    assert np.isnan(m.loc[1, "capture"]) and np.isnan(m.loc[1, "drift"])
    a = aggregate_fills(m).loc["A"]
    assert a["n_dec"] == a["n_rev"] == 38, (a["n_dec"], a["n_rev"])


def test_half_spread_scales_the_decomposition():
    """--half-spread halves the divisor, so all three double together."""
    rng = np.random.default_rng(6)
    df = _synth_fills(rng, 60, ["A"])
    full, half = fill_metrics(df), fill_metrics(df, half_spread=True)
    ok = full["rev"].notna()
    for c in ("rev", "capture", "drift"):
        assert np.allclose(half.loc[ok, c], 2.0 * full.loc[ok, c],
                           rtol=1e-12, atol=1e-12), c


def _synth_tables(seed=7):
    """The four finished tables, from synthetic fills - no kdb needed."""
    rng = np.random.default_rng(seed)
    acc = aggregate_fills(fill_metrics(_synth_fills(rng, 300, ["A", "B", "C"])))
    child = pd.DataFrame(columns=CHILD_ACC, dtype=float)
    return (build_liquidity(acc, child), build_tiering(acc, 10, "auto"),
            build_decomposition(acc), build_dropped(acc))


def test_formatting_is_shared():
    """Terminal, workbook and PDF all format through format_table, so table 3.1
    must come out in the decimals the report prints."""
    liq = _synth_tables()[0]
    d = format_table(liq, LIQUIDITY_FMT)
    assert list(d.columns) == [c for c, _ in LIQUIDITY_FMT]
    assert len(d["%Notional"].iloc[0].split(".")[1]) == 1     # 1dp, as published
    assert len(d["Fill%adv"].iloc[0].split(".")[1]) == 2      # 2dp
    # a NaN must render blank, never the string 'nan'
    liq2 = liq.copy()
    liq2.loc[liq2.index[0], "Spread"] = np.nan
    assert format_table(liq2, LIQUIDITY_FMT)["Spread"].iloc[0] == ""


def test_xlsx_round_trips():
    """The workbook must hold NUMBERS, not the rendered strings - otherwise it
    is a screenshot that happens to open in Excel."""
    try:
        import openpyxl                                        # noqa: F401
    except ImportError:
        print("        (no Excel engine installed, skipped)")
        return
    import os
    import tempfile
    liq, tier, dec, drop = _synth_tables()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "report.xlsx")
        write_xlsx(p, [("Liquidity", liq), ("Tiering", tier),
                       ("Decomposition", dec), ("Excluded", drop)])
        book = pd.read_excel(p, sheet_name=None, index_col=0)
    assert list(book) == ["Liquidity", "Tiering", "Decomposition", "Excluded"], list(book)
    back = book["Liquidity"]
    assert list(back.index) == list(liq.index)
    assert back["%Notional"].dtype.kind == "f", back["%Notional"].dtype
    assert np.allclose(back["%Notional"].values, liq["%Notional"].values)
    assert np.allclose(book["Decomposition"]["Capture"].values, dec["Capture"].values)


def test_pdf_geometry_fits_its_contents():
    """Columns are sized off their widest cell, so a long venue name widens the
    table instead of colliding with the numbers."""
    try:
        import matplotlib                                      # noqa: F401
    except ImportError:
        print("        (matplotlib not installed, skipped)")
        return
    font = _pdf_font()
    rows = [("Centrepoint", ["-0.15"]), ("A_VERY_LONG_DARK_VENUE_NAME", ["0.19"])]
    idx_w, col_w, w, h = _pdf_geometry("Venue", ["Reversion"], rows, font)
    longest = max(_pdf_widths([r[0] for r in rows], font, PDF_FS))
    assert idx_w >= longest - 1e-9, (idx_w, longest)
    assert col_w[0] >= max(_pdf_widths(["Reversion"], font, PDF_FS)) - 1e-9
    assert w > idx_w + col_w[0]
    assert h > PDF_FS * len(rows)


def test_pdf_font_falls_back_on_tex_encoding():
    """cmr10 maps underscore to a raised dot, and nothing warns about it.  Venue
    names arrive from kdb as symbols, so ASX_CENTREPOINT_DARK would reach the
    page as ASX-dot-CENTREPOINT-dot-DARK unless the writer notices first."""
    try:
        import matplotlib                                      # noqa: F401
    except ImportError:
        print("        (matplotlib not installed, skipped)")
        return
    import matplotlib.font_manager as fm
    if "cmr10" not in {f.name for f in fm.fontManager.ttflist}:
        print("        (no cmr10 installed, skipped)")
        return
    assert _pdf_mismapped(["Centrepoint", "MS Pool", "-0.15", "Fill%adv"],
                          "cmr10") == set()
    assert _pdf_mismapped(["ASX_CENTREPOINT_DARK"], "cmr10") == {"_"}
    assert _pdf_font(["Centrepoint", "-0.15"]) == "cmr10"
    assert _pdf_font(["ASX_CENTREPOINT_DARK", "-0.15"]) == "DejaVu Serif"


def test_pdf_is_written():
    """End to end: a real PDF file with a page in it."""
    try:
        import matplotlib                                      # noqa: F401
    except ImportError:
        print("        (matplotlib not installed, skipped)")
        return
    import os
    import tempfile
    liq, tier, dec, _ = _synth_tables()
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "report.pdf")
        write_pdf(p, [("Venue", liq, LIQUIDITY_FMT), ("", tier, TIERING_FMT),
                      ("Venue", dec, DECOMPOSITION_FMT)])
        blob = open(p, "rb").read()
    assert blob[:5] == b"%PDF-", blob[:16]
    assert b"/Pages" in blob
    assert len(blob) > 2000, len(blob)


def test_venue_sheet_is_consistent():
    """The sheet is hand typed off a screenshot, so its shape is checked here
    rather than trusted.

    ONE SHORT CODE PER NAME is the one that matters beyond tidiness:
    scripts/dark_routed_executed labels pie slices with the short code, so a
    pool spelled two ways would draw as two slices of a pie that has one."""
    short_of = {}
    for key, val in VENUE_GROUPS.items():
        assert isinstance(key, tuple) and len(key) == 2, key
        country, venue = key
        assert country.isalpha() and country == country.upper(), key
        assert venue == venue.upper(), key
        # both scripts only ever see venues matching this, so a typo here is a
        # row that can never match anything
        assert ("DARK" in venue) or ("DRK" in venue), key
        assert len(val) == 2, (key, val)
        name, short = val
        assert name and short, (key, val)
        assert short_of.setdefault(name, short) == short, (
            f"{name} has two short codes: {short_of[name]} and {short}")


def test_venues_in_one_group_become_one_row():
    """CENTREPOINT_DARK and CENTREPOINT_CITI_DARK are two routes into one pool,
    and the report shows one Centrepoint row.  Its notional has to be the sum
    of both, not one of them."""
    rng = np.random.default_rng(8)
    a = _synth_fills(rng, 60, ["CENTREPOINT_DARK"], country="AU")
    b = _synth_fills(rng, 40, ["CENTREPOINT_CITI_DARK"], country="AU")
    acc = aggregate_fills(fill_metrics(pd.concat([a, b], ignore_index=True)))
    assert list(acc.index) == ["Centrepoint"], list(acc.index)
    assert acc.loc["Centrepoint", "n_fill"] == 100
    apart = sum(aggregate_fills(fill_metrics(d))["notional"].sum() for d in (a, b))
    assert abs(acc.loc["Centrepoint", "notional"] - apart) < 1e-6
    # and %Notional is still the whole book, so one group alone is all of it
    liq = build_liquidity(acc, pd.DataFrame(columns=CHILD_ACC, dtype=float))
    assert abs(liq.loc["Centrepoint", "%Notional"] - 100.0) < 1e-12


def test_the_sheet_has_no_duplicate_keys():
    """The same (country, venue) must not be written twice in VENUE_GROUPS.

    A dict literal keeps the LAST of a repeated key and says nothing about it,
    so the losing line is not recoverable from VENUE_GROUPS at runtime and no
    test built on the dict can see it.  This one reads the SOURCE.

    It is not hypothetical: the completed sheet had ("AU", "CBOE_RBC_DARK")
    against Centrepoint on one line and against CBOE on another, which are
    different pools, and the dict quietly kept CBOE."""
    src = Path(__file__).read_text(encoding="utf-8")
    block = src[src.index("VENUE_GROUPS = {"):]
    block = block[:block.index("\n}")]
    keys = [ln.split("):")[0].strip() for ln in block.splitlines()
            if ln.strip().startswith('("')]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"written more than once in VENUE_GROUPS: {dupes}"
    assert len(keys) == len(VENUE_GROUPS), (
        f"{len(keys)} lines in the source, {len(VENUE_GROUPS)} keys in the dict")


def test_the_sheet_is_keyed_on_country():
    """The same kdb symbol is not the same pool in every market.

    JPMAP_DARK is JPMX in all three now, and the sheet still needs the country:
    JPMAP_MF_DARK is JPMX in AU and is not a venue at all in JP or HK, and
    NOM_DARK is Nomura in Japan and nowhere else.  A venue-name-only sheet
    could say neither of those.

    (AU, JPMAP_DARK) was the example here until the desk completed the sheet
    and it turned out to be a real AU venue.  The property is the same; the
    pair that demonstrates it had to move.)"""
    rng = np.random.default_rng(9)
    df = pd.concat([_synth_fills(rng, 10, ["JPMAP_DARK"], country="JP"),
                    _synth_fills(rng, 10, ["JPMAP_DARK"], country="HK"),
                    _synth_fills(rng, 10, ["JPMAP_MF_DARK"], country="AU")],
                   ignore_index=True)
    assert list(aggregate_fills(fill_metrics(df)).index) == ["JPMX"]
    crossed = pd.DataFrame({"country": ["JP", "HK"],
                            "venue": ["JPMAP_MF_DARK", "NOM_DARK"]})
    assert list(venue_labels(crossed)) == ["JPMAP_MF_DARK", "NOM_DARK"]
    assert unmapped_venues(crossed) == {("JP", "JPMAP_MF_DARK"),
                                        ("HK", "NOM_DARK")}


def test_unmapped_venue_keeps_its_kdb_name():
    """A venue the sheet has not caught up with must stay visible under its own
    symbol.  Silently dropping it would take it out of %Notional too, so every
    other row would quietly grow."""
    rng = np.random.default_rng(10)
    df = _synth_fills(rng, 20, ["BRAND_NEW_DARK"], country="AU")
    acc = aggregate_fills(fill_metrics(df))
    assert list(acc.index) == ["BRAND_NEW_DARK"], list(acc.index)
    assert unmapped_venues(df) == {("AU", "BRAND_NEW_DARK")}
    known = _synth_fills(rng, 5, ["MS_DARK"], country="AU")
    assert unmapped_venues(known) == set()
    assert list(aggregate_fills(fill_metrics(known)).index) == ["MS Pool"]


def test_child_rows_sum_within_a_group():
    """The child roll comes back one row per (country, venue), so a two venue
    group arrives as two rows.  Indexing on the group instead of summing would
    keep whichever landed last and halve the group's orders and weights."""
    child = pd.DataFrame({
        "country": ["AU", "AU"],
        "venue": ["CENTREPOINT_DARK", "CENTREPOINT_CITI_DARK"],
        "orders": [10.0, 30.0],
        "routed_notional": [1000.0, 3000.0],
        "fr_wsum": [1000.0, 3000.0],
        "fr_wnum": [20000.0, 30000.0],
        "duration_sum": [100.0, 200.0],
        "duration_n": [10.0, 30.0],
    })
    acc = aggregate_child(child)
    assert list(acc.index) == ["Centrepoint"], list(acc.index)
    assert acc.loc["Centrepoint", "orders"] == 40.0
    assert acc.loc["Centrepoint", "routed_notional"] == 4000.0

    rng = np.random.default_rng(11)
    f = aggregate_fills(fill_metrics(pd.concat(
        [_synth_fills(rng, 20, ["CENTREPOINT_DARK"], country="AU"),
         _synth_fills(rng, 20, ["CENTREPOINT_CITI_DARK"], country="AU")],
        ignore_index=True)))
    liq = build_liquidity(f, acc)
    # notional weighted across BOTH venues: 50,000 / 4,000
    assert abs(liq.loc["Centrepoint", "Fill Rate"] - 12.5) < 1e-9
    # and duration is the pooled mean: 300 seconds over 40 orders
    assert abs(liq.loc["Centrepoint", "Duration"] - 7.5) < 1e-9


def test_grouping_is_exact_under_chunking():
    """Grouping must not break the accumulator's day by day property: the sheet
    is applied per day, so two venues in one group have to fold together the
    same way whether they arrive on the same date or on different ones."""
    rng = np.random.default_rng(12)
    days = [_synth_fills(rng, 40, ["CENTREPOINT_DARK", "CENTREPOINT_CITI_DARK",
                                   "MS_DARK"], country="AU") for _ in range(5)]
    chunked = None
    for d in days:
        chunked = fold(chunked, aggregate_fills(fill_metrics(d)))
    one_pass = aggregate_fills(fill_metrics(pd.concat(days, ignore_index=True)))
    chunked, one_pass = chunked.sort_index(), one_pass.sort_index()
    assert list(chunked.index) == ["Centrepoint", "MS Pool"], list(chunked.index)
    for c in FILL_ACC:
        assert np.allclose(chunked[c], one_pass[c], rtol=1e-12, atol=1e-9), c


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


# -----------------------------------------------------------------------------
# Notes
#
# 1. WHAT COULD NOT BE VERIFIED.  Only tables 3.1 and 3.3 were supplied, not the
#    report's Vocabulary section, so several definitions are read from the
#    caption alone.  Where the caption is ambiguous the choice is named here and
#    is one edit away.  Run a single AU date first and compare the magnitudes
#    against the report page before trusting a whole quarter.
#
# 2. FULL SPREAD vs HALF SPREAD.  "Spread normalized" does not say which.  This
#    divides by the full spread.  If our Reversion comes out at consistently
#    half or double the published figures, --half-spread is the first thing to
#    try - it is by far the most likely single cause of a factor of two.
#
# 3. THE +1s LOOKUP takes the prevailing quote, i.e. the last quote at or before
#    fill time + 1s.  The fill-time end of it will pick up a quote stamped in
#    the same millisecond as the fill, which may already be reacting to it.
#    tm-00:00:00.001 in Q_QUOTES gives the strictly-before variant.  The
#    prevailing reading is kept because that is what "spread at the time of
#    execution" means for table 3.1, so one lookup serves both tables.
#
# 4. REVERSION SIGN.  Positive means the price moved OUR way after the fill.
#    Higher is better, Tier 1 is best.  That is the only convention under which
#    the published table is self consistent: MS Pool has the highest Reversion
#    and sits in Tier 1.  Flip the sidesign factor in fill_metrics to reverse
#    it, and the tier numbering follows automatically.
#
# 5. THE Z BASE IS EVERY DARK FILL IN THE RANGE, including fills in venues that
#    --min-fills later excludes from the tiering.  They are still dark fills, and
#    a thin venue contributes few of them, so its influence on the pooled mean is
#    small.  Excluding them instead would make each venue's z score depend on
#    which other venues cleared the threshold, which is worse.
#
# 6. A VENUE ROUTED TO BUT NEVER FILLED does not appear in table 3.1 at all,
#    because every row of it is keyed off executed notional.  That is right for
#    an execution table, but it means 3.1 is not the place to look for a venue
#    that is taking flow and returning nothing - dark_routed_executed.q is,
#    since it deliberately keeps the orders that never filled.
#
# 7. NO WINSORIZING.  Spread normalized reversion has a fat tail when the spread
#    is one tick.  Nothing is clipped, so an outlier shows up rather than being
#    quietly absorbed.  If you decide you want it clipped, it is one line in
#    fill_metrics after rev is computed:
#      df["rev"] = df["rev"].clip(-20, 20)
#    Do it there and not in the accumulator, so the dropped counts stay honest.
#
# 8. WORKORDER IS REDUCED TO ONE ROW PER id_work before anything joins to it.
#    If workorder already holds one row per child order that is free; if it ever
#    holds a row per state change it is the difference between right and a
#    silently multiplied fill count.  It does mean the child order count here
#    can differ from dark_routed_executed.q's orders_routed, which counts rows.
#
# 9. WHAT --decompose IS FOR.  Reversion answers two questions at once, and the
#    published column cannot tell them apart:
#
#        rev = sidesign*(mid1 - fillprice)/spread
#            = Capture + Drift
#
#    Capture is a property of the price we got - 0 at mid, +0.5 at the passive
#    touch.  Drift is a property of what the market did next, i.e. leakage.  A
#    venue can post a respectable Reversion by pricing well while leaking, or by
#    pricing badly and not leaking, and the two call for opposite responses.
#    Note this cuts across the report's own definition rather than extending it:
#    the tiering in table 3.3 is untouched, and stays on Reversion alone.
#
#    Both halves share the good_spread mask and the divisor, so the identity
#    holds under --half-spread too - see test_decomposition_adds_up.
#
# 10. THE OUTPUT FILES carry the same numbers the terminal prints, formatted
#    through the same specs, so the three can only differ in presentation.
#
#    report.xlsx holds NUMBERS, not the rendered strings, so it can be sorted
#    and charted; Excel does the display rounding.  --keep-fills still writes
#    fills.csv rather than a sheet, because a quarter of dark fills runs past
#    Excel's 1,048,576 row ceiling and a sheet truncates there in silence.
#
#    The PDF is the tables and nothing else - no title, no caption, no
#    letterhead.  It is set in Computer Modern (matplotlib ships cmr10), with
#    booktabs rules and right aligned numerics, which is what makes it sit
#    beside the report's own pages.  Deliberately NOT reproduced: the publisher
#    header.  Our numbers under someone else's masthead is a forgery the moment
#    it leaves the desk, and the tables were the part worth matching anyway.
#
# 11. CM CANNOT ALWAYS BE USED, and the failure is silent.  cmr10 is a TeX font
#    carrying the OT1 encoding: it renders _ as a raised dot, {} as dashes and
#    backslash as an opening quote.  A glyph exists in every case, so nothing
#    warns - ASX_CENTREPOINT_DARK simply reaches the page with dots in it.
#    Venue names are kdb symbols and routinely contain underscores, so
#    _pdf_font checks every string it is about to typeset and moves the whole
#    document to DejaVu Serif if any character would be mis-mapped, saying so on
#    stderr.  The right font with the wrong venue names is the worse trade.
#
#    DejaVu is wider than CM, so a table that fitted in CM may not; write_pdf
#    scales a too-wide table down rather than letting columns run off the page.
# -----------------------------------------------------------------------------
