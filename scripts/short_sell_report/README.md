# short_sell_report

Every short sell order of the session, its completion and its rejections,
summarised by market, as a one page PDF and the same page as a PNG — optionally
mailed to a list of people.

```
python scripts/short_sell_report/short_sell_report.py
python scripts/short_sell_report/short_sell_report.py --monthly 2026-07
python scripts/short_sell_report/short_sell_report.py --demo       # preview, no kdb
python scripts/short_sell_report/short_sell_report.py --compare    # old counting beside the new
python scripts/short_sell_report/short_sell_report.py --chains     # what got chained
python scripts/short_sell_report/short_sell_report.py --orders     # every order, with its rejections
python scripts/short_sell_report/short_sell_report.py --probe      # meta for every table it reads
python scripts/short_sell_report/short_sell_report.py --no-tag     # what carries no 9604
python scripts/short_sell_report/short_sell_report.py --reject-reasons  # the rejection texts
python scripts/short_sell_report/short_sell_report.py --self-test
```

The default run is a **real-time snapshot** of the session in progress.
`--monthly` reads the historical server instead and adds two per day charts.

---

## The page

```
Short-Sell Order Report
By market · 2026-07-24 18:37
────────────────────────────────────────────────────────
732                 58.7%                394
Short-sell orders   Overall completion   Rejections

+----------+--------+----------------+----------------+------------+------------+
| Market   | Orders | Notional       | Notional       | Completion | Rejections |
|          |        | Ordered (USD)  | Executed (USD) |            |            |
+----------+--------+----------------+----------------+------------+------------+
| Hong Kong|    109 |        79.2m   |        42.1m   |      53.1% |        239 |
| Japan    |    541 |        36.4m   |        30.5m   |      83.7% |          3 |
| Korea    |     82 |         8.1m   |         3.2m   |      39.3% |        152 |
| Malaysia |      0 |           —    |           —    |          — |          0 |
| Taiwan   |     23 |         7.2m   |         4.6m   |      63.1% |         31 |
| Thailand |      1 |       270.0m   |           —    |       0.0% |          2 |
+----------+--------+----------------+----------------+------------+------------+

Completion by market            Rejections by market
   Japan ████████████ 84%          Hong Kong █████ ▓▓▓▓▓ 239
  Taiwan ████████ 63%                  Korea ███ ▓▓▓ 152
Hong Kong ███████ 53%                  Taiwan █ ▓ 31
    Korea █████ 39%                     Japan 3

                                █ short sell   ▓ other
```

The rejections chart is **stacked by why** — see *Why an order was rejected*.
Completion is a single measure and stays a plain bar; stacking it would be
stacking percentages, which do not add up to anything.

`--monthly` adds **Completion by day** and **Rejections by day** underneath, as
two full-width **vertical** charts, one column per trading day:

```
Completion by day
        54%           59%                     59%
  37%   ██   47%      ██                      ██
  ██    ██   ██   35% ██  42%             48% ██
  ██ 20%██   ██   ██  ██  ██  ...         ██  ██
  ─────────────────────────────────────────────────
   1  0  0   0    0   0   0                0   0
   0  7  7   7    7   7   7                7   7
   -  -  -   -    -   -   -                -   -
   ⋮  ⋮  ⋮   ⋮    ⋮   ⋮   ⋮                ⋮   ⋮      ← 2026-07-01, turned 90°
```

A month is a sequence, and a sequence reads left to right — the horizontal form
the five markets use would put time on the vertical axis, which is the wrong axis
for it. Chronological, never sorted by value.

Each column is labelled with the whole `YYYY-MM-DD`, turned on its side under the
baseline, rather than the day of month: these pages get read on their own months
later, where "17" is only a date if the subtitle is still in view.

Completion runs on a fixed 0–100% scale, so the columns mean the same thing from
one month's report to the next; rejections are a count with no natural ceiling,
so that chart scales to its own data.

Written to `--out-dir` (default `scripts/short_sell_report/out/`) as
`short_sell_report_2026-07-24.pdf` and `.png`; monthly is
`short_sell_report_2026-07.pdf`. `--out-dir` takes any path the machine can
reach, a network share included.

---


## What the numbers mean

