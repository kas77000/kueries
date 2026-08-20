#!/usr/bin/env python3
"""
=============================================================================
mailer.py

Send a report to a list of people: a plain text body, an HTML body with the
charts inline, and the PDF attached.  Nothing in here knows anything about any
particular report, so the next script that needs to mail one imports it as it
stands.

  from lib.mailer import Mail, Smtp, build_message, send

  msg = build_message(Mail(
      subject="Short-Sell Order Report - 2026-07-24",
      sender="algo-reports@example.com",
      to=["desk@example.com", "compliance@example.com"],
      text="732 short-sell orders, 55.7% complete, 394 rejections.",
      html="<p>...</p><img src='cid:page'>",
      inline_images=[("page", Path("short_sell_report_2026-07-24.png"))],
      attachments=[Path("short_sell_report_2026-07-24.pdf")]))
  send(msg, Smtp(host="smtp.example.com", port=25))

TWO STEPS ON PURPOSE.  build_message() is pure - it touches the filesystem to
read the attachments and nothing else - so a caller can assemble a message,
check it, and print it without a mail server anywhere in reach.  send() is the
only function that opens a socket.  Every caller's --self-test can therefore
cover the message it actually sends.

  python scripts/lib/mailer.py --self-test

CONFIGURATION.  Smtp() carries the server; its fields default from the
environment (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_STARTTLS,
SMTP_FROM) so a password never has to be committed or passed on a command line
where it would land in shell history.

WHAT THIS DELIBERATELY DOES NOT DO.  No retry loop, no queue, no bounce
handling.  A report mailer that silently swallows a failure is worse than one
that raises: the run is cheap to repeat, and a report nobody received is only
harmless if somebody knows it was not received.
=============================================================================
"""

from __future__ import annotations

import mimetypes
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import NamedTuple, Optional, Sequence


# A deliberately permissive check.  It is here to catch a truncated address, a
# stray comma or a name that was never turned into an address - not to decide
# what the RFC allows, which no regexp does correctly and which the receiving
# server settles anyway.
_ADDR = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

MAX_ATTACHMENT_MB = 20      # most gateways bounce past this; fail loudly first


class Smtp(NamedTuple):
    """Where to send.  Every field falls back to the environment."""
    host: str = ""
    port: int = 0
    user: Optional[str] = None
    password: Optional[str] = None
    starttls: bool = False
    timeout: int = 30

    @classmethod
    def from_env(cls, **overrides) -> "Smtp":
        env = {
            "host": os.environ.get("SMTP_HOST", ""),
            "port": int(os.environ.get("SMTP_PORT") or 0),
            "user": os.environ.get("SMTP_USER") or None,
            "password": os.environ.get("SMTP_PASSWORD") or None,
            "starttls": _truthy(os.environ.get("SMTP_STARTTLS")),
        }
        env.update({k: v for k, v in overrides.items() if v not in (None, "", 0)})
        return cls(**env)

    def resolved_port(self) -> int:
        return self.port or (587 if self.starttls else 25)


