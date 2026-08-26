#!/usr/bin/env python3
"""Liquidity through the day for ONE stock on ONE date, in three panels on one
x axis: volume per intraday bucket, the bid and ask, and the cumulative share.

    python scripts/liquidity_profile/liquidity_profile.py --sym 0700.HK --date 2026-08-25
    python scripts/liquidity_profile/liquidity_profile.py --id-target 84213 --date 2026-08-25
    python scripts/liquidity_profile/liquidity_profile.py --sym 7203.JP --date 2026-08-25 --mins 5
    python scripts/liquidity_profile/liquidity_profile.py --sym 0700.HK --date 2026-08-25 --probe
    python scripts/liquidity_profile/liquidity_profile.py --self-test

Writes a PNG, a PDF and a CSV to scripts/liquidity_profile/out/, plus a second
CSV of the child orders when --id-target is given.

WITH --id-target the chart gains our own side of the day: the executed shares
stack into the volume bars, and every child order's price becomes a point on
the quote panel, coloured by what became of it.  Without it, the same three
panels show the market alone and the order server is never opened.

The q is not duplicated here: queries/liquidity_profile/liquidity_profile.q is
sent to both servers as it stands, so the chart and anything else reading these
tables cannot drift apart.

QATT_SERVER and ORDER_SERVER go in a local_settings.py beside this file - see
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
ORDER_SERVER = "CHANGEME:5010"     # execution, workorder, target

MINS = 10                          # bucket width, minutes
THEME = "dark"                     # dark | light
DPI = 144

OUT_DIR = Path(__file__).parent / "out"
_PLACEHOLDER = "CHANGEME"
QUERY_FILE = (Path(__file__).resolve().parents[2]
              / "queries" / "liquidity_profile" / "liquidity_profile.q")

# =============================================================================
# Palette.  Series slots taken UNCHANGED from the data-viz reference palette,
# the same slots market_stats_charts.py uses, so the desk's charts read as one
# set.  Status steps are the reference's fixed four, which are deliberately
# distinct from the series slots so a status colour never impersonates a series.
# =============================================================================

SLOTS = {                          # slot: (light, dark)
    1: ("#2a78d6", "#3987e5"),     # blue    - market volume, and the bid
    2: ("#eb6834", "#d95926"),     # orange  - our executed shares
    3: ("#1baf7a", "#199e70"),     # aqua    - cumulative
    5: ("#e87ba4", "#d55181"),     # magenta - the ask
    6: ("#008300", "#008300"),     # green   - a partial fill
}
STATUS = {"good": "#0ca30c", "critical": "#d03b3b"}    # fixed, never themed
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}
INK = {"light": "#0b0b0b", "dark": "#ffffff"}
INK2 = {"light": "#52514e", "dark": "#c3c2b7"}
GRID = {"light": "#e3e2df", "dark": "#333331"}

#  What became of a child order.  COLOUR IS NEVER THE ONLY CHANNEL - each class
#  carries its own marker too, which matters here because the state vocabulary
#  this server uses is not known to this script: a class that lands on the
#  wrong colour is at least still a distinguishable shape, and --id-target
#  prints the state/request values it actually saw.
#  key: (label, colour or None for muted ink, matplotlib marker kwargs)
POINTS = {
    "filled":    ("filled", STATUS["good"], dict(marker="o", fillstyle="full",
                                                 markersize=7)),
    "partial":   ("part filled", None, dict(marker="o", fillstyle="bottom",
                                            markersize=7)),
    "rejected":  ("rejected", STATUS["critical"], dict(marker="X",
                                                       markersize=8)),
    "cancelled": ("cancelled", None, dict(marker="o", fillstyle="full",
                                          markersize=5)),
    "other":     ("other", None, dict(marker="o", fillstyle="none",
                                      markersize=7)),
}
POINT_ORDER = ["filled", "partial", "rejected", "cancelled", "other"]


def hue(slot: int, theme: str) -> str:
    return SLOTS[slot][1 if theme == "dark" else 0]


def point_colour(key: str, theme: str) -> str:
    """A class's own status colour, or muted ink where it has none.  partial is
    the series green rather than the status green: two greens that differ in
    hue AND in fill, since 'filled' and 'part filled' are the one pair a reader
    is most likely to confuse."""
    if key == "partial":
        return hue(6, theme)
    return POINTS[key][1] or INK2[theme]


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


# =============================================================================
# Fetch
# =============================================================================

def connect(hostport, what):
    """Open a PyKX connection on a host and a port; the servers are open, so
    there is nothing to log in with.  pykx is imported here so the rest of this
    file stays importable, and self-testable, without it."""
    if hostport.startswith(_PLACEHOLDER):
        raise SystemExit(
            f"{hostport!r} is still the placeholder.  Put {what} in a "
            f"local_settings.py beside {Path(__file__).name}.")
    try:
        import pykx
    except ImportError:
        raise SystemExit("pykx is not installed.  pip install pykx")
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected host:port, got {hostport!r}")
    return pykx.QConnection(host=host, port=int(port))


def _load(hostport, what):
    """A connection with the query loaded onto it."""
    h = connect(hostport, what)
    h(QUERY_FILE.read_text(encoding="utf-8"))
    return h


def _bkt(h, mins: int):
    """The bucket as a q time.  int * 00:01:00.000 is a time in milliseconds,
    which is the one unit the query buckets against."""
    return h('{"t"$x*00:01:00.000}', mins)


def _s(v) -> str:
    """A q symbol reaches pandas as bytes; a null as something falsy."""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return "" if v is None or (isinstance(v, float) and np.isnan(v)) else str(v)


def _i(v) -> int:
    """A q int null reaches pandas as NaN or as the -2^31 sentinel; both mean
    'no quantity', and both would otherwise poison a comparison."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if n == -2147483648 else n


