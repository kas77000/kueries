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
Region      Orders   Ordered (USD)   Executed (USD)   Completion   Pinned %   Short, fav.   Short, adv.
Japan           27                   313.6k                    225.6k        71.9%            13            13
China           22                     8.4m                      4.6m        55.0%            11            11
...
Total          105                    23.3m                      8.5m        36.6%            52            52
```

### Notional, and why completion is in shares

`size` is a **share count**, so putting an order in USD needs a price — and the
**unfilled** part of an order never traded at one. The ladder is
[`short_sell_report`](../short_sell_report/README.md)'s, best first:

| | |
|---|---|
| **`limit_price`** | a limit order is worth what the client said it was worth |
| **the quote** | a market order has no limit, so it is valued at the side we would actually have traded — the **bid** for a sell, the **ask** for a buy — as at the moment the **first child was sent** |
| **the close** | `adjclose`, else `orgclose`. The fallback for an order that never produced a child at all — on *this* page not a rare branch, because an order that sent nothing is the thing the report exists to find |

**Executed is not priced that way at all.** It is `make × the child's own
avg_fill_price` — what those shares really cost.

So **the two notional columns are not a ratio, and nothing on the page divides
them.** Ordered is *theoretical* — the whole quantity at a price the unfilled
part never traded at. Executed is *realised*. Executed can exceed ordered, and
when it does that is a true fact about where the price went — a sell that
filled above the bid it was valued at — not a completion over 100%.

**Completion is `executed / order_qty`, in shares.** How much of the order got
done is a quantity question, so it is answered in quantity, and it cannot
exceed 100%. This is the same defect, and the same fix, as
[`short_sell_report`](../short_sell_report/README.md#why-completion-is-in-shares) —
see that section for the full argument, including why the bias only ever ran
one way on a page of sells.

Everything is multiplied by `target_stock.fxlast`, local → USD.

**An order with no price or no fx contributes nothing, and is counted.** The run
says so, and the region CSV carries `unpriced_orders`:

```
1 of 105 orders could not be valued - no price or no fxlast - and
contribute NOTHING to the notional columns
```

A notional that quietly omits a market is worse than one that admits a gap.

`fmt_usd` prints the unit the number is actually in — `23.3m`, `765.5k`, `442` —
because `23,263,496` is a number nobody reads. **Zero prints as a dash**: it
means the orders could not be valued, not that they were worth nothing. The CSV
keeps full precision, since that is the file somebody adds up.

**`Short, fav.` and `Short, adv.`** count orders that **came up short** — did
not fully execute — split by which side of the band they were on:

| | |
|---|---|
| **Short, fav.** | the band was on the side we could trade into: **selling into a limit up**, or **buying into a limit down**. There was a queue resting at the band and we were the other side of it, and we still did not finish. This is the column to read first, and it is red when it is not zero |
| **Short, adv.** | the same on the other side. **Not an excuse** — a market order is marketable into a band either way, which is why it is on the page rather than filtered out |

Neither counts an order the book could not call: a `limit_dir` of `unknown` or
`mixed` is in **neither** column, because an order nobody can classify is not
an order that was on the wrong side. The two therefore need not add up to the
incomplete orders in the row, and that gap is the size of what the close could
not settle.

*Short* is the trading sense — the order came up short of what it asked for —
and both count **orders**, not quantity. "Came up short" is deliberate: an
order that finished is not in either column however unfavourable the band was.

**Completion is quantity weighted** — summed executed over summed order
quantity, both for a row and for the total. Not a mean of the rows: a region
with one small order would otherwise pull the headline as hard as one with
four hundred.

A region with no order shows an **em dash, not 0%**. Nothing to measure against
is a different statement from nothing done.

### Page 2 onwards — every order at a limit

Behind the summary, one line per order, **28 to a page**, sorted by quantity
missed with the ones we could have traded into first:

```
Region     Symbol    Target id  Side  Type    Ordered (USD)  Executed (USD)  Completion  Limit    Limit window   Mins  Splits
Indonesia  1094.IJ          94  buy   limit               442             128       29.0%  down *   11:04–11:43      39       2
Taiwan     1105.TT         105  sell  limit              7.5m               —        0.0%  up *     11:00–12:00      60       0
```

Chosen for someone deciding whether a line is a problem — what the order was,
how much of it got done, what the book was doing, and how long the two
overlapped. Everything else is in `--raw`.

| column | what it means |
|---|---|
| `Target id` | the row to look up when the line needs checking. Printed with **no thousands separator** — it is an id, and `1,105,432` is not what anyone types into a query |
| `Ordered (USD)`, `Executed (USD)` | the same notional as the summary, per order. A dash means the order could not be valued |
| `Type` | `market` or `limit` — a market order was marketable whichever way the band went |
| `Limit` | the direction, with a **star** when that was the side we could trade into. One column rather than two: the direction alone does not say whether it was ours to take without the side beside it, and a reader should not have to do that join by eye on every row |
| `Limit window` | first period start to last period end. Not one continuous period when the order met several |
| `Mins` | how long the order and the limit actually **overlapped** — not how long the stock was pinned |
| `Splits` | how many children the order sent. **Zero is red**: it never tried |

`Completion` is red where the order came up short *and* the band was
favourable — the same test as `Short, fav.` on the summary, so a count there
and the lines under it cannot disagree.

Nothing is truncated: every order in scope is on one of these pages, and the
run logs how many pages it took.

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
OURS ────────────────────────────────────────────────────────────────────────
date,region,sym,side,otype,id_server,id_target,tag_9604,basket,order_qty,
executed,completion_pct,t_gen,order_start,order_end,splits,split_first_gen,
split_last_off,
THE MARKET'S ────────────────────────────────────────────────────────────────
limit_periods,limit_first_start,limit_last_end,limit_mins,limit_price,
ref_close,pct_from_close,limit_dir,limit_noask,limit_nobid,limit_net,
limit_locked,
BOTH ────────────────────────────────────────────────────────────────────────
overlap_mins
```

