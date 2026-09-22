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

WHERE THE NUMBERS COME FROM.  kdb does not store pairs.  It stores ONE rate per
currency per date, against the dollar, in the fx_last table on the REF process:

    date        CRNCY  fx_last
    2024.08.12  EUR    1.0932      USD per 1 EUR
    2024.08.12  CNY    0.1394      USD per 1 CNY

(built each night by get_fx_last.q from d_fx_last, which reads equity.fx_last
by CRNCY).  A pair is the ratio of two of those:

    EURCNY = fx_last[EUR] / fx_last[CNY]      CNY per 1 EUR

Talks to ONE kdb process over PyKX - the REF process holding fx_last.
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
# see scripts/lib/README.md.  The REF process: the one get_fx_last.q refreshes
# after it writes ref/fx_last.
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

_PLACEHOLDER = "CHANGEME"

# -----------------------------------------------------------------------------
# Anything above can be overridden from a local_settings.py beside this script,
# which git ignores - so the server survives a pull and this file never has to
# be edited.  See scripts/lib/README.md.
# -----------------------------------------------------------------------------

apply_local(globals(), __file__)

DEFAULT_OUT_DIR = Path(__file__).resolve().parent / "out"


# -----------------------------------------------------------------------------
# q source.  Sent as text + typed args - dates and currency codes travel as q
# values, never interpolated into the text.
#
# The currencies arrive as ONE char vector, "EUR CNY GBP", and are split and
# cast on the server.  PyKX sends a python str as a symbol and a list of them
# as a symbol list, but a single one-element list comes over as something else
# again; bytes always arrive as chars, so that is the one shape relied on.
#
# A plain select, no join, no pivot: the ratio is taken in python, where
# --self-test can prove it.
# -----------------------------------------------------------------------------

Q_FX = """
{[d0;d1;c]
  c:`$" " vs c;
  select date,CRNCY,fx_last from fx_last where date within (d0;d1), CRNCY in c
 }
"""

