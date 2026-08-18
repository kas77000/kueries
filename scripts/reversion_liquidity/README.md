# reversion_liquidity

Reproduces two tables from the Bernstein dark pool report against our own data,
for **dark executions only**. Reads the order server and `qatt`, both
historical, through PyKX — it is a Python script rather than a `.q` because the
answers are statistics (pooled z-scores, k-means tiers) rather than query
results.

- **Table 3.1 Liquidity** — per venue: `%Notional`, `Spread`, `Adv`,
  `Fill%adv`, `Fill Rate`, `Duration`
- **Table 3.3 Tiering** — per venue: `Reversion`, `Stability`, `Score`, `Tier`

## Running it

Set the two endpoints once, at the top of the script:

```python
ORDER_SERVER = "CHANGEME:5010"     # workorder, execution, target_stock
QATT_SERVER  = "CHANGEME:5011"     # qatt
```

Both are the **historical** processes, not the realtime ones — `qatt` exists in
both flavours and only the historical one carries a `date` column. Then:

```
pip install pykx
python scripts/reversion_liquidity/reversion_liquidity.py --start 2026-04-01 --end 2026-06-30 --country AU
```

`pykx` unlicensed mode is enough. All q evaluation happens server-side, so no q
licence and no `QHOME` are needed locally.

```
--country      target_stock country, e.g. AU. Blank for all.
--min-fills    minimum usable fills before a venue is TIERED (default 1000)
--tiers        'auto' (silhouette) or an integer k
--half-spread  normalise reversion by half the spread instead of the full spread
--keep-fills   also retain fill level rows (will exhaust memory on a long range)
--out-dir      also write liquidity.csv and tiering.csv
--self-test    run the built-in tests; needs no kdb connection
```

## How the data is produced

Nothing is computed over the whole range at once. The script walks **one date
at a time**, makes two IPC calls per date, reduces that day to per-venue sums,
and throws the fill rows away. Everything after the loop is arithmetic on those
sums.

```
                 for each date d in [start, end]
                 ───────────────────────────────

  ORDER SERVER                          QATT SERVER
  ────────────                          ───────────
  Q_FILLS   ─┐                          Q_QUOTES
  Q_CHILD   ─┘                              ▲ │
      │                                     │ │
      │  fills (one row per fill)  ─────────┘ │
      │                                       │
      │  qbid0 qask0 qbid1 qask1  ◄───────────┘
      ▼
  fill_metrics()      derive notional, spread_bps, rev, stable per fill
      ▼
  aggregate_fills()   group to per-venue SUMS
      ▼
  fold()              add into the running accumulator, drop the fills
                 ───────────────────────────────
                        after the loop
  build_liquidity()   → Table 3.1
  pooled_z()          → Reversion, Stability, Score
  build_tiering()     → Tier
```

### Step 1 — dark child orders and their fills (`Q_FILLS`, order server)

Starts from `workorder`, filtered to the date and to venues whose **upper-cased
name matches `*DARK*` or `*DRK*`**. That is the same test `dark_summary.q` and
`dark_routed_executed.q` use, so all three agree on what "dark" means by
construction rather than by coincidence.

`workorder` is then collapsed to **one row per `id_work`** with `last`, before
anything joins to it. If it already holds one row per child order that grouping
costs nothing; if it ever holds a row per state change, it is the difference
between a correct fill count and a silently multiplied one.

Those child orders are inner-joined to `target_stock` on
`date,id_server,id_target` to pick up `adv`, `fxlast` and `country` — the inner
join is also what applies `--country`, since a stock outside the filter simply
has no row to match.

Finally `execution` is pulled for those `id_work` values with `fillsize>0`, and
joined back to get `venue`, `adv` and `fxlast` onto each fill. The fill
timestamp is:

```q
tm: time^t_oes_xact          / t_oes_xact, falling back to time
```

`t_oes_xact` is the exchange transaction time. `time` is when the row landed in
the OMS, which would smear the +1s lookup by the OMS latency.

**Returns:** `date sym tm venue fillprice fillsize sidesign adv fxlast
bidprice askprice`, one row per fill, sorted by `sym,tm`.

### Step 2 — the child order roll (`Q_CHILD`, order server)

