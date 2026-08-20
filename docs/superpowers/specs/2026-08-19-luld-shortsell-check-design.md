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

Transcribed as it stands. Four cells are blank or "need to chk" — Taiwan, China
and India short sells, and Indonesia entirely. **Short sell checks run only on the
five markets whose rule is confirmed**: Hong Kong, Japan, Korea, Malaysia and
Thailand. The other three are `RULE_UNKNOWN` by choice, not by omission — §5.1
records what their rules appear to be and why they are not yet enforced.
Indonesia is out of scope entirely (§2).

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
manufactures violations.

**China — the band is a function of the board, and the board is in the symbol
prefix.** This is fully derivable, so it is a rule, not a gap:

| prefix | board | band | in force from |
|---|---|---|---|
| `600` `601` `603` `605` | SSE main | ±10% | |
| `688` `689` | **STAR** (科创板) | **±20%** | 2019-07-22 |
| `000` `001` | SZSE main | ±10% | |
| `002` `003` | SZSE main (former SME board, merged 2021) | ±10% | |
| `300` `301` | **ChiNext** (创业板) | **±20%** | 2020-08-24 — **±10% before that date** |
| `900` / `200` | SSE / SZSE B shares | ±10% | |
| `430` `83x` `87x` `920` | Beijing (BSE) | ±30% | |

Two riders on that table:

- **ChiNext's ±20% is date-effective.** It was ±10% until 24 August 2020, so the
  rule carries an effective date rather than a bare percentage. Any audit range
  straddling it would otherwise be wrong on one side.
- **STAR and ChiNext have no price limit for the first five trading days** after
  listing; main board day one is special-cased too. `target_stock.ipo` supplies
  the listing date.

**Korea** — ±30% is KOSPI/KOSDAQ, KONEX is ±15%. Moot in practice: KR reads its
band from `target_oms` (§3), which already reflects whichever applies.

**Taiwan** — ±10%, but no limit for the first 5 days of a new listing (`ipo`).

### 1.2 The one China case the prefix cannot reach, and why it is safe

**ST / \*ST names are ±5% on the main board**, and that status lives in the stock
*name*, not the code — nothing in `target_stock` carries it. An ST name therefore
gets an assumed ±10% band: **twice as wide as the truth**.

That error runs in the safe direction, and the distinction matters enough to
state once:

- a band assumed **too wide** produces **false negatives** — real violations
  between 5% and 10% are missed
- a band assumed **too narrow** produces **false positives** — fabricated
  violations against legal prices

Only the second kind destroys trust in the report, so where the band must be
guessed, it is guessed wide. ST names are also self-correcting on any day they
actually hit their limit: the observed pin lands at ±5%, contradicts the ±10%
computed band, and §3.1 resolves it from observation. The residual exposure is
narrow — ST names, main board only, on days they approach but never touch ±5%.

Note that ST status does **not** reduce the band on STAR or ChiNext; those stay
±20%. So this gap is confined to `600`/`601`/`603`/`605`/`000`/`001`/`002`/`003`.

Everything not covered by a prefix rule is marked `assumed`, and its confidence
travels with it into the report rather than being hidden.

---

## 2. Scope

**In scope — eight markets.** Every parent order in `target` over
`[--start, --end]` whose `target_stock.country` is Hong Kong, Japan, Korea,
Malaysia, Thailand, China, Taiwan or India, and every child split in `workorder`
beneath it — **filled or not**. Unfilled splits are the point: the Hong Kong and
Korea notes are both about splits that failed to fill.

**Indonesia is out of scope.** Its band needs the IDX auto-rejection tier table
and the effective date of each revision, and unlike Japan there is no
observational rescue. ID orders are counted in a single `excluded_market` line and
no check runs on them. Re-admitting it is one market-table entry plus a tier
function — see §11.3.

**Also out of scope.** Markets outside the eight (counted and reported as
`RULE_UNKNOWN`, never silently passed). Any attempt to reconstruct what the
engine's config was set to — deviations from config-dependent rules are reported
as deviations, not violations, precisely because that config is not in the data.

**Not a gate.** This script reports. It does not block, amend or cancel anything.

---

## 3. Where the band comes from

