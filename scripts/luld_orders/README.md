# luld_orders

**Orders at a Limit** — of the orders whose stock was limit up or limit down
while the order was live, how much did we ask for and how much got done, by
region.

```bash
python scripts/luld_orders/luld_orders.py                       # today, realtime
python scripts/luld_orders/luld_orders.py --date 2026-07-01     # one session
python scripts/luld_orders/luld_orders.py --monthly 2026-07     # a month
python scripts/luld_orders/luld_orders.py --monthly 2026-07 --csv --raw

python scripts/luld_orders/luld_orders.py --self-test           # no kdb needed
python scripts/luld_orders/luld_orders.py --demo                # sample page
```

This is **step 1 of a rebuild**. What is deliberately not here yet is at the
bottom of this file.

---

## The page

One row per region, always all eight, plus a total.

```
Region      Orders   Order qty   Executed   Completion
Japan           26   3,504,500   2,523,240        72.0%
Korea           19   2,278,000     933,977        41.0%
...
Total          103  12,542,000   7,075,504        56.4%
```

**Completion is quantity weighted** — summed executed over summed order
quantity, both for a row and for the total. Not a mean of the rows: a region
with one small order would otherwise pull the headline as hard as one with
four hundred.

A region with no order shows an **em dash, not 0%**. Nothing to measure against
is a different statement from nothing done.

Written to `--out-dir` as a PDF and a PNG. `--csv` writes the same rows beside
it as `luld_orders_<stamp>.csv` — built from **the same `Row` objects the page
draws**, so the two cannot disagree. A CSV re-derived from the source would be
a second answer to keep in step. `--raw` writes the order-level lines those
rows are made of; see below.

---

## `--raw` — the lines the table is made of

`--raw` writes `<stem>_raw.csv`: **one line per order in scope**, with the limit
period it was live through. This is the file to open when a regional figure
looks wrong.

```
date,region,sym,side,id_server,id_target,tag_9604,order_qty,executed,
completion_pct,order_start,order_end,limit_periods,limit_first_start,
limit_last_end,limit_mins,limit_price,overlap_mins
```

| column | what it is |
|---|---|
| `id_target`, `id_server` | the target row this line is, so it can be looked up |
| `tag_9604` | the client's own id — empty when the client sent none |
| `order_start`, `order_end` | the order's live window, `HH:MM:SS`, ready to type back into a query. Empty end = still working |
| `limit_periods` | how many qualifying periods the order was live through |
| `limit_first_start`, `limit_last_end` | the span those periods cover |
| `limit_mins` | how long the periods lasted, summed |
| `overlap_mins` | how long the order and the limit actually **coexisted** — each period clipped to the order's own window |

`limit_mins` and `overlap_mins` differ whenever the order arrived late or
finished early: a 60-minute limit an order only saw the second half of is
`60.0` and `30.0`. `overlap_mins` is the one that matters — it is the first
piece of the marketable window this is being built towards.

**One line per order, not per limit period.** An order live through three
periods is still one line, because that is the unit the page counts: summing
`order_qty` and `executed` over this file reproduces the table exactly, region
by region, and a check holds it to that. A line per period would read more raw
and add up to more than the report.

---

## Both sides count

A limit up is favourable to a seller and a limit down to a buyer, but an
unfavourable limit is **not** an excuse: a market order can be marketable into
one. So side is not a filter, nothing is dropped for being on the wrong side of
the band, and the report never has to decide which side a period was at.

That decision is what the old `luld_report` spent its `noask`/`nobid`
comparison on — and a period whose side could not be told was **dropped**.
Here there is no such branch and no such loss.

---

## What a limit period is

A stock at its limit stops quoting two sided: it **locks** (`qbid=qask`) or goes
**one sided** (one side empty, the other carrying the band). That is the whole
test, and it is
[`queries/limit_up_down/limit_up_down.q`](../../queries/limit_up_down/limit_up_down.q)'s
expression verbatim:

```q
lim: ((qbid=qask)&0<qbid) | ((0=qbid)&0<qask) | ((0=qask)&0<qbid)
```

Contiguous runs of it are one period each, via `sums differ lim by sym`. The
**boundaries are the normal ticks either side of a run**, never a gap
threshold — two limit periods with two-sided quoting between them are genuinely
two periods, and a threshold would have to guess which.

```
 09:31  bid 1200  ask 1201   normal
 09:33  bid 1250  ask    0   LIMIT  ┐
 ...                                │ one period, 09:33–11:04
 11:04  bid 1250  ask    0   LIMIT  ┘
 11:05  bid 1249  ask 1250   normal   <- this tick ends it
 11:06  bid 1250  ask    0   LIMIT  ┐ a SECOND period, not the same one
```

