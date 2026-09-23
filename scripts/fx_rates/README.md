# fx_rates

Daily FX rates for a list of currency pairs over a date range, written as the
three-column file the desk passes around. It has one row per pair per date and
no header:

```
EURCNY,20240812,7.8418
EURCNY,20240813,7.8698
EURGBP,20240812,0.85629
```

Pairs come out in the order you asked for them, with dates ascending. Only dates
the database has a rate for appear, so weekends and holidays are not in the file.

## Where the numbers come from

The `curncy` table on the **REF** process, which holds **quoted pairs** — one
row per Bloomberg ticker per date:

```
date        IBD              PX_LAST
2024.08.12  EURCNY Curncy    7.8418     CNY per 1 EUR
2024.08.12  EURGBP Curncy    0.85629    GBP per 1 EUR
```

So the rate is read straight off the table, with nothing computed:

```q
select PX_LAST from curncy where date=2024.08.12, IBD=`$"EURCNY Curncy"
```

The ticker is the pair plus `" Curncy"`. A pair is always written base first,
`<BASE><QUOTE>` — the one direction the desk file uses — so `EUR` + `CNY` is
always `EURCNY Curncy`. No cross is built out of two dollar rates: every rate in
the file is a quote somebody publishes, and a pair with no ticker on `curncy` is
reported missing instead.

## Running it

Set the REF endpoint (the process holding `curncy`) and your usual currencies
once, in `local_settings.py`
beside the script (git ignores it):

```python
REF_SERVER = "ref-host:5010"
CURRENCIES = ["CNH", "CNY", "GBP"]     # 3 letters pair with BASE, 6 are a pair
BASE = "EUR"
```

Then only the dates are needed:

```
pip install pykx pandas openpyxl
python scripts/fx_rates/fx_rates.py --start 2024-08-12 --end 2024-09-11
```

`--currencies` **replaces** the `CURRENCIES` list for that run. It does not add
to it:

```
python scripts/fx_rates/fx_rates.py --start 2024-08-12 --end 2024-09-11 --currencies CNH CNY GBP
python scripts/fx_rates/fx_rates.py --start 2024-08-12 --end 2024-09-11 --currencies EURCNY,USDJPY --out fx.xlsx
```

```
--start / --end   the date range, both inclusive, YYYY-MM-DD
--currencies      three-letter codes are paired with --base; six-letter codes
                  are full pairs. Separate them with spaces or commas.
                  Default: CURRENCIES from local_settings.py
--base            the currency three-letter codes are quoted against.
                  Default: BASE from local_settings.py (EUR)
--out             .csv or .xlsx. Default: out/fx_<base>_<start>_<end>.csv here
--sig-digits      significant digits in the rate (5, like the desk file)
--self-test       run the built-in tests. Needs no kdb connection.
```

If a pair gets no rates at all, the script says so and lists the tickers that
**are** on `curncy` over the range touching either of its currencies — for
example `EURCNY` where `EURCNH` was asked for. It still writes the other pairs,
then exits with code 1.

## Caveats

- **CNH vs CNY.** Offshore and onshore yuan are separate quotes. The script asks
  for exactly the pair it was given, and never falls back to a cross through USD.
- **No filling.** A date `curncy` has no quote for is left out, not carried
  forward from the day before.
