# luld_shortsell_check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Audit our algo engine's child splits against each market's limit up/down and short sell rules over a date range, and report anomalies with enough context to reproduce them.

**Architecture:** One Python script driving q lambdas over PyKX against two historical kdb processes, walking one date at a time. Per date it resolves a price band per stock from four layered sources, runs rule checks and situational detectors over every child split, and folds results into per-market counters plus finding rows. Everything after the IPC boundary is pure Python and covered by an offline `--self-test`.

**Tech Stack:** Python 3, numpy, pandas, pykx (lazy import), openpyxl (lazy, `--out-dir` only).

**Spec:** [`docs/superpowers/specs/2026-08-19-luld-shortsell-check-design.md`](../specs/2026-08-19-luld-shortsell-check-design.md). Section references below (§3, §5, §6) point into it.

## Global Constraints

- **Single module**, `scripts/luld_shortsell_check/luld_shortsell_check.py`, plus `README.md` in the same folder. This matches `scripts/reversion_liquidity/` exactly. The section-comment banners are the seams if it ever needs splitting.
- **No kdb, no pykx, and no q on the development machine.** Every task's tests must pass with `python luld_shortsell_check.py --self-test` and nothing else. `pykx` is imported lazily inside `connect()`.
- **q lambdas are sent as source text with typed arguments.** Dates and country codes travel as q values, never interpolated into the text.
- **Country codes go to q as char vectors** (`b"CN"`), never python `str` — PyKX turns a `str` into a q symbol and `` `$ `` on a symbol is a `type` error.
- **Markets in scope (8):** `HK JP KR MY TH CN TW IN`. Indonesia is excluded.
- **Short sell checks run on 5 markets only:** `HK JP KR MY TH`. `CN TW IN` emit `RULE_UNKNOWN`.
- **Band from `target_oms` for `{IN, KR}` only.**
- **Where a band must be guessed, guess wide.** Under-reporting beats fabricating (§10.7).
- Server constants are `CHANGEME` placeholders; a run that has not had them set must exit with a clear message, not a connection error.

---

## File Structure

| file | responsibility |
|---|---|
| `scripts/luld_shortsell_check/luld_shortsell_check.py` | everything: CLI, q sources, band resolution, rules, detectors, report, self-test |
| `scripts/luld_shortsell_check/README.md` | how to run it, what each check means, how to read the report, where the judgement calls are |

Module layout inside the script, in order, each behind a banner comment:

```
CONNECTIONS          server constants
MARKET TABLE         MARKETS, CN board prefixes, JP step table
TICKS                round_inward, recover_tick_ladder
BANDS                compute_band, resolve_band, Band
Q SOURCES            Q_ORDERS, Q_BAND, Q_MKT, Q_OMS_BAND
STATE                classify_state
RULES                check_* per §5
DETECTORS            detect_* per §6
SCORING              severity, impact
REPORT               scorecard, workbook, suppression footer
RUN                  per-date loop, diagnose
SELF TEST            all offline tests
MAIN                 argparse
```

---

## Task 1: Skeleton, market table, and the self-test harness

**Files:**
- Create: `scripts/luld_shortsell_check/luld_shortsell_check.py`

**Interfaces:**
- Produces: `MARKETS: dict[str, Market]`; `Market` NamedTuple with fields `code, band_rule, band_pct, ss_rule, band_from_oms`; `run_self_test() -> int`; `main() -> int`

- [ ] **Step 1: Write the failing test**

Create the file with only the test and a stub, so the harness exists before anything uses it.

```python
#!/usr/bin/env python3
"""luld_shortsell_check.py - audit child splits against LULD and short sell rules."""
from __future__ import annotations

import argparse
import sys
from typing import NamedTuple, Optional

ORDER_SERVER = "CHANGEME:5010"
QATT_SERVER = "CHANGEME:5011"
USER = None
PASSWORD = None
_PLACEHOLDER = "CHANGEME"


class Market(NamedTuple):
    code: str
    band_rule: Optional[str]   # None | "jp_step" | "pct" | "cn_board" | "oms_only"
    band_pct: Optional[float]
    ss_rule: Optional[str]     # None | "always_ask" | "uptick" | "ltp_plus_tick"
    band_from_oms: bool


MARKETS: dict = {}


def test_markets_cover_exactly_the_eight_in_scope():
    assert set(MARKETS) == {"HK", "JP", "KR", "MY", "TH", "CN", "TW", "IN"}
    assert "ID" not in MARKETS, "Indonesia is out of scope"


def test_short_sell_rules_only_on_confirmed_markets():
    checked = {c for c, m in MARKETS.items() if m.ss_rule is not None}
    assert checked == {"HK", "JP", "KR", "MY", "TH"}
    for c in ("CN", "TW", "IN"):
        assert MARKETS[c].ss_rule is None, f"{c} short sell must stay RULE_UNKNOWN"


def test_band_from_oms_is_two_markets():
    assert {c for c, m in MARKETS.items() if m.band_from_oms} == {"IN", "KR"}


def test_hk_has_no_band_rule():
    assert MARKETS["HK"].band_rule is None
```

- [ ] **Step 2: Add the runner and `main`, then run it to see the tests fail**

```python
def run_self_test() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true", help="run built-in tests; needs no kdb")
    args = p.parse_args()
    if args.self_test:
        return run_self_test()
    p.error("nothing to do; pass --self-test or a date range")
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 4 FAILs — `MARKETS` is empty.

- [ ] **Step 3: Fill in the market table**

```python
MARKETS = {
    "HK": Market("HK", None,        None, "always_ask",    False),
    "JP": Market("JP", "jp_step",   None, "uptick",        False),
    "KR": Market("KR", "pct",       30.0, "uptick",        True),
    "MY": Market("MY", "pct",       30.0, "uptick",        False),
    "TH": Market("TH", "pct",       30.0, "ltp_plus_tick", False),
    "CN": Market("CN", "cn_board",  None, None,            False),
    "TW": Market("TW", "pct",       10.0, None,            False),
    "IN": Market("IN", "oms_only",  None, None,            True),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `4/4 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Start the LULD and short sell audit with the market table"
```

---

## Task 2: The Japan step table

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (MARKET TABLE section)

**Interfaces:**
- Produces: `jp_limit_width(base: float) -> float` — the ± yen limit for a TSE base price

- [ ] **Step 1: Write the failing tests**

```python
def test_jp_step_table_low_end():
    assert jp_limit_width(50) == 30
    assert jp_limit_width(99.9) == 30
    assert jp_limit_width(100) == 50
    assert jp_limit_width(199) == 50
    assert jp_limit_width(200) == 80
    assert jp_limit_width(499) == 80
    assert jp_limit_width(500) == 100
    assert jp_limit_width(699) == 100
    assert jp_limit_width(700) == 150
    assert jp_limit_width(999) == 150


def test_jp_step_table_is_self_similar_per_decade():
    # from 1000 the pattern repeats x10 per decade
    assert jp_limit_width(1000) == 300
    assert jp_limit_width(1500) == 400
    assert jp_limit_width(2000) == 500
    assert jp_limit_width(3000) == 700
    assert jp_limit_width(5000) == 1000
    assert jp_limit_width(7000) == 1500
    assert jp_limit_width(10000) == 3000
    assert jp_limit_width(20000) == 5000
    assert jp_limit_width(50000) == 10000
    assert jp_limit_width(100000) == 30000


def test_jp_step_table_caps_at_ten_million():
    assert jp_limit_width(50_000_000) == 10_000_000
    assert jp_limit_width(90_000_000) == 10_000_000
    assert jp_limit_width(500_000_000) == 10_000_000


def test_jp_step_table_is_monotonic():
    prev = 0.0
    for base in [1, 50, 100, 250, 600, 800, 1200, 1800, 2500, 4000,
                 6000, 8000, 12000, 18000, 25000, 40000, 120000, 1_000_000]:
        w = jp_limit_width(base)
        assert w >= prev, f"width fell at base {base}"
        prev = w
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 4 FAILs with `NameError: name 'jp_limit_width' is not defined` surfacing as an error — if the runner reports an error rather than a FAIL, widen the `except` in `run_self_test` to `except Exception`.

- [ ] **Step 3: Implement**

```python
import math

# TSE daily price limit (seigen nehaba).  Below 1000 the table is irregular and
# is listed; from 1000 up it repeats x10 per decade, so it is generated.
_JP_LOW = ((100, 30), (200, 50), (500, 80), (700, 100), (1000, 150))
_JP_MANTISSA = ((1.5, 300), (2, 400), (3, 500), (5, 700), (7, 1000), (10, 1500))
_JP_CAP = 10_000_000


def jp_limit_width(base: float) -> float:
    """The +/- yen daily limit for a TSE base price (kijun nedan)."""
    if base <= 0:
        return 0.0
    for hi, width in _JP_LOW:
        if base < hi:
            return float(width)
    k = int(math.floor(math.log10(base))) - 3
    m = base / (10.0 ** (k + 3))
    # float division can land m fractionally at or past 10 on exact decade
    # boundaries; clamp rather than fall off the end of the table
    if m >= 10:
        m, k = m / 10.0, k + 1
    for hi, width in _JP_MANTISSA:
        if m < hi:
            return float(min(width * (10 ** k), _JP_CAP))
    return float(_JP_CAP)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `8/8 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Generate the Japanese price limit table instead of listing it"