**Three blocks, left to right.** Everything **we** did comes first — the order
and the children it sent. Then what the **book** was doing. Then, last, the one
column that needed both. Going along a line you meet our side before the
market's, and a column's block tells you which side to go and check when the
number looks wrong. Checks hold the layout, so a new column cannot quietly land
in the wrong block.

#### Ours — from `target`

| column | source | what it means |
|---|---|---|
| `date` | `target.date` | the session. Empty on a realtime run, which has only the one |
| `region` | derived from `sym` | one of the eight, by suffix — never from `target_stock.country` |
| `sym` | `target.sym` | the instrument, as the feed spells it |
| `side` | `target.side` | `buy` or `sell` |
| `otype` | `target.otype` | the order type — see the table below |
| `id_server`, `id_target` | `target` | **the row this line is.** Look it up with these two and the date |
| `tag_9604` | FIX tag 9604 in `target.fixmsg` | the client's own order id. Empty when the client sent none. Two lines sharing one are the same order re-sent — not chained here, see below |
| `basket` | `target.basket` | what the order was sent as part of. Empty when it was sent on its own — a basket that pinned on one name will show up as several lines sharing this |
| `order_qty` | `target.size` | what this send asked for, in **shares** |
| `executed` | Σ `workorder.make` | what its children did, in **shares**, whatever state they ended in |
| `completion_pct` | `executed / order_qty` | the **share** completion. Empty when there was no quantity to measure against — which is not the same as 0% |
| `ordered_usd` | `size × price × fxlast` | the notional, full precision. Empty when the order could not be valued |
| `executed_usd` | Σ `make × avg_fill_price × fxlast` | what the fills really paid |
| `notional_completion_pct` | `executed_usd / ordered_usd` | the one the page shows. Not the same number as `completion_pct` |
| `price` | the ladder's answer | what `ordered_usd` was valued at, in local currency |
| `price_source` | `limit` / `quote` / `close` / `none` | which rung of the ladder it came from — the column to check when a notional looks wrong |
| `fxlast` | `target_stock.fxlast` | local → USD |
| `t_gen` | `target.t_gen` | when the **order** was created. Not `split_first_gen`, which is the first child's |
| `order_start`, `order_end` | `target.t_start`, `t_end` | the order's live window, `HH:MM:SS`, ready to type back into a query. **Empty end = still working** |

#### Ours, continued — the children, from `workorder`

| column | source | what it means |
|---|---|---|
| `splits` | count of `workorder` rows | how many children the engine made under this target. **Every row counts**, whatever became of it — a rejected split is still one the engine made, and leaving those out would say the order tried less than it did. `0` is the interesting value |
| `split_first_gen` | min `workorder.t_gen` | when the **first** child was created |
| `split_last_off` | max `workorder.t_off_market` | when the **last** child left the book. Empty on a split that never reached the market — a missing time cannot move the bound, or the order would be dated to midnight |

#### The market's — from `qatt`, and `target_stock` for the close

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

Read `split_first_gen` and `split_last_off` against `limit_first_start` and
`limit_last_end` — the last of our block against the first of the market's —
and the question this is being built towards is on one line: **was anything of
ours on the book while the stock was pinned?**

```
sym       otype   limit window        splits  first gen  last off   executed
1001.JP   limit   11:01:00-11:27:00        2   11:01:00  11:11:00    129,240
1104.TT   limit   11:00:00-12:00:00        0          -         -          0
```

The second line is the shape the report exists to find: an hour at a limit, and
not one child sent. `splits = 0` is where to start reading.

**`t_gen` is creation, not sending.** `t_transmit` and `t_oes_send` say when we
sent it and `t_on_market` when it arrived — three different questions, and only
creation is on the line so far.

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

