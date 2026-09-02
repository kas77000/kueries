#!/usr/bin/env python3
"""Every real-time field B-PIPE will serve us, as a CSV.

WHY THIS EXISTS.  The first probe found that PX_MIN_LIMIT, PX_MAX_LIMIT and
PX_LAST come back "Field not permitted to datafeed users", while MIN_LIMIT
and MAX_LIMIT answer immediately.  Those are not two names for one thing.
Bloomberg has two field families:

  static / reference   PX_LAST, PX_MAX_LIMIT, EQY_BETA, CUR_MKT_CAP, ID_ISIN
                       - what LimitUpDown.r and CreateTradingDataENT.r ask
                       for through R_bdp, and what our B-PIPE entitlement
                       does NOT carry
  real-time            LAST_PRICE, MIN_LIMIT, MAX_LIMIT, VOLUME_TDY
                       - what it does

So the useful question is never "what is the B-PIPE name for PX_FOO".  It is
"is there a real-time field that carries what PX_FOO carried", and the answer
is sometimes no.  This script prints the whole real-time family so that
question can be answered by reading rather than by guessing a mnemonic and
waiting for a subscription that never ticks.

HOW IT ASKS.  //blp/apiflds serves three requests; only one of them can
filter by family:

  FieldInfoRequest            given mnemonics, describe them.  Its 'ftype'
                              is a data category - Price, Character - and
                              says NOTHING about real-time vs static.  This
                              is what misled the first probe.
  FieldSearchRequest          search text, and filter include.fieldType to
                              RealTime or Static.  <- the one we want
  CategorizedFieldSearchRequest   the same, arranged by category

An empty searchSpec asks for everything.  Some servers cap that, so if the
answer looks too small this sweeps a=z 0-9 and merges, and says which it
did.  Fields are de-duplicated by mnemonic, never by Bloomberg's field id.

    python bpipe_fields.py                    every real-time field -> CSV
    python bpipe_fields.py --search limit     only ones matching "limit"
    python bpipe_fields.py --static           the static family instead
    python bpipe_fields.py --all              both, with a Family column
    python bpipe_fields.py --like "limit|cap" grep the result, print matches
    python bpipe_fields.py --self-test        parsing only, no Bloomberg

Connection settings come from bpipe_probe.py - fill them in there once, or
pass --host / --port / --app here.

THE TERMINAL DOES THIS TOO.  FLDS <GO> browses the same list by hand and is
the faster way to answer one question.  This script is for the other case:
having the whole list offline, diffable, and greppable while designing a job
around it.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import bpipe_probe

FIELD_TYPES = {"realtime": "RealTime", "static": "Static", "all": "All"}

CSV_COLUMNS = ["Mnemonic", "FieldId", "Family", "Category", "Datatype",
               "Description"]

#  An empty searchSpec should return thousands.  Fewer than this means the
#  server capped us and the sweep is worth doing.
SUSPICIOUSLY_FEW = 200

SWEEP_SEEDS = [c for c in "abcdefghijklmnopqrstuvwxyz0123456789"]


# =============================================================================
# PARSING  - testable with no Bloomberg
# =============================================================================

def field_row(field_id: str, info: dict, family: str) -> dict:
    """One //blp/apiflds fieldInfo -> one CSV row."""
    return {"Mnemonic": info.get("mnemonic") or field_id,
            "FieldId": field_id,
            "Family": family,
            "Category": info.get("ftype", ""),
            "Datatype": info.get("datatype", ""),
            "Description": info.get("description", "")}


def merge(rows) -> list:
    """De-duplicate on mnemonic, keeping the first, and sort.

    On mnemonic and not on FieldId: the sweep asks the same question 36
    times and a field answers to more than one seed, but the mnemonic is
    what anyone downstream actually types."""
    out = {}
    for row in rows:
        key = (row["Mnemonic"] or "").upper()
        if key and key not in out:
            out[key] = row
    return [out[k] for k in sorted(out)]


def looks_capped(rows, searched_everything: bool) -> bool:
    """Did an ask-for-everything come back too small to believe?"""
    return searched_everything and len(rows) < SUSPICIOUSLY_FEW


