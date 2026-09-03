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
| `targets` | SHADOW parent orders in the period |
| `target_qty` | their own quantity, summed — the shares we were actually asked to trade |
| `children` | child orders those parents produced, lit and dark |
| `routed_dark` | `size` summed over the dark children |
| `routed_lit` | `size` summed over the lit children |
| **`dark_pct_routed`** | `routed_dark % (routed_dark + routed_lit)` — **share of routing that went dark** |
| `exec_dark` | `make` summed over the dark children |
| **`dark_pct_exec`** | `exec_dark % routed_dark` — **fill rate on what we sent to the dark** |
| `exec_lit` | `make` summed over the lit children, for comparison |
| `shares_in_dark` | peak shares resting in dark venues, per parent, summed |
| `mkt_volume` | market volume in those names while we were dark — see below |
| `dark_pct_vol` | `exec_dark % mkt_volume` — our dark share of the tape |

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
whichever side rotates harder — normally the dark side. Set
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

## Where the data comes from

| table | server | what we take |
| --- | --- | --- |
| `target` | order | `algo` to find the SHADOW parents, `size` for `target_qty` |
| `workorder` | order | the children — size, make, venue, state, times, `onmkt_adv1t` |
| `VENUEMAP` | order | `category`, the venue classification |

Everything is on the order server, so `shadowDarkPeriod` goes out as one lambda
and needs no handle.

`t_on_market > 0` excludes children that never reached a venue — rejected,
never acked, never transmitted — on both the lit and the dark side.

## Status

Written against the schemas in `no_git/kdb` and the desk's own queries. **Not
yet run.** The first live run is the test — particularly `.shd.roll`, the
functional aggregation that makes `.shd.groupBy` work, and the `VENUEMAP` join.
