# short_sell_report

Every short sell order of the session, its completion and its rejections,
summarised by market, as a one page PDF and the same page as a PNG — optionally
mailed to a list of people.

```
python scripts/short_sell_report/short_sell_report.py
python scripts/short_sell_report/short_sell_report.py --monthly 2026-07
python scripts/short_sell_report/short_sell_report.py --self-test
python scripts/short_sell_report/short_sell_report.py --demo        # preview, no kdb
```

The default run is a **real-time snapshot** of the session in progress.
`--monthly` reads the historical server instead and adds two per day charts.

---

## The page

```
Short-Sell Order Report
By market · 2026-07-24 18:37
────────────────────────────────────────────────────────
732                 55.7%                394
Short-sell orders   Overall completion   Rejections

┌──────────┬────────┬────────────┬────────────┬────────────┬────────────┐
│ Market   │ Orders │  Order qty │   Executed │ Completion │ Rejections │
├──────────┼────────┼────────────┼────────────┼────────────┼────────────┤
│ Hong Kong│    109 │ 49,882,020 │ 26,476,220 │      53.1% │        239 │
│ Japan    │    541 │  5,357,418 │  4,482,418 │      83.7% │          3 │
│ Korea    │     82 │  1,030,478 │    404,515 │      39.3% │        152 │
│ Malaysia │      0 │          0 │          0 │          — │          0 │
│ Thailand │      0 │          0 │          0 │          — │          0 │
└──────────┴────────┴────────────┴────────────┴────────────┴────────────┘

Completion by market            Rejections by market
   Japan ████████████ 84%          Hong Kong ██████████ 239
Hong Kong ███████ 53%                  Korea ██████ 152
    Korea █████ 39%                     Japan ▏3
```

`--monthly` adds **Completion by day** and **Rejections by day** underneath, as
two full-width **vertical** charts, one column per trading day:

```
Completion by day
        54%           59%                     59%
  37%   ██   47%      ██                      ██
  ██    ██   ██   35% ██  42%             48% ██
  ██ 20%██   ██   ██  ██  ██  ...         ██  ██
  ─────────────────────────────────────────────────
   1  0  0   0    0   0   0                0   0
   0  7  7   7    7   7   7                7   7
   -  -  -   -    -   -   -                -   -
   ⋮  ⋮  ⋮   ⋮    ⋮   ⋮   ⋮                ⋮   ⋮      ← 2026-07-01, turned 90°
```

A month is a sequence, and a sequence reads left to right — the horizontal form
the five markets use would put time on the vertical axis, which is the wrong axis
for it. Chronological, never sorted by value.

Each column is labelled with the whole `YYYY-MM-DD`, turned on its side under the
baseline, rather than the day of month: these pages get read on their own months
later, where "17" is only a date if the subtitle is still in view.

Completion runs on a fixed 0–100% scale, so the columns mean the same thing from
one month's report to the next; rejections are a count with no natural ceiling,
so that chart scales to its own data.

Written to `--out-dir` (default `scripts/short_sell_report/out/`) as
`short_sell_report_2026-07-24.pdf` and `.png`; monthly is
`short_sell_report_2026-07.pdf`. `--out-dir` takes any path the machine can
reach, a network share included.

---

## What the numbers mean

| | |
|---|---|
| **Orders** | parent short sell orders — `target` rows with ``side=`sellshort``. A target **is** an order, so this is a row count. |
| **Order qty** | the sum of `size` over the targets in that market. `size` **is** the order's quantity, taken as it stands — nothing is aggregated within an order; the only sum is the one across the market's orders. |
| **Executed** | the sum of `workorder.make`. A workorder **is** a child order, and `make` is what that child executed — whatever state it ended in, so a cancelled child that part-filled still contributes what it filled. Nothing but `make` says a quantity was executed. |
| **Completion** | `Executed / Order qty`. The **headline** figure is that same ratio taken over all five markets at once, **not** the average of the five percentages — a market with 500 orders must not weigh the same as one with 5. |
| **Rejections** | the `workorder` rows whose state is ``` `rejected ```. Counted per **child** order, not per parent, which is why Hong Kong can show 109 orders and 239 rejections. |

**Nothing is grouped, in either table.** A target is an order and a workorder
is a child order, so every figure on the page is a plain row count or a plain
sum over rows the query returns as they stand:

