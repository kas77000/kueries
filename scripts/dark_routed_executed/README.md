# dark_routed_executed

Our **dark** activity split into what we **routed** and what actually
**executed**, per venue, over a date range — plus the two pie charts the
Bernstein report draws from it.

| column | meaning |
| --- | --- |
| `Routed %` | share of dark notional we **sent** to each venue |
| `Executed %` | share of dark notional that came **back** from each venue |
| `Fill Rate` | executed / routed, money weighted, per venue |
| `Orders` / `Filled` | the order-weighted version of the same thing |
| `Routed $m` / `Executed $m` | the notionals the percentages come from |

The gap between the two percentages is the point. A venue taking 47% of the
flow and returning 88% of the fills is a different venue from one doing 6% and
4% — and that pair of numbers, side by side as two pies, is the whole slide.

Reads **one** kdb process: the historical order server (`workorder`,
`target_stock`). No quotes are involved, so `qatt` is not needed.

## Running it

Set the endpoint once. It ships as a placeholder at the top of the script:

```python
ORDER_SERVER = "CHANGEME:5010"     # workorder, target_stock
```

but the place to put the real one is a **`local_settings.py` beside this
script**, which git ignores — see [Local settings](#local-settings) below, and
[`scripts/lib/README.md`](../lib/README.md). Editing the script itself means the
file you run is never the file in git.

The **historical** process, not the realtime one. Then:

```
pip install pykx matplotlib
python scripts/dark_routed_executed/dark_routed_executed.py --start 2026-04-01 --end 2026-06-30 --country AU --out-dir out
```

`pykx` unlicensed mode is enough. All q evaluation happens server-side, so no q
licence and no `QHOME` are needed locally. `matplotlib` is only needed to draw;
`--no-pies` writes the CSVs without it.

```
--country       target_stock country, e.g. AU. Blank for all.
--other-below   pie slices under this % roll into one Other slice (default 3.0);
                0 keeps every venue as its own slice
--out-dir       write the table, routed.csv, executed.csv and pies.png/.pdf here
--no-pies       write the CSVs but do not draw
--diagnose      query the FIRST date only and show where its rows are lost,
                stage by stage; use when a range reports nothing
--quiet         no per-date progress on stderr; the report still prints
--self-test     run the built-in tests; needs no kdb connection
```

Progress goes to **stderr**, one line per date, and is on by default. The
report goes to stdout, so `> out.txt` keeps the two apart.

### When a range reports nothing

`no dark child orders across N dates` means every date came back empty, and the
useful question is *which filter emptied it*. Re-run with `--diagnose` and the
same arguments: it queries the first date only and prints the funnel —
`workorder_rows`, `dark_venue_rows`, `of_those_filled`, `stock_rows`,
`after_country` — plus the countries that date actually held, so a `--country`
that matched nothing is obvious rather than mysterious.

## The venue sheet

`VENUE_GROUPS`, near the top of the script, maps **(country, kdb venue)** onto
**(name for the table, short name for the pies)**:

```python
("AU", "CENTREPOINT_DARK"):      ("Centrepoint", "CentrePt"),
("AU", "CENTREPOINT_CITI_DARK"): ("Centrepoint", "CentrePt"),
```

Two things it does:

**It groups.** Several kdb symbols can be one pool. The report's Routed pie has
one `Ctrpnt` slice at 46.6 where our `workorder` table has `CENTREPOINT_DARK`
for one route in and `CENTREPOINT_CITI_DARK` for another. Every figure in the
table and every slice of both pies is computed on the **group**.

**It shortens.** A pie slice has no room for "Centrepoint", so the second name
is what labels it — `CentrePt,46.6`. The first name is what the table prints.

The key is a **pair** because the sheet is keyed that way: `JPMAP_DARK` is JPMX
in JP and in HK, while in AU the same pool is reached as `JPMAP_MF_DARK`. A
venue-name-only table could not say that.

> The published pie labels the Centrepoint slice `Ctrpnt`; the sheet says
> `CentrePt`, and the sheet is what this follows. Change that one line if you
> want the published spelling.

### A venue that is not in the sheet

Keeps its raw kdb symbol as its row label, and is named on stdout just above
the table:

```
  2 venue(s) are not in VENUE_GROUPS, so they keep their raw kdb name below.
  Add them to the sheet near the top of this script to group them:
    ("AU", "SOME_NEW_DARK"):
    ("HK", "ANOTHER_DRK"):
```

Dropping it instead would take it out of the denominator, so every other row
and slice would quietly grow.

It is **not exempt from `--other-below`**: a thin stray venue rolls into
`Other` on the pie like any other thin venue. The table above the pies and that
notice above it are what always name it — exempting it would put an `ALL_CAPS`
sliver on a chart to say something the notice already said. Above the threshold
it does draw its own slice, labelled with its kdb symbol, since it has no short
name.

The same sheet lives in `scripts/reversion_liquidity/reversion_liquidity.py`.
Each script folder stands on its own, so **a new venue has to be added in both**
— `test_venue_sheet_is_consistent` in each script checks its own copy's shape,
but nothing checks the two against each other.

## Local settings

Everything above the `apply_local(globals(), __file__)` line near the top of the
script — the server, and `VENUE_GROUPS` — can be overridden from an untracked
file beside it:

```python
# scripts/dark_routed_executed/local_settings.py     (git ignores it)
ORDER_SERVER = "prod-oms-hist:5010"
```

A template with everything commented out is already there. Uncomment what you
need; anything left commented keeps the placeholder, and a placeholder server
fails loudly on `connect()` rather than half-running.

`git pull` is then always clean and the settings survive it. On startup the
script prints **which** names it took and never the values:

```
  local_settings.py: ORDER_SERVER
```

**Strict on purpose.** A name the script does not define is an **error**, not a
new setting — `ORDER_SERVR` with a missing letter would otherwise sit there
doing nothing while the run went on reading the placeholder. A broken settings
file names itself and stops.

`VENUE_GROUPS` can be set here too, but normally should not be: given locally it
**replaces the whole dict**, so a partial copy silently unmaps every venue left
out of it, and the sheet is tracked precisely so this script and
`reversion_liquidity` name the same pool the same way. An unmapped venue is not
a crash — it keeps its kdb symbol, is named on stdout, and still rolls into
`Other` if it is thin.

`apply_local` sits **above** `SHORT_NAMES`, which is derived from the sheet, so
a sheet set here reaches the pie labels too. `SHORT_NAMES` itself is derived,
not a setting: naming it is one of those errors.

## How the data is produced

### Step 1 — the routed/executed roll (`Q_ROUTED_EXECUTED`, order server)

This is `queries/dark_summary/dark_routed_executed.q` run per date, with three
changes:

- a **country filter**, so a range can be cut the way the report cuts it
- grouped **`by country,venue`**, because the venue sheet is keyed on the pair
- an **`ij`** rather than an `lj` onto `target_stock`, so a child order whose
  parent is in another country is dropped rather than kept with a null fx that
  would silently zero its notional. With no `--country` the two joins agree.

A venue is **dark** when its name contains `DARK` or `DRK`, case insensitively
— identical to `dark_summary.q`, `dark_routed_executed.q` and
`scripts/reversion_liquidity`, so all four agree by construction.

There is **no `make>0` filter** anywhere. The children that never filled are
exactly what makes routed differ from executed; filtering them would collapse
the two pies onto each other and put Fill Rate at 100% everywhere.

`workorder` is reduced to one row per `id_work` with `last` before anything is
joined to it. If it already holds one row per child order that is a no-op; if
it ever holds a row per state change, `orders_routed` would otherwise be
counting state changes.

Routed notional is `size * px_routed * fxlast`, where

```q
px_routed: transmit_lastprice ^ ?[price>0; price; 0n]
```

— the price the child was sent with, falling back to the last trade at transmit
time for market and pegged orders that carry no usable limit. `workorder` also
carries `limit_target`, `limit_candidate`, `transmit_bidprice` and
`transmit_askprice` if you would rather value it another way; that one line is
the only place that decides.

Executed notional is `make * avg_fill_price * fxlast`.

### Step 2 — accumulate (`aggregate`, `fold`)

Every column is a plain sum, so a day folds in as one frame addition and memory
stays flat whether you ask for one date or a quarter. The sheet is applied
**per day**, on the way in: q returns the roll one row per `(country, venue)`,
so a group built out of two symbols arrives as two rows and they are summed.

### Step 3 — the table (`build_table`)

Every percentage divides the **accumulated** totals, once, at the end:

```
Routed %   = 100 * notional_routed   / sum(notional_routed)
Executed % = 100 * notional_executed / sum(notional_executed)
Fill Rate  = 100 * notional_executed / notional_routed
```

A mean of daily percentages is a different number — it weights a quiet Tuesday
the same as a heavy Thursday — and the two diverge by far more than rounding
whenever the venue mix moves during the range.
`test_percentages_come_from_totals` pins that with a two-day case where the
mean-of-days answer is 50% and the truth is 9.1%.

`Fill Rate` is **money weighted**. `Filled`/`Orders` beside it is the
order-weighted version, and the two diverge whenever one venue is getting the
big orders.

### Step 4 — the pies (`pie_series`, `write_pies`)

`pie_series` turns a percentage column into slices: display names become short
codes, and anything under `--other-below` is rolled into a single `Other`
slice, sorted descending with `Other` in its natural place.

The **3.0 default reproduces the report**. Table 3.1's Australian shares are
CLSA 1.7, Centrepoint 88.6, JPMX 2.6, MS Pool 4.3, Posit 2.9; a 3.0 threshold
rolls CLSA + JPMX + Posit into `Other` at exactly the published **7.2** and
leaves MSPL at 4.3 standing — while the Routed pie, whose smallest slice is
6.1, keeps all five venues. `test_other_reproduces_the_published_pie` asserts
both halves of that.

The roll-up happens **before** rounding, so `Other` is the true remainder
rather than a sum of already-rounded numbers.

It is a **display threshold only**. The table above the pies always shows every
venue, and `Other` never enters a calculation.

The drawing is the Office-style pie from `CASStudy/latex_pie/pie_slide.py` —
same pastel palette, same clockwise wedges, same outside `Name,Value` leader
labels with the de-collision pass down each side. One difference on purpose:
colours are assigned **by name across both pies**, so a venue in both keeps its
colour left and right. The published slide re-colours each pie independently,
which makes the pair harder to read.

## Outputs

Everything prints to stdout. `--out-dir` additionally writes:

| file | what |
| --- | --- |
| `dark_routed_executed.csv` | the full table, every venue, full precision |
| `routed.csv` | `name,percentage` for the left pie |
| `executed.csv` | `name,percentage` for the right pie |
| `pies.png` | both pies, 200 dpi |
| `pies.pdf` | both pies, vector — this is the one to put in a document |

The two pie CSVs are in the exact format
`CASStudy/latex_pie/pie_slide.py` reads, so the pies can be redrawn or
hand-edited without going back to kdb:

```
python pie_slide.py out/routed.csv out/executed.csv --titles "Routed %" "Executed %"
```

## Verifying it

```
python scripts/dark_routed_executed/dark_routed_executed.py --self-test
```

16 tests, no kdb connection needed — this script is written on a machine with
no kdb access, so everything except the three q constants is pure Python and is
checked here. Run it before shipping a change.

The ones worth knowing about:

- `test_other_reproduces_the_published_pie` — the 3.0 default against the
  report's own Executed pie, including that `Other` is 7.2
- `test_percentages_come_from_totals` — percentages divide accumulated
  notionals, not averaged daily ones
- `test_chunking_is_exact` — folding day by day equals one pass, which is what
  the whole accumulator design rests on
- `test_venues_in_one_group_become_one_row` — both Centrepoint symbols land in
  one row carrying both notionals
- `test_the_sheet_is_keyed_on_country` — `JPMAP_DARK` maps in JP and HK but not
  in AU, and `JPMAP_MF_DARK` the other way round
- `test_pie_csv_matches_the_latex_pie_format` — the CSVs read back through the
  same parser `pie_slide.py` uses
- `test_unmapped_venue_is_not_exempt_from_other` — a thin unmapped venue rolls
  into `Other` rather than getting a special case
- `test_country_reaches_q_as_chars` — the country filter arrives as a char
  vector, not a symbol. PyKX sends a Python `str` as a q symbol, and `` `$ ``
  on a symbol is a `'type` error, so getting this wrong fails on every date.

`test_server_constant` skips itself while `ORDER_SERVER` still holds the
placeholder; that is not a failure.

## Where the judgement calls are

1. **Routed is valued at the price the child was sent with**, falling back to
   the last trade at transmit time. The `px_routed` line in the q is the only
   place that decides.
2. **No `make>0` filter.** The unfilled children are the entire difference
   between the two pies.
3. **`ij`, not `lj`, onto `target_stock`** — so `--country` actually excludes
   rather than nulls.
4. **Fill Rate is money weighted**; the order-weighted version sits beside it.
5. **Percentages divide accumulated totals**, once, at the end.
6. **`--other-below` defaults to 3.0** because that is what reproduces the
   report. It is display only.
7. **An unmapped venue keeps its kdb symbol** rather than being dropped, and
   is named on stdout — but `--other-below` still applies to it on the pie.

## Related

- `queries/dark_summary/dark_routed_executed.q` — the single-date q this is
  built on, no country filter and no grouping
- `queries/dark_summary/dark_summary.q` — `notional_executed` here reconciles
  with `notional_usd` there
- `scripts/reversion_liquidity` — tables 3.1 and 3.3. Its `%Notional` and this
  script's `Executed %` measure almost the same thing off two different columns
  (`fillsize*fillprice` per execution there, `make*avg_fill_price` per child
  order here), so they agree to a rounding rather than to the bit. The report
  shows Centrepoint at 88.6 in table 3.1 and 88.5 in the Executed pie for
  exactly that reason.
- `kmonitor/dark_routed_executed` — the same split as a live dashboard
