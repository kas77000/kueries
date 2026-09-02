#!/usr/bin/env python3
"""Can B-PIPE tell us a Japanese stock's daily price limits?

A probe, not a pipeline.  It answers one question - does B-PIPE serve
MIN_LIMIT and MAX_LIMIT for a name like 7203 JT Equity, and are they the
same numbers the R job gets from PX_MAX_LIMIT / PX_MIN_LIMIT - before
anyone writes a second LimitUpDown around the answer.

CONNECTION.  Fill in HOST, PORT and APP_NAME below on the machine that has
B-PIPE, or pass --host / --port / --app.  They ship empty on purpose: an
address and an application name are the two things in this file that are
worth nothing to anyone but us and something to everyone else.

Each one is a field in the Bloomberg API Demo Tool, with API Product set to
BPipe and Authorization Type set to Application:

    HOST       <- Host Addresses
    PORT       <- Port
    APP_NAME   <- Application Name

The tool's "Simplify Authentication" tick hides a two step handshake that
this script has to do in the open: generate a token, then send an
AuthorizationRequest to //blp/apiauth and keep the Identity it fills in.
Every later request and subscription is made AS that identity.  Skip it and
B-PIPE answers with an entitlement failure rather than a price.

THREE PROBES, in increasing order of what they assume:

  fieldinfo   //blp/apiflds    Are MIN_LIMIT and MAX_LIMIT real fields, and
                               are they real-TIME fields?  This is the whole
                               risk of the idea, answered in one request.
  snapshot    //blp/refdata    The limits now, next to PX_MIN_LIMIT and
                               PX_MAX_LIMIT - the fields LimitUpDown.r uses.
                               Side by side is how we learn whether the two
                               agree.  Works out of hours; a subscription
                               does not.
  stream      //blp/mktdata    The real-time subscription, every tick
                               printed with a timestamp until both limits
                               arrive or the clock runs out.

WHY THE SNAPSHOT MATTERS AS MUCH AS THE STREAM.  LimitUpDown runs between
07:30 and 09:03 Hong Kong time, which is 08:30 to 10:03 in Tokyo.  Whether
the limits are already on the wire at that hour is the question that
decides if a subscription is usable at all, so the stream prints the clock
time each value lands.  The snapshot is the control: it proves the number
exists even when nothing is ticking.

    python bpipe_probe.py                          7203 JT Equity, all three
    python bpipe_probe.py "9984 JT Equity"         another name
    python bpipe_probe.py --only fieldinfo         just the field question
    python bpipe_probe.py --seconds 120            wait longer on the stream
    python bpipe_probe.py --self-test              parsing only, no Bloomberg

Exit status is 0 only if the limits actually came back, so this is a check
and not a wall of print.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from decimal import Decimal, InvalidOperation

#  Fill these in on the target machine, or pass --host / --port / --app.
#  Empty is not a default that quietly connects somewhere - it is refused
#  with a message naming the setting, the same way local_settings.py is
#  strict about a name it does not recognise.
HOST = ""
PORT = ""
APP_NAME = ""

SECURITY = "7203 JT Equity"          # Toyota, on the Tokyo Stock Exchange

#  What we came for.
RT_FIELDS = ["MIN_LIMIT", "MAX_LIMIT"]

#  The snapshot asks for more: the same two, the two LimitUpDown.r uses, and
#  a last price to sanity check that the limits bracket it.
REF_FIELDS = ["MIN_LIMIT", "MAX_LIMIT",
              "PX_MIN_LIMIT", "PX_MAX_LIMIT",
              "PX_LAST", "CRNCY", "NAME"]

AUTH_TEMPLATE = ("AuthenticationMode=APPLICATION_ONLY;"
                 "ApplicationAuthenticationType=APPNAME_AND_KEY;"
                 "ApplicationName={app}")

TIMEOUT_MS = 30_000


def _blpapi():
    """Imported here, never at module level, so --self-test runs on a machine
    with no Bloomberg at all."""
    try:
        import blpapi
    except ImportError:
        raise SystemExit(
            "blpapi is not installed.\n"
            "    pip install --index-url="
            "https://blpapi.bloomberg.com/repository/releases/python/simple/"
            " blpapi")
    return blpapi


def _now() -> str:
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class SettingError(Exception):
    pass


def resolve_connection(host, port, app, no_auth: bool):
    """Command line first, then the constants at the top of this file, then
    a refusal naming what is missing.

    Nothing here defaults to a working value.  A probe that quietly connects
    somewhere other than where you meant is worse than one that will not
    start, because you will believe its answer."""
    host = (host or "").strip()
    port = str(port or "").strip()
    app = (app or "").strip()

    if not host:
        raise SettingError("no host: set HOST at the top of bpipe_probe.py, "
                           "or pass --host")
    if not port:
        raise SettingError("no port: set PORT at the top of bpipe_probe.py, "
                           "or pass --port")
    try:
        port = int(port)
    except ValueError:
        raise SettingError(f"port {port!r} is not a number")
    if not 1 <= port <= 65535:
        raise SettingError(f"port {port} is not a port number")

    if no_auth:
        #  Deliberate, and only sane against a local Terminal.
        return host, port, ""
    if not app:
        raise SettingError(
            "no application name: set APP_NAME at the top of bpipe_probe.py, "
            "or pass --app. B-PIPE will refuse an unauthenticated session, so "
            "this is a hard error rather than a silent attempt. Use --no-auth "
            "only against a local Terminal.")
    return host, port, app


# =============================================================================
# PARSING  - the part that can be tested without Bloomberg
# =============================================================================

def element_to_dict(element) -> dict:
    """A blpapi Element of named sub-elements -> {name: value}.

    Both a refdata fieldData block and a subscription tick have this shape,
    which is why one function reads both."""
    out = {}
    for i in range(element.numElements()):
        child = element.getElement(i)
        try:
            out[str(child.name())] = child.getValue()
        except Exception:                            # noqa: BLE001
            #  A field with no value in this tick.  Present, but empty.
            out[str(child.name())] = None
    return out


def to_decimal(value):
    """Anything that is not a positive finite number is not a price.  Same
    rule as kdbsource._to_decimal, and for the same reason: a limit of 0 is
    not a limit, it is a missing value wearing a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except UnicodeDecodeError:
            return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