| | |
|---|---|
| **Orders** | one per **order**, not one per `target` row. A rejected-and-replaced order writes a **new `id_target`** each time it is re-sent, so counting target rows counts one economic order several times. They are chained back together on **FIX tag 9604** — see below. |
| **Notional Ordered (USD)** | the chain's quantity × a price × `fxlast`. The price ladder, best first: **`limit_price`** for a limit order; else **the quote on our side** — the bid for a sell, the ask for a buy, as at the moment the first child was sent; else the **reference close** (`adjclose`, then `orgclose`) for an order that never produced a child. The quantity is what the chain asked for, not `sum size`: three sends of 27m that never traded asked for **27m**, not 81m. |
| **Notional Executed (USD)** | Σ(`make` × the child's own `avg_fill_price`) × `fxlast`, across **every** attempt. What those shares really cost. `make` counts whatever state the child ended in — a cancelled child that part-filled still contributes what it filled. |
| **Completion** | per market, Notional Executed / Notional Ordered. **Not** the share completion — it cannot be, because the two sides are priced differently: the unfilled part at a limit or a quote, the filled part at what it actually paid. |
| **Overall completion** | the same ratio over every market at once — summed USD executed over summed USD ordered. |
| **Rejections** | **`REJECTTOOMANY` alerts** on the `alerts` table, across **all** attempts of the chain, split by the four categories below. One order raises several, which is why Hong Kong can show 109 orders and 239 rejections. |

**Rejections are alerts, not workorder states.** `workorder.state` says a child
came back refused; a `REJECTTOOMANY` alert is the engine saying *this order has
been refused too often*, which is the thing worth putting on a page. The two
counts are different and the alert is the one the column reports.

`workorder` also carries `invalid_ack` and `fail_ack`. Neither those nor the
`` `rejected `` state feed this column: they are a different failure — a
malformed or unacknowledged send rather than a venue saying no — and folding
them in would inflate the one number on this page a compliance reader will
quote.

Nothing is joined or grouped in q. A target is one send and a workorder is a
child order; the chaining, the sums and the counts all happen in Python, where
`--self-test` can prove them. See *Three selects, not one lambda*.


## The chain key: FIX tag 9604

The client puts **its own order id in tag 9604** of `fixmsg`, and a
cancel-and-replace carries the **same id** — the client saying "this is still
that order". That is a fact, not an inference.

```
...;16589=108223;9604=104642494_SG_HK_PORTAL_LIV_20260819162013;17717=...
...;16589=108543;9604=104642494_SG_HK_PORTAL_LIV_20260819162013;17717=...
                      ^^^^^^^^^^ same id, so one order
```

Chained on **(date, tag 9604)**.

**`id_server` is deliberately not in the key** — a trader can move an order to
another order server mid-life, and the two halves are still one order. Keying on
the server would split them back apart. How often it happens is reported:

```
1 chain spans more than one order server - a trader moved the order.  Not an
error; keying on id_server would have split these back apart
```

A target whose 9604 is empty cannot be chained to anything, so it **stands
alone** — keyed on its own server *and* `id_target`, which keeps two unrelated
untagged orders apart (`id_target` is not unique across servers).

> An earlier version of this grouped on the `oes_oid` prefix. That was a
> convention; 9604 is a contract. The prefix version is gone.

### Reading the tag

**The separator is a semicolon** in this feed, as above. SOH and pipe are
accepted too, since a stored copy may be rewritten either way.

**A caret is not a separator**, though it looks like one — it is used *inside*
values throughout this feed:

```
9012=274=1^275=1                                   one field
1008649713=SILK_FLOW^TargetPart=30^SharedTempl^^   one field
```

Splitting on it would carve values into pieces. Nor is a space, for the same
reason.

The whole tag is compared after splitting, rather than searching for `"9604="` —
so `19604=`, `96040=` and a `9604=` appearing inside another field's *value* are
all correctly ignored.


## The two checks, run every time

Both of the checks you asked for are built in and print on every run — they are
not something to remember to look at.

### 1. Is tag 9604 populated for the universe we ask for?

```
chains: 924 targets -> 871 orders (43 chained, longest 3)
tag 9604 is populated on every target
```

or, when it is not:

```
61 of 924 targets (6.6%) carry no tag 9604 and stand alone, as counting targets
them: TH 38, JP 19, KR 4
```

Broken down **per market**, because "the client does not tag Thailand" is a
different problem from "the client tags nothing". A high number does not
invalidate the report — those orders are simply not chained — but it says how
much of it the tag is actually doing.

**`--no-tag` lists them**, largest first, because a percentage is not something
anyone can act on:

```
2 of 3 targets carry no tag 9604 (66.7%)
  Thailand 1, Japan 1

each stands alone and is counted exactly as counting targets it

  market     sym                id_target            size  algo      basket      oes_oid
  TH         SCB-R.TB                   1      27,000,000  vwap      B1          OID.1
  JP         7203.JP                    2       5,000,000  vwap      NIGHT       OID.2
```

The count also rides on the page footer, so a printed report discloses how much
of itself was never chained.

It is also the tripwire for a parse failure. If `fixmsg` uses a separator the
parser does not know, **every** target reads as untagged and the run says so in
the strongest terms it has:

```
WARNING: NOT ONE of 924 targets carries tag 9604. Either the client sends
         none, or fixmsg uses a separator fix_tag does not know - check one
         fixmsg by hand before believing any of this. Nothing has been chained.
```

### 2. Does one id ever cover two different orders?

A chain must agree on **sym, side, algo and basket**:

```
no chain mixes sym, side, algo or basket
```

or:

```
WARNING: 2 chains disagree on sym, algo - a 9604 is covering more than one
         order and these numbers are WRONG.  --chains lists them
```

**That must be zero.** None of those four fields is in the key **on purpose** —
putting them in would make the key right by construction and silent, and the
whole question is whether 9604 is trustworthy on its own.

`--chains` prints the offending chains attempt by attempt with each field, so
what got merged is visible at once:

```
2 chains cover more than one order - tag 9604 is NOT safe on its own here:

  9604=CLI-X  disagrees on sym, algo
      id_target 3    XJ.JP        sellshort  vwap    basket B1   size    100
      id_target 4    OTHER.JP     sellshort  twap    basket B1   size    100
```

Checks assert the two branches of each are **exclusive** — a run that printed
both the warning and the all-clear would be worse than one that printed neither,
and an if/else is exactly what a careless edit breaks.

### 3. What quantity did a chain ask for?

Executed is summed over **every** attempt, so this decides what those fills are
measured against — and the attempts are not all the same *kind* of thing:

| | |
|---|---|
| a **replacement** | supersedes the one before it. Three sends of 27m that never traded are **one 27m order**, not 81m — the whole reason the chaining exists. |
| a **top-up** | extra quantity on an order that already finished. Sizes 900, 1700, 2500 filling 3,600 in total asked for **5,100**, not 2,500. |

Both are real, they pull opposite ways, and no "take the Nth size" rule handles
both. `asked` reads it off the fills instead:

```
asked = (what every attempt filled) + (what the LAST one still had to do)
```

|  | sizes | fills | executed | asked |
|---|---|---|---|---|
| top-ups | 900, 1700, 2500 | 900, 1700, 1000 | 3,600 | **5,100** |
| reject ×3 | 27m, 27m, 27m | 0, 0, 0 | 0 | **27m** |
| remainder replace | 100, 70 | 30, 70 | 100 | **100** |

A superseded attempt contributes only what it *traded*, so a replacement is not
counted twice; a top-up contributes its whole size, because it filled it. A
single attempt is just its own size.

The others are kept for `--chain-qty` comparison: **`sum`** is right for top-ups
and puts a rejected-and-replaced order straight back to v1's number; **`max`**
is right for replacements and reads 144% on top-ups; **`first`** / **`last`**
each fail one of the two.

### The tripwires

`asked` cannot print over 100% — `qty − executed` *is* the last attempt's
residual, which is never negative. But that also means the chain-level check
**can never fire under it**, so it would validate nothing on its own. Two other
checks carry it:

**A target that filled more than its own size** — independent of `CHAIN_QTY`,
because the anomaly is per target, not per grouping:

```
WARNING: 1 individual target executed MORE than their own size. That is not a
         grouping question - a workorder is filling more than the target it
         belongs to:
      id_target 1270254699  6103.JP  size 100  executed 400  (400%)
```

**A chain that still over-fills gets un-chained.** Under `--chain-qty max` or
`sum` a chain can still exceed its quantity; whatever grouped it was wrong, so
it is exploded back into one order per target — exactly what v1 would have said
— rather than printing 144% on the page:

```
2 chains above have been UN-CHAINED into their 5 targets and counted the way
counting targets them, so the page does not read over 100%. Those are the ones to
look at with --chains
```

`--keep-over` leaves them chained if you would rather see the raw number.

### 4. Orders that never produced a workorder

Reported, **not removed**:

```
14 orders never produced a workorder (41,300,000 qty), and are IN the numbers above:
      11 died within 60s (33,100,000 qty) - pulled before we had a chance
       3 lived longer (8,200,000 qty) - WE sent nothing, longest 120 min on SCB-R.TB.
         These are a finding, not noise, and are why none of this is dropped
         automatically
```

"No workorder" is ambiguous between two **opposite** readings, and nothing in
the row says which:

| | |
|---|---|
| **the client pulled it** | cancelled seconds after arriving. We never had a chance, and its quantity arguably does not belong in a completion percentage at all. |
| **we sent nothing** | it sat there for hours and the algo generated nothing — very much our failure, and precisely what a completion report exists to surface. |

How **long it was live** is what separates them, so that is what gets measured
(`QUICK_CANCEL_SECS`, 60s). A *rejected* workorder counts as having produced
one: we sent something and the venue said no, which is the opposite of never
having sent anything.

Until that split shows which case dominates on real data, both stay in the
numbers and both are disclosed. Removing the quick ones is defensible once the
data supports it — removing the slow ones would delete a finding.

---


## Why an order was rejected

```
python scripts/short_sell_report/short_sell_report.py --reject-reasons --date 2026-08-19
```

The reason is on **`alerts`** — not on `workorder`, which has no text field,
and **not on `execution`**: a rejected order *has* no execution, and `ostat` and
`comment` there mean something else entirely.

`alerts` holds every alert an order raised, keyed the way targets are.
`alerttype` says what kind and `alertstr` carries the text. This keeps
**`REJECTTOOMANY`** alerts whose text is about **short selling**, and discards
the rest — a price-band reject is not this report's business.

```
8 alerts on the orders in scope: 6 are REJECTTOOMANY, 5 of those are about
short selling (over 5 orders)
  other alert types, not looked at: PRICEFAR 1, LATENCY 1

Hong Kong  -  3 alerts over 2 distinct texts
   alerts   share   orders    trig  reason
        2   66.7%        2       6  Short Sell rejected by venue: no locate available
        1   33.3%        1       1  Short-Selling restricted on this security
```

**The wording match is deliberately permissive**, because the text is written by
whoever wrote that venue's rejection and there is no house style to rely on:

```python
SHORT_SELL_RE = r"short[\s\-_]*(?:sell|sale)"
```

catches `short sell`, `short-sell`, `shortsell`, `short selling`,
`Short-Selling`, `SHORT_SELL`, `short sale`, `shortsales`. There is no leading
`` on purpose — an underscore is a word character, so `` would fail on
`X_SHORTSELL`.

**Texts group case-insensitively and the commonest spelling is shown**, so
`Short Sell` and `SHORT SELL` are one line. Punctuation is *not* folded:
`short-sell` against `short sell` may be two venues wording one rule, and
merging them would hide that rather than reveal it. Whitespace is squeezed and
the line truncated at 110 characters so two rejects differing only in an order
id land together. `--top N` changes how many per market.

**Two things the run tells you so you are not reading an absence as a fact:**
the alert types it never looked at, and — if `REJECTTOOMANY` alerts exist but
*none* matched the short-sell wording — a note to check `SHORT_SELL_RE` against
one of them by hand before concluding there were none.

### The four categories

Every `REJECTTOOMANY` alert on the page lands in exactly one bucket, tested in
this order — **first match wins**:

| | matches | |
|---|---|---|
| **short sell** | `SHORT_SELL_RE` above | the rule this report is about |
| **open** | `open` | refused around the opening auction |
| **close** | `close` | refused around the closing auction |
| **continuous** | everything else | the rest of the session |

Order matters and is the whole design. *"Short sell rejected in the OPEN
auction"* is a **short sell** reject that happened to be at the open, not an
open reject — the reason we could not trade is the rule, not the clock. So the
short-sell test runs first and takes it. Only alerts that are **not** about
short selling are then sorted by session.

`open` and `close` are matched as plain substrings, which is what makes
*"reopened"* read as **open** and *"undisclosed"* as **close**. Both are covered
by `--self-test` so the behaviour is on the record rather than a surprise; if
either shows up in real texts, tighten `OPEN_RE` / `CLOSE_RE` to a word
boundary. `--reject-reasons` is how you find out.

### What the chart shows: two, not four

The bar is stacked **short sell** against **other** — `open`, `close` and
`continuous` added together:

```python
CHART_CATEGORIES = ("short sell", "other")
```

Four segments on a bar that is nine pixels long for half the markets is four
things nobody can compare. The split that earns its ink on this page is the rule
the report is about against everything else.

**Only the drawing is folded.** All four are still counted, still summed in the
Rejections column, still in `--orders` and still in the CSV — the detail is a
command away rather than gone. The two segments partition the four exactly, so
the bar length is still the number in the column.

---

## Only the orders that could have traded

Most markets here cap how far a stock may move from the previous close in a
session. An order priced **beyond that band never had a chance** — it is not a
fill that got away, and counting it drags completion down while burying the
orders somebody could actually have done something about. Those orders are
**dropped**, and counted as they go:

```
14 orders excluded as not marketable - priced beyond the day's limit band,
so they could never have traded
    Korea              9   band +/-30% of the previous close
    Taiwan             5   band +/-10% of the previous close
      2330.TT      size     9,000,000  limit     130.0000  prev close     100.0000

109 orders could not be judged and are KEPT (Hong Kong: no daily limit)
```

The excluded count also goes in the page footer, and `--keep-unmarketable` puts
them back — the numbers on the page are then the numbers before this filter.

The rules live in [`scripts/lib/price_bands.py`](../lib/README.md), which is the
one place to correct them. The previous close comes from
`target_stock.adjclose`, else `orgclose`, which the report already fetches — so
this costs no extra table and no quote server.

**Three things it deliberately does:**

- **Judges the direction, not the distance.** These are sells, and a sell dies
  only **above** limit up. A sell priced *under* limit down is cheap, not off
  limit, and it trades. Treating both ends the same way would throw away real
  orders.
- **Keeps what it cannot judge.** Hong Kong caps nothing, and a name with no
  previous close cannot be assessed at all. Both are kept, both are reported.
  Dropping an order because we do not know the rule is inventing a rejection.
- **Judges the chain, not the send.** A chain survives if **any** attempt was
  marketable. A client who replaces an off-limit price with a sane one had an
  order that could trade, and that is one order.

A market order has no price to be wrong, so it is always marketable. Tokyo is a
**step table in yen**, not a percentage — a 1,234 yen name may move 300, so an
order at 1,600 is dead where a flat 30% rule would have waved it through.

Its children and its rejection alerts go with it, so a row on the page is always
internally consistent: the rejections counted are rejections of orders that are
also counted.

---

## The orders behind the page

```
python scripts/short_sell_report/short_sell_report.py --orders
python scripts/short_sell_report/short_sell_report.py --orders --top 20
python scripts/short_sell_report/short_sell_report.py --orders-csv out/orders.csv
python scripts/short_sell_report/short_sell_report.py --monthly 2026-07 --orders-csv out/july.csv
```

The page is six rows of arithmetic. `--orders` is how you take one of them
apart: **one line per order**, with the rejections that belong to it, so the
lines sum to the page.

```
4 orders - worst first, one line per ORDER, so the lines sum to the page
market     sym         targets       algo       ordered       exec   done    ordered USD       exec USD src    kids  rej  ss/op/cl/co
-------------------------------------------------------------------------------------------------------------------------------------
Thailand   PTT.TB      901+902+903   vwap    27,000,000  4,000,000  14.8%     27,000,000      4,000,000 limit     1    3  2/1/0/0
      Short Sell not permitted - no locate | Rejected before OPEN auction
Korea      005930.KS   45            vwap       197,500     80,975  41.0% 14,022,500,000  5,708,737,500 limit     1    1  0/0/1/0
      CLOSE auction would not accept
Japan      7203.JP     10            pov        215,000    150,000  69.8%    559,000,000    388,500,000 limit     1    0  0/0/0/0
Hong Kong  700.HK      11            close       49,000     30,000  61.2%     20,065,500     12,270,000 quote     1    0  0/0/0/0
```

**It is what the report counted, not what the server holds** — after the
chaining and after the marketable filter. That is the point: a number you cannot
reconcile is a number nobody will defend. Four checks assert that a line's
notional, executed and rejections are *the same values* the market row above it
was built from.

- **`targets`** — every `id_target` the order was sent under, oldest first, so a
  row can be pulled back out of `target`. What does not fit is **counted**, never
  dropped: `901+902 +3` still says five sends.
- **`rej` and `ss/op/cl/co`** — the rejection count and its four buckets, in the
  same order as the stacked chart.
- **The indented line** is the **distinct** rejection texts, commonest first. One
  venue wording repeated forty times is one reason, not forty.
- **`src`** — where the price came from: `limit`, `quote`, `close`, or `none`.
  `none` is an order nothing could value; it is shown rather than hidden,
  because it is one of the orders dragging the notional.
- **`[NOT marketable]`** only appears with `--keep-unmarketable`, since those
  orders are otherwise gone. *Unknown* is not flagged per line — it is the normal
  answer for Hong Kong, and tagging every line there would bury the verdict worth
  seeing.
- The **date** column appears only on a `--monthly` run. A single session is one
  date, and the same ten characters on every line is not information.

Rows come **worst first** — most rejections, then largest — because that is the
order somebody actually reads them in. `--top N` limits the lines; unset shows
**all** of them.

### `--orders-csv PATH`

The same rows with **every** field, one row per order, for a spreadsheet:

```
date, market, sym, client_id, targets, algo, basket, side,
ordered_qty, executed_qty, completion_qty,
limit_price, prev_close, price, price_source, fx,
ordered_usd, executed_usd, completion_usd,
marketable, splits, splits_rejected,
rejections, rej_short_sell, rej_open, rej_close, rej_continuous, reject_texts
```

`limit_price` and `prev_close` are there so the **marketable** verdict can be
checked by hand against the band, and `price`, `price_source` and `fx` so the
notional can. Combine with `--orders` to see them as well as write them.

Written `utf-8-sig`, because Excel reads a plain UTF-8 CSV as cp1252 and mangles
any venue that put a non-ASCII character in its rejection text. A run with no
orders still writes the header row rather than an empty file.

Rejections are **counted** in the row and their texts joined into one field. A
row per rejection would read more easily and would no longer sum to anything.

---

## `--compare`, `--chains`, `--no-tag`

One fetch, two rollups, printed side by side — so any difference is the counting
and nothing else. Executed and rejections are identical **by construction** and
the footer says so; if they ever differ, something is wrong with the chaining, not with
the data.

Plain ASCII, including the dash for a market with no orders: this goes to a
console, and a diagnostic that raises `UnicodeEncodeError` on cp1252 is no use.


## Scope

**Hong Kong, Japan, Korea, Malaysia, Taiwan and Thailand**, always all six and always
in that order. A market with no short sell flow prints as a zero row rather than
vanishing, because a market that is absent from the data is otherwise
indistinguishable from one nobody remembered to ask about. Anything whose sym
carries another suffix — or none — never enters a total.

The market is the **sym suffix**:

| suffix | market | | suffix | market |
|---|---|---|---|---|
| `.HK` | Hong Kong | | `.MK` | Malaysia |
| `.JP` | Japan | | `.TT` | Taiwan |
| `.KS` | Korea | | `.TB` | Thailand |

The `*.HK`-style patterns q filters on are **built from the same table** Python
maps back, so they cannot drift. Matching is case-insensitive and only the last
dot counts, so `BRK.A.HK` is Hong Kong.

**Japan excludes restricted names.** A parent whose `fixmsg` matches
`*RSHO=1*` is dropped before anything is counted, so it appears in neither the
order count, the quantities, nor the rejections — and its child splits go with
it. The match is case-insensitive, and the count of what was dropped is printed
on the run and carried in the page footer, so an exclusion is never silent.

---


## Real-time and historical

Both flavours of the order server hold the same tables; the historical ones
carry an extra `date` column, and that is the only difference the query has to
care about. It is **one lambda** for both, with the two extractions inside
`$[hist;…;…]` — q parses the whole thing but only resolves the branch it takes,
so the historical branch's `date=d` never has to exist on the realtime side. The
realtime branch bolts on `date:0Nd`, so everything downstream — the grouping,
the join keys, the frame Python sees — has one shape.

### Three selects, not one lambda

`Q_TARGETS`, `Q_STOCK` and `Q_WORK` are three round trips, and each is a **plain
select**: no join, no `by`, no `0!`, no `xkey`. The extra hops cost nothing next
to a month of data.

This is not tidiness. A month against the historical server once came back as:

```
pykx.exceptions.QError: nyi
```

and that was all of it — no table, no column, no line. One lambda that selects
from three tables, groups one of them and joins it to another fails as **one
call**, so `nyi` could have meant any of six things. Split up, the stage that
broke is the first line of the traceback, and `_stage()` adds the table and the
columns it asked for:

```
the target_stock query failed for 2026-07-01.
  q said:  QError: type
  columns it asked for: id_target fxlast adjclose orgclose
  target_stock is a table, 96 columns:
      date                   d
      id_target              j   g
      fxlast                 f
      adjclose               f
Compare the two.  A column that is absent, a type that is not what the query
assumed, or a KEYED table where a plain one was expected are what this error is.
```

**The schema comes with the error.** `` `type `` and `` `nyi `` name nothing at
all, and reading them off a photograph of a terminal against a schema by eye is
how an afternoon goes. So a failing stage asks the server for `meta` of the
table it was querying and prints it underneath — the missing column, or the type
that is not what the query assumed, is then on the screen beside the error. It
also says whether the thing is a plain table, a **keyed** one or a dictionary,
because a `select` refuses the last two with exactly `` `type ``.

`--probe` asks all four tables up front without running the report, which is
what to reach for rather than four failed runs:

```
python scripts/short_sell_report/short_sell_report.py --probe
```

### ids go in as the column's own type

PyKX sends a Python list of ints as a **long** vector, and `id_target` is an
**int** on every server here. `in` across the two is not something to rely on —
it may match, it may quietly match nothing, and with an index on the column it
may raise. So each query asks the table what it wants and casts to that:

```q
ids:(first exec t from meta target_stock where c=`id_target)$ids;
```

The join `Q_STOCK` used to do in q is now `merge_stock()` in Python, keyed on
`(date, id_server, id_target)`, last row wins — `target_stock` describes the
*stock*, so a second row is a restatement rather than another order. A target
with no stock row keeps its place and is valued at nothing, which downstream
already reads as *unpriced* and reports. All of that is under `--self-test`; the
`lj` never was.

### A note on names in the q

`ss` is q's string-search keyword, so a lambda parameter called `ss` is a
**parse** error — the query dies on the name before it does anything, and comes
back as `QError: ss`. Nothing local can catch that: it needs a real q process.

[`scripts/lib/q_lint.py`](../lib/README.md) reads the query text instead, and
`--self-test` runs it over all four queries: reserved words used as names,
unbalanced brackets, a symbol argument compared against char vectors, and any
join or `by`-aggregation that creeps back in. Add a name to a query and the
check covers it automatically. The one to watch for is a plausible-looking short
name — `ss`, `in`, `var`, `max`, `string`, `last`, `like`, `div`, `bin`, `do`,
`if`.

Set both endpoints before the first run:

```python
ORDER_SERVER_RT   = "CHANGEME:5012"   # realtime
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical, the same tables plus `date`
```

Host and port is the whole of it — both are open processes, so `connect()` is
`kx.SyncQConnection(host=…, port=…)` with no username and no password, the same
way the mail relay needs no login. A check asserts no `USER` or `PASSWORD`
constant has crept back in.

`--monthly` walks the month a date at a time, skipping weekends. Holidays are
**not** skipped: a holiday calendar we would have to maintain is a worse failure
mode than a handful of queries that return nothing.

`--date 2026-07-01` runs **one past session** off the historical server, in the
daily layout — the way to reproduce a given day, or to check the historical path
without waiting for a month. A date in the future is rejected rather than run.

The three modes are decided by one pure function, `plan()`, so `--self-test`
proves which server each one reaches, which dates it asks for and what the file
is called — rather than that being found out by pointing a mode at the wrong
server.

| | server | dates | layout | file |
|---|---|---|---|---|
| *(no flags)* | realtime | the session in progress | daily | `short_sell_report_2026-07-24` |
| `--date 2026-07-01` | historical | that one session | daily | `short_sell_report_2026-07-01` |
| `--monthly 2026-07` | historical | every weekday of the month | monthly | `short_sell_report_2026-07` |

---


## Seeing the page before it touches kdb

```
python scripts/short_sell_report/short_sell_report.py --demo
```

Draws **both** layouts from made up numbers and exits — no connection, no pykx,
no server constants set. Four files land in `--out-dir`:

```
short_sell_report_SAMPLE_daily.pdf    .png
short_sell_report_SAMPLE_monthly.pdf  .png
```

The numbers are deterministic, so the preview only moves when the layout does,
and the daily one reproduces the table at the top of this README exactly. It is
stamped `SAMPLE` in the subtitle, in the footer and in the file name, on purpose:
these pages end up in compliance folders, and a preview that looks like a real
report is worse than no preview at all.

---


## Email

Configured **in the script**, not on the command line — near the top, beside the
server constants:

```python
EMAIL_TO   = ["desk@example.com", "compliance@example.com"]
EMAIL_CC   = []
EMAIL_BCC  = []
EMAIL_FROM = "algo-reports@example.com"

