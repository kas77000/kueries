# kmonitor

Queries written for the [KdbMonitor](../../kdbmonitor) dashboard app rather than
for a bare q session. Same logic as the scripts in `queries/`, packaged the way
KdbMonitor consumes it: chained datasets, one raw q query each, with the tokens
KdbMonitor fills in before sending — and reworked so each one answers over a
**historical range** as well as in real time. `stale_price_check/` is the
exception: it is declared real-time only, so it carries neither the mode blocks
nor the `{{conn:…}}` handles the others need.

One folder per dashboard. Each is self-contained: the `.q` is the source of
truth, `build_dashboard.py` turns it into the importable JSON, and the folder
can be copied wherever KdbMonitor wants it without dragging the others along.

| Folder | Dashboard | Built from | Reads |
| --- | --- | --- | --- |
| `limit_up_down/` | Limit up/down — orders on pinned stocks | `queries/limit_up_down/limit_up_down_v2.q` | OMS + quote server |
| `dark_summary/` | Dark venue execution | `queries/dark_summary/dark_summary.q` | OMS only |
| `dark_routed_executed/` | Dark routed vs executed, by country | `queries/dark_summary/dark_routed_executed.q` | OMS only |
| `market_stats/` | Market Statistics — six panels per market | `queries/market_stats/market_stats.q` | QUOTES + OMS (fx) |
| `stale_price_check/` | Stale price check - take orders vs the touch | `queries/stale_price_check/stale_price_check.q` | OMS + QATT, **real-time only** |

Each folder has its own README with the install steps and what its numbers mean.

## The shape they all share

- **`*_kmonitor.q` is the source of truth.** Dataset blocks are delimited by
  `/ ==== DATASET: name | env=ENV ====` … `/ ==== END ====`, and the `env=` in
  that header is where you set your own environment names.
- **`build_dashboard.py`** parses those blocks and writes the JSON, so the query
  in the dashboard and the query in the repo cannot drift. Edit the `.q`, re-run
  the builder, re-import. Editing the JSON by hand works once and is lost the
  next time anyone regenerates it.
- **Every environment needs both sides** — a real-time server and its historical
  twin, registered in Admin. That pairing is the whole mechanism behind the
  period switch; without it the dashboard is offered one period only. A
  dashboard declared `periods: realtime` needs only the live server.
- **Mode blocks carry the difference between periods.** `{{#historical}}…
  {{/historical}}` and `{{#realtime}}…{{/realtime}}` keep one query text serving
  both, which matters because a date predicate is mandatory against a
  partitioned HDB and meaningless against an RDB.