```
Orders      count of target rows
Order qty   sum of target.size
Executed    sum of workorder.make
Rejections  count of workorder rows in state `rejected
```

The `last … by` clauses this query used to carry were guarding against a row
multiplication that does not happen, at the price of hiding one that would. A
check asserts none has crept back in.

`workorder` also carries `invalid_ack` and `fail_ack`. They are **not** counted:
they are a different failure — a malformed or unacknowledged send rather than a
venue saying no — and folding them in would inflate the one number on this page
a compliance reader will quote.

## Two tables, no join

`target` for the parent orders, `workorder` for their children. That is the
whole query:

```
target     where side=`sellshort, sym suffix is one of the five   -> parents
workorder  where id_target in those parents                       -> children
```

The market is the **sym suffix**, so no third table is needed to find out where
an order traded:

| suffix | market |
|---|---|
| `.HK` | Hong Kong |
| `.JP` | Japan |
| `.KS` | Korea |
| `.MK` | Malaysia |
| `.TB` | Thailand |

The suffixes live in one table in the script, `MARKETS`. The `*.HK`-style
patterns q filters on are **built from it** and sent as an argument, so what q
selects and what Python maps back cannot drift apart. Matching is
case-insensitive, and only the last dot counts, so `BRK.A.HK` is Hong Kong.

## Scope

**Hong Kong, Japan, Korea, Malaysia and Thailand**, always all five and always
in that order. A market with no short sell flow prints as a zero row rather than
vanishing, because a market that is absent from the data is otherwise
indistinguishable from one nobody remembered to ask about. Anything whose sym
carries another suffix — or none — never enters a total.

**Japan excludes restricted names.** A parent whose `fixmsg` matches
`*RSHO=1*` is dropped before anything is counted, so it appears in neither the
order count, the quantities, nor the rejections — and its child splits go with
it. The match is case-insensitive, and the count of what was dropped is printed
on the run and carried in the page footer, so an exclusion is never silent.

---

## Real-time and historical

Both flavours of the order server hold the same tables; the historical ones
carry an extra `date` column, and that is the only difference the query has to
care about. It is **one lambda** for both, with the two extractions inside
`$[hist;…;…]` — q parses the whole thing but only resolves the branch it takes,
so the historical branch's `date=d` never has to exist on the realtime side. The
realtime branch bolts on `date:0Nd`, so everything downstream — the grouping,
the join keys, the frame Python sees — has one shape.

### A note on names in the q

`ss` is q's string-search keyword, so a lambda parameter called `ss` is a
**parse** error — the query dies on the name before it does anything, and comes
back as `QError: ss`. Nothing local can catch that: it needs a real q process.

`--self-test` therefore pulls every parameter and local out of `Q_SESSION` and
checks them against `.Q.res`. Add a name to the query and the check covers it
automatically. The one to watch for is a plausible-looking short name — `ss`,
`in`, `var`, `max`, `string`, `last`, `like`, `div`, `bin`, `do`, `if`.

Set both endpoints before the first run:

```python
ORDER_SERVER_RT   = "CHANGEME:5012"   # realtime
ORDER_SERVER_HIST = "CHANGEME:5010"   # historical, the same tables plus `date`
```

Host and port is the whole of it — both are open processes, so `connect()` is
`kx.SyncQConnection(host=…, port=…)` with no username and no password, the same
way the mail relay needs no login. A check asserts no `USER` or `PASSWORD`
constant has crept back in.

`--monthly` walks the month a date at a time, skipping weekends. Holidays are
**not** skipped: a holiday calendar we would have to maintain is a worse failure
mode than a handful of queries that return nothing.

`--date 2026-07-01` runs **one past session** off the historical server, in the
daily layout — the way to reproduce a given day, or to check the historical path
without waiting for a month. A date in the future is rejected rather than run.

The three modes are decided by one pure function, `plan()`, so `--self-test`
proves which server each one reaches, which dates it asks for and what the file
is called — rather than that being found out by pointing a mode at the wrong
server.

| | server | dates | layout | file |
|---|---|---|---|---|
| *(no flags)* | realtime | the session in progress | daily | `short_sell_report_2026-07-24` |
| `--date 2026-07-01` | historical | that one session | daily | `short_sell_report_2026-07-01` |
| `--monthly 2026-07` | historical | every weekday of the month | monthly | `short_sell_report_2026-07` |