Four layers, best first, plus an operator-supplied override (§3.3). Every
resolved band carries its provenance, and the report exposes it on every row.

| layer | source | available |
|---|---|---|
| **published** | `target_oms.limitup/limitdn` — the engine's own band, off the feed | **India and Korea only**, see below |
| **observed** | `qatt`: book locked (`qbid=qask`) or one-sided (`qask=0` up, `qbid=0` down). The pinned price *is* the band. | only when the stock actually hit the band |
| **computed** | base price from `qatt`: `preCls = price - netChange`, cross-checked against `price / (1 + pctChange/100)` | any stock that printed |
| **fallback** | `target_stock.orgclose^adjclose` on the **same date** — that column already holds the previous close | always |

`target_oms.limitup/limitdn` is **inconsistent across markets generally, but
always populated for India and Korea**. It is therefore read behind a
per-market allowlist — `BAND_FROM_TARGET_OMS = {IN, KR}`, a module constant —
and never trusted globally. This is what makes India checkable at all: its
per-scrip circuit filters cannot be derived from anything else we have.

Two consequences worth stating. For IN and KR the band is *what the algo itself
believed*, so a `LULD_CAP` breach there is unambiguous — the engine had the right
number and priced through it anyway. And for those two markets §6's
`LULD_GUARD_INACTIVE` becomes a direct read (`limitup <= 0` means no guard) rather
than an inference from behaviour.

Adding a market to that allowlist requires evidence, not optimism: `--diagnose`
prints per-market `limitup/limitdn` population and null rates precisely so the
set can be widened on data rather than on hope.

**`target_oms` holds many rows per `id_target`** — it is a tickstream, one record
per update, not one per order. Two rules follow:

- Take the band **prevailing at `t_transmit`**, via `aj` on the split's transmit
  time, not the last row of the day. For a static daily band the two agree; for a
  band that moves intraday only the `aj` is right, and it costs nothing to be
  correct in both cases. Where a split has no earlier `target_oms` record, fall
  back to the first record after it, then to the day's last.
- Take the last **non-null, positive** value, not the last row. `FlexOrderStream`
  only writes `limitup`/`limitdn` into the record when `quote != null`, so rows
  with a zero or null band are common and mean *"not known at this instant"*, not
  *"no band"*. Reading the last row blindly returns 0 on a stock that has a
  perfectly good band a few records earlier, and 0 would then read as
  `LULD_GUARD_INACTIVE` — a fabricated finding from a parsing mistake.

`mbref`'s `luld` table (`time; sym; atime; flag; limitup; limitdown`) holds the
feed-published band, but **we have no access to that server**, so it plays no
part. Noted only so nobody re-derives it as an option.

### 3.1 Provenance and reconciliation

| field | values |
|---|---|
| `band_src` | `override` · `target_oms` · `observed` · `qatt_netchange` · `target_stock` |
| `band_conf` | `confirmed` — published, or an observed pin agrees with the computed band within one tick<br>`assumed` — never hit the band; computed only<br>`widened_observed` — computed band contradicted, but the cause is known (§3.2), so the observed extreme is used instead<br>`contradicted` — contradicted with no known cause; band discarded |

**`contradicted` discards the computed band and suppresses that stock's LULD
findings.** Reporting "could not establish a band for 40 names" is worth more
than 40 fabricated violations. Suppressed stocks are counted and listed.

`widened_observed` is the deliberate exception, and it applies where a
contradiction has a *known* mechanism rather than an unknown one — Japan's
expanded limits (§3.2). There the observed extreme is a better band than no band,
so the finding is kept and its confidence marked down instead of being dropped.

Bands round **inward** to the tick grid (§3.4), so a band is never reported wider
than the rule allows.

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

Japan is not on the `target_oms` allowlist and has no reference source, so the
step table alone is not enough. **Three things make it wrong on exactly the
stocks that matter:**

1. **Expanded limits.** A name closing limit-up/down on a special quote gets a
   widened limit the next day — doubled, wider again on consecutive days. The
   step table then reports a band narrower than the real one, and every split
   between the two reads as a violation. The single largest false-positive source
   for JP.
