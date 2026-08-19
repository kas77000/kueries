# luld_shortsell_check — design

Audit our algo engine's child splits against each market's **limit up/down** and
**short sell** rules over a date range, and report anomalies with enough context
to reproduce them.

Source of the rule set: the `LULD_SS.xlsx` table and its notes. Source of the
engine's actual behaviour: the Argo Java slice in `../ai3`, which is what makes
several of these checks possible at all.

---

## 1. The rules being checked

As transcribed from the sheet.

| | Hong Kong | Japan | Korea | Malaysia | Thailand | Indonesia | China | Taiwan | India |
|---|---|---|---|---|---|---|---|---|---|
| **LULD Rule** | *(none)* | Price Band | PreCls ±30% | PreCls ±30% | PreCls ±30% | Price Band | PreCls ±10% | PreCls ±10% | Price Band |
| **LULD Status** | | NotOK | | | | | NotOK | | |
| **SS Rule** | Always Ask | UpTick (Trend) | UpTick (Trend) | UpTick (Trend) | LTP+1Tick | | | Need to chk | |
| **SS Status** | OK | OK | OK | OK | NotOK | | | | |
| **SS-Other Issue** | | | NotOK | | | | | | |

The notes under the table are the anomaly definitions, and each maps to a check
in §5:

- **China** — child splits must be capped by limit up/down. A split was
  generated *below* limit down with TPA=4 (`1370265478` / `600584.CH`, 16 July),
  same in the close auction. Separately, a config to trade ±1 tick from the
  *unfavourable* band.
- **Japan** — with config, limit computed 1 tick passive; without config the
  algo goes to limit up/down and ignores the client limit. Does not work for SQ.
- **Korea SS** — the uptick price must still be clamped inside limit up/down.
  PART orders trading in the close. DMA short sells that do not reprice or chase.
- **Hong Kong SS** — DLP/DMA aggressive short sells split per the uptick rule,
  but do not reprice or chase when the market moves away. Splits at Ask+2 ticks
  not filling.
- **Thailand SS** — LTP+1 tick unsupported.
- **India, Taiwan** — unverified.

### 1.1 Corrections to the table

The percentage is not one number per market, and applying the table literally
manufactures violations:

- **China** — ±10% is main board only. STAR (`688*`) and ChiNext (`300*`) are
  ±20%; ST / \*ST names ±5%; day-one listings unlimited.
- **Korea** — ±30% is KOSPI/KOSDAQ. KONEX is ±15%.
- **Taiwan** — ±10%, but no limit for the first 5 days of a new listing.

Board is derived from the symbol prefix where it can be; everything else is
marked `assumed` and its confidence is carried into the report rather than
hidden.

---

## 2. Scope

**In scope.** Every parent order in `target` over `[--start, --end]` whose
`target_stock.country` is one of the nine markets, and every child split in
`workorder` beneath it — **filled or not**. Unfilled splits are the point: the
Hong Kong and Korea notes are both about splits that failed to fill.

**Out of scope.** Markets outside the nine (counted and reported as
`RULE_UNKNOWN`, never silently passed). Any attempt to reconstruct what the
engine's config was set to — deviations from config-dependent rules are reported
as deviations, not violations, precisely because that config is not in the data.

**Not a gate.** This script reports. It does not block, amend or cancel anything.

---

## 3. Where the band comes from

Three layers, best first, plus an optional fourth (§3.2). Every resolved band
carries its provenance, and the report exposes it on every row.

| layer | source | available |
|---|---|---|
| **observed** | `qatt`: book locked (`qbid=qask`) or one-sided (`qask=0` up, `qbid=0` down). The pinned price *is* the band. | only when the stock actually hit the band |
| **computed** | base price from `qatt`: `preCls = price - netChange`, cross-checked against `price / (1 + pctChange/100)` | any stock that printed |
| **fallback** | `target_stock.orgclose^adjclose` on the **same date** — that column already holds the previous close | always |

