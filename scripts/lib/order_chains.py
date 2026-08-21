#!/usr/bin/env python3
"""
=============================================================================
order_chains.py

Putting a rejected-and-replaced order back together.

The engine writes a NEW id_target every time an order is re-sent, so counting
target rows counts one economic order several times and multiplies its size.
Seen live: Thailand read 3 orders / 81,000,000 / 0 executed, and it was ONE
27m order rejected and replaced twice.

  from lib.order_chains import CLIENT_ID_TAG, chain_key, chain_size, fix_tag

  cid = fix_tag(row["fixmsg"])                    # the client's own order id
  key = chain_key(date, cid, id_server, id_target)
  qty = chain_size(sizes, fills, "asked")         # what the chain asked for

WHAT IS HERE AND WHAT IS NOT.  The RULE is here - how to read the tag, what
makes two rows the same order, and what quantity that order asked for.  The
RECORDS are not: each report has its own idea of what an attempt carries and
what it wants to say about one, and forcing those into a shared shape would
buy nothing.

  python scripts/lib/order_chains.py --self-test
=============================================================================
"""

from __future__ import annotations

import sys

# =============================================================================
# THE TAG
#
# The client puts its own order id in FIX tag 9604, and a cancel-and-replace
# carries the SAME id - the client saying "this is still that order", which is
# a fact rather than an inference.
# =============================================================================

CLIENT_ID_TAG = "9604"

# THE SEPARATOR IS A SEMICOLON in this feed.  From a real fixmsg:
#
#   ...;16589=108223;9604=104642494_SG_HK_PORTAL_LIV_20260819162013;17717=...
#
# SOH and pipe are accepted too, since a stored copy may be rewritten either
# way and neither appears inside a value here.
#
# A CARET IS NOT A SEPARATOR, even though it looks like one.  It is used INSIDE
# values all over this feed - `SILK_FLOW^1008649713^TargetPart=30^SharedTempl^^`
# and `9012=274=1^275=1` are both one field - so splitting on it would carve
# values into pieces.  Nor is a space, for the same reason.
FIX_SEPS = "\x01;|\n\r"


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def fix_tag(fixmsg, tag=CLIENT_ID_TAG) -> str:
    """The value of one FIX tag in a fixmsg, or "" if it is not there.

    Split into fields first and compare the WHOLE tag, rather than searching for
    "9604=": that would also match 19604=, 96040= and a 9604= sitting inside
    another field's value, and a client id taken from the wrong tag is worse
    than no client id at all.
    """
    txt = _s(fixmsg)
    if not txt:
        return ""
    field = ""
    for ch in txt:
        if ch in FIX_SEPS:
            k, sep, val = field.partition("=")
            if sep and k.strip() == tag:
                return val.strip()
            field = ""
        else:
            field += ch
    k, sep, val = field.partition("=")
    return val.strip() if sep and k.strip() == tag else ""


# =============================================================================
# WHAT MAKES TWO ROWS ONE ORDER
# =============================================================================

def chain_key(date, client_id, id_server, id_target) -> tuple:
    """The key two target rows share when they are the same order.

    id_server is NOT in it - a trader can move an order to another order server
    mid-life and it is still the same order, which is exactly the case a server
    in the key would split back apart.

    A target with no 9604 keys on its own SERVER AND id_target instead, so it
    stands alone.  Grouping the un-tagged ones together would merge every
    unrelated order the client did not label, which is the one mistake here that
    would be invisible - and id_target alone is not unique across servers, hence
    both.
    """
    if not client_id:
        return (date, "", id_server, id_target)
    return (date, client_id)