def band_is_sane(low, high, last=None):
    """What the ATS needs to be true of any limit pair we would publish."""
    problems = []
    if low is None:
        problems.append("no MIN_LIMIT")
    if high is None:
        problems.append("no MAX_LIMIT")
    if low is not None and high is not None:
        if high <= low:
            problems.append(f"MAX_LIMIT {high} is not above MIN_LIMIT {low}")
        elif last is not None and not (low <= last <= high):
            problems.append(f"last price {last} is outside [{low}, {high}]")
    return problems


# =============================================================================
# CONNECTION
# =============================================================================

def start_session(host: str, port: int, app_name: str):
    """Connect, then authorize as the application.  Returns (session,
    identity); identity is None only when no app name was given, which is the
    Terminal case, not the B-PIPE one."""
    blpapi = _blpapi()
    print(f"blpapi {getattr(blpapi, '__version__', 'unknown')}")

    opts = blpapi.SessionOptions()
    opts.setServerHost(host)
    opts.setServerPort(port)
    if app_name:
        opts.setAuthenticationOptions(AUTH_TEMPLATE.format(app=app_name))

    session = blpapi.Session(opts)
    if not session.start():
        raise SystemExit(
            f"FAIL  cannot reach B-PIPE at {host}:{port}.\n"
            f"      Check the host is reachable and the port is open.")
    print(f"ok    connected to {host}:{port}")

    if not app_name:
        print("warn  --no-auth: connecting unauthenticated, which only works "
              "against a local Terminal")
        return session, None

    identity = authorize(session, app_name)
    print(f"ok    authorized as {app_name}")
    return session, identity