`target_oms.limitup/limitdn` carries the engine's own band and would be the
strongest source, but its data was checked and found inconsistent. It is
**deliberately not used**. If it is ever fixed it slots in above `observed` and
turns §6's `LULD_GUARD_INACTIVE` from an inference into a direct read.

`mbref`'s `luld` table (`time; sym; atime; flag; limitup; limitdown`) is the
feed-published band. LULD is a US NMS mechanism and the table may hold US names
only, so the script **probes for it at startup** and uses it when populated for
the market in question. Never required.

### 3.1 Provenance and reconciliation

| field | values |
|---|---|
| `band_src` | `observed` · `qatt_netchange` · `target_stock` · `mbref_luld` |
| `band_conf` | `confirmed` — an observed pin agrees with the computed band within one tick<br>`assumed` — never hit the band; computed only<br>`contradicted` — a pin, or the session `highPrice`/`lowPrice`, sits outside the computed band |

**`contradicted` discards the computed band and suppresses that stock's LULD
findings.** Reporting "could not establish a band for 40 names" is worth more
than 40 fabricated violations. Suppressed stocks are counted and listed.

Bands round **inward** to `target_stock.ticksize`, so a band is never reported
wider than the rule allows.

### 3.2 Japan

Japan has no percentage. TSE's 制限値幅 is a step table on the base price
(基準値段, normally the previous close), in yen. It is generated, not hardcoded:

```
base < 100                        -> 30
100 <= base < 200                 -> 50
200 <= base < 500                 -> 80
500 <= base < 700                 -> 100
700 <= base < 1000                -> 150

base >= 1000:  k = floor(log10(base)) - 3
               m = base / 10^(k+3)          in [1, 10)
               limit = g(m) * 10^k
               g: [1,1.5)->300  [1.5,2)->400  [2,3)->500
                  [3,5)->700    [5,7)->1000   [7,10)->1500
               capped at 10,000,000
```

Checks: base 1,000 -> 300; 10,000 -> 3,000; 20,000 -> 5,000; 100,000 -> 30,000.
These are asserted in the self-test against the published table.

**Three things make this table wrong on exactly the stocks that matter, and all
three are handled by §3.1 rather than by widening the table:**

1. **Expanded limits.** A name closing limit-up/down on a special quote gets a
   widened limit the next day — doubled, wider again on consecutive days. The
   step table then reports a band narrower than the real one, and every split
   between the two reads as a violation. This is the single largest
   false-positive source for JP, and `band_conf=contradicted` is what catches it.
2. **Base price is not always the previous close** — after a corporate action, or
   when TSE carries a last special quote forward. The `orgclose` / `adjclose`
   distinction.
3. **SQ days** — the notes already say the algo does not work for SQ.

India (scrip-specific 2/5/10/20% circuit filters) and Indonesia (IDX
auto-rejection tiers, revised repeatedly) are the same problem and get the same
treatment: computed where possible, `assumed`, and suppressed on contradiction.

---

## 4. Classifying an order

Per parent, from `target.side` and `target_stock.country`:

| class | condition |
|---|---|
| `shortsell` | `side = sellshort` |
| `luld` | country has a band rule |
| `both` | both of the above |
| `neither` | HK long orders, and anything outside the nine markets |

`sellshort` is the confirmed value. It is a module constant, not a literal, and
`--diagnose` prints the distinct `side` / `sidesign` values seen so a change in
the OMS vocabulary is visible rather than silent.

**Market identification is `target_stock.country`, with the symbol suffix as a
cross-check** — the two do not spell markets the same way. China carries the
suffix `.CH` (`600584.CH`), not `.CN`, and `algo_violation_tickdata.q` filters on
`*.CN*` separately again. `--diagnose` prints the country-to-suffix crosstab, and
any stock whose two disagree is reported rather than assigned to a market by
guess. This matters more than it looks: `reversion_liquidity` has a documented
failure mode where a country filter silently matches nothing and every date comes
back empty.

---

## 5. Rule checks

Two classes, kept apart because the notes mix them:

