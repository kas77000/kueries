# shadow_dark_v2

**What SHADOW did in the dark over a period** — one row for a month, a day or a
stock, instead of one row per order. Three questions:

1. Of everything SHADOW routed, what share went to dark venues? (`dark_pct_routed`)
2. Of what we routed into the dark, what share filled? (`dark_pct_exec`)
3. How much volume was trading in those names while we were in there? (`mkt_volume`)

The per-order version is [`../shadow_dark`](../shadow_dark). Same tables, same
venue classification, same `shares_in_dark` sweep — this one aggregates instead
of listing.

```q
q)\l queries/shadow_dark_v2/shadow_dark_v2.q
q)shadowDarkPeriod[2026.08.01;2026.08.31]
```

Or `h(shadowDarkPeriod;d0;d1)` from elsewhere. Both dates are included.
**Do not include today** — `onmkt_adv1t` mid-session is a partial volume.

## SHADOW is not a dark-only algo

That is the whole reason v2 exists. A SHADOW parent sends children to dark
pools *and* to lit venues, and the mix moves with the stock, the urgency and
the time of day. v1 filtered the lit children away before counting anything, so
it could never tell you what fraction of the effort was dark. v2 keeps both
sides and splits them by `VENUEMAP.category`.

## The result

| column | meaning |
| --- | --- |
| `targets` | SHADOW parent orders in the period that passed `.shd.minExecPct` |
| `target_qty` | their own quantity, summed — the shares we were actually asked to trade |
| `children` | child orders those parents produced, lit and dark |
| `routed_dark` | `size` summed over the dark children |
| `routed_lit` | `size` summed over the lit children |
| **`dark_pct_routed`** | `routed_dark % (routed_dark + routed_lit)` — **share of routing that went dark** |
| `exec_dark` | `make` summed over the dark children |
| `exec_lit` | `make` summed over the lit children |
| **`dark_pct_traded`** | `exec_dark % (exec_dark + exec_lit)` — **share of what we actually traded that was dark** |
| **`dark_pct_exec`** | `exec_dark % routed_dark` — **fill rate on what we sent to the dark** |
| `notional_ord_usd` | what we were asked to trade, in USD — see [below](#the-notional-columns) |
| `notional_dark_usd` | executed in dark venues, in USD |
| `notional_lit_usd` | executed in lit venues, in USD |
| **`dark_pct_notl`** | `notional_dark_usd % (notional_dark_usd + notional_lit_usd)` — the dark share **weighted by money, not shares** |
| `shares_in_dark` | peak shares resting in dark venues, per parent, summed |
| `mkt_volume` | market volume in those names while we were dark — see below |
| `mkt_vol_mult` | `mkt_volume % target_qty` — how many times the order's own size traded past us |
| `dark_pct_vol` | `exec_dark % mkt_volume` — our dark share of the tape |

## Orders that actually traded: `.shd.minExecPct`

```q
.shd.minExecPct:0.10;   / keep parents that filled at least 10% of their size
.shd.minExecPct:0;      / off - every parent with a child that reached a venue
```

Applied per **parent**, against `(exec_dark + exec_lit) % target_qty` — lit and
dark fills together, because this is a question about the order, not about a
venue. A parent that fails it is removed whole: its children stop counting
towards `routed_dark`, `routed_lit`, `children`, `shares_in_dark` and
`mkt_volume`, not just towards the execution columns.

It exists because a SHADOW parent that traded 0.4% of itself is mostly a record
of *attempts*. Its children inflate `routed_dark` and drag `dark_pct_exec` down
without saying much about how the dark venues behaved when we were really
trading. At 10% the surviving rows are orders the desk actually worked.

`targets` in the result counts the parents that **passed**. To see how many
were dropped, run once with `.shd.minExecPct:0` and compare.

Two things to keep in mind when quoting a filtered number:

- It is a **survivorship-biased** view by construction. `dark_pct_exec` will
  rise, because the orders that never filled are exactly the ones removed. That
  is the point of the filter, but it means the figure answers "when SHADOW
  traded, how did the dark do" and not "how does SHADOW do".
- A parent still working at the end of the period is judged on a partial day.

## Which venues count as dark: `.shd.darkCategories`

```q
.shd.darkCategories:`Dark;   / dark pools only
```

`Pmid` — midpoint-pegged, non-displayed — is **not** in the default. The engine
classifies PMID as lit-but-hidden (`AggressionLevel.isLit()` returns true for
it) and `grey_include_pmid` defaults to `0` outside EU, so for Asia the engine
itself does not count it as dark. The full argument, and the two wider sets
(`` `Dark`Pmid `` and `` `Dark`Pmid`PostBlind ``), are in
[`../shadow_dark/README.md`](../shadow_dark/README.md#why-pmid-is-not-in-the-default).

Changing this moves the split twice: a pmid child leaving `routed_dark` lands
in `routed_lit`.

## Which stocks: `.shd.symLike`

```q
.shd.symLike:"*.JP";      / Japan - the default
.shd.symLike:"*.HK";      / Hong Kong
.shd.symLike:"*";         / everything
```

Matched against `workorder.sym` inside the `where` clause, so the rows are
never read rather than read and thrown away. Children carry their parent's
`sym`, so filtering the children is the same as filtering the targets.

## Choosing the rows: `.shd.groupBy`

```q
.shd.groupBy:`symbol$();   / one row for the whole period
.shd.groupBy:`sym;         / one row per stock, biggest dark router first
.shd.groupBy:`date;        / one row per day
.shd.groupBy:`date`sym;    / both
```

Everything is aggregated at `date`+`sym` first and rolled up from there, so the
numbers agree across grains: the `` `sym `` rows add up to the single
whole-period row, column by column. The percentages are recomputed after the
roll-up, never averaged.

## `dark_pct_routed` is a share of effort, not of the order

`routed_dark` and `routed_lit` both count the same shares **once per send**.
SHADOW rotates a resting order round the venues — sends, cancels
(`rotate_venue`), sends the same shares to the next pool — so a parent that
rotated forty times contributes forty times its own quantity. The lit side
churns too, on re-prices rather than rotations.

That makes `dark_pct_routed` an honest answer to *"where did the algo spend its
effort?"* and a wrong answer to *"how much of the order sat in the dark?"* The
second question is what `shares_in_dark` is for: the most shares resting in dark
venues at any one instant, per parent, summed over parents.
[The sweep is explained here](../shadow_dark/shares_in_dark.md).

Read the two together. `shares_in_dark % target_qty` says how much of the book
was genuinely exposed to dark liquidity; `routed_dark % shares_in_dark` says how
many times over we re-sent it to get there.

Because the two sides churn at different rates, `dark_pct_routed` overstates
whichever side rotates harder, and that is always the dark side. On a month of
real flow it reads **99.9%** — not because SHADOW barely touches lit, but
because a dark child is re-sent about twelve times and a lit child is not.

`dark_pct_traded` is the same question asked of executions instead of sends:
each fill is counted once, whatever it took to get it, so no rotation can
inflate it. On that same month it reads **90.2%**. Quote `dark_pct_traded` when
someone asks "how much of our SHADOW flow is dark"; quote `dark_pct_routed`
when the question is about where the algo spends its messages.

Set
`.shd.keepFilter:1b` to drop the cancels that were the parent moving rather
than a venue decision, applied to lit and dark alike so the comparison stays
like-for-like; the reason list is documented in
[`../shadow_dark/README.md`](../shadow_dark/README.md#shdkeepreasons--which-cancelled-orders-still-count).
It is **off by default**, because for a share-of-effort split the honest
denominator is every child that reached a venue.

## `dark_pct_exec` is a real fill rate

`exec_dark % routed_dark` — of the shares we put into dark pools, how many came
back. Both sides of that ratio are inflated by the same rotations, so unlike
`dark_pct_routed` the churn largely cancels: sending 40,000 ten times and
filling 4,000 reads 10%, exactly as sending 40,000 once and filling 4,000 does.

`exec_lit` is in the result so the two can be put side by side, but note that a
lit fill rate is a different animal — lit children cross the spread and fill by
construction, dark children wait for a counterparty.

## `mkt_volume`, and what it really is

`onmkt_adv1t` is the volume traded in the stock, read off at the instant a
child went on market. We take the **last** such reading from each parent's dark
children — the latest view of the tape while we were in there — then, for each
day and stock, the **maximum** across parents, and only then sum.

The max matters. Two SHADOW parents in `388.HK` on the same day are reading the
*same tape*; summing their readings would count the day's volume twice. Taking
the largest reading per day per stock and summing across days and stocks gives
a figure that means "the volume that went past while we were dark".

Two caveats worth stating to anyone who quotes the number:

- It is **all volume, not lit-only**. `onmkt_adv1t` carries no venue or price
  filter, so dark prints are in there too. There is no lit-only equivalent on
  the order server.
- It is a **reading, not a total for the period**. It is whatever had printed
  by our last dark touch, which on a parent that finished at 10:00 is a morning
  figure. Ordering all children by time and taking the last is the closest the
  table gets to "volume while we were there".

`dark_pct_vol` divides `exec_dark` by it: our dark executions as a share of
everything that traded. Small numbers are normal.

## The notional columns

All three are **USD**, so they add up across markets. `target_stock.fxlast`
converts local currency to USD, the same way `dark_summary.q` does it.

| column | how |
| --- | --- |
| `notional_ord_usd` | `target_qty * px_arr * fxlast` |
| `notional_dark_usd` | `sum make * avg_fill_price` over dark children, `* fxlast` |
| `notional_lit_usd` | the same over lit children |

`px_arr` is the **arrival price**: `transmit_lastprice` on the parent's first
child — the last trade at the moment we first went on market — falling back to
`price`, the price the child was sent with, when that is missing. `target` has
no arrival price of its own and its `limit_price` is a limit rather than a
valuation, so the first child is the closest the schema gets to "what the order
was worth when we got it".

Executions are valued at what they actually filled at (`avg_fill_price`), not
at arrival, so `notional_dark_usd + notional_lit_usd` against
`notional_ord_usd` carries the price drift as well as the fill rate. It is a
value ratio, not a completion ratio — use `exec_dark + exec_lit` against
`target_qty` for completion.

**`dark_pct_notl` is the one to prefer over `dark_pct_traded` when the rows
span many stocks.** `dark_pct_traded` counts shares, so a 10,000-share fill in
a ¥400 stock weighs the same as one in a ¥40,000 stock. Weighted by money, the
two are 100x apart. On a single-stock row they say the same thing.

If `fxlast` is missing for a parent, that parent contributes nothing to the
notional columns and still contributes to the share columns — the shortfall is
silent. It should not happen: `target_stock` carries one row per parent.

## How much liquidity went past: `mkt_vol_mult`

`mkt_volume % target_qty`. If the order was 300,000 shares and 6,000,000 traded
in the name while we were in the dark, it reads **20** — twenty times the order
went past us, and we caught whatever `dark_pct_vol` says.

Read the two together. A low `dark_pct_exec` with a **high** `mkt_vol_mult`
means the liquidity was there and we did not get it. The same fill rate with a
**low** `mkt_vol_mult` means there was nothing to get, and the algo is not the
problem — the size is too big for the name.

It inherits `mkt_volume`'s caveats below: all volume rather than lit-only, and
a reading rather than a period total.

## Where the data comes from

| table | server | what we take |
| --- | --- | --- |
| `target` | order | `algo` to find the SHADOW parents, `size` for `target_qty` |
| `workorder` | order | the children — size, make, venue, state, times, prices, `onmkt_adv1t` |
| `target_stock` | order | `fxlast`, local currency to USD |
| `VENUEMAP` | order | `category`, the venue classification |

Everything is on the order server, so `shadowDarkPeriod` goes out as one lambda
and needs no handle.

`t_on_market > 0` excludes children that never reached a venue — rejected,
never acked, never transmitted — on both the lit and the dark side.

## A first run, and one trap it caught

A month of SHADOW flow, single row, `.shd.groupBy` empty. It predates both
`.shd.symLike` and the removal of `Pmid`, so it is every region and it counts
midpoint as dark — the shape of the answer is the point, not the figures:

| | |
| --- | ---: |
| `targets` | 665 |
| `target_qty` | 394,759,809 |
| `children` | 61,165 |
| `routed_dark` | 4,092,054,048 |
| `routed_lit` | 4,968,913 |
| `dark_pct_routed` | 99.88% |
| `exec_dark` | 33,076,624 |
| `exec_lit` | 3,574,113 |
| `dark_pct_traded` | 90.25% |
| `dark_pct_exec` | 0.81% |
| `shares_in_dark` | 326,146,641 |
| `mkt_volume` | 966,999,981 |
| `dark_pct_vol` | 3.42% |

Read as: **83% of the book was genuinely resting in dark pools**
(`shares_in_dark % target_qty`), re-sent about **12.5 times over**
(`routed_dark % shares_in_dark`), and **90% of everything we traded came from
the dark**. The 0.81% `dark_pct_exec` is a fill rate against that inflated
denominator — it is per *send*, not per share, and the twelve rotations are
what make it small.

**`size` and `make` are 32-bit ints, and q's `sum` over an int vector returns
an int.** The first run of this query returned `routed_dark` as
−202,913,248 — the true 4,092,054,048 wrapped at 2³² — which then silenced
`dark_pct_routed` and `dark_pct_exec`, because `.shd.pct` guards on a positive
denominator. Both scripts now cast with `"j"$` in the source select, so every
sum downstream is 64-bit. Per-order totals in v1 never came near the limit;
a period aggregate crosses it in a few days.

Worth a second look on your own data: `routed_lit` is only 1.3% of
`target_qty`. Either SHADOW really does put almost everything in the dark, or
some of its lit trading is routed under a different parent and never appears as
a child of the SHADOW `id_target`. `dark_pct_traded` would be overstated if the
second is true.

## Status

Run over a month on the order server. `.shd.roll` and the `VENUEMAP` join both
behave; the `.shd.groupBy` grains other than the default have not been
exercised yet.
