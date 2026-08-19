# luld_shortsell_check

Audits our algo engine's **child splits** against each market's limit up/down
and short sell rules over a date range, and reports anomalies with enough
context to reproduce them.

It answers three questions, and they need different evidence:

1. **Did a split break a rule?** — price against the band, the client's limit,
   and the market's short sell rule.
2. **Did the engine notice?** — the split's `state`, since LULD breaches and
   short sell breaches fail through completely different ones.
3. **Should a split have existed at all?** — the cases where the stock was
   pinned at the band on the side we *could* have traded, and we sent nothing.

Design rationale, and why each judgement call went the way it did:
[`docs/superpowers/specs/2026-08-19-luld-shortsell-check-design.md`](../../docs/superpowers/specs/2026-08-19-luld-shortsell-check-design.md).

## Running it

Set the two endpoints once, at the top of the script:

```python
ORDER_SERVER = "CHANGEME:5010"     # target, target_state, target_stock, workorder, target_oms
QATT_SERVER  = "CHANGEME:5011"     # qatt
```

Both are the **historical** processes, not the realtime ones — `qatt` exists in
both flavours and only the historical one carries a `date` column. Then:

```
pip install pykx
python scripts/luld_shortsell_check/luld_shortsell_check.py \
    --start 2026-07-01 --end 2026-07-31 --country CN --out-dir out
```

`pykx` unlicensed mode is enough. All q evaluation happens server-side, so no q
licence and no `QHOME` are needed locally.

```
--start --end     date range (required)
--country         target_stock country, e.g. CN. Blank for all.
--checks          comma list of rule ids, or 'all' (default). An unknown id is
                  refused, not ignored.
--band-file       CSV of known bands, overriding every computed layer
--pin-mins        minimum pin minutes before the no-split family fires (5)
--chase-ticks     SS_HK_CHASE: ticks the ask must move (2)
--chase-secs      SS_HK_CHASE: seconds without a reprice (30)
--out-dir         also write report.xlsx here, one sheet per rule
--diagnose        first date only; distinct values and stage row counts
--quiet           no per-date progress on stderr; the report still prints
--self-test       run the built-in tests; needs no kdb connection
```

`--checks`, the date range and the server constants are all validated **before**
anything touches kdb, so a typo fails in a second rather than after a connection
and a day of queries.

Progress goes to **stderr**, one line per date. The report goes to stdout, so
`> out.txt` keeps the two apart.

## Scope, and what is deliberately not checked

**Eight markets:** `HK JP KR MY TH CN TW IN`.

**Indonesia is excluded.** It is the one market with neither a derivable band
nor a stated short sell rule — its band needs the IDX auto-rejection tier table
and the effective date of every revision, and unlike Japan there is no
observational rescue. ID orders are counted on one `excluded_market` line and
nothing else runs on them.

**Short sell checks run on five markets only** — `HK JP KR MY TH`, the ones our
own sheet states a rule for. Taiwan, China and India have real rules and they
are written down in §5.1 of the spec:

| | the actual rule |
|---|---|
| China | short sale price not below the **latest transaction price**, previous close before the first print |
| Taiwan | a floor at the **previous close** — not a tick rule — and only when yesterday fell more than 3.5%, with margin-eligible names exempt |
| India | **no price rule at all.** SEBI's framework has nine clauses and not one constrains the price of a short sale |

They are **researched, not enforced.** A short sell finding is a compliance
assertion, and China and Taiwan come from secondary sources on markets that have
rewritten these rules under stress more than once. Enforcing a rule we are not
certain of would put fabricated breaches next to real ones and cost the report
its credibility. They report `RULE_UNKNOWN`, which shows in the scorecard as
*unchecked* rather than as clean. Enabling one is a line in `MARKETS` plus its
fixtures.

Their **LULD bands are unaffected** — those are solid, and `LULD_CAP` runs on
all three.

## Where the band comes from

Four layers, best first. Every band carries `band_src` and `band_conf`, and both
travel onto every finding.

| layer | source | applies to |
|---|---|---|
| `override` | `--band-file` | whatever you supply |
| `target_oms` | the engine's own `limitup`/`limitdn` | **India and Korea only** |
| `observed` | `qatt` — a locked or one-sided book *is* the band | any stock that pinned |
| `computed` | previous close from `qatt.netChange`, else `target_stock`, then the market's rule | everything else |

**`target_oms` is read for India and Korea and nowhere else.** It is
inconsistent across markets generally but always populated for those two, and it
is what makes India checkable at all — its per-scrip circuit filters cannot be
derived from anything we have. Widen `BAND_FROM_TARGET_OMS` only on the null
rates `--diagnose` prints.

Two details there that are easy to get wrong and expensive to get wrong:

- `target_oms` is a **tickstream, many rows per `id_target`**, so the band taken
  is the one **prevailing at `t_transmit`** (via `aj`), not the last of the day.