- **VIOLATION** — cannot be legal regardless of config.
- **DEVIATION** — depends on a config that is not in the data. Reported with the
  observed offset in ticks, so a market that is consistently 0 or consistently 1
  is legible as a config setting rather than as hundreds of failures.

| id | markets | check | class |
|---|---|---|---|
| `LULD_CAP` | JP KR MY TH ID CN TW IN | `limit_dn <= price <= limit_up` | violation |
| `LULD_CLIENT_LIMIT` | all | split not more aggressive than parent `limit_price` | violation |
| `LULD_OFFSET` | CN JP | offset in ticks from the unfavourable band | deviation |
| `SS_HK_ASK` | HK | `price >= qask` at `t_transmit`; a market-order short sell fails by construction | violation |
| `SS_UPTICK` | JP KR MY | `price > lastPrice`, or `>=` when `qatt.trdTick` shows the last tick was up | violation |
| `SS_TH_LTP1` | TH | `price = lastPrice + ticksize` | deviation; violation if *below* |
| `SS_KR_CLAMP` | KR | an uptick price exceeding the band must be capped at the band, not sent through | violation |
| `SS_HK_CHASE` | HK | resting split, ask moved away > `--chase-ticks` for > `--chase-secs`, `count_chaseprice = 0`, no replacement child | violation |
| `RULE_UNKNOWN` | ID CN TW IN short sells | counted as **unverifiable**, never as passing | — |

### 5.1 Two reference markets, deliberately

Every split is checked twice:

- against `workorder.transmit_bidprice/askprice/lastprice` — the algo's own
  snapshot. Answers *did the algo apply its rule to the data it had?*
- against `qatt` at `t_transmit` via `aj` — what the market published. Order and
  qatt share one clock, so no conversion is needed. Answers *was the price
  actually legal?*

A split failing **both** is an algo bug. A split legal against `transmit_*` but
illegal against `qatt` is a **stale-snapshot** finding and gets its own column —
that is the exact shape of the Hong Kong "market moves away" note.

---

## 6. State, and the situational detectors

### 6.1 State is an axis, not a gate

`OrderStateType` has 90 values, and the two rule families fail through different
ones:

- **Short sell** fails loudly: `REJECTED`, `INVALID_ACK`, `FAIL_ACK`.
- **LULD usually produces no rejection at all**: `CLOSE_BAD_PRICE` (7 call sites,
  all on the price path), `CLOSE_TAKE_OUTOFMONEY`, `CLOSE_STOCK_HALT`,
  `CLOSE_ORDER_HALT`, `STOPPED_VOLATILITY_TAG262` / `TAG325` — or silence.

So every row carries `state` and `state_class` (`rejected` / `suppressed` /
`halted` / `never_on_market` / `normal`), derived from the enum's own predicates
(`isRejected`, `isClosed`, `neverOnMkt`, `isStopppedVolatility`). No check is
gated on state.

### 6.2 Why "no split" happens

From `FlexOrderStream.checkPriceFinal`, a child order is **not sent** when:

```java
if (!quote.isLatest() && !skipPriceCheck)                        return 0;  // stale quote
if (extremeMarketCondition(bid, ask) == 2 && isLit                          // crossed / one-sided
    && !isAuction && !(vol_auc_send_order > 0 && volatilityAuction))
                                                                 return 0;
```

and `ABSStrategy:1599`:

```java
if (!qs.isLatest() || qs.midPrice() < Util.ZEROPRICE) return false;
```

A stock pinned limit-up has `qask = 0`. Its mid is degenerate and its quote goes
stale, so **the algo stops generating splits by construction**. On the
unfavourable side that is correct — you cannot buy into a limit-up. On the
**favourable** side it is a missed opportunity: we are a seller, there is a queue
of buyers resting at the limit, and we send nothing.

### 6.3 The detectors

