# short_sell_report_v2

The [Short-Sell Order Report](../short_sell_report/README.md), counting a
**rejected-and-replaced order once**.

```
python scripts/short_sell_report_v2/short_sell_report_v2.py
python scripts/short_sell_report_v2/short_sell_report_v2.py --compare    # v1 beside v2
python scripts/short_sell_report_v2/short_sell_report_v2.py --chains     # what got chained
python scripts/short_sell_report_v2/short_sell_report_v2.py --no-tag    # what carries no 9604
python scripts/short_sell_report_v2/short_sell_report_v2.py --self-test
```

v1 is untouched. Run either; run `--compare` to see both over one fetch.

---

## The problem

When an order is rejected and re-sent, the engine writes a **new `id_target`**.
v1 counts target rows, so one economic order becomes several and its `size` is
counted once per attempt.

Seen live on 2026-08-19 — Thailand read **3 orders / 81,000,000 / 0 executed**.
It was **one 27m order**, rejected and replaced twice, the last attempt
cancelled.

Dropping the rejected orders would fix the quantity and delete the finding: the
rejections are what the report is for. So the attempts have to be collapsed
while every rejection they produced is kept.

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

## What changes, and what does not

| | |
|---|---|
| **Orders** | one per **chain**, not one per target | **changed** |
| **Order qty** | the chain's size, taken **once** | **changed** |
| **Executed** | sum of `make` over every attempt's workorders | unchanged |
| **Rejections** | every workorder row in state `` `rejected ``, **across all attempts** | unchanged |
| **Completion** | executed / order qty per market, mean across markets | unchanged |

On the live Thailand case:

```
                        orders                   order qty            completion
                  v1        v2            v1            v2         v1         v2
--------------------------------------------------------------------------------
Thailand           3         1    81,000,000    27,000,000       0.0%       0.0%
```

— and both still report its **2 rejections**, which is the whole point.

## It is not a copy of v1

The markets, the suffix routing, the Japan exclusion, what counts as a
rejection, the rollup of `make`, the page, the mail and the mode routing are
**imported from `short_sell_report.py`**, not duplicated. Only the chain logic
and the orders/qty rollup live here — about 300 lines against v1's 1,500.

That is deliberate: a second copy of a report is a report that drifts, and a
comparison is only meaningful if the *only* difference is the thing being
compared. Checks assert each reused name really comes from v1.

**v1 is not modified at all.** v2 reaches v1's title through its `TITLE` global
rather than through an argument, precisely because `short_sell_report.py` is the
file you have to *edit* — the servers and the mail live in it — so a copy in the
wild is often not the copy in git. v2 must not need a particular signature from
it. A check draws the page against a simulated v1 whose `draw()` predates any
keyword v2 might have wanted.

---

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
61 of 924 targets (6.6%) carry no tag 9604 and stand alone, as v1 counts
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

each stands alone and is counted exactly as v1 counts it

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
| a **replacement** | supersedes the one before it. Three sends of 27m that never traded are **one 27m order**, not 81m — the whole reason v2 exists. |
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
v1 counts them, so the page does not read over 100%. Those are the ones to
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

## `--compare`

One fetch, two rollups, printed side by side — so any difference is the counting
and nothing else. Executed and rejections are identical **by construction** and
the footer says so; if they ever differ, something is wrong with v2, not with
the data.

Plain ASCII, including the dash for a market with no orders: this goes to a
console, and a diagnostic that raises `UnicodeEncodeError` on cp1252 is no use.

## Configuration

Servers and email are **v1's** — this reads the same processes and goes to the
same people. Edit them in `short_sell_report.py`, once.

Output goes to `scripts/short_sell_report_v2/out/` as
`short_sell_report_v2_<date>.pdf` and `.png`, so the two reports never overwrite
each other.

## Running it offline

```
python scripts/short_sell_report_v2/short_sell_report_v2.py --self-test
```

167 checks, no kdb and no pykx: parsing tag 9604 out of a fixmsg in four
separator styles and refusing the 19604/96040/embedded-value traps, chaining on
the client id, untagged targets standing alone, which attempt sets the quantity,
both checks and the exclusivity of their branches, the Thailand rollup end to
end, and that every reused name really comes from v1.
session, what does and does not share a chain, which attempt sets the quantity
(including a replace that shrank and one sent out of order), the validation
counters, the Thailand rollup end to end, that v1 over the same data still says
three orders, and that every reused name really comes from v1.