```

---

## Task 3: China board bands from the symbol prefix

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (MARKET TABLE section)

**Interfaces:**
- Consumes: nothing
- Produces: `cn_band_pct(sym: str, trade_date: datetime.date) -> Optional[float]` — the ± percent for a Chinese symbol, `None` if the prefix is unrecognised

- [ ] **Step 1: Write the failing tests**

```python
import datetime as dt


def test_cn_main_board_is_ten_percent():
    d = dt.date(2026, 7, 16)
    for sym in ("600584.CH", "601398.CH", "603288.CH", "605080.CH",
                "000001.CH", "001979.CH", "002415.CH", "003816.CH"):
        assert cn_band_pct(sym, d) == 10.0, sym


def test_cn_star_board_is_twenty_percent():
    d = dt.date(2026, 7, 16)
    assert cn_band_pct("688981.CH", d) == 20.0
    assert cn_band_pct("689009.CH", d) == 20.0


def test_cn_chinext_is_twenty_percent_only_after_the_2020_reform():
    assert cn_band_pct("300750.CH", dt.date(2026, 7, 16)) == 20.0
    assert cn_band_pct("301029.CH", dt.date(2026, 7, 16)) == 20.0
    # the day the reform took effect, and the day before it
    assert cn_band_pct("300750.CH", dt.date(2020, 8, 24)) == 20.0
    assert cn_band_pct("300750.CH", dt.date(2020, 8, 21)) == 10.0


def test_cn_b_shares_and_beijing():
    d = dt.date(2026, 7, 16)
    assert cn_band_pct("900901.CH", d) == 10.0
    assert cn_band_pct("200011.CH", d) == 10.0
    assert cn_band_pct("430047.CH", d) == 30.0
    assert cn_band_pct("832000.CH", d) == 30.0
    assert cn_band_pct("871981.CH", d) == 30.0
    assert cn_band_pct("920002.CH", d) == 30.0


def test_cn_unknown_prefix_returns_none_rather_than_guessing():
    d = dt.date(2026, 7, 16)
    assert cn_band_pct("123456.CH", d) is None
    assert cn_band_pct("NOTANUM.CH", d) is None
    assert cn_band_pct("", d) is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 5 FAILs, `cn_band_pct` not defined.

- [ ] **Step 3: Implement**

```python
# ChiNext moved from +/-10% to +/-20% on 2020-08-24.  Stored as a date rather
# than a bare number so an audit range straddling it is right on both sides.
CHINEXT_20PCT_FROM = dt.date(2020, 8, 24)


def _cn_digits(sym: str) -> str:
    head = sym.split(".", 1)[0]
    return head if head.isdigit() else ""


def cn_band_pct(sym: str, trade_date: dt.date) -> Optional[float]:
    """+/- percent band for a Chinese symbol, from its board.  None if unknown.

    ST / *ST names are +/-5% but carry that in the NAME, not the code, so they
    are indistinguishable here and come back 10.0 - deliberately wide, which
    under-reports rather than fabricates.  See spec section 1.2.
    """
    d = _cn_digits(sym)
    if len(d) != 6:
        return None
    if d[:3] in ("600", "601", "603", "605"):
        return 10.0
    if d[:3] in ("688", "689"):
        return 20.0
    if d[:3] in ("300", "301"):
        return 20.0 if trade_date >= CHINEXT_20PCT_FROM else 10.0
    if d[:3] in ("000", "001", "002", "003"):
        return 10.0
    if d[:3] in ("900", "200"):
        return 10.0
    if d[:3] == "430" or d[:3] == "920" or d[0] == "8":
        return 30.0
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `13/13 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Read the Chinese board off the symbol, with ChiNext dated"
```

---

## Task 4: Tick grid — inward rounding and empirical ladder recovery

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (TICKS section)

**Interfaces:**
- Produces:
  - `round_inward(price: float, base: float, tick: float) -> float`
  - `recover_tick_ladder(prices) -> list[tuple[float, float]]` — `[(from_price, tick), ...]` ascending, `[]` if the data is not a clean ladder
  - `tick_at(ladder: list[tuple[float, float]], price: float, fallback: float) -> float`

- [ ] **Step 1: Write the failing tests**

```python
import numpy as np


def test_round_inward_never_widens_a_band():
    # base 100, +30% = 130 exactly on grid
    assert round_inward(130.0, 100.0, 0.01) == 130.0
    # 30% of 33.33 is 43.329; inward means DOWN toward base for an upper band
    assert abs(round_inward(43.329, 33.33, 0.01) - 43.32) < 1e-9
    # lower band rounds UP toward base
    assert abs(round_inward(23.331, 33.33, 0.01) - 23.34) < 1e-9


def test_round_inward_is_idempotent_on_grid_values():
    for p in (10.0, 10.05, 99.99):
        assert abs(round_inward(p, 50.0, 0.01) - p) < 1e-9


def test_round_inward_handles_a_zero_tick():
    assert round_inward(43.329, 33.33, 0.0) == 43.329


def test_recover_tick_ladder_finds_a_two_step_grid():
    lo = np.arange(1.0, 2.0, 0.001)      # tick 0.001 below 2
    hi = np.arange(2.0, 3.0, 0.005)      # tick 0.005 from 2
    ladder = recover_tick_ladder(np.concatenate([lo, hi]))
    assert ladder, "expected a ladder"
    assert abs(tick_at(ladder, 1.5, 0.01) - 0.001) < 1e-9
    assert abs(tick_at(ladder, 2.5, 0.01) - 0.005) < 1e-9


def test_recover_tick_ladder_refuses_ragged_data():
    rng = np.random.default_rng(0)
    assert recover_tick_ladder(rng.uniform(1.0, 100.0, 500)) == []


def test_recover_tick_ladder_refuses_too_few_points():
    assert recover_tick_ladder(np.array([1.0, 1.01, 1.02])) == []


def test_tick_at_falls_back_when_ladder_is_empty():
    assert tick_at([], 42.0, 0.05) == 0.05
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 7 FAILs.

- [ ] **Step 3: Implement**

```python
_LADDER_MIN_POINTS = 50
_LADDER_BUCKETS = 12
_LADDER_PURITY = 0.80    # share of gaps that must be the modal gap


def round_inward(price: float, base: float, tick: float) -> float:
    """Snap price to the tick grid, moving TOWARD base.

    An upper band rounds down, a lower band rounds up, so the band is never
    reported wider than the rule allows.
    """
    if tick <= 0 or price <= 0:
        return price
    n = price / tick
    snapped = math.floor(n) * tick if price > base else math.ceil(n) * tick
    return round(snapped, 10)


def recover_tick_ladder(prices) -> list:
    """Recover a tick ladder from observed prices sharing one tsid.

    Returns [(from_price, tick), ...] ascending, or [] when the data does not
    look like a ladder.  Refusing is the point: rounding against a ragged grid
    is worse than falling back to the scalar ticksize.
    """
    p = np.unique(np.asarray(prices, dtype=float))
    p = p[np.isfinite(p) & (p > 0)]
    if p.size < _LADDER_MIN_POINTS:
        return []
    edges = np.geomspace(p[0], p[-1], _LADDER_BUCKETS + 1)
    ladder = []
    for i in range(_LADDER_BUCKETS):
        seg = p[(p >= edges[i]) & (p < edges[i + 1])]
        if seg.size < 8:
            continue
        gaps = np.diff(seg)
        gaps = np.round(gaps[gaps > 0], 8)
        if gaps.size < 5:
            continue
        vals, counts = np.unique(gaps, return_counts=True)
        modal = float(vals[counts.argmax()])
        if counts.max() / gaps.size < _LADDER_PURITY:
            return []
        if not ladder or abs(ladder[-1][1] - modal) > 1e-12:
            ladder.append((float(edges[i]), modal))
    if len(ladder) < 1:
        return []
    return ladder


def tick_at(ladder: list, price: float, fallback: float) -> float:
    """Tick size at a price, from a recovered ladder, else the fallback."""
    if not ladder:
        return fallback
    tick = ladder[0][1]
    for frm, t in ladder:
        if price >= frm:
            tick = t
        else:
            break
    return tick
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `20/20 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Round bands toward the base, on a grid recovered from the prices"
```

---

## Task 5: Band computation and provenance

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (BANDS section)

**Interfaces:**
- Consumes: `MARKETS`, `jp_limit_width`, `cn_band_pct`, `round_inward`, `tick_at`
- Produces:
  - `Band` NamedTuple: `up: float, dn: float, src: str, conf: str`
  - `compute_band(base, country, sym, trade_date, tick) -> Optional[Band]`
  - `reconcile_band(computed, pin, session_high, session_low, country, tick) -> Optional[Band]`

- [ ] **Step 1: Write the failing tests**

```python
def test_compute_band_percent_markets():
    d = dt.date(2026, 7, 16)
    b = compute_band(100.0, "KR", "005930.KS", d, 0.01)
    assert b.up == 130.0 and b.dn == 70.0 and b.src == "computed"
    b = compute_band(100.0, "TW", "2330.TW", d, 0.01)
    assert b.up == 110.0 and b.dn == 90.0