---

## Seeing the page before it touches kdb

```
python scripts/short_sell_report/short_sell_report.py --demo
```

Draws **both** layouts from made up numbers and exits — no connection, no pykx,
no server constants set. Four files land in `--out-dir`:

```
short_sell_report_SAMPLE_daily.pdf    .png
short_sell_report_SAMPLE_monthly.pdf  .png
```

The numbers are deterministic, so the preview only moves when the layout does,
and the daily one reproduces the table at the top of this README exactly. It is
stamped `SAMPLE` in the subtitle, in the footer and in the file name, on purpose:
these pages end up in compliance folders, and a preview that looks like a real
report is worse than no preview at all.

---

## Email

Configured **in the script**, not on the command line — near the top, beside the
server constants:

```python
EMAIL_TO   = ["desk@example.com", "compliance@example.com"]
EMAIL_CC   = []
EMAIL_BCC  = []
EMAIL_FROM = "algo-reports@example.com"

SMTP_HOST    = "mail.example.com"
SMTP_PORT    = 0           # 0 -> 25
SMTP_TIMEOUT = 30          # seconds

EMAIL_DRY_RUN     = False
EMAIL_SIGNATURE   = "Best Regards,

Khalife"
```

Who gets this report is part of what the report *is*, not of one run of it — a
distribution list living in whatever someone last typed is a list that quietly
loses people. **`EMAIL_TO` empty means do not send**, and that is the whole
switch: there is no separate enable flag to leave in the wrong position.

The report is the **attachment**. The body is just:

```
Best Regards,

Khalife
```

No HTML, no inlined page, no table repeated in the message. A body that
restates the report is a second copy to keep in step, and it renders at the
mercy of whatever client opens it — which is what went wrong with the first
version of this. `EMAIL_SIGNATURE` is a constant in the same block if the
sign-off changes.

Each address may itself be a comma or semicolon separated list, so a pasted
distribution list works as it is. Nobody is mailed twice. An address that does
not parse **raises** rather than being dropped: a recipient list quietly one
short is exactly the failure that goes unnoticed for months.

`EMAIL_DRY_RUN = True` builds the message, prints who it would go to and what is
attached, and opens no socket — the way to check a new recipient list.

**SMTP is host, port and timeout — there is nothing else.** No credentials and
no STARTTLS: the relay takes mail from the host this runs on, so there is
nothing to authenticate with, and an auth path nobody exercises is a path that
is broken by the time somebody needs it. One check covers both this and the kdb
connection: no `SMTP_USER`, `SMTP_PASSWORD`, `USER` or `PASSWORD` constant
exists.

The mailer itself is **[`scripts/lib/mailer.py`](../lib/README.md)**, which knows
nothing about this report and is meant to be reused by the next script that
needs to send one. Only the subject line and the body live here. If you copy
this folder somewhere, copy `scripts/lib` beside it.

---

## Running it offline

`pykx` is imported lazily inside `connect()`, and everything between the query
and the page is a pure function over plain records, so the whole analytic and
rendering path runs on a machine with no kdb, no pykx and no q licence:

```
python scripts/short_sell_report/short_sell_report.py --self-test
```

It rebuilds the page above from synthetic records — 132 checks — covering the
suffix routing (including the suffixes that are *not* ours, like Tokyo's `.T`),
the market rollup, the quantity weighted headline, the Japan exclusion
(including that the dropped orders' fills and rejections go with them), the
counting rules (only `make` executes, a cancelled child still contributes what
it filled, only ``` `rejected ``` rejects), the day series, the rendering of
both layouts, and the email — the bodies, the attachment and the recipient
parsing — end to end.

Needs `matplotlib` to draw and `pandas` to read what PyKX returns; `--self-test`
skips the rendering checks rather than failing if matplotlib is absent.

## Colour

Taken unchanged from the data-viz reference palette, which documents its own
validation: completion is categorical slot 1 blue `#2a78d6`, rejections are
status `critical` red `#d03b3b` — deliberately not the categorical red, so it
never reads as "series 8". Two charts with one series each, so hue is chart
identity rather than series identity and there is no within-chart separation at
stake. Light only: this page gets printed and pasted into documents, where a
themed surface is a liability rather than a feature.
