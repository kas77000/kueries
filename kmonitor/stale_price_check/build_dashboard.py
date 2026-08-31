"""Build the KdbMonitor dashboard export from stale_price_check_kmonitor.q.

The q is the source of truth; this only carries it into the JSON KdbMonitor
imports, so the two cannot drift. Run it after any edit to the .q:

    python build_dashboard.py

Writes stale_price_check_kmonitor_dashboard.json beside it. Import that in
KdbMonitor under Dashboards -> Import.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "stale_price_check_kmonitor.q"
TARGET = HERE / "stale_price_check_kmonitor_dashboard.json"

# / ==== DATASET: name | env=ENV ====  ...q...  / ==== END ====
BLOCK = re.compile(
    r"^/ ==== DATASET:\s*(\w+)\s*\|\s*env=(\w+)\s*====\s*$(.*?)^/ ==== END ====\s*$",
    re.M | re.S,
)

NAMES = ("live_takes", "touch_check")


def blocks() -> dict[str, tuple[str, str]]:
    """Every dataset block in the .q, as {name: (env, qsql)}, in file order."""
    found = {m.group(1): (m.group(2), m.group(3).strip())
             for m in BLOCK.finditer(SOURCE.read_text(encoding="utf-8"))}
    missing = set(NAMES) - set(found)
    if missing:
        raise SystemExit(f"{SOURCE.name} is missing block(s): {sorted(missing)}")
    return found


def dataset(name: str, env: str, qsql: str) -> dict:
    return {
        "name": name,
        "env": env,
        # PINNED, not inherited: both datasets are real-time and there is no
        # historical branch in the q for them to fall back to.  The dashboard
        # offers real-time alone as well, so this is belt and braces.
        "time_mode": "realtime",
        "time_context": None,
        "mode": "raw",
        "table": "",
        "filters": [],
        "raw_qsql": qsql,
        "extra_connections": [],
        "transforms": [],
        "max_rows": 5000,
        "static": False,
        "source": "kdb",
        "shape": None,
        "file_label": "",
    }


def parameters() -> list:
    return [
        {
            "name": "lookback_mins",
            "label": "Lookback (minutes)",
            "kind": "number",
            "choices": [],
            "dataset": "",
            "column": "",
            "default": "10",
            "q_type": "number",
            "help": ("How recently the workorder was created. This is what "
                     "keeps the query runnable on a refresh - reading the "
                     "whole session out of qatt is too slow. qatt itself is "
                     "read from twice this far back, so an order at the start "
                     "of the window still has prints before it to land on."),
            "required": True,
            "pattern": "",
            "pattern_message": "",
            "minimum": "1",
            "maximum": "",
            "integer": True,
            "weekdays_only": False,
        },
        {
            "name": "max_ticks",
            "label": "Max ticks off the touch",
            "kind": "number",
            "choices": [],
            "dataset": "",
            "column": "",
            "default": "5",
            "q_type": "number",
            "help": ("How far off the touch a take order may sit before it "
                     "counts. A buy is measured against the ask and a sell "
                     "against the bid, in ticks of that stock's own ticksize. "
                     "0 means it must be exactly on the touch."),
            "required": True,
            "pattern": "",
            "pattern_message": "",
            "minimum": "0",
            "maximum": "",
            "integer": False,
            "weekdays_only": False,
        },
    ]


def build() -> dict:
    q = blocks()
    return {
        "kind": "kdbmonitor-export",
        "version": 2,
        "exported_at": None,
        "connections": [],
        "alerts": [],
        "dashboards": [{
            "id": None,
            "name": "Stale price check - take orders vs the touch",
            "description": (
                "Take orders that were NOT sitting on the touch they should "
                "have been. A take lifts the offer or hits the bid, so its "
                "price is dictated by the book rather than chosen - sent at "
                "anything else, the book the algo saw was not the book that "
                "existed. Aggressive orders (a buy above the offer, a sell "
                "below the bid) are excluded: they cross and fill anyway. "
                "Short sales are excluded. Only breaches come back, so an "
                "EMPTY TABLE IS THE GOOD ANSWER. REAL-TIME ONLY: both "
                "datasets run against their environment's live server and "
                "mean today. Source of truth is "
                "stale_price_check_kmonitor.q; regenerate this file with "
                "build_dashboard.py rather than editing it."
            ),
            "group": "Market structure",
            "refresh_secs": 30,
            "periods": "realtime",
            "orientation": "landscape",
            "source": "kdb",
            "time_context": {"mode": "realtime"},
            "parameters": parameters(),
            "datasets": [dataset(n, *q[n]) for n in NAMES],
            "rows": [
                {
                    "widgets": [
                        {"type": "kpi", "dataset": "touch_check",
                         "title": "Orders off the touch",
                         "spec": {"column": "id_work", "agg": "count",
                                  "fmt": ",.0f",
                                  "thresholds": [{"op": ">", "value": 0,
                                                  "color": "critical"}]},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "touch_check",
                         "title": "Names affected",
                         "spec": {"column": "sym", "agg": "nunique",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "touch_check",
                         "title": "Worst",
                         "spec": {"column": "ticks_abs", "agg": "max",
                                  "fmt": ",.1f", "suffix": " ticks"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "touch_check",
                         "title": "Oldest quote at t_gen",
                         "spec": {"column": "quote_age_ms", "agg": "max",
                                  "fmt": ",.0f", "suffix": " ms"},
                         "width": 1.0},
                    ],
                    "height_in": 0.9,
                    "gap_above_in": 0.0,
                    "col_gap_in": 0.1,
                },
                {
                    "widgets": [
                        {"type": "table", "dataset": "touch_check",
                         "title": ("Take orders that missed the touch "
                                   "they should have been on"),
                         "spec": {
                             "columns": ["date", "sym", "side", "ref_side",
                                         "venue", "state", "trader",
                                         "id_target", "id_work", "size",
                                         "t_gen", "order_price", "qbid",
                                         "qask", "touch", "ticksize",
                                         "ticks_off", "ptime", "quote_age_ms",
                                         "now_age_ms", "flag"],
                             "labels": {
                                 "sym": "Stock", "side": "Side",
                                 "ref_side": "Measured vs", "venue": "Venue",
                                 "state": "Child state", "trader": "Trader",
                                 "id_target": "Parent", "id_work": "Child",
                                 "size": "Order qty", "t_gen": "Generated",
                                 "order_price": "Order price",
                                 "qbid": "Bid", "qask": "Ask",
                                 "touch": "Touch", "ticksize": "Tick",
                                 "ticks_off": "Ticks off",
                                 "ptime": "Quote time",
                                 "quote_age_ms": "Quote age (ms)",
                                 "now_age_ms": "Since last quote (ms)",
                                 "flag": "Verdict",
                             },
                             "formats": {"size": ",.0f",
                                         "order_price": ",.4f",
                                         "qbid": ",.4f", "qask": ",.4f",
                                         "touch": ",.4f", "ticksize": ",.4f",
                                         "ticks_off": ",.1f",
                                         "quote_age_ms": ",.0f",
                                         "now_age_ms": ",.0f"},
                             # Only "critical", "good", "blue", "ink", "ink2",
                             # "muted" or a #hex resolve - anything else goes
                             # to ink and reads as no highlight at all.
                             "highlight": [
                                 {"column": "flag", "op": "=",
                                  "value": "off", "color": "critical"},
                                 {"column": "flag", "op": "=",
                                  "value": "noquote", "color": "blue"},
                                 {"column": "flag", "op": "=",
                                  "value": "notick", "color": "blue"},
                             ],
                         },
                         "width": 1.0},
                    ],
                    "height_in": 6.4,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.0,
                },
            ],
        }],
    }


if __name__ == "__main__":
    TARGET.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.name}")