def authorize(session, app_name: str):
    """Token, then AuthorizationRequest.  The demo tool's 'Simplify
    Authentication' checkbox is exactly these two steps."""
    blpapi = _blpapi()

    queue = blpapi.EventQueue()
    session.generateToken(eventQueue=queue)
    token = None
    while token is None:
        event = queue.nextEvent(TIMEOUT_MS)
        for msg in event:
            kind = str(msg.messageType())
            if kind == "TokenGenerationSuccess":
                token = msg.getElementAsString("token")
            elif kind == "TokenGenerationFailure":
                raise SystemExit(f"FAIL  token generation refused:\n{msg}")
        if event.eventType() == blpapi.Event.TIMEOUT:
            raise SystemExit("FAIL  timed out waiting for a token")

    if not session.openService("//blp/apiauth"):
        raise SystemExit("FAIL  could not open //blp/apiauth")

    auth_service = session.getService("//blp/apiauth")
    request = auth_service.createAuthorizationRequest()
    request.set("token", token)

    identity = session.createIdentity()
    queue = blpapi.EventQueue()
    session.sendAuthorizationRequest(
        request, identity, blpapi.CorrelationId("auth"), queue)

    while True:
        event = queue.nextEvent(TIMEOUT_MS)
        for msg in event:
            kind = str(msg.messageType())
            if kind == "AuthorizationSuccess":
                return identity
            if kind == "AuthorizationFailure":
                raise SystemExit(
                    f"FAIL  B-PIPE refused the application {app_name!r}:\n"
                    f"{msg}")
        if event.eventType() == blpapi.Event.TIMEOUT:
            raise SystemExit("FAIL  timed out waiting for authorization")


def _send(session, request, identity):
    """Send one request and yield every message of the response."""
    blpapi = _blpapi()
    session.sendRequest(request, identity)
    while True:
        event = session.nextEvent(TIMEOUT_MS)
        for msg in event:
            yield msg
        if event.eventType() == blpapi.Event.RESPONSE:
            return
        if event.eventType() == blpapi.Event.TIMEOUT:
            raise SystemExit("FAIL  timed out waiting for a response")


# =============================================================================
# PROBE 1 - are these fields real, and are they real-time?
# =============================================================================

def probe_fieldinfo(session, identity, fields) -> bool:
    print("\n--- //blp/apiflds : what are these fields? ---")
    if not session.openService("//blp/apiflds"):
        print("FAIL  could not open //blp/apiflds")
        return False

    service = session.getService("//blp/apiflds")
    request = service.createRequest("FieldInfoRequest")
    for field in fields:
        request.getElement("id").appendValue(field)
    request.set("returnFieldDocumentation", False)

    seen = {}
    for msg in _send(session, request, identity):
        if not msg.hasElement("fieldData"):
            continue
        data = msg.getElement("fieldData")
        for i in range(data.numValues()):
            entry = data.getValueAsElement(i)
            asked = entry.getElementAsString("id")
            if entry.hasElement("fieldError"):
                print(f"  {asked:<14} NOT A FIELD  "
                      f"{entry.getElement('fieldError')}")
                seen[asked] = None
                continue
            info = entry.getElement("fieldInfo")
            row = {name: info.getElementAsString(name)
                   for name in ("mnemonic", "datatype", "ftype")
                   if info.hasElement(name)}
            desc = (info.getElementAsString("description")
                    if info.hasElement("description") else "")
            print(f"  {row.get('mnemonic', asked):<14} "
                  f"{row.get('ftype', '?'):<10} "
                  f"{row.get('datatype', '?'):<12} {desc}")
            seen[asked] = row

    missing = [f for f in fields if not seen.get(f)]
    if missing:
        print(f"\n  {', '.join(missing)} do not exist as Bloomberg fields - "
              f"the mnemonics are wrong, and nothing downstream can work "
              f"until they are right.")
        return False
    return True


