# launchers

Double-click a report instead of remembering a command line, and give Task
Scheduler something to call at 19:00. One `.cmd` per report; they all go through
`_run.cmd`, which is the only file that knows how a report is actually started.

```
launchers/
  run_luld_orders.cmd          the LULD order report, today
  run_short_sell_report.cmd    the short sell report, today
  run_all.cmd                  both, one after the other, one prompt
  _run.cmd                     the shared launcher.  Not for double clicking
  local_settings.cmd.example   copy to local_settings.cmd and edit
  logs/                        written on the first scheduled run
```

Everything is resolved from the folder's own location (`%~dp0`), so the repo can
sit anywhere and neither mode depends on a working directory the scheduler
happens to hand us. Move the folder *out* of the repo and it stops working — it
looks for `..\scripts\<name>\<name>.py`.

## Before the first run

The reports still need their own servers and recipients, in the
`local_settings.py` beside each one — see
[`scripts/lib/README.md`](../scripts/lib/README.md). The launcher does not set those and
cannot: a placeholder server fails loudly on connect, which is the point.

What the launcher itself needs is a python, and it guesses: `py -3`, or `python`
on a machine with no `py` launcher. If that guess is wrong — a venv, or several
installs — copy `local_settings.cmd.example` to **`local_settings.cmd`** (git
ignores it) and set one line:

```bat
set "PYTHON=C:\venvs\kdb\Scripts\python.exe"
```

Same file moves the logs (`LOG_DIR`) and changes how long they are kept
(`KEEP_LOG_DAYS`, default 30 days).

## Double clicking

The window stays open. Output is on screen as it happens, and at the end it
says `done.` or `FAILED - exit code N` and waits, so a traceback can be read
rather than glimpsed.

`run_all.cmd` waits **once, at the end**, not after each report. Both run
straight through and the prompt comes after the second, under a line saying
whether either failed. The first report's output has not gone anywhere — scroll
back for it.

Arguments still work, from a prompt or from a shortcut:

```bat
run_luld_orders.cmd --date 2026-08-21 --no-email
run_short_sell_report.cmd --monthly 2026-07
```

## The `scheduled` word

Task Scheduler has no console to print to. Called as

```bat
run_luld_orders.cmd scheduled
```

the same file writes everything to `logs\luld_orders_YYYYMMDD_HHmmss.log`
instead, does not pause, and **passes the script's exit code back** — that is
what the scheduler records as *Last Run Result*, and it is the difference
between a task that failed and a task that looks fine forever. Anything after
that first word is still handed to the script.

## The `nopause` word

The same idea, one step smaller: run interactively, output on screen as usual,
but **do not wait at the end**.

```bat
run_luld_orders.cmd nopause
```

This is what `run_all.cmd` passes to each report so the prompt happens once
rather than twice. On its own it is rarely what you want from a double click —
the window closes the instant the report ends and takes the traceback with it —
but it is the right thing when something else is doing the waiting, or when a
report is one step of a longer script.

Like `scheduled`, the word can sit anywhere on the line and is **not** handed to
the python script. `scheduled` implies it: nothing waits under Task Scheduler,
where there is no console and nobody to press the key, and a pause there is a
task that hangs until it is killed.

## Task Scheduler, 19:00 Monday to Friday

Two tasks, ten minutes apart. They read the same order server, and a report
that is late is better than two that fought over the same process. (Or one task
on `run_all.cmd`, which serialises them itself.)

The quick way — a Command Prompt, one line per report (elevate it if it
complains about access):

```bat
schtasks /Create /TN "kdb-reports\luld_orders" ^
  /TR "C:\Users\user\Desktop\Projects\kdb-queries\launchers\run_luld_orders.cmd scheduled" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 19:00 /F

schtasks /Create /TN "kdb-reports\short_sell_report" ^
  /TR "C:\Users\user\Desktop\Projects\kdb-queries\launchers\run_short_sell_report.cmd scheduled" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 19:10 /F
```

That creates them to run **as the logged-on user, only while logged on**. To
have them run with the machine merely switched on, add `/RU <user> /RP *` and
type the password when asked — or use the GUI, below, which is also where the
settings that actually matter live.

### The GUI, and why not "Basic Task"

`taskschd.msc` → **Create Task…** (not *Create Basic Task* — that one cannot
set "run whether the user is logged on or not", and cannot set the conditions
that stop a laptop from skipping the run).

- **General** — name `luld_orders`. Pick **Run whether user is logged on or
  not**. Leave *Run with highest privileges* alone; nothing here needs admin.
- **Triggers → New** — **Weekly**, recur every `1` week, tick **Mon Tue Wed Thu
  Fri**, start `19:00`. Leave the date as today.
- **Actions → New** — *Start a program*.
  - Program/script:
    `C:\Users\user\Desktop\Projects\kdb-queries\launchers\run_luld_orders.cmd`
  - Add arguments: `scheduled`
  - Start in: `C:\Users\user\Desktop\Projects\kdb-queries\launchers`
- **Conditions** — untick **Start the task only if the computer is on AC
  power**, or a laptop on battery silently skips the report. Tick *Wake the
  computer to run this task* if it sleeps before 19:00.
- **Settings** — tick **Run task as soon as possible after a scheduled start is
  missed**, tick *If the task fails, restart every* `5 minutes`, *up to* `3`
  times, and set **Stop the task if it runs longer than** `2 hours`.

Then the same again for `short_sell_report` at 19:10.

A run whether-logged-on-or-not happens in session 0: there is no window and
nothing on screen even if you are sitting there. That is exactly why the
scheduled mode logs.

### Checking it

- **Last Run Result `0x0`** is a clean run. `0x1` or `0x2` is the script
  saying no — the log says which.
- The log is the whole of it: the command line, the script's own output, and
  `=== exit code N ===` on the last line.
- To test the task without waiting for 19:00: right-click it → **Run**. It runs
  the real report and mails it, so use a day you do not mind mailing, or add
  `--no-email` to the arguments for the test and take it out afterwards.

## Adding another report

Copy `run_luld_orders.cmd`, change the one `set "SCRIPT=..."` line to the
script's folder name, and change the comment. That is all — `_run.cmd` finds
`..\scripts\<name>\<name>.py` from it.

`reversion_liquidity` and `dark_routed_executed` are deliberately not here:
both want an explicit `--start` and `--end`, so there is no sensible "today"
for a 19:00 task to run. Launch those from a prompt, or write a `.cmd` that
pins the range you actually want.
