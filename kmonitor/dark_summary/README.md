# Dark venue execution — KdbMonitor dashboard

What we executed in dark venues: shares done and notional in USD, by venue, with
each venue's share of the dark total. Same arithmetic as
`queries/dark_summary/dark_summary.q` — same dark-venue test, same fill price,
same rounding — generalised from **one day** to whatever period the reader picks,
so it answers historically as well as live.

| File | What it is |
| --- | --- |
| `dark_summary_kmonitor.q` | **The source of truth.** Both dataset queries, with the reasoning. |
| `build_dashboard.py` | Reads the `.q` and writes the JSON, so the two cannot drift. |
| `dark_summary_kmonitor_dashboard.json` | Generated. This is what you import. |

## Installing it

1. Change `OMS` to whatever your order-server environment is called in Admin.
   It is the `env=` field in the `/ ==== DATASET: … ====` headers of the `.q`.
2. `python build_dashboard.py`
3. KdbMonitor → **Dashboards → Import** → pick
   `dark_summary_kmonitor_dashboard.json`.

One environment only — this reads `workorder` and `target_stock` and nothing
else, exactly as `darkSummary` does. No quote server, no handle. It does need
both a real-time and a historical server registered against that environment,
or the period switch is not offered.

## Two things to know before reading it

**Percentages are shares of the dark book, never of the day's trading.** Nothing
here looks at a lit venue, so this dashboard cannot answer "how much did we do
in the dark" — only "of what we did in the dark, where did it go".

**Over a range, `pct_notional` is a share of the whole range**, not an average of
daily shares. A venue that took everything on one quiet day and nothing since
reads small — which is the honest answer to where the flow went. The per-day
view is the bottom row.

## Which server answers what

| Period selected | Where the rows come from |
| --- | --- |
| Real-time | the RDB, today. Nothing else is asked. |
| A range **not** reaching today | the HDB. Nothing else is asked. |
| A range that **includes** today | the HDB for the range **plus** the RDB for today, unioned — unless the HDB already holds today, in which case the HDB answers alone. |

KdbMonitor sends a dataset to one server, so on a historical period the query
lands on the HDB and reaches back to the RDB itself through
`{{conn:ENV:realtime}}`. The safeguard is `hasToday`: if the HDB has already
been written down for today, the range covers it and stitching would count it
twice, so the RDB is never opened. Each dataset asks that question of **its own**
server — the order HDB and the quote HDB are written down on their own
schedules.

This needs each HDB process to be able to reach its RDB. If it cannot, `hopen`
throws and the panel shows the error — which is the right failure, since a
silently short answer is the thing this fixes.

Editing the q means editing the `.q` and re-running the builder. Editing the
JSON by hand works once and is lost the next time anyone regenerates it.
