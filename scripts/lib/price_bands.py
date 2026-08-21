"""Daily price limit bands, and whether an order could ever have traded.

WHY THIS EXISTS
---------------
A short-sell order priced far away from the market is not a fill we missed -
it is an order that was never going to trade.  Counting it drags completion
down and hides the orders we could actually have done something about.  So the
report drops orders that are priced OUTSIDE the day's limit band.

Every market in scope except Hong Kong caps how far a stock may move from the
previous close in one session, and an order priced beyond that cap cannot
execute, full stop.  The previous close is already on the order - target_stock
carries adjclose / orgclose - so nothing here needs a quote server.

WHAT "COULD NEVER TRADE" MEANS, EXACTLY
---------------------------------------
It is DIRECTIONAL.  A SELL limit above the ceiling can never be hit, because
nothing may print that high.  A SELL limit below the floor is fine - it just
trades at whatever the market is.  Buys are the mirror.  Judging both sides the
same way would throw away marketable orders.

THE NUMBERS BELOW ARE A RULE, NOT A FEED
----------------------------------------
Exchanges change these.  They are set out as data, one entry per market, so a
desk that knows better can correct them in one place - and BANDS is the only
thing to correct.  A market that is absent, or mapped to None, is never judged:
its orders all count, which is the safe direction to be wrong in.

Reference prices are LOCAL currency, matching limit_price - there is no fx
anywhere in this file.
"""

from typing import Optional

#  Tokyo caps the move in absolute yen, off a step table keyed on the previous
#  close, rather than as a percentage.  (upper bound EXCLUSIVE, +/- limit).
#  The mantissa walks 1, 1.5, 2, 3, 5, 7 per decade and the limit follows it;
#  the table is written out rather than generated so it can be read against the
#  exchange's own page line by line.
JP_STEPS = (
    (100,          30),
    (200,          50),
    (500,          80),
    (700,         100),
    (1_000,       150),
    (1_500,       300),
    (2_000,       400),
    (3_000,       500),
    (5_000,       700),
    (7_000,     1_000),
    (10_000,    1_500),
    (15_000,    3_000),
    (20_000,    4_000),
    (30_000,    5_000),
    (50_000,    7_000),
    (70_000,   10_000),
    (100_000,  15_000),
    (150_000,  30_000),
    (200_000,  40_000),
    (300_000,  50_000),
    (500_000,  70_000),
    (700_000, 100_000),
    (1_000_000,   150_000),
    (1_500_000,   300_000),
    (2_000_000,   400_000),
    (3_000_000,   500_000),
    (5_000_000,   700_000),
    (7_000_000, 1_000_000),
    (10_000_000, 1_500_000),
    (15_000_000, 3_000_000),
    (20_000_000, 4_000_000),
    (30_000_000, 5_000_000),
    (50_000_000, 7_000_000),
)
JP_TOP = 10_000_000       # 50m yen and up

#  code -> rule.  ("pct", f) is +/- f of the previous close; ("steps", table)
#  is Tokyo's; None is "this market does not cap the daily move".
BANDS = {
    "HK": None,                  # no daily price limit in continuous trading
    "JP": ("steps", JP_STEPS),
    "KR": ("pct", 0.30),         # KOSPI / KOSDAQ, +/-30%
    "MY": ("pct", 0.30),         # Bursa static limit, +/-30%
    "TW": ("pct", 0.10),         # TWSE / TPEx, +/-10%
    "TH": ("pct", 0.30),         # SET ceiling / floor, +/-30%
}

#  Exchanges round the band to a tick and we do not have the tick tables, so a
#  price sitting a hair outside a computed edge is treated as inside.  An order
#  that is genuinely off limit misses by far more than this.
TOL = 0.0025                     # of the reference price


def jp_limit(prev_close: float) -> float:
    """Tokyo's +/- limit in yen for a stock that closed at prev_close."""
    for bound, lim in JP_STEPS:
        if prev_close < bound:
            return float(lim)
    return float(JP_TOP)


def band(code: str, prev_close: float) -> Optional[tuple]:
    """(floor, ceiling) in local currency, or None if it cannot be said.

    None means one of: the market has no daily limit, the market is not in the
    table, or we have no previous close.  All three are "do not judge".
    """
    rule = BANDS.get(code)
    if rule is None or not prev_close or prev_close <= 0:
        return None
    kind, arg = rule
    lim = prev_close * arg if kind == "pct" else jp_limit(prev_close)
    return max(0.0, prev_close - lim), prev_close + lim


def marketable(code: str, prev_close: float, limit_price: float,
               sidesign: int) -> Optional[bool]:
    """Could this order ever have traded?  True / False / None for unknown.

    - no limit price at all: a market order always could.  True.
    - no band: nothing to judge it against.  None.
    - a SELL is dead only above the ceiling, a BUY only below the floor.
    """
    if not limit_price or limit_price <= 0:
        return True
    b = band(code, prev_close)
    if b is None:
        return None
    floor, ceiling = b
    slack = prev_close * TOL
    if sidesign < 0:
        return limit_price <= ceiling + slack
    return limit_price >= floor - slack


