#!/usr/bin/env python3
"""Does B-PIPE give us back the trading day that just finished?

A probe, not a pipeline.  It answers one question - when a session closes,
can this entitlement fetch that session back out of Bloomberg - before
anyone plans work that assumes the answer is yes.

WHY ASK AT ALL.  The first probe found that this entitlement serves the
real-time field family and not the static one: PX_LAST came back "Field not
permitted to datafeed users" on the same request where LAST_PRICE answered.
History is the same kind of question one layer up.  A datafeed entitlement
is sold as a live feed; the right to replay yesterday is a separate line
item that we may or may not have bought.  Nothing in the field list tells
you which - the field list describes fields, not the services that carry
them.  Only asking tells you.

THREE REQUESTS, all on //blp/refdata, all for one name over one session:

  ticks    IntradayTickRequest     every TRADE, BID and ASK with a
                                   timestamp.  The literal question.
  bars     IntradayBarRequest      one minute OHLCV over the same window.
                                   Asked because an entitlement can carry
                                   bars while barring raw ticks, and "ticks
                                   no, bars yes" is a finding you only get
                                   by asking both.
  daily    HistoricalDataRequest   one daily row, PX_LAST and PX_VOLUME.
                                   Expected to fail: daily history IS the
                                   static family, the one already known to
                                   be barred.  It is in here so the output
                                   says so in Bloomberg's own words instead
                                   of by inference.

WHICH DAY, AND THE TIMEZONE TRAP.  Bloomberg wants startDateTime and
endDateTime in UTC.  "The day that just passed" is a fact about the
exchange's clock - not ours, and not UTC's.  So --tz-offset (default +9,
Tokyo, matching the default security) turns UTC now into exchange local now,
the last completed session is the previous weekday from that, and the window
is that local day 00:00 to 24:00 pushed back into UTC.  Both windows print
before each request, so a surprising answer is inspectable rather than
mysterious.

HOLIDAYS ARE NOT HANDLED, and that matters more than it sounds.  A Tokyo
public holiday returns exactly what no entitlement returns: nothing.  This
script cannot tell those two apart and does not pretend to - it names the
date it picked and says to confirm it was a trading day before believing an
empty result.  Pass --date to choose a session by hand.

THREE WAYS TO GET NOTHING, and they do not mean the same thing:

  not permitted   a fieldException, or text that says so   -> entitlement
  error           a responseError or securityError         -> wrong name,
                                                              window, or
                                                              service
  empty           a clean response carrying no rows        -> served, but
                                                              nothing there

Collapsing those three into "no data" is what would make this script
useless, so each prints differently and the summary keeps them apart.

    python bpipe_history.py                      7203 JT Equity, last session
    python bpipe_history.py "005930 KS Equity"   another name
    python bpipe_history.py --date 2026-09-02    a session by hand
    python bpipe_history.py --tz-offset 8        an exchange that is not Tokyo
    python bpipe_history.py --only ticks         one request instead of three
    python bpipe_history.py --self-test          dates and parsing, no Bloomberg

Connection settings come from bpipe_probe.py - fill them in there once, or
pass --host / --port / --app here.

Exit status is 0 only if at least one request actually returned rows, so
this is a check and not a wall of print.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

import bpipe_probe

#  What a tick is, as far as this probe cares.  TRADE answers "did it
#  trade"; BID and ASK are here because an entitlement can serve trades and
#  withhold quotes, which is worth knowing in the same run.
TICK_EVENT_TYPES = ["TRADE", "BID", "ASK"]

#  Daily history has no real-time spelling to fall back on.  These are the
#  static names on purpose - see the module docstring.
DAILY_FIELDS = ["PX_LAST", "PX_VOLUME"]

#  Tokyo, because the default security is Japanese.  An offset and not a
#  zone name: this probe reads one session, so the hours it is ahead of UTC
#  are the whole of what a timezone means here, and a zone database would be
#  a dependency bought for nothing.
DEFAULT_TZ_OFFSET = 9.0

BAR_INTERVAL_MINUTES = 1

#  How Bloomberg words a refusal.  Substrings, lowercased, because the
#  wording varies with the service while the failure is the same one.
BARRED_MARKERS = ("not permitted", "not authorized", "unauthorized",
                  "entitle", "permission")


# =============================================================================
# CHOOSING THE SESSION  - pure, and the part most likely to be wrong
# =============================================================================

def last_completed_session(now_utc: dt.datetime,
                           tz_offset_hours: float) -> dt.date:
    """The most recent weekday that has finished, on the exchange's clock.

    Not on ours.  At 23:00 UTC on a Sunday it is already Monday morning in
    Tokyo, and the day that just passed there is the previous Friday - a
    calculation that gets the wrong answer twice if it is done in the local
    timezone of whatever machine happens to run the probe.

    Weekends are skipped, holidays are not.  See the module docstring: an
    empty response on a holiday is indistinguishable from an empty response
    on no entitlement, so the honest thing is to name the date and let the
    reader check it."""
    local_now = now_utc + dt.timedelta(hours=tz_offset_hours)
    day = local_now.date() - dt.timedelta(days=1)
    while day.weekday() >= 5:                    # 5 Saturday, 6 Sunday
        day -= dt.timedelta(days=1)
    return day


def session_window(local_day: dt.date, tz_offset_hours: float) -> tuple:
    """One whole exchange-local day, as the naive UTC pair blpapi wants.

    Midnight to midnight rather than the trading hours, because the trading
    hours are a per-venue fact this probe deliberately does not know.  A day
    wide window costs nothing on a request the session bounds anyway, and it
    means the same code reads Tokyo and Manila."""
    offset = dt.timedelta(hours=tz_offset_hours)
    start_local = dt.datetime.combine(local_day, dt.time(0, 0))
    end_local = start_local + dt.timedelta(days=1)
    return start_local - offset, end_local - offset


# =============================================================================
# READING THE ANSWER  - also pure, also testable with no Bloomberg
# =============================================================================

def looks_barred(text: str) -> bool:
    """Is this complaint an entitlement refusal rather than a mistake?"""
    return any(marker in (text or "").lower() for marker in BARRED_MARKERS)


def classify(rows: int, errors: list) -> tuple:
    """(verdict, detail) - which of the four things happened.

    Rows beat complaints: a HistoricalDataRequest can bar one field and
    serve another on the same response, and a request that returned data did
    not fail.  With no rows, though, the reason is the entire finding, so a
    refusal is looked for ahead of the generic error."""
    if rows:
        return "ok", f"{rows} rows"
    barred = [e for e in errors if looks_barred(e)]
    if barred:
        return "not permitted", barred[0]
    if errors:
        return "error", errors[0]
    return "empty", "a clean response carrying no rows"


def summarise_ticks(ticks: list) -> dict:
    """Count, span and a tally per event type.

    The span is what says whether a session came back or a fragment of one:
    six hours of timestamps is Tokyo, four ticks at midnight is not."""
    times = sorted(t["time"] for t in ticks if t.get("time") is not None)
    types = {}
    for tick in ticks:
        kind = str(tick.get("type") or "?")
        types[kind] = types.get(kind, 0) + 1
    return {"count": len(ticks),
            "first": times[0] if times else None,
            "last": times[-1] if times else None,
            "types": dict(sorted(types.items()))}


def summarise_bars(bars: list) -> dict:
    """Count, span, the last close, and the volume across the session."""
    times = sorted(b["time"] for b in bars if b.get("time") is not None)
    volume = 0
    for bar in bars:
        parsed = bpipe_probe.to_decimal(bar.get("volume"))
        if parsed is not None:
            volume += parsed
    close = None
    if times:
        latest = max((b for b in bars if b.get("time") is not None),
                     key=lambda b: b["time"])
        close = bpipe_probe.to_decimal(latest.get("close"))
    return {"count": len(bars),
            "first": times[0] if times else None,
            "last": times[-1] if times else None,
            "volume": volume or None,
            "close": close}


def stamp(when) -> str:
    """A datetime as Bloomberg handed it over - UTC, and said to be."""
    if when is None:
        return "-"
    if isinstance(when, dt.datetime):
        return when.strftime("%Y-%m-%d %H:%M:%S") + "Z"
    return str(when)


def error_text(element) -> str:
    """The readable half of a Bloomberg error element."""
    if element.hasElement("message"):
        return element.getElementAsString("message")
    return " ".join(str(element).split())


def report(name: str, verdict: str, detail: str, errors: list) -> bool:
    """One probe's outcome, printed the same way every time.

    Complaints print even when rows came back, because a served response
    carrying a barred field is exactly the finding this script is for."""
    for problem in errors:
        label = "barred" if looks_barred(problem) else "error"
        print(f"  {label:<7} {problem}")
    print(f"  {'ok  ' if verdict == 'ok' else 'no  '}  {name}: "
          f"{verdict} - {detail}")
    return verdict == "ok"


# =============================================================================
# PROBE 1 - every tick of the session
# =============================================================================

def probe_ticks(session, identity, security, start_utc, end_utc,
                max_print) -> str:
    print(f"\n--- //blp/refdata IntradayTickRequest : {security} ---")
    service = session.getService("//blp/refdata")
    request = service.createRequest("IntradayTickRequest")
    request.set("security", security)
    for event in TICK_EVENT_TYPES:
        request.getElement("eventTypes").appendValue(event)
    request.set("startDateTime", start_utc)
    request.set("endDateTime", end_utc)
    request.set("includeConditionCodes", True)
    request.set("includeExchangeCodes", True)

    ticks, errors = [], []
    for msg in bpipe_probe._send(session, request, identity):
        if msg.hasElement("responseError"):
            errors.append(error_text(msg.getElement("responseError")))
            continue
        if not msg.hasElement("tickData"):
            continue
        #  The response nests tickData inside tickData.  That is not a typo
        #  here or in the schema: the outer element is the container that
        #  also carries responseError.
        outer = msg.getElement("tickData")
        if outer.hasElement("responseError"):
            errors.append(error_text(outer.getElement("responseError")))
            continue
        if not outer.hasElement("tickData"):
            continue
        array = outer.getElement("tickData")
        for i in range(array.numValues()):
            ticks.append(
                bpipe_probe.element_to_dict(array.getValueAsElement(i)))

    summary = summarise_ticks(ticks)
    for tick in ticks[:max_print]:
        print(f"  {stamp(tick.get('time'))}  "
              f"{str(tick.get('type') or '?'):<6} "
              f"{tick.get('value')} x {tick.get('size')}")
    if summary["count"] > max_print:
        print(f"  ... {summary['count'] - max_print} more")
    if summary["count"]:
        print(f"  {summary['count']} ticks, {stamp(summary['first'])} .. "
              f"{stamp(summary['last'])}")
        print("  " + ", ".join(f"{k} {v}"
                               for k, v in summary["types"].items()))

    verdict, detail = classify(summary["count"], errors)
    report("ticks", verdict, detail, errors)
    return verdict


# =============================================================================
# PROBE 2 - the same session as bars
# =============================================================================

def probe_bars(session, identity, security, start_utc, end_utc,
               interval) -> str:
    print(f"\n--- //blp/refdata IntradayBarRequest : {security}, "
          f"{interval}min ---")
    service = session.getService("//blp/refdata")
    request = service.createRequest("IntradayBarRequest")
    request.set("security", security)
    request.set("eventType", "TRADE")
    request.set("interval", interval)
    request.set("startDateTime", start_utc)
    request.set("endDateTime", end_utc)

    bars, errors = [], []
    for msg in bpipe_probe._send(session, request, identity):
        if msg.hasElement("responseError"):
            errors.append(error_text(msg.getElement("responseError")))
            continue
        if not msg.hasElement("barData"):
            continue
        outer = msg.getElement("barData")
        if outer.hasElement("responseError"):
            errors.append(error_text(outer.getElement("responseError")))
            continue
        if not outer.hasElement("barTickData"):
            continue
        array = outer.getElement("barTickData")
        for i in range(array.numValues()):
            bars.append(
                bpipe_probe.element_to_dict(array.getValueAsElement(i)))

    summary = summarise_bars(bars)
    if summary["count"]:
        print(f"  {summary['count']} bars, {stamp(summary['first'])} .. "
              f"{stamp(summary['last'])}")
        print(f"  last close {summary['close']}, session volume "
              f"{summary['volume']}")

    verdict, detail = classify(summary["count"], errors)
    report("bars", verdict, detail, errors)
    return verdict


# =============================================================================
# PROBE 3 - the one that is expected to be refused
# =============================================================================

def probe_daily(session, identity, security, day) -> str:
    print(f"\n--- //blp/refdata HistoricalDataRequest : {security}, "
          f"{day.isoformat()} ---")
    service = session.getService("//blp/refdata")
    request = service.createRequest("HistoricalDataRequest")
    request.getElement("securities").appendValue(security)
    for field in DAILY_FIELDS:
        request.getElement("fields").appendValue(field)
    marker = day.strftime("%Y%m%d")
    request.set("startDate", marker)
    request.set("endDate", marker)
    request.set("periodicitySelection", "DAILY")

    rows, errors = [], []
    for msg in bpipe_probe._send(session, request, identity):
        if msg.hasElement("responseError"):
            errors.append(error_text(msg.getElement("responseError")))
            continue
        if not msg.hasElement("securityData"):
            continue
        #  Unlike a ReferenceDataRequest, this one carries a single
        #  securityData element rather than an array of them.
        data = msg.getElement("securityData")
        if data.hasElement("securityError"):
            errors.append(error_text(data.getElement("securityError")))
        if data.hasElement("fieldExceptions"):
            exceptions = data.getElement("fieldExceptions")
            for i in range(exceptions.numValues()):
                item = exceptions.getValueAsElement(i)
                errors.append(f"{item.getElementAsString('fieldId')}: "
                              f"{error_text(item.getElement('errorInfo'))}")
        if data.hasElement("fieldData"):
            array = data.getElement("fieldData")
            for i in range(array.numValues()):
                rows.append(
                    bpipe_probe.element_to_dict(array.getValueAsElement(i)))

    for row in rows:
        print("  " + ", ".join(f"{k} {v}" for k, v in sorted(row.items())))

    verdict, detail = classify(len(rows), errors)
    report("daily", verdict, detail, errors)
    return verdict


# =============================================================================
# MAIN
# =============================================================================

def resolve_day(chosen: str, tz_offset: float) -> dt.date:
    """--date if it was given, otherwise the last weekday to have finished
    on the exchange's clock."""
    if not chosen:
        return last_completed_session(dt.datetime.utcnow(), tz_offset)
    try:
        return dt.date.fromisoformat(chosen)
    except ValueError:
        raise bpipe_probe.SettingError(
            f"--date {chosen!r} is not a date; write it as YYYY-MM-DD")