def test_compute_band_japan_uses_the_step_table():
    b = compute_band(1000.0, "JP", "7203.JT", dt.date(2026, 7, 16), 1.0)
    assert b.up == 1300.0 and b.dn == 700.0


def test_compute_band_china_uses_the_board():
    d = dt.date(2026, 7, 16)
    assert compute_band(100.0, "CN", "600584.CH", d, 0.01).up == 110.0
    assert compute_band(100.0, "CN", "688981.CH", d, 0.01).up == 120.0


def test_compute_band_returns_none_where_no_rule_exists():
    d = dt.date(2026, 7, 16)
    assert compute_band(100.0, "HK", "0005.HK", d, 0.01) is None   # no band rule
    assert compute_band(100.0, "IN", "RELIANCE.IN", d, 0.05) is None  # oms only
    assert compute_band(0.0, "KR", "005930.KS", d, 0.01) is None   # no base
    assert compute_band(100.0, "CN", "123456.CH", d, 0.01) is None  # unknown board


def test_compute_band_rounds_inward_so_it_never_exceeds_the_rule():
    b = compute_band(33.33, "KR", "X.KS", dt.date(2026, 7, 16), 0.01)
    assert b.up <= 33.33 * 1.30 + 1e-9
    assert b.dn >= 33.33 * 0.70 - 1e-9


def test_reconcile_confirms_when_a_pin_agrees():
    c = Band(130.0, 70.0, "computed", "assumed")
    r = reconcile_band(c, pin=130.0, session_high=130.0, session_low=95.0,
                       country="KR", tick=0.01)
    assert r.conf == "confirmed"


def test_reconcile_contradicts_and_discards_when_the_session_escapes():
    c = Band(130.0, 70.0, "computed", "assumed")
    r = reconcile_band(c, pin=None, session_high=145.0, session_low=95.0,
                       country="KR", tick=0.01)
    assert r is None, "a contradicted band must be discarded, not reported"


def test_reconcile_widens_rather_than_discards_for_japan():
    c = Band(1300.0, 700.0, "computed", "assumed")
    r = reconcile_band(c, pin=None, session_high=1500.0, session_low=900.0,
                       country="JP", tick=1.0)
    assert r is not None, "Japan widens, it does not suppress"
    assert r.conf == "widened_observed"
    assert r.up == 1500.0


def test_reconcile_leaves_an_untouched_band_assumed():
    c = Band(130.0, 70.0, "computed", "assumed")
    r = reconcile_band(c, pin=None, session_high=110.0, session_low=95.0,
                       country="KR", tick=0.01)
    assert r.conf == "assumed" and r.up == 130.0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 9 FAILs.

- [ ] **Step 3: Implement**

```python
class Band(NamedTuple):
    up: float
    dn: float
    src: str    # override | target_oms | observed | computed
    conf: str   # confirmed | assumed | widened_observed


# Markets where a contradicted band has a KNOWN cause, so the observed extreme
# is a better answer than no answer.  Japan's limits expand overnight after a
# limit close; see spec section 3.2.
WIDEN_ON_CONTRADICTION = {"JP"}


def compute_band(base: float, country: str, sym: str,
                 trade_date: dt.date, tick: float):
    """The rule-derived band, or None where no rule applies or no base exists."""
    m = MARKETS.get(country)
    if m is None or base is None or base <= 0:
        return None
    rule = m.band_rule
    if rule is None or rule == "oms_only":
        return None
    if rule == "pct":
        pct = m.band_pct
    elif rule == "cn_board":
        pct = cn_band_pct(sym, trade_date)
    elif rule == "jp_step":
        w = jp_limit_width(base)
        if w <= 0:
            return None
        return Band(round_inward(base + w, base, tick),
                    round_inward(base - w, base, tick), "computed", "assumed")
    else:
        return None
    if pct is None:
        return None
    return Band(round_inward(base * (1 + pct / 100.0), base, tick),
                round_inward(base * (1 - pct / 100.0), base, tick),
                "computed", "assumed")


def reconcile_band(computed, pin, session_high, session_low,
                   country: str, tick: float):
    """Grade a computed band against what the market actually did.

    Returns a Band, or None when the band is contradicted with no known cause -
    discarding it is deliberate: a wrong band produces confident nonsense.
    """
    if computed is None:
        return None
    tol = tick if tick > 0 else 0.0
    if pin is not None and pin > 0:
        if abs(pin - computed.up) <= tol or abs(pin - computed.dn) <= tol:
            return computed._replace(conf="confirmed")
    escaped_up = session_high is not None and session_high > computed.up + tol
    escaped_dn = session_low is not None and 0 < session_low < computed.dn - tol
    pin_outside = pin is not None and pin > 0 and (
        pin > computed.up + tol or pin < computed.dn - tol)
    if escaped_up or escaped_dn or pin_outside:
        if country not in WIDEN_ON_CONTRADICTION:
            return None
        up = max(computed.up, session_high or 0.0, pin or 0.0)
        lows = [v for v in (computed.dn, session_low, pin) if v and v > 0]
        return Band(up, min(lows), "observed", "widened_observed")
    return computed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `29/29 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Grade every band against what the market actually did"
```

---

## Task 6: State classification

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (STATE section)

**Interfaces:**
- Produces: `classify_state(state: str) -> str` returning one of `rejected | suppressed | halted | never_on_market | normal | unknown`

- [ ] **Step 1: Write the failing tests**

```python
def test_state_rejections_are_rejected():
    for s in ("rejected", "REJECTED", "invalid_ack", "fail_ack"):
        assert classify_state(s) == "rejected", s


def test_state_price_suppression_is_suppressed():
    for s in ("close_bad_price", "close_take_outofmoney", "close_ioi_outofmoney"):
        assert classify_state(s) == "suppressed", s


def test_state_halts_and_volatility_stops_are_halted():
    for s in ("close_stock_halt", "close_order_halt",
              "stopped_volatility_tag262", "stopped_volatility_tag325"):
        assert classify_state(s) == "halted", s


def test_state_never_on_market():
    for s in ("close_not_ack", "close_after_cutoff", "dest_down",
              "close_no_transmit", "fail_ord_status"):
        assert classify_state(s) == "never_on_market", s


def test_state_normal_lifecycle():
    for s in ("filled", "acked", "leave", "done", "cxl", "transmitted"):
        assert classify_state(s) == "normal", s


def test_state_unknown_is_not_silently_normal():
    assert classify_state("something_new_from_the_engine") == "unknown"
    assert classify_state("") == "unknown"
    assert classify_state(None) == "unknown"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 6 FAILs.

- [ ] **Step 3: Implement**

```python
# From OrderStateType in the engine (ai3 src/com/kas/ai/OrderStateType.java).
# LULD failures mostly land in "suppressed" or "halted" and produce NO
# rejection; short sell failures land in "rejected".  That asymmetry is why
# state is carried as an axis and never used as a gate.
_STATE_CLASS = {}
for _s in ("rejected", "invalid_ack", "fail_ack"):
    _STATE_CLASS[_s] = "rejected"
for _s in ("close_bad_price", "close_take_outofmoney", "close_ioi_outofmoney",
           "close_invalid", "close_bad_size", "close_oddlot",
           "close_less_min_order_size"):
    _STATE_CLASS[_s] = "suppressed"
for _s in ("close_stock_halt", "close_order_halt", "close_lunch_hour",
           "stopped_volatility_tag262", "stopped_volatility_tag325"):
    _STATE_CLASS[_s] = "halted"
for _s in ("close_not_ack", "close_after_cutoff", "dest_down",
           "close_no_transmit", "fail_ord_status", "fail_transmit",
           "fail_sys_ack", "fail_no_ref", "failed"):
    _STATE_CLASS[_s] = "never_on_market"
for _s in ("created", "init", "scheduled", "activated", "intransmit",
           "transmitted", "acked", "leave", "cxl_pending", "cxl", "cxlrej",
           "filled", "done", "rpld", "expired", "cxlord_succeed", "closed"):
    _STATE_CLASS[_s] = "normal"


def classify_state(state) -> str:
    """Bucket a workorder state.  Unrecognised states are 'unknown', never
    'normal' - a new engine state must be visible, not absorbed."""
    if not state:
        return "unknown"
    return _STATE_CLASS.get(str(state).strip().lower(), "unknown")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `35/35 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Bucket workorder states, and refuse to call an unknown one normal"
