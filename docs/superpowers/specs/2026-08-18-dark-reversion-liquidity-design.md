# Dark venue liquidity and reversion tiering

Reproduce two tables from the Bernstein Electronic Trading dark pool report
(Dark Pool Report Q2 2026) against our own data, for DARK executions only:

- **Table 3.1 Liquidity** - per dark venue: %Notional, Spread, Adv, Fill%adv,
  Fill Rate, Duration.
- **Table 3.3 Venue tiering/ranking** - per dark venue: Reversion, Stability,
  Score, Tier, from spread normalized 1s reversion and quote stability.

Deliverable: one Python script driving two kdb connections through PyKX.

- `scripts/reversion_liquidity/reversion_liquidity.py`

The q lambdas live as commented module level string constants inside that file.
PyKX sends them as source text plus **typed arguments** - dates and symbols stay
serialized q values and are never interpolated into the text - which is the same
contract as sending a lambda over a raw handle.

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

Two PyKX connections, and a loop over dates. The quote table never leaves its
server unfiltered.

```
for each date d in [d0, d1]:

  ho -> ORDER SERVER (historical)          Q_FILLS, Q_CHILD
        workorder    : dark child orders -> venue, size, make, timestamps
        execution    : fills on those    -> fillprice, fillsize, sidesign, t_oes_xact
        target_stock : adv, fxlast, country
        returns: dark fills for d, and a per venue child order roll for d

  hq -> QATT SERVER (historical)           Q_QUOTES
        receives the fill keys (sym, tm) as a typed q table argument
        aj against qatt twice: at the fill, and at fill + 1s
        returns: qbid0, qask0, qbid1, qask1 per fill, in input row order

  local: derive per fill metrics, fold into per venue accumulators
         discard the fill rows

after the loop: z-score, roll up, cluster, render
```

PyKX runs in **unlicensed mode** - `SyncQConnection` against a remote process
needs no q licence and no `QHOME`, because all q evaluation happens on the
server. Only `pykx`, `pandas` and `numpy` are required.

### CLI

```
python scripts/reversion_liquidity/reversion_liquidity.py \
    --start 2026-04-01 --end 2026-06-30 --country AU \
    [--min-fills 1000] [--tiers auto] [--out-dir DIR] [--half-spread]
```

The two host:port pairs are fixed for the life of the script, so they are
`ORDER_SERVER` and `QATT_SERVER` constants at the top of the file rather than
arguments - along with `USER` and `PASSWORD`, which stay `None` for an open
process. They ship holding a `CHANGEME` placeholder that `connect` refuses,
naming the file to edit, so an unset constant fails immediately and legibly
instead of timing out. `--self-test` additionally checks that an edited
constant still parses as host:port, since a typo would otherwise only surface
as a connection failure on the machine that actually runs this.

`--country` matches `target_stock`.`country`; omit it for all countries.
Both tables are printed; `--out-dir` also writes `liquidity.csv` and
`tiering.csv`.

### Why the date loop, and why accumulators

The fill rows have to reach Python anyway, because Python is what ships them to
the quote server. That removes the need for a kdb/Python statistics boundary -
Python has the fills, so it z-scores them directly.

What it does not remove is the volume problem. A quarter of dark fills held in
one DataFrame, plus its quote join, is an unbounded memory bet on a range the
user chooses at the command line. So the script processes **one date at a time**
and folds each date into per venue running sums:

```
n_rev, sum_rev, sumsq_rev            reversion
n_stable, sum_stable                 stability
notional, w_spread, w_adv, w_filladv fill level weighted sums for 3.1
routed_notional, w_fillrate          child order weighted sums for 3.1
duration_sum, duration_n             child order durations
```

Every figure in both tables is recoverable from these, including the pooled
mean and variance the z-scores need, because sums and sums of squares are
sufficient statistics for both. Memory is then flat in the length of the date
range, and a failure on day 40 of 60 has not thrown away the first 39.

`--keep-fills` retains the fill level frame as well, for ad hoc work. Off by
default, and documented as the thing that will exhaust memory on a long range.

## Dark classification

Unchanged from `dark_summary.q` and `dark_routed_executed.q`: a venue is DARK
when `upper venue` matches `*DARK*` or `*DRK*`. Keeping the identical test means
this script agrees with the existing two by construction. The patterns live in
one variable at the top of the q constant.

## Table 3.1 definitions

Two grains, accumulated separately and joined on venue at the end. Four columns
are per fill, two are per child order.

| Column | Grain | Definition |
| --- | --- | --- |
| %Notional | fill | `100 * venue notional / total dark notional`, notional = `fillsize*fillprice*fxlast` |
| Spread | fill | notional weighted mean of `10000*(qask0-qbid0)/mid0` |
| Adv | fill | notional weighted mean of `target_stock.adv / 1e6` |
| Fill%adv | fill | notional weighted mean of `100*fillsize/adv` |
| Fill Rate | child order | routed notional weighted mean of `100*make/size` |
| Duration | child order | mean of `t_off_market - t_on_market`, seconds |

Decisions:

- **Quote source is qatt for every quote**, including the spread in 3.1.
  `execution` carries its own bidprice/askprice at the fill, but mixing an OMS
  stamped quote with a qatt stamped quote would manufacture differences out of
  feed disagreement alone. One source throughout. execution bidprice/askprice is
  returned as a cross-check column, not used in any figure.
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
- The child order roll is aggregated **on the order server**, since it needs no
  quotes. Only one row per venue per date comes back.

## Table 3.3 definitions

Per fill:

```
rev    = sidesign * (mid1 - fillprice) / (qask0 - qbid0)
stable = (qbid1 == qbid0) and (qask1 == qask0)
```

- mid0, mid1 are the qatt mids at the fill and 1 second after.
- Positive rev means the price moved our way after the fill, i.e. no adverse
  selection. Higher is better. This is the convention that makes the published
  tiers coherent: MS Pool has the highest Reversion and gets Tier 1.
- stable is strict: both touches unchanged. A venue is charged for any touch
  move in the second after its fill.
- Normalization is by the **full** spread. The caption does not specify;
  `--half-spread` switches it, since this is the single most likely reason for a
  factor of two disagreement against the published numbers.
- **The two metrics have different usable populations, so they are counted
  separately.** A fill with no quote at all is unusable for both. A fill with
  `qask0 <= qbid0` (crossed, locked, or one sided) has no meaningful spread to
  normalize by, so it is dropped from reversion - but its touches are still
  comparable a second later, so it is kept for stability. Collapsing both onto a
  single n would silently misweight one of the two z-scores, so the accumulators
  carry `n_rev` and `n_stable` distinctly.
- Fills excluded from either metric are counted per venue in a `dropped` frame
  (`venue, no_quote, bad_spread`) and printed as a footer, so a venue whose
  numbers rest on a small surviving fraction of its fills is visible rather than
  merely plausible.
- No winsorizing. Spread normalized reversion has a fat tail when the spread is
  one tick; clipping is documented as a one-liner but is not applied by default.

### The two aj lookups

`aj` returns the last quote at or before the target time, which is the
prevailing quote - the right semantics for both ends.

```q
q0:aj[`sym`time; select sym, time:tm       from f; qt]
q1:aj[`sym`time; select sym, time:tm+00:00:01.000 from f; qt]
```

`qt` is filtered to two sided rows (`qbid>0, qask>0`) for the date and the syms
actually traded, and must be sorted by sym then time for `aj` to be valid.

One subtlety recorded rather than hidden: the fill-time lookup will pick up a
quote stamped in the *same millisecond* as the fill, which may already be
reacting to it. `tm-00:00:00.001` gives the strictly-before variant. Full
millisecond resolution makes this a small effect, and the prevailing-quote
reading is what "spread at the time of execution" means in 3.1, so the same
lookup serves both tables.

## Tiering

1. Drop venues below `--min-fills` from the tiering only. This is why the report
   shows fewer venues in 3.3 than 3.1: CLSA and Posit, the two smallest, are
   absent. 3.1 keeps every venue.
2. Pooled z-scores from the accumulators, then `Score = (Reversion+Stability)/2`.
3. Tier by exact 1-D k-means via dynamic programming. Optimal clusters on a line
   are contiguous in sorted order, so the exact minimum of within-cluster
   variance is reachable by DP in O(k n^2). Deterministic and dependency free -
   no scikit-learn, no random initialization, no seed to pin, and no chance of
   two runs on the same data disagreeing about the tiers.
4. `--tiers auto` selects k by silhouette over `2..min(5, n-1)`; an explicit
   integer overrides.
5. Number tiers by descending mean Score, so Tier 1 is best.

Rendering precision, matching the report: %Notional, Spread, Adv, Fill Rate at
1dp; Fill%adv at 2dp; Duration at 0dp; Reversion, Stability, Score at 2dp.

## Testing

Everything except the two q constants is pure Python and unit tested, with no
kdb connection required:

- 1-D k-means DP against brute force enumeration of all contiguous partitions on
  small inputs, including ties and duplicate scores.
- pooled z reconstruction from accumulators against a direct computation over a
  synthetic fill list - the property that matters is that chunking by date gives
  bit-comparable results to one pass over the whole range.
- the Score arithmetic against the three published Bernstein rows.
- rev and stable derivation against hand-built fills, including the crossed
  quote case that must count for stability but not reversion.

The q side cannot be unit tested here, so it is verified by reconciliation
against what already exists:

- executed notional per venue must equal `darkRoutedExecuted` notional_executed
  for the same single date. Any difference is a bug in the new dark filter or
  the execution/workorder join.
- %Notional must equal `darkRoutedExecuted` pct_executed for a single date.
- Fill Rate ordering must be consistent with the routed/executed gap: a venue
  taking a much larger share of routed than executed notional must have a lower
  fill rate.

## Deliberately not in scope

- Table 3.2, the distribution of executed size%adv. Confirmed unrelated to
  these two tables and out of scope.
- The Vocabulary section benchmark definitions, which are referenced by the
  report but not supplied. Where a definition was ambiguous the choice is
  recorded above and flagged in the script notes.
- Lit venues. Every figure here is a share of the dark book only.