`Fill Rate` and `Duration` are properties of a **child order**, not of a fill,
so they are computed on a separate grain — and since they need no quotes, they
are aggregated on the server and only one row per venue comes back.

```q
px_routed:       transmit_lastprice^?[price>0;price;0n]
notional_routed: size*px_routed*fxlast
fill_pct:        100*make%size
dur:             0.001*"f"$t_off_market-t_on_market
```

`px_routed` is the `dark_routed_executed.q` rule: the price the child was sent
with, falling back to the last trade at transmit time for market and pegged
orders that carry no usable limit.

The weighted mean of `fill_pct` carries **its own weight sum** (`fr_wsum`)
rather than reusing `routed_notional`, so a child order with no usable routed
price cannot sit in a denominator it contributes no numerator to.

### Step 3 — the two quote lookups (`Q_QUOTES`, qatt server)

The fill table is shipped **to** the quote server as a typed q table, rather
than quotes being pulled back. A day of dark fills is thousands of rows; a day
of `qatt` is not. Only four columns come back.

```q
qt: two-sided qatt rows for d and those syms, sorted by sym then time
q0: aj[`sym`time; select sym, time:tm                from f; qt]
q1: aj[`sym`time; select sym, time:tm+00:00:01.000   from f; qt]
```

`aj` returns the last quote at or before the target time — the prevailing
quote. `aj` preserves the left table's row order, which is what lets the four
columns be concatenated straight back onto the fills.

**Returns:** `qbid0 qask0 qbid1 qask1`.

### Step 4 — per-fill derivation (`fill_metrics`)

```
notional   = fillsize * fillprice * fxlast
spread_bps = 10000 * (qask0 - qbid0) / mid0
rev        = sidesign * (mid1 - fillprice) / (qask0 - qbid0)
stable     = (qbid1 == qbid0) and (qask1 == qask0)
```

`rev` is positive when the price moved **our way** after the fill — we bought
and it rose — so higher is better and Tier 1 is the best tier. `stable` is
strict: both touches must be unchanged, so a venue is charged for any touch
move in the second after its fill.

**Two validity flags, not one**, because the two metrics of 3.3 do not have the
same usable population:

| Flag | Meaning | Effect |
| --- | --- | --- |
| `has_quote` | all four touches present and positive | a fill without it is unusable for **both** metrics |
| `good_spread` | `has_quote` **and** the quote is not crossed or locked | a crossed quote has no spread to normalise by, so the fill is dropped from **reversion** — but its touches are still comparable a second later, so it is **kept for stability** |

Columns that must not count a row carry `NaN` there, and every sum downstream
skips `NaN`, so the two populations stay separate without any bookkeeping.
Collapsing them onto one `n` would quietly misweight one of the two z-scores;
`test_populations_are_separate` fails if that ever happens.

### Step 5 — accumulate (`aggregate_fills`, `fold`)

The day is grouped to per-venue sums and added into a running frame. Every
accumulated column is a **plain sum**, so folding a day in is one frame
addition:

```
notional  n_fill                          executed notional and fill count
w_spread  wsum_spread                     weighted numerator and its own weight
w_adv     wsum_adv
w_filladv wsum_filladv
n_rev     sum_rev  sumsq_rev              reversion
n_stable  sum_stable                      stability
no_quote  bad_spread                      diagnostics
```

Each weighted mean keeps its own weight sum for the same reason as step 2 —
`Adv` divides by the notional of the fills that actually had an `adv`, not by
the venue's whole notional.

Sums and sums of squares are **sufficient statistics** for everything both
tables need, including the pooled mean and variance the z-scores use. So
chunking by date costs nothing in accuracy — `test_chunking_is_exact` asserts
that folding day by day gives the same numbers as one pass over the whole
range, down to floating point. Memory stays flat whether you ask for one day or
a quarter, and a failure on day 40 of 60 has not discarded the first 39.

### Step 6 — Table 3.1 (`build_liquidity`)

Pure division of the accumulated sums. This is where the two grains — fill and
child order — finally meet, on venue.

| Column | From |
| --- | --- |
| `%Notional` | `100 * notional / notional.sum()` |
| `Spread` | `w_spread / wsum_spread` |
| `Adv` | `w_adv / wsum_adv` |
| `Fill%adv` | `w_filladv / wsum_filladv` |
| `Fill Rate` | `fr_wnum / fr_wsum` (child order) |
| `Duration` | `duration_sum / duration_n` (child order) |

