# scripts/lib

Shared pieces that are not about any one report. Everything under `scripts/` is
otherwise self-contained, one folder per script; this is the exception, for the
things a second script would otherwise copy.

Not a package to install. Scripts reach it by putting `scripts/` on the path:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib import mailer
```

If you copy a script folder to another machine, copy this one beside it.

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

send(msg, Smtp.from_env())
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

### Configuration

`Smtp.from_env()` reads `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
`SMTP_STARTTLS` and `SMTP_FROM`, and anything passed explicitly wins over the
environment. The port defaults to 587 with STARTTLS and 25 without. Keeping
credentials in the environment is what keeps a password out of shell history.

### What it deliberately does not do

No retry loop, no queue, no bounce handling. A report mailer that swallows a
failure is worse than one that raises: the run is cheap to repeat, and a report
nobody received is only harmless if somebody knows it was not received.

Standard library only — `smtplib` and `email`.

Used by [`scripts/short_sell_report`](../short_sell_report/README.md).