def fetch(sym: str, day: dt.date, mins: int, id_target=None) -> dict:
    """Everything one chart needs, as plain frames.  The order server is opened
    only when there is an id_target to look up."""
    src_sym = "--sym"
    tgt = None
    ho = None

    if id_target is not None:
        log(f"  order server  {ORDER_SERVER} ...")
        ho = _load(ORDER_SERVER, "ORDER_SERVER")
        tgt = ho(".lp.tgt", day, id_target).pd()
        if not len(tgt):
            raise SystemExit(
                f"no target with id_target={id_target} on {day}.  Check the "
                f"date - the id is only unique within one.")
        if len(tgt.drop_duplicates(subset=["id_server", "id_target"])) > 1:
            raise SystemExit(
                f"id_target={id_target} on {day} matches more than one server: "
                f"{sorted(set(_i(v) for v in tgt['id_server']))}.  This script "
                f"reads one parent order, so name the server with --id-server.")
        found = _s(tgt["sym"].iloc[0])
        if sym and sym != found:
            raise SystemExit(
                f"--sym {sym} but id_target={id_target} is {found}.  One of "
                f"the two is wrong; drop --sym and it is read from the target.")
        if not sym:
            sym, src_sym = found, "the target"

    log(f"liquidity_profile  {sym} (from {src_sym})  {day}  "
        f"{mins} minute buckets")
    log(f"  quote server  {QATT_SERVER} ...")
    hq = _load(QATT_SERVER, "QATT_SERVER")
    bkt = _bkt(hq, mins)

    t0 = time.perf_counter()
    prof = hq(".lp.profile", sym.encode(), day, bkt).pd()
    log(f"  {len(prof):>7,} buckets   {time.perf_counter() - t0:5.1f}s")

    t0 = time.perf_counter()
    quotes = hq(".lp.quotes", sym.encode(), day).pd()
    log(f"  {len(quotes):>7,} quotes    {time.perf_counter() - t0:5.1f}s"
        f"   (every print, not a bucket average)")

    orders, execs = None, None
    if id_target is not None and len(prof):
        execs = ho(".lp.execs", day, id_target).pd()
        prof = _stack(prof, execs, float(mins) * 60_000.0)
        orders = _classify(ho(".lp.orders", day, id_target).pd())
        log(f"  {len(orders):>7,} child orders, {len(execs):,} fills, "
            f"{_i(prof['ours'].sum()):,} shares executed")
        _say_states(orders)

    return {"profile": prof, "quotes": quotes, "orders": orders,
            "execs": execs, "sym": sym, "day": day, "mins": mins,
            "id_target": id_target}


def bucket_of(t_ms, ms: float):
    """The bucket a time falls in, in milliseconds since midnight.

    The SAME arithmetic .lp.buckets does on the server - ms*(t div ms) - and
    anchored at midnight rather than at the first print, which is what makes
    the two grids line up.  It is done here rather than in q so that one read
    of execution feeds both the bars and the marks: a second, pre-aggregated
    read could disagree with this one and nothing would say so."""
    return np.floor(np.asarray(t_ms, dtype=float) / ms) * ms


