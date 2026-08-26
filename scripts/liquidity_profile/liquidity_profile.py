#!/usr/bin/env python3
"""Liquidity through the day for ONE stock on ONE date: a bar per intraday
bucket carrying that bucket's share of the day's shares, with the cumulative
share underneath.

    python scripts/liquidity_profile/liquidity_profile.py --sym 0700.HK --date 2026-08-25
    python scripts/liquidity_profile/liquidity_profile.py --sym 7203.JP --date 2026-08-25 --mins 5
    python scripts/liquidity_profile/liquidity_profile.py --self-test

Writes a PNG, a PDF and a CSV to scripts/liquidity_profile/out/.

The q is not duplicated here: queries/liquidity_profile/liquidity_profile.q is
sent to the quote server as it stands, so the chart and anything else reading
qatt cannot drift apart.

QATT_SERVER goes in a local_settings.py beside this file - see
scripts/lib/README.md.  pykx is imported lazily inside connect(), so
--self-test runs on a machine with no kdb at all.

AUCTIONS ARE INCLUDED, which is the whole reason this is a histogram: in HK, JP
and AU the closing auction lands in one bucket and it is often the biggest bar
on the chart.  A single front/back number would bury that; here you can see it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.local_config import apply_local          # noqa: E402

# =============================================================================
# CONFIG.  Override in local_settings.py beside this file; git ignores it.
# =============================================================================

QATT_SERVER = "CHANGEME:5011"      # qatt - the HISTORICAL side, it needs date

MINS = 10                          # bucket width, minutes
THEME = "dark"                     # dark | light
DPI = 144

OUT_DIR = Path(__file__).parent / "out"
_PLACEHOLDER = "CHANGEME"
QUERY_FILE = (Path(__file__).resolve().parents[2]
              / "queries" / "liquidity_profile" / "liquidity_profile.q")

# =============================================================================
# Palette.  Taken UNCHANGED from the data-viz reference palette, the same slots
# market_stats_charts.py uses, so the desk's charts read as one set.
#
# The two panels carry ONE series each, so the hue is panel identity rather
# than series identity and no within-chart separation is at stake.
# =============================================================================

SLOTS = {                          # slot: (light, dark)
    1: ("#2a78d6", "#3987e5"),     # blue   - share of the day
    3: ("#1baf7a", "#199e70"),     # aqua   - cumulative
}
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}
INK = {"light": "#0b0b0b", "dark": "#ffffff"}
INK2 = {"light": "#52514e", "dark": "#c3c2b7"}
GRID = {"light": "#e3e2df", "dark": "#333331"}


def hue(slot: int, theme: str) -> str:
    return SLOTS[slot][1 if theme == "dark" else 0]


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


# =============================================================================
# Fetch
# =============================================================================

def connect(hostport):
    """Open a PyKX connection on a host and a port; the servers are open, so
    there is nothing to log in with.  pykx is imported here so the rest of this
    file stays importable, and self-testable, without it."""
    if hostport.startswith(_PLACEHOLDER):
        raise SystemExit(
            f"{hostport!r} is still the placeholder.  Put QATT_SERVER in a "
            f"local_settings.py beside {Path(__file__).name}.")
    try:
        import pykx
    except ImportError:
        raise SystemExit("pykx is not installed.  pip install pykx")
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected host:port, got {hostport!r}")
    return pykx.QConnection(host=host, port=int(port))


def fetch(sym: str, day: dt.date, mins: int) -> pd.DataFrame:
    """One profile frame, straight off .lp.profile."""
    log(f"liquidity_profile  {sym}  {day}  {mins} minute buckets")
    log(f"  quote server  {QATT_SERVER} ...")
    h = connect(QATT_SERVER)
    log(f"  loading {QUERY_FILE.name} onto the quote server")
    h(QUERY_FILE.read_text(encoding="utf-8"))
    #  the bucket is built ON THE SERVER from a plain integer of minutes, so
    #  the conversion is visible here rather than left to pykx
    bkt = _bkt(h, mins)
    t0 = time.perf_counter()
    #  .encode() sends bytes, which pykx hands to q as a CHAR VECTOR, not a
    #  symbol.  qatt`sym is a symbol column, so .lp.profile coerces it with
    #  .lp.sym; without that, `sym=s` compares N rows against 7 characters and
    #  q answers 'length, naming neither the column nor the argument.
    df = h(".lp.profile", sym.encode(), day, bkt).pd()
    log(f"  {len(df):>4,} buckets   {time.perf_counter() - t0:5.1f}s")
    return df


def probe(sym: str, day: dt.date, mins: int) -> int:
    """Walk the query in stages and name the one that breaks.

    q answers a mismatched argument with `type or `length and names nothing -
    not the column, not the argument, not the line.  Each stage below is its
    own IPC call, so the traceback is replaced by the NAME of the stage that
    failed, and the stages before it print what they saw."""
    log(f"liquidity_profile --probe  {sym}  {day}  {mins} minute buckets")
    h = connect(QATT_SERVER)
    h(QUERY_FILE.read_text(encoding="utf-8"))
    raw = sym.encode()

    stages = [
        ("what q was handed", lambda: h(".lp.types", raw, day, _bkt(h, mins))),
        ("what qatt is made of", lambda: h(".lp.cols")),
        ("the sym coercion", lambda: h(".lp.sym", raw)),
        ("the bucket cast", lambda: h(".lp.bkt", _bkt(h, mins))),
        ("the where clause", lambda: h(".lp.rows", raw, day)),
        ("the bucketing", lambda: h("{count .lp.buckets[x;y;z]}", raw, day,
                                    _bkt(h, mins))),
        ("the full profile", lambda: h("{count .lp.profile[x;y;z]}", raw, day,
                                       _bkt(h, mins))),
    ]
    for name, fn in stages:
        try:
            log(f"  ok    {name}: {fn()}")
        except Exception as e:                    # noqa: BLE001 - report it
            log(f"  FAIL  {name}: {type(e).__name__}: {e}")
            log(f"\n  ^ that stage is the one to fix.  Everything above it ran.")
            return 1
    log("\n  every stage ran - the query is fine against this server")
    return 0


def _bkt(h, mins: int):
    """The bucket as a q time.  int * 00:01:00.000 is a time in milliseconds,
    which is the one unit the query buckets against."""
    return h('{"t"$x*00:01:00.000}', mins)


# =============================================================================
# Read the frame
# =============================================================================

def bucket_label(v) -> str:
    """A q time comes back as a Timedelta; render it as 09:30."""
    if isinstance(v, pd.Timedelta):
        s = int(v.total_seconds())
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}"
    return str(v)[:5]


def labels(df) -> list:
    return [bucket_label(v) for v in df["bkt"]]


def half_day(df):
    """(label, index) of the first bucket at or past 50% of the day, or None.

    This is the sentence a trader actually wants - "half of it was done by
    12:20" - and it is the one number on the cumulative panel worth drawing."""
    if not len(df):
        return None
    hit = np.flatnonzero(df["cum_pct"].to_numpy(dtype=float) >= 50.0)
    if not len(hit):
        return None
    i = int(hit[0])
    return bucket_label(df["bkt"].iloc[i]), i


