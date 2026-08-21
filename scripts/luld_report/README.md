# luld_report

The orders whose stock was pinned at its daily price limit while they were live
— their completion and rejections by market, and then the ones that matter: the
orders where the limit was on the side we could have traded, and we sent nothing
into it.

```
python scripts/luld_report/luld_report.py
python scripts/luld_report/luld_report.py --monthly 2026-07
python scripts/luld_report/luld_report.py --demo        # preview, no kdb
python scripts/luld_report/luld_report.py --self-test
```

Sibling of [`short_sell_report`](../short_sell_report/README.md) — same page,
same conventions, same headline figures. What is new is the bottom table.

**Overall completion is the mean of the eight market percentages**, each market
counting once whatever its size, with a market that had no LULD orders left out
rather than averaged in as a zero.

---

## The pages

**Page 1** — four headline figures, the eight markets, completion and rejections
by market:

```
Limit Up / Limit Down Order Report
By market · 2026-07-24 18:37
────────────────────────────────────────────────────────────────────
103                43.6%                309           10
Orders at a limit  Overall completion   Rejections    Favourable, no split

┌───────────┬────────┬────────────┬────────────┬────────────┬────────────┐
│ Market    │ Orders │  Order qty │   Executed │ Completion │ Rejections │
├───────────┼────────┼────────────┼────────────┼────────────┼────────────┤
│ Japan     │     26 │  3,504,500 │  2,217,240 │      63.3% │          0 │
│ Korea     │     19 │  2,278,000 │    767,927 │      33.7% │        196 │
│ …         │        │            │            │            │            │
└───────────┴────────┴────────────┴────────────┴────────────┴────────────┘
```

**Page 2** — *Favourable limit, no split on the market*:

```
┌────────┬─────────┬──────┬───────────┬──────────┬────────────┬───────┬──────┬─────────────┬──────┬────────┐
│ Market │ Symbol  │ Side │ Order qty │ Exec qty │ Completion │ Limit │ At   │ Limit period│ Mins │ Splits │
├────────┼─────────┼──────┼───────────┼──────────┼────────────┼───────┼──────┼─────────────┼──────┼────────┤
│ Japan  │ 1010.JP │ sell │   215,000 │        0 │       0.0% │  12.5 │ up   │ 11:10–11:26 │   16 │      0 │
│ Taiwan │ 1070.TT │ sell │   185,000 │        0 │       0.0% │  27.5 │ up   │ 12:10–12:46 │   36 │      0 │
│ Korea  │ 1045.KS │ buy  │   197,500 │   80,975 │      41.0% │ 21.25 │ down │ 11:45–11:56 │   11 │      1 │
└────────┴─────────┴──────┴───────────┴──────────┴────────────┴───────┴──────┴─────────────┴──────┴────────┘
```

**Completion is the number in red**: on a page about limits we could have traded
into, how little of the order got done *is* the finding. Order qty beside it is
what that percentage is a percentage of, so the quantity missed is still there —
`Order qty × (1 − Completion)` — without a column spent printing it.

Sorted by that missed quantity, biggest first — the page is read from the top.

`--monthly` inserts a page of **Completion by day** and **Rejections by day**
between the two, as full-width vertical charts.

Written to `--out-dir` as one PDF and a PNG per page.

---

## Where the limit comes from

**The book, not a rule.** A stock at its limit stops having a two-sided quote:
at limit up nobody will offer, at limit down nobody will bid, and a locked book
(`bid = ask`) is the same thing caught mid-transition. So a **limit period** is a
contiguous run of `qatt` ticks where one side is missing or the two are equal:

```q
lim: ((qbid=qask)&0<qbid) | ((0=qbid)&0<qask) | ((0=qask)&0<qbid)
grp: sums differ lim by sym          / contiguous runs -> one row each
```

Run boundaries come from the **normal ticks between them**, not from a gap
threshold — two limit periods either side of a spell of two-sided quoting are
genuinely two periods, and a threshold would have to guess where.