def matching(rows, pattern: str):
    """Rows whose mnemonic or description matches, case insensitively.  The
    pattern is a regex, so 'limit|cap' works."""
    if not pattern:
        return []
    rx = re.compile(pattern, re.IGNORECASE)
    return [r for r in rows
            if rx.search(r["Mnemonic"]) or rx.search(r["Description"])]


def write_csv(path, rows) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS,
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


# =============================================================================
# THE REQUEST
# =============================================================================

def search(session, identity, term: str, field_type: str) -> list:
    """One FieldSearchRequest.  field_type is RealTime, Static or All."""
    service = session.getService("//blp/apiflds")
    request = service.createRequest("FieldSearchRequest")
    request.set("searchSpec", term)
    if field_type != "All":
        try:
            request.getElement("include").setElement("fieldType", field_type)
        except Exception as e:                       # noqa: BLE001
            raise SystemExit(
                f"FAIL  this apiflds schema will not filter on fieldType "
                f"({type(e).__name__}: {e}). Re-run with --all and filter "
                f"the CSV by hand.")
    request.set("returnFieldDocumentation", False)

    rows = []
    for msg in bpipe_probe._send(session, request, identity):
        if msg.hasElement("responseError"):
            raise SystemExit(
                f"FAIL  responseError: {msg.getElement('responseError')}")
        if not msg.hasElement("fieldData"):
            continue
        data = msg.getElement("fieldData")
        for i in range(data.numValues()):
            entry = data.getValueAsElement(i)
            field_id = entry.getElementAsString("id")
            if entry.hasElement("fieldError"):
                continue
            if not entry.hasElement("fieldInfo"):
                continue
            info_element = entry.getElement("fieldInfo")
            info = {}
            for name in ("mnemonic", "description", "datatype", "ftype"):
                if info_element.hasElement(name):
                    info[name] = info_element.getElementAsString(name)
            rows.append(field_row(field_id, info, field_type))
    return rows


