# luld_orders pinned % Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `luld_orders` reporting zero orders every day, by gating on the share of an order's life its stock spent at a limit instead of on each limit period's own length — and fix the completion percentage the same way `short_sell_report` was fixed.

**Architecture:** Union the limit runs for a stock, intersect that union with the order's live window, and express it as a percentage of the window. Unioning is what makes it work: `Q_LIMITS` splits a run in two on a single normal tick, so a pinned stock produces many short runs, and a per-run minimum discards all of them. The window comes from `target_state`'s first and last timed rows — already fetched, no new query.

**Tech Stack:** Python 3, stdlib only. The script's own `--self-test` is the test harness; there is no pytest in this repo.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-26-luld-orders-pinned-pct-design.md`
- Everything lives in `scripts/luld_orders/luld_orders.py`. One folder per script, README about that script only.
- **No kdb or pykx on this machine.** Every change must be provable by `--self-test`, which needs no connection.
- Tests are `check(name, got, want)` calls inside `self_test()`, not pytest. Run with:
  `python scripts/luld_orders/luld_orders.py --self-test`
- Default `--min-pinned-pct` is **25**. Gate is `>=`.
- `MIN_LIMIT_MINS` and `--min-mins` are **removed**, not lowered.
- Commit after each task. Do not push; the user pushes.

---

### Task 1: Union the limit runs and measure the overlap

**Files:**
- Modify: `scripts/luld_orders/luld_orders.py` — add `merge_spans()` and `pinned_ms()` beside `overlap_mins()` (~line 1284); add tests in `self_test()`

**Interfaces:**
- Produces: `merge_spans(spans) -> list[tuple[int, int]]`, `pinned_ms(window, periods) -> int`
  - `spans` is `[(start_ms, end_ms)]`, `window` is `(start_ms, end_ms)`, `periods` is a list of `Limit`
- Consumes: nothing

- [ ] **Step 1: Write the failing tests**

Add to `self_test()`, immediately after the `print("\nlimit periods")` block (search for `to_limits([_lim(`):

