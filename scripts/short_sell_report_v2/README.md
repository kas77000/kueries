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

## The chain key

`oes_primoid` is always empty, so the link is the **`oes_oid` stem** — the id
with its last dot-component (the attempt) removed:

```
SCB-R.TB.APPD2.1w519.2p5   ->  stem SCB-R.TB.APPD2.1w519   attempt 2p5
SCB-R.TB.APPD2.1w519.3p1   ->  stem SCB-R.TB.APPD2.1w519   attempt 3p1
```

which is the q you already had:

```q
by stem:{"." sv -1 _ "." vs string x} each oes_oid
```

Chained on **(date, id_server, stem, basket)**.

**The stem alone is not an order** — two orders in different baskets can share
one, so the basket is what makes it an order. Side is *not* in the key: this
whole report is one side, so it could never separate two rows here.

**The rule is tested, not insured against.** `sym` and `side` are deliberately
kept **out** of the key, so that stem + basket merging two orders that are not
the same order is something the run can *see* and complain about. Putting them
in would make the key look right by construction and tell you nothing.

An `oes_oid` with no dot has no attempt to strip and becomes its own chain —
the safe reading, since it can only ever fail to collapse something.

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

## Validate it before trusting it

Two things are assumed and **neither is proven on your data**. Both are reported
on every run rather than quietly relied on.

**1. That stem + basket is an order.** Every run prints:

```
chains: 924 targets -> 871 orders (43 chained, longest 3)
1 oes_oid stem shared by 2 different orders - the basket kept them apart;
a stem-only key would have merged them.  --chains lists them
```

and, if the rule breaks:

```
WARNING: 2 chains hold more than one sym or side. stem + basket has merged
         orders that are NOT the same order and these numbers are WRONG.
```

**That warning must be absent.** It is the one number that says the key is
wrong, and it exists only because sym and side are kept out of the key.

The `stem shared by N orders` line is the opposite direction and is **not** an
error — it is how much work the basket is doing. Zero means the stem was unique
anyway.

`--chains` shows the chains, any mixed ones, and the shared stems, so all three
can be checked against the engine:

```
  SCB-R.TB  TH  sellshort  stem SCB-R.TB.A.1w519  basket ALPHA  -> qty 27,000,000
      id_target 1270254699   size     27,000,000  t     38102
      id_target 1270254812   size     27,000,000  t     38455
      id_target 1270255001   size     27,000,000  t     39120

1 stem held more than one order - this is what the basket is in the key FOR:

  stem SCB-R.TB.A.1w519
      SCB-R.TB       sellshort  basket ALPHA      qty     27,000,000  3 attempts
      SCB-R.TB       sellshort  basket BETA       qty      4,000,000  1 attempt
```

If the warning fires, `--chains` prints the offending chains with each
attempt's sym and side, so what got merged is visible immediately.

**2. That the chain's quantity is the last attempt's size.** `CHAIN_QTY` is
`"last"`. Use `--chain-qty max` if a replace can come back for only the
**unfilled remainder** — summing fills across attempts against a smaller final
size would overstate completion. Every run reports:

```
NOTE: 5 chains have attempts of differing size; CHAIN_QTY='last' takes the last
```

**While that count is zero the two settings are identical** and the choice does
not matter. A pure reject-and-replace re-sends the same quantity, so on the
Thailand case it is zero.

Also reported: targets with **no `oes_oid` at all**, each of which becomes its
own chain and is therefore counted exactly as v1 counts it.

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

70 checks, no kdb and no pykx: the stem rule against both ids from your qStudio
session, what does and does not share a chain, which attempt sets the quantity
(including a replace that shrank and one sent out of order), the validation
counters, the Thailand rollup end to end, that v1 over the same data still says
three orders, and that every reused name really comes from v1.
