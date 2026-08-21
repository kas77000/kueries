# scripts/lib

Shared pieces that are not about any one report: the page they are drawn on,
and the mail that carries them. Everything under `scripts/` is
otherwise self-contained, one folder per script; this is the exception, for the
things a second script would otherwise copy.

Not a package to install. Scripts reach it by putting `scripts/` on the path:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import mailer
```

If you copy a script folder to another machine, copy this one beside it.

---

## `report_page.py`

The A4 page these reports are made of: the palette, a hand-drawn table, a KPI
row, and horizontal and vertical bar charts. It knows nothing about orders,
markets or kdb — a caller passes text and numbers and gets marks on a page.

```python
from lib.report_page import figure, heading, kpis, table, barchart, save, INK, BLUE

fig = figure()                                   # blank A4 portrait
heading(fig, "My Report", "By market · 2026-07-24")
kpis(fig, [("732", "Orders", INK)], 0.884)
table(fig, COLS, rows, y_top=0.808, row_h=0.040)
barchart(fig, (L, 0.195, 0.405, 0.265), "By market", labels, values, texts, BLUE)
save(fig, out_dir, "my_report")                  # .pdf + .png
```

```
python scripts/lib/report_page.py --self-test
```

**Why it is a library.** The second report wanted the same page as the first,
and two copies of a layout drift the moment one of them is corrected. What is
in here is the part that is genuinely the same. The *layout* — where the bands
sit, how tall the rows are, what goes in the columns — stays in each report,
because that is the part that legitimately differs.

**Not a grid.** The page is a document rather than a plot: a title block, a
rule, a KPI row, a table, then charts. Only the bars live in an axes, and every
position is a figure fraction, so a caller reads like a layout rather than like
a chain of subplot calls.

- `table()` takes `[(label, width fraction, right_aligned)]` and rows of
  `(text, colour, weight)` **per cell**, so the caller decides what is
  emphasised without this module knowing why. It returns the y it ended at.
- `barchart()` / `vbarchart()` draw no axes, no grid and no ticks — every bar
  carries a direct label, which for a handful of categories is more precise
  than an axis and less furniture.
- `save()` takes one figure, or a list for a multi-page PDF — in which case it
  also writes `stem_p1.png`, `stem_p2.png`… PNG has no concept of a page, and
  silently writing only the first one is how a second page goes unnoticed.
- The palette is the validated data-viz reference set, light only: these pages
  get printed and pasted into documents, where a themed surface is a liability.

Used by [`short_sell_report`](../short_sell_report/README.md) and
[`luld_report`](../luld_report/README.md).

---

## `order_chains.py`

Putting a rejected-and-replaced order back together. The engine writes a **new
`id_target`** every time an order is re-sent, so counting target rows counts one
economic order several times and multiplies its size.

```python
from lib.order_chains import chain_key, chain_size, fix_tag

cid = fix_tag(row["fixmsg"])                     # the client's own order id
key = chain_key(date, cid, id_server, id_target)
qty = chain_size(sizes, fills, "asked")          # what the chain asked for
```

```
python scripts/lib/order_chains.py --self-test
```

**The rule is here; the records are not.** Each report has its own idea of what
an attempt carries and what it wants to say about one, and forcing those into a
shared shape would buy nothing. What is shared is what must not drift: how to
read the tag, what makes two rows one order, and what quantity that order asked
for.

- **`fix_tag`** splits `fixmsg` into fields and compares the *whole* tag, so
  `19604=`, `96040=` and a `9604=` inside another field's value are ignored. The
  separator is `;` (also SOH and `|`); a **caret is not** — it appears inside
  values in this feed.
- **`chain_key`** leaves `id_server` out: a trader can move an order to another
  server and it is still the same order. A target with no 9604 keys on its own
  server *and* `id_target`, so untagged orders never merge with each other.
- **`chain_size`** defaults to `"asked"` — every attempt's fills plus what the
  last one still had to do. It is the only rule that handles both a
  **replacement** (3×27m that never traded is 27m) and a **top-up** (900, 1700,
  2500 filling 3,600 asked for 5,100), and it cannot produce a completion over
  100%.

Used by [`short_sell_report`](../short_sell_report/README.md) and
[`luld_report`](../luld_report/README.md).

---

## `mailer.py`

Send a report to a list of people: a plain text body, an HTML body with the
charts inline, and the PDF attached.

```python
from lib.mailer import Mail, Smtp, build_message, send

msg = build_message(Mail(
    subject="Short-Sell Order Report - 2026-07-24",
    sender="algo-reports@example.com",
    to="desk@example.com, compliance@example.com",   # or a list
    cc=["risk@example.com"],
    text=plain_body,
    html=html_body,
    inline_images=[("report-page", Path("report.png"))],   # <img src="cid:report-page">
    attachments=[Path("report.pdf")]))

send(msg, Smtp(host="mail.example.com", port=25))
```

```
python scripts/lib/mailer.py --self-test
```

### Two steps on purpose

`build_message()` is pure — it touches the filesystem to read the attachments,
and nothing else. `send()` is the only function that opens a socket. So a
caller's own `--self-test` can cover the message it actually sends, and
`send(msg, smtp, dry_run=True)` proves the recipient list and the attachments
before anything leaves the machine.

### What it takes care of

- **Recipient lists as people actually paste them.** Commas, semicolons or
  newlines; a list, or one string, or a list of strings each holding several.
  `Name <a@b.com>` survives intact. Nobody is mailed twice.
- **Addresses that do not parse raise**, rather than being dropped. A recipient
  list quietly one short is the failure that goes unnoticed for months.
- **Bcc on the envelope, never in a header** — which is the whole point of it.
- **The message shape every client handles**: `text/plain` and `text/html` as
  alternatives, inline images *related* to the HTML that references them, real
  attachments beside both. The plain text part is required and the HTML one is
  not, so a client that will not render HTML still gets the numbers.
- **Attachment size checked** against a 20MB ceiling, because most gateways
  bounce past it and a loud failure here beats a silent one there.
- **`html_table()` / `text_table()`** — the same table for both bodies, with
  inline styles only, since mail clients strip `<style>` blocks and half of them
  strip `<head>` with it. `html_table` takes per cell colours, so a caller can
  pick out the numbers that matter without this module knowing why they matter.

### The server

`Smtp(host, port, timeout)` — and nothing else. The port defaults to 25 and the
timeout to 30s.

**No credentials, no STARTTLS.** An internal relay that accepts mail from the
host it runs on has nothing to authenticate with, and an auth path nobody
exercises is a path that is broken by the time somebody needs it. `send()` is
therefore exactly `smtplib.SMTP(host, port, timeout=…)` and one `send_message`.
Adding auth back is a `login()` call and two more fields.

### What it deliberately does not do

No retry loop, no queue, no bounce handling. A report mailer that swallows a
failure is worse than one that raises: the run is cheap to repeat, and a report
nobody received is only harmless if somebody knows it was not received.

Standard library only — `smtplib` and `email`.

Used by [`short_sell_report`](../short_sell_report/README.md) and
[`luld_report`](../luld_report/README.md).
