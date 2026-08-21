#!/usr/bin/env python3
"""
=============================================================================
local_config.py

Keep the servers and the mail OUT of the tracked file.

Every script here ships with placeholder constants - ORDER_SERVER_RT,
EMAIL_TO, SMTP_HOST - that have to be edited before a first run.  Editing them
in the script itself means the file you run is never the file in git, so a pull
conflicts, and a half-pulled tree is what produced four crashes in a row: a
draw() without a parameter, a mail_report() with the wrong signature, a totals()
that silently returned a different number, and a v2 that no longer exists.

Instead, put them in a local_settings.py beside the script:

    scripts/short_sell_report/local_settings.py     (git ignores it)

        ORDER_SERVER_RT = "prod-oms-1:5012"
        ORDER_SERVER_HIST = "prod-oms-hist:5010"
        EMAIL_TO = ["desk@example.com"]
        EMAIL_FROM = "algo-reports@example.com"
        SMTP_HOST = "mail.example.com"

and the script picks them up.  Now `git pull` is always clean and the settings
survive it.

STRICT ON PURPOSE.  A name the script does not define is an ERROR, not a new
setting: `EMAIL_T0 = [...]` with a zero would otherwise sit there doing nothing
while the report quietly went to no one.
=============================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

LOCAL_NAME = "local_settings.py"


def local_path(script_file) -> Path:
    return Path(script_file).resolve().parent / LOCAL_NAME


def apply_local(module_globals, script_file, quiet=False) -> list:
    """Override this module's constants from local_settings.py beside it.

    Returns the names it changed.  Missing file is fine and is the normal case
    for a fresh checkout - the placeholders then fail loudly on their own.
    """
    path = local_path(script_file)
    if not path.is_file():
        return []

    ns = {}
    try:
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"),
             {"__file__": str(path), "__builtins__": __builtins__}, ns)
    except Exception as e:                       # noqa: BLE001 - report and stop
        raise SystemExit(f"{path}: {type(e).__name__}: {e}")

    changed, unknown = [], []
    for k, v in ns.items():
        if k.startswith("_"):
            continue
        if k not in module_globals:
            unknown.append(k)
            continue
        module_globals[k] = v
        changed.append(k)

    if unknown:
        raise SystemExit(
            f"{path} sets {', '.join(sorted(unknown))}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not a setting this script "
            f"has. Check the spelling - a name that does nothing is worse than "
            f"one that errors.")

    if changed and not quiet:
        #  say WHICH, never the values: a password does not belong in a log even
        #  when this script has no password to print
        print(f"  {path.name}: {', '.join(sorted(changed))}",
              file=sys.stderr, flush=True)
    return changed


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

    print("local_config --self-test\n")
    with tempfile.TemporaryDirectory() as d:
        script = Path(d) / "report.py"
        script.write_text("# pretend script", encoding="utf-8")
        local = Path(d) / LOCAL_NAME

        g = {"ORDER_SERVER_RT": "CHANGEME:5012", "EMAIL_TO": [], "DPI": 200}
        check("no local file is fine, and changes nothing",
              apply_local(g, script, quiet=True), [])
        check("the placeholders are untouched",
              g["ORDER_SERVER_RT"], "CHANGEME:5012")

        local.write_text('ORDER_SERVER_RT = "prod:5012"\n'
                         'EMAIL_TO = ["desk@example.com"]\n', encoding="utf-8")
        changed = apply_local(g, script, quiet=True)
        check("it overrides what it names", sorted(changed),
              ["EMAIL_TO", "ORDER_SERVER_RT"])
        check("the server", g["ORDER_SERVER_RT"], "prod:5012")
        check("the recipients", g["EMAIL_TO"], ["desk@example.com"])
        check("and leaves everything else alone", g["DPI"], 200)

        local.write_text('_helper = 1\nDPI = 300\n', encoding="utf-8")
        apply_local(g, script, quiet=True)
        check("an underscore name is a local of the settings file, not a "
              "setting", "_helper" in g, False)
        check("a real setting still lands", g["DPI"], 300)

        local.write_text('EMAIL_T0 = ["desk@example.com"]\n', encoding="utf-8")
        raised = ""
        try:
            apply_local(g, script, quiet=True)
        except SystemExit as e:
            raised = str(e)
        check("a typo'd name is an ERROR, not a setting that does nothing",
              "EMAIL_T0" in raised and "not a setting" in raised, True)

        local.write_text('ORDER_SERVER_RT = \n', encoding="utf-8")
        raised = ""
        try:
            apply_local(g, script, quiet=True)
        except SystemExit as e:
            raised = str(e)
        check("a broken settings file names itself and stops",
              LOCAL_NAME in raised and "SyntaxError" in raised, True)

        local.write_text('ORDER_SERVER_RT = "prod:5012"\n', encoding="utf-8")
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            apply_local(g, script)
        said = buf.getvalue()
        check("it says which names it took", "ORDER_SERVER_RT" in said, True)
        check("and NEVER the values", "prod:5012" in said, False)

    print("\n" + ("all checks passed" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    print(__doc__)