Reading the limit off the book rather than off a band table is what lets this
cover **Indonesia and China**, where
[`luld_shortsell_check`](../luld_shortsell_check/README.md) has no derivable
band. It is also why every window here is a **floor rather than an estimate**: a
pinned stock often stops quoting altogether, so a period **ends at the last tick
that proved it**, never later. Under-reporting is the direction to be wrong in.

**Which limit it was** is decided per period, not per tick: `noask` and `nobid`
are counted across the run and compared. A tie is `None` and the period is
**dropped** — a window whose side is a guess cannot say whether it was
favourable, and that is the whole question.

---

## The table at the bottom

An order qualifies when **all** of these hold:

| | |
|---|---|
| **Favourable** | selling into a limit up, or buying into a limit down. There is a queue resting at the band and we are the other side of it. |
| **Long enough** | the limit period overlapped the order's live window by at least `MIN_PIN_MINS` (default 2). Below that it is a print, not a period. |
| **Still to do** | the order had quantity left — `size` − Σ`make` > 0. |
| **Nothing on the book** | **no** child split was on the market at any point during that overlap. |

**"On the market" means `t_on_market` → `t_off_market`**, not when the child was
generated or sent — `t_gen` and `t_transmit` answer when we decided and when we
sent, which is a different question. A split still open at the end counts as
active.

```
limit period:        |--------- limit up ---------|
split A:           |----|                            no  (gone before)
split B:                 |------|                    ACTIVE
split C:                               |---------->  ACTIVE (still open)
split D:                                        |--| no (after)
```

This is stricter than `luld_shortsell_check`'s `LULD_FAVOURABLE_NO_SPLIT`, which
only asks whether the order ever produced a child and ignores timing — so an
order that worked all morning and slept through an afternoon limit is invisible
to it and caught here.

**The `Splits` column is the order's total child count**, and it is what tells
the two cases apart: `0` means the order never worked at all; a number means it
worked, just not while the limit was there. Those are different conversations.

**One row per order**, not per window. An order that missed three limit periods
is one conversation; the row shows its longest, and the others are counted
behind it.

**Guards.** A close-only order with a window of 30 minutes or less is doing what
it was told and is not flagged. A rejected split does **not** excuse us — it
never reached the market, so it cannot have been resting there.

The findings table paginates at 28 rows, capped at 4 pages. Anything past that
is **counted on the page and logged**, never silently dropped.

---

## Replaced orders are chained

The engine writes a **new `id_target`** every time an order is re-sent, so
counting target rows counts one order several times. Attempts are chained back
together on **FIX tag 9604**, the client's own order id — the rule lives in
[`scripts/lib/order_chains.py`](../lib/README.md) and is shared with
[`short_sell_report`](../short_sell_report/README.md).

**Splits are pooled across the chain**, and that is the part that matters here.
Asking "was anything of ours on the book during the limit" of a *single attempt*
gives a **false positive** whenever a sibling attempt was the one resting there:

```
limit:              |------- limit up -------|
attempt 1:  |------|                            re-sent at 11:36
attempt 2:          |=========== on the book ===========|

  per attempt   attempt 1 "sent nothing during the limit"   <-- WRONG
  per order     the order WAS on the book                   <-- right
```

A finding pointing at nothing is worse than no finding, so a check asserts both
halves: chained, nothing is reported; un-chained, the first send is flagged.

The **order's live window** spans every attempt — first send to last end — so a
limit that arrives between two sends still counts as having touched the order.
A close-only order is only excused if **every** attempt was close-only; one
working attempt is a working order.

Every run reports what the chaining did, and where it is not safe:

```
chains: 924 targets -> 429 orders (54 chained, longest 162)
12 of 924 targets (1.3%) carry no tag 9604 and stand alone: JP 11, HK 1
WARNING: 3 chains disagree on algo - a 9604 is covering more than one order
         and these numbers are WRONG
```

