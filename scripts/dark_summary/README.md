# dark_summary

**Dark Venue Execution** — of the shares we got done in dark venues, where did
they go, and what were they worth.

```bash
python scripts/dark_summary/dark_summary.py                     # today, live
python scripts/dark_summary/dark_summary.py --date 2026-07-01   # one session
python scripts/dark_summary/dark_summary.py --monthly 2026-07   # a month
python scripts/dark_summary/dark_summary.py --monthly 2026-07 --csv --raw

python scripts/dark_summary/dark_summary.py --self-test          # no kdb needed
python scripts/dark_summary/dark_summary.py --demo               # sample page
```

Same launch as [`luld_orders`](../luld_orders/README.md) and
[`short_sell_report`](../short_sell_report/README.md): no argument is today off
the live server, `--date` and `--monthly` go to the historical one, and the page
is an A4 PDF plus a PNG in `out/`.

Or without a command line at all — [`launchers/run_dark_summary.cmd`](../../launchers/README.md),
double-clicked, or called by Task Scheduler as `run_dark_summary.cmd scheduled`,
which logs instead of printing and passes the exit code back. Arguments still
pass straight through it. It is **not** in `run_all.cmd`: whether this goes out
every evening beside the other two is a decision about a distribution list.

## The page

One row per venue, biggest notional first, with a total.

```
Venue                    Orders   Name-days    Shares   Notional (USD)   % of dark
UBS-DARK                    123          78     12.1m            71.8m      44.24%
CS-CROSSFINDER-DRK           99          66      9.4m            50.3m      31.02%
MS-POOL-DARK                 63          45      4.1m            24.2m      14.91%
...
Total                       351           —     28.5m           162.2m     100.00%
```

| column | meaning |
| --- | --- |
| `Orders` | child orders that **filled** in that venue — `make > 0`, not children sent |
| `Names` / `Name-days` | distinct stocks. See [below](#names-cannot-be-added-up) |
| `Shares` | `make`, summed |
| `Notional (USD)` | `make × avg_fill_price × fxlast` — what those shares really cost |
| `% of dark` | that venue's share of the dark book |

### Percentages are shares of the dark book, never of the day's trading

Nothing here looks at a lit venue, so this page **cannot** answer "how much of
our flow went dark" — only "of what we did in the dark, where did it go". That
sentence is on the page as well as in this file, because it is the one way the
report can be misread.

### Names cannot be added up

`darkSummary` counts **distinct syms per venue per day**, and a stock traded on
two days — or in two venues — is one stock in both. So:

- On a **single day** the column is `Names`, and it is distinct stocks per venue.
- Over a **range** the header changes to `Name-days`, because that is what
  summing it gives.
- The **total is a dash**, always. There is no correct number to put there, and
  a wrong one that adds up neatly is worse than an obvious gap.

### Over a range, `% of dark` is a share of the whole range

Not an average of the daily shares. A venue that took everything on one quiet
day and nothing since reads small — which is the honest answer to where the flow
went. Same convention as the [kmonitor dashboard](../../kmonitor/dark_summary/).

### The tail is folded, not dropped

`--top` (default 20) is how many venues get a row of their own; the rest become
one `Other (n venues)` row. Its notional still counts, so the page still adds to
100% and the total still reconciles. `--csv` lists every venue.

## What it reads

**One kdb process.** `queries/dark_summary/dark_summary.q` reads `workorder` and
`target_stock` and nothing else — no quote server, and no FX call, because
`fxlast` rides along on `target_stock`.

```python
ORDER_SERVER_RT = "CHANGEME:5012"     # realtime   - workorder, target_stock
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical - the same two, plus `date`
```

Put the real ones in a **`local_settings.py` beside this script**, which git
ignores — see [`scripts/lib/README.md`](../lib/README.md). Editing the script
itself means the file you run is never the file in git.

### The query is not copied into the script

`queries/dark_summary/dark_summary.q` is sent to the server **as it stands** and
`darkSummary` is called once per date. So this script, the kmonitor dashboard
and a bare q session cannot drift apart, and a fix to the dark-venue test lands
in all three at once.

Both tables carry `date` on the RDB as well as the HDB, which is what lets the
one function serve either server unchanged.

**A venue is dark when its name contains `DARK` or `DRK`.** That match *is* the
classification, not an approximation of it. If a venue on your server is dark
under some other name, it is not in this report and nothing here will say so —
that test lives in the `.q`, and `--self-test` checks it is still what this
README claims.

### The live run takes its date off the server

Not off this machine. The plant's clock runs ahead of UTC, so either side of
midnight here the two are different days, and a local date would ask the live
server for a session it does not hold.

## The two CSVs

| flag | file | what it is |
| --- | --- | --- |
| `--csv` | `<stem>.csv` | the page's table — **every** venue, unfolded, at full precision |
| `--raw` | `<stem>_raw.csv` | **one line per venue per day** |

`--raw` is the venue × day breakdown, which the page sums away and nothing else
in this repo produces. Its `pct_of_that_day` is each venue's share of **that
day's** dark book, straight off `darkSummary` — not a share of the range, and
the column name says so.

## Email

Same arrangement as the other two reports. `EMAIL_TO` empty means **do not
send**, and that is the whole switch — there is no separate enable flag to leave
in the wrong position. Recipients are not command-line arguments on purpose: who
gets this report is part of what the report *is*, not of one run of it.

`--no-email` writes the report and does not send it, whatever `EMAIL_TO` says.
`EMAIL_DRY_RUN = True` builds the message and reports who it would go to without
opening a socket.

The body is the sign-off and nothing else — the report is the attachment. A body
that restates the table is a second copy of the numbers to keep in step.

## Checking it without kdb

```bash
python scripts/dark_summary/dark_summary.py --self-test
```

77 checks, no server and no pykx: the `.q` it sends is linted and checked for
the columns this script reads and for the `DARK`/`DRK` test itself; the
day-summing, the percentage recomputation, the tail folding and both CSVs are
proved on synthetic days; and the page is rendered and **measured** — every line
on it is checked against the right margin, because a note that runs long is
silently clipped and a test that only checks the PDF is non-empty would never
see it.

`--demo` renders the same page off synthetic data if you want to look at one.

## Related

| | |
| --- | --- |
| [`dark_routed_executed`](../dark_routed_executed/README.md) | the same dark flow split into what we **routed** and what **executed**, with the fill rate and the two pies |
| [`kmonitor/dark_summary`](../../kmonitor/dark_summary/) | the live dashboard version, by venue and by day |
| `queries/dark_summary/dark_summary.q` | the query itself — the source of truth for all three |
