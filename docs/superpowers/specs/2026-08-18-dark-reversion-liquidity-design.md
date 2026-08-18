# Dark venue liquidity and reversion tiering

Reproduce two tables from the Bernstein Electronic Trading dark pool report
(Dark Pool Report Q2 2026) against our own data, for DARK executions only:

- **Table 3.1 Liquidity** - per dark venue: %Notional, Spread, Adv, Fill%adv,
  Fill Rate, Duration.
- **Table 3.3 Venue tiering/ranking** - per dark venue: Reversion, Stability,
  Score, Tier, from spread normalized 1s reversion and quote stability.

Deliverables: one q script and one Python script.

- `queries/reversion_liquidity/reversion_liquidity.q`
- `queries/reversion_liquidity/venue_tiering.py`

## Source material

Table 3.1 caption, verbatim:

> The following table shows the percentage of notional realized in each venue,
> notional weighted average spread at the time of execution (bps), stock adv in
> million shares, fillsize as a percentage of adv, as well as fill rate as the
> routed notional weighted average of executed size%routed size for every child
> order, and child order duration (seconds).

Table 3.3 caption, verbatim:

> We combine with equal weights z scores for spread normalized 1s reversion and
> % of time we see stability in the quote before vs 1s post-execution. We
> average both metrics of reversion and quote stability into an overall Score.
> Tiering is done through k-means clustering: tiers of venues are created to
> group the most similar venues in term of their overall Score. K-means iterates
> to create clusters which minimize the sum of within-cluster variances, leading
> to an unconflicted way to determine tiers.

Score was confirmed to be an unweighted mean of the two z columns by checking it
against the published figures:

```
MS Pool     ( 0.19 + -0.33) / 2 = -0.07   matches
Centrepoint (-0.15 + -0.32) / 2 = -0.235  matches -0.24
JPMX        (-0.62 + -0.37) / 2 = -0.495  matches -0.50
```

The published z columns do not average to zero across the three rows shown, so
the z base is wider than the displayed venues. We pool across fills.

## Architecture

Three locations. The quote table never leaves its server unfiltered.

```
ho -> ORDER SERVER (historical)
      workorder    : dark child orders -> venue, size, make, timestamps
      execution    : fills on those    -> fillprice, fillsize, sidesign, t_oes_xact
      target_stock : adv, fxlast, country
      returns: fill level rows + a per venue child order roll

hq -> QATT SERVER (historical)
      receives the fill table (date, sym, time, fillprice, sidesign)
      aj against qatt twice: at the fill, and at fill + 1s
      returns: qbid0, qask0, mid0, qbid1, qask1, mid1 per fill

local combines the two, rolls up by venue, writes two CSVs
```

Both hops send serialized lambdas over the handle, matching
`jp_no_print_check_v2.q` and `limit_up_down_v2.q`. `0i` is accepted for either
handle so the script still works when a table happens to be in the local
process.

Signature:

