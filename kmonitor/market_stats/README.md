# Market Statistics — KdbMonitor dashboard

Price, Volatility, Spread, Volume, Trade Size and Quote Size for one APAC market
over a date range — the six panels from the Market Statistics page.

| File | What it is |
| --- | --- |
| `market_stats_kmonitor.q` | **The source of truth.** Both dataset queries. |
| `build_dashboard.py` | Reads the `.q` and writes the JSON, so the two cannot drift. |
| `market_stats_kmonitor_dashboard.json` | Generated. This is what you import. |

## Installing it

1. Change `OMS` and `QUOTES` to your environment names in the `env=` of each
   `/ ==== DATASET: … ====` header.
2. **Check two things first**, both from
   `queries/market_stats/market_stats.q`: `.ms.probeRows` (is the feed deltas or
   snapshots — `both` near zero means telling trades from quotes by their fields
   is sound) and `.ms.probeSession` (where the auction volume spikes actually
   are, so `sess` matches your exchange hours and your timestamp zone).
3. `python build_dashboard.py`
4. KdbMonitor → **Dashboards → Import**.

## The three controls

| Control | Choices | What it changes |
| --- | --- | --- |
| Market | AU, JP, HK, IN | the sym suffix the universe is taken from |
| Volume unit | shares, notional | what Volume, Trade Size and Quote Size **mean**; Price, Volatility and Spread are always bps |
| View | intraday, daily | one bar per 10 minutes, or one per date |

All three go back to the server — each changes what is read, so none can be a
transform. One dataset serves both views: `view` decides whether the bucket
column is the 10-minute bar or a constant, and `label` is the x axis either way,
so the six charts are drawn once and the dropdown reshapes them.

## Two things to know

**Volume is not stacked here.** The bar widget takes one `y`, so the panel shows
the total and the table underneath carries `volume_cont` and `volume_auct`. The
chart script in `scripts/market_stats/` draws the stacked version.

**Daily is not an average of the buckets.** It is computed over the whole day in
one pass — averaging bucket means would weight a thin 09:30 bucket the same as a
busy one. Volume still sums, which is why the daily view reads in millions.

The universe is every name **on the feed** with that country's suffix — not an
index, and not the exchange's full list. Worth saying on anything that leaves
the desk.