```

---

## Task 7: The §5 rule checks

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (RULES section)

**Interfaces:**
- Consumes: `Band`, `MARKETS`, `classify_state`
- Produces:
  - `Split` NamedTuple: `id_target, id_work, sym, country, side, sidesign, otype, price, size, state, t_transmit, parent_limit, tick, q_bid, q_ask, q_last, q_trdtick, t_bid, t_ask, t_last`
  - `Finding` NamedTuple: `rule, severity, sym, id_target, id_work, expected, delta_ticks, reason`
  - `check_luld_cap(sp, band) -> Optional[Finding]`
  - `check_client_limit(sp) -> Optional[Finding]`
  - `check_ss_hk_ask(sp, ref) -> Optional[Finding]`
  - `check_ss_uptick(sp, ref) -> Optional[Finding]`
  - `check_ss_th_ltp1(sp, ref) -> Optional[Finding]`
  - `check_ss_kr_clamp(sp, band, ref) -> Optional[Finding]`
  - `run_rules(sp, band, ref) -> list[Finding]`
  - `SHORTSELL_SIDE = "sellshort"`

`ref` is the string `"qatt"` or `"transmit"` selecting which market snapshot to judge against; the caller runs every check twice, once per reference.

- [ ] **Step 1: Write the failing tests**

```python
def _split(**kw):
    base = dict(id_target=1, id_work=2, sym="X.KS", country="KR", side="sell",
                sidesign=-1, otype="limit", price=100.0, size=1000,
                state="acked", t_transmit=0, parent_limit=0.0, tick=0.01,
                q_bid=99.0, q_ask=101.0, q_last=100.0, q_trdtick=0,
                t_bid=99.0, t_ask=101.0, t_last=100.0)
    base.update(kw)
    return Split(**base)


def test_luld_cap_flags_a_split_above_the_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    f = check_luld_cap(_split(price=115.0), b)
    assert f is not None and f.rule == "LULD_CAP" and f.severity == "violation"
    assert abs(f.delta_ticks - 500.0) < 1e-6


def test_luld_cap_flags_a_split_below_the_band():
    # the 600584.CH case from the notes: split generated BELOW limit down
    b = Band(41.83, 34.23, "computed", "confirmed")
    f = check_luld_cap(_split(sym="600584.CH", country="CN", price=34.00), b)
    assert f is not None and f.rule == "LULD_CAP"


def test_luld_cap_passes_a_split_at_the_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    assert check_luld_cap(_split(price=110.0), b) is None
    assert check_luld_cap(_split(price=90.0), b) is None


def test_luld_cap_ignores_market_orders_and_missing_bands():
    b = Band(110.0, 90.0, "computed", "confirmed")
    assert check_luld_cap(_split(price=0.0, otype="market"), b) is None
    assert check_luld_cap(_split(price=115.0), None) is None


def test_client_limit_flags_a_buy_priced_through_the_parent_limit():
    f = check_client_limit(_split(side="buy", sidesign=1, price=105.0,
                                  parent_limit=100.0))
    assert f is not None and f.rule == "LULD_CLIENT_LIMIT"


def test_client_limit_flags_a_sell_priced_through_the_parent_limit():
    f = check_client_limit(_split(side="sell", sidesign=-1, price=95.0,
                                  parent_limit=100.0))
    assert f is not None


def test_client_limit_passes_at_the_limit_and_when_absent():
    assert check_client_limit(_split(side="buy", sidesign=1, price=100.0,
                                     parent_limit=100.0)) is None
    assert check_client_limit(_split(side="buy", sidesign=1, price=105.0,
                                     parent_limit=0.0)) is None


def test_hk_short_sell_must_be_at_or_above_the_ask():
    sp = _split(country="HK", sym="0005.HK", price=100.5, q_ask=101.0)
    f = check_ss_hk_ask(sp, "qatt")
    assert f is not None and f.rule == "SS_HK_ASK"
    assert check_ss_hk_ask(_split(country="HK", price=101.0, q_ask=101.0),
                           "qatt") is None


def test_hk_market_order_short_sell_fails_by_construction():
    sp = _split(country="HK", price=0.0, otype="market")
    f = check_ss_hk_ask(sp, "qatt")
    assert f is not None and "market" in f.reason.lower()


def test_uptick_requires_strictly_above_last_on_a_downtick():
    sp = _split(country="JP", price=100.0, q_last=100.0, q_trdtick=-1)
    assert check_ss_uptick(sp, "qatt") is not None
    assert check_ss_uptick(_split(country="JP", price=100.01, q_last=100.0,
                                  q_trdtick=-1), "qatt") is None


def test_uptick_allows_equal_to_last_on_a_zero_plus_tick():
    sp = _split(country="JP", price=100.0, q_last=100.0, q_trdtick=1)
    assert check_ss_uptick(sp, "qatt") is None


def test_thailand_wants_ltp_plus_one_tick():
    sp = _split(country="TH", sym="PTT.TB", price=100.0, q_last=100.0, tick=0.25)
    f = check_ss_th_ltp1(sp, "qatt")
    assert f is not None and f.severity == "deviation"
    below = _split(country="TH", price=99.75, q_last=100.0, tick=0.25)
    assert check_ss_th_ltp1(below, "qatt").severity == "violation"
    ok = _split(country="TH", price=100.25, q_last=100.0, tick=0.25)
    assert check_ss_th_ltp1(ok, "qatt") is None


def test_korea_uptick_price_must_still_be_clamped_to_the_band():
    b = Band(110.0, 90.0, "computed", "confirmed")
    sp = _split(country="KR", price=112.0, q_last=111.0, q_trdtick=1)
    f = check_ss_kr_clamp(sp, b, "qatt")
    assert f is not None and f.rule == "SS_KR_CLAMP"
    assert abs(f.expected - 110.0) < 1e-9


def test_run_rules_skips_short_sell_checks_on_unconfirmed_markets():
    b = Band(110.0, 90.0, "computed", "confirmed")
    for country, sym in (("CN", "600584.CH"), ("TW", "2330.TW"),
                         ("IN", "RELIANCE.IN")):
        sp = _split(country=country, sym=sym, side=SHORTSELL_SIDE,
                    sidesign=-1, price=95.0, q_ask=101.0, q_last=100.0,
                    q_trdtick=-1)
        rules = {f.rule for f in run_rules(sp, b, "qatt")}
        assert not any(r.startswith("SS_") for r in rules), (country, rules)


def test_run_rules_runs_short_sell_checks_on_confirmed_markets():
    b = Band(110.0, 90.0, "computed", "confirmed")
    sp = _split(country="JP", sym="7203.JT", side=SHORTSELL_SIDE, sidesign=-1,
                price=100.0, q_last=100.0, q_trdtick=-1)
    assert "SS_UPTICK" in {f.rule for f in run_rules(sp, b, "qatt")}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 15 FAILs.

- [ ] **Step 3: Implement**

