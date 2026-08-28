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

NAMES = ("live_orders", "stale_check")


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
        "time_mode": "inherit",      # follows the period the reader picked
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
            "name": "min_dev_bps",
            "label": "Minimum deviation (bps)",
            "kind": "number",
            "choices": [],
            "dataset": "",
            "column": "",
            "default": "25",
            "q_type": "number",
            "help": ("How far the order price has to sit from the print qatt "
                     "had at t_gen before it counts. Set it to 0 to turn the "
                     "price test off and see every workorder - that is the "
                     "calibration run."),
            "required": True,
            "pattern": "",
            "pattern_message": "",
            "minimum": "0",
            "maximum": "",
            "integer": False,
            "weekdays_only": False,
        },
        {
            "name": "min_price_age_ms",
            "label": "Minimum print age (ms)",
            "kind": "number",
            "choices": [],
            "dataset": "",
            "column": "",
            "default": "5000",
            "q_type": "number",
            "help": ("How old the last print already was when the order was "
                     "generated before it counts. Set it to 0 to turn the age "
                     "test off. Thin names sit high on this one because they "
                     "are not trading, not because anything is stale."),
            "required": True,
            "pattern": "",
            "pattern_message": "",
            "minimum": "0",
            "maximum": "",
            "integer": True,
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
            "name": "Stale price check - workorders vs the tape",
            "description": (
                "Every workorder under an activated parent, with the price the "
                "algo gave it beside the last print qatt actually had for that "
                "name at the same instant. dev_bps is the gap; price_age_ms is "
                "how old that print already was. Both thresholds accept 0, "
                "which turns that test off and returns everything for "
                "calibration. Reads the same live and over a historical range. "
                "Source of truth is stale_price_check_kmonitor.q; regenerate "
                "this file with build_dashboard.py rather than editing it."
            ),
            "group": "Market structure",
            "refresh_secs": 30,
            "periods": "both",
            "orientation": "landscape",
            "source": "kdb",
            "time_context": {"mode": "realtime"},
            "parameters": parameters(),
            "datasets": [dataset(n, *q[n]) for n in NAMES],
            "rows": [
                {
                    "widgets": [
                        {"type": "kpi", "dataset": "stale_check",
                         "title": "Workorders checked",
                         "spec": {"column": "id_work", "agg": "count",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "stale_check",
                         "title": "Flagged",
                         "spec": {"column": "flagged", "agg": "sum",
                                  "fmt": ",.0f",
                                  "thresholds": [{"op": ">", "value": 0,
                                                  "color": "critical"}]},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "stale_check",
                         "title": "Worst deviation",
                         "spec": {"column": "abs_dev_bps", "agg": "max",
                                  "fmt": ",.1f", "suffix": " bps"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "stale_check",
                         "title": "Oldest print at t_gen",
                         "spec": {"column": "price_age_ms", "agg": "max",
                                  "fmt": ",.0f", "suffix": " ms"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "stale_check",
                         "title": "Names affected",
                         "spec": {"column": "sym", "agg": "nunique",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                    ],
                    "height_in": 0.9,
                    "gap_above_in": 0.0,
                    "col_gap_in": 0.1,
                },
                {
                    "widgets": [
                        {"type": "table", "dataset": "stale_check",
                         "title": ("Workorder price vs the print qatt had at "
                                   "t_gen"),
                         "spec": {
                             "columns": ["date", "sym", "side", "state",
                                         "otype", "trader", "id_target",
                                         "id_work", "size", "t_gen", "price",
                                         "gen_price", "dev_bps", "ptime",
                                         "price_age_ms", "now_price",
                                         "now_dev_bps", "now_age_ms", "flag"],
                             "labels": {
                                 "sym": "Stock", "side": "Side",
                                 "state": "Child state", "otype": "Type",
                                 "trader": "Trader", "id_target": "Parent",
                                 "id_work": "Child", "size": "Order qty",
                                 "t_gen": "Generated",
                                 "price": "Order price",
                                 "gen_price": "Print at t_gen",
                                 "dev_bps": "Gap (bps)",
                                 "ptime": "Print time",
                                 "price_age_ms": "Print age (ms)",
                                 "now_price": "Print now",
                                 "now_dev_bps": "Gap now (bps)",
                                 "now_age_ms": "Since last print (ms)",
                                 "flag": "Verdict",
                             },
                             "formats": {"size": ",.0f", "price": ",.4f",
                                         "gen_price": ",.4f",
                                         "now_price": ",.4f",
                                         "dev_bps": ",.1f",
                                         "now_dev_bps": ",.1f",
                                         "price_age_ms": ",.0f",
                                         "now_age_ms": ",.0f"},
                             # Only "critical", "good", "blue", "ink", "ink2",
                             # "muted" or a #hex resolve - anything else goes
                             # to ink and reads as no highlight at all.
                             "highlight": [
                                 {"column": "flag", "op": "=",
                                  "value": "both", "color": "critical"},
                                 {"column": "flag", "op": "=",
                                  "value": "noprint", "color": "critical"},
                                 {"column": "flag", "op": "=",
                                  "value": "price", "color": "blue"},
                                 {"column": "flag", "op": "=",
                                  "value": "age", "color": "blue"},
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