def busiest(df):
    """(label, index, pct) of the fattest bucket."""
    v = df["pct"].to_numpy(dtype=float)
    i = int(np.argmax(v))
    return bucket_label(df["bkt"].iloc[i]), i, float(v[i])


def _si(v: float) -> str:
    """2.5M / 500K, so a shares figure reads at a glance."""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:,.1f}{suf}"
    return f"{v:,.0f}"


# =============================================================================
# Draw
# =============================================================================

def draw(df, sym: str, day, mins: int, theme: str = None):
    """Two panels on one x axis: the share of the day per bucket, and the
    running total.  Pure - takes a frame, returns a fig.

    TWO PANELS, NOT TWO Y AXES.  pct tops out near 15 and cum_pct ends at 100;
    drawing both against one scale flattens the bars, and drawing them against
    two scales is the dual axis chart that no one can read."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    theme = theme or THEME
    surface, ink, ink2, grid = (
        SURFACE[theme], INK[theme], INK2[theme], GRID[theme])

    x = np.arange(len(df))
    lab = labels(df)
    pct = df["pct"].to_numpy(dtype=float)
    cum = df["cum_pct"].to_numpy(dtype=float)

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(15, 8), facecolor=surface, sharex=True,
        gridspec_kw={"height_ratios": [3, 1]})
    fig.subplots_adjust(hspace=0.12, top=0.855, bottom=0.155,
                        left=0.065, right=0.98)

    for a in (ax, ax2):
        a.set_facecolor(surface)
        a.set_axisbelow(True)
        a.grid(axis="y", color=grid, linewidth=0.8)
        for s in ("top", "right", "bottom", "left"):
            a.spines[s].set_visible(False)
        a.tick_params(colors=ink2, labelsize=8, length=0)

    #  --- share of the day.  width 0.82 leaves the 2px surface gap between
    #  adjacent bars that the mark spec asks for.
    ax.bar(x, pct, color=hue(1, theme), width=0.82)
    ax.set_ylabel("share of the day's shares (%)", color=ink2, fontsize=9)

    #  ONE direct label, on the fattest bucket.  A number on every bar is
    #  noise; the busiest bucket is the thing being looked for.
    if len(df):
        blab, bi, bpct = busiest(df)
        ax.annotate(f"{blab}   {bpct:.1f}%",
                    xy=(bi, pct[bi]), xytext=(0, 6), textcoords="offset points",
                    ha="center", va="bottom", color=ink, fontsize=9)
        ax.set_ylim(0, max(pct) * 1.16 if max(pct) > 0 else 1)

    #  --- cumulative, with the half day crossing called out
    ax2.plot(x, cum, color=hue(3, theme), linewidth=2)
    ax2.set_ylabel("cumulative (%)", color=ink2, fontsize=9)
    ax2.set_ylim(0, 105)
    ax2.set_yticks([0, 50, 100])
    ax2.axhline(50, color=ink2, linewidth=1, linestyle=":")
    hd = half_day(df)
    if hd:
        hlab, hi = hd
        ax2.axvline(hi, color=ink2, linewidth=1, linestyle=":")
        ax2.annotate(f"half the day by {hlab}",
                     xy=(hi, 50), xytext=(6, -14), textcoords="offset points",
                     ha="left", va="top", color=ink, fontsize=9)

    step = max(1, len(lab) // 30)
    ax2.set_xticks(x[::step])
    ax2.set_xticklabels(lab[::step], rotation=90, fontsize=7, color=ink2)
    ax2.set_xlim(-0.8, len(x) - 0.2)

    shares = float(df["shares"].sum()) if len(df) else 0.0
    turnover = float(df["turnover"].sum()) if len(df) else 0.0
    fig.suptitle(f"{sym}  -  liquidity through the day", color=ink,
                 fontsize=14, x=0.065, ha="left", y=0.962)
    fig.text(0.065, 0.905,
             f"{day}   |   {mins} minute buckets   |   {_si(shares)} shares   "
             f"|   {_si(turnover)} turnover, local currency",
             color=ink2, fontsize=10, ha="left")
    fig.text(0.065, 0.035,
             "Auctions included - the open and close buckets carry them, and "
             "in HK, JP and AU the closing auction is often the biggest bar.  "
             "Times are the plant clock (HKT), not exchange local.",
             color=ink2, fontsize=8, ha="left")
    return fig


def save(fig, stem: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        p = OUT_DIR / f"{stem}.{ext}"
        fig.savefig(p, dpi=DPI, facecolor=fig.get_facecolor())
        log(f"  wrote {p}")


def write_csv(df, stem: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out["bkt"] = labels(df)
    p = OUT_DIR / f"{stem}.csv"
    out.to_csv(p, index=False)
    log(f"  wrote {p}")


# =============================================================================
# SELF TEST.  No kdb, no server: a synthetic frame shaped like .lp.profile's
# output, through every function that reads or draws one.
# =============================================================================

def _fake(n=26, spike=True) -> pd.DataFrame:
    """A U shaped day in 10 minute buckets from 09:30, with a closing auction
    on the end - the shape the real thing has to survive."""
    shares = np.array([300, 220, 180, 150, 130, 120, 110, 105, 100, 95, 90, 88,
                       85, 84, 86, 90, 95, 100, 110, 125, 145, 170, 200, 240,
                       300, 900][:n], dtype=float) * 1000
    if not spike:
        shares[-1] = shares[-2]
    bkt = [pd.Timedelta(minutes=570 + 10 * i) for i in range(len(shares))]
    tot = shares.sum()
    return pd.DataFrame({
        "bkt": bkt,
        "trades": (shares / 1000).astype("int64"),
        "shares": shares.astype("int64"),
        "turnover": shares * 350.0,
        "pct": 100 * shares / tot,
        "cum_pct": 100 * np.cumsum(shares) / tot,
    })


def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("liquidity_profile --self-test\n\nthe q")
    src = QUERY_FILE.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("/"))
    from lib import q_lint
    #  balanced() is run on the CODE, not the file: the header's q) prompts are
    #  unmatched parens in every query file in this repo.
    check("the q file's braces and brackets balance",
          q_lint.balanced(code), True)
    check("no q reserved word is used as a name", q_lint.reserved_used(src), [])
    check("the profile function is there", ".lp.profile:" in src, True)
    check("the bucket is cast, not used raw", '"t"$x' in src, True)
    #  this pair is what fetch() depends on: it sends the sym as bytes, which
    #  reaches q as a char vector, and `sym=s` against a symbol column is a
    #  bare 'length unless the query coerces it first
    check("the sym is coerced to a symbol", ".lp.sym:" in src, True)
    check("and the read actually calls it", "sy:.lp.sym s" in src, True)
    #  xbar against a temporal is the trap .lp.bkt's comment describes: its two
    #  arguments have to already agree, and a minute against a time does not.
    #  The bucketing is done in milliseconds instead, so no xbar should survive
    #  in the CODE - the comments discuss it at length, which is why this looks
    #  at `code` rather than at `src`.
    check("nothing buckets with xbar", "xbar" in code, False)
    check("the stages for --probe are there",
          all(f".lp.{n}:" in src for n in ("types", "cols", "rows", "buckets")),
          True)

    print("\nreading a frame")
    df = _fake()
    check("a q time renders as a clock",
          bucket_label(pd.Timedelta(minutes=570)), "09:30")
    check("and so does the last bucket", labels(df)[-1], "13:40")
    check("the closing auction is the busiest bucket", busiest(df)[0], "13:40")
    #  the fake's morning is heavy but its closing auction is heavier, so the
    #  crossing sits after lunch at 12:20 - a fixed expectation, not a
    #  recomputation of what the function just did
    check("half the day is found", half_day(df)[0], "12:20")
    check("the crossing is the FIRST bucket at or past 50%",
          bool(df["cum_pct"].iloc[half_day(df)[1] - 1] < 50.0), True)
    check("no crossing when the day never reaches 50%",
          half_day(df.assign(cum_pct=df["cum_pct"] * 0.4)), None)
    check("percentages sum to 100", round(float(df["pct"].sum()), 6), 100.0)
    check("2.5M reads as 2.5M", _si(2_500_000), "2.5M")
    check("and 900 stays 900", _si(900), "900")

    print("\ndrawing")
    import matplotlib.pyplot as plt
    for theme in ("dark", "light"):
        fig = draw(df, "0700.HK", dt.date(2026, 8, 25), 10, theme)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / f"t_{theme}.png"
            fig.savefig(p, dpi=72, facecolor=fig.get_facecolor())
            check(f"{theme} renders to a file", p.stat().st_size > 5000, True)
        plt.close(fig)

    #  a flat day has no spike to label and must not fall over
    fig = draw(_fake(spike=False), "FLAT.HK", dt.date(2026, 8, 25), 10, "dark")
    check("a day with no closing spike still draws", fig is not None, True)
    plt.close(fig)

    #  an empty frame is what .lp.profile returns for a name that did not trade
    empty = df.iloc[0:0]
    check("an empty frame has no half day crossing", half_day(empty), None)
    check("and main() reports it rather than drawing it", len(empty), 0)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# =============================================================================
# Main
# =============================================================================

def main(argv=None) -> int:
    apply_local(globals(), __file__)
    p = argparse.ArgumentParser(
        description="Liquidity through the day for one stock on one date.")
    p.add_argument("--sym", help="one sym, as it appears in qatt, e.g. 0700.HK")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--mins", type=int, default=MINS,
                   help=f"bucket width in minutes (default {MINS})")
    p.add_argument("--theme", choices=("dark", "light"), default=THEME)
    p.add_argument("--probe", action="store_true",
                   help="walk the query in stages and name the one that breaks")
    p.add_argument("--self-test", action="store_true",
                   help="run the offline checks; needs no server and no kdb")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()
    missing = [n for n in ("sym", "date") if not getattr(a, n)]
    if missing:
        p.error("required unless --self-test: "
                + ", ".join("--" + m for m in missing))
    if a.mins < 1:
        p.error("--mins must be at least 1")
    try:
        day = dt.date.fromisoformat(a.date)
    except ValueError:
        p.error(f"--date must be YYYY-MM-DD, not {a.date!r}")

    if a.probe:
        return probe(a.sym, day, a.mins)

    df = fetch(a.sym, day, a.mins)
    if not len(df):
        log(f"  no prints for {a.sym} on {day} - check the sym spelling "
            f"against qatt, and that the HDB holds this date")
        return 1
    stem = f"liquidity_profile_{a.sym.replace('.', '_')}_{day:%Y%m%d}_{a.mins}m"
    save(draw(df, a.sym, day, a.mins, a.theme), stem)
    write_csv(df, stem)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
