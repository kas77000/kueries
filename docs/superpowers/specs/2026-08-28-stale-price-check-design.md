# Stale price check — design

**Date:** 2026-08-28
**Problem:** orders in the ai3 algo went out priced off market data that had
gone stale. There is no way to see, live, which orders that is still happening
to.

## What it answers

For every **take** order under a live parent: the price it was sent at, beside
**the touch it should have been sitting on** at the same instant.

## Why a take order, and not any order

A take lifts the offer or hits the bid. Its price is not a judgement call — it
is dictated by the book at the instant the order was generated. Sent at
anything else, the book the algo was looking at was not the book that existed.

The first version of this query held the order price against the last **print**
in `qatt`. That cannot tell a stale quote from a wide spread, and it needs a
bps threshold nobody can pick before seeing the data. The touch test needs no
such threshold: the correct answer is *on the touch*, and the only tolerance is
how many ticks of slack the venue and the algo allow.

| Side | Measured against | |
| --- | --- | --- |
| buy | `qask` | you lift the offer |
| sell | `qbid` | you hit the bid |
| short sale | — | **dropped** |

Short sales are excluded because a short-sale price test can stop the order
sitting at the bid, and it would then read as off-touch for a reason unrelated
to stale data.

## Row set

1. **Live parents.** Latest `state` per `date,id_server,id_target` in
   `target_state`; keep the ones now `` `activated ``.
2. **Take children.** `workorder0` rows under those parents whose `venue`,
   upper-cased, is `like "*TAKE*"`. The constraint sits last in the where
   clause so it runs on the rows the cheap ones already left.
3. **One row per child, and it is the FIRST one.** `workorder0` carries a row
   per state change. `t_gen` is stamped at generation and never moves, but
   `price` is rewritten when the algo chases. So everything describing the
   order comes off the first row by `sequence`; only `state` is read from the
   last.

   Taking the last row pairs a repriced price with a generation timestamp and
   holds it against the book at generation: right time, wrong price. That was
   the original implementation and it was wrong; caught 2026-08-28 against a
   real order.

   **Known gap:** a repricing is not checked. Doing that means one row per price
   change against the book at *its own* timestamp, for which `workorder0`
   carries a per-row `time`.
4. **Market orders dropped** (`0<price`) — no order price to hold against a
   touch.
5. **Tick size** joined from `target_stock` on `date,id_server,id_target`. Only
   `ticksize` is taken: `target_stock` also carries `sym`, which an `lj` would
   overwrite.

## The comparison

`qatt` filtered to **two-sided quotes** (`0<qbid`, `0<qask`) — a one-sided book
has no far touch, and a zero on either side would read as a touch of nothing.

`aj[`sym`time; orders; quotes]` puts each order on the last quote at or before
its `t_gen`. `t_gen` and `qatt`.`time` come off the same clock, so they compare
directly — no timezone or DST conversion.

```
ticks_off = (order_price - touch) % ticksize
```

Signed, and it means the same thing on both sides: how far the price sits
**above** the touch. A buy at `+3` went three ticks through the offer; a sell at
`+3` was three ticks short of hitting the bid. `maxTicks` flags on the absolute
value, so `0` means it must be exactly on the touch.

`flag` is `off` / `noquote` / `notick` / `ok`. The middle two mean the test
could not be **run** — no quote in the window, no tick size for that stock —
which is a different statement from the test passing, so neither reads as `ok`.

## Vocabularies this query does not know

`venue` and `side` are matched on their text, not against an enum: *contains
TAKE* case-insensitively, and anything beginning `b` buys. Both raw values stay
in the output so a wrong reading is visible.

`stalePriceVocab[h;lookback]` lists the `venue` / `venuetype` / `side` values
actually in use with counts, and deliberately does not apply the venue filter.
A filter that silently matches nothing, or a side that silently reads as a buy,
then shows up on the terminal rather than as an empty report. Run it first.

## The lookback, and the two windows

`lookback` is in minutes and bounds `t_gen`. It exists because reading the whole
session out of `qatt` is too slow to run live. Named `lookback`, not `mins`:
`mins` is the q keyword for running minimums.

`qatt` is read from **twice that far back**, because an order generated at the
very start of the order window still needs quotes before it to as-of onto:

```
qatt scanned   |<--------- 2 x lookback --------->|
take orders                   |<--- lookback ---->|
               t0-lookback   t0                  now
```

So `noquote` means "no two-sided quote in the scanned window", not "never quoted
today" — still a finding, since a name we are taking on that has not quoted in
twice the lookback is stale by any reading.

In the KdbMonitor version the lookback applies to the **real-time period only**;
a historical period passes `00:00:00.000` to both windows, because "the last 10
minutes" cannot mean anything on a past date.

`sym in syms` stays the *first* constraint in the `qatt` where clause, ahead of
the time bound. The RDB keeps `` `g#sym `` and the where clause can only use
that attribute on the constraint it applies first; the time cut then runs on
what survives.

## Where it runs

`qatt` is on the quote server, `workorder0` / `target_state` / `target_stock` on
the OMS, and an `aj` needs both sides local. So the query runs **from the quote
session** and ships a lambda to the OMS over a handle. Serialized lambda, not a
query string. Pass `0i` when the order tables are local.

## KdbMonitor version

| Dataset | Env | Does |
| --- | --- | --- |
| `live_takes` | OMS | the row set above, with `ticksize` |
| `touch_check` | QUOTES | takes `{{table:live_takes}}` whole, the quote lookup, returns the blotter |

Both halves use the `hasToday` stitch guard. The quote RDB has no `date` column,
so its half gets `update date:.z.D` and the as-of joins on date too, which stops
a historical range dating an order against another day's book. `now_age_ms` on a
past date is measured to that day's session end.

Reader parameters: `lookback_mins` (default 10, real-time only) and `max_ticks`
(default 5).

## Deliverables

```
queries/stale_price_check/stale_price_check.q      stalePriceCheck / Vocab / Rows
kmonitor/stale_price_check/stale_price_check_kmonitor.q
                          /build_dashboard.py
                          /stale_price_check_kmonitor_dashboard.json
                          /README.md
```