# What IS on file over the range, so a currency that matched nothing can be
# compared against what was there to match - CNH versus CNY, GBP versus GBp.
Q_AVAILABLE = """
{[d0;d1]
  asc exec distinct CRNCY from fx_last where date within (d0;d1)
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
    """PyKX table -> DataFrame, symbols as str and fx_last as plain float64.

    PyKX hands symbols back as bytes in some versions and str in others, and a
    numeric column holding a q null comes back MASKED rather than as NaN.  Both
    are flattened here, at the boundary, so nothing below has to know."""
    df = tbl.pd()
    if "CRNCY" in df:
        df["CRNCY"] = df["CRNCY"].map(_decode).astype(str)
    if "fx_last" in df:
        v = getattr(df["fx_last"], "values", df["fx_last"])
        if isinstance(v, np.ma.MaskedArray):
            v = v.astype("float64").filled(np.nan)
        df["fx_last"] = pd.Series(np.asarray(v, dtype="float64"), index=df.index)
    return df


def fetch(ho, d0, d1, currencies):
    """Every (date, CRNCY, fx_last) on file for these currencies and dates."""
    return _to_pandas(ho(Q_FX, d0, d1, " ".join(currencies).encode()))


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


def currencies_needed(pairs):
    """Every code the query has to bring back, in first-seen order."""
    return list(dict.fromkeys(c for _, b, q in pairs for c in (b, q)))


def build_rows(fx, pairs):
    """fx rows -> [(pair, yyyymmdd, rate)], plus the pairs that got nothing.

    USD is 1.0 on every date the database has, whether or not the table
    carries a USD row: fx_last is USD per unit, so the dollar against itself
    needs no lookup.  A zero or null rate is treated as missing - dividing by
    it would write inf or 0 into a file somebody will price off."""
    fx = fx.copy()
    fx["date"] = pd.to_datetime(fx["date"]).dt.normalize()
    fx = fx[fx["fx_last"].notna() & (fx["fx_last"] > 0)]
    # one rate per (date, currency): get_fx_last keeps the last, so do the same
    wide = (fx.drop_duplicates(["date", "CRNCY"], keep="last")
              .pivot(index="date", columns="CRNCY", values="fx_last")
              .sort_index())
    if "USD" not in wide:
        wide["USD"] = np.nan
    wide["USD"] = wide["USD"].fillna(1.0)

    rows, empty = [], []
    for pair, b, q in pairs:
        if b not in wide or q not in wide:
            empty.append(pair)
            continue
        rate = (wide[b] / wide[q]).dropna()
        if rate.empty:
            empty.append(pair)
            continue
        rows.extend((pair, int(d.strftime("%Y%m%d")), float(r))
                    for d, r in rate.items())
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
    ccys = currencies_needed(pairs)
    out = Path(args.out) if args.out else default_out(args.base.upper(),
                                                      args.start, args.end)

    log(f"fx_rates  {args.start} to {args.end}  "
        f"{', '.join(p for p, _, _ in pairs)}")
    log(f"  ref server  {REF_SERVER} ...")
    ho = connect(REF_SERVER)

    fx = fetch(ho, args.start, args.end, ccys)
    log(f"  {len(fx):,} currency rows back")
    rows, empty = build_rows(fx, pairs) if len(fx) else ([], [p for p, _, _ in pairs])

    if empty:
        have = available(ho, args.start, args.end)
        log(f"\n  no rates for {', '.join(empty)} between {args.start} and "
            f"{args.end}.")
        missing = sorted({c for p, b, q in pairs if p in empty for c in (b, q)}
                         - set(have) - {"USD"})
        if missing:
            log(f"  {', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                f"not in fx_last over that range.  What is there:")
            log("    " + " ".join(have) if have else "    nothing at all - "
                "check the dates, and that REF_SERVER is the REF process")

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
        description="Daily FX rates per pair over a date range, from fx_last",
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

def _fx(rows):
    return pd.DataFrame(rows, columns=["date", "CRNCY", "fx_last"])


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


def test_the_pair_is_base_over_quote():
    """fx_last is USD per unit, so EURCNY is EUR's rate over CNY's."""
    fx = _fx([("2024-08-12", "EUR", 1.0932), ("2024-08-12", "CNY", 0.1394)])
    rows, empty = build_rows(fx, parse_pairs(["CNY"], "EUR"))
    assert empty == []
    assert rows[0][:2] == ("EURCNY", 20240812)
    assert abs(rows[0][2] - 1.0932 / 0.1394) < 1e-12, rows


def test_usd_needs_no_row():
    fx = _fx([("2024-08-12", "JPY", 0.0068)])
    rows, _ = build_rows(fx, parse_pairs(["USDJPY"], "EUR"))
    assert abs(rows[0][2] - 1 / 0.0068) < 1e-9, rows


def test_a_date_missing_either_side_is_left_out():
    fx = _fx([("2024-08-12", "EUR", 1.1), ("2024-08-12", "GBP", 1.28),
              ("2024-08-13", "EUR", 1.1),                          # no GBP
              ("2024-08-14", "GBP", 1.27),                         # no EUR
              ("2024-08-15", "EUR", 1.1), ("2024-08-15", "GBP", 0.0)])  # bad
    rows, _ = build_rows(fx, parse_pairs(["GBP"], "EUR"))
    assert [d for _, d, _ in rows] == [20240812], rows


def test_rows_are_by_pair_in_the_order_asked_then_by_date():
    fx = _fx([(d, c, v) for d in ("2024-08-13", "2024-08-12")
              for c, v in (("EUR", 1.1), ("GBP", 1.3), ("CNY", 0.14))])
    rows, _ = build_rows(fx, parse_pairs(["GBP", "CNY"], "EUR"))
    assert [(p, d) for p, d, _ in rows] == [
        ("EURGBP", 20240812), ("EURGBP", 20240813),
        ("EURCNY", 20240812), ("EURCNY", 20240813)], rows


def test_a_currency_not_on_file_is_reported_not_dropped_silently():
    fx = _fx([("2024-08-12", "EUR", 1.1), ("2024-08-12", "CNY", 0.14)])
    rows, empty = build_rows(fx, parse_pairs(["CNH", "CNY"], "EUR"))
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


def test_currencies_reach_q_as_chars():
    """bytes arrive as a char vector, which the q splits with vs."""
    sent = []

    class Result:
        def pd(self):
            return _fx([])

    class Handle:
        def __call__(self, qsql, *args):
            sent.append(args)
            return Result()

    fetch(Handle(), dt.date(2024, 8, 12), dt.date(2024, 8, 13), ["EUR", "CNY"])
    assert sent[0][2] == b"EUR CNY", sent


def test_a_null_rate_arrives_as_nan():
    class Result:
        def pd(self):
            df = pd.DataFrame({"date": pd.to_datetime(["2024-08-12"] * 2),
                               "CRNCY": [b"EUR", b"CNY"]})
            df["fx_last"] = pd.Series(np.ma.array([1.1, 0.0], mask=[False, True]))
            return df

    df = _to_pandas(Result())
    assert df["CRNCY"].tolist() == ["EUR", "CNY"]
    assert df["fx_last"].dtype == np.float64
    assert np.isnan(df["fx_last"].iloc[1])


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
# 1. THE SOURCE IS fx_last ON REF, one USD rate per currency per date.  The
#    same numbers are on equity.fx_last per stock, which is where d_fx_last
#    builds them from; reading the finished table means one row per currency
#    rather than one per stock.
#
# 2. A PAIR IS A RATIO OF TWO DOLLAR RATES - a cross through USD, not a quoted
#    cross.  EURCNY here can differ from a screen EURCNY in the last digit.
#
# 3. MINOR UNITS.  fx_last is keyed on the equity's CRNCY, so GBp (pence) sits
#    beside GBP at a hundredth of it.  Codes are upper cased on the way in, so
#    GBP is always the pound; ask for GBp and you get GBP.
#
# 4. NO FILLING.  A date with no rate for either side of a pair is left out,
#    not carried forward.  The file says what the database had.
# =============================================================================
