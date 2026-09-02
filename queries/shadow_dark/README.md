# shadow_dark

**What a SHADOW order did in the dark** — for every SHADOW parent order over a
date range: how many shares we put into dark venues, how many came back as
fills, and how much volume was actually there to trade against while we sat in
them.

Runs entirely on the order server — `workorder`, `target` and `VENUEMAP` are
all there, so no handle is needed.

```q
q)\l queries/shadow_dark/shadow_dark.q
q)shadowDark[2026.08.24;2026.08.28]
```

Or `h(shadowDark;d0;d1)` from elsewhere, the way `darkSummary` is called.

Both dates are included. **Do not pass today** — `onmkt_adv1t` mid-session is a
partial volume.

## The result

One row per parent order, days ascending, biggest order first within each day.

| column | meaning |
| --- | --- |
| `date` | trading day |
| `id_target` | the parent order — one row per attempt, see [below](#a-replaced-order-appears-twice) |
| `sym` | the stock |
| `target_size` | the parent's own quantity, from `target` |
| `children` | how many dark child orders that parent is made of |
| `shares_routed` | `size` summed over those children — **share-attempts, not shares**, see [below](#shares_routed-counts-the-same-shares-many-times) |
| `shares_in_dark` | most shares resting in the dark at any one instant — the one comparable to `target_size` |
| `dark_pct` | `shares_in_dark` as a % of `target_size` — how much of the order was in the dark |
| `shares_executed` | `make` summed — what actually filled |
| `exec_pct` | `shares_executed` as a % of `target_size` — how much of the order came back |
| `adv1t_last` | `onmkt_adv1t` of the last child: volume at the moment we last went on market |
| `rest_ms_avg` | mean `t_off_market - t_on_market` across those children, in ms |

**Read a row as `dark_pct` then `exec_pct`:** *we had 100% of this order
sitting in dark pools, and 1.1% of it came back.* `shares_routed % target_size`
is the third number — how many times over we sent it — and the gap between
that and `dark_pct` is the churn.

`shares_executed % shares_routed` is the plain fill rate, but against a routed
figure inflated by re-sends it flatters nothing; `exec_pct` is the honest one.

## The three settings

Everything this query judges is the three settings at the top of
`shadow_dark.q`. Editing that file and reloading it is the only step.

### `.shd.darkCategories` — which venues are dark

```q
.shd.darkCategories:`Dark`Pmid;
```

Venues are classified by **`VENUEMAP`**, a reference table on the order server
with one row per venue and a `category` column: `Dark`, `Pmid`, `Post`,
`PostBlind`, `Take`, plus the auction categories. We join it and keep the
categories listed above.

The obvious alternative — testing the venue's *name* with
`venue like "*DARK*"` — is what the older queries in this repo do, and it is
wrong in two ways:

- **It silently drops PMID.** `-PMID` venues contain neither "DARK" nor "DRK",
  but they are midpoint dark liquidity. The desk's own `darkchase.q` matches
  `*-PMID` alongside `*-DARK` and `*-CLNDRK`.
- **It breaks on renames.** `darktoxic.q` carries a hand-patched line
  rewriting `LEHM-DARK` to `BCAP-DARK`, and there is a whole `VENUERENAME`
  table. A name test bakes in today's names; a category survives the rename.

Set it to `` `Dark `` alone to count only true dark pools and exclude midpoint.
The desk goes both ways depending on the question — `darktoxic.q` uses
`` `Dark`Pmid ``, `cleandirty.q` uses `` `Dark `` — so this is a real choice,
and the point of putting it in a setting is that it is now made deliberately.

### `.shd.minRestMs` — how long a child had to sit

```q
.shd.minRestMs:0;        / off.  600000 would be 10 minutes
```

Milliseconds of time on market, `t_off_market - t_on_market`. Applies to
**every** child, filled or not.

**Off by default**, because `shares_routed` is meant to show the whole effort,
churn included — and `shares_in_dark` now gives the un-inflated number
alongside it, which is what a duration floor was being used to approximate.

Independently of the setting, a child still on the market is always excluded:
`t_off_market` is `0`, so it has no duration and would corrupt the
`shares_in_dark` sweep.

### `.shd.keepReasons` — which cancelled orders still count

```q
.shd.keepReasons:`rotate_venue`chase_price`chase_liquidity`adverse_mkt`over_duration;
```

A child order that never filled is not automatically a failure. SHADOW cancels
resting dark orders constantly, and *why* it cancelled is the whole question.

The order server records this: it writes the state and the kill reason into one
column as `"state:killreason"`, so `state` reads `cxl:rotate_venue`,
`cxl:chase_price`, `cxl:goal_change`. We split on the colon and match the
second half.

**Counted** — the order was a genuine dark attempt that the algo pulled on
purpose:

| reason | what happened |
| --- | --- |
| `rotate_venue` | moved off a cold venue on the rotation cycle. The most common SHADOW cancel, and entirely by design |
| `chase_price` | pulled and re-priced because the market moved |
| `chase_liquidity` | freed to replenish a venue that *is* filling |
| `adverse_mkt` | pulled because the market went against us |
| `over_duration` | sat its allotted time and timed out — a completed attempt, not a failure |

**Not counted** — the parent moved, not the venue: `goal_change`,
`goal_met`, `need_shares`, `need_take`, `target_modify`, `oversize`, plus the
session boundaries (`stop_to_finish`, `cancel_for_eod`, `stop_dark_eod`,
`scheduler_halt`, `lunchbreak`, `moo_after_open`) and the risk controls
(`churn_prevent`, `below_minfill`). Counting these would blame a venue for a
decision taken above it — the order was pulled for reasons that had nothing to
do with how the venue behaved.

Orders that never reached a venue at all — rejected, never acked, never
transmitted — are excluded by `t_on_market > 0`, which is where every dark
query on this desk starts.

### What a 10-minute floor does, if you turn it on

Set `.shd.minRestMs:600000` and the short-lived churn goes. On one
500,000-share order in `388.HK`, `shares_routed` fell from 12,212,800 to
497,000: **96% of the routed shares came from children that rested under ten
minutes**, and what was left came out within 1% of the order itself. Across a
run over 2026-08-28 to 2026-09-01 the median `shares_routed % target_size` went
to 0.99, with 12 of 19 rows at or under 1.2.

It is off by default because `shares_in_dark` answers the same question without
discarding rows: the churn shows up as the gap between `shares_routed` and
`shares_in_dark` rather than by being filtered away. Turn it on when you want
the long sitters specifically.

A note on the duration itself: the engine's rotation logic uses a per-stock
value clamped to 10–100 seconds, but that is the **minimum age** before a
resting order becomes a rotation candidate (`findColdOrders` skips anything
younger) — it does not cap how long a child lives. Observed `rest_ms_avg` runs
from ten minutes to about six hours, the latter a full ASX session.

## The volume column, and the better one we did not use

`adv1t_last` is `onmkt_adv1t` from the last child — a **single reading, taken
at one instant**, of all volume, with no price filter. It is a rough proxy for
"how much was trading while we were in there", and it is the only volume figure
this query carries. Being a reading rather than a total for the period, on a
day still trading it is whatever had printed by then — so only pass completed
days.

There is a better measure, and it is worth knowing it exists. `work_list`
carries `vvalid` and `vvalida`: the volume that printed **at or through that
child's own limit price**, accumulated across the whole time the child rested
(`exst.q`, `mktst1a`). That matters because dividing fills by total volume
punishes a venue whenever the stock traded heavily at prices we would never
have paid. `vvalid` asks instead: *of the volume I would have taken, how much
did I get?* `cleandirty.q` — the desk's own SHADOW-only query — uses
`make % vvalida` as its effective fill rate.

It is not here because **`work_list` is on the EXST server, not the order
server**. Reaching it costs a second handle, a second round trip, and the
single-lambda shape every other query in this repo has. If the effective fill
rate becomes the question, that is the change to make, and `cleandirty.q` is
the model for it.

## shares_routed counts the same shares many times

SHADOW rotates a parent order round the venues: it sends shares to one pool,
cancels (`rotate_venue`), sends the same shares to the next. `shares_routed`
sums `size` across every one of those children, so **a parent that rotated
forty times has routed forty times its own quantity**.

That is what "routed" means, and it is the right denominator for "how hard did
we work this order". It is *not* the parent's size, and `shares_routed` can
comfortably exceed both the parent quantity and the whole day's volume in the
name. The `children` column is in the result so that the multiple is visible
rather than surprising: `shares_routed % children` is roughly the typical
child size.

`target_size` is the parent's own quantity, taken from `target` rather than
summed from children, so `shares_routed % target_size` reads directly as the
rotation multiple — how many times over we sent the order into the dark.

**`shares_in_dark` is the column to compare against `target_size`.** It is the
most shares resting in dark venues at any one instant: the children's on-market
intervals swept in time order, taking the running maximum. A parent that sent
100,000 shares, cancelled, and sent the same 100,000 again counts 200,000
routed but 100,000 in the dark. It answers "how much of this order was actually
sitting in dark pools", which is the question `shares_routed` looks like it
answers and does not.

Keep both: `shares_routed` measures effort, `shares_in_dark` measures exposure,
and the gap between them is the churn.

## A replaced order appears twice

When an order is rejected and re-sent, the engine writes a **new `id_target`**
for each attempt. One economic order therefore appears as several rows, and its
quantity is counted once per attempt.

This is left as-is on purpose — it is what the raw table says, and the
rejections are often the finding. The chain-collapse logic lives in
`scripts/short_sell_report_v2`.

## Where the data comes from

| table | server | what we take |
| --- | --- | --- |
| `target` | order | `algo`, to find the SHADOW parents |
| `workorder` | order | the child orders — size, make, venue, state, times |
| `VENUEMAP` | order | `category`, the venue classification |

Everything is on the order server, so `shadowDark` goes out as one lambda
and needs no handle at all.

## One trap worth writing down

**`count_chaseprice` is not the chase counter.** The order server writes
`minExecSize()` into that column — the minimum execution size, nothing to do
with chasing. The real counter exists in the engine but is never persisted.
Anyone reaching for that column to find price chases will be filtering on
minimum fill size and getting a plausible-looking, meaningless answer. The kill
reason in `state` is the only place a chase is recorded.

## Status

Written against the schemas in `no_git/kdb` and the desk's own queries. **Not
yet run** — there is no kdb on the machine this was written on. The first live
run is the real test, particularly the `VENUEMAP` join and the `state` split,
whose exact letter case should be confirmed against real rows.