def describe(code: str) -> str:
    """The rule for one market, for a footnote or the terminal."""
    rule = BANDS.get(code)
    if rule is None:
        return "no daily limit" if code in BANDS else "no rule on file"
    kind, arg = rule
    if kind == "pct":
        return f"+/-{arg * 100:g}% of the previous close"
    return "the exchange step table on the previous close"


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("price_bands --self-test\n\nthe percentage markets")
    check("Taiwan is a tenth either way", band("TW", 100.0), (90.0, 110.0))
    check("Korea is thirty percent", band("KR", 10_000.0),
          (7_000.0, 13_000.0))
    check("Thailand too", band("TH", 10.0), (7.0, 13.0))
    check("and Malaysia", band("MY", 2.0), (1.4, 2.6))
    check("Hong Kong does not cap the day at all", band("HK", 50.0), None)
    check("nor does a market nobody wrote a rule for",
          band("XX", 50.0), None)
    check("no previous close, no band", band("TW", 0.0), None)
    check("a cheap Thai name floors at 70%", band("TH", 1.0), (0.7, 1.3))
    check("no rule on file can push a floor below zero - the clamp is there "
          "for a future band wider than 100%",
          [band(c, px)[0] >= 0 for c in BANDS if BANDS[c]
           for px in (0.01, 1.0, 1e6)], [True] * 15)

    print("\nTokyo's step table")
    check("a penny stock moves 30 yen", jp_limit(80.0), 30.0)
    check("the boundary belongs to the step ABOVE it - 100 is not under 100",
          jp_limit(100.0), 50.0)
    check("just under is the step below", jp_limit(99.99), 30.0)
    check("a 1234 yen stock moves 300", jp_limit(1234.0), 300.0)
    check("and its band", band("JP", 1234.0), (934.0, 1534.0))
    check("3000 yen moves 700", jp_limit(3_000.0), 700.0)
    check("a 40k yen stock moves 7000", jp_limit(40_000.0), 7_000.0)
    check("above the last row it is flat", jp_limit(90_000_000.0), 10_000_000.0)
    check("exactly at the last bound, too", jp_limit(50_000_000.0),
          10_000_000.0)
    check("the table only ever goes up",
          [lim for _, lim in JP_STEPS] == sorted(lim for _, lim in JP_STEPS),
          True)
    check("and its bounds only ever go up",
          [b for b, _ in JP_STEPS] == sorted(b for b, _ in JP_STEPS), True)
    check("no bound is repeated", len({b for b, _ in JP_STEPS}),
          len(JP_STEPS))

    print("\ncould this order ever have traded")
    SELL, BUY = -1, 1
    check("a market order always could - there is no price to be wrong",
          marketable("TW", 100.0, 0.0, SELL), True)
    check("even where we know no band",
          marketable("HK", 0.0, 0.0, SELL), True)
    check("a sell at the last price, obviously",
          marketable("TW", 100.0, 100.0, SELL), True)
    check("a sell ABOVE limit up never trades",
          marketable("TW", 100.0, 120.0, SELL), False)
    check("a sell BELOW limit down trades fine - it is not off limit, it is "
          "just cheap", marketable("TW", 100.0, 50.0, SELL), True)
    check("a buy below limit down never trades",
          marketable("TW", 100.0, 50.0, BUY), False)
    check("a buy above limit up trades fine",
          marketable("TW", 100.0, 120.0, BUY), True)
    check("Hong Kong can never be judged, so it never is",
          marketable("HK", 100.0, 1e9, SELL), None)
    check("nor can a name with no previous close",
          marketable("TW", 0.0, 1e9, SELL), None)

    print("\nthe tick tolerance")
    check("right at the ceiling counts", marketable("TW", 100.0, 110.0, SELL),
          True)
    check("a rounding tick past it still counts",
          marketable("TW", 100.0, 110.2, SELL), True)
    check("a quarter of a percent past is the edge of forgiveness",
          marketable("TW", 100.0, 110.25, SELL), True)
    check("beyond that it does not", marketable("TW", 100.0, 110.30, SELL),
          False)
    check("the tolerance is far too small to rescue a real off-limit price",
          marketable("TW", 100.0, 111.0, SELL), False)

    print("\nsaying the rule out loud")
    check("Taiwan", describe("TW"), "+/-10% of the previous close")
    check("Korea", describe("KR"), "+/-30% of the previous close")
    check("Japan", describe("JP"),
          "the exchange step table on the previous close")
    check("Hong Kong", describe("HK"), "no daily limit")
    check("a market nobody wrote down", describe("XX"), "no rule on file")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
