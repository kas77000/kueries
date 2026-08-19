# Market Statistics charts

Six panels — Price, Volatility, Spread, Volume, Trade Size, Quote Size — for one
APAC market over a date range, as a PNG and a PDF. Written to be lifted into
another project, so `draw()` is a pure function: give it a frame, get a figure.

```
python scripts/market_stats/market_stats_charts.py
```

There are **no command line arguments**. Everything is a constant in the CONFIG
block at the top of the file. Edit it, run it again.

| Constant | |
| --- | --- |
| `ORDER_SERVER` / `QATT_SERVER` | `host:port`. Both start as `CHANGEME` and the script refuses to dial until you set them. |
| `START` / `END` | inclusive date range |
| `COUNTRY` | `AU` `JP` `HK` `IN` — add more to `.ms.mkt` in the queries file first |
| `UNIT` | `shares` or `notional` |
| `VIEW` | `intraday`, `daily`, or `both` |
| `THEME` | `dark` or `light` |
| `OUT_DIR`, `DPI` | where the files land, and how sharp |

Output is `market_stats_<COUNTRY>_<START>_<END>_<UNIT>_<view>.png` (and `.pdf`).

---

## How the graphs are put together

### 1. The q is not in this file

`queries/market_stats/market_stats.q` is read off disk and sent to the quote
server as it stands. The script then calls `.ms.intradayWith` / `.ms.dailyWith`.
Nothing about the metrics is restated in Python, so the charts and anything else
reading those tables cannot drift apart. Change a metric in the `.q` and the
next run picks it up.

### 2. Two servers, and neither has to reach the other

`qatt` lives on the quote server. `fxlast` lives on the order server. The script
opens **both**, fetches fx as a table, and hands that table to the quote server
as a query argument — so the two processes never need a route between them.

For `UNIT = "shares"` no rate is needed and the order server is **not opened at
all**.

### 3. One frame shape, two views

`intraday` returns one row per 10-minute bucket; `daily` returns one row per
date. Both carry the same metric columns, so `draw()` takes a `view` argument
only to decide the x tick labels (`09:30` versus `2026/07/28`). The six panels
are described once, in `PANELS`:

```python
PANELS = [
    ("Price",      "price_bps",      "(bps)", 2, False),
    ("Volatility", "volatility_bps", "(bps)", 1, False),
    ...      #  title,  column,     y label, hue slot, stacked?
]
```

Adding a seventh metric is a row in that list plus a column in the q — the
layout code does not change.

### 4. Colour is chosen by rule, not by taste

The palette is the data-viz reference palette, **used unchanged**. Its slots are
already validated for colour-blind separation (adjacent-pair CVD ΔE 8.4 on the
dark surface, 9.1 on light); re-picking hues by eye would throw that away, so
the script indexes into the documented slots and never generates a colour.

Only the **Volume** panel has two series sharing one mark space — Continuous and
Auction — and it takes slots 1 and 2, the adjacent pair those numbers are quoted
for, with a 2px surface-coloured edge between the stacked segments so they stay
separable when they are nearly equal. It is also the only panel with a legend,
because it is the only one with more than one series; the other five are named
by their title.

The other five panels carry **one** series each, so their hue is *panel
identity*, not series identity — nothing within those charts is being told
apart by colour, which is why a different hue per panel is legitimate rather
than decorative.

Both `THEME` values are real palettes stepped for their own surface, not an
inverted copy of each other.

### 5. Everything else is recessive

Grid lines horizontal only and behind the marks, no spines, no tick marks, axis
text in secondary ink rather than the series colour. Tick labels thin out
automatically (`step = len(labels) // 24`) so a 90-date range does not turn the
x axis into a black bar. Share and notional axes are formatted `2.5M` / `500K`
so they read like the page they replace.

A zero line is drawn only when the data actually crosses zero — Price usually
does, the others do not.

### 6. The footer is part of the chart

Every figure carries:

> Universe: every name on the feed carrying the country suffix — not an index,
> and not the exchange's full list.

That sentence is the single most important caveat about these numbers, and a
chart that leaves the desk without it will be read as market-wide truth.

---

## Testing it without kdb

`pykx` is imported lazily inside `connect()`, so the file imports and `draw()`
runs on any machine. To exercise the rendering, build a frame with the metric
columns and call it directly:

```python
import importlib.util
spec = importlib.util.spec_from_file_location("ms", "market_stats_charts.py")
M = importlib.util.module_from_spec(spec); spec.loader.exec_module(M)
fig = M.draw(my_frame, "intraday", unit="shares", theme="dark")
M.save(fig, "test")
```

That is how the layout was checked before it ever saw a server — and it is what
caught the figure title colliding with the Price panel.

## Before the numbers mean anything

Nothing here depends on a vendor vocabulary any more — `typ` carries `"U"`
before the open and is empty for the rest of the session, so trades and quotes
are told apart by **which fields a row carries**, and auction from continuous by
**the clock**.

Two things still to confirm against your own data:

- `.ms.probeRows` — is the feed deltas or snapshots? If `both` is near zero,
  telling a print from a quote by its fields is sound. If `both` is near `n`,
  every row repeats the last of everything and the trade side over-counts
  badly; note 8 in the queries file has the `differ trdSeq` form.
- `.ms.probeSession` — the 5-minute volume profile. The opening and closing
  auction spikes are unmistakable; set `.ms.sess` from where they actually are
  rather than from a published hours table.