def collect(session, identity, term: str, field_type: str) -> list:
    """The search, plus the sweep when an ask-for-everything looks capped."""
    rows = search(session, identity, term, field_type)
    print(f"  searchSpec {term!r:<12} {len(rows)} field(s)")

    if not looks_capped(rows, searched_everything=not term):
        return merge(rows)

    print(f"  only {len(rows)} back for an empty search - the server is "
          f"capping it, sweeping a-z 0-9 instead")
    for seed in SWEEP_SEEDS:
        found = search(session, identity, seed, field_type)
        rows.extend(found)
        print(f"    {seed}  {len(found):>5}", end="\n" if seed == "9" else "")
    return merge(rows)


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="List the Bloomberg fields B-PIPE will serve us.")
    p.add_argument("--search", default="",
                   help="only fields matching this text; empty means all")
    p.add_argument("--static", action="store_true",
                   help="the static/reference family instead of real-time")
    p.add_argument("--all", action="store_true",
                   help="both families, with a Family column")
    p.add_argument("--like", default="",
                   help="regex; print matching rows to the screen as well")
    p.add_argument("--csv", default="",
                   help="where to write; default out/bpipe_<family>.csv")
    p.add_argument("--host", default=bpipe_probe.HOST)
    p.add_argument("--port", default=bpipe_probe.PORT)
    p.add_argument("--app", default=bpipe_probe.APP_NAME)
    p.add_argument("--no-auth", action="store_true")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()

    if a.all:
        family = "All"
    elif a.static:
        family = "Static"
    else:
        family = "RealTime"

    try:
        host, port, app = bpipe_probe.resolve_connection(
            a.host, a.port, a.app, a.no_auth)
    except bpipe_probe.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        print("      Settings live at the top of bpipe_probe.py and are "
              "shared by both scripts.", file=sys.stderr)
        return 2

    session, identity = bpipe_probe.start_session(host, port, app)
    try:
        if not session.openService("//blp/apiflds"):
            print("FAIL  could not open //blp/apiflds", file=sys.stderr)
            return 1
        print(f"\n--- //blp/apiflds : {family} fields ---")
        rows = collect(session, identity, a.search, family)
    finally:
        session.stop()

    if not rows:
        print("\nNothing came back. If --search was set, try a shorter term.")
        return 1

    out = a.csv or str(Path(__file__).resolve().parent / "out" /
                       f"bpipe_{family.lower()}_fields.csv")
    write_csv(out, rows)
    print(f"\n{len(rows)} field(s) -> {out}")

    by_category = {}
    for row in rows:
        by_category[row["Category"]] = by_category.get(row["Category"], 0) + 1
    print("\nby category:")
    for name, count in sorted(by_category.items(), key=lambda kv: -kv[1])[:15]:
        print(f"  {count:>6}  {name or '(none)'}")

    hits = matching(rows, a.like)
    if a.like:
        print(f"\nmatching {a.like!r}:")
        for row in hits:
            print(f"  {row['Mnemonic']:<34} {row['Datatype']:<10} "
                  f"{row['Description']}")
        if not hits:
            print("  nothing")
    return 0


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    print("bpipe_fields --self-test\n\nshaping a row")
    check("a full fieldInfo",
          field_row("PR005", {"mnemonic": "MIN_LIMIT",
                              "description": "Minimum Limit Price",
                              "datatype": "Double", "ftype": "Price"},
                    "RealTime"),
          {"Mnemonic": "MIN_LIMIT", "FieldId": "PR005", "Family": "RealTime",
           "Category": "Price", "Datatype": "Double",
           "Description": "Minimum Limit Price"})
    check("no mnemonic falls back to the field id, so a row is never blank",
          field_row("PR005", {}, "RealTime")["Mnemonic"], "PR005")

    print("\nmerging what the sweep returns")
    A = field_row("1", {"mnemonic": "MAX_LIMIT"}, "RealTime")
    B = field_row("2", {"mnemonic": "MIN_LIMIT"}, "RealTime")
    check("sorted by mnemonic",
          [r["Mnemonic"] for r in merge([A, B])], ["MAX_LIMIT", "MIN_LIMIT"])
    check("the same field found under several seeds appears once",
          len(merge([A, A, A])), 1)
    check("case does not make two fields out of one",
          len(merge([A, field_row("1", {"mnemonic": "max_limit"},
                                  "RealTime")])), 1)
    check("a row with no mnemonic and no id is dropped rather than written "
          "as an empty line",
          merge([{"Mnemonic": "", "Description": ""}]), [])

    print("\ndeciding whether the server capped us")
    many = [field_row(str(i), {"mnemonic": f"F{i}"}, "RealTime")
            for i in range(SUSPICIOUSLY_FEW + 1)]
    few = many[:10]
    check("a small answer to an ask-for-everything is suspicious",
          looks_capped(few, True), True)
    check("a big one is not", looks_capped(many, True), False)
    check("a small answer to a NARROW search is expected, not suspicious",
          looks_capped(few, False), False)

    print("\ngrepping the result")
    rows = [field_row("1", {"mnemonic": "MIN_LIMIT",
                            "description": "Minimum Limit Price"}, "RealTime"),
            field_row("2", {"mnemonic": "VOLUME_TDY",
                            "description": "Volume today"}, "RealTime"),
            field_row("3", {"mnemonic": "CUR_MKT_CAP",
                            "description": "Current market capitalisation"},
                      "Static")]
    check("matches the mnemonic",
          [r["Mnemonic"] for r in matching(rows, "MIN_")], ["MIN_LIMIT"])
    check("and the description, which is how you find a field whose name "
          "you cannot guess",
          [r["Mnemonic"] for r in matching(rows, "capitalisation")],
          ["CUR_MKT_CAP"])
    check("case insensitively",
          [r["Mnemonic"] for r in matching(rows, "volume")], ["VOLUME_TDY"])
    check("a regex alternation",
          [r["Mnemonic"] for r in matching(rows, "limit|volume")],
          ["MIN_LIMIT", "VOLUME_TDY"])
    check("no pattern matches nothing, rather than everything",
          matching(rows, ""), [])

    print("\nwriting the csv")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "fields.csv"
        write_csv(path, rows)
        text = path.read_text(encoding="utf-8")
        check("the header", text.splitlines()[0], ",".join(CSV_COLUMNS))
        check("a row per field", len(text.splitlines()), 4)
        check("unix line endings", "\r\n" in text, False)
        check("the directory was created", path.is_file(), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
