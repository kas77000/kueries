# Stale price check — design

**Date:** 2026-08-28
**Problem:** orders in the ai3 algo went out priced off market data that had
gone stale. There is no way to see, live, which workorders that is still
happening to.

## What it answers

For every workorder sitting under a live parent: the price the algo gave it,
beside the last print `qatt` actually had for that name at the same instant.
Two numbers side by side, and the gap between them.

## Row set

1. **Live parents.** Latest `state` per `date,id_server,id_target` in
   `target_state`; keep the ones now `` `activated ``. That is the live book.
2. **Their children.** Every `workorder0` row whose `id_target` is in that set.
3. **One row per child.** `workorder0` carries a row per state change, so
   collapse to the last row per `date,id_server,id_work`, ordered by
   `sequence`. `state` survives as a column, not a filter — a child that is
   `init` under a live parent is still exposed to the same bad data.

No `state` filter on the child. The parent being activated is the filter.

## Price comparison

`qatt` is filtered to **prints only** (`0<price`). Quote-only rows carry a null
`price`, and an as-of onto one of those would date the order against a row that
never traded.

`aj[`sym`time; workorders; prints]` puts each workorder on the last print
at or before its own `t_gen`. `t_gen` and `qatt`.`time` come off the same
clock, so they compare directly — no timezone or DST conversion.

| Column | Meaning |
| --- | --- |
| `price` | what the algo priced the workorder at |
| `gen_price` | last `qatt` print at or before `t_gen` |
| `dev_bps` | `10000*(price-gen_price)%gen_price` |
| `ptime` | when that print was |
| `price_age_ms` | `t_gen - ptime` — how old the print already was |
| `now_price` | latest print for that name |
| `now_dev_bps` | the gap to it now |
| `now_age_ms` | how long since the tape last moved on that name |
| `flag` | `noprint` / `both` / `price` / `age` / `ok` |
| `flagged` | `flag` is not `ok` — one boolean for counting |

`price_age_ms` answers "was the data already old when the order was born".
`now_age_ms` answers "is it still old right now", which is the difference
between an order that was born bad and one that is still being fed bad data.

**Market orders are filtered out entirely** — this is a limit-order report. One
carries `price` 0, so there is no order price to hold a print against, and a
`dev_bps` of `-10000` for an order that never had a price is a lie that would
sort to the top of every run.

The filter is `0<price`, applied on the OMS side *after* the collapse to one row
per `id_work`, so the order's current price is what decides rather than some
earlier row's. `0<` drops a null price as well. `otype` stays in the output so
the kind of limit order is still visible.

## The lookback, and the two windows

`lookback` is in minutes and bounds `t_gen` — how recently the workorder was
created. It exists because reading the whole session out of `qatt` is too slow
to run live. Named `lookback`, not `mins`: `mins` is the q keyword for running
minimums and using it as a parameter gives a type error.

`qatt` is read from **twice that far back**, because an order generated at the
very start of the order window still needs prints before it to as-of onto:

```
qatt scanned   |<--------- 2 x lookback --------->|
workorders                    |<--- lookback ---->|
               t0-lookback   t0                  now
```

So `noprint` means **"no print in the scanned window"**, not "never traded
today". Still a finding — a name we are working that has not printed in twice
the lookback is stale by any reading — but a different sentence from the one
the column said before the bound existed.

In the KdbMonitor version the lookback applies to the **real-time period only**.
On a historical period both windows are passed `00:00:00.000`, ie no bound: "the
last 10 minutes" cannot mean anything on a past date, where the reader has
already bounded the frame with the dates.

`sym in syms` stays the *first* constraint in the `qatt` where clause, ahead of
the time bound. The RDB keeps `` `g#sym `` and the where clause can only use
that attribute on the constraint it applies first; the time cut then runs on
what survives.

## Thresholds

`stalePriceCheck[h;lookback;minDevBps;minAgeMs]`.

A threshold of `0` turns that test **off**. `[h;10;0;0]` therefore returns every
workorder in the window, ranked worst-first, with every number filled in
and `flag` reading `ok` on all of them — the calibration run. Once the numbers
on this book are known, `[h;25;5000]` returns breaches only.

Rows with no print in the scanned window are always returned, whatever the
thresholds. A name we are working that has not traded is a finding, not an
absence.

## Known limit

Prints-only means a thin name shows a large `price_age_ms` because it is not
trading, not because anything is stale. `dev_bps` is the honest signal there.
If the noise proves bad, the fix is to divide by how often that name normally
prints — `adv` off `target_stock` — which is deliberately not built here.

## Where it runs

`qatt` is on the quote server, `workorder0` and `target_state` on the OMS, and
an `aj` needs both sides local. So the query runs **from the quote session** and
ships a lambda to the OMS over a handle. Serialized lambda, not a query string.
Same arrangement as `queries/limit_up_down/limit_up_down_v2.q`.

Pass `0i` for the handle when the order tables are local.

## KdbMonitor version

Two chained datasets — dataset 1 already carries every order field, so dataset
2 finishes the job. No third hop back to the OMS.

| Dataset | Env | Does |
| --- | --- | --- |
| `live_orders` | OMS | the row set above |
| `stale_check` | QUOTES | takes `{{table:live_orders}}` whole, both price lookups, returns the blotter |

Both halves use the `hasToday` stitch guard the other dashboards use: on a
historical period the query lands on the HDB and reaches back to its own RDB
through `{{conn:ENV:realtime}}`, unless the HDB already holds today.

The quote RDB has no `date` column, so its half gets `update date:.z.D` and the
as-of becomes `aj[`date`sym`time; …]` — an exact match on date and sym, as-of on
time, so a historical range cannot join a print across a day boundary.

`now_*` on a past date means **that day's last print**, not this second. It is
measured to the session end read off the other names in the frame, the same way
`limit_up_down` reads a session end.

Reader parameters: `lookback_mins` (default 10, real-time only),
`min_dev_bps` (default 25), `min_price_age_ms` (default 5000). Both
thresholds accept 0 to turn that test off.

## Deliverables

```
queries/stale_price_check/stale_price_check.q
kmonitor/stale_price_check/stale_price_check_kmonitor.q
                          /build_dashboard.py
                          /stale_price_check_kmonitor_dashboard.json
                          /README.md
```
