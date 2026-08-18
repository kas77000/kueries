"""Build the KdbMonitor dashboard export from dark_summary_kmonitor.q.

The q is the source of truth; this only carries it into the JSON KdbMonitor
imports, so the two cannot drift. Run it after any edit to the .q:

    python build_dashboard.py

Writes dark_summary_kmonitor_dashboard.json beside it. Import that in
KdbMonitor under Dashboards -> Import.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "dark_summary_kmonitor.q"
TARGET = HERE / "dark_summary_kmonitor_dashboard.json"

# / ==== DATASET: name | env=ENV ====  ...q...  / ==== END ====
BLOCK = re.compile(
    r"^/ ==== DATASET:\s*(\w+)\s*\|\s*env=(\w+)\s*====\s*$(.*?)^/ ==== END ====\s*$",
    re.M | re.S,
)

WANTED = ("dark_by_venue", "dark_by_day")


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
            "name": "Dark venue execution",
            "description": (
                "What we executed in dark venues over the chosen period: "
                "shares done and notional in USD, broken down by venue, with "
                "each venue's share of the dark total. Reads the same in real "
                "time and over a date range. Shares of the DARK book only - "
                "nothing here looks at a lit venue. Source of truth is "
                "dark_summary_kmonitor.q; regenerate this file with "
                "build_dashboard.py rather than editing it."
            ),
            "group": "Execution quality",
            "refresh_secs": 60,
            "periods": "both",
            "orientation": "landscape",
            "source": "kdb",
            "time_context": {"mode": "realtime"},
            "parameters": [],
            "datasets": [dataset(n, *q[n]) for n in WANTED],
            "rows": [
                {
                    "widgets": [
                        {"type": "kpi", "dataset": "dark_by_venue",
                         "title": "Dark notional (USD)",
                         "spec": {"column": "notional_usd", "agg": "sum",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "dark_by_venue",
                         "title": "Shares executed",
                         "spec": {"column": "shares", "agg": "sum",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "dark_by_venue",
                         "title": "Venues used",
                         "spec": {"column": "venue", "agg": "count",
                                  "fmt": ",.0f"},
                         "width": 1.0},
                        {"type": "kpi", "dataset": "dark_by_venue",
                         "title": "Largest venue share",
                         "spec": {"column": "pct_notional", "agg": "max",
                                  "fmt": ".2f", "suffix": "%"},
                         "width": 1.0},
                    ],
                    "height_in": 0.9,
                    "gap_above_in": 0.0,
                    "col_gap_in": 0.1,
                },
                {
                    "widgets": [
                        {"type": "pie", "dataset": "dark_by_venue",
                         "title": "Where the dark notional went",
                         "spec": {"by": "venue", "value": "notional_usd",
                                  "donut": True},
                         "width": 1.0},
                        {"type": "table", "dataset": "dark_by_venue",
                         "title": "By venue",
                         "spec": {
                             "columns": ["venue", "orders", "syms", "shares",
                                         "notional_usd", "pct_notional"],
                             "labels": {"venue": "Venue", "orders": "Fills",
                                        "syms": "Stocks", "shares": "Shares",
                                        "notional_usd": "Notional (USD)",
                                        "pct_notional": "Share"},
                             "formats": {"shares": ",.0f", "orders": ",.0f",
                                         "syms": ",.0f",
                                         "notional_usd": ",.0f",
                                         "pct_notional": ".2f"},
                         },
                         "width": 1.5},
                    ],
                    "height_in": 3.6,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.15,
                },
                {
                    "widgets": [
                        {"type": "bar", "dataset": "dark_by_day",
                         "title": "Dark notional per day (USD)",
                         "spec": {"x": "date", "y": "notional_usd"},
                         "width": 1.5},
                        {"type": "table", "dataset": "dark_by_day",
                         "title": "By day",
                         "spec": {
                             "columns": ["date", "venues", "orders", "syms",
                                         "shares", "notional_usd"],
                             "labels": {"date": "Date", "venues": "Venues",
                                        "orders": "Fills", "syms": "Stocks",
                                        "shares": "Shares",
                                        "notional_usd": "Notional (USD)"},
                             "formats": {"venues": ",.0f", "orders": ",.0f",
                                         "syms": ",.0f", "shares": ",.0f",
                                         "notional_usd": ",.0f"},
                         },
                         "width": 1.0},
                    ],
                    "height_in": 3.0,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.15,
                },
            ],
        }],
    }


if __name__ == "__main__":
    TARGET.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {TARGET.name}")