| id | shape | class |
|---|---|---|
| `LULD_FAVOURABLE_NO_SPLIT` | favourable side, pinned >= `--pin-mins`, `leave > 0`, **zero splits** in the window | opportunity |
| `LULD_FAVOURABLE_PASSIVE` | favourable side, pinned, splits priced *behind* the band rather than at or through it | opportunity |
| `LULD_APPROACH_BACKOFF` | price within `--approach-pct` of the band and moving toward it, favourable side, participation **falls** | opportunity |
| `LULD_UNFAVOURABLE_CHURN` | unfavourable side, pinned, splits that cannot fill — message traffic and `count_send` inflation | improvement |
| `LULD_GUARD_INACTIVE` | a stock with **three or more** `LULD_CAP` breaches while observably pinned — a pattern, not an incident: the cap was not applied at all rather than missed once | violation |
| `LULD_BLIND_SUPPRESSION` | no split, or `CLOSE_BAD_PRICE`, coinciding with a one-sided book — the algo went blind *because of* the limit | improvement |
| `SS_REJECT_CLUSTER` | `REJECTED` splits on `sellshort` parents, clustered by market + venue + hour | violation |
| `SS_PART_IN_CLOSE` | Korea: PART orders trading quantity in the close | improvement |

`favourable` means `sidesign < 0` into a limit-up, or `sidesign > 0` into a
limit-down.

`LULD_GUARD_INACTIVE` is a **rollup of `LULD_CAP` rows**, not an independent
finding. It appears on its own sheet and in the scorecard as a stock count, and
its constituent splits are counted once, under `LULD_CAP`. Every other detector
in this table is independent of §5 and can co-fire with it.

**`LULD_FAVOURABLE_NO_SPLIT` needs guards or it produces noise at scale.** All of
these must hold before it fires, and each is a column on the output row so a
false positive can be diagnosed:

- parent state is `activated`, and the pin window falls inside `t_start` .. `t_end`
- `leave > 0`
- the parent is not close-only (`doclose` set with no continuous participation)
- the stock is not halted and not in a lunch break
- the pin lasted at least `--pin-mins` (default strict)

### 6.4 Severity, confidence, impact

Every finding carries three independent fields rather than one verdict:

- `severity` — `violation` / `deviation` / `opportunity` / `improvement`
- `confidence` — inherited from `band_conf`
- `impact` — unfilled notional for the no-split family, `abs(price delta) * size`
  in USD via `target_stock.fxlast` for the pricing family

The report sorts by `impact` so the expensive findings read first.

---

## 7. Architecture

`scripts/luld_shortsell_check/luld_shortsell_check.py`, plus a README covering
this script only. Same shape as `scripts/reversion_liquidity`: Python driving q
lambdas over PyKX against two historical processes, one date at a time, with a
`--self-test` that needs no kdb.

```
                for each date d in [--start, --end]
                ----------------------------------

  ORDER SERVER                                  QATT SERVER
  ------------                                  -----------
  Q_ORDERS   target |> target_state             Q_BAND    per sym: first tick with
             |> target_stock |> workorder                 netChange, locked and
             one row per id_work via `last`               one-sided pins, session
                    |                                     high/low, pin intervals
                    |                           Q_MKT     aj at t_transmit ->
                    |                                     qbid qask lastPrice trdTick
                    v
             resolve_band()       3 layers -> band, band_src, band_conf
                    v
             classify_order()     luld / shortsell / both / neither
                    v
             check_rules()        section 5 -- one function per rule
             detect_situations()  section 6 -- needs pin intervals and the split roll
                    v
             fold()               counters stay, finding rows accumulate
                ----------------------------------
  scorecard()   -> stdout: the section 1 table with computed OK / NotOK and counts
  workbook()    -> --out-dir report.xlsx, one sheet per rule, every finding in full
```

q lambdas are sent as source text with **typed arguments** — dates and country
codes travel as q values, never interpolated. Country codes go as **char
vectors**, not python `str`, because PyKX turns a `str` into a q symbol and
`` `$ `` on a symbol is a type error.

`workorder` is collapsed to one row per `id_work` with `last` before anything
joins to it. If it already holds one row per child that grouping is free; if it
ever holds a row per state change, it is the difference between a correct count
and a silently multiplied one.

Memory stays flat across a long range: per date the script reduces to per-market
counters plus the finding rows, and drops the split rows.

### 7.1 Arguments

```
--start --end        date range (required)
--country            restrict to one market; blank for all
--checks             comma list of rule/detector ids, or 'all' (default)
--pin-mins           minimum pin duration before the no-split family fires
                     (default 5)