# =============================================================================
# PROBE 2 - the limits right now, next to the ones the R job uses
# =============================================================================

def probe_snapshot(session, identity, security: str, fields) -> bool:
    print(f"\n--- //blp/refdata : {security} now ---")
    if not session.openService("//blp/refdata"):
        print("FAIL  could not open //blp/refdata")
        return False

    service = session.getService("//blp/refdata")
    request = service.createRequest("ReferenceDataRequest")
    request.getElement("securities").appendValue(security)
    for field in fields:
        request.getElement("fields").appendValue(field)

    values = {}
    for msg in _send(session, request, identity):
        if msg.hasElement("responseError"):
            print(f"FAIL  responseError: {msg.getElement('responseError')}")
            return False
        if not msg.hasElement("securityData"):
            continue
        data = msg.getElement("securityData")
        for i in range(data.numValues()):
            entry = data.getValueAsElement(i)
            if entry.hasElement("securityError"):
                print(f"FAIL  {security}: "
                      f"{entry.getElement('securityError')}")
                return False
            values.update(element_to_dict(entry.getElement("fieldData")))
            #  A field that is real but not available this way says so here,
            #  which is the difference between "wrong name" and "wrong
            #  service".
            if entry.hasElement("fieldExceptions"):
                exceptions = entry.getElement("fieldExceptions")
                for j in range(exceptions.numValues()):
                    ex = exceptions.getValueAsElement(j)
                    field = ex.getElementAsString("fieldId")
                    info = ex.getElement("errorInfo")
                    message = (info.getElementAsString("message")
                               if info.hasElement("message") else str(info))
                    print(f"  {field:<14} not served here: {message}")

    for field in fields:
        if field in values:
            print(f"  {field:<14} {values[field]}")

    low = to_decimal(values.get("MIN_LIMIT"))
    high = to_decimal(values.get("MAX_LIMIT"))
    r_low = to_decimal(values.get("PX_MIN_LIMIT"))
    r_high = to_decimal(values.get("PX_MAX_LIMIT"))
    last = to_decimal(values.get("PX_LAST"))

    problems = band_is_sane(low, high, last)
    if problems:
        print("  " + "; ".join(problems))
    else:
        print(f"  band {low} .. {high}, last {last} - consistent")

    #  The comparison this whole exercise exists for.
    if (low, high) != (None, None) and (r_low, r_high) != (None, None):
        if (low, high) == (r_low, r_high):
            print("  MIN/MAX_LIMIT agree with PX_MIN/PX_MAX_LIMIT exactly - "
                  "the real-time pair is a drop-in for what the R job reads")
        else:
            print(f"  DIFFER: real-time {low}/{high} vs "
                  f"reference {r_low}/{r_high}. Explain this before "
                  f"building on it.")

    return not problems


# =============================================================================
# PROBE 3 - the subscription
# =============================================================================