- It is the last **non-null, positive** value, not the last row.
  `FlexOrderStream` only writes the band when it has a quote, so a zero means
  *"not known at this instant"*, not *"no band"*. A blind last-row read returns
  0 on a stock with a perfectly good band a few records earlier — and that 0
  would then report itself as `LULD_GUARD_INACTIVE`, a fabricated finding out of
  a parsing mistake.

### The band rules

| market | rule |
|---|---|
| JP | TSE step table on the base price — generated, not listed, and asserted against the published values |
| KR MY TH | previous close ±30% |
| TW | previous close ±10% |
| CN | **from the symbol prefix** — see below |
| IN KR | published, see above |
| HK | no band rule exists |

**China's band is a function of the board, and the board is in the prefix:**

| prefix | board | band |
|---|---|---|
| `600` `601` `603` `605` | SSE main | ±10% |
| `688` `689` | STAR | ±20% |
| `000` `001` `002` `003` | SZSE main | ±10% |
| `300` `301` | ChiNext | ±20% — **and ±10% before 2020-08-24** |
| `900` / `200` | B shares | ±10% |
| `430` `83x` `87x` `920` | Beijing | ±30% |

ChiNext's reform date is stored as a date rather than a bare percentage, so an
audit range straddling it is right on both sides.

### `band_conf`, and why a band can be discarded

| value | meaning |
|---|---|
| `confirmed` | published, or an observed pin agrees with the computed band within a tick |
| `assumed` | never hit the band; computed only |
| `widened_observed` | contradicted, but the cause is known — see Japan |
| *(discarded)* | contradicted with no known cause; the stock's LULD findings are **suppressed** and it is listed in the footer |

Discarding is deliberate. A wrong band produces confident nonsense, and
reporting *"could not establish a band for 40 names"* is worth more than 40
fabricated violations.

**Japan is the exception.** Its limits expand overnight after a limit close, so
a contradiction there has a known cause and the observed extreme is used instead
of nothing. Japan also gets prior-day pin detection for the same reason. No
Japan finding rests on the expansion multiplier alone — the observed data
overrides it.

### The tick grid

`target_stock.tsid` is opaque — the table it indexes lives in `mbref`, which we
cannot reach — but **every stock sharing a `tsid` shares a tick ladder by
construction**, so the ladder is recovered from the prices themselves and used
for inward rounding. Bands always round **toward** the base price, so a band is
never reported wider than the rule allows.

The recovery is self-validating: a real ladder is a clean step function, so if
the recovered grid is ragged the script falls back to the scalar
`target_stock.ticksize` rather than rounding against nonsense.

## The checks

**VIOLATION** cannot be legal regardless of config. **DEVIATION** depends on a
config we cannot see, so the offset is reported and the distribution speaks — a
market consistently one tick off is a setting, not hundreds of failures.

| id | markets | check | class |
|---|---|---|---|
| `LULD_CAP` | JP KR MY TH CN TW IN | split price inside the band | violation |
| `LULD_CLIENT_LIMIT` | all | split not more aggressive than the parent's `limit_price` | violation |
| `LULD_OFFSET` | CN JP | ticks from the unfavourable band | deviation |
| `SS_HK_ASK` | HK | short sell at or above the ask; a market-order short sell fails by construction | violation |
| `SS_UPTICK` | JP KR MY | above the last trade, or equal on a zero-plus tick (`qatt.trdTick`) | violation |
| `SS_TH_LTP1` | TH | last traded price + 1 tick | deviation, violation if below the last trade |
| `SS_KR_CLAMP` | KR | an uptick price above the band must be capped at it, not sent through | violation |
| `SS_HK_CHASE` | HK | resting split, ask moved away, no reprice | violation |

### Every split is judged twice, and the disagreement is the point

Once against `qatt` at `t_transmit` — what the exchange published — and once
against `workorder.transmit_*` — the snapshot the algo actually held. Order and
`qatt` share one clock, so nothing is converted. The `ref_verdict` column says
which:

| `ref_verdict` | meaning |
|---|---|
| `both` | failed against the market *and* against the algo's own view — a pricing bug |
| `qatt_only` | legal on the snapshot the algo held, illegal against what the exchange published — a **stale snapshot** |
| `transmit_only` | the algo's own view was worse than reality |

`qatt_only` is the shape of the Hong Kong *"market moves away"* note. A finding
is counted **once**, not once per reference.

## The situational detectors

These are the "should a split have existed at all" half, and they have a
mechanism behind them rather than a hunch. From `FlexOrderStream.checkPriceFinal`,
a child order is not sent when the quote is stale or `extremeMarketCondition`
reports a one-sided book on a lit venue; `ABSStrategy` bails when `midPrice` is
below `ZEROPRICE`. **A stock pinned limit up has no ask, so its mid is
degenerate and its quote goes stale — the algo stops generating splits by
construction.** On the side that cannot fill, that is correct. On the side that
can, it is a queue of resting counterparties we never joined.

