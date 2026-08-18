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

Talks to TWO kdb processes over PyKX:

  --order-server   workorder, execution, target_stock   (historical)
  --qatt-server    qatt                                 (historical)

  python scripts/reversion_liquidity.py \
      --order-server orderhist:5010 --qatt-server qatthist:5011 \
      --start 2026-04-01 --end 2026-06-30 --country AU

PyKX runs in unlicensed mode - SyncQConnection against a remote process needs
no q licence and no QHOME, because all q evaluation happens on the server.
pykx is imported lazily inside connect(), so --self-test runs anywhere.

  python scripts/reversion_liquidity.py --self-test

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
=============================================================================
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from itertools import combinations

import numpy as np
import pandas as pd

# -----------------------------------------------------------------------------
# q sources.  Sent as text + typed args; see module docstring.
# -----------------------------------------------------------------------------

# Dark fills for one date.  Returns one row per fill, carrying the venue from
# the child order and adv/fxlast from the stock.
#
# ctry is a CHAR VECTOR, not a symbol - "AU", or "" for every country.  Passing
# it as chars and casting with `$ on this side avoids depending on how PyKX
# converts a Python str, which differs between licensed and unlicensed mode.
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
  w:0!select last id_target, last sym, last venue, last size, last make
    by date,id_server,id_work from w;
  ids:exec distinct id_target from w;
  x:select date,id_server,id_target,adv,fxlast,country
    from target_stock where date=d, id_target in ids;
  x:$[0=count ctry; x; select from x where country=`$ctry];
  x:`date`id_server`id_target xkey delete country from x;
  w:w ij x;
  wk:exec distinct id_work from w;
  e:select date,id_server,id_work,sym,fillprice,fillsize,sidesign,
      t_oes_xact,time,bidprice,askprice
    from execution
    where date=d, id_work in wk, fillsize>0;
  k:`date`id_server`id_work xkey
    select date,id_server,id_work,venue,adv,fxlast from w;
  f:e ij k;
  f:update tm:time^t_oes_xact from f;
  `sym`tm xasc select date,sym,tm,venue,fillprice,fillsize,sidesign,
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
  w:select date,id_server,id_work,id_target,venue,size,make,price,
      transmit_lastprice,t_on_market,t_off_market
    from workorder
    where date=d, any (upper venue) like/: dk;
  w:0!select last id_target, last venue, last size, last make, last price,
      last transmit_lastprice, last t_on_market, last t_off_market
    by date,id_server,id_work from w;
  ids:exec distinct id_target from w;
  x:select date,id_server,id_target,fxlast,country
    from target_stock where date=d, id_target in ids;
  x:$[0=count ctry; x; select from x where country=`$ctry];
  x:`date`id_server`id_target xkey delete country from x;
  w:w ij x;
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
    by venue from w
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

# Columns carried in the fill level accumulator.  Every one is a plain sum, so
# folding a day in is a single frame addition.
FILL_ACC = [
    "notional", "n_fill",
    "w_spread", "wsum_spread",
    "w_adv", "wsum_adv",
    "w_filladv", "wsum_filladv",
    "n_rev", "sum_rev", "sumsq_rev",
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


def connect(hostport, user=None, password=None):
    """Open a PyKX connection.  pykx is imported here, not at module level, so
    the pure-python half of this file stays importable without it."""
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Only IPC is needed here, so unlicensed mode is enough - no q "
            "licence and no QHOME required."
        )
    host, port = parse_hostport(hostport)
    kw = {}
    if user:
        kw["username"] = user
    if password:
        kw["password"] = password
    return pykx.SyncQConnection(host=host, port=port, **kw)


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
    """Fold one day of fills into per venue sums (index = venue)."""
    g = df.groupby("venue", dropna=False)
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
        n_stable=("stable", "count"),
        sum_stable=("stable", "sum"),
        no_quote=("no_quote", "sum"),
        bad_spread=("bad_spread", "sum"),
    )
    return out.reindex(columns=FILL_ACC).astype(float)


def aggregate_child(df):
    """Reindex the child order roll onto venue and drop to the sum columns."""
    if len(df) == 0:
        return pd.DataFrame(columns=CHILD_ACC, dtype=float)
    out = df.set_index("venue")
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
    return out.sort_index()


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

def render_liquidity(t):
    d = t.copy()
    for c, n in [("%Notional", 1), ("Spread", 1), ("Adv", 1),
                 ("Fill%adv", 2), ("Fill Rate", 1), ("Duration", 0)]:
        d[c] = d[c].map(lambda v, n=n: "" if pd.isna(v) else f"{v:.{n}f}")
    return d.to_string()


def render_tiering(t):
    d = t[["Reversion", "Stability", "Score", "Tier"]].copy()
    for c in ("Reversion", "Stability", "Score"):
        d[c] = d[c].map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    return d.to_string()


def render_dropped(fill_acc):
    d = fill_acc[["n_fill", "no_quote", "bad_spread"]].copy()
    d["% no quote"] = (100.0 * d["no_quote"] / d["n_fill"]).map(lambda v: f"{v:.1f}")
    d["% crossed"] = (100.0 * d["bad_spread"] / d["n_fill"]).map(lambda v: f"{v:.1f}")
    d = d.astype({"n_fill": int, "no_quote": int, "bad_spread": int})
    d.index.name = "Venue"
    return d.to_string()


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

def daterange(d0, d1):
    d = d0
    while d <= d1:
        yield d
        d += dt.timedelta(days=1)


def run(args):
    ho = connect(args.order_server, args.user, args.password)
    hq = connect(args.qatt_server, args.user, args.password)
    country = args.country or ""

    fill_acc, child_acc, kept = None, None, []
    for day in daterange(args.start, args.end):
        try:
            fills, child = fetch_day(ho, hq, day, country)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {day}: FAILED - {exc}", file=sys.stderr)
            continue
        child_acc = fold(child_acc, aggregate_child(child))
        if len(fills) == 0:
            continue
        m = fill_metrics(fills, half_spread=args.half_spread)
        fill_acc = fold(fill_acc, aggregate_fills(m))
        if args.keep_fills:
            kept.append(m)
        if args.verbose:
            print(f"  {day}: {len(fills)} fills", file=sys.stderr)

    if fill_acc is None or len(fill_acc) == 0:
        raise SystemExit("no dark fills in range - nothing to report")

    liquidity = build_liquidity(fill_acc, child_acc)
    tiering = build_tiering(fill_acc, args.min_fills, args.tiers)

    print(f"\nDark venues {args.start} to {args.end}"
          + (f", country {args.country}" if args.country else "")
          + (", half-spread normalised" if args.half_spread else ""))
    print("\nTable 3.1: Liquidity\n")
    print(render_liquidity(liquidity))
    print("\nTable 3.3: Venue tiering on 1s reversion and quote stability\n")
    if len(tiering) == 0:
        print(f"  no venue reached --min-fills {args.min_fills}")
    else:
        print(render_tiering(tiering))
        if len(tiering) < len(liquidity):
            dropped = sorted(set(liquidity.index) - set(tiering.index))
            print(f"\n  below --min-fills {args.min_fills}, not tiered: "
                  + ", ".join(dropped))
    print("\nFills excluded from the quote based metrics\n")
    print(render_dropped(fill_acc))

    if args.out_dir:
        import os
        os.makedirs(args.out_dir, exist_ok=True)
        liquidity.to_csv(os.path.join(args.out_dir, "liquidity.csv"))
        tiering.to_csv(os.path.join(args.out_dir, "tiering.csv"))
        if args.keep_fills and kept:
            pd.concat(kept).to_csv(os.path.join(args.out_dir, "fills.csv"),
                                   index=False)
        print(f"\nwritten to {args.out_dir}")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Dark venue liquidity and reversion tiering",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--order-server", help="host:port for workorder/execution/target_stock")
    p.add_argument("--qatt-server", help="host:port for historical qatt")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--start", type=dt.date.fromisoformat)
    p.add_argument("--end", type=dt.date.fromisoformat)
    p.add_argument("--country", default="", help="target_stock country, e.g. AU; blank for all")
    p.add_argument("--min-fills", type=int, default=1000,
                   help="minimum usable fills for a venue to be TIERED")
    p.add_argument("--tiers", default="auto", help="'auto' (silhouette) or an integer k")
    p.add_argument("--half-spread", action="store_true",
                   help="normalise reversion by half the spread instead of the full spread")
    p.add_argument("--keep-fills", action="store_true",
                   help="also retain fill level rows; will exhaust memory on a long range")
    p.add_argument("--out-dir", help="also write liquidity.csv and tiering.csv here")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in tests; needs no kdb connection")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    missing = [n for n in ("order_server", "qatt_server", "start", "end")
               if getattr(args, n) is None]
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


def _synth_fills(rng, n, venues):
    """A fill frame with the awkward cases deliberately present."""
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


def test_parse_hostport():
    assert parse_hostport("h:5010") == ("h", 5010)
    for bad in ("h", "h:", ":5010", "h:abc"):
        try:
            parse_hostport(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


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
# -----------------------------------------------------------------------------