def _stack(prof: pd.DataFrame, execs: pd.DataFrame, ms: float) -> pd.DataFrame:
    """Our executed shares summed onto the buckets, and the market's remainder.

    OURS CANNOT EXCEED THE TAPE, arithmetically, but it can here: a venue the
    feed does not carry, or a print counted twice, and the subtraction goes
    negative.  It clamps, and the caller is told how many buckets clamped -
    a negative bar drawn upside down would be the wrong way to find that out."""
    out = prof.copy()
    agg: dict = {}
    if execs is not None and len(execs):
        for b, q in zip(bucket_of(to_ms(execs["time"]), ms),
                        (_i(v) for v in execs["fillsize"])):
            k = int(round(b))
            agg[k] = agg.get(k, 0) + q
    out["ours"] = [agg.get(int(round(to_ms(b))), 0) for b in out["bkt"]]
    over = int((out["ours"] > out["shares"]).sum())
    out["ours"] = np.minimum(out["ours"], out["shares"])
    out["rest"] = out["shares"] - out["ours"]
    if over:
        log(f"  note: our executed exceeded the tape in {over} bucket(s) and "
            f"was clamped to it - a venue off the feed, or a double count")
    return out


def _classify(orders: pd.DataFrame) -> pd.DataFrame:
    """One class per child order, plus the time and price its point sits at."""
    if not len(orders):
        return orders.assign(cls=[], at=[], px=[])
    out = orders.copy()
    out["cls"] = [classify(_i(s), _i(m), _s(st), _s(rq))
                  for s, m, st, rq in zip(out["size"], out["make"],
                                          out["state"], out["request"])]
    #  t_on_market is when the price was actually live against the quote drawn
    #  behind it; an order rejected before it got there has none, and falls
    #  back to the row's own time so it is still placed rather than dropped.
    out["at"] = out["t_on_market"].where(out["t_on_market"].notna(),
                                         out["time"])
    out["px"] = pd.to_numeric(out["price"], errors="coerce")
    return out


def classify(size: int, make: int, state: str, request: str) -> str:
    """What became of a child order.

    FILL FIRST, then the words.  An order that filled and was then cancelled is
    a fill - the cancel is what tidied up the remainder - so make decides
    before state ever gets a say, which is also why the cxl class only ever
    sees make=0 and needs no test for it.

    The words are matched as SUBSTRINGS, case-insensitively, against state and
    request together, because the vocabulary differs by server and this script
    does not know it.  Anything unmatched is `other` and is drawn as a hollow
    ring rather than being folded into a class it might not belong to."""
    if make > 0:
        return "filled" if size > 0 and make >= size else "partial"
    words = f"{state} {request}".lower()
    if "rej" in words:
        return "rejected"
    if "cxl" in words or "cancel" in words:
        return "cancelled"
    return "other"


def _say_states(orders: pd.DataFrame):
    """Print the vocabulary this server actually used, and how each value was
    classified.  The colours rest on substring guesses; this is what makes a
    wrong guess visible instead of silent."""
    if not len(orders):
        return
    seen = {}
    for st, rq, cls in zip(orders["state"], orders["request"], orders["cls"]):
        seen.setdefault((_s(st), _s(rq), cls), 0)
        seen[(_s(st), _s(rq), cls)] += 1
    log("    state / request -> class")
    for (st, rq, cls), n in sorted(seen.items(), key=lambda kv: -kv[1]):
        log(f"      {st or '-':<16} {rq or '-':<16} -> {cls:<10} {n:>6,}")


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


def to_ms(v):
    """A Timedelta, or a series of them, as milliseconds since midnight."""
    if isinstance(v, pd.Series):
        return pd.to_timedelta(v).dt.total_seconds() * 1000.0
    return v.total_seconds() * 1000.0


def xpos(times, lo_ms: float, ms: float):
    """A time placed on the bar axis, which is indexed by bucket.

    Bar i is drawn at x=i and covers [lo+i*ms, lo+(i+1)*ms), so its CENTRE in
    time is half a bucket past its start.  Without the -0.5 a quote curve would
    sit half a bucket to the right of the bars it belongs to - five minutes on
    a ten minute grid, which is visible and wrong."""
    return (np.asarray(times, dtype=float) - lo_ms) / ms - 0.5


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


def _fmt_axis(ax, values):
    """Thousands as 2.5M / 500K, so a shares axis reads like the page."""
    top = float(np.nanmax(np.abs(values))) if len(values) else 0.0
    div, suf = (1e9, "B") if top >= 1e9 else (1e6, "M") if top >= 1e6 \
        else (1e3, "K") if top >= 1e3 else (1.0, "")
    ax.yaxis.set_major_formatter(
        lambda v, _pos: f"{v / div:,.1f}{suf}".replace(".0" + suf, suf))


# =============================================================================
# Draw
# =============================================================================

