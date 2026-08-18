"""Build the KdbMonitor dashboard export from limit_up_down_kmonitor.q.

The q is the source of truth; this only carries it into the JSON KdbMonitor
imports, so the two cannot drift. Run it after any edit to the .q:

    python build_dashboard.py

Writes limit_up_down_kmonitor_dashboard.json beside it. Import that in
KdbMonitor under Dashboards -> Import.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "limit_up_down_kmonitor.q"
TARGET = HERE / "limit_up_down_kmonitor_dashboard.json"

# / ==== DATASET: name | env=ENV ====  ...q...  / ==== END ====
BLOCK = re.compile(
    r"^/ ==== DATASET:\s*(\w+)\s*\|\s*env=(\w+)\s*====\s*$(.*?)^/ ==== END ====\s*$",
    re.M | re.S,
)


def blocks() -> dict[str, tuple[str, str]]:
    """Every dataset block in the .q, as {name: (env, qsql)}, in file order."""
    found = {m.group(1): (m.group(2), m.group(3).strip())
             for m in BLOCK.finditer(SOURCE.read_text(encoding="utf-8"))}
    missing = {"order_syms", "limit_state", "blotter"} - set(found)
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
            "name": "Limit up/down - orders on pinned stocks",
            "description": (
                "Every parent order, activated or not, sitting on a stock that "
                "was locked or one-sided for longer than the chosen threshold, "
                "with how long that lasted. Reads the same in real time and "
                "over a historical range: the period switch sends each dataset "
                "to the real-time or the historical side of its environment. "
                "Source of truth is limit_up_down_kmonitor.q; regenerate this "
                "file with build_dashboard.py rather than editing it."
            ),
            "group": "Market structure",
            "refresh_secs": 30,
            "periods": "both",
            "orientation": "landscape",
            "source": "kdb",
            "time_context": {"mode": "realtime"},
            "parameters": [{
                "name": "min_mins",
                "label": "Minimum duration (minutes)",
                "kind": "number",
                "choices": [],
                "dataset": "",
                "column": "",
                "default": "20",
                "q_type": "number",
                "help": ("How long the stock has to have been locked or "
                         "one-sided before it counts. This one is inside the "
                         "query, so changing it goes back to the server."),
                "required": True,
                "pattern": "",
                "pattern_message": "",
                "minimum": "0",
                "maximum": "",
                "integer": True,
                "weekdays_only": False,
            }],
            "datasets": [dataset(n, *q[n])
                         for n in ("order_syms", "limit_state", "blotter")],
            "rows": [
                {
                    "widgets": [
                        {"type": "kpi", "dataset": "limit_state",
                         "title": "Names in limit",
                         "spec": {"column": "sym", "agg": "nunique",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "blotter",
                         "title": "Orders affected",
                         "spec": {"column": "id_target", "agg": "count",
                                  "fmt": ",.0f",
                                  "thresholds": [{"op": ">", "value": 0,
                                                  "color": "critical"}]},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "limit_state",
                         "title": "Longest run",
                         "spec": {"column": "lasted_mins", "agg": "max",
                                  "fmt": ",.0f", "suffix": " min"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "blotter",
                         "title": "Order qty held up",
                         "spec": {"column": "size", "agg": "sum",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                    ],
                    "height_in": 0.9,
                    "gap_above_in": 0.0,
                    "col_gap_in": 0.1,
                },
                {
                    "widgets": [
                        {"type": "table", "dataset": "blotter",
                         "title": "Orders on stocks that were limit up/down",
                         "spec": {
                             "columns": ["date", "id_target", "sym", "mkt",
                                         "basket", "side", "state", "dir",
                                         "algo", "beta", "size", "exec_qty",
                                         "splits", "first_workorder",
                                         "last_workorder", "kind", "qbid",
                                         "qask", "started", "ended",
                                         "lasted_mins", "ongoing", "episodes",
                                         "latest_venue"],
                             "labels": {
                                 "id_target": "Parent", "sym": "Stock",
                                 "mkt": "Market", "state": "State",
                                 "dir": "Direction", "size": "Order qty",
                                 "exec_qty": "Executed", "splits": "Children",
                                 "first_workorder": "First child",
                                 "last_workorder": "Last child",
                                 "kind": "Quote", "started": "From",
                                 "ended": "To", "lasted_mins": "Lasted (min)",
                                 "ongoing": "Still on",
                                 "episodes": "Episodes",
                                 "latest_venue": "Last venue",
                             },
                             "formats": {"size": ",.0f", "exec_qty": ",.0f",
                                         "splits": ",.0f",
                                         "lasted_mins": ",.0f",
                                         "qbid": ",.2f", "qask": ",.2f"},
                             # Only "critical", "good", "blue", "ink", "ink2",
                             # "muted" or a #hex resolve - anything else goes
                             # to ink and reads as no highlight at all.
                             "highlight": [
                                 {"column": "ongoing", "op": "=",
                                  "value": True, "color": "critical"},
                             ],
                         },
                         "width": 1.0},
                    ],
                    "height_in": 5.5,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.0,
                },
                {
                    "widgets": [
                        {"type": "bar", "dataset": "limit_state",
                         "title": "How long each name was pinned",
                         "spec": {"x": "sym", "y": "lasted_mins",
                                  "orientation": "h", "sort": "asc"},
                         "width": 1.6},
                        {"type": "table", "dataset": "limit_state",
                         "title": "Episodes",
                         "spec": {
                             "columns": ["date", "sym", "mkt", "kind", "dir0",
                                         "started", "ended", "lasted_mins",
                                         "ongoing", "episodes", "quotes"],
                             "labels": {"sym": "Stock", "mkt": "Market",
                                        "kind": "Quote", "dir0": "Side",
                                        "started": "From", "ended": "To",
                                        "lasted_mins": "Lasted (min)",
                                        "ongoing": "Still on",
                                        "episodes": "Episodes",
                                        "quotes": "Quote updates"},
                             "formats": {"lasted_mins": ",.0f",
                                         "quotes": ",.0f"},
                         },
                         "width": 1.0},
                    ],
                    "height_in": 3.2,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.15,
                },
            ],
        }],
    }


if __name__ == "__main__":
    TARGET.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.name}")
