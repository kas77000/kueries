#!/usr/bin/env python3
"""Market Statistics charts: Price, Volatility, Spread, Volume, Trade Size,
Quote Size - as an INTRADAY panel (per 10 minute bucket) and a DAILY AVERAGES
panel (per date), for one country and one date range.

    python scripts/market_stats/market_stats_charts.py

There are NO command line arguments.  Everything is a constant in the CONFIG
block below - edit it and run it again.

The q is not duplicated here: queries/market_stats/market_stats.q is sent to the
quote server as it stands, so the charts and anything else reading those tables
cannot drift apart.

Two connections.  qatt lives on the quote server; fxlast lives on the order
server, and is fetched separately and passed in as a table, so the quote server
never needs to reach the order server.  For UNIT = "shares" no rate is needed
and the order server is not opened at all.

pykx is imported lazily, so --this file imports-- and its rendering can be
exercised without kdb.  Only fetch() needs a server.
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# =============================================================================
# CONFIG.  Edit these.  No arguments, by design.
# =============================================================================

ORDER_SERVER = "CHANGEME:5010"      # target_stock, for fxlast
QATT_SERVER = "CHANGEME:5011"       # qatt - the HISTORICAL side, it needs date
USER = None
PASSWORD = None

START = dt.date(2026, 7, 28)
END = dt.date(2026, 7, 30)

COUNTRY = "HK"          # AU | JP | HK | IN  (add more in market_stats.q)
UNIT = "shares"         # shares | notional
VIEW = "both"           # intraday | daily | both
THEME = "dark"          # dark | light

OUT_DIR = Path(__file__).parent / "out"
DPI = 144

_PLACEHOLDER = "CHANGEME"
QUERY_FILE = Path(__file__).resolve().parents[2] / "queries" / "market_stats" / "market_stats.q"

# =============================================================================
# Palette.  Taken UNCHANGED from the data-viz reference palette, which
# documents its own validation - adjacent-pair CVD dE 9.1 light / 8.4 dark,
# normal-vision 19.6 / 19.3.  Slots are used in fixed order and never cycled.
#
# Only the Volume chart has two series in one mark space (Continuous, Auction);
# it takes slots 1 and 2, the pair those numbers are quoted for.  The other five
# panels carry ONE series each, so their hue is panel identity rather than
# series identity, and no within-chart separation is at stake.
# =============================================================================

SLOTS = {                          # slot: (light, dark)
    1: ("#2a78d6", "#3987e5"),     # blue
    2: ("#eb6834", "#d95926"),     # orange
    3: ("#1baf7a", "#199e70"),     # aqua
    4: ("#eda100", "#c98500"),     # yellow
    5: ("#e87ba4", "#d55181"),     # magenta
    6: ("#008300", "#008300"),     # green
}
SURFACE = {"light": "#fcfcfb", "dark": "#1a1a19"}
INK = {"light": "#0b0b0b", "dark": "#ffffff"}
INK2 = {"light": "#52514e", "dark": "#c3c2b7"}
GRID = {"light": "#e3e2df", "dark": "#333331"}


def hue(slot: int) -> str:
    return SLOTS[slot][1 if THEME == "dark" else 0]


# Panel order matches the page being reproduced: three rates, then three sizes.
# (title, column, unit-label, slot, stacked)
PANELS = [
    ("Price",      "price_bps",      "(bps)", 2, False),
    ("Volatility", "volatility_bps", "(bps)", 1, False),
    ("Spread",     "spread_bps",     "(bps)", 3, False),
    ("Volume",     "volume",         None,    1, True),
    ("Trade Size", "trade_size",     None,    4, False),
    ("Quote Size", "quote_size",     None,    5, False),
]


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


# =============================================================================
# Fetch
# =============================================================================

def connect(hostport, user=None, password=None):
    """Open a PyKX connection.  pykx is imported here so the rest of this file
    stays importable without it."""
    if hostport.startswith(_PLACEHOLDER):
        raise SystemExit(
            f"{hostport!r} is still the placeholder.  Set ORDER_SERVER and "
            f"QATT_SERVER near the top of {__file__}.")
    try:
        import pykx
    except ImportError:
        raise SystemExit("pykx is not installed.  pip install pykx")
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        raise SystemExit(f"expected host:port, got {hostport!r}")
    kw = {"username": user, "password": password} if user else {}
    return pykx.QConnection(host=host, port=int(port), **kw)


def _dates():
    n = (END - START).days + 1
    if n < 1:
        raise SystemExit(f"END {END} is before START {START}")
    return [START + dt.timedelta(days=i) for i in range(n)]


def fetch():
    """(intraday, daily) frames.  Either is None when VIEW excludes it."""
    if UNIT not in ("shares", "notional"):
        raise SystemExit(f"UNIT must be 'shares' or 'notional', not {UNIT!r}")
    if VIEW not in ("intraday", "daily", "both"):
        raise SystemExit(f"VIEW must be 'intraday', 'daily' or 'both', not {VIEW!r}")

    days = _dates()
    log(f"market_stats  {START} to {END}  ({len(days)} dates), {COUNTRY}, {UNIT}")
    log(f"  quote server  {QATT_SERVER} ...")
    hq = connect(QATT_SERVER, USER, PASSWORD)
    log(f"  loading {QUERY_FILE.name} onto the quote server")
    hq(QUERY_FILE.read_text(encoding="utf-8"))

    # fx off the ORDER server, then handed to the quote server as a table - so
    # the two never have to reach each other.
    if UNIT == "notional":
        log(f"  order server  {ORDER_SERVER} ...  (fxlast, for notional)")
        ho = connect(ORDER_SERVER, USER, PASSWORD)
        ho(QUERY_FILE.read_text(encoding="utf-8"))
        sfx = f"*.{COUNTRY}"
        fx = ho(".ms.fxOn", days, sfx.encode())
        log(f"  fx rows: {len(fx.pd()):,}")
    else:
        fx = hq("([] date:0#0Nd; sym:0#`; fxlast:0#0n)")

    out = {}
    for name, fn in (("intraday", ".ms.intradayWith"), ("daily", ".ms.dailyWith")):
        if VIEW not in (name, "both"):
            out[name] = None
            continue
        t0 = time.perf_counter()
        df = hq(fn, fx, days, COUNTRY.encode(), UNIT.encode()).pd()
        log(f"  {name:<9} {len(df):>6,} rows   {time.perf_counter()-t0:5.1f}s")
        out[name] = df
    return out["intraday"], out["daily"]


# =============================================================================
# Draw
# =============================================================================

def _x_labels(df, view):
    """Bucket times as 09:30, or dates as 2026/07/28 - matching the page."""
    if view == "intraday":
        return [str(v)[:5] if not isinstance(v, pd.Timedelta)
                else f"{int(v.total_seconds())//3600:02d}:"
                     f"{int(v.total_seconds())%3600//60:02d}"
                for v in df["bkt"]]
    return [pd.Timestamp(v).strftime("%Y/%m/%d") for v in df["date"]]


def _fmt_axis(ax, values):
    """Thousands as 2.5M / 500K, so a shares axis reads like the page."""
    top = float(np.nanmax(np.abs(values))) if len(values) else 0.0
    div, suf = (1e9, "B") if top >= 1e9 else (1e6, "M") if top >= 1e6 \
        else (1e3, "K") if top >= 1e3 else (1.0, "")
    ax.yaxis.set_major_formatter(
        lambda v, _pos: f"{v/div:,.1f}{suf}".replace(".0" + suf, suf))


def draw(df, view, unit=None, theme=None, title=None):
    """One 2x3 figure of the six panels.  Pure: takes a frame, returns a fig."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    unit = unit or UNIT
    theme = theme or THEME
    surface, ink, ink2, grid = (SURFACE[theme], INK[theme], INK2[theme], GRID[theme])
    size_unit = "( # shares )" if unit == "shares" else "( USD )"

    x = np.arange(len(df))
    labels = _x_labels(df, view)
    fig, axes = plt.subplots(2, 3, figsize=(16, 8.4), facecolor=surface)
    # top leaves room for the suptitle: at 0.90 it collided with the Price title
    fig.subplots_adjust(hspace=0.46, wspace=0.22, top=0.855,
                        bottom=0.135, left=0.06, right=0.98)

    for ax, (name, col, ulab, slot, stacked) in zip(axes.ravel(), PANELS):
        ax.set_facecolor(surface)
        ax.set_title(name, color=ink, fontsize=12, pad=10)
        ax.set_ylabel(ulab or size_unit, color=ink2, fontsize=9)
        # recessive grid, horizontal only, behind the marks
        ax.set_axisbelow(True)
        ax.grid(axis="y", color=grid, linewidth=0.8)
        for s in ("top", "right", "bottom", "left"):
            ax.spines[s].set_visible(False)
        ax.tick_params(colors=ink2, labelsize=8, length=0)

        if stacked:
            cont = df["volume_cont"].to_numpy(dtype=float)
            auct = df["volume_auct"].to_numpy(dtype=float)
            # 2px surface gap between the segments, per the mark spec
            ax.bar(x, cont, color=hue(1), label="Continuous", width=0.82)
            ax.bar(x, auct, bottom=cont, color=hue(2), label="Auction",
                   width=0.82, edgecolor=surface, linewidth=2)
            ax.legend(frameon=False, fontsize=8, labelcolor=ink2,
                      loc="upper left", ncol=2, handlelength=1.2)
            _fmt_axis(ax, cont + auct)
        else:
            v = df[col].to_numpy(dtype=float)
            ax.bar(x, v, color=hue(slot), width=0.82)
            if col in ("trade_size", "quote_size"):
                _fmt_axis(ax, v)
            if np.nanmin(v) < 0 < np.nanmax(v):
                ax.axhline(0, color=ink2, linewidth=1)

        step = max(1, len(labels) // 24)
        ax.set_xticks(x[::step])
        ax.set_xticklabels(labels[::step], rotation=90, fontsize=7, color=ink2)
        ax.set_xlim(-0.8, len(x) - 0.2)

    head = title or (f"Market Statistics - {'Intraday' if view == 'intraday' else 'Daily averages'}"
                     f"   |   {COUNTRY}   {START} to {END}   |   "
                     f"volume in {'shares' if unit == 'shares' else 'USD notional'}")
    fig.suptitle(head, color=ink, fontsize=13, x=0.06, ha="left", y=0.955)
    fig.text(0.06, 0.03,
             "Universe: every name on the feed carrying the country suffix - "
             "not an index, and not the exchange's full list.",
             color=ink2, fontsize=8, ha="left")
    return fig


def save(fig, name):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        p = OUT_DIR / f"{name}.{ext}"
        fig.savefig(p, dpi=DPI, facecolor=fig.get_facecolor())
        log(f"  wrote {p}")


def main():
    intraday, daily = fetch()
    stem = f"market_stats_{COUNTRY}_{START:%Y%m%d}_{END:%Y%m%d}_{UNIT}"
    for df, view in ((intraday, "intraday"), (daily, "daily")):
        if df is None:
            continue
        if len(df) == 0:
            log(f"  {view}: no rows - run .ms.probeRows to check the feed "
                f"shape, and confirm the HDB holds these dates")
            continue
        save(draw(df, view), f"{stem}_{view}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