2. **Base price is not always the previous close** — after a corporate action, or
   when TSE carries a last special quote forward. The `orgclose` / `adjclose`
   distinction.
3. **SQ days** — the notes already say the algo does not work for SQ. Derivable
   from the calendar: the second Friday of each month.

**Japan therefore gets two extra steps that no other market needs:**

**Prior-day expansion detection.** Read the *previous session's* `qatt` for the
same syms. If the stock ended pinned — locked or one-sided — at the previous
day's computed band edge, TSE widened today's limit, and the step-table value is
multiplied accordingly. One extra query per date, and the previous session is
already being fetched for nothing else, so it is a genuine addition rather than a
reuse.

**Observed widening.** When today's session `highPrice`/`lowPrice` exceeds the
computed band, the band is set to the observed extreme and marked
`band_conf=widened_observed` — **not** suppressed. Suppression is right when a
contradiction means "band unknown"; in Japan it has a known cause, so the
observed extreme is strictly better than nothing.

The second step is what makes the first one safe. The exact expansion multiplier
for consecutive limit closes is the one number here taken on trust, and if it is
wrong the observed data overrides it rather than propagating the error. That
ordering is deliberate: no Japan finding rests on the multiplier alone.

Indonesia would have needed a rescue of its own and has none available, which is
why it is out of scope rather than merely unreliable (§2).

### 3.3 The override file

Some bands cannot be derived from any data we have — India's per-scrip circuit
filters most of all (see §11). Rather than block on them, `--band-file` takes a
CSV that wins over every computed layer:

```
date,sym,limit_up,limit_dn,source
2026.07.16,600584.CH,41.83,34.23,exchange
```

Partial coverage is fine: a stock present in the file uses it and gets
`band_src=override`, `band_conf=confirmed`; a stock absent falls through to the
normal chain. This makes reference data **pluggable as it arrives** instead of a
precondition, and it is also how a single disputed case gets pinned for a
re-run.

### 3.4 The tick grid, from `tsid`

`target_stock.tstbl` / `tsid` carry the **tick size table id** — confirmed in the
engine, where `Stock.java` emits `|tstbl:` from `_ticksizetableid` and
`kdb/load_ticksizeids.q` loads one per sym out of `mbref`. Observed values: every
HK stock is `5872`; Chinese stocks are `10058` or `10216`.

The table those ids point into lives in `mbref`, which we cannot reach, so the id
is opaque on its own. It is still worth having, because **every stock sharing a
`tsid` shares a tick ladder by construction**. That makes the ladder recoverable
from data we already have:

```
for each tsid group:
    prices  <- distinct qbid, qask and trade prices from qatt over the range
    sort, take successive differences
    the ladder is the step function of (price level -> modal difference)
```

For `5872` this should reproduce the HKEX spread table (0.001 / 0.005 / 0.010 /
0.025 / 0.050 ...). The reconstruction is **self-validating**: a real ladder is a
clean step function, so if the recovered grid is ragged, the grouping is wrong and
the script says so rather than rounding against nonsense. Where recovery fails,
the scalar `target_stock.ticksize` is the fallback.

This replaces a scalar tick with the real grid, which is what makes the inward
rounding in §3.1 exact rather than approximate.

**A crosstab worth printing, as a check rather than a source.** A-share ticks are
a uniform 0.01 CNY, so China having *two* ids probably does not encode tick size
at all — more likely SH vs SZ, or main board vs STAR/ChiNext. `--diagnose` prints
`tsid` against symbol prefix (`600*` `601*` `603*` vs `000*` `002*` vs `300*` vs
`688*`).

This is no longer needed to *determine* the board — §1.1 derives that from the
prefix directly. Its value is as an **independent second signal**: if `tsid`
partitions China the same way the prefix rule does, two unrelated sources agree
and the board mapping is confirmed. If they disagree, one of them is wrong about
a stock and the crosstab says which — a cheap correctness check on the most
consequential band rule in the script.

---

## 4. Classifying an order

Per parent, from `target.side` and `target_stock.country`:

| class | condition |
|---|---|
| `shortsell` | `side = sellshort` |
| `luld` | country has a band rule |
| `both` | both of the above |
| `neither` | HK long orders, and anything outside the eight markets |

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
| `LULD_CAP` | JP KR MY TH CN TW IN | `limit_dn <= price <= limit_up` | violation |
| `LULD_CLIENT_LIMIT` | all | split not more aggressive than parent `limit_price` | violation |
| `LULD_OFFSET` | CN JP | offset in ticks from the unfavourable band | deviation |
| `LULD_REJECT` | JP KR MY TH CN TW IN | split **rejected by the venue** while priced outside the band — the exchange confirms the breach (§5.2) | violation |
| `LULD_REJECT_INBAND` | JP KR MY TH CN TW IN | rejected while priced *inside* our band — our band is suspect, not the algo (§5.2) | deviation |
| `SS_HK_ASK` | HK | `price >= qask` at `t_transmit`; a market-order short sell fails by construction | violation |
| `SS_UPTICK` | JP KR MY | `price > lastPrice`, or `>=` when `qatt.trdTick` shows the last tick was up | violation |
| `SS_TH_LTP1` | TH | `price = lastPrice + ticksize` | deviation; violation if *below* |
| `SS_KR_CLAMP` | KR | an uptick price exceeding the band must be capped at the band, not sent through | violation |
| `SS_HK_CHASE` | HK | resting split, ask moved away > `--chase-ticks` for > `--chase-secs`, `count_chaseprice = 0`, no replacement child | violation |
| `RULE_UNKNOWN` | **CN TW IN short sells** | counted as **unverifiable**, never as passing. Deliberately not checked — see §5.1 | — |

### 5.1 Markets we deliberately do not check for short sell

**Taiwan, China and India emit `RULE_UNKNOWN` and no short sell check runs on
them.** Their LULD bands are unaffected — those are solid (§11.5) and `LULD_CAP`
runs there as normal. This section is the *reason*, and the head start for
whenever the desk decides to enable them.

The rules below were researched from the regulators. They are **not implemented**,
because a short sell finding is a compliance assertion and the confidence here is
not compliance-grade: China and Taiwan come from exchange rules and regulator
releases — secondary sources — and short selling rules in both markets have been
changed repeatedly under market stress. Enforcing a rule we are not certain of
would put fabricated violations next to real ones and cost the whole report its
credibility, which is the §10.7 principle applied to rules rather than to bands.

**China — an uptick rule.** SSE and SZSE margin trading rules require a short sale
(融券卖出) be declared at a price **not lower than the latest transaction price**,
falling back to the **previous close** where the stock has not yet traded. Same
shape as Japan, Korea and Malaysia, so enabling it is `SS_UPTICK` plus a no-trade
fallback. Short selling is also confined to the exchanges' designated eligible
list (标的证券), which we do not have.

**Taiwan — a previous-close floor, not a tick rule.** The one most likely to be
got wrong by analogy. SBL short sales must be entered **at or above the previous
day's closing price** — the *previous close*, never the last trade — and it is
conditional:

- it bites when the previous close fell **more than 3.5%** (or, absent a trade,
  the lowest sell order at close was down more than 3.5%)
- margin-eligible shares are **exempt**, unless the previous close was at **limit
  down**, or the price was the lowest recorded sell order at close with no trade
- TWSE and TPEx publish the **daily exemption list** (CSV, from 2013-09-23)

We already compute the previous close for §3, so the floor and the −3.5% trigger
would be free, and the exemption list is the only external piece.

**India — there is no price rule.** Verified against SEBI's *Broad framework for
short selling*, read in full. Its nine clauses cover definition, permitted
investors, the naked-shorting ban, gross settlement, deterrents, SLB, F&O
eligibility, disclosure and position reporting. **None constrains the price at
which a short sale may be entered** — India never adopted an uptick rule. This is
the most useful finding in the section: it means no amount of further work will
produce a price check for India, and any future attempt to add one is a mistake.

What India does impose, if it is ever enabled, is clause 4 — no institutional
intraday square-off — checkable as a `sellshort` parent offset by a same-day buy
in the same sym.

**To enable any of these** takes one entry in the rule table, its fixtures in the
self-test, and a line in §11.5. The blocker is desk confirmation, not engineering.

### 5.2 Rejections, which cut both ways