| id | shape | class |
|---|---|---|
| `LULD_FAVOURABLE_NO_SPLIT` | fillable side, pinned, `leave > 0`, **zero splits** | opportunity |
| `LULD_FAVOURABLE_PASSIVE` | fillable side, every split priced *behind* the band | opportunity |
| `LULD_UNFAVOURABLE_CHURN` | splits sent into the band on the side that cannot fill | improvement |
| `LULD_BLIND_SUPPRESSION` | splits killed on the price path while the book was one-sided | improvement |
| `LULD_GUARD_INACTIVE` | 3+ `LULD_CAP` breaches on one stock — a pattern, not an incident | violation |
| `SS_REJECT_CLUSTER` | 5+ rejected short sells on one venue in one hour | violation |

`LULD_GUARD_INACTIVE` is a **rollup** of `LULD_CAP`; its splits stay counted
once, under `LULD_CAP`.

**`LULD_FAVOURABLE_NO_SPLIT` would drown you in noise without guards**, so all
of these must hold, and each is a column on the output row so a false positive
can be diagnosed rather than argued about:

- parent state is `activated`
- `leave > 0`
- the stock is not halted
- the parent is not close-only
- the pin overlaps the parent's `t_start`–`t_end` window
- the overlap lasts at least `--pin-mins` (default 5, deliberately strict)

`LULD_APPROACH_BACKOFF` is in the spec but **not implemented** — it needs a
participation-rate series the current queries do not fetch.

## Reading the report

**stdout** is the scorecard: per market and per rule, how many splits were
checked and how many were violations, deviations or opportunities.

```
market rule                        checked   viol    dev    opp  status
CN     LULD_CAP                        412      7      0      0  NotOK
JP     SS_UPTICK                       880      0      0      0  OK
TW     RULE_UNKNOWN                    203      -      -      -  unchecked (no confirmed rule)
ID     excluded_market                  56      -      -      -  out of scope
```

**`unchecked` is not `OK`.** A market reading clean because nothing was
checkable must not look like one where everything passed.

Then the **suppression footer** — stocks dropped for a contradicted band, the
band-confidence mix per market, splits unchecked for want of a rule, and orders
skipped as out of scope. **Read this before the findings.** It is the only thing
that distinguishes a cell that is clean because everything passed from one that
is clean because nothing was checkable.

**`--out-dir`** writes `report.xlsx`, one sheet per rule, every finding in full
— ids, `t_transmit`, the price sent, the expected price, the delta in ticks, the
band and its provenance, both market snapshots, `ref_verdict`, severity and
`impact_usd`. Cells hold numbers rather than rendered strings, so the workbook
sorts and charts. Rows are sorted by impact, so the expensive findings read
first. Needs `openpyxl`, imported only when the flag is used.

`reason` is a one-line English statement of what failed, so a row is readable
without the spec open.

## `--diagnose`

Runs the first date only and prints what tells a real empty result from a filter
that silently matched nothing:

- **distinct `target.side`** — confirms `sellshort` is still the short sell value
- **country × symbol suffix** — China is `.CH`, not `.CN`, and a country filter
  that matches nothing empties every date in silence
- **`tsid` × symbol prefix** — a second, independent opinion on the Chinese
  board mapping. Agreement confirms it; disagreement names the stock that breaks
  it.
- **`workorder.state` with its class** — so a state the engine grew since this
  was written shows up as `unknown` rather than being absorbed into `normal`

## Verifying it

```
python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test
```

105 tests, no kdb connection required — which matters, because this is written
on a machine that has none. They cover the Japan step table against the
published values, inward rounding, ladder recovery including its refusal to
guess on ragged data, band reconciliation in all four confidence states, every
rule at the boundary and one tick either side, every guard on the no-split
detector, the two-reference merge, and the scoring arithmetic.

The **q half cannot be unit tested here.** It is checked two ways:

1. **Reconciliation** — for a single date, parent and split counts per market
   must agree with `queries/limit_up_down/limit_up_down.q` over the same stocks.
   Disagreement is a bug in the join, not a definitional difference.
2. **The acceptance case** — `1370265478` / `600584.CH`, 16 July, a split
   generated *below* limit down. It runs offline as a fixture
   (`test_acceptance_the_600584_case_from_the_notes`) and must also appear in
   the `LULD_CAP` sheet on a real run. If it does not, the script does not work.

## Where the judgement calls are

All eight are in the notes block at the foot of the script, each with the one
line that reverses it. The one that governs the rest:

> **Where a band or a rule must be guessed, guess in the direction that
> under-reports.**

A missed violation is a gap someone can close later. A fabricated violation
against a legal price is what makes people stop reading the report — and then
the gaps stop mattering, because nothing gets read at all.

That is why Chinese ST names get a 10% band when they are really 5% (too wide,
so it misses rather than invents), why a contradicted band is discarded rather
than reported, and why Taiwan, China and India short sells are counted as
unchecked rather than assessed against a rule we only think we know.