```python
SHORTSELL_SIDE = "sellshort"   # confirmed value of target.side


class Split(NamedTuple):
    id_target: int
    id_work: int
    sym: str
    country: str
    side: str
    sidesign: int
    otype: str
    price: float
    size: int
    state: str
    t_transmit: int
    parent_limit: float
    tick: float
    q_bid: float
    q_ask: float
    q_last: float
    q_trdtick: int
    t_bid: float
    t_ask: float
    t_last: float


class Finding(NamedTuple):
    rule: str
    severity: str      # violation | deviation | opportunity | improvement
    sym: str
    id_target: int
    id_work: int
    expected: float
    delta_ticks: float
    reason: str


def _mkt(sp: Split, ref: str):
    """(bid, ask, last) from the chosen reference - qatt or the algo's own snapshot."""
    return (sp.q_bid, sp.q_ask, sp.q_last) if ref == "qatt" else (sp.t_bid, sp.t_ask, sp.t_last)


def _ticks(delta: float, tick: float) -> float:
    return delta / tick if tick > 0 else 0.0


def _priced(sp: Split) -> bool:
    return sp.price is not None and sp.price > 0


def check_luld_cap(sp: Split, band):
    if band is None or not _priced(sp):
        return None
    if sp.price > band.up:
        return Finding("LULD_CAP", "violation", sp.sym, sp.id_target, sp.id_work,
                       band.up, _ticks(sp.price - band.up, sp.tick),
                       f"split priced {sp.price} above limit up {band.up}")
    if sp.price < band.dn:
        return Finding("LULD_CAP", "violation", sp.sym, sp.id_target, sp.id_work,
                       band.dn, _ticks(band.dn - sp.price, sp.tick),
                       f"split priced {sp.price} below limit down {band.dn}")
    return None


def check_client_limit(sp: Split):
    if not _priced(sp) or not sp.parent_limit or sp.parent_limit <= 0:
        return None
    if sp.sidesign > 0 and sp.price > sp.parent_limit:
        d = sp.price - sp.parent_limit
    elif sp.sidesign < 0 and sp.price < sp.parent_limit:
        d = sp.parent_limit - sp.price
    else:
        return None
    return Finding("LULD_CLIENT_LIMIT", "violation", sp.sym, sp.id_target,
                   sp.id_work, sp.parent_limit, _ticks(d, sp.tick),
                   f"split priced {sp.price} through client limit {sp.parent_limit}")


def check_ss_hk_ask(sp: Split, ref: str):
    _, ask, _ = _mkt(sp, ref)
    if not _priced(sp):
        return Finding("SS_HK_ASK", "violation", sp.sym, sp.id_target, sp.id_work,
                       ask, 0.0,
                       "market order short sell in HK cannot satisfy always-ask")
    if ask is None or ask <= 0:
        return None
    if sp.price < ask:
        return Finding("SS_HK_ASK", "violation", sp.sym, sp.id_target, sp.id_work,
                       ask, _ticks(ask - sp.price, sp.tick),
                       f"short sell priced {sp.price} below ask {ask} ({ref})")
    return None


def check_ss_uptick(sp: Split, ref: str):
    _, _, last = _mkt(sp, ref)
    if not _priced(sp) or last is None or last <= 0:
        return None
    # zero-plus tick: equal to last is allowed only when the last tick was up
    ok = sp.price > last or (sp.price == last and sp.q_trdtick > 0)
    if ok:
        return None
    return Finding("SS_UPTICK", "violation", sp.sym, sp.id_target, sp.id_work,
                   last, _ticks(last - sp.price, sp.tick),
                   f"short sell priced {sp.price} at/below last {last} "
                   f"on a non-uptick ({ref})")


def check_ss_th_ltp1(sp: Split, ref: str):
    _, _, last = _mkt(sp, ref)
    if not _priced(sp) or last is None or last <= 0 or sp.tick <= 0:
        return None
    want = round(last + sp.tick, 10)
    if abs(sp.price - want) < 1e-9:
        return None
    sev = "violation" if sp.price < want else "deviation"
    return Finding("SS_TH_LTP1", sev, sp.sym, sp.id_target, sp.id_work, want,
                   _ticks(sp.price - want, sp.tick),
                   f"short sell priced {sp.price}, LTP+1 tick is {want} ({ref})")


def check_ss_kr_clamp(sp: Split, band, ref: str):
    if band is None or not _priced(sp) or sp.price <= band.up:
        return None
    return Finding("SS_KR_CLAMP", "violation", sp.sym, sp.id_target, sp.id_work,
                   band.up, _ticks(sp.price - band.up, sp.tick),
                   f"uptick price {sp.price} sent through limit up {band.up} "
                   f"instead of being capped ({ref})")


def run_rules(sp: Split, band, ref: str) -> list:
    """Every §5 check that applies to this split, for one reference market."""
    out = []
    for f in (check_luld_cap(sp, band), check_client_limit(sp)):
        if f:
            out.append(f)
    if sp.side != SHORTSELL_SIDE:
        return out
    m = MARKETS.get(sp.country)
    if m is None or m.ss_rule is None:
        return out            # CN / TW / IN -> RULE_UNKNOWN, counted elsewhere
    if m.ss_rule == "always_ask":
        f = check_ss_hk_ask(sp, ref)
    elif m.ss_rule == "uptick":
        f = check_ss_uptick(sp, ref)
    elif m.ss_rule == "ltp_plus_tick":
        f = check_ss_th_ltp1(sp, ref)
    else:
        f = None
    if f:
        out.append(f)
    if sp.country == "KR":
        f = check_ss_kr_clamp(sp, band, ref)
        if f:
            out.append(f)
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `50/50 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Check each split against its market's rule, twice over"
```

---

## Task 8: The situational detectors

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (DETECTORS section)

**Interfaces:**
- Consumes: `Band`, `Finding`, `classify_state`
- Produces:
  - `Pin` NamedTuple: `sym, side_pinned ("up"|"down"), start, end, price`
  - `Parent` NamedTuple: `id_target, sym, country, sidesign, state, leave, t_start, t_end, doclose, halted, size, fxlast`
  - `is_favourable(sidesign: int, pinned: str) -> bool`
  - `detect_favourable_no_split(parent, pin, splits, pin_mins) -> Optional[Finding]`
  - `detect_unfavourable_churn(parent, pin, splits) -> Optional[Finding]`
  - `detect_guard_inactive(sym, cap_findings) -> Optional[Finding]`

- [ ] **Step 1: Write the failing tests**

```python
def _parent(**kw):
    base = dict(id_target=1, sym="7203.JT", country="JP", sidesign=-1,
                state="activated", leave=5000, t_start=0, t_end=30_000_000,
                doclose=0, halted=False, size=10000, fxlast=0.0068)
    base.update(kw)
    return Parent(**base)


def _pin(**kw):
    base = dict(sym="7203.JT", side_pinned="up", start=10_000_000,
                end=20_000_000, price=1300.0)
    base.update(kw)
    return Pin(**base)


def test_favourable_is_selling_into_a_limit_up():
    assert is_favourable(-1, "up") is True
    assert is_favourable(1, "up") is False
    assert is_favourable(1, "down") is True
    assert is_favourable(-1, "down") is False


def test_no_split_fires_when_we_could_have_sold_into_a_limit_up():
    f = detect_favourable_no_split(_parent(), _pin(), splits=[], pin_mins=5)
    assert f is not None
    assert f.rule == "LULD_FAVOURABLE_NO_SPLIT" and f.severity == "opportunity"


def test_no_split_is_silent_when_splits_exist():
    assert detect_favourable_no_split(_parent(), _pin(), splits=[object()],
                                      pin_mins=5) is None


def test_no_split_guard_parent_must_be_activated():
    assert detect_favourable_no_split(_parent(state="scheduled"), _pin(),
                                      [], 5) is None


def test_no_split_guard_needs_something_left_to_work():
    assert detect_favourable_no_split(_parent(leave=0), _pin(), [], 5) is None


def test_no_split_guard_pin_must_outlast_pin_mins():
    short = _pin(start=10_000_000, end=10_120_000)   # two minutes
    assert detect_favourable_no_split(_parent(), short, [], pin_mins=5) is None


def test_no_split_guard_pin_must_fall_inside_the_parent_window():
    late = _pin(start=40_000_000, end=45_000_000)
    assert detect_favourable_no_split(_parent(), late, [], 5) is None


def test_no_split_guard_skips_halted_and_close_only_parents():
    assert detect_favourable_no_split(_parent(halted=True), _pin(), [], 5) is None
    assert detect_favourable_no_split(_parent(doclose=1, t_start=29_000_000),
                                      _pin(), [], 5) is None


def test_no_split_is_silent_on_the_unfavourable_side():
    assert detect_favourable_no_split(_parent(sidesign=1), _pin(), [], 5) is None


def test_churn_fires_when_we_keep_sending_into_a_wall():
    f = detect_unfavourable_churn(_parent(sidesign=1), _pin(),
                                  splits=[object()] * 12)
    assert f is not None and f.rule == "LULD_UNFAVOURABLE_CHURN"
    assert f.severity == "improvement"


def test_churn_needs_more_than_a_couple_of_splits():
    assert detect_unfavourable_churn(_parent(sidesign=1), _pin(),
                                     splits=[object()] * 2) is None


def test_guard_inactive_needs_a_pattern_not_an_incident():
    one = [Finding("LULD_CAP", "violation", "X.CH", 1, 2, 10.0, 1.0, "")]
    assert detect_guard_inactive("X.CH", one) is None
    three = one * 3
    f = detect_guard_inactive("X.CH", three)
    assert f is not None and f.rule == "LULD_GUARD_INACTIVE"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 12 FAILs.

- [ ] **Step 3: Implement**

```python
CHURN_MIN_SPLITS = 5
GUARD_INACTIVE_MIN_BREACHES = 3


class Pin(NamedTuple):
    sym: str
    side_pinned: str    # "up" | "down"
    start: int          # ms since midnight
    end: int
    price: float


class Parent(NamedTuple):
    id_target: int
    sym: str
    country: str
    sidesign: int
    state: str
    leave: int
    t_start: int
    t_end: int
    doclose: int
    halted: bool
    size: int
    fxlast: float


def is_favourable(sidesign: int, pinned: str) -> bool:
    """Selling into a limit up, or buying into a limit down - the side that
    CAN fill, because there is a queue resting at the band."""
    return (sidesign < 0 and pinned == "up") or (sidesign > 0 and pinned == "down")


def _pin_overlaps_parent(parent: Parent, pin: Pin) -> bool:
    return pin.start < parent.t_end and pin.end > parent.t_start


def detect_favourable_no_split(parent: Parent, pin: Pin, splits, pin_mins: int):
    """We could have traded at the band and sent nothing.

    Every guard is separate and each is a column on the output row, so a false
    positive can be diagnosed rather than argued about.
    """
    if not is_favourable(parent.sidesign, pin.side_pinned):
        return None
    if splits:
        return None
    if str(parent.state).lower() != "activated":
        return None
    if parent.leave is None or parent.leave <= 0:
        return None
    if parent.halted:
        return None
    if parent.doclose and parent.t_start >= parent.t_end - 60_000:
        return None
    if not _pin_overlaps_parent(parent, pin):
        return None
    held_mins = (min(pin.end, parent.t_end) - max(pin.start, parent.t_start)) / 60_000.0
    if held_mins < pin_mins:
        return None
    return Finding("LULD_FAVOURABLE_NO_SPLIT", "opportunity", parent.sym,
                   parent.id_target, 0, pin.price, 0.0,
                   f"{parent.leave} left, stock pinned limit {pin.side_pinned} at "
                   f"{pin.price} for {held_mins:.0f} min on our fillable side, "
                   f"no child split generated")


def detect_unfavourable_churn(parent: Parent, pin: Pin, splits):
    """Splits that cannot fill, sent into the band anyway."""
    if is_favourable(parent.sidesign, pin.side_pinned):
        return None
    n = len(splits)
    if n < CHURN_MIN_SPLITS:
        return None
    if not _pin_overlaps_parent(parent, pin):
        return None
    return Finding("LULD_UNFAVOURABLE_CHURN", "improvement", parent.sym,
                   parent.id_target, 0, pin.price, 0.0,
                   f"{n} splits sent while pinned limit {pin.side_pinned} on the "
                   f"side that cannot fill")


def detect_guard_inactive(sym: str, cap_findings):
    """A rollup of LULD_CAP, not a finding of its own.

    Three or more breaches on one stock is the cap not being applied at all,
    rather than missed once.  The constituent splits stay counted under
    LULD_CAP; this row is a stock count.
    """
    n = len(cap_findings)
    if n < GUARD_INACTIVE_MIN_BREACHES:
        return None
    f0 = cap_findings[0]
    return Finding("LULD_GUARD_INACTIVE", "violation", sym, f0.id_target, 0,
                   f0.expected, 0.0,
                   f"{n} splits priced through the band on one stock - the cap "
                   f"looks inactive rather than missed")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `62/62 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Detect the splits we should have sent and the ones we should not have"
