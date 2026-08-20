# short_sell_report_v2

The [Short-Sell Order Report](../short_sell_report/README.md), counting a
**rejected-and-replaced order once**.

```
python scripts/short_sell_report_v2/short_sell_report_v2.py
python scripts/short_sell_report_v2/short_sell_report_v2.py --compare    # v1 beside v2
python scripts/short_sell_report_v2/short_sell_report_v2.py --chains     # what got chained
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
8=FIX.4.2 | 35=D | 9604=CLI-0001 | 59=0     attempt 1
8=FIX.4.2 | 35=D | 9604=CLI-0001 | 59=0     attempt 2   same order
```

Chained on **(date, id_server, tag 9604)**. A target whose 9604 is empty cannot
be chained to anything, so it **stands alone** and is counted exactly as v1
counts it — untagged orders are never grouped together, which would merge every
order the client did not label.

> An earlier version of this grouped on the `oes_oid` prefix. That was a
> convention; 9604 is a contract. The prefix version is gone.

**Reading the tag.** Fields are split on SOH (``), pipe, semicolon or caret —
a space is *not* a separator, since values contain them. The whole tag is
compared after splitting rather than searching for `"9604="`, so `19604=`,
`96040=` and a `9604=` appearing inside another field's *value* are all
correctly ignored.

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

The one change to v1 was an optional `title=` on `draw()`, so v2 can put its own
name on the same layout. No number moved.

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

### One thing still assumed

That the chain's quantity is the **last** attempt's size. `--chain-qty max` if a
replace can come back for only the unfilled remainder. Every run reports:

```
NOTE: 5 chains have attempts of differing size; CHAIN_QTY='last' takes the last
```

While that count is zero the two settings are identical. A chain that resizes is
**not** reported as mixed — a replace may legitimately change quantity, which is
the difference between that and a chain that changes stock.

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

95 checks, no kdb and no pykx: parsing tag 9604 out of a fixmsg in four
separator styles and refusing the 19604/96040/embedded-value traps, chaining on
the client id, untagged targets standing alone, which attempt sets the quantity,
both checks and the exclusivity of their branches, the Thailand rollup end to
end, and that every reused name really comes from v1.
session, what does and does not share a chain, which attempt sets the quantity
(including a replace that shrank and one sent out of order), the validation
counters, the Thailand rollup end to end, that v1 over the same data still says
three orders, and that every reused name really comes from v1.