def probe_stream(session, identity, security: str, fields, seconds: int):
    print(f"\n--- //blp/mktdata : {security}, up to {seconds}s ---")
    blpapi = _blpapi()
    if not session.openService("//blp/mktdata"):
        print("FAIL  could not open //blp/mktdata")
        return False

    subscriptions = blpapi.SubscriptionList()
    subscriptions.add(security, fields, "", blpapi.CorrelationId(security))
    session.subscribe(subscriptions, identity)

    wanted = set(fields)
    got = {}
    started = time.monotonic()
    deadline = started + seconds

    while time.monotonic() < deadline and not wanted <= set(got):
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        event = session.nextEvent(min(remaining, 1000))
        kind = event.eventType()

        if kind == blpapi.Event.SUBSCRIPTION_STATUS:
            for msg in event:
                #  SubscriptionFailure and the exceptions array inside
                #  SubscriptionStarted are where a bad field name surfaces,
                #  so print the message whole rather than picking at it.
                print(f"  {_now()}  {msg.messageType()}\n{msg}")
        elif kind == blpapi.Event.SUBSCRIPTION_DATA:
            for msg in event:
                tick = element_to_dict(msg.asElement())
                interesting = {k: v for k, v in tick.items()
                               if k in wanted and v is not None}
                if interesting:
                    for k, v in interesting.items():
                        got.setdefault(k, (v, _now()))
                    print(f"  {_now()}  {interesting}")

    session.unsubscribe(subscriptions)
    elapsed = time.monotonic() - started

    print()
    for field in fields:
        if field in got:
            value, when = got[field]
            print(f"  {field:<14} {value}   first seen {when}")
        else:
            print(f"  {field:<14} never arrived in {elapsed:.0f}s")

    if wanted <= set(got):
        low = to_decimal(got.get("MIN_LIMIT", (None,))[0])
        high = to_decimal(got.get("MAX_LIMIT", (None,))[0])
        problems = band_is_sane(low, high)
        if problems:
            print("  " + "; ".join(problems))
            return False
        print(f"  band {low} .. {high} off the live feed in {elapsed:.1f}s")
        return True

    print("  Nothing wrong has necessarily happened: outside Tokyo hours a\n"
          "  quiet subscription is expected. Compare against the snapshot\n"
          "  above, which does not need the market to be open.")
    return False


