# Stale price check — KdbMonitor dashboard

Take orders under an **activated** parent that were **not sitting on the touch
they should have been**, with the book beside the price they were sent at.

**Only breaches come back, so an empty panel is the good answer.**

Built after orders in ai3 went out priced off market data that had gone stale.

**Real-time only.** Both datasets run against their environment's live server
and always mean today. There is no period switch on this dashboard, which is
why the `.q` carries no `{{#historical}}` block and no `{{conn:…}}` handle —
those exist to stitch a historical range onto today, and there is no range
here to stitch.

Same logic as `queries/stale_price_check/stale_price_check.q`, repackaged the
way KdbMonitor consumes it: chained datasets, one raw q query each, with the
tokens KdbMonitor fills in before sending.

| File | What it is |
| --- | --- |
| `stale_price_check_kmonitor.q` | **The source of truth.** The two dataset queries, with the reasoning. |
| `build_dashboard.py` | Reads the `.q` and writes the JSON, so the two cannot drift. |
| `stale_price_check_kmonitor_dashboard.json` | Generated. This is what you import. |

## Why a take order is the sharp test

A take lifts the offer or hits the bid. Its price is **not a judgement call** —
it is dictated by the book at the instant the order was generated. Sent at
anything else, the book the algo was looking at was not the book that existed.

That is a far tighter test than holding an order price against the last print,
which is what this dashboard did before and which cannot tell a stale quote
from a wide spread.

The reference is the **far touch**, by side:

| Side | Measured against | Because |
| --- | --- | --- |
| buy | `qask` | you lift the offer |
| sell | `qbid` | you hit the bid |
| short sale | — | **dropped** |

Short sales are excluded because a short-sale price test can stop the order
sitting at the bid, and it would then read as off-touch for a reason that has
nothing to do with stale data.

## Two filters decide what is even asked

**Aggressive takes are dropped** — a buy *above* the offer, a sell *below* the
bid. Those cross and fill at the touch anyway, so they are deliberate
aggression rather than a book the algo misread.

Stale data shows up as the **opposite**: a book that has moved away leaves the
buy below the offer and the sell above the bid, sitting there not filling. That
is the direction this dashboard keeps, so after the filter `ticks_off` is `<=0`
on buys and `>=0` on sells.

| `ref_side` | `ticks_off` | |
| --- | --- | --- |
| `ask` (buy) | `> 0` | **dropped** — crossed the offer |
| `ask` (buy) | `<= 0` | kept — this is where stale shows |
| `bid` (sell) | `< 0` | **dropped** — crossed the bid |
| `bid` (sell) | `>= 0` | kept — this is where stale shows |

One consequence worth knowing: if stale data made the algo cross *through* the
touch, that order is dropped too. It is indistinguishable from deliberate
aggression, and it fills at the touch regardless — so it costs nothing. The
direction that costs you is the one kept.

**Then only breaches come back.** An order sitting on the touch is the book
behaving and there is nothing to look at, so `ok` rows drop out. `flag` still
says which kind of finding each row is, and rows that could not be tested
(`noquote`, `notick`) always survive both filters — a null comparison is false
either way, so they are never mistaken for aggressive.

## Installing it

1. Change the two environment names if yours are not `OMS` (`target_state`,
   `workorder0`, `target_stock`) and `QATT` (`qatt`). They are the `env=`
   fields in the `/ ==== DATASET: … ====` headers of the `.q`, and **nowhere
   else** — nothing inside either query names an environment.
2. `python build_dashboard.py`
3. KdbMonitor → **Dashboards → Import** → pick
   `stale_price_check_kmonitor_dashboard.json`.

Each environment needs a **real-time** server registered in Admin. A historical
twin is not required: the dashboard is declared `periods: realtime` and both
datasets are pinned to `time_mode: realtime`, so neither is ever asked for one.

## Run `stalePriceVocab` first

The venue and side vocabularies are **not known to this query**. `venue` is
matched as *contains TAKE*, case-insensitively; `side` is read off its text —
anything beginning `b` buys and looks at the offer, everything else sells and
looks at the bid.

Both raw values stay in the output, so a wrong reading is visible rather than
silent. But before trusting a number here, run the bare-q helper against the
same server:

```q
q)stalePriceVocab[h;10]
```

It lists the `venue` / `venuetype` / `side` values actually in use, with counts,
and deliberately does **not** apply the venue filter. A filter that silently
matches nothing, or a side that silently reads as a buy, shows up there instead
of being inferred from an empty dashboard.