```

---

## Task 9: The q sources

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (Q SOURCES section)

**Interfaces:**
- Produces: `Q_ORDERS`, `Q_BAND`, `Q_MKT`, `Q_OMS_BAND` (module-level strings); `connect(endpoint)`; `_check_servers()`

There is no kdb here, so these are tested structurally only. That is the honest limit, and §9 of the spec says so: the q half is checked by reconciliation against `limit_up_down.q` on real data.

- [ ] **Step 1: Write the failing tests**

```python
def test_q_sources_are_lambdas_with_typed_parameters():
    for name, src in (("Q_ORDERS", Q_ORDERS), ("Q_BAND", Q_BAND),
                      ("Q_MKT", Q_MKT), ("Q_OMS_BAND", Q_OMS_BAND)):
        s = src.strip()
        assert s.startswith("{["), f"{name} must be a lambda taking named args"
        assert s.endswith("}"), f"{name} must be a closed lambda"


def test_q_sources_never_interpolate_python_values():
    for src in (Q_ORDERS, Q_BAND, Q_MKT, Q_OMS_BAND):
        assert "%s" not in src and "{}" not in src.replace("{[", "")
        assert "format(" not in src


def test_q_sources_filter_on_date_first():
    # date is the partition column; not constraining it first scans the HDB
    for name, src in (("Q_ORDERS", Q_ORDERS), ("Q_OMS_BAND", Q_OMS_BAND)):
        for line in src.splitlines():
            if " from " in line and "where" in line:
                assert "date=d" in line or "date =d" in line, f"{name}: {line}"


def test_q_orders_collapses_workorder_to_one_row_per_id_work():
    assert "by date,id_server,id_work" in Q_ORDERS.replace(" ", "")\
        .replace("bydate,id_server,id_work", "by date,id_server,id_work") \
        or "by date,id_server,id_work" in Q_ORDERS


def test_q_orders_excludes_indonesia():
    assert "EXCLUDED_COUNTRIES" in Q_ORDERS or "ex_ctry" in Q_ORDERS


def test_check_servers_refuses_placeholders():
    try:
        _check_servers()
    except SystemExit as e:
        assert "CHANGEME" in str(e)
    else:
        raise AssertionError("must refuse to run against a CHANGEME endpoint")
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 6 FAILs.

- [ ] **Step 3: Implement**

```python
# ---------------------------------------------------------------------------
# Q SOURCES.  Sent as text with TYPED arguments; nothing is interpolated.
# ctry arrives as a CHAR VECTOR (b"CN"), never a python str - PyKX turns a str
# into a q symbol and `$ on a symbol is a 'type error.
# ---------------------------------------------------------------------------

# Parents, their state, their stock reference, and their child splits, for one
# date.  workorder is reduced to one row per id_work with `last` BEFORE any
# join: if it already holds one row per child that is free, and if it ever
# holds a row per state change it is the difference between a correct split
# count and a silently multiplied one.
Q_ORDERS = """
{[d;ctry;exctry]
  t:select date,id_server,id_target,sym,side,sidesign,size,otype,limit_price,
      t_start,t_end,algo,doclose
    from target where date=d;
  ids:exec distinct id_target from t;
  x:select date,id_server,id_target,country,ticksize,tsid,orgclose,adjclose,
      fxlast,ipo
    from target_stock where date=d, id_target in ids;
  x:select from x where not country in exctry;
  x:$[0=count ctry; x; select from x where country=`$ctry];
  x:`date`id_server`id_target xkey x;
  t:t ij x;
  ids:exec distinct id_target from t;
  s:select state:last state, leave:last leave, make:last make
    by date,id_server,id_target from target_state where date=d, id_target in ids;
  t:t lj `date`id_server`id_target xkey 0!s;
  w:select date,id_server,id_work,id_target,sym,side,size,otype,price,
      limit_target,venue,venuetype,state,count_send,count_chaseprice,make,
      t_gen,t_transmit,t_on_market,t_off_market,
      transmit_bidprice,transmit_askprice,transmit_lastprice
    from workorder where date=d, id_target in ids;
  w:0!select last id_target, last sym, last side, last size, last otype,
      last price, last limit_target, last venue, last venuetype, last state,
      last count_send, last count_chaseprice, last make, last t_gen,
      last t_transmit, last t_on_market, last t_off_market,
      last transmit_bidprice, last transmit_askprice, last transmit_lastprice
    by date,id_server,id_work from w;
  (t;w)
  }
"""

# Band evidence from qatt, for one date and a list of syms.  Returns one row
# per sym: the first tick carrying a usable netChange (the previous close), the
# session extremes, and the pinned price if the book ever locked or went one
# sided.  Rows with nothing on either side are trade prints or pre-open gaps
# and would read as one sided, so they are dropped first.
Q_BAND = """
{[d;syms]
  q:select time,sym,price,qbid:0^qbid,qask:0^qask,netChange:0^netChange,
      pctChange:0^pctChange,highPrice,lowPrice,lastPrice,trdTick
    from qatt where date=d, sym in syms, (0<0^qbid)|0<0^qask;
  q:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from q;
  b:select preClsTick:first price, preClsNet:first netChange,
      preClsPct:first pctChange
    by sym from q where price>0, netChange<>0;
  e:select sessHigh:max highPrice, sessLow:min lowPrice,
      firstTime:first time, lastTime:last time
    by sym from q;
  p:select pinPrice:last ?[0=qask;qbid;qask], pinUp:last 0=qask,
      pinStart:first time, pinEnd:last time, pinTicks:count i
    by sym from q where lim;
  ((0!b) lj `sym xkey 0!e) lj `sym xkey 0!p
  }
"""

# The prevailing quote at each split's transmit time.  aj returns the last
# quote at or before the target time, and preserves the left table's row order,
# so the columns concatenate straight back onto the splits.  Order and qatt
# share one clock, so no conversion is applied.
Q_MKT = """
{[d;f]
  qt:`sym`time xasc select time,sym,qbid:0^qbid,qask:0^qask,
      lastPrice:0^lastPrice,trdTick:0^trdTick
    from qatt where date=d, sym in exec distinct sym from f;
  r:aj[`sym`time; `sym`time xasc select sym, time:t_transmit from f; qt];
  select qbid,qask,lastPrice,trdTick from r
  }
"""

# The engine's own band, for the markets where it is reliable.  target_oms is a
# tickstream - many rows per id_target - so this takes the band PREVAILING at
# t_transmit, and only from rows that actually carry one.  FlexOrderStream
# writes limitup/limitdn only when it has a quote, so zero rows mean "not known
# at this instant", not "no band"; reading the last row blindly would return 0
# and that 0 would then report itself as a missing guard.
Q_OMS_BAND = """
{[d;f]
  o:`id_target`t_algo xasc select id_target,t_algo,limitup,limitdn
    from target_oms where date=d, limitup>0, limitdn>0,
      id_target in exec distinct id_target from f;
  r:aj[`id_target`t_algo;
      `id_target`t_algo xasc select id_target, t_algo:t_transmit from f; o];
  select limitup,limitdn from r
  }
"""

EXCLUDED_COUNTRIES = ("ID",)          # Indonesia, see spec section 2
BAND_FROM_TARGET_OMS = ("IN", "KR")   # see spec section 3


def _check_servers():
    bad = [n for n, v in (("ORDER_SERVER", ORDER_SERVER),
                          ("QATT_SERVER", QATT_SERVER)) if _PLACEHOLDER in v]
    if bad:
        raise SystemExit(
            f"{', '.join(bad)} still set to {_PLACEHOLDER}. Edit the constants "
            "at the top of this script before running against kdb.")


def connect(endpoint: str):
    """Open a PyKX connection.  Imported lazily so --self-test runs anywhere."""
    import pykx as kx
    host, port = endpoint.rsplit(":", 1)
    return kx.SyncQConnection(host=host, port=int(port),
                              username=USER, password=PASSWORD)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `68/68 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Ask kdb for the orders, the band evidence and the quote at transmit"
```

---

## Task 10: Scoring and the scorecard

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (SCORING, REPORT sections)

**Interfaces:**
- Consumes: `Finding`, `Band`, `MARKETS`
- Produces:
  - `impact_usd(finding, size, fxlast, price) -> float`
  - `Tally` class with `.add(country, rule, severity)`, `.unverifiable(country)`, `.suppress(country, sym)`, `.excluded(country)`
  - `scorecard(tally) -> str`

