# Stale price check — KdbMonitor dashboard

Every workorder sitting under an **activated** parent, with the price the algo
gave it beside **the last print `qatt` actually had for that name at the same
instant**. Two prices, side by side, and the gap between them.

Built after orders in ai3 went out priced off market data that had gone stale,
to see which workorders that is still happening to. Reads the same live and
over a historical range.

Same logic as `queries/stale_price_check/stale_price_check.q`, repackaged the
way KdbMonitor consumes it: chained datasets, one raw q query each, with the
tokens KdbMonitor fills in before sending.

| File | What it is |
| --- | --- |
| `stale_price_check_kmonitor.q` | **The source of truth.** The two dataset queries, with the reasoning. |
| `build_dashboard.py` | Reads the `.q` and writes the JSON, so the two cannot drift. |
| `stale_price_check_kmonitor_dashboard.json` | Generated. This is what you import. |

## Installing it

1. Change the two environment names if yours are not `OMS` (`target_state`,
   `workorder0`) and `QUOTES` (`qatt`). They are the `env=` fields in the
   `/ ==== DATASET: … ====` headers of the `.q` **and** the
   `{{conn:OMS:realtime}}` / `{{conn:QUOTES:realtime}}` calls inside them.
2. `python build_dashboard.py`
3. KdbMonitor → **Dashboards → Import** → pick
   `stale_price_check_kmonitor_dashboard.json`.

Both environments need a real-time **and** a historical server registered in
Admin, otherwise the period switch is not offered.

## What the columns mean

| Column | |
| --- | --- |
| `price` | what the algo priced the workorder at |
| `gen_price` | last `qatt` print at or before that workorder's `t_gen` |
| `dev_bps` | `10000*(price-gen_price)%gen_price` — the gap between the two |
| `ptime` | when that print was |
| `price_age_ms` | `t_gen - ptime` — how old the print already was when the order was born |
| `now_price` | latest print for that name |
| `now_dev_bps` | the gap to it now |
| `now_age_ms` | how long since the tape last moved on that name |
| `flag` | `noprint` / `both` / `price` / `age` / `ok` — see the lookback section for what `noprint` means |

`price_age_ms` answers *"was the data already old when the order was born"*.
`now_age_ms` answers *"is it still old right now"* — which is the difference
between an order that was born bad and one still being fed bad data.

`t_gen` and `qatt`'s `time` come off the same clock, so they compare directly.
No timezone and no DST conversion anywhere in this dashboard.

## The lookback, and the two windows

**Lookback (minutes)** bounds `t_gen` — how recently the workorder was created.
It exists because reading the whole session out of `qatt` is too slow to run on
a refresh.

`qatt` is read from **twice that far back**. An order generated at the very
start of the order window still needs prints *before* it to as-of onto, so the
quote window has to open earlier than the order window does:

```
qatt scanned   |<--------- 2 x lookback --------->|
workorders                    |<--- lookback ---->|
               t0-lookback   t0                  now
```

The consequence is that **`noprint` means "no print in the scanned window"**,
not "never traded today". That is still a finding rather than a gap: a name we
are working that has not printed in twice your lookback is stale by any
reading. But it is a different sentence from the one the column used to say,
and a very short lookback will produce more of them on thin names.

**On a historical period the lookback is ignored.** "The last 10 minutes"
cannot mean anything on a past date — the reader already bounded that frame
with the dates — so both windows are passed `00:00:00.000` and the whole of
each day in range is read. A historical run is deliberately the slow path.

## The two thresholds

**Minimum deviation (bps)** and **Minimum print age (ms)**. A threshold of `0`
turns **that test off** rather than making everything breach it.

So `0` / `0` is the calibration run: every workorder under a live parent, every
number filled in, `flag` reading `ok` on all of them because nothing was asked
of them. Read `dev_bps` and `price_age_ms` directly on that run, decide what
"bad" looks like on this book, then set the two boxes. The shipped defaults —
25 bps and 5000 ms — are a starting guess, not a measurement.

Rows where the name has **no print in the scanned window** come back whatever
the thresholds say. A name we are working that has not traded is a finding, not
an absence.

## What it deliberately does not do

`qatt` is filtered to **prints only** (`0<price`). Quote-only rows carry a null
price, and an as-of onto one of those would date the order against a row that
never traded.

The cost of that is a thin name showing a large `price_age_ms` because it is
not trading, not because anything is stale. **`dev_bps` is the honest signal
there.** If that noise proves bad, the fix is to divide by how often the name
normally prints — `adv` off `target_stock` — which is not built here.

**Market orders are filtered out entirely.** One carries `price` 0, so there is
no order price to hold a print against — a `dev_bps` of −10000 for an order that
never had a price is a lie, and it would sort to the top of every run. This is
a limit-order report.

The filter is `0<price` on the OMS side, applied *after* the collapse to one row
per `id_work`, so it is the order's current price that decides. `0<` drops a
null price too. `otype` still rides along in the output, so you can see which
kind of limit order each row is.

## Untested edge, worth checking on the first run

When **nothing is activated**, dataset 2 returns early and hands the widgets the
empty *order* table rather than an empty result carrying the columns they name.
An empty book may therefore show as a column error rather than as no rows. This
is the same early return `limit_up_down_v2.q` ships, kept rather than changed
because there is no kdb here to prove a replacement. If it does misbehave on a
quiet morning, the fix is to drop the `if[0=count w; :w]` line in the
`stale_check` block and let the empty table flow through the joins.

## Which server answers what

| Period selected | Where the rows come from |
| --- | --- |
| Real-time | the RDB, today. Nothing else is asked. |
| A range **not** reaching today | the HDB. Nothing else is asked. |
| A range that **includes** today | the HDB for the range **plus** the RDB for today, unioned — unless the HDB already holds today, in which case the HDB answers alone. |

KdbMonitor sends a dataset to one server, so on a historical period the query
lands on the HDB and reaches back to the RDB itself through
`{{conn:ENV:realtime}}`. The safeguard is `hasToday`: if the HDB has already
been written down for today, stitching would count it twice, so the RDB is
never opened. Each dataset asks that question of **its own** server — the order
HDB and the quote HDB are written down on their own schedules.

Two things are specific to this dashboard:

- The quote RDB has **no `date` column**, so its half gets `update date:.z.D`
  and the as-of becomes `aj[`date`sym`time; …]` — an exact match on date and
  sym, as-of on time. A historical range therefore cannot date an order against
  another day's print.
- **`now_*` on a past date means that day's last print**, not this second. It
  is measured to the session end read off the other names in the frame. Only in
  real time does `now_age_ms` mean "as of right now".

## Why only two datasets

The other dashboards here use three because they have to go back to the OMS for
the blotter. Dataset 1 already carries every order field, so dataset 2 does both
`qatt` lookups and returns the finished table. The whole order table crosses to
the quote server because an `aj` needs both sides local.

Editing the q means editing the `.q` and re-running the builder. Editing the
JSON by hand works once and is lost the next time anyone regenerates it.