A split priced outside the band that actually reaches the venue **comes back
rejected**. That makes rejections a second detection channel for LULD, and a
better-evidenced one than `LULD_CAP`: where `LULD_CAP` rests on our
reconstruction of the exchange's rule, a rejection is the exchange's own answer.
`LULD_REJECT` therefore carries **no band-confidence caveat** — it is true even
if our band is wrong.

It does not replace `LULD_CAP`, because plenty of LULD problems never produce a
rejection at all:

- `CLOSE_BAD_PRICE` — we stopped the order ourselves and it was never sent
- the no-split family (§6) — the order was never built
- a bad price that happened to stay inside the band

Three outcomes, and the middle one is about **our** band rather than the algo:

| split price vs our band | reading |
|---|---|
| outside | `LULD_REJECT`, violation — the venue agrees with us |
| inside | `LULD_REJECT_INBAND`, deviation — either our band is too wide, or the reject was not price related. **Not charged to the algo.** |
| no band resolved | `LULD_REJECT`, violation — rejected on a band market we could not price, worth seeing precisely because we cannot judge it |

**Rejections and acceptances are also band evidence**, and this is the more
valuable half. A price the venue *accepted* is by definition inside the real
band, so the extreme accepted prices bound it from the inside:

```
acc_high > computed.up   ->  the computed band is too narrow -> contradicted
acc_low  < computed.dn   ->  same
```

This is a sharper test than the session `highPrice`/`lowPrice` of §3.1, because
it is **our own order and the venue's own answer to it** rather than something
inferred from the tape. It feeds `reconcile_band` alongside the pin and the
session extremes, so in Japan it widens the band and everywhere else it
suppresses it.

Only states that **prove** a split reached the market count as accepted —
`acked`, `leave`, `filled`, `done`, `rpld`, `expired`, `cxl`, `cxlord_succeed`.
`transmitted` is deliberately excluded: sent is not the same as accepted.

### 5.3 Two reference markets, deliberately

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
- **LULD fails through both channels.** A split priced outside the band that
  reaches the venue is `REJECTED`, and §5.2 checks exactly that. But a LULD
  problem often never reaches the venue at all — `CLOSE_BAD_PRICE` (7 call
  sites, all on the price path), `CLOSE_TAKE_OUTOFMONEY`, `CLOSE_STOCK_HALT`,
  `CLOSE_ORDER_HALT`, `STOPPED_VOLATILITY_TAG262` / `TAG325` — or produces no
  order to reject. So rejections are an **additional** channel for LULD, never
  the only one, and no LULD check is gated on state.

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
--band-file          CSV of known bands that overrides every computed layer
                     (§3.3); partial coverage is fine
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
5. **`target_oms` is read behind a two-market allowlist**, not globally. Its data
   is inconsistent across markets but always populated for India and Korea, so
   `{IN, KR}` use it and nobody else does. Widening the set is a data question,
   and `--diagnose` prints the null rates that answer it.
6. **Short sell checks run only where the rule is confirmed** — the five markets
   our own sheet states. Taiwan, China and India were researched (§5.1) and
   deliberately left unenforced: a short sell finding is a compliance assertion,
   and secondary sources on markets that revise their rules under stress are not
   a basis for making one. They report `RULE_UNKNOWN`, which is honest, rather
   than passing, which would be false.
7. **Where a band or eligibility list must be guessed, guess in the direction that
   under-reports.** Wide bands, downgraded severities. A missed violation is a gap;
   a fabricated one against a legal price is what makes the whole report
   ignorable (§1.2).
8. **Indonesia is excluded rather than carried as unverifiable.** A market that can
   only ever emit `RULE_UNKNOWN` adds a row to every table and no information.

---

## 11. What is missing to get every band exactly right

Ordered by value per unit of effort. The script ships useful without any of
these -- §3.1 already reports `assumed` and suppresses `contradicted` rather than
guessing -- but each one converts a market from approximate to exact.

### 11.1 Settled since the first draft

| was missing | resolved by |
|---|---|
| India per-scrip circuit filters -- previously a hard blocker | `target_oms.limitup/limitdn`, always populated for IN (§3) |
| Korea board bands (KOSPI/KOSDAQ vs KONEX) | same -- the published band already reflects whichever applies |
| China day-one listings, Taiwan first five days | `target_stock.ipo` |
| the tick grid | `tsid` grouping plus empirical recovery from `qatt` (§3.4) |
| Japan expanded limits | prior-day pin detection plus observed widening (§3.2) |