```python
    print("\nunioning the runs")
    M = 60_000
    #  ONE NORMAL TICK SPLITS A RUN.  Q_LIMITS groups on `differ lim`, so a
    #  stock that flickers at its band produces many short runs rather than one
    #  long one, and a minimum applied to each run on its own discards them all.
    #  Unioning first is what makes the flicker irrelevant.
    check("two runs that overlap become one",
          merge_spans([(0, 10 * M), (5 * M, 20 * M)]), [(0, 20 * M)])
    check("two runs that merely touch become one",
          merge_spans([(0, 10 * M), (10 * M, 20 * M)]), [(0, 20 * M)])
    check("a gap between them is kept",
          merge_spans([(0, 10 * M), (11 * M, 20 * M)]),
          [(0, 10 * M), (11 * M, 20 * M)])
    check("they come back in order however they went in",
          merge_spans([(11 * M, 20 * M), (0, 10 * M)]),
          [(0, 10 * M), (11 * M, 20 * M)])
    check("one inside another is absorbed, not counted twice",
          merge_spans([(0, 20 * M), (5 * M, 6 * M)]), [(0, 20 * M)])
    check("nothing in, nothing out", merge_spans([]), [])

    #  the case from the investigation: 12 four-minute runs over an hour, each
    #  broken by one normal tick, against an order live for that hour
    flicker = to_limits([_lim("7203.JP", 11 * H + i * 5 * M,
                              11 * H + i * 5 * M + 4 * M) for i in range(12)])
    check("twelve runs, all of them kept now there is no per-run minimum",
          len(flicker), 12)
    check("and together they cover 48 of the 60 minutes",
          pinned_ms((11 * H, 12 * H), flicker), 48 * M)
    check("a period reaching past the window is clipped to it",
          pinned_ms((11 * H, 11 * H + 10 * M),
                    to_limits([_lim("a", 11 * H, 12 * H)])), 10 * M)
    check("a period entirely outside it contributes nothing",
          pinned_ms((11 * H, 12 * H),
                    to_limits([_lim("a", 9 * H, 10 * H)])), 0)
    check("no periods at all is zero, not undefined",
          pinned_ms((11 * H, 12 * H), []), 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `NameError: name 'merge_spans' is not defined`

- [ ] **Step 3: Write the implementation**

Insert directly above `def overlap_mins(o, periods)`:

```python
def merge_spans(spans) -> list:
    """[(start, end)] -> the same time with overlapping and TOUCHING runs
    joined, in order.

    Q_LIMITS ends a run on a single normal tick - `grp: sums differ lim by
    sym` - so a stock that flickers at its band comes back as many short runs
    rather than one long one.  Summing them without merging double counts the
    overlaps; measuring them one at a time is what made the report read zero.
    Touching runs join too: a run ending at the same millisecond the next
    begins is one period the feed happened to punctuate.
    """
    out = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def pinned_ms(window, periods) -> int:
    """Milliseconds of `window` covered by any of `periods`.

    window is (start, end) in ms since midnight, both known - order_window()
    is what decides that, and returns None rather than guessing.
    """
    lo, hi = window
    total = 0
    for a, b in merge_spans([(w.start, w.end) for w in periods]):
        total += max(0, min(hi, b) - max(lo, a))
    return total
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_orders/luld_orders.py
git commit -m "Union the limit runs before measuring them"
```

---

### Task 2: The order's live window, from target_state

**Files:**
- Modify: `scripts/luld_orders/luld_orders.py` — add `life_by_order()` beside `last_state_by_order()` (~line 610) and `order_window()` beside it; tests in `self_test()`

**Interfaces:**
- Produces:
  - `life_by_order(records, orders) -> dict` — `{order key: (first_ms, last_ms)}`
  - `order_window(o, sp, life) -> tuple | None` — `(start_ms, end_ms)`; `o` is an `Order`, `sp` a `Splits`, `life` the `(first, last)` for that order or `None`
- Consumes: nothing from Task 1

- [ ] **Step 1: Write the failing tests**

Add to `self_test()` after the Task 1 block:

```python
    print("\nthe order's live window")
    #  target_state is the order's REAL lifecycle.  t_start and t_end are what
    #  the target row INTENDED, which is a different question and a worse
    #  answer to this one.
    def _st(idt, *times, srv=1, d=None):
        return [{"date": d, "id_server": srv, "id_target": idt,
                 "time": dt.timedelta(milliseconds=t), "state": "x"}
                for t in times]

    w_ord = to_orders([_p(1, "7203.JP", 1000, t_start=10 * H, t_end=15 * H)])
    check("the fixture is one order", len(w_ord), 1)
    life = life_by_order(_st(1, 11 * H, 13 * H, 12 * H), w_ord)
    check("first and last TIMED state rows, whatever order they arrive in",
          life[w_ord[0].key], (11 * H, 13 * H))
    check("and target_state wins over the target row's own times",
          order_window(w_ord[0], Splits(), life[w_ord[0].key]), (11 * H, 13 * H))
    check("a row with no time cannot bound anything",
          life_by_order([{"date": None, "id_server": 1, "id_target": 1,
                          "time": None, "state": "x"}], w_ord), {})
    check("with no state rows at all it falls back to the target row",
          order_window(w_ord[0], Splits(), None), (10 * H, 15 * H))
    #  a single state row is a zero length life, which is not a window to take
    #  a share of - so it falls through rather than returning nothing
    check("one state row is not a window, so the next source is used",
          order_window(w_ord[0], Splits(), (11 * H, 11 * H)), (10 * H, 15 * H))
    #  the children are the last resort
    no_t = to_orders([_p(2, "7203.JP", 1000, t_start=None, t_end=None)])[0]
    kids = Splits(n=1, first_gen=10 * H, last_off=14 * H)
    check("with no state rows and no target times, the children bound it",
          order_window(no_t, kids, None), (10 * H, 14 * H))
    check("and with nothing at all there is no window",
          order_window(no_t, Splits(), None), None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `NameError: name 'life_by_order' is not defined`

> If instead it fails on `_p() got an unexpected keyword argument 't_start'`, read `_p`'s signature near the fixtures and pass the times the way it already accepts them. Do not change `_p`.

- [ ] **Step 3: Write the implementation**

Insert directly below `last_state_by_order()`:

```python
def life_by_order(records, orders) -> dict:
    """{order key: (first, last)} from target_state's TIMED rows.

    The order's real lifecycle, which is what a share of its life has to be
    measured against.  Same shape as last_state_by_order and for the same
    reasons: decided here rather than in q, where the self test can prove it.

    A row with no time is skipped at BOTH ends - it cannot be ordered, so it
    can be neither the first nor the last, and taking it as midnight would
    make it the first.
    """
    known = {o.key for o in orders}
    out = {}
    for r in records:
        key = (_d(r.get("date")), _i(r.get("id_server")),
               _i(r.get("id_target")))
        if key not in known:
            continue
        at = _ms(r.get("time"))
        if at is None:
            continue
        got = out.get(key)
        out[key] = ((at, at) if got is None
                    else (min(got[0], at), max(got[1], at)))
    return out


def order_window(o, sp, life) -> Optional[tuple]:
    """(start, end) in ms, or None when nothing can bound the order.

    Three sources, best first: target_state's first and last rows, then the
    target row's own t_start/t_end, then the children.

    EACH SOURCE IS TRIED AS A PAIR.  A start from one and an end from another
    straddle two different notions of when the order lived, and a window that
    mixes them is not one this can defend - the denominator of a percentage is
    exactly the wrong place to be approximately right.

    A source that gives a zero length window - one state row, say - is not a
    window to take a share of, so it falls through to the next rather than
    ending the search.
    """
    for lo, hi in (life or (None, None), (o.t_start, o.t_end),
                   (sp.first_gen, sp.last_off)):
        if lo is not None and hi is not None and hi > lo:
            return lo, hi
    return None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `all checks passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_orders/luld_orders.py
git commit -m "The order's live window comes from target_state"
```

---

### Task 3: Gate on pinned %, and delete the per-period minimum

**Files:**
- Modify: `scripts/luld_orders/luld_orders.py` — `MIN_LIMIT_MINS` (~line 97), `to_limits()` (~line 664), `touched()` (~line 712), `run()` (~line 1493), the argument parser; tests in `self_test()`

**Interfaces:**
- Consumes: `pinned_ms` (Task 1), `life_by_order` / `order_window` (Task 2)
- Produces: `touched(orders, limits, lives, splits, min_pct) -> (kept, hits, pinned)` where `pinned` is `{order key: float | None}`

- [ ] **Step 1: Write the failing tests**

```python
    print("\nthe gate is a share of the order's life")
    g_ord = to_orders([_p(1, "7203.JP", 1000, t_start=11 * H, t_end=12 * H)])
    g_life = life_by_order(_st(1, 11 * H, 12 * H), g_ord)
    def _gate(runs, pct):
        return touched(g_ord, to_limits(runs), g_life, {}, pct)
    flick = [_lim("7203.JP", 11 * H + i * 5 * M, 11 * H + i * 5 * M + 4 * M)
             for i in range(12)]
    kept, _hits, pin = _gate(flick, 25.0)
    check("the flickering stock is counted now", len(kept), 1)
    check("at 80% of the order's life", round(pin[g_ord[0].key], 1), 80.0)
    check("and it clears a 50% gate too", len(_gate(flick, 50.0)[0]), 1)
    check("but not a 90% one", len(_gate(flick, 90.0)[0]), 0)
    #  exactly on the line counts - the gate is >=
    check("exactly the threshold counts",
          len(_gate([_lim("7203.JP", 11 * H, 11 * H + 15 * M)], 25.0)[0]), 1)
    #  a stock that brushes its band once is what the old minimum was for, and
    #  the percentage does that job without discarding the flicker
    check("a forty second brush does not",
          len(_gate([_lim("7203.JP", 11 * H, 11 * H + 40_000)], 25.0)[0]), 0)
    check("an order nothing can bound gets no percentage and is not counted",
          _gate([_lim("7203.JP", 11 * H, 12 * H)], 25.0)[2].get(
              to_orders([_p(9, "7203.JP", 1000, t_start=None,
                            t_end=None)])[0].key), None)
    #  to_limits no longer filters on length at all
    check("to_limits keeps a one minute run",
          len(to_limits([_lim("a", 11 * H, 11 * H + M)])), 1)
    check("and still drops one with no length",
          len(to_limits([_lim("a", 11 * H, 11 * H)])), 0)
    check("--min-mins is gone from the parser",
          "--min-mins" in build_parser().format_help(), False)
    check("--min-pinned-pct replaced it, defaulting to 25",
          build_parser().parse_args([]).min_pinned_pct, 25.0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `TypeError: touched() takes 2 positional arguments but 5 were given`

> `build_parser()` may not exist yet — the parser is built inline in `main()`. If the test fails on `NameError: build_parser`, extract the parser into `def build_parser():` returning `p`, exactly as `reversion_liquidity.py` does, and have `main()` call it. That extraction is part of this task.

- [ ] **Step 3: Write the implementation**

**3a.** Delete the constant. Remove the `MIN_LIMIT_MINS = 20.0` line (~97) and put this in its place:

```python
#  A LIMIT PERIOD HAS NO MINIMUM LENGTH.  There used to be one, of 20 minutes,
#  applied to each period on its own - and it made this report print zero
#  orders every day on a book full of them.  Three things compound: Q_LIMITS
#  ends a run on a single normal tick, a run is a FLOOR because a pinned stock
#  stops quoting, and the minimum was then applied to each run separately.  So
#  the harder a stock was pinned the shorter its runs and the more certainly
#  they were discarded - the filter was biased against the very orders the
#  report exists to find.
#
#  What counts now is the share of an ORDER'S LIFE its stock spent at a limit,
#  with the runs unioned first so the tick splitting is irrelevant.  Noise
#  filters itself: to_limits still drops a run with no length, and a two tick
#  blip is seconds against an order's hours.
MIN_PINNED_PCT = 25.0
```

**3b.** `to_limits` loses its threshold. Change the signature and delete the check:

```python
def to_limits(records, d=None) -> list:
    """Every limit period the quotes prove, at any length.

    A period is a FLOOR: a pinned stock often stops quoting altogether, so it
    ends at the last tick that PROVED it and never later.  Under-reporting is
    the chosen direction - a window this cannot prove is not one it claims.

    NO MINIMUM LENGTH.  See MIN_PINNED_PCT for why there used to be one and
    why it was wrong.  A run with no length at all is still dropped: it cannot
    be ordered against anything.
    """
```

Delete these two lines from the body:

```python
        if lim.minutes < min_mins:
            continue
```

**3c.** `touched` gates on the share:

```python
def touched(orders, limits, lives, splits, min_pct=MIN_PINNED_PCT) -> tuple:
    """(orders at a limit, {key: its periods}, {key: pinned % or None}).

    An order counts when its stock was at a limit for at least min_pct of the
    order's own life - not when some single period was long enough.  The runs
    are unioned before they are measured, so a period the feed punctuated is
    one period.

    An order nothing can bound gets None rather than a number, is left out,
    and is counted by the caller.  A share of an unknown life is not a small
    share, and overlap()'s midnight-to-midnight stand-in would make it one.
    """
    index = limits_by_sym(limits)
    keep, hits, pinned = [], {}, {}
    for o in orders:
        got = sorted(index.get((o.date, o.sym), ()), key=lambda w: w.start)
        win = order_window(o, splits.get(o.key, Splits()), lives.get(o.key))
        if win is None:
            pinned[o.key] = None
            continue
        pct = 100.0 * pinned_ms(win, got) / (win[1] - win[0])
        pinned[o.key] = pct
        if pct < min_pct:
            continue
        keep.append(o)
        hits[o.key] = got
    return keep, hits, pinned
```

**3d.** `run()` — replace the fetch/limit/touch block. The splits must be built **before** `touched`, because the window falls back to them. Find:

```python
        syms = sorted({o.sym for o in day})
        lims = to_limits(fetch_limits(qh, pl.hist, d, syms), d, args.min_mins)
        if not lims:
```

and replace through `executed.update(splits_by_order(wr, kept))` with:

```python
        syms = sorted({o.sym for o in day})
        lims = to_limits(fetch_limits(qh, pl.hist, d, syms), d)
        if not lims:
            #  a day with no limit period anywhere is possible; a run of them
            #  means the quote query is matching nothing rather than the market
            #  being calm
            no_limit_days += 1
            continue
        day_splits = splits_by_order(wr, day)
        kept, day_hits, day_pinned = touched(
            day, lims, life_by_order(sr, day), day_splits, args.min_pinned_pct)
        unbounded += sum(1 for v in day_pinned.values() if v is None)
        if not kept:
            continue
        orders.extend(kept)
        hits.update(day_hits)
        pinned.update({k: v for k, v in day_pinned.items() if v is not None})
        executed.update({o.key: day_splits.get(o.key, Splits()) for o in kept})
```

Add `pinned, unbounded = {}, 0` beside the existing `orders, executed, hits = [], {}, {}` and `seen, cancelled, no_limit_days = 0, 0, 0`.

**3e.** The warning text. Replace the `--min-mins` sentence:

```python
    if seen and not tot.orders:
        log(f"  WARNING: {seen:,} orders were in scope and NOT ONE spent "
            f"{args.min_pinned_pct:g}% of its life at a limit. Check "
            f"{pl.qatt_server} has the syms, and try --min-pinned-pct lower.")
    if unbounded:
        log(f"  {unbounded:,} order(s) had no target_state row, no t_start/"
            f"t_end and no children, so their life could not be bounded and "
            f"they have no pinned %")
```

**3f.** The parser. Remove the `--min-mins` argument entirely and add:

```python
    p.add_argument("--min-pinned-pct", type=float, default=MIN_PINNED_PCT,
                   help="an order counts when its stock was at a limit for at "
                        "least this %% of the order's own life")
```

**3g.** Fix the other `touched(` and `to_limits(` callers. Search the file for both; the demo path (~line 1710) calls `touched(orders, to_limits(lims, d))`. Update it to pass `{}, {}` for lives and splits — the demo's orders carry `t_start`/`t_end`, so the second source bounds them:

```python
    kept, hits, _pin = touched(orders, to_limits(lims, d), {}, {})
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `all checks passed`

Then check the demo still draws:
Run: `python scripts/luld_orders/luld_orders.py --demo --out-dir /tmp/luld`
Expected: writes .pdf and .png without a traceback

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_orders/luld_orders.py
git commit -m "Gate on the share of the order's life spent at a limit"
```

---

### Task 4: Put pinned % on the page, the CSV and the raw file

**Files:**
- Modify: `scripts/luld_orders/luld_orders.py` — `Line` (~line 800), `Row`/`Totals`/`by_region`/`totals` (~line 880), `REGION_COLS`/`_row_cells` (~line 978), `CSV_HEADER`/`csv_rows` (~line 1173), `BOTH_COLS`/`raw_rows` (~line 1235), `to_lines` (~line 858), the note text (~line 1009)
- Modify: `scripts/luld_orders/README.md`

**Interfaces:**
- Consumes: `pinned` dict from `touched()` (Task 3)
- Produces: `Line.pinned` (`float | None`), `Row.pinned_pct` / `Totals.pinned_pct` (`float | None`)

- [ ] **Step 1: Write the failing tests**

```python
    print("\npinned % on the page")
    p_ord = to_orders([_p(1, "7203.JP", 1000, t_start=11 * H, t_end=12 * H),
                       _p(2, "6103.JP", 4000, t_start=11 * H, t_end=12 * H)])
    p_lines = to_lines(p_ord, {}, {}, {p_ord[0].key: 80.0, p_ord[1].key: 30.0})
    check("a line carries its own pinned %", p_lines[0].pinned, 80.0)
    r = [x for x in by_region(p_lines) if x.code == "JP"][0]
    #  weighted by ORDERED NOTIONAL, so one tiny order pinned all day cannot
    #  outvote a large one.  1000 x 10 x 1 = 10k at 80%, 4000 x 10 x 1 = 40k
    #  at 30%  ->  (10*80 + 40*30) / 50 = 40.0
    check("the region is the notional weighted mean, not a plain one",
          round(r.pinned_pct, 1), 40.0)
    check("a region with no orders has no pinned %",
          [x for x in by_region([]) if x.code == "JP"][0].pinned_pct, None)
    check("an order with no pinned % is left out of the mean entirely",
          round([x for x in by_region(to_lines(
              p_ord, {}, {}, {p_ord[0].key: 80.0})) if x.code == "JP"][0]
              .pinned_pct, 1), 80.0)
    check("the column is on the page", "Pinned %",
          [c[0] for c in REGION_COLS][5])
    check("and in the CSV header", "pinned_pct" in CSV_HEADER, True)
    check("and in the raw file", "pinned_pct" in RAW_HEADER, True)
    check("the listing sorts worst first",
          [x.o.id_target for x in list_order(p_lines)], [1, 2])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `TypeError: to_lines() takes 3 positional arguments but 4 were given`

- [ ] **Step 3: Write the implementation**

**4a.** `Line` gets a field. Add `pinned: Optional[float] = None` as the last field of the `Line` NamedTuple.

**4b.** `to_lines` takes the dict:

```python
def to_lines(orders, splits, hits, pinned=None) -> list:
    """One Line per order, in the order the report counted them."""
    pinned = pinned or {}
    out = []
    for o in orders:
        got = tuple(sorted(hits.get(o.key, ()), key=lambda w: w.start))
        d = line_direction(got, o.ref)
        sp = splits.get(o.key, Splits())
        px, src = order_price(o, sp)
        out.append(Line(o, sp, got, d, favourable_for(o.side, d), px, src,
                        pinned.get(o.key)))
    return out
```

Update its caller in `run()`: `lines = to_lines(orders, executed, hits, pinned)`.

**4c.** `Row` and `Totals` each gain two fields, carrying the weighted sum and the weight rather than a finished percentage — so `totals()` can add rows up without averaging averages:

```python
    pin_wsum: float = 0.0     # sum of pinned% * ordered_usd
    pin_w: float = 0.0        # sum of ordered_usd over lines that had a %

    @property
    def pinned_pct(self) -> Optional[float]:
        """Notional weighted, so one tiny order pinned all day cannot outvote
        a large one.  A line with no pinned % is out of both sums - a share of
        an unknown life is not a small share."""
        return (self.pin_wsum / self.pin_w) if self.pin_w else None
```

Add the identical two fields and property to **both** `Row` and `Totals`.

**4d.** `by_region` accumulates them. Add beside the existing dicts:

```python
    pw = {c: 0.0 for c in REGION_CODES}
    pws = {c: 0.0 for c in REGION_CODES}
```

and inside the loop, after `made[c] += ln.executed_usd`:

```python
        if ln.pinned is not None and ln.ordered_usd > 0:
            pw[c] += ln.ordered_usd
            pws[c] += ln.pinned * ln.ordered_usd
```

and pass `pws[r.code], pw[r.code]` as the last two arguments of each `Row(...)`.

**4e.** `totals` sums them: add `sum(r.pin_wsum for r in rows), sum(r.pin_w for r in rows)` as the last two arguments of `Totals(...)`.

**4f.** The column. Insert into `REGION_COLS` between `Completion` and `Short, fav.`, and take the width from the two notional columns so the row still sums to 1.0:

```python
REGION_COLS = (
    ("Region", 0.15, False),
    ("Orders", 0.08, True),
    ("Notional Ordered (USD)", 0.17, True),
    ("Notional Executed (USD)", 0.18, True),
    ("Completion", 0.11, True),
    ("Pinned %", 0.09, True),
    ("Short, fav.", 0.11, True),
    ("Short, adv.", 0.11, True),
)
```

and in `_row_cells`, insert after the completion cell:

```python
            (fmt_pct1(r.pinned_pct), INK, "normal"),
```

**4g.** CSV. Add `"pinned_pct"` to `CSV_HEADER` after `"completion_pct"`, and in `csv_rows`'s `one()` add after the completion entry:

```python
                "" if r.pinned_pct is None else f"{r.pinned_pct:.1f}",
```

**4h.** Raw file. Change `BOTH_COLS`:

```python
BOTH_COLS = ("overlap_mins", "pinned_pct")
```

and in `raw_rows`, append after the existing `overlap_mins` value:

```python
            "" if ln.pinned is None else f"{ln.pinned:.1f}",
```

**4i.** `list_order` sorts on it. Find `def list_order(lines)` and sort worst first, `None` last:

```python
def list_order(lines) -> list:
    """Worst first: the orders somebody would actually go and look at."""
    return sorted(lines, key=lambda ln: (-(ln.pinned or 0.0),
                                         -ln.ordered_usd))
```

**4j.** The note under the title (~line 1009). Replace the sentence about which orders appear:

```python
    fig.text(L, 0.788, "Orders whose stock was limit up OR limit down for at "
             "least the pinned % shown, measured over the order's own life. "
             "Both sides count: an unfavourable limit can still be marketable.",
```

**4k.** README. In `scripts/luld_orders/README.md`, replace the `--min-mins` paragraph (search for "A run counts only if it lasted at least") with:

```markdown
A limit period has **no minimum length**. What counts is the share of an
**order's own life** its stock spent at a limit — `--min-pinned-pct`, default
**25** — with the periods unioned first.

There used to be a 20-minute minimum on each period, and it made this report
print zero orders every day. Three things compound: one normal tick ends a run
(see the example above), a run is a floor because a pinned stock stops quoting,
and the minimum was then applied to each run separately. So the harder a stock
was pinned, the shorter its runs and the more certainly they were discarded —
the filter was biased against exactly the orders this report exists to find. A
stock at its limit for 48 of 60 minutes, as twelve four-minute runs, kept
**zero** periods.
```

Also update the flags list and the column table in that README to include `Pinned %`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `all checks passed`

Run: `python scripts/luld_orders/luld_orders.py --demo --out-dir /tmp/luld`
Expected: writes .pdf and .png; open the .png and confirm the `Pinned %` column is present and nothing collides.

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_orders/luld_orders.py scripts/luld_orders/README.md
git commit -m "Show the pinned % on the page, the CSV and the raw file"
```

---

### Task 5: Completion in shares

**Files:**
- Modify: `scripts/luld_orders/luld_orders.py` — `Line.completion` (~line 838), `Row`/`Totals`/`by_region`/`totals` (~line 880), the NOTIONAL comment block (~line 838), the note text (~line 1019)
- Modify: `scripts/luld_orders/README.md`

**Interfaces:**
- Consumes: nothing from Tasks 1-4
- Produces: `Row.order_qty`, `Row.executed` (share counts), same on `Totals`

- [ ] **Step 1: Write the failing tests**

```python
    print("\ncompletion is a share ratio here too")
    #  the same defect fixed in short_sell_report 46c0be4: ordered is
    #  THEORETICAL, executed is REALISED, so their ratio is the share
    #  completion times a price move and can print over 100%
    c_ord = to_orders([_p(1, "7203.JP", 1000, limit_price=10.0)])
    c_sp = {c_ord[0].key: Splits(n=1, made=1000, filled_local=10_500.0)}
    c_ln = to_lines(c_ord, c_sp, {}, {})
    check("a full fill reads 100%, not 105%", c_ln[0].completion, 100.0)
    check("while the executed notional stays REAL money",
          c_ln[0].executed_usd, 10_500.0)
    check("which may exceed the theoretical ordered side",
          c_ln[0].executed_usd > c_ln[0].ordered_usd, True)
    c_row = [x for x in by_region(c_ln) if x.code == "JP"][0]
    check("the region row is shares too", c_row.completion, 100.0)
    check("and so is the headline", totals(by_region(c_ln)).completion, 100.0)
    #  a partial fill into a rising market was the case that used to lie
    h_sp = {c_ord[0].key: Splits(n=1, made=800, filled_local=10_400.0)}
    h_ln = to_lines(c_ord, h_sp, {}, {})
    check("a partial fill reads as partial", h_ln[0].completion, 80.0)
    check("even when the money says otherwise",
          h_ln[0].executed_usd > h_ln[0].ordered_usd, True)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `FAIL a full fill reads 100%, not 105% got 105.0, want 100.0`

- [ ] **Step 3: Write the implementation**

**5a.** `Line.completion` becomes the share ratio:

```python
    @property
    def completion(self) -> Optional[float]:
        """SHARES.  Completion asks how much of the order got done, which is a
        quantity question.

        It was executed_usd over ordered_usd, and that can print over 100%:
        ordered is THEORETICAL - the whole quantity at a price the unfilled
        part never traded at - and executed is REALISED, so the ratio is this
        number multiplied by a price move.  Same defect, same fix, as
        short_sell_report 46c0be4.  Both notional columns still mean what they
        say; nothing divides them.
        """
        return _completion(self.executed, self.o.size)
```

Leave `share_completion` in place — `raw_rows` uses it, and it now returns the same thing.

**5b.** `Row` and `Totals` carry the share counts. Add to both, before the `pin_wsum`/`pin_w` fields from Task 4:

```python
    order_qty: int = 0
    executed: int = 0
```

and change the `completion` property on both:

```python
    @property
    def completion(self) -> Optional[float]:
        """SHARES - see Line.completion."""
        return _completion(self.executed, self.order_qty)
```

**5c.** `by_region` accumulates them. Add beside the other dicts:

```python
    qty = {c: 0 for c in REGION_CODES}
    ex = {c: 0 for c in REGION_CODES}
```

in the loop:

```python
        qty[c] += ln.o.size
        ex[c] += ln.executed
```

and pass `qty[r.code], ex[r.code]` into each `Row(...)` in the field order declared.

**5d.** `totals` sums them: `sum(r.order_qty for r in rows), sum(r.executed for r in rows)` in the matching position.

**5e.** The NOTIONAL comment block (~line 838). Replace the last sentence of the EXECUTED paragraph:

```
# EXECUTED is not priced this way at all: it is the sum of make * the child's
# own avg_fill_price, which is what those shares really cost.  So the two
# columns are NOT a ratio, and nothing on the page divides them.  Ordered is
# theoretical and executed is realised; executed CAN exceed ordered, and when
# it does that is a true fact about where the price went, not a completion
# over 100%.  Completion is a share ratio - see Line.completion.
```

**5f.** The note text (~line 1019). Delete the sentence beginning "Executed is what the fills really paid, so completion is the notional one" and replace with:

```python
             "Executed is what the fills really paid, so the two notional "
             "columns are not a ratio. Completion is shares: executed over "
             "the quantity ordered.",
```

**5g.** README: update the Completion row of the column table the same way, and add a short "Why completion is in shares" note pointing at `short_sell_report`'s README section of the same name.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python scripts/luld_orders/luld_orders.py --self-test`
Expected: `all checks passed`

Run: `python scripts/luld_orders/luld_orders.py --demo --out-dir /tmp/luld`
Expected: writes without a traceback; every Completion on the page is <= 100%

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_orders/luld_orders.py scripts/luld_orders/README.md
git commit -m "Completion is a share ratio here too"
```

---

## Self-Review

**Spec coverage:** §1 diagnosis → Task 3's `MIN_PINNED_PCT` comment and README. §2 measure → Tasks 1 and 3. §3 window → Task 2. §4 page → Task 4. §5 tests → distributed across Tasks 1-4. §6 completion → Task 5. No gaps.

**Deviation from the spec, deliberate:** §3's table reads as per-bound fallback (state start, else `t_start`, else child). Task 2 tries each source **as a pair** instead. A start from `target_state` with an end from a child straddles two different notions of when the order lived, and the denominator of a percentage is the wrong place to be approximately right. Same sources, same priority; noted here rather than silently diverging.

**Type consistency:** `touched` returns a 3-tuple everywhere after Task 3 — `run()` and the demo path both updated in 3d and 3g. `to_lines` takes 4 arguments after Task 4, with a default so Task 5's tests can pass `{}`. `Row`/`Totals` field order is declared once in 4c and extended once in 5b; every `Row(...)` construction is updated in the same step that adds the fields.
