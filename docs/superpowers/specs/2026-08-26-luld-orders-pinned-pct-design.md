# luld_orders — pinned %, replacing the per-period minimum

`luld_orders` reports **zero orders in every region, every day**, on a book that
plainly has limit up/down orders in it. This replaces the filter that causes
that with one measured against the order rather than against the quote feed.

---

## 1. Why it reads zero

Three mechanisms compound, and each one shortens the *measured* length of a
limit period:

**A single normal tick ends a period.** `Q_LIMITS` groups contiguous runs with
`grp: sums differ lim by sym`, so one tick with both sides quoting splits a run
in two. The README's own example says so:

```
11:04  bid 1250  ask    0   LIMIT  ┘
11:05  bid 1249  ask 1250   normal   <- this tick ends it
11:06  bid 1250  ask    0   LIMIT  ┐ a SECOND period, not the same one
```

**Every window is a floor.** A pinned stock often stops quoting altogether, so a
period ends at the last tick that *proved* it and never later. Under-reporting
is deliberate and correct — but it means measured length tracks how long the
feed kept ticking, not how long the stock was actually pinned.

**The 20-minute minimum is applied to each period individually.**
`to_limits(..., min_mins=MIN_LIMIT_MINS)` discards any single run shorter than
`MIN_LIMIT_MINS = 20.0` before anything else runs.

Together these are biased against exactly the population the report exists to
find: the harder a stock is pinned, the more it flickers and the less it quotes,
so the shorter its runs and the more certainly they are discarded. Demonstrated
against the script's own `to_limits`:

```
A stock at its limit for 48 of 60 minutes (80%), as 12 four-minute runs:
    kept at --min-mins 20:   0 periods      <- the page reads zero
    kept at --min-mins  2:  12 periods
```

`luld_report` reads the same `qatt` table with the same `lim` expression and
does **not** have this problem: its `to_pins()` applies no minimum at all, and
its `MIN_PIN_MINS = 2.0` gates one sub-list on the *overlap*, not on the period.
That divergence between two scripts doing the same detection is the tell.

## 2. The measure

Per order:

```
periods    limit runs for that stock on that date
union      merge overlapping and adjacent runs
window     the order's live window (§3)
pinned_ms  total( union ∩ window )
pinned %   pinned_ms / len(window)
```

An order counts when `pinned % >= --min-pinned-pct`, default **25**.

**Unioning first is the point.** It makes the tick-splitting irrelevant: twelve
four-minute runs broken by single normal ticks collapse into one 48-minute span
against a 60-minute order, giving 80% rather than twelve individually worthless
runs.

25% because an order spending a quarter of its life against a band is already
fighting it, while a stock that brushes a limit once for forty seconds lands
near 1% and stays out. `--min-pinned-pct 50` tightens it to the literal reading
of "most of its life".

### `MIN_LIMIT_MINS` and `--min-mins` are removed

They are the bug, and no smaller version of them is kept. Noise filters itself
under the new measure: `to_limits` already drops single-tick runs (`end <=
start`), and a two-tick blip contributes seconds — under 1% of any real order's
life. Retaining a per-period floor *alongside* a percentage gate would
reintroduce the same bias in miniature.

## 3. The order's live window

The denominator must be a window we can prove, taken from **`target_state`** —
the order's actual lifecycle, rather than the intent recorded on the target row.

| bound | first choice | then | then |
|---|---|---|---|
| start | first `target_state.time` | `t_start` | earliest child `t_gen` |
| end | last `target_state.time` | `t_end` | latest child `t_off_market` |

`target_state` is already fetched — `Q_ORDERS` returns it as `st` and
`last_state_by_order()` walks it for `drop_dead_on_arrival`. A companion
`life_by_order()` keeps the **first and last** timed rows per order. Rows with no
time are skipped at both ends, for the reason the existing function gives: a row
that cannot be ordered cannot be the last one, and treating it as midnight would
make it the first.

`Splits` already carries `first_gen` and `last_off`, so the final fallback needs
no new query either.

**An order that no source can bound gets no percentage.** It is counted and named
in the log rather than silently included or dropped, consistent with
`died_before_starting`'s *NOT DROPPED ON MISSING EVIDENCE*.

`overlap()` keeps its current behaviour — a missing start means "from the open",
a missing end "still live" — because that is right for *did these overlap at
all*. It must not be reused as a denominator: substituting midnight-to-midnight
for a missing bound would crush every percentage toward zero. The two questions
stay separate functions.

## 4. The page

One new column, **`Pinned %`**, per region — the headline answer to how hard a
region was fighting the band.

It is the **notional-weighted mean over the orders on that row**, i.e. over the
orders that cleared the gate, not over every order in scope: the row is about
orders at a limit, so averaging in orders the row does not contain would dilute
it toward nothing. Weighted by ordered notional rather than a plain mean, so one
tiny order pinned all day cannot outvote a large one. An order with no
percentage (§3) is excluded from both the weight and the mean.

The order listing carries it per line and **sorts on it, worst first**, so triage
reads top-down. `Short, fav.` and `Short, adv.` are unchanged.

The note under the title changes to describe the new gate. `Pinned %` joins the
CSV and the raw file.

## 5. Tests

Written before the change, from the cases in the investigation:

- twelve four-minute runs against a sixty-minute order → 80%, counted
- one continuous run → unchanged behaviour
- a run split by exactly one normal tick → unions to **one** span, not two
- adjacent runs that touch exactly → union to one, no double count
- an order with no `t_start` → falls back to the first child's `t_gen`
- an order nothing can bound → no percentage, counted, named
- exactly 25% → counted (the gate is `>=`)
- a forty-second brush of a limit → ~1%, out
- the removed `--min-mins` is gone from the parser and from `to_limits`

## 6. Out of scope

`luld_orders` computes Completion as notional executed over notional ordered —
the same defect fixed in `short_sell_report` (commit `46c0be4`), and it can
print over 100% the same way. Noted here so it is not lost; it is a separate
change with its own commit.