### 11.2 Still worth having, cheap

**A `tsid` x symbol-prefix crosstab for China.** Not needed to determine the
board — §1.1 does that from the prefix — but it is a free second opinion on the
mapping. Agreement confirms it; disagreement names the stock that breaks it.
`--diagnose` prints it.

**Per-market `target_oms` population rates.** The allowlist is `{IN, KR}` on your
word. If JP, MY or TH turn out to be populated too, each one moves from computed
to published and the whole of §3.2 becomes a fallback rather than the primary
path for Japan. `--diagnose` prints null rates per market for exactly this.

**A `target_stock` sample, 20 rows across the eight markets.** Lower value than
before -- `ipo` and `tsid` are now understood -- but `segment`, `stype`, `etf`,
`preferred` and the unknown `mrp` / `p2c` / `mos` / `tac` are still undecoded, and
`segment` would replace prefix inference for CN and KR boards.

### 11.3 Still genuinely external

| what | missing | why it cannot be derived | effect when absent |
|---|---|---|---|
| **China** ST / \*ST status (±5%) | the stock **name** | carried in the name, not the code | band assumed wide → false negatives only (§1.2) |
| **CN, TW, IN** short sell rules | desk confirmation, not data | researched in §5.1 but not compliance-grade | `RULE_UNKNOWN` — no short sell check runs |
| **Taiwan** daily exemption list | TWSE / TPEx CSV | needed only if TW is ever enabled | n/a today |
| **Korea** caution / warning designations | exchange, intraday | largely moot — KR reads its band from `target_oms` | none material |

`--band-file` (§3.3) covers the band gaps, and **downgrades a finding rather than
suppressing it**, so a missing list understates severity instead of manufacturing
it. The short sell gaps are not files — they are a decision.

**Re-admitting Indonesia** (§2) needs the IDX auto-rejection tier table plus the
effective date of each revision. With that it is one market-table entry and one
tier function; the rest of the pipeline already handles it.

### 11.4 The exact answer, if it can ever be exported

`Stock.java` holds an **`IFCPriceLimit`** per stock:

```java
public interface IFCPriceLimit {
    String   id();
    double[] priceLimits(IFCStock, OrderSideType, IFCQuote, double);
    boolean  fromFeed();
}
```

and `Stock.toString()` emits `|pricelimit:<id>|dn:<limitdn>|up:<limitup>`. The
implementations are in a jar absent from the `ai3` slice; the per-stock
assignment lives in the security master.

This is the band the algo is actually held to, and `fromFeed()` says whether it
came off the feed or from a rule. An export of the rule table plus its per-stock
assignment would make every market exact at once, and would change the question
from *"does the algo match the exchange's rule"* to *"does the algo match its own
configured rule"* -- the more useful one, since a mis-assigned `pricelimit` id is
itself a defect worth finding.

### 11.5 Coverage as designed

**Bands — every in-scope band market is covered.**

| | band | confidence |
|---|---|---|
| **India, Korea** | published (`target_oms`) | exact |
| **China, Taiwan, Malaysia, Thailand** | computed close, board from prefix | exact |
| **Japan** | step table, expansion-corrected, observation-widened | exact for ordinary names; `widened_observed` for the rest |
| **Hong Kong** | no band rule exists | n/a — short sell checks only |
| **China ST names** | unresolvable without a name feed | `assumed` wide, self-correcting on any day they pin (§1.2) |

**Short sell — five of eight markets are checked.**

| | rule | status |
|---|---|---|
| **HK** | Always Ask | checked |
| **JP, KR, MY** | UpTick (Trend) | checked |
| **TH** | LTP + 1 tick | checked |
| **CN, TW, IN** | researched but not enforced (§5.1) | `RULE_UNKNOWN` |

Every short sell finding the script emits therefore rests on a rule from our own
sheet. Taiwan, China and India are visible in the scorecard as unchecked rather
than silently passing, so the gap is legible without being asserted.