# =============================================================================
# MAIN
# =============================================================================

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Ask B-PIPE for a Japanese stock's daily price limits.")
    p.add_argument("security", nargs="?", default=SECURITY,
                   help=f'default "{SECURITY}"')
    p.add_argument("--host", default=HOST, help="B-PIPE host address")
    p.add_argument("--port", default=PORT, help="B-PIPE port")
    p.add_argument("--app", default=APP_NAME, help="application name")
    p.add_argument("--no-auth", action="store_true",
                   help="skip authorization entirely - a local Terminal only")
    p.add_argument("--seconds", type=int, default=30,
                   help="how long to hold the subscription open")
    p.add_argument("--only", choices=("fieldinfo", "snapshot", "stream"),
                   help="run a single probe instead of all three")
    p.add_argument("--self-test", action="store_true",
                   help="check the parsing, with no Bloomberg at all")
    a = p.parse_args(argv)

    if a.self_test:
        return self_test()

    try:
        host, port, app = resolve_connection(a.host, a.port, a.app, a.no_auth)
    except SettingError as e:
        print(f"FAIL  {e}", file=sys.stderr)
        return 2

    session, identity = start_session(host, port, app)
    try:
        results = {}
        if a.only in (None, "fieldinfo"):
            results["fieldinfo"] = probe_fieldinfo(session, identity,
                                                   RT_FIELDS)
        if a.only in (None, "snapshot"):
            results["snapshot"] = probe_snapshot(session, identity,
                                                 a.security, REF_FIELDS)
        if a.only in (None, "stream"):
            results["stream"] = probe_stream(session, identity, a.security,
                                             RT_FIELDS, a.seconds)
    finally:
        session.stop()

    print("\n--- result ---")
    for name, good in results.items():
        print(f"  {'ok  ' if good else 'no  '}  {name}")

    #  The stream is allowed to be quiet out of hours; the snapshot is not.
    decisive = [good for name, good in results.items() if name != "stream"]
    return 0 if decisive and all(decisive) else 1


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

    class FakeElement:
        """The slice of blpapi's Element interface this file uses."""

        def __init__(self, name, value=None, children=None):
            self._name = name
            self._value = value
            self._children = children or []

        def name(self):
            return self._name

        def numElements(self):
            return len(self._children)

        def getElement(self, i):
            return self._children[i]

        def getValue(self):
            if self._value is _RAISES:
                raise RuntimeError("no value in this tick")
            return self._value

    _RAISES = object()
    D = Decimal

    print("bpipe_probe --self-test\n\nreading an element")
    tick = FakeElement("tick", children=[
        FakeElement("MIN_LIMIT", 2500.0),
        FakeElement("MAX_LIMIT", 3100.0),
        FakeElement("LAST_PRICE", 2800.0)])
    check("every named child becomes a key", element_to_dict(tick),
          {"MIN_LIMIT": 2500.0, "MAX_LIMIT": 3100.0, "LAST_PRICE": 2800.0})
    check("an empty element is an empty dict",
          element_to_dict(FakeElement("tick")), {})
    check("a field present with no value is kept as None, not dropped - the "
          "difference between 'not subscribed' and 'nothing yet' matters",
          element_to_dict(FakeElement("tick", children=[
              FakeElement("MIN_LIMIT", _RAISES)])),
          {"MIN_LIMIT": None})

    print("\nturning a value into a price")
    check("a float", to_decimal(2500.0), D("2500.0"))
    check("a string", to_decimal("2500"), D("2500"))
    check("bytes", to_decimal(b"2500"), D("2500"))
    check("no value is not a price", to_decimal(None), None)
    check("nor is zero - a limit of 0 is a missing value wearing a number",
          to_decimal(0), None)
    check("nor is a negative", to_decimal(-1), None)
    check("nor is a nan", to_decimal(float("nan")), None)
    check("nor is text", to_decimal("N.A."), None)
    check("nor is a bool", to_decimal(True), None)

    print("\njudging the pair")
    check("a band around the last price is fine",
          band_is_sane(D("2500"), D("3100"), D("2800")), [])
    check("the last price may sit exactly on a limit - it does, on a day the "
          "stock is limit up",
          band_is_sane(D("2500"), D("3100"), D("3100")), [])
    check("no last price to check against is not a problem",
          band_is_sane(D("2500"), D("3100")), [])
    check("a missing low", band_is_sane(None, D("3100")), ["no MIN_LIMIT"])
    check("a missing high", band_is_sane(D("2500"), None), ["no MAX_LIMIT"])
    check("both missing, both reported", band_is_sane(None, None),
          ["no MIN_LIMIT", "no MAX_LIMIT"])
    check("an inverted band",
          band_is_sane(D("3100"), D("2500")),
          ["MAX_LIMIT 2500 is not above MIN_LIMIT 3100"])
    check("a last price outside the band means one of the three is wrong",
          band_is_sane(D("2500"), D("3100"), D("4000")),
          ["last price 4000 is outside [2500, 3100]"])

    print("\nthe authorization string sent to B-PIPE")
    check("application only, app name and key",
          AUTH_TEMPLATE.format(app="APPNAME"),
          "AuthenticationMode=APPLICATION_ONLY;"
          "ApplicationAuthenticationType=APPNAME_AND_KEY;"
          "ApplicationName=APPNAME")

    def raises(name, fn, fragment):
        nonlocal ok
        try:
            got = repr(fn())
        except SettingError as e:
            got = str(e)
        good = fragment in got
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want it to contain "
                                f"{fragment!r}"))

    print("\nresolving the connection settings")
    check("all three given", resolve_connection("h", "8194", "a", False),
          ("h", 8194, "a"))
    check("a port that arrives as a number, not a string",
          resolve_connection("h", 8194, "a", False), ("h", 8194, "a"))
    check("whitespace around a pasted value is not part of it",
          resolve_connection("  h  ", " 8194 ", " a ", False),
          ("h", 8194, "a"))
    check("--no-auth drops the application name deliberately",
          resolve_connection("h", "8194", "a", True), ("h", 8194, ""))
    raises("an empty host is refused by name, not defaulted",
           lambda: resolve_connection("", "8194", "a", False), "HOST")
    raises("so is an empty port",
           lambda: resolve_connection("h", "", "a", False), "PORT")
    raises("so is an empty application name - B-PIPE would refuse the "
           "session anyway, and later",
           lambda: resolve_connection("h", "8194", "", False), "APP_NAME")
    raises("a port that is not a number",
           lambda: resolve_connection("h", "eight", "a", False),
           "not a number")
    raises("a number that is not a port",
           lambda: resolve_connection("h", "99999", "a", False),
           "not a port number")

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