### Step 7 — the z-scores (`pooled_z`)

Each venue is scored against the distribution pooled over **every dark fill in
the range**, not against the other venues:

```
mean = sum(sum_rev) / sum(n_rev)
var  = sum(sumsq_rev) / sum(n_rev) - mean²
Reversion[v] = (sum_rev[v]/n_rev[v] - mean) / sqrt(var)
```

`stable` is 0/1, so its pooled variance is `p(1-p)` and it needs no sum of
squares. Then, with equal weights as the report specifies:

```
Score = (Reversion + Stability) / 2
```

Pooling across fills is what reproduces the report's magnitudes: a heavy venue
regresses toward zero, a light one does not. Scoring venues against each other
instead would force the column to average to zero, which the published table
does not do.

### Step 8 — tiering (`build_tiering`)

Venues below `--min-fills` are dropped **from the tiering only** — 3.1 keeps
every venue. That is why the report shows fewer venues in 3.3 than in 3.1.

Tiers come from **exact 1-D k-means by dynamic programming**, not Lloyd's
algorithm. Optimal clusters on a line are contiguous in sorted order, so the
true minimum of within-cluster variance is reachable by DP in O(k·n²) — and n
is a handful of venues. No random initialisation, no seed, and no chance of two
runs on the same scores disagreeing about the tiers.
`test_kmeans_matches_brute_force` checks it against brute-force enumeration of
every contiguous partition.

`--tiers auto` picks k by silhouette over `2..min(5, n-1)`. Fewer than three
venues cannot be clustered meaningfully, so they all land in Tier 1 rather than
being split on noise. Tiers are then numbered by **descending mean Score**, so
Tier 1 is the best group.

## What gets excluded, and where to see it

The run prints a footer per venue:

```
Venue      n_fill  no_quote  bad_spread  % no quote  % crossed
```

`no_quote` had no usable quote at the fill or a second later. `bad_spread` had
a crossed or locked quote — dropped from reversion, still counted for
stability. Nothing is winsorized and nothing is silently discarded, so a venue
whose numbers rest on a small surviving fraction of its fills is **visible**
rather than merely plausible. Check this before reading anything into a venue's
Reversion.

## Verifying it

```
python scripts/reversion_liquidity/reversion_liquidity.py --self-test
```

Everything except the three q constants is pure Python and covered offline —
the clustering against brute force, the chunking equivalence, the two separate
fill populations, the weighted-mean denominators, and the Score arithmetic
against the three published Bernstein rows. No kdb connection required, which
matters because this is written on a machine that has none.

The q half cannot be unit tested here, so it is checked by **reconciliation**
against what already exists. For a single date:

- executed notional per venue must equal `darkRoutedExecuted`'s
  `notional_executed`
- `%Notional` must equal its `pct_executed`
- a venue taking a much larger share of routed than executed notional must show
  a lower `Fill Rate`

Any disagreement is a bug in the dark filter or the `execution`↔`workorder`
join, not a definitional difference.

## Where the judgement calls are

The report's Vocabulary section was not supplied, so several definitions are
read from the table captions alone. All eight are listed in the notes block at
the bottom of the script, each with the one-line change that reverses it. The
two most likely to bite:

- **Full vs half spread.** "Spread normalized" does not say which; this divides
  by the full spread. If our figures come out at consistently half or double
  the published ones, try `--half-spread` first — it is by far the likeliest
  single cause of a factor of two, which is why it is a flag and not an edit.
- **The +1s lookup takes the prevailing quote**, so its fill-time end can pick
  up a quote stamped in the same millisecond as the fill, which may already be
  reacting to it. `tm-00:00:00.001` in `Q_QUOTES` gives the strictly-before
  variant.

Run one date and compare magnitudes against the report page before trusting a
whole quarter.

Design rationale, and why each ambiguous definition was resolved the way it
was, is in
[`docs/superpowers/specs/2026-08-18-dark-reversion-liquidity-design.md`](../../docs/superpowers/specs/2026-08-18-dark-reversion-liquidity-design.md).