A run counts only if it lasted at least **`--min-mins` (default 20)** — that is
`limit_up_down.q`'s `lookback` doing the job it does there, keeping a two-tick
blip from reading as a limit period.

**Every window is a floor.** A pinned stock often stops quoting altogether, so
a period ends at the last tick that *proved* it and never later. Under-reporting
is the chosen direction throughout: a window this cannot prove is not one it
claims.

### Not a snapshot

`limit_up_down.q` takes `now:.z.T` and asks *"is this stock in limit right now,
with nothing normal in the last N minutes"*. That is the right question for a
live monitor and the wrong one for a report of a past day — it would only ever
describe the instant it ran, and every limit period that resolved before then
would be invisible, along with every order that traded through one.

So the rule's substance is kept — locked-or-one-sided, anchored on the last
normal quote, with a minimum duration — and applied to **every period in the
session** instead of only the one in force at `now`.

---

## Which orders are in scope

`target` rows on the eight regions, **every side**, no `target_state` join —
this is a historical report, so there is no "currently activated" to filter on.

An order is in scope when its live window `[t_start, t_end]` **overlaps any
qualifying period** on its own `(date, sym)`. An order that finished before its
stock went to the limit is not a LULD order, however dramatic the stock's
afternoon was. A missing `t_start` or `t_end` cannot rule an overlap out — the
order was live from the open, or is live still.

**Order qty** is `target.size`. **Executed** is the sum of `make` over that
target's `workorder` children — what each child executed, whatever state it
ended in.

A workorder whose parent is not in scope is **dropped, not counted**. That is
not a detail: counting quantity off one grouping and fills off another is
exactly what made the old report print Korea 417 asked against 675 executed.
A check holds the two invariants — no region executes what it had no order for,
and no row completes more than it asked for.

### The eight regions

`JP .JP` · `KR .KS` · `MY .MK` · `TH .TB` · `ID .IJ` · `CN .CH .C1 .C2` ·
`TW .TT` · `IN .IN .IS`

Hong Kong, Australia, Singapore and New Zealand are **out**: no daily price
limit, so there is no limit to be up or down against. It is a whitelist, so a
new venue is out until someone puts it in. The `*.JP`-style patterns q filters
on are **built from the same table** Python maps back with, so the two cannot
drift apart.

---

## Not chained — and it says so

The engine writes a **new `id_target`** every time an order is re-sent, so three
sends of 27m read here as **three orders and 81m asked**. That is what "no
grouping" means at this step.

The run prints the size of it, and the page carries the same line:

```
NOT CHAINED: 46 of 312 targets share a 9604 with another, 4 carry none.
A re-sent order is counted once per send.
```

Nothing is done about it yet. Chaining is step 2.

---

## Settings

Servers live in the block at the top of the script, and can be overridden from a
**`local_settings.py` beside it**, which git ignores:

```python
# scripts/luld_orders/local_settings.py
ORDER_SERVER_RT = "prod-oms-1:5012"
ORDER_SERVER_HIST = "prod-oms-hist:5010"
QATT_SERVER_RT = "prod-qatt-1:5013"
QATT_SERVER_HIST = "prod-qatt-hist:5011"
MIN_LIMIT_MINS = 20.0
```

A name the script does not define is an error that stops the run, not a new
setting. See [`scripts/lib`](../lib/README.md).

The order server and `qatt` stamp their times on the **same clock**, so an
order's window is compared against a limit period with no timezone or DST
conversion anywhere. That is relied on and is not a gap.

---

## Modes

| | servers | dates |
|---|---|---|
| *(no flags)* | realtime | the session in progress |
| `--date 2026-07-01` | historical | that one session |
| `--monthly 2026-07` | historical | every weekday of the month |

Nothing is grouped in q: a `target` row is one send and a `workorder` row is one
child. Every sum and count happens in Python, where `--self-test` can prove it —
72 checks, no kdb, no pykx, no q licence. `--demo` renders the sample above off
synthetic data.

---

## Not here yet, on purpose

- **Chaining** replaced orders back into one order (step 2).
- **The marketable window**, and whether a split was actually on the book during
  it — the question this is being built towards.
- **Rejections**, email, and the findings table.
- **A price sanity check.** Every locked or one-sided run counts, with no test
  that the price sits at a real band, so a thin book, a halt or an auction
  imbalance reads as a limit period. `limit_up_down.q` guards this with
  `pctFromClose` against the previous close; there is no equivalent here.

`luld_report` is the older report and is untouched by this one.
