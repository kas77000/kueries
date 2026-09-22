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

kdb does not store pairs. The `fx_last` table on the **REF** process holds one
rate per currency per date, **in USD per unit**. `get_fx_last.q` builds it each
night from `d_fx_last`, which reads `equity.fx_last` by `CRNCY`. A pair is the
ratio of two of those rates:

```
EURCNY = fx_last[EUR] / fx_last[CNY]        CNY per 1 EUR
```

That makes it a cross through USD. It can differ from a screen quote in the last
digit. USD is taken as 1.0 whether or not the table has a row for it.

## Running it

Set the REF endpoint and your usual currencies once, in `local_settings.py`
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

If a pair gets no rates at all, the script says so and lists the currencies
that **are** in `fx_last` over the range, for example CNY where CNH was asked
for. It still writes the other pairs, then exits with code 1.

## Caveats

- **GBp.** `fx_last` is keyed on the equities' currency, so pence can sit beside
  pounds at a hundredth of the value. Codes are upper-cased, so `GBP` always
  means the pound.
- **No filling.** A date with no rate for either side of a pair is left out,
  not carried forward from the day before.
