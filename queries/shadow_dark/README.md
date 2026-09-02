# shadow_dark

**What a SHADOW order did in the dark** — for every SHADOW parent order over a
date range: how many shares we put into dark venues, how many came back as
fills, and how much volume was actually there to trade against while we sat in
them.

```q
q)\l queries/shadow_dark/shadow_dark.q
q)hw:hopen `:orderserver:port      / workorder, target, VENUEMAP
q)hs:hopen `:statsserver:port      / work_list
q)shadowDark[hw;hs;2026.08.24;2026.08.28]
```

Both dates are included. **Do not pass today** — see
[the volume columns](#the-four-volume-columns) for why.

## The result

One row per parent order, days ascending, biggest order first within each day.

| column | meaning |
| --- | --- |
| `date` | trading day |
| `id_target` | the parent order — one row per attempt, see [below](#a-replaced-order-appears-twice) |
| `sym` | the stock |
| `shares_routed` | `size` summed over the dark children we counted |
| `shares_executed` | `make` summed — what actually filled |
| `adv1t_last` | `onmkt_adv1t` of the last child: volume at the moment we last went on market |
| `vvalid` | volume that printed **at or through our limit** while our children rested |
| `vvalida` | the same, measured up to the last fill rather than to the end |

`shares_executed % shares_routed` is the plain fill rate.
`shares_executed % vvalida` is the *effective* fill rate — the one the desk's
own SHADOW query uses (`cleandirty.q`). See [below](#the-four-volume-columns).

## The two settings

Everything this query judges is the two settings at the top of
`shadow_dark.q`. They are shipped to the server with the query, so editing that
file is the only step.

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

### Why not a time threshold

The obvious rule is "keep the orders that rested long enough", say ten minutes.
It does not work, and the engine's own numbers say why: its notion of a
reasonable dark rest is computed per stock from how often that stock trades,
then **clamped to between 10 and 100 seconds**. The SHADOW venue-rotation cycle
is bounded by the same scale.

So 100 seconds is the *ceiling* on a normal rest. A ten-minute floor would not
select the valid cancels — it would select the pathological ones, and throw
away nearly every genuine dark attempt.

The kill reason separates "rotated out after a normal rest" from "cancelled
instantly because the parent changed" far better than any duration can, because
that is the distinction it was written to record.

## The four volume columns

All four answer "how much volume was around while we were trying", and they are
**not** interchangeable.

`adv1t_last` is `onmkt_adv1t` from the last child — a **single reading, taken
at one instant**, of all volume, with no price filter. It is a rough proxy.

`vvalid` is the real measure. While a child rests in the dark, the market keeps
printing. Some of those prints are at prices we would have been happy with,
most are not. `vvalid` accumulates across the whole time the child was live and
counts **only the prints at or through that child's own limit price** — buy
side, everything at or below our limit; sell side, at or above.

That matters because dividing fills by total volume punishes a venue whenever
the stock traded heavily at prices we would never have paid. Dividing by
`vvalid` asks the fair question: *of the volume I would have taken, how much
did I get?*

`vvalida` is the same calculation measured up to the order's last fill instead
of to the end of its life. It is the one `cleandirty.q` — the desk's own
SHADOW-only query — uses for its effective fill rate.

### Two cautions on these numbers

**`vvalid` double counts when children overlap.** It is summed across a
parent's children, and when two children rest in two venues at the same time,
the same market prints are counted once for each. Read it as a scale, not as a
quantity that reconciles against the tape.

**`adv1t_last` on today is a partial volume.** The day is not finished, so the
reading is whatever had traded by then. Only pass completed days.

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
| `work_list` | stats | `vvalid`, `vvalida` |

`work_list` living on a different server is why `shadowDark` takes two handles
rather than running as one lambda on the order server. `cleandirty.q` splits
the same way, for the same reason.

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