def verdict_note(verdicts: dict) -> str:
    """What the run means, said once, so the reader does not have to hold
    three outcomes in their head to reach a conclusion."""
    values = set(verdicts.values())
    if "ok" in values:
        served = [name for name, v in verdicts.items() if v == "ok"]
        return (f"  This entitlement replays a finished session: "
                f"{', '.join(served)} answered.")
    if "not permitted" in values:
        return ("  Refused, not empty. This is an entitlement question, and\n"
                "  no change to the request will fix it.")
    if values == {"empty"}:
        return ("  Served, but nothing came back. Before reading that as no\n"
                "  history, confirm the date above was a trading day on that\n"
                "  exchange and that --tz-offset matches it. A holiday looks\n"
                "  exactly like this.")
    return ("  Errors rather than refusals - check the security and the\n"
            "  window before concluding anything about entitlement.")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Can B-PIPE give us back the session that just finished?")
    p.add_argument("security", nargs="?", default=bpipe_probe.SECURITY,
                   help=f'default "{bpipe_probe.SECURITY}"')
    p.add_argument("--host", default=bpipe_probe.HOST, help="B-PIPE host")
    p.add_argument("--port", default=bpipe_probe.PORT, help="B-PIPE port")
    p.add_argument("--app", default=bpipe_probe.APP_NAME,
                   help="application name")
    p.add_argument("--no-auth", action="store_true",
                   help="skip authorization entirely - a local Terminal only")
    p.add_argument("--date", default="",
                   help="the session to ask for, YYYY-MM-DD in exchange local "
                        "time; default is the last completed weekday")
    p.add_argument("--tz-offset", type=float, default=DEFAULT_TZ_OFFSET,
                   help=f"hours the exchange is ahead of UTC (default "
                        f"{DEFAULT_TZ_OFFSET:+g}, Tokyo)")
    p.add_argument("--interval", type=int, default=BAR_INTERVAL_MINUTES,
                   help="bar size in minutes")
    p.add_argument("--max-ticks", type=int, default=10,
                   help="how many ticks to print before summarising")
    p.add_argument("--only", choices=("ticks", "bars", "daily"),
                   help="run a single request instead of all three")
    p.add_argument("--self-test", action="store_true",
                   help="check the dates and the parsing, with no Bloomberg")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()

    try:
        host, port, app = bpipe_probe.resolve_connection(
            a.host, a.port, a.app, a.no_auth)
        day = resolve_day(a.date, a.tz_offset)
    except bpipe_probe.SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        if any(name in str(e) for name in ("HOST", "PORT", "APP_NAME")):
            print("      Settings live at the top of bpipe_probe.py and are "
                  "shared by every probe in this folder.", file=sys.stderr)
        return 2

    start_utc, end_utc = session_window(day, a.tz_offset)
    print(f"session   {day.isoformat()} ({day.strftime('%A')}) on the "
          f"exchange's clock, UTC{a.tz_offset:+g}")
    print(f"window    {stamp(start_utc)} .. {stamp(end_utc)}")
    print("note      weekends are skipped, holidays are not - confirm that "
          "date traded")

    session, identity = bpipe_probe.start_session(host, port, app)
    try:
        if not session.openService("//blp/refdata"):
            print("FAIL  could not open //blp/refdata")
            return 1
        verdicts = {}
        if a.only in (None, "ticks"):
            verdicts["ticks"] = probe_ticks(session, identity, a.security,
                                            start_utc, end_utc, a.max_ticks)
        if a.only in (None, "bars"):
            verdicts["bars"] = probe_bars(session, identity, a.security,
                                          start_utc, end_utc, a.interval)
        if a.only in (None, "daily"):
            verdicts["daily"] = probe_daily(session, identity, a.security, day)
    finally:
        session.stop()

    print("\n--- result ---")
    for name, verdict in verdicts.items():
        print(f"  {'ok  ' if verdict == 'ok' else 'no  '}  {name:<6} "
              f"{verdict}")
    print(verdict_note(verdicts))

    return 0 if "ok" in verdicts.values() else 1


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

    D = dt.date
    T = dt.datetime

    print("bpipe_history --self-test\n\nchoosing the session that just passed")
    check("a weekday afternoon: yesterday",
          last_completed_session(T(2026, 9, 3, 12, 0), 0), D(2026, 9, 2))
    check("Monday: the previous Friday, not Sunday",
          last_completed_session(T(2026, 9, 7, 1, 0), 0), D(2026, 9, 4))
    check("Sunday: Friday",
          last_completed_session(T(2026, 9, 6, 12, 0), 0), D(2026, 9, 4))
    check("Saturday: Friday",
          last_completed_session(T(2026, 9, 5, 12, 0), 0), D(2026, 9, 4))
    check("Sunday evening UTC is Monday morning in Tokyo, so the day that "
          "just passed there is Friday - the case a naive yesterday gets "
          "wrong",
          last_completed_session(T(2026, 9, 6, 23, 0), 9), D(2026, 9, 4))
    check("and Monday morning UTC is still Sunday in New York, which also "
          "lands on Friday",
          last_completed_session(T(2026, 9, 7, 2, 0), -5), D(2026, 9, 4))
    check("Tokyo just past midnight local, which is mid-afternoon UTC the "
          "day before",
          last_completed_session(T(2026, 9, 2, 15, 30), 9), D(2026, 9, 2))

    print("\nturning that day into the UTC pair blpapi wants")
    check("Tokyo is nine hours ahead, so its day starts the afternoon before",
          session_window(D(2026, 9, 4), 9),
          (T(2026, 9, 3, 15, 0), T(2026, 9, 4, 15, 0)))
    check("at UTC the window is just the calendar day",
          session_window(D(2026, 9, 4), 0),
          (T(2026, 9, 4, 0, 0), T(2026, 9, 5, 0, 0)))
    check("behind UTC, the window starts later the same day",
          session_window(D(2026, 9, 4), -5),
          (T(2026, 9, 4, 5, 0), T(2026, 9, 5, 5, 0)))
    check("a half hour offset is an offset like any other",
          session_window(D(2026, 9, 4), 5.5),
          (T(2026, 9, 3, 18, 30), T(2026, 9, 4, 18, 30)))
    start, end = session_window(D(2026, 9, 4), 9)
    check("the window is exactly one day wide", end - start,
          dt.timedelta(days=1))

    print("\ntelling a refusal from a mistake")
    check("Bloomberg's own wording, the one this entitlement already returns "
          "for PX_LAST",
          looks_barred("Field not permitted to datafeed users"), True)
    check("an entitlement complaint by another name",
          looks_barred("User is not entitled for this data"), True)
    check("and another", looks_barred("Not authorized"), True)
    check("a bad security is not an entitlement problem",
          looks_barred("Unknown/Invalid Security"), False)
    check("nor is a bad window", looks_barred("Invalid start date"), False)
    check("no complaint at all", looks_barred(""), False)
    check("nor None", looks_barred(None), False)

    print("\nthe four things that can happen")
    check("rows came back", classify(18442, []), ("ok", "18442 rows"))
    check("rows beat complaints - a request that returned data did not fail, "
          "and one barred field on a served response is normal",
          classify(1, ["PX_LAST: Field not permitted to datafeed users"]),
          ("ok", "1 rows"))
    check("no rows and a refusal is the entitlement answer",
          classify(0, ["Field not permitted to datafeed users"]),
          ("not permitted", "Field not permitted to datafeed users"))
    check("a refusal is found even behind an unrelated complaint",
          classify(0, ["Invalid start date",
                       "Field not permitted to datafeed users"]),
          ("not permitted", "Field not permitted to datafeed users"))
    check("no rows and an ordinary error is a mistake we made",
          classify(0, ["Unknown/Invalid Security"]),
          ("error", "Unknown/Invalid Security"))
    check("no rows and no complaint is the ambiguous one - served, empty, "
          "and possibly just a holiday",
          classify(0, []), ("empty", "a clean response carrying no rows"))

    print("\nsummarising ticks")
    ticks = [{"time": T(2026, 9, 4, 0, 5), "type": "TRADE", "value": 2500.0,
              "size": 100},
             {"time": T(2026, 9, 4, 6, 0), "type": "BID", "value": 2499.0,
              "size": 200},
             {"time": T(2026, 9, 4, 2, 0), "type": "TRADE", "value": 2510.0,
              "size": 300}]
    check("counted", summarise_ticks(ticks)["count"], 3)
    check("the span is the earliest and the latest, not the first and the "
          "last in the list",
          (summarise_ticks(ticks)["first"], summarise_ticks(ticks)["last"]),
          (T(2026, 9, 4, 0, 5), T(2026, 9, 4, 6, 0)))
    check("tallied by event type, so trades served without quotes shows up",
          summarise_ticks(ticks)["types"], {"BID": 1, "TRADE": 2})
    check("nothing at all summarises to nothing, without raising",
          summarise_ticks([]),
          {"count": 0, "first": None, "last": None, "types": {}})
    check("a tick with no timestamp still counts, and does not break the span",
          summarise_ticks([{"type": "TRADE"}]),
          {"count": 1, "first": None, "last": None, "types": {"TRADE": 1}})

    print("\nsummarising bars")
    bars = [{"time": T(2026, 9, 4, 0, 0), "close": 2500.0, "volume": 1000},
            {"time": T(2026, 9, 4, 6, 0), "close": 2530.0, "volume": 2000},
            {"time": T(2026, 9, 4, 3, 0), "close": 2515.0, "volume": 500}]
    summary = summarise_bars(bars)
    check("counted", summary["count"], 3)
    check("volume adds up across the session", summary["volume"], 3500)
    check("the close is the latest bar's, by time and not by position",
          summary["close"], bpipe_probe.to_decimal(2530.0))
    check("no bars is not an error", summarise_bars([]),
          {"count": 0, "first": None, "last": None,
           "volume": None, "close": None})

    print("\nprinting a Bloomberg timestamp")
    check("said to be UTC, because it is and the reader cannot tell",
          stamp(T(2026, 9, 4, 6, 30, 15)), "2026-09-04 06:30:15Z")
    check("nothing prints as a dash, not as None", stamp(None), "-")

    print("\nresolving --date")
    check("given, it wins over the calculation",
          resolve_day("2026-09-01", 9), D(2026, 9, 1))
    check("a weekend can be asked for by hand - skipping one is a default, "
          "not a rule",
          resolve_day("2026-09-05", 9), D(2026, 9, 5))

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            got = repr(fn())
        except bpipe_probe.SettingError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                 f"{fragment!r}"))

    raises("a date that is not one is refused by shape, before connecting",
           lambda: resolve_day("last tuesday", 9), "YYYY-MM-DD")

    print("\nwhat the run means")
    check("one served request is enough to answer yes",
          "replays a finished session" in verdict_note(
              {"ticks": "ok", "bars": "empty", "daily": "not permitted"}),
          True)
    check("a refusal with nothing served is the entitlement answer",
          "entitlement question" in verdict_note(
              {"ticks": "not permitted", "daily": "empty"}),
          True)
    check("all empty points at the holiday it might be rather than "
          "concluding",
          "holiday" in verdict_note({"ticks": "empty", "bars": "empty"}),
          True)
    check("errors alone conclude nothing about entitlement",
          "check the security" in verdict_note({"ticks": "error"}), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
