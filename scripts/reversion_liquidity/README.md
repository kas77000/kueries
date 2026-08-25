# reversion_liquidity

Reproduces two tables from the Bernstein dark pool report against our own data,
for **dark executions only**. Reads the order server and `qatt`, both
historical, through PyKX — it is a Python script rather than a `.q` because the
answers are statistics (pooled z-scores, k-means tiers) rather than query
results.

- **Table 3.1 Liquidity** — per venue: `%Notional`, `Spread`, `Adv`,
  `Fill%adv`, `Fill Rate`, `Duration`
- **Table 3.3 Tiering** — per venue: `Reversion`, `Stability`, `Score`, `Tier`
- **Reversion decomposition** (`--decompose`, ours not theirs) — per venue:
  `Capture`, `Drift`, `Reversion`, `n`

The tables print to stdout, and can also be written to a workbook (`--out-dir`)
or typeset as a PDF (`--pdf`). See [Outputs](#outputs).

## Running it

Set the two endpoints once. They ship as placeholders at the top of the script:

```python
ORDER_SERVER = "CHANGEME:5010"     # workorder, execution, target_stock
QATT_SERVER  = "CHANGEME:5011"     # qatt
```

but the place to put the real ones is a **`local_settings.py` beside this
script**, which git ignores — see [Local settings](#local-settings) below, and
[`scripts/lib/README.md`](../lib/README.md). Editing the script itself means the
file you run is never the file in git.

Both are the **historical** processes, not the realtime ones — `qatt` exists in
both flavours and only the historical one carries a `date` column. Then:

```
pip install pykx
python scripts/reversion_liquidity/reversion_liquidity.py --start 2026-04-01 --end 2026-06-30 --country AU
```

`pykx` unlicensed mode is enough. All q evaluation happens server-side, so no q
licence and no `QHOME` are needed locally.

```
--country      market, matched against the SYM SUFFIX: AU for *.AU, JP for
               *.JP. Case insensitive. Blank for all.
--min-venue-fills
               a venue with fewer fills than this gets no row in table 3.1 at
               all, and its notional leaves %Notional (default 2000). 0 keeps
               every venue.
--min-fills    minimum QUOTED fills before a venue already in 3.1 is also
               TIERED in 3.3 (default 1000). Does not affect 3.1.
--tiers        'auto' (silhouette) or an integer k
--half-spread  normalise reversion by half the spread instead of the full spread
--decompose    also show Reversion split into Capture and Drift
--keep-fills   also retain fill level rows (will exhaust memory on a long range)
--out-dir      also write report.xlsx here, one sheet per table
--pdf          also typeset the tables to this .pdf
--diagnose     query the FIRST date only and show where its rows are lost,
               stage by stage; use when a range reports nothing
--quiet        no per-date progress on stderr; the report still prints
--self-test    run the built-in tests; needs no kdb connection
```

Progress goes to **stderr**, one line per date, and is on by default — a run
that makes two IPC calls per date should not look identical to one that has
hung. The report goes to stdout, so `> out.txt` keeps the two apart.

### When a range reports nothing

`no dark fills across N dates` means every date came back empty, and the useful
question is *which filter emptied it*. Re-run the same command with
`--diagnose`:

```
  workorder_rows        482,913
  dark_venue_rows        30,514     6.3% of previous
  of_those_filled        28,880    94.6% of previous
  after_country               0     0.0% of previous   <- everything dropped here
  stock_rows_found            0

  markets on 2026-04-01, by dark child orders - the SYM SUFFIX, which is what
  --country matches:
    JP    610      HK    240      SG     74

  --country AU is not among them, which is why the range came back empty.
  It is matched against the end of the sym - 7203.JP is JP - and nothing else.
```

`after_country` is the only filter that can empty a market. `stock_rows_found`
is last and cannot: `target_stock` is left-joined, so a parent it does not
carry costs you `adv` and `fxlast` on those rows and nothing else.

## The venue sheet

A row of these tables is a **pool**, not a kdb symbol. `VENUE_GROUPS`, near the
top of the script, is what says which is which — it maps **(country, kdb
venue)** onto **(name for the tables, short name for the pies)**:

```python
("AU", "CENTREPOINT_DARK"):      ("Centrepoint", "CentrePt"),
("AU", "CENTREPOINT_CITI_DARK"): ("Centrepoint", "CentrePt"),
```

The report's table 3.1 has one **Centrepoint** row at 88.6% where our
`workorder` table has `CENTREPOINT_DARK` for one route into the pool and
`CENTREPOINT_CITI_DARK` for another. Every figure in both tables — notional,
spread, reversion, tier — is computed on the **group**, so the two arrive as
one row rather than two half-sized ones.

The key is a **pair** because the sheet is keyed that way: `JPMAP_DARK` is JPMX
in JP and in HK, while in AU the same pool is reached as `JPMAP_MF_DARK`. A
venue-name-only table could not say that.

The country in that key is the **sym suffix** — `7203.JP` is JP, `BHP.AU` is AU
— derived in the q from the `sym` already on the row, and carried through to
Python so `Q_CHILD` can aggregate `by country,venue`. It is **not**
`target_stock.country`; see [Which market a row belongs
to](#which-market-a-row-belongs-to).

The sheet carries **41 routes** across the three markets — several per pool,
because a pool is reached under more than one kdb name (`CITI_DARK` and
`CITI_DARK_PASS` in HK are both Citi) and because the same name means different
things in different markets.

The second name is a pie label and is unused here; it belongs to
[`scripts/dark_routed_executed`](../dark_routed_executed/README.md), which
carries **its own copy of this same sheet**. Each script folder stands on its
own, so **a new venue has to be added in both.**
`test_venue_sheet_is_consistent` in each script checks its own copy's shape —
one short code per name, every venue actually dark — but nothing checks the two
copies against each other.

> The published pie labels the Centrepoint slice `Ctrpnt`; the sheet says
> `CentrePt`, and the sheet is what this follows.

### A venue that is not in the sheet

Keeps its raw kdb symbol as its row label, and is named on **stdout**, above
the tables, so `--quiet` cannot hide it:

```
  2 venue(s) are not in VENUE_GROUPS, so they keep their raw kdb name below.
  Add them to the sheet near the top of this script to group them:
    ("AU", "SOME_NEW_DARK"):
    ("HK", "ANOTHER_DRK"):
```

Dropping it instead would merge it by guesswork or hide it; under its own
`ALL_CAPS` name it is visible, in the right total, and obviously asking to be
added. It is **not exempt from `--min-venue-fills`** — a stray venue thin enough
to be under the cut is filtered like any other, and named there instead.

## Local settings

Everything above the `apply_local(globals(), __file__)` line near the top of the
script — the two servers, and `VENUE_GROUPS` — can be overridden from an
untracked file beside it:

```python
# scripts/reversion_liquidity/local_settings.py     (git ignores it)
ORDER_SERVER = "prod-oms-hist:5010"
QATT_SERVER  = "prod-qatt-hist:5011"
```

A template with everything commented out is already there. Uncomment what you
need; anything left commented keeps the placeholder, and a placeholder server
fails loudly on `connect()` rather than half-running.

`git pull` is then always clean and the settings survive it. On startup the
script prints **which** names it took and never the values:

```
  local_settings.py: ORDER_SERVER, QATT_SERVER
```

**Strict on purpose.** A name the script does not define is an **error**, not a
new setting — `QATT_SERVR` with a missing letter would otherwise sit there doing
nothing while the run went on reading the placeholder. A broken settings file
names itself and stops.

`VENUE_GROUPS` can be set here too, but normally should not be: given locally it
**replaces the whole dict**, so a partial copy silently unmaps every venue left
out of it, and the sheet is tracked precisely so this script and
`dark_routed_executed` name the same pool the same way. An unmapped venue is not
a crash — see [A venue that is not in the sheet](#a-venue-that-is-not-in-the-sheet).

`apply_local` sits **above** everything derived from the sheet, so a sheet set
here is the one the row labels and the groupings are built from.

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
  build_liquidity()      → Table 3.1
  pooled_z()             → Reversion, Stability, Score
  build_tiering()        → Tier
  build_decomposition()  → Capture, Drift          (--decompose)
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

The market is then taken off the **sym suffix** and `--country` is applied
there, on `workorder`'s own rows, before anything is joined:

```q
w:$[count w; update country:`$upper {last "." vs x} each string sym from w;
             update country:`symbol$() from w];
w:$[0=count ctry; w; select from w where country=`$upper ctry];
```

Those child orders are then **left**-joined to `target_stock` on
`date,id_server,id_target` for `adv` and `fxlast`. `lj`, not `ij`: the market is
already decided, so the stock table is a source of two numbers and not a vote on
which rows exist. `country` reaches Python because the venue sheet is keyed on
`(country, venue)`.

Finally `execution` is pulled for those `id_work` values with `fillsize>0`, and
joined back to get `venue`, `adv` and `fxlast` onto each fill. The fill
timestamp is:

```q
tm: time^t_oes_xact          / t_oes_xact, falling back to time
```

`t_oes_xact` is the exchange transaction time. `time` is when the row landed in
the OMS, which would smear the +1s lookup by the OMS latency.

**Returns:** `date sym tm venue country fillprice fillsize sidesign adv fxlast
bidprice askprice`, one row per fill, sorted by `sym,tm`.

#### Which market a row belongs to

**The sym suffix, and nothing else.** `7203.JP` is JP, `0005.HK` is HK, `BHP.AU`
is AU. `queries/market_stats/market_stats.q` names a market the same way.

`target_stock` has a `country` column and this script does not read it. It was
read, once: `--country JP` returned nothing for a whole quarter while the JP
dark fills sat in `workorder` the entire time, and the same range came back
correct for AU. A column that is right for one market and blank or different for
the next cannot decide which rows a report contains.

Three tests hold the line — `test_the_market_is_the_sym_suffix` (all four
lambdas derive it identically), `test_no_query_reads_target_stock_country` (no
`select ... from target_stock` pulls that column), and
`test_target_stock_cannot_delete_a_fill` (the join stays `lj`).

Matching is case-insensitive on both sides, so `--country jp` is `--country JP`
rather than a silently empty report.

### Step 2 — the child order roll (`Q_CHILD`, order server)

`Fill Rate` and `Duration` are properties of a **child order**, not of a fill,
so they are computed on a separate grain — and since they need no quotes, they
are aggregated on the server and only one row per `(country, venue)` comes
back.

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

Because the roll comes back one row per `(country, venue)`, a group built out
of two symbols arrives as **two rows**, and `aggregate_child` sums them.
Indexing on the group instead would keep whichever row landed last and halve
the group's orders, its routed notional and the weights under its fill rate —
`test_child_rows_sum_within_a_group` is there to catch that.

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

The day is grouped to per-**pool** sums — the venue sheet is applied here, on
the way in — and added into a running frame. Every
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
child order — finally meet, on the pool.

| Column | From |
| --- | --- |
| `%Notional` | `100 * notional / notional.sum()`, **over the rows shown** |
| `Spread` | `w_spread / wsum_spread` |
| `Adv` | `w_adv / wsum_adv` |
| `Fill%adv` | `w_filladv / wsum_filladv` |
| `Fill Rate` | `fr_wnum / fr_wsum` (child order) |
| `Duration` | `duration_sum / duration_n` (child order) |

### Which venues get a row (`thin_venues`, `--min-venue-fills`)

The report's tables carry fewer venues than we accumulate, and the cut it makes
is on **size**, not on which venue it is: JP publishes LNAL at 5.3% of notional
while HK, where the same pool is thinner, leaves it out entirely. So it is not
a fixed exclusion list.

It is not `%Notional` either, and JP is what rules that out. HK needs a cut
**above 3.0%** to lose LNAL; JP needs one **at or below 2.1%** to keep Posit.
No single number is both.

**Fill count** separates them, and one threshold covers all three markets. Per
market, the biggest venue the report drops against the smallest one it keeps,
in our own fill counts for 2026-04-01..06-30:

| Market | Biggest dropped | Smallest kept | Threshold must be in |
| --- | --- | --- | --- |
| JP | LNAL Cond, 89 | LNAL, 3,108 | 89 < T ≤ 3,108 |
| AU | CBOE, 334 | MS Pool, 5,228 | 334 < T ≤ 5,228 |
| HK | LNAL, 1,639 | CLSA, 15,683 | 1,639 < T ≤ 15,683 |

All three intersect at **1,639 < T ≤ 3,108**, and the `2000` default sits
inside it with room on both sides. It reproduces every published venue list
exactly — 7 venues in JP, 5 in AU, 6 in HK.
`test_thin_cut_reproduces_the_published_venue_lists` pins those counts, so if a
later change to what counts as a fill moves them, it fails there rather than
silently in a table someone sends out.

A count is also the sturdier thing to threshold on **while our notionals and
theirs still disagree** — HK Posit is 10.3% for us against their 17.2% — because
it is a count of fills rather than a money-weighted figure, so it does not move
with whatever is causing that gap. Closing the gap itself is a separate problem
and this cut does not address it.

Two consequences worth knowing:

- The dropped venue's notional **leaves the denominator**, so `%Notional` over
  the rows shown sums to 100, as the report's does. Pass `--min-venue-fills 0`
  to keep every venue and get a share of all dark flow instead.
- A venue with no row in 3.1 gets none in 3.3 either. That is a different cut
  from `--min-fills`, which narrows 3.3 alone.

Both are printed, and the excluded-fills footer still reports **every** venue,
so nothing disappears without saying so.

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

Venues below `--min-fills` are dropped **from the tiering only** — a venue 3.1
carries can still be too thinly *quoted* to score. That is why the report shows
fewer venues in 3.3 than in 3.1: AU publishes five venues in 3.1 and tiers only
three. This is a different cut from `--min-venue-fills`, which decides what 3.1
carries in the first place — see below.

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

## The reversion decomposition (`--decompose`)

Reversion answers two questions at once, and the single published number cannot
tell them apart. Add and subtract the fill-time mid and it separates:

```
rev = sidesign*(mid1 - fillprice)/spread

    = sidesign*(mid0 - fillprice)/spread     Capture — the price I got
    + sidesign*(mid1 - mid0)/spread          Drift   — where it went next
```

`Capture` is a property of **the fill**: 0 at mid, +0.5 at the passive touch,
−0.5 at the aggressive one. `Drift` is a property of **what the market did in
the second afterwards** — leakage. A venue can post a respectable Reversion by
pricing well while leaking, or by pricing badly and not leaking, and those two
call for opposite responses.

```
Reversion decomposition: Capture + Drift = Reversion, per fill, in spreads

             Capture   Drift  Reversion       n
Centrepoint    0.008  -0.011     -0.003  61,043
MS Pool        0.021  -0.004      0.017  84,201
JPMX          -0.002  -0.014     -0.016  23,118
```

Both halves are masked and divided exactly as `rev` is, so the identity holds
row by row, venue by venue, and under `--half-spread` too — that is what
`test_decomposition_adds_up` pins.

This **extends** the report rather than reinterpreting it: table 3.3 is
untouched, and the tiering still runs on Reversion alone. Every venue appears
here, thin ones included, with `n` alongside so a two-fill venue at the top of
the sort is visible as one.

## Outputs

**Row order.** Every table is sorted on the column it exists to show, biggest
first — 3.1 by `%Notional`, 3.3 by `Score` (so the tiers come out contiguous,
Tier 1 at the top), the decomposition by `Reversion`, and the excluded-fills
footer by fill count. None of them is alphabetical: with fifteen pools on the
sheet, a name-ordered table is a lookup table — every row has to be read before
the shape of the flow is visible. Venues that tie fall back to name order, so a
run is reproducible. `test_the_tables_are_ordered_biggest_first` pins it. The
workbook holds numbers rather than strings, so any of it can be re-sorted in
Excel.

Everything goes to stdout by default. The two file outputs carry the same
numbers through the same format specs, so they can only differ from the
terminal in presentation.

**`--out-dir DIR`** writes `report.xlsx`, one sheet per table — `Liquidity`,
`Tiering`, `Excluded`, and `Decomposition` when `--decompose` is on. Cells hold
**numbers, not the rendered strings**, so the workbook can be sorted and
charted; Excel applies the display rounding. Needs `openpyxl` (or
`xlsxwriter`), imported only when the flag is used.

This **replaces** the old `liquidity.csv` / `tiering.csv`. `--keep-fills` still
writes `fills.csv` alongside, deliberately: a quarter of dark fills runs past
Excel's 1,048,576-row ceiling, and a sheet truncates there without saying so.

**`--pdf PATH`** typesets the tables the way the report typesets them — set in
Computer Modern (matplotlib ships `cmr10`), booktabs rules, numerics right
aligned, venue column left. The page holds **the tables and nothing else**: no
title, no caption, no letterhead. Column widths come from the widest cell, and
a table too wide for the page is scaled down to fit rather than losing its
right-hand columns off the edge. Needs `matplotlib`, imported only when the
flag is used.

One catch worth knowing about, because it is silent. `cmr10` is a TeX font and
carries the OT1 encoding with it: it renders `_` as a raised dot, `{}` as
dashes, `\` as an opening quote. A glyph exists in each case, so nothing warns
— `ASX_CENTREPOINT_DARK` just arrives on the page with dots in it. Venue names
are kdb symbols and routinely contain underscores, so the writer checks every
string it is about to set and moves the **whole document** to DejaVu Serif if
any character would be mis-mapped:

```
  pdf: Computer Modern mis-maps _ (TeX OT1 encoding), so the tables are set in DejaVu Serif
```

The right font with the wrong venue names is the worse trade. If you want the
Computer Modern look back, the venue names have to lose their underscores.

Not reproduced: the publisher's masthead. Our numbers under someone else's
letterhead is a forgery the moment the file leaves the desk, and the tables
were the part worth matching.

## Verifying it

```
python scripts/reversion_liquidity/reversion_liquidity.py --self-test
```

31 tests. Everything except the q itself is pure Python and covered
offline — the clustering against brute force, the chunking equivalence, the two
separate fill populations, the weighted-mean denominators, the venue grouping,
and the Score arithmetic against the three published Bernstein rows. No kdb
connection required, which matters because this is written on a machine that
has none.

Two hold what a **null number** must do — a q null makes PyKX return a masked
array where the same column without one is a plain ndarray, and a mask reaching
`fill_metrics` is `TypeError: bad operand type for unary ~: 'float'` out of
pandas internals:

- `test_a_null_number_arrives_as_nan` — nothing downstream of `_to_pandas` ever
  sees a mask or a nullable extension dtype
- `test_a_null_adv_is_dropped_from_adv_not_from_the_row` — a fill on a name with
  no ADV still counts for `%Notional`; it just cannot count for `Adv` or
  `Fill%adv`

Four hold the market rule, and they fail on the version of this script that
read `target_stock.country` — that is what they are for:

- `test_the_market_is_the_sym_suffix` — all four lambdas derive the market from
  `sym` with the same line
- `test_no_query_reads_target_stock_country` — no `select ... from target_stock`
  anywhere pulls that column
- `test_target_stock_cannot_delete_a_fill` — the stock join is `lj`, never `ij`
- `test_country_is_normalised_before_it_reaches_q` — `jp` and `JP` are one
  request

The six that cover the venue sheet:

- `test_venue_sheet_is_consistent` — one short code per name, every venue
  actually dark, every country a bare upper-case code
- `test_the_sheet_has_no_duplicate_keys` — reads the **source**, because a dict
  literal keeps the last of a repeated key and says nothing; the losing line is
  gone before any test built on `VENUE_GROUPS` could see it
- `test_venues_in_one_group_become_one_row` — both Centrepoint symbols land in
  one row whose notional is the sum of both
- `test_the_sheet_is_keyed_on_country` — `JPMAP_DARK` maps in JP and HK but not
  in AU, and `JPMAP_MF_DARK` the other way round
- `test_unmapped_venue_keeps_its_kdb_name` — an unknown venue keeps its symbol,
  stays in `%Notional`, and is reported
- `test_child_rows_sum_within_a_group` — the two-row child roll sums instead of
  overwriting, checked through `Fill Rate` and `Duration`

The q half cannot be unit tested here, so it is checked by **reconciliation**
against what already exists. For a single date:

- executed notional per venue must equal `darkRoutedExecuted`'s
  `notional_executed` — summed over the symbols in a pool, since the q does not
  group
- `%Notional` must equal its `pct_executed`, to a rounding: the q values a fill
  as `make*avg_fill_price` per child order where this values it as
  `fillsize*fillprice` per execution
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