```q
revLiq[ho;hq;d0;d1;country]     / revLiq[ho;hq;2026.04.01;2026.06.30;`AU]
```

country matches `target_stock`.`country`; pass the null symbol for all
countries.

### Why fills go to the quote server

A quarter of AU dark fills is on the order of tens of thousands of rows. A
quarter of qatt is orders of magnitude larger. Shipping the small table to the
big one and returning six columns per fill is the only version of this that
finishes.

## Dark classification

Unchanged from `dark_summary.q` and `dark_routed_executed.q`: a venue is DARK
when `upper venue` matches `*DARK*` or `*DRK*`. Keeping the identical test means
this script agrees with the existing two by construction. The patterns live in
one variable at the top of the function.

## Table 3.1 definitions

Two grains, computed separately and joined on venue. Four columns are per fill,
two are per child order.

| Column | Grain | Definition |
| --- | --- | --- |
| %Notional | fill | `100 * venue notional % total dark notional`, notional = `fillsize*fillprice*fxlast` |
| Spread | fill | notional weighted mean of `10000*(qask0-qbid0)%mid0` |
| Adv | fill | notional weighted mean of `target_stock.adv % 1e6` |
| Fill%adv | fill | notional weighted mean of `100*fillsize%adv` |
| Fill Rate | child order | routed notional weighted mean of `100*make%size` |
| Duration | child order | mean of `t_off_market - t_on_market`, seconds |

Decisions:

- **Quote source is qatt for every quote**, including the spread in 3.1.
  `execution` carries its own bidprice/askprice at the fill, but mixing an OMS
  stamped quote with a qatt stamped quote would manufacture differences out of
  feed disagreement alone. One source throughout. execution bidprice/askprice is
  retained as a cross-check column, not used in any figure.
- **Fill timestamp is `t_oes_xact`**, falling back to `time`. t_oes_xact is the
  exchange transaction time; time is when the row landed in the OMS and would
  smear the +1s lookup by OMS latency.
- **Routed notional weights** for Fill Rate reuse the px_routed rule already in
  `dark_routed_executed.q`: price where positive, else transmit_lastprice.
- **Duration** uses only child orders with both t_on_market and t_off_market
  present. Orders still live at the end of the range have a null t_off_market
  and are excluded rather than counted as zero.
- Adv comes from `target_stock`.`adv` in shares. adv1m and dayadv are the
  alternatives if adv turns out to be the wrong window.

## Table 3.3 definitions

Per fill:

```q
rev:    sidesign * (mid1 - fillprice) % (qask0 - qbid0)
stable: (qbid1 = qbid0) and (qask1 = qask0)
```

- mid1 is the qatt mid 1 second after the fill.
- Positive rev means the price moved our way after the fill, i.e. no adverse
  selection. Higher is better. This is the convention that makes the published
  tiers coherent: MS Pool has the highest Reversion and gets Tier 1.
- stable is strict: both touches unchanged. A venue is charged for any touch
  move in the second after its fill.
- Normalization is by the **full** spread. The caption does not specify;
  half-spread is a one character edit on that line.
- Fills with `qask0 <= qbid0`, a null mid1, or a zero spread are dropped from
  reversion, and counted in a dropped diagnostic rather than silently lost.
- No winsorizing. Spread normalized reversion has a fat tail when the spread is
  one tick; clipping is documented as a one-liner but is not applied by default.

## The kdb / Python boundary

kdb returns **sufficient statistics** per venue, not finished z-scores:

```
venue, n_fills, sum_rev, sumsq_rev, sum_stable
```

Python reconstructs the pooled mean and standard deviation exactly from these,
z-scores, averages per venue, and clusters. This is mathematically identical to
z-scoring in kdb, but the z base is not baked in at query time: a venue can be
excluded and the whole table recomputed without re-running a quarter long query
against the quote server.

Pooled reconstruction:

```
mean  = sum(sum_rev) / sum(n_fills)
var   = sum(sumsq_rev) / sum(n_fills) - mean^2
z_ven = (sum_rev[v]/n[v] - mean) / sqrt(var)
```

and the same for stable, which is a 0/1 variable so sumsq_stable = sum_stable
and only one column is needed.

### q outputs

Two CSVs written to a caller supplied directory:

- `liquidity.csv` - the six columns of Table 3.1, per venue, unrounded.
- `reversion_stats.csv` - venue, n_fills, sum_rev, sumsq_rev, sum_stable.

Rounding happens only in Python, at render time. Nothing downstream inherits a
rounded figure, matching the convention in `dark_summary.q`.

### Python script

1. Read both CSVs.
2. Drop venues below `--min-fills` from the tiering only. This is why the report
   shows fewer venues in 3.3 than 3.1: CLSA and Posit, the two smallest, are
   absent. 3.1 keeps every venue.
3. Reconstruct pooled z-scores, compute Reversion, Stability,
   `Score = (Reversion+Stability)/2`.
4. Tier by exact 1-D k-means via dynamic programming. Optimal clusters on a line
   are contiguous in sorted order, so the exact minimum of within-cluster
   variance is reachable by DP in O(k n^2). Deterministic and dependency free -
   no scikit-learn, no random initialization, no seed to pin.
5. `--tiers auto` selects k by silhouette over `2..min(5, n-1)`; an explicit
   integer overrides.
6. Number tiers by descending mean Score, so Tier 1 is best.
7. Print both tables in the report format and optionally write them out.

Rendering precision, matching the report: %Notional, Spread, Adv, Fill Rate at
1dp; Fill%adv at 2dp; Duration at 0dp; Reversion, Stability, Score at 2dp.

## Testing

The q side has no test harness in this repo, so verification is by
reconciliation against what already exists:

- revLiq executed notional per venue must equal darkRoutedExecuted
  notional_executed for the same single date. Any difference is a bug in the new
  dark filter or the execution/workorder join.
- %Notional must equal darkRoutedExecuted pct_executed for a single date.
- Fill Rate ordering must be consistent with the routed/executed gap: a venue
  taking a much larger share of routed than executed notional must have a lower
  fill rate.

The Python side is unit testable and will be:

- 1-D k-means DP against a brute force enumeration of all contiguous partitions
  on small inputs.
- pooled z reconstruction from sufficient statistics against a direct
  computation over a synthetic fill list.
- the Score arithmetic against the three published Bernstein rows.

## Deliberately not in scope

- Table 3.2, which is not in the supplied images.
- The Vocabulary section benchmark definitions, which are referenced by the
  report but not supplied. Where a definition was ambiguous the choice is
  recorded above and flagged in the script notes.
- Lit venues. Every figure here is a share of the dark book only.
