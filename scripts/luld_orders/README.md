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
date,region,sym,side,otype,id_server,id_target,tag_9604,order_qty,executed,
completion_pct,order_start,order_end,limit_periods,limit_first_start,
limit_last_end,limit_mins,limit_price,ref_close,pct_from_close,limit_dir,
limit_noask,limit_nobid,limit_net,limit_locked,overlap_mins
```

Every column, in order. **The order** is what the first block describes, **the
limit** the second, and **the two together** the last — which is where the
question this is being built towards lives.

#### The order — from `target`

| column | source | what it means |
|---|---|---|
| `date` | `target.date` | the session. Empty on a realtime run, which has only the one |
| `region` | derived from `sym` | one of the eight, by suffix — never from `target_stock.country` |
| `sym` | `target.sym` | the instrument, as the feed spells it |
| `side` | `target.side` | `buy` or `sell` |
| `otype` | `target.otype` | the order type — see the table below |
| `id_server`, `id_target` | `target` | **the row this line is.** Look it up with these two and the date |
| `tag_9604` | FIX tag 9604 in `target.fixmsg` | the client's own order id. Empty when the client sent none. Two lines sharing one are the same order re-sent — not chained here, see below |
| `order_qty` | `target.size` | what this send asked for |
| `executed` | Σ `workorder.make` | what its children did, whatever state they ended in |
| `completion_pct` | `executed / order_qty` | empty when there was no quantity to measure against — which is not the same as 0% |
| `order_start`, `order_end` | `target.t_start`, `t_end` | the order's live window, `HH:MM:SS`, ready to type back into a query. **Empty end = still working** |

#### The limit — from `qatt`, and `target_stock` for the close

| column | source | what it means |
|---|---|---|
| `limit_periods` | derived | how many qualifying periods this order was live through. Always ≥ 1, or the line would not exist |
| `limit_first_start`, `limit_last_end` | `qatt.time` | the span those periods cover, first start to last end. **Not** one continuous period when `limit_periods > 1` |
| `limit_mins` | derived | how long the periods lasted, summed. The stock's afternoon, not the order's |
| `limit_price` | `qatt` | the band: the bid, or the ask when there is no bid — q's `?[0=qbid;qask;qbid]` |
| `ref_close` | `target_stock.adjclose`, else `orgclose` | the previous close the band is measured from. Empty when the stock has no close on file |
| `pct_from_close` | `limit_price` vs `ref_close` | how far the band sat from it. **Reported, never filtered on** — see *Not here yet* |
| `limit_dir` | derived | `up`, `down`, `unknown` or `mixed` — see below |
| `limit_noask` | Σ ticks with no ask | evidence for `limit_dir`: at limit up nobody will offer |
| `limit_nobid` | Σ ticks with no bid | evidence for `limit_dir`: at limit down nobody bids |
| `limit_net` | `qatt.netChange` | last resort evidence. Comes off the last *traded* price, so it is 0 or empty on exactly these stocks |
| `limit_locked` | derived | `yes` when no side ever went missing — bid = ask throughout. These are the ones `ref_close` has to call |

#### The two together

| column | what it means |
|---|---|
| `overlap_mins` | how long the order and the limit **actually coexisted** — each period clipped to the order's own window, summed |

`limit_mins` is the stock's; `overlap_mins` is ours. They differ whenever the
order arrived late or finished early: a 60-minute limit an order only saw the
second half of is `60.0` and `30.0`. **`overlap_mins` is the one that matters**
— it is the first piece of the marketable window this is being built towards.

### `otype`

| value | what it means | why it is on the line |
|---|---|---|
| `market` | no price on it — takes whatever the book offers | **marketable whichever way the band went.** A market order sitting through a limit with nothing executed is a real question, not a favourable/unfavourable one |
| `limit` | priced — fills only at that price or better | whether it was marketable depends on its price against the band, which this file does not carry yet |
| *(empty)* | the feed sent none | not guessed at |

Those are the two this feed is known to send, from
[`luld_shortsell_check`](../luld_shortsell_check/README.md), which treats an
unpriced split as a market order. **The value is passed through as it arrives**,
so anything else the feed carries appears here as itself rather than being
folded into one of these.

Nothing is filtered on `otype` — a market order and a limit order are both in
scope, and a check holds that.

### `limit_dir`

**A stock at its limit does not always go one sided.** It can **lock** — bid =
ask, both present — and then no side went missing and the book cannot answer on
its own. That is why there are three questions rather than one, in the order
[`limit_up_down.q`](../../queries/limit_up_down/limit_up_down.q) asks them:

1. **Which side went away.** At limit up nobody will offer, so the ask goes
   missing; at limit down nobody bids. Counted across the **whole run** rather
   than read off one tick, because a pinned book flickers. `limit_noask` and
   `limit_nobid` are those counts.
2. **Where the price sits against the previous close.** This is what settles a
   locked book: a band above the close is limit up, below it is down. `ref_close`
   and `pct_from_close` are on the line, so the call can be checked rather than
   taken.
3. **`netChange`, and only then.** It comes off the last *traded* price, so it
   is 0 or null on exactly the stocks being hunted — a last resort, and the q
   script says the same.

Each outranks the ones below it: a book that went one sided is called from that
whatever the close says, and the close outranks `netChange`.

- **`unknown`** is what is left when all three decline — a locked stock with no
  close on file, or a band sitting exactly at it. A real answer, and better
  than a guess.
- **`mixed`** means the order was live through periods that disagreed — limit
  down in the morning, limit up in the afternoon. Picking the first would
  quietly claim it was only that one.

On the sample, 35 of 104 lines are locked; the close calls all but one of them,
and that one has no close on file.

**Direction is reported, never filtered on.** Nothing is dropped for being
unfavourable or for being unknown — an unfavourable limit can still be
marketable, and a limit the book cannot call is still a limit. This is the one
place the old `luld_report` lost data: it needed the side to decide whether a
finding was favourable, so a period whose side was a tie was **thrown away**.

Whether a limit was favourable to a given order is `side` against `limit_dir`
— selling into `up`, or buying into `down` — and is left to whoever reads the
file, since it is the interpretation rather than the observation.

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
Here the same evidence is still read, and more besides, but only to REPORT the
direction in `--raw`; it never decides whether an order is in scope, so nothing
is lost.

---

## What a limit period is

A stock at its limit stops quoting two sided: it **locks** (`qbid=qask`, both
present — common, and not the exception) or goes **one sided** (one side empty,
the other carrying the band). That is the whole test, and it is
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
105 checks, no kdb, no pykx, no q licence. `--demo` renders the sample above off
synthetic data.

---

## Not here yet, on purpose

- **Chaining** replaced orders back into one order (step 2).
- **The marketable window**, and whether a split was actually on the book during
  it — the question this is being built towards.
- **Rejections**, email, and the findings table.
- **A price sanity check that actually filters.** `pct_from_close` is now
  *reported* on every raw line, but nothing is dropped on it — so a thin book,
  a halt or an auction imbalance still reads as a limit period.
  `limit_up_down.q` calls this the sanity check: a genuine limit sits AT the
  band, so something locked at +0.1% is a locked market and not a limit. Making
  it a threshold needs per-market band reference data, which that script's own
  comment says to verify before trusting. Read the column first and see what
  the distribution looks like.

`luld_report` is the older report and is untouched by this one.
