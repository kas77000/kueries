"""Build the KdbMonitor dashboard export from dark_routed_executed_kmonitor.q.

The q is the source of truth; this only carries it into the JSON KdbMonitor
imports, so the two cannot drift. Run it after any edit to the .q:

    python build_dashboard.py

Writes dark_routed_executed_kmonitor_dashboard.json beside it. Import that in
KdbMonitor under Dashboards -> Import.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "dark_routed_executed_kmonitor.q"
TARGET = HERE / "dark_routed_executed_kmonitor_dashboard.json"

# / ==== DATASET: name | env=ENV ====  ...q...  / ==== END ====
BLOCK = re.compile(
    r"^/ ==== DATASET:\s*(\w+)\s*\|\s*env=(\w+)\s*====\s*$(.*?)^/ ==== END ====\s*$",
    re.M | re.S,
)

WANTED = ("dark_routed",)


def blocks() -> dict[str, tuple[str, str]]:
    """Every dataset block in the .q, as {name: (env, qsql)}."""
    found = {m.group(1): (m.group(2), m.group(3).strip())
             for m in BLOCK.finditer(SOURCE.read_text(encoding="utf-8"))}
    missing = set(WANTED) - set(found)
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
        # The country picker lives HERE, not in the query. choices_for reads a
        # column parameter's options from the dataset's RAW frame - before this
        # transform runs - so one dataset can both offer the countries and be
        # narrowed to one of them, and changing the pick never re-queries.
        "transforms": [
            {"kind": "filter",
             "params": {"column": "country", "op": "=",
                        "value": "{{param:country}}"}},
        ],
        "max_rows": 5000,
        "static": False,
        "source": "kdb",
        "shape": None,
        "file_label": "",
    }


def _table(title: str, columns: list, labels: dict, formats: dict,
           highlight: list = None, width: float = 1.0) -> dict:
    spec = {"columns": columns, "labels": labels, "formats": formats}
    if highlight:
        spec["highlight"] = highlight
    return {"type": "table", "dataset": "dark_routed", "title": title,
            "spec": spec, "width": width}


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
            "name": "Dark routed vs executed",
            "description": (
                "Where our dark flow was ROUTED against where it actually "
                "EXECUTED, by venue, for one country or the whole book. The "
                "two pies share a shape but not a denominator - the gap "
                "between them is the point, and fill_rate names it. Reads the "
                "same in real time and over a date range. Source of truth is "
                "dark_routed_executed_kmonitor.q; regenerate this file with "
                "build_dashboard.py rather than editing it."
            ),
            "group": "Execution quality",
            "refresh_secs": 60,
            "periods": "both",
            "orientation": "landscape",
            "source": "kdb",
            "time_context": {"mode": "realtime"},
            "parameters": [{
                "name": "country",
                "label": "Country",
                "kind": "column",
                "choices": [],
                # Reads its options from this dataset's raw frame. ALL leads
                # the query's result, so it is the first option and the one
                # this opens on.
                "dataset": "dark_routed",
                "column": "country",
                "default": "ALL",
                "q_type": "",
                "help": ("ALL is the whole dark book. Picking a market "
                         "re-cuts the pies and both tables from rows already "
                         "in hand — it does not go back to the server."),
                "required": False,
                "pattern": "",
                "pattern_message": "",
                "minimum": "",
                "maximum": "",
                "integer": False,
                "weekdays_only": False,
            }],
            "datasets": [dataset(n, *q[n]) for n in WANTED],
            "rows": [
                {
                    "widgets": [
                        {"type": "kpi", "dataset": "dark_routed",
                         "title": "Routed (USD)",
                         "spec": {"column": "notional_routed", "agg": "sum",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "dark_routed",
                         "title": "Executed (USD)",
                         "spec": {"column": "notional_executed", "agg": "sum",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "dark_routed",
                         "title": "Venues",
                         "spec": {"column": "venue", "agg": "nunique",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "dark_routed",
                         "title": "Best venue fill rate",
                         "spec": {"column": "fill_rate", "agg": "max",
                                  "fmt": ".2f", "suffix": "%"},
                         "width": 1.0},
                    ],
                    "height_in": 0.9,
                    "gap_above_in": 0.0,
                    "col_gap_in": 0.1,
                },
                {
                    "widgets": [
                        # The pie works its own percentages out of the values,
                        # so it is fed the notional rather than pct_* - one
                        # fewer thing that has to agree with the table beside
                        # it, and immune to the 2dp rounding.
                        {"type": "pie", "dataset": "dark_routed",
                         "title": "Routed %",
                         "spec": {"by": "venue", "value": "notional_routed",
                                  "donut": True},
                         "width": 1.0},
                        {"type": "pie", "dataset": "dark_routed",
                         "title": "Executed %",
                         "spec": {"by": "venue", "value": "notional_executed",
                                  "donut": True},
                         "width": 1.0},
                    ],
                    "height_in": 3.4,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.2,
                },
                {
                    "widgets": [
                        _table(
                            "Routed",
                            ["venue", "orders_routed", "syms", "shares_routed",
                             "notional_routed", "pct_routed"],
                            {"venue": "Venue", "orders_routed": "Orders",
                             "syms": "Stocks", "shares_routed": "Shares",
                             "notional_routed": "Notional (USD)",
                             "pct_routed": "Share %"},
                            {"orders_routed": ",.0f", "syms": ",.0f",
                             "shares_routed": ",.0f",
                             "notional_routed": ",.0f", "pct_routed": ".2f"},
                        ),
                        _table(
                            "Executed",
                            ["venue", "orders_filled", "shares_executed",
                             "notional_executed", "pct_executed", "fill_rate"],
                            {"venue": "Venue", "orders_filled": "Fills",
                             "shares_executed": "Shares",
                             "notional_executed": "Notional (USD)",
                             "pct_executed": "Share %",
                             "fill_rate": "Fill rate %"},
                            {"orders_filled": ",.0f",
                             "shares_executed": ",.0f",
                             "notional_executed": ",.0f",
                             "pct_executed": ".2f", "fill_rate": ".2f"},
                            highlight=[{"column": "fill_rate", "op": "<=",
                                        "value": 0, "color": "critical"}],
                        ),
                    ],
                    "height_in": 3.2,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.2,
                },
            ],
        }],
    }


if __name__ == "__main__":
    TARGET.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.name}")
