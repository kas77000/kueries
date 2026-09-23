#!/usr/bin/env python3
"""
=============================================================================
fx_rates.py

Daily FX rates for a list of currency pairs over a date range, written as the
three column file the desk passes around:

    EURCNY  20240812  7.8418
    EURCNY  20240813  7.8698
    EURGBP  20240812  0.85629

one row per pair per date, no header, pairs in the order they were asked for
and dates ascending within each pair.  Only dates the database has a rate for
appear, so weekends and holidays are simply not there.

WHERE THE NUMBERS COME FROM.  The curncy table on the REF process, which
carries QUOTED pairs, one row per Bloomberg ticker per date:

    date        IBD              PX_LAST
    2024.08.12  EURCNY Curncy    7.8418     CNY per 1 EUR
    2024.08.12  EURGBP Curncy    0.85629    GBP per 1 EUR

So EURCNY is read straight off the table - no cross, no arithmetic:

    EURCNY = PX_LAST[`$"EURCNY Curncy"]

The ticker is the pair plus " Curncy".  A pair is always written base first,
<BASE><QUOTE>, the way the desk asks for it; a pair with no ticker on curncy is
reported missing rather than crossed through the dollar, so every rate in the
file is a quote.

Talks to ONE kdb process over PyKX - the REF process holding curncy.
host:port is a constant below rather than an argument; set it once, before
first use.

  python scripts/fx_rates/fx_rates.py --start 2024-08-12 --end 2024-09-11

  python scripts/fx_rates/fx_rates.py --start 2024-08-12 --end 2024-09-11 \\
      --currencies CNH CNY GBP

  python scripts/fx_rates/fx_rates.py --start 2024-08-12 --end 2024-09-11 \\
      --currencies EURCNY,USDJPY,GBPUSD --out fx.xlsx

The pairs come from CURRENCIES, set in local_settings.py beside this script;
--currencies on the command line replaces that list for one run.  A three
letter code is paired with --base (BASE, EUR unless told otherwise); a six
letter code is a full pair and is used as it is.

PyKX runs in unlicensed mode - SyncQConnection against a remote process needs
no q licence and no QHOME, because all q evaluation happens on the server.
pykx is imported lazily inside connect(), so --self-test runs anywhere.

  python scripts/fx_rates/fx_rates.py --self-test
=============================================================================
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# scripts/lib holds local_config, which reads the settings file beside this
# script.  Added to the path rather than installed, so this still runs as
# `python scripts/fx_rates/fx_rates.py` from the repo root.  Copy scripts/lib
# alongside this folder if you move it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.local_config import apply_local                        # noqa: E402

# -----------------------------------------------------------------------------
# CONNECTION.  Edit this, or put it in a local_settings.py beside this script -
# see scripts/lib/README.md.  The REF process: the one holding curncy.
#
# It is an open process, so host and port is the whole of it - connect() takes
# no credentials.
# -----------------------------------------------------------------------------

REF_SERVER = "CHANGEME:5010"

# Significant digits in the rate column.  5 is what the desk file carries:
# 7.8418 for EURCNY, 0.85629 for EURGBP.  A trailing zero is dropped the same
# way Excel drops it, so 7.8780 is written 7.878.
SIG_DIGITS = 5

# The pairs to fetch when --currencies is not given.  Same rules as the
# argument: a three letter code pairs with BASE, a six letter code is a full
# pair.  --currencies on the command line REPLACES this list, it does not add
# to it.
CURRENCIES = ["CNH", "CNY", "GBP"]
BASE = "EUR"

# What turns a pair into an IBD on curncy: EURCNH -> `$"EURCNH Curncy".
TICKER_SUFFIX = " Curncy"

_PLACEHOLDER = "CHANGEME"

# -----------------------------------------------------------------------------
# Anything above can be overridden from a local_settings.py beside this script,
# which git ignores - so the server survives a pull and this file never has to
# be edited.  See scripts/lib/README.md.
# -----------------------------------------------------------------------------

apply_local(globals(), __file__)

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"


# -----------------------------------------------------------------------------
# q source.  Sent as text + typed args - dates and pairs travel as q values,
# never interpolated into the text.
#
# The pairs arrive as ONE char vector, "EURCNH EURCNY", and the " Curncy" is
# appended on the server, because an IBD has a space in it and the space is
# what the list is split on.  PyKX sends a python str as a symbol and a list of
# them as a symbol list, but a single one-element list comes over as something
# else again; bytes always arrive as chars, so that is the one shape relied on.
#
# A plain select, no join, no pivot: the rate is already the pair.
# -----------------------------------------------------------------------------

Q_FX = """
{[d0;d1;p]
  p:`$(" " vs p),\\:" Curncy";
  select date,IBD,PX_LAST from curncy where date within (d0;d1), IBD in p
 }
"""

# What IS on file over the range, so a pair that matched nothing can be
# compared against the tickers that were there to match - EURCNH versus EURCNY.
Q_AVAILABLE = """
{[d0;d1]
  asc exec distinct IBD from curncy where date within (d0;d1)
 }
"""


# -----------------------------------------------------------------------------
# kdb IO
# -----------------------------------------------------------------------------

def parse_hostport(s):
    """'host:port' -> ('host', port).  Raises on anything else."""
    if ":" not in s:
        raise ValueError(f"expected host:port, got {s!r}")
    host, _, port = s.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"expected host:port, got {s!r}")
    return host, int(port)


def connect(hostport):
    """Open a PyKX connection on a host and a port; the server is open, so there
    is nothing to log in with.  pykx is imported here, not at module level, so
    the pure-python half of this file stays importable without it."""
    if hostport.startswith(_PLACEHOLDER):
        raise SystemExit(
            f"{hostport!r} is still the placeholder.  Set REF_SERVER in a "
            f"local_settings.py beside this script, or near the top of "
            f"{__file__}."
        )
    try:
        import pykx
    except ImportError:
        raise SystemExit(
            "pykx is not installed.  pip install pykx\n"
            "Only IPC is needed here, so unlicensed mode is enough - no q "
            "licence and no QHOME required."
        )
    host, port = parse_hostport(hostport)
    return pykx.SyncQConnection(host=host, port=port)


def _decode(v):
    return v.decode() if isinstance(v, bytes) else v


def _to_pandas(tbl):
    """PyKX table -> DataFrame, symbols as str and PX_LAST as plain float64.

    PyKX hands symbols back as bytes in some versions and str in others, and a
    numeric column holding a q null comes back MASKED rather than as NaN.  Both
    are flattened here, at the boundary, so nothing below has to know."""
    df = tbl.pd()
    if "IBD" in df:
        df["IBD"] = df["IBD"].map(_decode).astype(str)
    if "PX_LAST" in df:
        v = getattr(df["PX_LAST"], "values", df["PX_LAST"])
        if isinstance(v, np.ma.MaskedArray):
            v = v.astype("float64").filled(np.nan)
        df["PX_LAST"] = pd.Series(np.asarray(v, dtype="float64"), index=df.index)
    return df


def fetch(ho, d0, d1, codes):
    """Every (date, IBD, PX_LAST) on file for these pairs and dates."""
    return _to_pandas(ho(Q_FX, d0, d1, " ".join(codes).encode()))


def available(ho, d0, d1):
    return [_decode(v) for v in ho(Q_AVAILABLE, d0, d1).py()]


# -----------------------------------------------------------------------------
# The pairs
# -----------------------------------------------------------------------------

def parse_pairs(tokens, base):
    """--currencies as typed -> [(pair, base, quote)], in the order given.

    Tokens may be split by spaces or commas.  Three letters pair with base;
    six letters are a full pair.  Upper cased, so `cny` is CNY.  A repeat is
    dropped, and so is base against itself - EUREUR is 1 on every line."""
    base = base.strip().upper()
    if len(base) != 3 or not base.isalpha():
        raise ValueError(f"--base must be a three letter code, got {base!r}")
    out, seen = [], set()
    for tok in (t for chunk in tokens for t in chunk.replace(",", " ").split()):
        t = tok.strip().upper()
        if len(t) == 3 and t.isalpha():
            b, q = base, t
        elif len(t) == 6 and t.isalpha():
            b, q = t[:3], t[3:]
        else:
            raise ValueError(f"{tok!r} is neither a currency (CNY) nor a pair "
                             f"(EURCNY)")
        if b == q or (b + q) in seen:
            continue
        seen.add(b + q)
        out.append((b + q, b, q))
    if not out:
        raise ValueError("no currency pairs to fetch")
    return out


def resolve_currencies(cli, configured):
    """--currencies if it was given, otherwise the CURRENCIES setting.

    The command line REPLACES the setting rather than adding to it, so one run
    can ask for a single pair without editing local_settings.py.  A setting
    written as one string, "CNY, GBP", is accepted as well as a list."""
    if cli:
        return list(cli)
    if isinstance(configured, str):
        return [configured]
    return list(configured or [])


def pair_codes(pairs):
    """Every pair the query has to ask curncy for, in the order asked."""
    return [p for p, _, _ in pairs]


def pair_of_ticker(ibd):
    """'EURCNY Curncy' -> 'EURCNY'.  Anything else is left as it came."""
    return ibd[:-len(TICKER_SUFFIX)] if ibd.endswith(TICKER_SUFFIX) else ibd


def build_rows(px, pairs):
    """curncy rows -> [(pair, yyyymmdd, rate)], plus the pairs that got nothing.

    The rate IS the pair, so there is nothing to divide.  A zero or null
    PX_LAST is treated as missing - it would otherwise write a 0 into a file
    somebody will price off."""
    px = px.copy()
    px["date"] = pd.to_datetime(px["date"]).dt.normalize()
    px["pair"] = px["IBD"].map(pair_of_ticker)
    px = px[px["PX_LAST"].notna() & (px["PX_LAST"] > 0)]
    # one rate per (date, pair): if curncy carries more than one, keep the last
    px = px.drop_duplicates(["date", "pair"], keep="last").sort_values("date")

    by_pair = {k: v for k, v in px.groupby("pair", sort=False)}
    rows, empty = [], []
    for pair, _, _ in pairs:
        got = by_pair.get(pair)
        if got is None or got.empty:
            empty.append(pair)
            continue
        rows.extend((pair, int(d.strftime("%Y%m%d")), float(r))
                    for d, r in zip(got["date"], got["PX_LAST"]))
    return rows, empty


def fmt_rate(x, sig=SIG_DIGITS):
    """7.87799 -> '7.878', 0.856293 -> '0.85629'.  Never exponent notation."""
    return np.format_float_positional(x, precision=sig, unique=False,
                                      fractional=False, trim="-")


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------

def write_csv(path, rows, sig=SIG_DIGITS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for pair, day, rate in rows:
            w.writerow([pair, day, fmt_rate(rate, sig)])


def write_xlsx(path, rows, sig=SIG_DIGITS):
    """Same three columns, no header.  The rate is written as a NUMBER rounded
    to sig digits, so Excel can sum and chart it - a string would sit left
    aligned and be ignored by every formula."""
    df = pd.DataFrame([(p, d, float(fmt_rate(r, sig))) for p, d, r in rows])
    df.to_excel(path, header=False, index=False)


def default_out(base, start, end):
    return DEFAULT_OUT_DIR / f"fx_{base}_{start:%Y%m%d}_{end:%Y%m%d}.csv"


def log(msg=""):
    print(msg, file=sys.stderr, flush=True)


def run(args):
    pairs = parse_pairs(args.currencies, args.base)
    codes = pair_codes(pairs)
    out = Path(args.out) if args.out else default_out(args.base.upper(),
                                                      args.start, args.end)

    log(f"fx_rates  {args.start} to {args.end}  {', '.join(codes)}")
    log(f"  ref server  {REF_SERVER} ...")
    ho = connect(REF_SERVER)

    px = fetch(ho, args.start, args.end, codes)
    log(f"  {len(px):,} curncy rows back")
    rows, empty = build_rows(px, pairs) if len(px) else ([], list(codes))

    if empty:
        have = [pair_of_ticker(t) for t in available(ho, args.start, args.end)]
        ccys = sorted({c for p, b, q in pairs if p in empty for c in (b, q)})
        log(f"\n  no rates for {', '.join(empty)} between {args.start} and "
            f"{args.end}.")
        near = [h for h in have if any(c in h for c in ccys)]
        if near:
            log(f"  pairs curncy has over that range touching "
                f"{', '.join(ccys)}:")
            log("    " + " ".join(near))
        elif have:
            log(f"  nothing on curncy touches {', '.join(ccys)} over that "
                f"range ({len(have):,} pairs on file).")
        else:
            log("    nothing at all - check the dates, and that REF_SERVER is "
                "the process holding curncy")

    if not rows:
        raise SystemExit("\nnothing to write")

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() == ".xlsx":
        write_xlsx(out, rows, args.sig_digits)
    else:
        write_csv(out, rows, args.sig_digits)

    n_dates = len({d for _, d, _ in rows})
    print(f"{len(rows):,} rows, {len(pairs) - len(empty)} pair(s) over "
          f"{n_dates} date(s)")
    print(f"written to {out}")
    return 1 if empty else 0


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Daily FX rates per pair over a date range, from curncy",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--start", type=dt.date.fromisoformat,
                   help="first date, inclusive, YYYY-MM-DD")
    p.add_argument("--end", type=dt.date.fromisoformat,
                   help="last date, inclusive, YYYY-MM-DD")
    p.add_argument("--currencies", nargs="+",
                   help="CNY GBP ... paired with --base, or full pairs like "
                        "EURCNY; spaces or commas.  Replaces CURRENCIES from "
                        "local_settings.py (now: " + " ".join(CURRENCIES) + ")")
    p.add_argument("--base", default=BASE,
                   help="the currency a three letter code is quoted against")
    p.add_argument("--out", help="output file, .csv or .xlsx; default "
                                 "out/fx_<base>_<start>_<end>.csv beside this "
                                 "script")
    p.add_argument("--sig-digits", type=int, default=SIG_DIGITS,
                   help="significant digits in the rate column")
    p.add_argument("--self-test", action="store_true",
                   help="run the built-in tests; needs no kdb connection")
    args = p.parse_args(argv)

    if args.self_test:
        return self_test()
    args.currencies = resolve_currencies(args.currencies, CURRENCIES)
    missing = [n for n in ("start", "end", "currencies") if not getattr(args, n)]
    if missing:
        p.error("required unless --self-test: " + ", ".join("--" + m for m in missing))
    if args.end < args.start:
        p.error(f"--end {args.end} is before --start {args.start}")
    try:
        parse_pairs(args.currencies, args.base)
    except ValueError as exc:
        p.error(str(exc))
    return run(args)


# -----------------------------------------------------------------------------
# Built-in tests.  Everything except the q is pure python and is checked here,
# so the script can be verified on a machine with no kdb.
# -----------------------------------------------------------------------------

def _px(rows):
    """(date, pair, rate) triples -> the frame fetch() returns."""
    return pd.DataFrame([(d, c + TICKER_SUFFIX, v) for d, c, v in rows],
                        columns=["date", "IBD", "PX_LAST"])


def test_three_letters_pair_with_base_six_are_a_pair():
    got = parse_pairs(["CNH", "cny,GBP", "USDJPY"], "eur")
    assert got == [("EURCNH", "EUR", "CNH"), ("EURCNY", "EUR", "CNY"),
                   ("EURGBP", "EUR", "GBP"), ("USDJPY", "USD", "JPY")], got


def test_repeats_and_base_against_itself_are_dropped():
    got = [p for p, _, _ in parse_pairs(["GBP", "EUR", "EURGBP", "gbp"], "EUR")]
    assert got == ["EURGBP"], got


def test_command_line_replaces_the_setting():
    assert resolve_currencies(["JPY"], ["CNY", "GBP"]) == ["JPY"]


def test_the_setting_is_used_when_nothing_is_typed():
    assert resolve_currencies(None, ["CNY", "GBP"]) == ["CNY", "GBP"]
    assert resolve_currencies(None, []) == []
    got = parse_pairs(resolve_currencies(None, "CNY, GBP"), "EUR")
    assert [p for p, _, _ in got] == ["EURCNY", "EURGBP"], got


def test_a_bad_code_is_an_error_not_a_skip():
    for bad in (["EU"], ["EURO"], ["EUR1"]):
        try:
            parse_pairs(bad, "EUR")
        except ValueError:
            continue
        raise AssertionError(f"{bad} should not parse")


def test_the_ticker_is_the_pair_plus_curncy():
    assert pair_of_ticker("EURCNY Curncy") == "EURCNY"
    assert pair_of_ticker("EURCNY") == "EURCNY"       # already stripped


def test_the_rate_is_the_quote_not_a_ratio():
    """curncy stores the pair, so EURCNY is PX_LAST as it stands."""
    px = _px([("2024-08-12", "EURCNY", 7.8418)])
    rows, empty = build_rows(px, parse_pairs(["CNY"], "EUR"))
    assert empty == []
    assert rows == [("EURCNY", 20240812, 7.8418)], rows


def test_usd_is_a_ticker_like_any_other():
    """No dollar special case any more - USDJPY is its own quote."""
    px = _px([("2024-08-12", "USDJPY", 146.9)])
    rows, empty = build_rows(px, parse_pairs(["USDJPY"], "EUR"))
    assert empty == [] and rows == [("USDJPY", 20240812, 146.9)], (rows, empty)


def test_the_pair_is_always_base_first():
    """Base first, whether the code was typed short or as a whole pair."""
    got = [p for p, _, _ in parse_pairs(["CNY", "USDJPY"], "EUR")]
    assert got == ["EURCNY", "USDJPY"], got
    px = _px([("2024-08-12", "EURCNY", 7.8418)])
    rows, _ = build_rows(px, parse_pairs(["CNY"], "EUR"))
    assert rows[0][0] == "EURCNY", rows


def test_a_null_or_zero_rate_is_left_out():
    px = _px([("2024-08-12", "EURGBP", 0.856), ("2024-08-13", "EURGBP", 0.0),
              ("2024-08-14", "EURGBP", np.nan)])
    rows, _ = build_rows(px, parse_pairs(["GBP"], "EUR"))
    assert [d for _, d, _ in rows] == [20240812], rows


def test_rows_are_by_pair_in_the_order_asked_then_by_date():
    px = _px([(d, c, v) for d in ("2024-08-13", "2024-08-12")
              for c, v in (("EURGBP", 0.85), ("EURCNY", 7.84))])
    rows, _ = build_rows(px, parse_pairs(["GBP", "CNY"], "EUR"))
    assert [(p, d) for p, d, _ in rows] == [
        ("EURGBP", 20240812), ("EURGBP", 20240813),
        ("EURCNY", 20240812), ("EURCNY", 20240813)], rows


def test_a_pair_not_on_file_is_reported_not_dropped_silently():
    px = _px([("2024-08-12", "EURCNY", 7.8418)])
    rows, empty = build_rows(px, parse_pairs(["CNH", "CNY"], "EUR"))
    assert empty == ["EURCNH"], empty
    assert [p for p, _, _ in rows] == ["EURCNY"]


def test_rate_format_matches_the_desk_file():
    assert fmt_rate(7.84181) == "7.8418"
    assert fmt_rate(0.856291) == "0.85629"
    assert fmt_rate(7.87799) == "7.878"          # trailing zero dropped
    assert fmt_rate(161.2345) == "161.23"
    assert fmt_rate(0.0068123) == "0.0068123"    # never 6.8123e-03


def test_csv_has_no_header_and_three_columns():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "fx.csv"
        write_csv(p, [("EURCNY", 20240812, 7.84181), ("EURGBP", 20240812, 0.856291)])
        lines = p.read_text(encoding="utf-8").splitlines()
    assert lines == ["EURCNY,20240812,7.8418", "EURGBP,20240812,0.85629"], lines


def test_pairs_reach_q_as_chars_without_the_suffix():
    """bytes arrive as a char vector; the q splits it and appends ' Curncy',
    because the suffix carries the space the split is on."""
    sent = []

    class Result:
        def pd(self):
            return _px([])

    class Handle:
        def __call__(self, qsql, *args):
            sent.append(args)
            return Result()

    fetch(Handle(), dt.date(2024, 8, 12), dt.date(2024, 8, 13),
          pair_codes(parse_pairs(["CNH", "CNY"], "EUR")))
    assert sent[0][2] == b"EURCNH EURCNY", sent


def test_a_null_rate_arrives_as_nan():
    class Result:
        def pd(self):
            df = pd.DataFrame({"date": pd.to_datetime(["2024-08-12"] * 2),
                               "IBD": [b"EURCNY Curncy", b"EURGBP Curncy"]})
            df["PX_LAST"] = pd.Series(np.ma.array([7.84, 0.0], mask=[False, True]))
            return df

    df = _to_pandas(Result())
    assert df["IBD"].tolist() == ["EURCNY Curncy", "EURGBP Curncy"]
    assert df["PX_LAST"].dtype == np.float64
    assert np.isnan(df["PX_LAST"].iloc[1])


def test_parse_hostport():
    assert parse_hostport("h:5010") == ("h", 5010)
    for bad in ("h", "h:", ":5010", "h:abc"):
        try:
            parse_hostport(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should not parse")


def test_server_constant():
    """Once edited, the connection constant must parse as host:port."""
    if REF_SERVER.startswith(_PLACEHOLDER):
        print("        (REF_SERVER not set yet)")
        return
    try:
        parse_hostport(REF_SERVER)
    except ValueError as exc:
        raise AssertionError(f"REF_SERVER={REF_SERVER!r}: {exc}")


def self_test():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main() or 0)


# =============================================================================
# WHERE THE JUDGEMENT CALLS ARE
#
# 1. THE SOURCE IS curncy ON REF, one QUOTED pair per ticker per date, read as
#    `$"EURCNY Curncy".  These are the screen quotes, so they match what the
#    desk sees; the USD crosses this script used to build out of ref/fx_last
#    could differ in the last digit.
#
# 2. THE PAIR IS ALWAYS <BASE><QUOTE>, base first, which is the one direction
#    the desk file is written in.  The ticker follows from it, and a cross is
#    never built out of two dollar rates.  Every rate in the file is a quote
#    somebody publishes; a pair with no ticker is reported missing.
#
# 3. THE PAIR IS TAKEN AS TYPED.  EURCNH and EURCNY are different tickers, and
#    the script asks for what it was given rather than guessing the onshore or
#    offshore one.
#
# 4. NO FILLING.  A date curncy has no quote for is left out, not carried
#    forward.  The file says what the database had.
# =============================================================================