- [ ] **Step 1: Write the failing tests**

```python
def test_impact_is_price_delta_times_size_in_usd():
    f = Finding("LULD_CAP", "violation", "X.JT", 1, 2, 1300.0, 5.0,
                "priced 1305 above limit up 1300")
    # 5 yen through, 1000 shares, fx 0.0068 -> 34 USD
    assert abs(impact_usd(f, size=1000, fxlast=0.0068, price=1305.0) - 34.0) < 1e-6


def test_impact_for_a_no_split_is_the_unfilled_notional():
    f = Finding("LULD_FAVOURABLE_NO_SPLIT", "opportunity", "X.JT", 1, 0,
                1300.0, 0.0, "5000 left")
    assert abs(impact_usd(f, size=5000, fxlast=0.0068, price=1300.0)
               - 5000 * 1300.0 * 0.0068) < 1e-6


def test_impact_is_zero_without_an_fx_rate_rather_than_wrong():
    f = Finding("LULD_CAP", "violation", "X.JT", 1, 2, 1300.0, 5.0, "")
    assert impact_usd(f, size=1000, fxlast=0.0, price=1305.0) == 0.0


def test_tally_counts_by_market_and_severity():
    t = Tally()
    t.add("JP", "LULD_CAP", "violation")
    t.add("JP", "LULD_CAP", "violation")
    t.add("JP", "SS_UPTICK", "violation")
    t.add("KR", "SS_TH_LTP1", "deviation")
    assert t.counts[("JP", "LULD_CAP")]["violation"] == 2
    assert t.counts[("KR", "SS_TH_LTP1")]["deviation"] == 1


def test_tally_tracks_unverifiable_separately_from_clean():
    t = Tally()
    t.unverifiable("CN")
    t.unverifiable("CN")
    assert t.unverifiable_n["CN"] == 2


def test_scorecard_marks_unchecked_markets_as_unchecked_not_ok():
    t = Tally()
    t.add("JP", "SS_UPTICK", "violation")
    t.unverifiable("TW")
    out = scorecard(t)
    assert "TW" in out
    assert "unchecked" in out.lower() or "RULE_UNKNOWN" in out
    assert "NotOK" in out          # JP has a violation


def test_scorecard_reports_a_clean_market_as_ok():
    t = Tally()
    t.seen("MY", "SS_UPTICK", 40)
    assert "OK" in scorecard(t)
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 7 FAILs.

- [ ] **Step 3: Implement**

```python
from collections import defaultdict

_NOTIONAL_RULES = {"LULD_FAVOURABLE_NO_SPLIT", "LULD_FAVOURABLE_PASSIVE",
                   "LULD_APPROACH_BACKOFF"}


def impact_usd(finding: Finding, size, fxlast, price) -> float:
    """What the finding is worth, in USD, so the report can sort by it.

    Returns 0.0 rather than a wrong number when fx is missing - an unsortable
    row is better than a misleading one.
    """
    if not fxlast or fxlast <= 0 or not size or size <= 0:
        return 0.0
    if finding.rule in _NOTIONAL_RULES:
        return float(size) * float(price or 0.0) * float(fxlast)
    if finding.expected and price:
        return abs(float(price) - float(finding.expected)) * float(size) * float(fxlast)
    return 0.0


class Tally:
    """Per-market, per-rule counters.  Cheap enough to keep for a whole range."""

    def __init__(self):
        self.counts = defaultdict(lambda: defaultdict(int))
        self.seen_n = defaultdict(int)
        self.unverifiable_n = defaultdict(int)
        self.suppressed = defaultdict(set)
        self.excluded_n = defaultdict(int)

    def add(self, country, rule, severity):
        self.counts[(country, rule)][severity] += 1

    def seen(self, country, rule, n=1):
        self.seen_n[(country, rule)] += n

    def unverifiable(self, country, n=1):
        self.unverifiable_n[country] += n

    def suppress(self, country, sym):
        self.suppressed[country].add(sym)

    def excluded(self, country, n=1):
        self.excluded_n[country] += n


def scorecard(tally: Tally) -> str:
    """The spec section 1 table, recomputed from what we actually found."""
    rows = []
    hdr = f"{'market':<7}{'rule':<24}{'checked':>9}{'viol':>7}{'dev':>7}{'opp':>7}  status"
    rows.append(hdr)
    rows.append("-" * len(hdr))
    keys = sorted(set(list(tally.counts) + list(tally.seen_n)))
    for country, rule in keys:
        c = tally.counts[(country, rule)]
        seen = tally.seen_n[(country, rule)]
        v, d, o = c["violation"], c["deviation"], c["opportunity"]
        status = "NotOK" if v else ("check" if d or o else "OK")
        rows.append(f"{country:<7}{rule:<24}{seen:>9}{v:>7}{d:>7}{o:>7}  {status}")
    for country, n in sorted(tally.unverifiable_n.items()):
        rows.append(f"{country:<7}{'RULE_UNKNOWN':<24}{n:>9}{'-':>7}{'-':>7}{'-':>7}  "
                    f"unchecked (no confirmed rule)")
    for country, n in sorted(tally.excluded_n.items()):
        rows.append(f"{country:<7}{'excluded_market':<24}{n:>9}{'-':>7}{'-':>7}{'-':>7}  "
                    f"out of scope")
    return "\n".join(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `75/75 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Score findings by what they cost, and show unchecked as unchecked"
```

---

## Task 11: The band override file and the per-date run loop

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (BANDS, RUN, MAIN sections)

**Interfaces:**
- Consumes: everything above
- Produces:
  - `load_band_overrides(path) -> dict[tuple[dt.date, str], Band]`
  - `resolve_band(...) -> Optional[Band]` — full chain: override → target_oms → observed/computed → reconcile
  - `run(args) -> int`

- [ ] **Step 1: Write the failing tests**

```python
import io as _io
import tempfile, os


def test_band_overrides_load_and_key_on_date_and_sym():
    csv = ("date,sym,limit_up,limit_dn,source\n"
           "2026.07.16,600584.CH,41.83,34.23,exchange\n"
           "2026-07-17,7203.JT,1500,1100,exchange\n")
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as fh:
        fh.write(csv)
    try:
        ov = load_band_overrides(path)
    finally:
        os.unlink(path)
    b = ov[(dt.date(2026, 7, 16), "600584.CH")]
    assert b.up == 41.83 and b.dn == 34.23
    assert b.src == "override" and b.conf == "confirmed"
    assert (dt.date(2026, 7, 17), "7203.JT") in ov, "both date formats must parse"


def test_resolve_band_prefers_an_override_over_everything():
    ov = {(dt.date(2026, 7, 16), "X.KS"):
          Band(200.0, 50.0, "override", "confirmed")}
    b = resolve_band(sym="X.KS", country="KR", trade_date=dt.date(2026, 7, 16),
                     base=100.0, tick=0.01, oms_up=111.0, oms_dn=89.0,
                     pin=None, pin_up=None, sess_high=None, sess_low=None,
                     overrides=ov)
    assert b.src == "override" and b.up == 200.0


def test_resolve_band_uses_target_oms_for_korea():
    b = resolve_band(sym="X.KS", country="KR", trade_date=dt.date(2026, 7, 16),
                     base=100.0, tick=0.01, oms_up=111.0, oms_dn=89.0,
                     pin=None, pin_up=None, sess_high=None, sess_low=None,
                     overrides={})
    assert b.src == "target_oms" and b.up == 111.0 and b.conf == "confirmed"


def test_resolve_band_ignores_target_oms_for_japan():
    b = resolve_band(sym="7203.JT", country="JP", trade_date=dt.date(2026, 7, 16),
                     base=1000.0, tick=1.0, oms_up=9999.0, oms_dn=1.0,
                     pin=None, pin_up=None, sess_high=None, sess_low=None,
                     overrides={})
    assert b.src == "computed" and b.up == 1300.0


def test_resolve_band_ignores_a_nonpositive_oms_band():
    b = resolve_band(sym="X.KS", country="KR", trade_date=dt.date(2026, 7, 16),
                     base=100.0, tick=0.01, oms_up=0.0, oms_dn=0.0,
                     pin=None, pin_up=None, sess_high=None, sess_low=None,
                     overrides={})
    assert b.src == "computed", "a zero oms band means 'unknown', not 'no band'"


def test_resolve_band_returns_none_for_india_without_oms():
    b = resolve_band(sym="RELIANCE.IN", country="IN",
                     trade_date=dt.date(2026, 7, 16), base=100.0, tick=0.05,
                     oms_up=0.0, oms_dn=0.0, pin=None, pin_up=None,
                     sess_high=None, sess_low=None, overrides={})
    assert b is None, "India has no computable band"
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 6 FAILs.

- [ ] **Step 3: Implement**

```python
import csv as _csv


def _parse_date(s: str) -> dt.date:
    s = s.strip().replace(".", "-")
    return dt.date(*(int(x) for x in s.split("-")))


def load_band_overrides(path):
    """CSV of known bands that wins over every computed layer.

    Partial coverage is fine - a sym present uses it, a sym absent falls
    through to the normal chain.  See spec section 3.3.
    """
    out = {}
    if not path:
        return out
    with open(path, newline="") as fh:
        for row in _csv.DictReader(fh):
            up, dn = float(row["limit_up"]), float(row["limit_dn"])
            if up <= 0 or dn <= 0 or dn > up:
                continue
            out[(_parse_date(row["date"]), row["sym"].strip())] = Band(
                up, dn, "override", "confirmed")
    return out


def resolve_band(sym, country, trade_date, base, tick, oms_up, oms_dn,
                 pin, pin_up, sess_high, sess_low, overrides):
    """The full chain: override, then target_oms where allowed, then computed
    and reconciled against what the market did.  None means no usable band."""
    hit = overrides.get((trade_date, sym))
    if hit is not None:
        return hit
    if country in BAND_FROM_TARGET_OMS and oms_up and oms_dn \
            and oms_up > 0 and oms_dn > 0:
        return Band(float(oms_up), float(oms_dn), "target_oms", "confirmed")
    computed = compute_band(base, country, sym, trade_date, tick)
    if computed is None:
        return None
    return reconcile_band(computed, pin, sess_high, sess_low, country, tick)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `81/81 passed`

- [ ] **Step 5: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Resolve a band from the best source that has one"
```

---

## Task 12: CLI, run loop, workbook and README

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (RUN, REPORT, MAIN)
- Create: `scripts/luld_shortsell_check/README.md`

**Interfaces:**
- Consumes: everything above
- Produces: `daterange(start, end) -> list[dt.date]`; `write_workbook(out_dir, findings)`; a complete `main()`

- [ ] **Step 1: Write the failing tests**

```python
def test_daterange_is_inclusive_of_both_ends():
    ds = daterange(dt.date(2026, 7, 16), dt.date(2026, 7, 18))
    assert ds == [dt.date(2026, 7, 16), dt.date(2026, 7, 17), dt.date(2026, 7, 18)]


def test_daterange_rejects_a_reversed_range():
    try:
        daterange(dt.date(2026, 7, 18), dt.date(2026, 7, 16))
    except SystemExit:
        return
    raise AssertionError("a reversed range must be refused, not silently empty")


def test_parser_defaults_match_the_spec():
    p = build_parser()
    a = p.parse_args(["--start", "2026-07-16", "--end", "2026-07-18"])
    assert a.pin_mins == 5
    assert a.approach_pct == 1.0
    assert a.chase_ticks == 2
    assert a.chase_secs == 30
    assert a.checks == "all"


def test_parser_accepts_the_documented_flags():
    p = build_parser()
    a = p.parse_args(["--start", "2026-07-16", "--end", "2026-07-16",
                      "--country", "CN", "--band-file", "b.csv",
                      "--out-dir", "out", "--diagnose", "--quiet"])
    assert a.country == "CN" and a.band_file == "b.csv"
    assert a.out_dir == "out" and a.diagnose and a.quiet
```

- [ ] **Step 2: Run to verify they fail**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: 4 FAILs.

- [ ] **Step 3: Implement**

```python
def daterange(start: dt.date, end: dt.date) -> list:
    if end < start:
        raise SystemExit(f"--end {end} is before --start {start}")
    return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]