A chain that executes more than it asked for is **un-chained** back into one
order per target and its findings dropped, rather than printing a completion
over 100%. `--keep-over` opts out; `--chain-qty` picks the quantity rule.

---

## Scope

**Japan, Korea, Malaysia, Thailand, Indonesia, China, Taiwan and India** —
everywhere we trade that has a daily price limit. All eight always print, so a
market with no LULD flow reads as a zero row rather than vanishing.

The market is the **sym suffix**, and two of them carry several:

| suffix | market | | suffix | market |
|---|---|---|---|---|
| `.JP` | Japan | | `.IJ` | Indonesia |
| `.KS` | Korea | | `.CH` `.C1` `.C2` | China |
| `.MK` | Malaysia | | `.TT` | Taiwan |
| `.TB` | Thailand | | `.IN` `.IS` | India |

Hong Kong, Australia, Singapore and New Zealand are **not** here: no daily price
limit, so there is no limit to be up or down against.

**The top table counts only orders the limit actually touched** — a limit period
on that stock overlapping the order's own live window. An order that finished
before the stock went limit is not a LULD order, however dramatic the
stock's afternoon was. So the rejection counts are LULD-related rejections, not
the market's whole day.

Rejections are `workorder` rows in state `` `rejected ``, the same rule the
short-sell report uses. `invalid_ack` and `fail_ack` are a different failure and
are deliberately not counted.

---

## Tables it reads

`target` and `workorder` off the **order** server, `qatt` off the **quote**
server. Four endpoints, because each exists realtime and historical:

```python
ORDER_SERVER_RT   = "CHANGEME:5012"
ORDER_SERVER_HIST = "CHANGEME:5010"
QATT_SERVER_RT    = "CHANGEME:5013"
QATT_SERVER_HIST  = "CHANGEME:5011"
```

Host and port only — all four are open processes.

The order server and `qatt` stamp their times on the **same clock**, so an order
window and a limit period are compared with no timezone or DST conversion. This
is relied on throughout and is not a gap.

Nothing is grouped in the order query: a target is an order, a workorder is a
child order, and the sums and counts happen in Python where `--self-test` can
prove them.

---

## Modes

| | servers | dates | pages |
|---|---|---|---|
| *(no flags)* | realtime | the session in progress | summary + findings |
| `--date 2026-07-01` | historical | that one session | summary + findings |
| `--monthly 2026-07` | historical | every weekday of the month | summary + by-day + findings |

`--min-mins` overrides how long a favourable limit must overlap an order before
it is reported.

---

## Email

Configured in the `EMAIL` block at the top of the script, not on the command
line — same shape as
[`short_sell_report`](../short_sell_report/README.md#email). `EMAIL_TO` empty
means do not send.

The report is the **attachment** — one PDF, however many pages it ran to. The
body is just the sign-off:

```
Best Regards,

Khalife
```

`EMAIL_DRY_RUN = True` builds the message and prints who it would reach without
opening a socket.

---

## Running it offline

```
python scripts/luld_report/luld_report.py --self-test
python scripts/luld_report/luld_report.py --demo
```

151 checks, no kdb and no pykx: the suffix routing including the many-to-one
markets, which limit a period was at, what counts as favourable, window
arithmetic including open-ended splits, what makes a split active, which orders
the limit touched, every guard on the findings table, the rollups, the mode
routing, both layouts and the mail bodies. It also checks the q for reserved
words — `ss` is q's string search and cost a live run to learn — and that no
chart title has drifted on top of its own bars.

`--demo` draws both reports from made-up numbers, stamped `SAMPLE` in the
subtitle, the footer and the filename.

The page itself is [`scripts/lib/report_page.py`](../lib/README.md); the mail is
`scripts/lib/mailer.py`. Copy `scripts/lib` beside this folder if you move it.