## What the columns mean

| Column | |
| --- | --- |
| `order_price` | what the order was sent at, **as generated** |
| `ref_side` | `ask` or `bid` — which side this row was measured against |
| `qbid` / `qask` | the book at or before `t_gen` |
| `touch` | the far touch: `qask` for a buy, `qbid` for a sell |
| `ticksize` | that stock's tick, off `target_stock` |
| `ticks_off` | `(order_price - touch) % ticksize`, **signed** |
| `ptime` / `quote_age_ms` | when that quote was, and how old it already was at `t_gen` |
| `now_age_ms` | how long since the book last moved on that name |
| `flag` | `off` / `noquote` / `notick` — `ok` never appears, those rows are filtered out |

`ticks_off` means the same thing on both sides — how far the price sits
**above** the touch. A buy at `+3` went three ticks through the offer; a sell at
`+3` was three ticks short of hitting the bid. **Max ticks** flags on the
absolute value, so `0` means it must be exactly on the touch.

`noquote` and `notick` both mean the test **could not be run** — no two-sided
quote in the window, or no tick size for that stock. That is a different
statement from the test passing, so they come back rather than being filtered
away with the `ok` rows, and they sort to the top.

## The lookback, and the two windows

**Lookback (minutes)** bounds `t_gen` — how recently the order was created. It
exists because reading the whole session out of `qatt` is too slow to run on a
refresh.

`qatt` is read from **twice that far back**. An order generated at the very
start of the order window still needs quotes *before* it to as-of onto:

```
qatt scanned   |<--------- 2 x lookback --------->|
take orders                   |<--- lookback ---->|
               t0-lookback   t0                  now
```

So `noquote` means "no two-sided quote in the scanned window", not "never quoted
today" — still a finding, since a name we are taking on that has not quoted in
twice the lookback is stale by any reading.

**The lookback is the only bound there is.** With no historical period to fall
back on, "the last 10 minutes" always means the last ten minutes, and both
windows are always set from it.

## The order price is the one it was GENERATED with

`workorder0` writes a row per state change. `t_gen` is stamped at generation and
never moves — but `price` **is** rewritten, because a chase repoints it. So the
report reads the **first** row by `sequence` for everything describing the
order. Only `state` comes off the last row.

Taking the last row instead pairs a repriced price with a generation timestamp
and holds it against the book at generation — right time, wrong price. That is
what this query did until it was caught against a real order.

The consequence to know about: **a repricing is not checked.** A chase at 14:30
also used market data and could also have been stale. Doing that properly means
one row per price change against the book at *its own* timestamp — `workorder0`
carries a per-row `time` for exactly that.

## Which server answers what

The RDB of each environment, today, and nothing else is ever asked. `live_takes`
goes to `OMS`, `touch_check` goes to `QATT`, and neither reaches past the
environment its `env=` names.

That is the whole story here, and it is worth saying plainly because the other
dashboards in this folder are not like it: they offer both periods, so they
land on an HDB and reach back to the RDB through `{{conn:ENV:realtime}}` to
stitch today on. This one has nothing to stitch.

Two things follow from being live-only:

- The quote RDB has **no `date` column**, so `touch_check` stamps one on with
  `update date:.z.D` and the as-of join matches on it — exact on date and sym,
  as-of on time. Both sides carry today and agree about it. The column stays in
  the join rather than being dropped, so a historical period could be added
  later without an order ever being dated against another day's book.
- **`now_age_ms` means "as of right now"**, always. There is no past date for it
  to be measured to a session end instead.

## Untested edge, worth checking on the first run

When **no take orders are found**, dataset 2 returns early and hands the widgets
the empty *order* table rather than an empty result carrying the columns they
name. A quiet window may therefore show as a column error rather than as no
rows. This is the same early return `limit_up_down_v2.q` ships, kept rather than
changed because there is no kdb here to prove a replacement. If it misbehaves,
drop the `if[0=count w; :w]` line in the `touch_check` block.

## Why only two datasets

The other dashboards here use three because they have to go back to the OMS for
the blotter. Dataset 1 already carries every order field and the tick size, so
dataset 2 does the quote lookup and returns the finished table. The whole order
table crosses to the quote server because an `aj` needs both sides local.

Editing the q means editing the `.q` and re-running the builder. Editing the
JSON by hand works once and is lost the next time anyone regenerates it.