SMTP_HOST    = "mail.example.com"
SMTP_PORT    = 0           # 0 -> 25
SMTP_TIMEOUT = 30          # seconds

EMAIL_DRY_RUN     = False
EMAIL_SIGNATURE   = "Best Regards,

Khalife"
```

Who gets this report is part of what the report *is*, not of one run of it — a
distribution list living in whatever someone last typed is a list that quietly
loses people. **`EMAIL_TO` empty means do not send**, and that is the whole
switch: there is no separate enable flag to leave in the wrong position.

The report is the **attachment**. The body is just:

```
Best Regards,

Khalife
```

No HTML, no inlined page, no table repeated in the message. A body that
restates the report is a second copy to keep in step, and it renders at the
mercy of whatever client opens it — which is what went wrong with the first
version of this. `EMAIL_SIGNATURE` is a constant in the same block if the
sign-off changes.

Each address may itself be a comma or semicolon separated list, so a pasted
distribution list works as it is. Nobody is mailed twice. An address that does
not parse **raises** rather than being dropped: a recipient list quietly one
short is exactly the failure that goes unnoticed for months.

`EMAIL_DRY_RUN = True` builds the message, prints who it would go to and what is
attached, and opens no socket — the way to check a new recipient list.

**SMTP is host, port and timeout — there is nothing else.** No credentials and
no STARTTLS: the relay takes mail from the host this runs on, so there is
nothing to authenticate with, and an auth path nobody exercises is a path that
is broken by the time somebody needs it. One check covers both this and the kdb
connection: no `SMTP_USER`, `SMTP_PASSWORD`, `USER` or `PASSWORD` constant
exists.

The mailer itself is **[`scripts/lib/mailer.py`](../lib/README.md)**, which knows
nothing about this report and is meant to be reused by the next script that
needs to send one. Only the subject line and the body live here. If you copy
this folder somewhere, copy `scripts/lib` beside it.

---


## Running it offline

`pykx` is imported lazily inside `connect()`, and everything between the query
and the page is a pure function over plain records, so the whole analytic and
rendering path runs on a machine with no kdb, no pykx and no q licence:

```
python scripts/short_sell_report/short_sell_report.py --self-test
```

It rebuilds the page above from synthetic records — 222 checks — covering
parsing tag 9604 out of a real `fixmsg`, the chaining and every guard on it, the
suffix routing (including the suffixes that are *not* ours, like Tokyo's `.T`),
the market rollup, the mean-of-markets headline, the Japan exclusion
(including that the dropped orders' fills and rejections go with them), the
counting rules (only `make` executes, a cancelled child still contributes what
it filled, only ``` `rejected ``` rejects), the day series, the rendering of
both layouts, and the email — the bodies, the attachment and the recipient
parsing — end to end.

Needs `matplotlib` to draw and `pandas` to read what PyKX returns; `--self-test`
skips the rendering checks rather than failing if matplotlib is absent.


## Colour

Taken unchanged from the data-viz reference palette, which documents its own
validation: completion is categorical slot 1 blue `#2a78d6`, rejections are
status `critical` red `#d03b3b` — deliberately not the categorical red, so it
never reads as "series 8". Two charts with one series each, so hue is chart
identity rather than series identity and there is no within-chart separation at
stake. Light only: this page gets printed and pasted into documents, where a
themed surface is a liability rather than a feature.