def write_workbook(out_dir: str, findings_by_rule: dict) -> str:
    """One sheet per rule, every finding in full.  Cells hold numbers, not
    rendered strings, so the workbook can be sorted and charted."""
    import os
    from openpyxl import Workbook
    os.makedirs(out_dir, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    cols = ["date", "id_target", "id_work", "sym", "country", "side", "state",
            "state_class", "t_transmit", "price_sent", "expected_price",
            "delta_ticks", "band_up", "band_dn", "band_src", "band_conf",
            "qbid", "qask", "lastPrice", "trdTick", "transmit_bid",
            "transmit_ask", "transmit_last", "severity", "confidence",
            "impact_usd", "reason"]
    for rule, rows in sorted(findings_by_rule.items()):
        ws = wb.create_sheet(rule[:31])
        ws.append(cols)
        for r in sorted(rows, key=lambda x: -x.get("impact_usd", 0.0)):
            ws.append([r.get(c) for c in cols])
    path = os.path.join(out_dir, "report.xlsx")
    wb.save(path)
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", type=_parse_date, help="first date, YYYY-MM-DD")
    p.add_argument("--end", type=_parse_date, help="last date, inclusive")
    p.add_argument("--country", default="", help="restrict to one market")
    p.add_argument("--checks", default="all", help="comma list of rule ids, or 'all'")
    p.add_argument("--band-file", default="", help="CSV of known bands (spec 3.3)")
    p.add_argument("--pin-mins", type=int, default=5,
                   help="minimum pin minutes before the no-split family fires")
    p.add_argument("--approach-pct", type=float, default=1.0,
                   help="band proximity for LULD_APPROACH_BACKOFF")
    p.add_argument("--chase-ticks", type=int, default=2,
                   help="SS_HK_CHASE: ticks the ask must move")
    p.add_argument("--chase-secs", type=int, default=30,
                   help="SS_HK_CHASE: seconds without a reprice")
    p.add_argument("--out-dir", default="", help="also write report.xlsx here")
    p.add_argument("--diagnose", action="store_true",
                   help="first date only; distinct values and stage row counts")
    p.add_argument("--quiet", action="store_true", help="no per-date progress")
    p.add_argument("--self-test", action="store_true",
                   help="run built-in tests; needs no kdb")
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.self_test:
        return run_self_test()
    if not args.start or not args.end:
        raise SystemExit("--start and --end are required (or pass --self-test)")
    _check_servers()
    return run(args)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: `85/85 passed`

- [ ] **Step 5: Write the README**

Cover, in this order: what it does; the eight in-scope markets and why Indonesia is out; how to run it including setting the two server constants; every flag; **the band table — which market gets its band from where, and what `band_conf` means**; the rule table from §5 with one plain sentence each; the detector table from §6 with the guards on `LULD_FAVOURABLE_NO_SPLIT` spelled out; how to read the scorecard including that `unchecked` is not `OK`; the suppression footer; `--diagnose`; the acceptance test (`1370265478` / `600584.CH`, 16 July); and the judgement calls from §10 with the one-line change that reverses each. Link the spec.

- [ ] **Step 6: Commit**

```bash
git add scripts/luld_shortsell_check/
git commit -m "Wire the audit up to a command line, a workbook and a README"
```

---

## Task 13: Acceptance against the known case

**Files:**
- Modify: `scripts/luld_shortsell_check/luld_shortsell_check.py` (SELF TEST)

**Interfaces:**
- Consumes: `run_rules`, `Band`, `Split`

- [ ] **Step 1: Write the failing test**

The case from the notes, as a fixture. If the script does not flag this, it does not work.

```python
def test_acceptance_the_600584_case_from_the_notes():
    """1370265478 / 600584.CH, 16 July - split generated BELOW limit down.

    600584 is SSE main board, so +/-10%.  A previous close of 38.03 gives a
    limit down of 34.23; the split went out at 34.00.
    """
    d = dt.date(2026, 7, 16)
    base = 38.03
    band = compute_band(base, "CN", "600584.CH", d, 0.01)
    assert band is not None
    assert abs(band.dn - 34.23) < 0.02, f"limit down came out {band.dn}"
    sp = Split(id_target=1370265478, id_work=1, sym="600584.CH", country="CN",
               side="sell", sidesign=-1, otype="limit", price=34.00, size=1000,
               state="acked", t_transmit=0, parent_limit=0.0, tick=0.01,
               q_bid=34.10, q_ask=34.30, q_last=34.20, q_trdtick=0,
               t_bid=34.10, t_ask=34.30, t_last=34.20)
    findings = run_rules(sp, band, "qatt")
    caps = [f for f in findings if f.rule == "LULD_CAP"]
    assert caps, "the known bad split must be flagged"
    assert caps[0].severity == "violation"
    assert "below limit down" in caps[0].reason
```

- [ ] **Step 2: Run to verify it passes or fails honestly**

Run: `python scripts/luld_shortsell_check/luld_shortsell_check.py --self-test`
Expected: PASS. If the `34.23` assertion fails, the real previous close was not 38.03 — replace `base` with the true close for that date rather than loosening the tolerance, and say so in the commit.

- [ ] **Step 3: Commit**

```bash
git add scripts/luld_shortsell_check/luld_shortsell_check.py
git commit -m "Pin the case from the notes as the acceptance test"
```

---

## After the plan

Once every task is done, the remaining verification cannot happen here — it needs kdb:

1. **Set `ORDER_SERVER` and `QATT_SERVER`**, then `--diagnose` on one date. Confirm: the distinct `side` values include `sellshort`; the country-to-suffix crosstab is sane (China is `.CH`, not `.CN`); `target_oms` `limitup`/`limitdn` null rates are low for IN and KR and high elsewhere; and the `tsid` crosstab against symbol prefix either agrees with the board rule or names the stock that breaks it.
2. **Reconcile against `queries/limit_up_down/limit_up_down.q`** for one date — parent and split counts per market must agree over the same stocks.
3. **Run the acceptance case for real** — 16 July, `--country CN`, and check `1370265478` appears in the `LULD_CAP` sheet.
4. **Read the suppression footer before the findings.** A market reading clean because nothing was checkable looks identical to one reading clean because everything passed, and only the footer tells them apart.