def draw(d: dict, theme: str = None):
    """Three panels on one x axis.  Pure - takes the frames, returns a fig.

    PANELS, NOT TWO Y AXES.  Shares, price and a running percentage share no
    scale; one axis each is the only honest way to put them on one page."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    theme = theme or THEME
    surface, ink, ink2, grid = (
        SURFACE[theme], INK[theme], INK2[theme], GRID[theme])
    df, quotes, orders = d["profile"], d["quotes"], d["orders"]
    stacked = "ours" in df.columns

    x = np.arange(len(df))
    lab = labels(df)
    ms = float(d["mins"]) * 60_000.0
    lo_ms = to_ms(df["bkt"].iloc[0]) if len(df) else 0.0

    fig, (ax, ax2, ax3) = plt.subplots(
        3, 1, figsize=(15, 11), facecolor=surface, sharex=True,
        gridspec_kw={"height_ratios": [3, 3, 1]})
    fig.subplots_adjust(hspace=0.14, top=0.885, bottom=0.115,
                        left=0.075, right=0.98)

    for a in (ax, ax2, ax3):
        a.set_facecolor(surface)
        a.set_axisbelow(True)
        a.grid(axis="y", color=grid, linewidth=0.8)
        for s in ("top", "right", "bottom", "left"):
            a.spines[s].set_visible(False)
        a.tick_params(colors=ink2, labelsize=8, length=0)

    _panel_volume(ax, df, x, stacked, theme, ink, ink2, surface)
    _panel_price(ax2, d, quotes, orders, lo_ms, ms, theme, ink, ink2, surface,
                 Line2D)
    _panel_cum(ax3, df, x, theme, ink, ink2)

    step = max(1, len(lab) // 30)
    ax3.set_xticks(x[::step])
    ax3.set_xticklabels(lab[::step], rotation=90, fontsize=7, color=ink2)
    ax3.set_xlim(-0.8, len(x) - 0.2)

    _titles(fig, d, ink, ink2, stacked)
    return fig


def _panel_volume(ax, df, x, stacked, theme, ink, ink2, surface):
    shares = df["shares"].to_numpy(dtype=float)
    if stacked:
        ours = df["ours"].to_numpy(dtype=float)
        rest = df["rest"].to_numpy(dtype=float)
        #  ours at the BASE, so the orange sits on the axis and is comparable
        #  bucket to bucket; the 2px surface gap separates the segments
        ax.bar(x, ours, color=hue(2, theme), width=0.82, label="ours, executed")
        ax.bar(x, rest, bottom=ours, color=hue(1, theme), width=0.82,
               edgecolor=surface, linewidth=2, label="rest of the market")
        ax.legend(frameon=False, fontsize=8, labelcolor=ink2, loc="upper left",
                  ncol=2, handlelength=1.2)
    else:
        ax.bar(x, shares, color=hue(1, theme), width=0.82)
    ax.set_ylabel("shares traded", color=ink2, fontsize=9)
    _fmt_axis(ax, shares)

    #  ONE direct label, on the fattest bucket.  A number on every bar is
    #  noise; the busiest bucket is the thing being looked for.
    if len(df):
        blab, bi, bpct = busiest(df)
        #  the fattest bucket is USUALLY the closing auction, i.e. the last
        #  bar, where a centred label runs off the axis - so it turns in at
        #  either end rather than being clipped
        ha = ("right" if bi > 0.8 * len(df) else
              "left" if bi < 0.2 * len(df) else "center")
        ax.annotate(f"{blab}   {_si(shares[bi])}   {bpct:.1f}% of the day",
                    xy=(bi, shares[bi]), xytext=(0, 6),
                    textcoords="offset points", ha=ha, va="bottom",
                    color=ink, fontsize=9)
        top = float(np.max(shares)) if len(shares) else 0.0
        ax.set_ylim(0, top * 1.16 if top > 0 else 1)


def _panel_price(ax, d, quotes, orders, lo_ms, ms, theme, ink, ink2, surface,
                 Line2D):
    ax.set_ylabel("price", color=ink2, fontsize=9)
    lows, highs = [], []

    if len(quotes):
        xq = xpos(to_ms(quotes["time"]), lo_ms, ms)
        bid = pd.to_numeric(quotes["qbid"], errors="coerce").to_numpy(float)
        ask = pd.to_numeric(quotes["qask"], errors="coerce").to_numpy(float)
        #  thin lines: at every print this is tens of thousands of segments,
        #  and anything heavier fills the panel with ink
        ax.fill_between(xq, bid, ask, color=hue(1, theme), alpha=0.10,
                        linewidth=0)
        ax.plot(xq, bid, color=hue(1, theme), linewidth=1.0, label="bid")
        ax.plot(xq, ask, color=hue(5, theme), linewidth=1.0, label="ask")
        lows.append(np.nanmin(bid))
        highs.append(np.nanmax(ask))

    handles = [Line2D([], [], color=hue(1, theme), lw=2, label="bid"),
               Line2D([], [], color=hue(5, theme), lw=2, label="ask")]

    #  WHERE WE ACTUALLY TRADED, one mark per fill, in the same orange as the
    #  executed segment of the bars above - so the two readings of "us" on this
    #  page share a colour.  Small and semi-transparent: a worked order can be
    #  thousands of fills, and they cluster.
    execs = d.get("execs")
    if execs is not None and len(execs):
        xf = xpos(to_ms(execs["time"]), lo_ms, ms)
        yf = pd.to_numeric(execs["fillprice"], errors="coerce").to_numpy(float)
        good = np.isfinite(yf) & (yf > 0)
        if good.any():
            ax.plot(xf[good], yf[good], linestyle="none", marker="o",
                    markersize=3.5, markeredgewidth=0, color=hue(2, theme),
                    alpha=0.75, zorder=2.5)
            handles.append(Line2D([], [], linestyle="none", marker="o",
                                  markersize=5, markeredgewidth=0,
                                  color=hue(2, theme),
                                  label=f"our fills ({int(good.sum())})"))
            lows.append(float(np.nanmin(yf[good])))
            highs.append(float(np.nanmax(yf[good])))

    if orders is not None and len(orders):
        o = orders[orders["px"] > 0].sort_values("at")
        dropped = len(orders) - len(o)
        xo = xpos(to_ms(o["at"]), lo_ms, ms)
        #  the path the algo walked, behind the points rather than over them
        ax.step(xo, o["px"].to_numpy(float), where="post", color=ink2,
                linewidth=1.0, alpha=0.55, zorder=2)
        for key in POINT_ORDER:
            m = (o["cls"] == key).to_numpy()
            if not m.any():
                continue
            c = point_colour(key, theme)
            kw = dict(POINTS[key][2])
            ax.plot(xo[m], o["px"].to_numpy(float)[m], linestyle="none",
                    color=c, markeredgecolor=c, markeredgewidth=1.4,
                    zorder=3, **kw)
            handles.append(Line2D([], [], linestyle="none", color=c,
                                  markeredgecolor=c, markeredgewidth=1.4,
                                  label=f"{POINTS[key][0]} ({int(m.sum())})",
                                  **kw))
        if len(o):
            lows.append(float(np.nanmin(o["px"])))
            highs.append(float(np.nanmax(o["px"])))
        if dropped:
            log(f"  note: {dropped} child order(s) had no usable price and are "
                f"not plotted")

    ax.legend(handles=handles, frameon=False, fontsize=8, labelcolor=ink2,
              loc="upper left", ncol=4, handlelength=1.4)
    if lows and highs:
        lo, hi = min(lows), max(highs)
        pad = (hi - lo) * 0.12 or (hi * 0.01 or 1.0)
        ax.set_ylim(lo - pad, hi + pad * 2.2)


def _panel_cum(ax, df, x, theme, ink, ink2):
    cum = df["cum_pct"].to_numpy(dtype=float)
    ax.plot(x, cum, color=hue(3, theme), linewidth=2)
    ax.set_ylabel("cumulative (%)", color=ink2, fontsize=9)
    ax.set_ylim(0, 105)
    ax.set_yticks([0, 50, 100])
    ax.axhline(50, color=ink2, linewidth=1, linestyle=":")
    hd = half_day(df)
    if hd:
        hlab, hi = hd
        ax.axvline(hi, color=ink2, linewidth=1, linestyle=":")
        ax.annotate(f"half the day by {hlab}", xy=(hi, 50), xytext=(6, -14),
                    textcoords="offset points", ha="left", va="top",
                    color=ink, fontsize=9)


def _titles(fig, d, ink, ink2, stacked):
    df = d["profile"]
    shares = float(df["shares"].sum()) if len(df) else 0.0
    turnover = float(df["turnover"].sum()) if len(df) else 0.0
    head = f"{d['sym']}  -  liquidity through the day"
    if d["id_target"] is not None:
        head += f"   |   id_target {d['id_target']}"
    fig.suptitle(head, color=ink, fontsize=14, x=0.075, ha="left", y=0.968)

    sub = (f"{d['day']}   |   {d['mins']} minute buckets   |   "
           f"{_si(shares)} shares   |   {_si(turnover)} turnover, local "
           f"currency   |   {len(d['quotes']):,} quotes")
    if stacked:
        ours = float(df["ours"].sum())
        pct = 100 * ours / shares if shares else 0.0
        sub += f"   |   ours {_si(ours)} shares, {pct:.2f}% of the tape"
    fig.text(0.075, 0.925, sub, color=ink2, fontsize=10, ha="left")

    #  two lines, because one ran off the right edge of the figure
    foot = ("Auctions included - the open and close buckets carry them, and in "
            "HK, JP and AU the closing auction is often the biggest bar.  "
            "Times are the plant clock (HKT), not exchange local.\n"
            "The bid and ask are every print's quote, not a bucket average.")
    if d["id_target"] is not None:
        foot += ("  Ringed points are the price we showed, at t_on_market (or "
                 "the order's own time where it never reached the book); the "
                 "small orange marks are our fills.")
    fig.text(0.075, 0.020, foot, color=ink2, fontsize=8, ha="left",
             va="bottom", linespacing=1.5)


def save(fig, stem: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        p = OUT_DIR / f"{stem}.{ext}"
        fig.savefig(p, dpi=DPI, facecolor=fig.get_facecolor())
        log(f"  wrote {p}")


def write_csv(d: dict, stem: str):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = d["profile"].copy()
    out["bkt"] = labels(d["profile"])
    p = OUT_DIR / f"{stem}.csv"
    out.to_csv(p, index=False)
    log(f"  wrote {p}")

    if d["orders"] is not None and len(d["orders"]):
        o = d["orders"].copy()
        for c in ("time", "t_transmit", "t_on_market", "at"):
            if c in o.columns:
                o[c] = [bucket_label(v) if isinstance(v, pd.Timedelta) else ""
                        for v in o[c]]
        for c in ("sym", "side", "state", "request"):
            if c in o.columns:
                o[c] = [_s(v) for v in o[c]]
        p = OUT_DIR / f"{stem}_orders.csv"
        o.to_csv(p, index=False)
        log(f"  wrote {p}")


# =============================================================================
# Probe.  q answers a mismatched argument with `type or `length and names
# nothing; each stage is its own IPC call, so the stage that throws is named.
# =============================================================================

def probe(sym: str, day: dt.date, mins: int, id_target=None) -> int:
    log(f"liquidity_profile --probe  {sym or '(from the target)'}  {day}")
    stages = []
    if id_target is not None:
        ho = _load(ORDER_SERVER, "ORDER_SERVER")
        stages += [
            ("the target row", lambda: ho(".lp.tgt", day, id_target).pd()),
            ("our fills", lambda: ho("{count .lp.execs[x;y]}", day, id_target)),
            ("the child orders", lambda: ho("{count .lp.orders[x;y]}", day,
                                            id_target)),
        ]
        if not sym:
            try:
                sym = _s(ho(".lp.tgt", day, id_target).pd()["sym"].iloc[0])
            except Exception:                     # noqa: BLE001 - stages say why
                sym = ""

    hq = _load(QATT_SERVER, "QATT_SERVER")
    raw = (sym or "").encode()
    stages += [
        ("what q was handed", lambda: hq(".lp.types", raw, day,
                                         _bkt(hq, mins))),
        ("what qatt is made of", lambda: hq(".lp.cols")),
        ("the sym coercion", lambda: hq(".lp.sym", raw)),
        ("the bucket cast", lambda: hq(".lp.bkt", _bkt(hq, mins))),
        ("the where clause", lambda: hq(".lp.rows", raw, day)),
        ("the bucketing", lambda: hq("{count .lp.buckets[x;y;z]}", raw, day,
                                     _bkt(hq, mins))),
        ("the full profile", lambda: hq("{count .lp.profile[x;y;z]}", raw, day,
                                        _bkt(hq, mins))),
        ("the quote curve", lambda: hq("{count .lp.quotes[x;y]}", raw, day)),
    ]
    for name, fn in stages:
        try:
            log(f"  ok    {name}: {fn()}")
        except Exception as e:                    # noqa: BLE001 - report it
            log(f"  FAIL  {name}: {type(e).__name__}: {e}")
            log("\n  ^ that stage is the one to fix.  Everything above it ran.")
            return 1
    log("\n  every stage ran - the query is fine against these servers")
    return 0


# =============================================================================
# SELF TEST.  No kdb, no server: synthetic frames shaped like the queries'
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


def _fake_quotes(n=4000) -> pd.DataFrame:
    """Prints across the same day, each carrying the quote that stood at it."""
    mins = np.linspace(570, 570 + 25 * 10, n)
    mid = 350 + 3 * np.sin(np.linspace(0, 7, n))
    return pd.DataFrame({
        "time": [pd.Timedelta(minutes=float(m)) for m in mins],
        "qbid": mid - 0.1,
        "qask": mid + 0.1,
    })


def _fake_orders() -> pd.DataFrame:
    """One child order of each class, plus one with no price at all."""
    rows = [
        #  time_min, t_on_market_min, size, make, state,    request, price
        (600, 601, 1000, 1000, b"done",     b"new",  351.0),
        (620, 621, 1000,  400, b"done",     b"new",  350.5),
        (640, 641, 1000,    0, b"rejected", b"new",  349.0),
        (660, 661, 1000,    0, b"done",     b"cxl",  352.0),
        (680, 681, 1000,    0, b"weird",    b"new",  353.0),
        (700, None, 1000,   0, b"rejected", b"new",    0.0),
    ]
    return pd.DataFrame({
        "time": [pd.Timedelta(minutes=r[0]) for r in rows],
        "t_on_market": [pd.Timedelta(minutes=r[1]) if r[1] else pd.NaT
                        for r in rows],
        "t_transmit": [pd.Timedelta(minutes=r[0]) for r in rows],
        "size": [r[2] for r in rows],
        "make": [r[3] for r in rows],
        "state": [r[4] for r in rows],
        "request": [r[5] for r in rows],
        "price": [r[6] for r in rows],
        "sym": [b"0700.HK"] * len(rows),
        "side": [b"buy"] * len(rows),
    })


def _fake_execs() -> pd.DataFrame:
    """Fills scattered through the morning, at prices around the quote."""
    mins = [592, 594, 601, 603, 605, 612, 623, 624, 651, 652, 690]
    return pd.DataFrame({
        "time": [pd.Timedelta(minutes=m) for m in mins],
        "fillprice": [350.9, 351.0, 351.1, 350.8, 351.0, 350.6, 350.4, 350.5,
                      350.2, 350.3, 349.8],
        "fillsize": [5_000, 5_000, 10_000, 10_000, 10_000, 10_000, 5_000,
                     5_000, 10_000, 10_000, 20_000],
        "id_work": list(range(11)),
    })


def _fake_day(with_target=False) -> dict:
    df = _fake()
    execs = _fake_execs() if with_target else None
    if with_target:
        df = _stack(df, execs, 10 * 60_000.0)
    return {"profile": df, "quotes": _fake_quotes(), "execs": execs,
            "orders": _classify(_fake_orders()) if with_target else None,
            "sym": "0700.HK", "day": dt.date(2026, 8, 25), "mins": 10,
            "id_target": 84213 if with_target else None}


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
    check("the bucket is cast, not used raw", '"t"$x' in src, True)
    check("the sym is coerced to a symbol", "sy:.lp.sym s" in src, True)
    check("and id_target is cast to an int", ".lp.tid:" in src, True)
    #  xbar against a temporal is the trap .lp.bkt's comment describes: its two
    #  arguments have to already agree, and a minute against a time does not.
    check("nothing buckets with xbar", "xbar" in code, False)
    check("every function the script calls is defined",
          all(f".lp.{n}:" in src for n in
              ("profile", "buckets", "quotes", "execs", "orders", "tgt",
               "types", "cols", "rows")), True)

    print("\nclassifying a child order")
    check("all of it filled", classify(1000, 1000, "done", "new"), "filled")
    check("some of it filled", classify(1000, 400, "done", "new"), "partial")
    check("a fill outranks the words - a cancel tidied the remainder",
          classify(1000, 400, "cxl_done", "cxl"), "partial")
    check("over-filled still reads as filled",
          classify(1000, 1200, "done", "new"), "filled")
    check("rejected", classify(1000, 0, "rejected", "new"), "rejected")
    check("rejected is matched as a substring",
          classify(1000, 0, "REJ_BY_BROKER", ""), "rejected")
    check("cancelled with nothing done", classify(1000, 0, "done", "cxl"),
          "cancelled")
    check("a state this script has never seen is not guessed at",
          classify(1000, 0, "weird", "new"), "other")
    check("and neither is a blank one", classify(1000, 0, "", ""), "other")
    check("a null size and a null make do not crash the comparison",
          classify(_i(np.nan), _i(-2147483648), "", ""), "other")

    print("\nbucketing our fills onto the same grid")
    df = _fake()
    ms = 10 * 60_000.0
    #  10:03 is inside the 10:00 bucket, which is index 3 of a day from 09:30
    check("a fill falls in the bucket that contains it",
          int(bucket_of([to_ms(pd.Timedelta(minutes=603))], ms)[0]),
          int(to_ms(pd.Timedelta(minutes=600))))
    check("a fill exactly on a boundary belongs to the bucket it opens",
          int(bucket_of([to_ms(pd.Timedelta(minutes=600))], ms)[0]),
          int(to_ms(pd.Timedelta(minutes=600))))
    one = pd.DataFrame({"time": [pd.Timedelta(minutes=603)],
                        "fillprice": [351.0], "fillsize": [60_000]})
    st = _stack(df, one, ms)
    check("ours lands in its own bucket", int(st["ours"].iloc[3]), 60_000)
    check("and nowhere else", int(st["ours"].iloc[4]), 0)
    check("the rest is the tape less ours",
          int(st["rest"].iloc[3]), int(df["shares"].iloc[3]) - 60_000)
    check("the segments still add to the tape",
          int(st["ours"].iloc[3] + st["rest"].iloc[3]),
          int(df["shares"].iloc[3]))
    check("two fills in one bucket add up",
          int(_stack(df, pd.concat([one, one]), ms)["ours"].iloc[3]), 120_000)
    big = one.assign(fillsize=[999_999_999])
    check("more than the tape clamps rather than drawing a negative bar",
          int(_stack(df, big, ms)["rest"].iloc[3]), 0)
    check("every fill is counted somewhere",
          int(_stack(df, _fake_execs(), ms)["ours"].sum()),
          int(_fake_execs()["fillsize"].sum()))

    print("\nplacing a time on the bar axis")
    lo = to_ms(pd.Timedelta(minutes=570))
    ms = 10 * 60_000.0
    check("a bucket's start sits at its bar's left edge",
          float(xpos([lo], lo, ms)[0]), -0.5)
    check("its midpoint sits at the bar's centre",
          float(xpos([lo + ms / 2], lo, ms)[0]), 0.0)
    check("the next bucket's midpoint is one bar along",
          float(xpos([lo + ms * 1.5], lo, ms)[0]), 1.0)

    print("\nreading a frame")
    check("a q time renders as a clock",
          bucket_label(pd.Timedelta(minutes=570)), "09:30")
    check("and so does the last bucket", labels(df)[-1], "13:40")
    check("the closing auction is the busiest bucket", busiest(df)[0], "13:40")
    check("half the day is found", half_day(df)[0], "12:20")
    check("the crossing is the FIRST bucket at or past 50%",
          bool(df["cum_pct"].iloc[half_day(df)[1] - 1] < 50.0), True)
    check("no crossing when the day never reaches 50%",
          half_day(df.assign(cum_pct=df["cum_pct"] * 0.4)), None)
    check("percentages sum to 100", round(float(df["pct"].sum()), 6), 100.0)
    check("2.5M reads as 2.5M", _si(2_500_000), "2.5M")
    check("a symbol's bytes become text", _s(b"0700.HK"), "0700.HK")

    print("\ndrawing")
    import matplotlib.pyplot as plt
    for with_target in (False, True):
        for theme in ("dark", "light"):
            fig = draw(_fake_day(with_target), theme)
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "t.png"
                fig.savefig(p, dpi=72, facecolor=fig.get_facecolor())
                check(f"{'with' if with_target else 'without'} an id_target, "
                      f"{theme}", p.stat().st_size > 5000, True)
            plt.close(fig)

    #  a flat day has no spike to label, and a day with no quotes at all must
    #  not take the price panel down with it
    fig = draw({**_fake_day(), "profile": _fake(spike=False)}, "dark")
    check("a day with no closing spike still draws", fig is not None, True)
    plt.close(fig)
    fig = draw({**_fake_day(), "quotes": _fake_quotes().iloc[0:0]}, "dark")
    check("and so does a day with no quotes", fig is not None, True)
    plt.close(fig)

    check("an empty frame has no half day crossing", half_day(df.iloc[0:0]),
          None)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


# =============================================================================
# Main
# =============================================================================

def main(argv=None) -> int:
    apply_local(globals(), __file__)
    p = argparse.ArgumentParser(
        description="Liquidity through the day for one stock on one date.")
    p.add_argument("--sym", help="one sym as it appears in qatt, e.g. 0700.HK."
                                 " Optional with --id-target, which names it")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--id-target", type=int,
                   help="a parent order: stacks our executed shares into the "
                        "bars and puts every child order's price on the quote "
                        "panel")
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
    if not a.date:
        p.error("required unless --self-test: --date")
    if not a.sym and a.id_target is None:
        p.error("name the stock with --sym, or a parent order with --id-target")
    if a.mins < 1:
        p.error("--mins must be at least 1")
    try:
        day = dt.date.fromisoformat(a.date)
    except ValueError:
        p.error(f"--date must be YYYY-MM-DD, not {a.date!r}")

    if a.probe:
        return probe(a.sym, day, a.mins, a.id_target)

    d = fetch(a.sym, day, a.mins, a.id_target)
    if not len(d["profile"]):
        log(f"  no prints for {d['sym']} on {day} - check the sym spelling "
            f"against qatt, and that the HDB holds this date")
        return 1
    stem = (f"liquidity_profile_{d['sym'].replace('.', '_')}"
            f"_{day:%Y%m%d}_{a.mins}m")
    if a.id_target is not None:
        stem += f"_t{a.id_target}"
    save(draw(d, a.theme), stem)
    write_csv(d, stem)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
