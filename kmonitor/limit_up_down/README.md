# Limit up/down — KdbMonitor dashboard

Every parent order — **activated or not** — sitting on a stock that was locked
or one-sided for longer than the **Minimum duration** the reader sets, and how
long that lasted. Reads the same live and over a date range; historically it
finds episodes that have since **ended**, which is the part a real-time-only
query cannot do.

Same detection as `queries/limit_up_down/limit_up_down_v2.q`, repackaged the way
KdbMonitor consumes it: chained datasets, one raw q query each, with the tokens
KdbMonitor fills in before sending.

| File | What it is |
| --- | --- |
| `limit_up_down_kmonitor.q` | **The source of truth.** The three dataset queries, with the reasoning. |
| `build_dashboard.py` | Reads the `.q` and writes the JSON, so the two cannot drift. |
| `limit_up_down_kmonitor_dashboard.json` | Generated. This is what you import. |

## Installing it

1. Change the two environment names if yours are not `OMS` (target /
   target_state / target_stock / workorder) and `QUOTES` (qatt). They are the
   `env=` fields in the `/ ==== DATASET: … ====` headers of the `.q`.
2. `python build_dashboard.py`
3. KdbMonitor → **Dashboards → Import** → pick
   `limit_up_down_kmonitor_dashboard.json`.

Both environments need a real-time **and** a historical server registered in
Admin, otherwise the period switch is not offered — that switch is the whole
mechanism behind querying live and querying back.

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
