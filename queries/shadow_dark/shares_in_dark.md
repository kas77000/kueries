# How `shares_in_dark` is computed

`shares_routed` adds up every child order we sent. If the algo sends 40,000
shares, cancels, and sends the same 40,000 again, that is 80,000 routed — but
only 40,000 was ever really in a dark pool.

`shares_in_dark` answers the other question: **at the busiest moment of the
day, how many shares were sitting in dark venues at once?**

## An example

One parent order for 100,000 shares. Three children:

| child | on market | off market | size |
| --- | --- | --- | --- |
| A | 09:30 | 09:40 | 40,000 |
| B | 09:35 | 09:50 | 30,000 |
| C | 09:40 | 09:55 | 40,000 |

Drawn on a timeline:

```
        09:30   09:35   09:40   09:45   09:50   09:55
          |       |       |       |       |       |
   A      [======40,000===]
   B              [==========30,000=======]
   C                      [==========40,000=======]

in dark   40,000  70,000  70,000  70,000  40,000   0
                  ^^^^^^^^^^^^^^^^^^^^^^
                         the peak
```

- `shares_routed` = 40,000 + 30,000 + 40,000 = **110,000** — more than the
  order itself
- `shares_in_dark` = **70,000** — the most that was ever in there at one time
- `dark_pct` = 70,000 / 100,000 = **70%** of the order was in dark pools

## How the code gets there

Every child becomes two events: `+size` when it goes on market, `−size` when it
comes off. Sort them by time, keep a running total, take the highest the total
ever reached.

| time | event | delta | running total |
| --- | --- | ---: | ---: |
| 09:30 | A on | +40,000 | 40,000 |
| 09:35 | B on | +30,000 | 70,000 ← peak |
| 09:40 | A off | −40,000 | 30,000 |
| 09:40 | C on | +40,000 | 70,000 ← peak |
| 09:50 | B off | −30,000 | 40,000 |
| 09:55 | C off | −40,000 | 0 |

In q that is one line:

```q
.shd.peak:{[on;off;sz] max sums exec d from `t`d xasc ([]t:on,off; d:sz,neg sz)};
```

reading right to left:

| | |
| --- | --- |
| `([]t:on,off; d:sz,neg sz)` | build the event table |
| `` `t`d xasc `` | sort by time, then by delta |
| `exec d from` | take the delta column |
| `sums` | the running total |
| `max` | the highest it reached |

## Two details that matter

**Sort by delta as well as time.** At 09:40 child A comes off and child C goes
on, in the same millisecond. Sorting the delta *ascending* puts A's −40,000
first, so the total dips to 30,000 and comes back to 70,000. Sorted the other
way, C's +40,000 lands first and the total touches 110,000 — a moment of double
exposure that never happened. With children cycling through venues all day,
that mistake would fire on nearly every rotation.

**A child still on the market is always excluded.** Its `t_off_market` is `0`,
which sorts before every other event, so the day would start at *minus* its
size and carry that error the whole way through. This is why the query filters
`t_off_market > 0` regardless of `.shd.minRestMs` — it is a correctness
requirement, not a setting.

## Reading it against real orders

`shares_in_dark` can never exceed the parent's size unless the engine really
did have more than the order resting at once, which makes it a useful check on
itself.

From a run over 2026-08-28, both patterns show up clearly:

| sym | target | children | routed | in_dark | dark_pct | child avg |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 388.HK | 500,000 | 143 | 11,775,200 | 499,500 | 99.9% | 82,344 |
| 9009.JP | 294,700 | 139 | 362,800 | 19,000 | 6.5% | 2,610 |

Both sent a similar number of children. The difference is the slice size: about
six venues are live at any moment in both cases, but `388.HK` was sending 82,000
at a time — the whole order resting in the dark, re-sent roughly 23 times over —
while `9009.JP` was sending 2,610 at a time, never showing more than 6.5% of
itself.

So a **low `dark_pct` with a large `shares_routed` is pinging**, not resting:
lots of small probes in quick succession. That is the distinction the column
exists to make.