--approach-pct       band proximity for LULD_APPROACH_BACKOFF (default 1.0)
--chase-ticks        SS_HK_CHASE: ticks the ask must move (default 2)
--chase-secs         SS_HK_CHASE: seconds without a reprice (default 30)
--out-dir            also write report.xlsx here
--diagnose           first date only; distinct side/state/country values and
                     stage-by-stage row counts
--quiet              no per-date progress on stderr
--self-test          run the built-in tests; needs no kdb connection
```

Progress to stderr, one line per date. Report to stdout.

---

## 8. Report

**stdout** — the §1 table, recomputed. Per market and per rule: orders checked,
violations, deviations, opportunities, unverifiable, and the band-confidence mix.
This is the artefact that answers "is the algo respecting the rules", and it is
directly comparable against the sheet's own OK / NotOK.

Then a suppression footer: stocks dropped for `band_conf=contradicted`, orders
skipped per guard, and markets with no rule — so a cell reading clean because
nothing was checkable is visibly different from a cell reading clean because
everything passed.

**`--out-dir report.xlsx`** — one sheet per rule, every finding in full:

```
date  id_target  id_work  sym  country  side  state  state_class
t_transmit  price_sent  expected_price  delta_ticks
band_up  band_dn  band_src  band_conf
qbid qask lastPrice trdTick          (qatt at t_transmit)
transmit_bid transmit_ask transmit_last
severity  confidence  impact_usd  reason
```

Cells hold numbers, not rendered strings. `reason` is a one-line English
statement of what failed, so a row is readable without the spec open.

---

## 9. Testing

`--self-test` runs offline, which matters because this is written on a machine
with no kdb and no pykx. Covered:

- the Japan step table against the published TSE values, including the decade
  self-similarity and the 10,000,000 cap
- inward tick rounding: a ±30% band never comes out wider than 30%
- band reconciliation: `confirmed` / `assumed` / `contradicted` on synthetic
  quote series, and that `contradicted` suppresses rather than reports
- every rule in §5 against hand-built fixtures, both sides, at the boundary and
  one tick either side of it
- the `LULD_FAVOURABLE_NO_SPLIT` guards: one fixture per guard that must
  suppress the finding
- China board detection from symbol prefixes
- severity / impact arithmetic

The q half cannot be unit tested here. It is checked by **reconciliation**: for a
single date, parent and split counts per market must agree with
`queries/limit_up_down/limit_up_down.q` over the same stocks. Disagreement is a
bug in the join, not a definitional difference.

The known case from the notes — `1370265478` / `600584.CH`, 16 July, split
generated below limit down — is the **acceptance test**. If the script does not
flag that order, it does not work.

---

## 10. Judgement calls

Listed here and in a notes block at the foot of the script, each with the
one-line change that reverses it.

1. **Contradicted bands are suppressed, not reported.** A wrong band produces
   confident nonsense; the alternative is a report nobody trusts. The suppressed
   list is printed so the gap is visible.
2. **Config-dependent rules are deviations, not violations.** The engine's config
   is not in the data. Reporting the offset distribution lets you infer the
   setting instead of guessing at it.
3. **Unfilled splits are in scope.** Two of the notes are about splits that never
   filled; excluding them would hide exactly what was asked for.
4. **The opportunity detectors ship in the same script**, behind `--checks`. They
   share the whole band-resolution pipeline, and splitting them would duplicate
   the expensive half.
5. **`target_oms` is not used**, on the finding that its data is inconsistent. It
   is the strongest band source if that is ever fixed.
6. **Markets without a rule are `RULE_UNKNOWN`, not a pass.** Indonesia, China,
   Taiwan and India short sells are blank on the sheet; counting them as
   compliant would be the worst available answer.