A limit period has **no minimum length**. What counts is the share of an
**order's own life** its stock spent at a limit — **`--min-pinned-pct`, default
25** — with the periods **unioned** first.

There used to be a 20-minute minimum on each period, and it made this report
print zero orders every day on a book full of them. Three things compound, and
each shortens the *measured* length of a period:

- **One normal tick ends a run** (see the example above), so a stock flickering
  at its band produces many short runs rather than one long one.
- **Every window is a floor**, because a pinned stock stops quoting.
- **The minimum was applied to each run separately.**

So the harder a stock was pinned, the shorter its runs and the more certainly
they were discarded — the filter was biased against exactly the orders this
report exists to find. A stock at its limit for 48 of 60 minutes, as twelve
four-minute runs, kept **zero** periods.

Unioning the runs first is what makes the tick-splitting irrelevant: those
twelve runs collapse into one 48-minute span, and against an order live for
that hour the answer is 80%.

Noise still filters itself. A run with no width at all is dropped, and a
two-tick blip is seconds against an order's hours — 0.6% of a 5½-hour order,
nowhere near the gate. That is the distinction the old minimum could not make:
it could not tell a blip from a flicker, because it only ever looked at one run
at a time.

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

`target` rows on the eight regions, **every side**. No filter on *which* state
an order reached — this is a historical report, so there is no "currently
activated" to ask about.

### Orders that were over before they began

An order **cancelled before its own `t_start`** never worked. Nothing of it was
ever going to reach the book, so counting it as an order that sat through a
limit is counting a false positive. Those are **dropped**, and the run says how
many:

```
14 of 312 orders were over before their own t_start - cancelled before
they worked - and are out
```

The test is the **last `target_state` row against `t_start`**: if the order's
life ended before it began, it did not begin. Last by *time*, decided in Python
rather than with a `by` in the query — grouping it down to one row per order in
q would hand back an answer with no way to see what it stood for, and this is
the evidence a whole order gets dropped on.

**Nothing is dropped on missing evidence.** No state row, a state row with no
time, or an order with no `t_start` all mean the question cannot be answered,
and the order stays. An order silently removed for want of a row in another
table is worse than one that should not be there: the first is invisible, the
second shows up in `--raw`. A state row exactly *at* `t_start` is not before it.

### The overlap test

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

## Email

Configured in the `EMAIL` block at the top of the script, or in
`local_settings.py` — **not on the command line**. Who gets this report is part
of what the report *is*, not of one run of it: a distribution list that lives in
whatever someone last typed is a list that quietly loses people.

**`EMAIL_TO` empty means do not send.** That is the whole switch; there is no
separate enable flag to leave in the wrong position. A `EMAIL_BCC` alone still
counts as configured — forgetting that is how a report goes quiet for the people
who only ever got it blind.

```python
EMAIL_TO = ["desk@example.com"]
EMAIL_FROM = "algo-reports@example.com"
SMTP_HOST = "mail.example.com"
EMAIL_DRY_RUN = True     # build it, say who it would go to, open no socket
```

The report is the **attachment** — one PDF, however many pages the listing ran
to. The body is just the sign-off:

```
Best Regards,

Khalife
```

No HTML part, no tables in the body, no page inlined as an image. A body that
restates the numbers is a second copy to keep in step and one more thing to
render wrong in somebody's client — checks assert all of that, including that
the body carries no digits at all.

SMTP is **host, port and timeout**: no credentials and no STARTTLS. These go
through an internal relay that takes mail from the host they run on, so there is
nothing to authenticate with — and an auth path nobody exercises is broken by
the time somebody needs it. Same shape as
[`short_sell_report`](../short_sell_report/README.md), which shares
[`scripts/lib/mailer.py`](../lib/README.md).

`--no-email` writes the report and sends nothing, whatever `EMAIL_TO` says.

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
EMAIL_TO = ["desk@example.com"]
EMAIL_FROM = "algo-reports@example.com"
SMTP_HOST = "mail.example.com"
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
162 checks, no kdb, no pykx, no q licence. `--demo` renders the sample above off
synthetic data.

---

## Not here yet, on purpose

- **Chaining** replaced orders back into one order (step 2).
- **The marketable window**, and whether a split was actually on the book during
  it — the question this is being built towards.
- **Rejections**, and the findings table.
- **A price sanity check that actually filters.** `pct_from_close` is now
  *reported* on every raw line, but nothing is dropped on it — so a thin book,
  a halt or an auction imbalance still reads as a limit period.
  `limit_up_down.q` calls this the sanity check: a genuine limit sits AT the
  band, so something locked at +0.1% is a locked market and not a limit. Making
  it a threshold needs per-market band reference data, which that script's own
  comment says to verify before trusting. Read the column first and see what
  the distribution looks like.

`luld_report` is the older report and is untouched by this one.