# =============================================================================
# WHAT QUANTITY THE CHAIN ASKED FOR
#
# Executed is summed over EVERY attempt, so this decides what those fills are
# measured against - and the attempts are not all the same KIND of thing:
#
#   a REPLACEMENT supersedes the one before it.  Three sends of 27m that never
#     traded are one 27m order, not 81m.
#   a TOP UP is extra quantity on an order that already finished.  Sizes
#     900, 1700, 2500 filling 3,600 in total are 5,100 asked for, not 2,500.
#
# Both are real and they pull opposite ways, so no "take the Nth size" rule
# works.  "asked" reads it off the fills instead:
#
#     asked = (what every attempt filled) + (what the LAST one still had to do)
#
#                        sizes            fills        executed   asked
#   top ups        900, 1700, 2500   900, 1700, 1000      3,600   5,100
#   reject x3      27m, 27m, 27m           0, 0, 0            0     27m
#   remainder          100, 70             30, 70          100     100
#
# It cannot print over 100%: qty minus executed IS the last attempt's residual,
# which is never negative.
# =============================================================================

QTY_CHOICES = ("asked", "sum", "max", "first", "last")
DEFAULT_QTY = "asked"


def chain_size(sizes, fills, qty=DEFAULT_QTY) -> int:
    """The quantity a chain asked for.

    sizes and fills are per attempt, IN THE ORDER THEY WERE SENT.  fills is only
    read by "asked"; pass zeros and it degenerates to the last size, which is
    what it should be when nothing filled.
    """
    sizes = list(sizes)
    if not sizes:
        return 0
    fills = list(fills) if fills else [0] * len(sizes)
    if len(fills) != len(sizes):
        raise ValueError("sizes and fills must be the same length")
    if qty == "asked":
        #  a superseded attempt contributes only what it TRADED, so a
        #  replacement is not counted twice; a top up contributes its whole
        #  size, because it filled it
        return sum(fills) + max(0, sizes[-1] - fills[-1])
    if qty == "sum":
        return sum(sizes)
    if qty == "max":
        return max(sizes)
    if qty == "first":
        return sizes[0]
    if qty == "last":
        return sizes[-1]
    raise ValueError(f"qty must be one of {QTY_CHOICES}, not {qty!r}")


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

    print("order_chains --self-test\n\nreading tag 9604 out of fixmsg")
    SOH = "\x01"
    REAL = ("35=D;9012=274=1^275=1;16589=108223;"
            "9604=104642494_SG_HK_PORTAL_LIV_20260819162013;"
            "17717=7280001184;16500=system;40=1;16505=GAM.MK")
    check("a real message from this feed",
          fix_tag(REAL), "104642494_SG_HK_PORTAL_LIV_20260819162013")
    check("a caret inside a value does NOT split it",
          fix_tag(REAL, tag="9012"), "274=1^275=1")
    check("a caret-joined value keeps its carets",
          fix_tag("35=D;1008649713=SILK_FLOW^TargetPart=30^SharedTempl^^;59=0",
                  tag="1008649713"), "SILK_FLOW^TargetPart=30^SharedTempl^^")
    check("SOH separated", fix_tag(f"8=FIX.4.2{SOH}9604=ABC{SOH}59=0"), "ABC")
    check("pipe separated", fix_tag("8=FIX.4.2|9604=ABC|59=0"), "ABC")
    check("the tag at the very end, no trailing separator",
          fix_tag("35=D;9604=ABC"), "ABC")
    check("a value with a space survives", fix_tag("9604=A B;59=0"), "A B")
    check("a value with an = survives", fix_tag("9604=A=B;59=0"), "A=B")
    check("absent is empty", fix_tag("35=D;59=0"), "")
    check("present but empty is empty", fix_tag("9604=;59=0"), "")
    check("no fixmsg is empty", fix_tag(""), "")
    check("None is empty, not a crash", fix_tag(None), "")
    check("bytes work too", fix_tag(b"35=D;9604=ABC;59=0"), "ABC")
    check("19604 is not 9604", fix_tag("19604=WRONG;59=0"), "")
    check("96040 is not 9604", fix_tag("96040=WRONG;59=0"), "")
    check("9604 inside a VALUE is not 9604",
          fix_tag("58=see 9604=WRONG;59=0"), "")
    check("the right tag still wins beside a decoy",
          fix_tag("19604=WRONG;9604=RIGHT;59=0"), "RIGHT")

    print("\nwhat makes two rows one order")
    d = "2026-08-19"
    check("two sends with one client id are one order",
          chain_key(d, "CLI-1", 1, 100) == chain_key(d, "CLI-1", 1, 200), True)
    check("a trader moving the order server does not split it",
          chain_key(d, "CLI-1", 1, 100) == chain_key(d, "CLI-1", 7, 200), True)
    check("a different day IS a different order",
          chain_key("2026-08-20", "CLI-1", 1, 100)
          == chain_key(d, "CLI-1", 1, 100), False)
    check("two untagged rows never merge",
          chain_key(d, "", 1, 100) == chain_key(d, "", 1, 200), False)
    check("nor do they across servers",
          chain_key(d, "", 1, 100) == chain_key(d, "", 7, 100), False)
    check("an untagged row is not the same as a tagged one",
          chain_key(d, "", 1, 100) == chain_key(d, "CLI-1", 1, 100), False)

    print("\nwhat quantity the chain asked for")
    cases = (
        ("top ups", [900, 1700, 2500], [900, 1700, 1000], 3600,
         {"asked": 5100, "sum": 5100, "max": 2500, "first": 900, "last": 2500}),
        ("reject x3", [27_000_000] * 3, [0, 0, 0], 0,
         {"asked": 27_000_000, "sum": 81_000_000, "max": 27_000_000,
          "first": 27_000_000, "last": 27_000_000}),
        ("remainder", [100, 70], [30, 70], 100,
         {"asked": 100, "sum": 170, "max": 100, "first": 100, "last": 70}),
        #  a replace AFTER a full fill: the first attempt did all 100, then a
        #  second asks for 150 more.  asked reads that as 250 asked for, which
        #  is right IF a replace carries the remainder - the assumption the
        #  whole rule rests on.  If a replace instead restates the FULL order
        #  quantity, 150 would be right and asked understates completion here.
        #  Understating is the safe direction and the case is reported: the
        #  chain shows up under "attempts of differing size".
        ("grown", [100, 150], [100, 50], 150,
         {"asked": 250, "sum": 250, "max": 150, "first": 100, "last": 150}),
        ("one attempt", [500], [200], 200,
         {"asked": 500, "sum": 500, "max": 500, "first": 500, "last": 500}),
    )
    for name, sizes, fills, _ex, want in cases:
        for q, w in want.items():
            check(f"{name}, {q}", chain_size(sizes, fills, q), w)

    print("\nasked can never print over 100%")
    for name, sizes, fills, ex, _w in cases:
        q = chain_size(sizes, fills, "asked")
        check(f"{name}: executed {ex:,} <= asked {q:,}", ex <= q, True)
    check("because qty minus executed IS the last residual",
          chain_size([900, 1700, 2500], [900, 1700, 1000], "asked") - 3600,
          2500 - 1000)
    check("and only asked is safe in BOTH directions",
          [q for q in QTY_CHOICES
           if all(ex <= chain_size(s, f, q) for _n, s, f, ex, _w in cases)
           and chain_size([27_000_000] * 3, [0, 0, 0], q) == 27_000_000],
          ["asked"])
    check("sum never over-reports either, but doubles the rejected order",
          chain_size([27_000_000] * 3, [0, 0, 0], "sum"), 81_000_000)
    check("max keeps that one right and over-reports the top ups",
          (chain_size([27_000_000] * 3, [0, 0, 0], "max"),
           3600 > chain_size([900, 1700, 2500], [900, 1700, 1000], "max")),
          (27_000_000, True))

    print("\nedges")
    check("no attempts at all", chain_size([], [], "asked"), 0)
    check("no fills given falls back to the last size",
          chain_size([100, 70], None, "asked"), 70)
    check("the default is asked", DEFAULT_QTY, "asked")
    for bad, args in (("mismatched lengths", ([1, 2], [1])),
                      ("an unknown rule", ([1], [1]))):
        raised = False
        try:
            chain_size(*args, *(("nonsense",) if "unknown" in bad else ()))
        except ValueError:
            raised = True
        check(f"{bad} raises", raised, True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