class Mail(NamedTuple):
    """One message.  to/cc/bcc take a list of addresses, or a comma or
    semicolon separated string - both are what people actually paste."""
    subject: str
    sender: str
    to: Sequence
    text: str
    html: Optional[str] = None
    cc: Sequence = ()
    bcc: Sequence = ()
    reply_to: Optional[str] = None
    attachments: Sequence = ()
    inline_images: Sequence = ()     # [(cid, path), ...], referenced as cid:<cid>


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_addresses(value) -> list:
    """A list of addresses from a list, or from one pasted string.

    Accepts commas, semicolons and whitespace as separators, keeps
    'Name <a@b.com>' intact, drops empties, and de-duplicates while keeping the
    order given - a report that reaches someone twice reads as two reports.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items = re.split(r"[,;\n]+", value)
    else:
        items = []
        for v in value:
            items.extend(re.split(r"[,;\n]+", v) if isinstance(v, str) else [v])
    out, seen = [], set()
    for raw in items:
        a = str(raw).strip()
        if not a:
            continue
        key = parseaddr(a)[1].lower() or a.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def validate_addresses(addrs, what="recipient") -> list:
    """Check every address parses to something that looks like one.

    Raises on the first bad one rather than dropping it.  A recipient list
    quietly one short is exactly the failure that goes unnoticed for months.
    """
    clean = parse_addresses(addrs)
    for a in clean:
        addr = parseaddr(a)[1]
        if not _ADDR.match(addr or ""):
            raise ValueError(f"{what} {a!r} does not look like an email address")
    return clean


def _attach_file(msg: EmailMessage, path: Path, cid: Optional[str] = None):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"cannot attach {path}: not a file")
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_ATTACHMENT_MB:
        raise ValueError(f"{path.name} is {size_mb:.1f}MB, over the "
                         f"{MAX_ATTACHMENT_MB}MB limit")
    ctype, _ = mimetypes.guess_type(path.name)
    maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
    kw = {"maintype": maintype, "subtype": subtype, "filename": path.name}
    if cid is not None:
        kw["cid"] = f"<{cid}>"
        kw["disposition"] = "inline"
    msg.add_attachment(path.read_bytes(), **kw)


def build_message(mail: Mail) -> EmailMessage:
    """Assemble the message.  Reads the attachments; opens no socket.

    The shape is the one every mail client handles without argument: a
    text/plain part and a text/html part as alternatives, the inline images
    related to the HTML, and the real attachments beside both.  A client that
    refuses HTML still gets a readable report rather than an empty body, which
    is why the text part is required and the HTML one is not.
    """
    if not str(mail.subject).strip():
        raise ValueError("the message needs a subject")
    if not str(mail.text).strip():
        raise ValueError("the message needs a plain text body - a client that "
                         "will not render HTML must still get the numbers")
    sender = validate_addresses([mail.sender], "sender")
    if not sender:
        raise ValueError("the message needs a sender")
    to = validate_addresses(mail.to, "recipient")
    cc = validate_addresses(mail.cc, "cc")
    bcc = validate_addresses(mail.bcc, "bcc")
    if not (to or cc or bcc):
        raise ValueError("the message needs at least one recipient")

    msg = EmailMessage()
    msg["Subject"] = mail.subject
    msg["From"] = sender[0]
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if mail.reply_to:
        msg["Reply-To"] = validate_addresses([mail.reply_to], "reply-to")[0]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    msg.set_content(mail.text)
    if mail.html:
        msg.add_alternative(mail.html, subtype="html")
        for cid, path in mail.inline_images:
            # onto the text/html part, so the image is `related` to the markup
            # that references it rather than a loose attachment beside it
            _attach_file(msg.get_payload()[-1], Path(path), cid=cid)
    elif mail.inline_images:
        raise ValueError("inline_images need an html body to be referenced from")

    for path in mail.attachments:
        _attach_file(msg, Path(path))

    # Bcc is carried on the envelope, never in a header - the whole point of it
    msg._mailer_envelope = to + cc + bcc      # read by send()
    return msg


def recipients(msg: EmailMessage) -> list:
    """Every envelope recipient, bcc included."""
    got = getattr(msg, "_mailer_envelope", None)
    if got:
        return list(got)
    out = []
    for header in ("To", "Cc"):
        out.extend(parse_addresses(msg.get(header, "")))
    return out


def send(msg: EmailMessage, smtp: Smtp, dry_run: bool = False) -> list:
    """Send it.  Returns the envelope recipients it was handed to.

    dry_run does everything except open the socket, so a caller can prove the
    message, the recipient list and the attachments are right before anything
    leaves the machine.
    """
    rcpt = recipients(msg)
    if not rcpt:
        raise ValueError("no envelope recipients")
    if dry_run:
        return rcpt
    if not smtp.host:
        raise ValueError("no SMTP host: pass one, or set SMTP_HOST")
    with smtplib.SMTP(smtp.host, smtp.resolved_port(), timeout=smtp.timeout) as s:
        s.ehlo()
        if smtp.starttls:
            s.starttls()
            s.ehlo()
        if smtp.user:
            s.login(smtp.user, smtp.password or "")
        s.send_message(msg, from_addr=parseaddr(msg["From"])[1], to_addrs=rcpt)
    return rcpt


def describe(msg: EmailMessage) -> str:
    """One line per thing a caller would want to check before sending."""
    parts = [f"    from     {msg['From']}",
             f"    to       {msg.get('To', '-')}"]
    if msg.get("Cc"):
        parts.append(f"    cc       {msg['Cc']}")
    parts.append(f"    subject  {msg['Subject']}")
    names = [p.get_filename() for p in msg.walk() if p.get_filename()]
    if names:
        parts.append(f"    files    {', '.join(names)}")
    return "\n".join(parts)


# =============================================================================
# HTML
#
# A minimal table builder, because every report that mails itself wants the same
# thing: the headline numbers, one table, and the chart underneath.  Inline
# styles only - mail clients strip <style> blocks, and half of them strip <head>
# with it.
# =============================================================================

_ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def esc(s) -> str:
    return "".join(_ESC.get(c, c) for c in str(s))


def html_table(headers, rows, align=None, colours=None) -> str:
    """An HTML table with inline styles.

    align is per column, "l" or "r".  colours is a list per row of per cell
    colours (None to leave a cell alone), so a caller can pick out the numbers
    that matter without this module knowing why they matter.
    """
    align = align or ["l"] * len(headers)
    out = ['<table cellspacing="0" cellpadding="0" style="border-collapse:'
           'collapse;font:13px system-ui,-apple-system,Segoe UI,Arial,sans-serif">']
    out.append("<tr>")
    for h, a in zip(headers, align):
        out.append(f'<th style="background:#3a3835;color:#ffffff;font-weight:600;'
                   f'padding:7px 12px;text-align:{"right" if a == "r" else "left"}">'
                   f"{esc(h)}</th>")
    out.append("</tr>")
    for i, row in enumerate(rows):
        out.append("<tr>")
        for j, (cell, a) in enumerate(zip(row, align)):
            colour = (colours[i][j] if colours and colours[i][j] else "#0b0b0b")
            out.append(f'<td style="border-bottom:1px solid #e1e0d9;color:{colour};'
                       f'padding:7px 12px;text-align:'
                       f'{"right" if a == "r" else "left"}">{esc(cell)}</td>')
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def text_table(headers, rows, align=None) -> str:
    """The same table as fixed width text, for the text/plain part."""
    align = align or ["l"] * len(headers)
    cols = [[str(h)] + [str(r[i]) for r in rows] for i, h in enumerate(headers)]
    w = [max(len(v) for v in col) for col in cols]
    def line(vals):
        return "  ".join(v.rjust(w[i]) if align[i] == "r" else v.ljust(w[i])
                         for i, v in enumerate(vals))
    out = [line([str(h) for h in headers]), "  ".join("-" * n for n in w)]
    out += [line([str(v) for v in r]) for r in rows]
    return "\n".join(out)


# =============================================================================
# SELF TEST
# =============================================================================

def self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'}  {name}"
              + ("" if good else f"   got {got!r}, want {want!r}"))

    def raises(name, fn, fragment=""):
        nonlocal ok
        try:
            fn()
        except Exception as e:
            good = fragment in str(e)
            ok = ok and good
            print(f"  {'ok  ' if good else 'FAIL'}  {name}"
                  + ("" if good else f"   raised {e!r}, wanted {fragment!r}"))
            return
        ok = False
        print(f"  FAIL  {name}   did not raise")

    print("mailer --self-test\n\naddresses")
    check("a comma separated string", parse_addresses("a@b.com, c@d.com"),
          ["a@b.com", "c@d.com"])
    check("semicolons too, as Outlook pastes them",
          parse_addresses("a@b.com; c@d.com"), ["a@b.com", "c@d.com"])
    check("a list of strings, each possibly a list",
          parse_addresses(["a@b.com", "c@d.com,e@f.com"]),
          ["a@b.com", "c@d.com", "e@f.com"])
    check("a display name survives",
          parse_addresses("Desk <a@b.com>"), ["Desk <a@b.com>"])
    check("nobody is mailed twice",
          parse_addresses("a@b.com, A@B.com, c@d.com"), ["a@b.com", "c@d.com"])
    check("blanks and stray separators go", parse_addresses("a@b.com,,  ,"),
          ["a@b.com"])
    check("None is empty, not an error", parse_addresses(None), [])
    raises("a truncated address raises", lambda: validate_addresses(["a@b"]),
           "does not look like")
    raises("a bare name raises", lambda: validate_addresses(["compliance"]),
           "does not look like")

    print("\nmessage")
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "report.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 500)
        png = Path(d) / "page.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 500)

        m = build_message(Mail(
            subject="Short-Sell Order Report - 2026-07-24",
            sender="algo-reports@example.com",
            to="desk@example.com, compliance@example.com",
            cc=["risk@example.com"],
            bcc=["archive@example.com"],
            text="732 orders, 55.7% complete, 394 rejections.",
            html='<p>732 orders</p><img src="cid:page">',
            inline_images=[("page", png)],
            attachments=[pdf]))

        check("subject", m["Subject"], "Short-Sell Order Report - 2026-07-24")
        check("To carries both", m["To"],
              "desk@example.com, compliance@example.com")
        check("Cc is a header", m["Cc"], "risk@example.com")
        check("Bcc is NOT a header", m["Bcc"], None)
        check("but it is an envelope recipient", recipients(m),
              ["desk@example.com", "compliance@example.com",
               "risk@example.com", "archive@example.com"])
        check("the pdf is attached",
              [p.get_filename() for p in m.walk() if p.get_filename()],
              ["page.png", "report.pdf"])
        check("the png is inline, not an attachment",
              [p.get_content_disposition() for p in m.walk()
               if p.get_filename() == "page.png"], ["inline"])
        check("and carries the cid the html references",
              [p["Content-ID"] for p in m.walk()
               if p.get_filename() == "page.png"], ["<page>"])
        check("there is a plain text alternative",
              any(p.get_content_type() == "text/plain" for p in m.walk()), True)
        check("the message serialises", m.as_bytes()[:5], b"Subje")
        check("dry_run reports the envelope without sending",
              send(m, Smtp(), dry_run=True), recipients(m))

        raises("no recipients raises",
               lambda: build_message(Mail("s", "a@b.com", [], "body")),
               "at least one recipient")
        raises("no subject raises",
               lambda: build_message(Mail("", "a@b.com", ["c@d.com"], "body")),
               "needs a subject")
        raises("an html-only body raises",
               lambda: build_message(Mail("s", "a@b.com", ["c@d.com"], "  ",
                                          html="<p>x</p>")),
               "plain text body")
        raises("an inline image with no html raises",
               lambda: build_message(Mail("s", "a@b.com", ["c@d.com"], "b",
                                          inline_images=[("page", png)])),
               "need an html body")
        raises("a missing attachment raises",
               lambda: build_message(Mail("s", "a@b.com", ["c@d.com"], "b",
                                          attachments=[Path(d) / "nope.pdf"])),
               "not a file")
        raises("sending with no host raises",
               lambda: send(m, Smtp()), "no SMTP host")

    print("\nsmtp config")
    os.environ["SMTP_HOST"] = "mail.example.com"
    os.environ["SMTP_PORT"] = ""
    os.environ["SMTP_STARTTLS"] = "yes"
    s = Smtp.from_env()
    check("host from the environment", s.host, "mail.example.com")
    check("starttls from the environment", s.starttls, True)
    check("port defaults to 587 with starttls", s.resolved_port(), 587)
    check("and to 25 without", Smtp(host="x").resolved_port(), 25)
    check("an explicit value beats the environment",
          Smtp.from_env(host="other.example.com").host, "other.example.com")
    del os.environ["SMTP_HOST"], os.environ["SMTP_PORT"], os.environ["SMTP_STARTTLS"]

    print("\ntables")
    t = text_table(["Market", "Orders"], [["Hong Kong", "109"], ["Japan", "541"]],
                   ["l", "r"])
    check("text table columns are as wide as their widest cell, header included",
          t.splitlines()[2], "Hong Kong     109")
    check("and the numbers right align under it",
          t.splitlines()[3], "Japan         541")
    h = html_table(["Market", "Orders"], [["Hong Kong", "109"]], ["l", "r"],
                   [[None, "#d03b3b"]])
    check("html table colours the cell it was told to",
          "#d03b3b" in h, True)
    check("and escapes its content", "&lt;" in html_table(["<b>"], [["x"]]), True)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
