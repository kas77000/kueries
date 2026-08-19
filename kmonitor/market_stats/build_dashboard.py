"""Build the KdbMonitor dashboard export from market_stats_kmonitor.q.

The q is the source of truth; this only carries it into the JSON KdbMonitor
imports, so the two cannot drift. Run it after any edit to the .q:

    python build_dashboard.py

Writes market_stats_kmonitor_dashboard.json beside it. Import that in
KdbMonitor under Dashboards -> Import.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
SOURCE = HERE / "market_stats_kmonitor.q"
TARGET = HERE / "market_stats_kmonitor_dashboard.json"

# / ==== DATASET: name | env=ENV ====  ...q...  / ==== END ====
BLOCK = re.compile(
    r"^/ ==== DATASET:\s*(\w+)\s*\|\s*env=(\w+)\s*====\s*$(.*?)^/ ==== END ====\s*$",
    re.M | re.S,
)

WANTED = ("fx", "stats")


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



def _choice(name, label, choices, default, help_):
    return {"name": name, "label": label, "kind": "choice", "choices": choices,
            "dataset": "", "column": "", "default": default, "q_type": "symbol",
            "help": help_, "required": True, "pattern": "", "pattern_message": "",
            "minimum": "", "maximum": "", "integer": False, "weekdays_only": False}


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
            "name": "Market Statistics",
            "description": (
                "Price, Volatility, Spread, Volume, Trade Size and Quote Size "
                "for one APAC market over a date range - per 10 minute bucket "
                "or per date, in shares or USD notional. Market wide: the "
                "universe is every name on the feed carrying the country "
                "suffix, not an index. Source of truth is "
                "market_stats_kmonitor.q; regenerate this file with "
                "build_dashboard.py rather than editing it."
            ),
            "group": "Market structure",
            "refresh_secs": 60,
            "periods": "both",
            "orientation": "landscape",
            "source": "kdb",
            "time_context": {"mode": "realtime"},
            "parameters": [
                _choice("country", "Market", ["AU", "JP", "HK", "IN"], "HK",
                        "Add more suffixes to .ms.mkt in the queries file first."),
                _choice("unit", "Volume unit", ["shares", "notional"], "shares",
                        "Changes what Volume, Trade Size and Quote Size mean. "
                        "Price, Volatility and Spread are always bps."),
                _choice("view", "View", ["intraday", "daily"], "intraday",
                        "intraday is one bar per 10 minutes; daily is one bar "
                        "per date, computed over the whole day rather than "
                        "averaged from the buckets."),
            ],
            "datasets": [dataset(n, *q[n]) for n in WANTED],
            "rows": [
                {
                    "widgets": [
                        {"type": "bar", "dataset": "stats",
                         "title": "Price",
                         "spec": {"x": "label", "y": "price_bps"},
                         "width": 1.0},
                        {"type": "bar", "dataset": "stats",
                         "title": "Volatility",
                         "spec": {"x": "label", "y": "volatility_bps"},
                         "width": 1.0},
                        {"type": "bar", "dataset": "stats",
                         "title": "Spread",
                         "spec": {"x": "label", "y": "spread_bps"},
                         "width": 1.0},
                    ],
                    "height_in": 2.9,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.15,
                },
                {
                    "widgets": [
                        {"type": "bar", "dataset": "stats",
                         "title": "Volume",
                         "spec": {"x": "label", "y": "volume"},
                         "width": 1.0},
                        {"type": "bar", "dataset": "stats",
                         "title": "Trade Size",
                         "spec": {"x": "label", "y": "trade_size"},
                         "width": 1.0},
                        {"type": "bar", "dataset": "stats",
                         "title": "Quote Size",
                         "spec": {"x": "label", "y": "quote_size"},
                         "width": 1.0},
                    ],
                    "height_in": 2.9,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.15,
                },
                {
                    "widgets": [
                        {"type": "table", "dataset": "stats",
                         "title": "Values, including the continuous / auction split",
                         "spec": {
                             "columns": ["label", "price_bps", "volatility_bps",
                                         "spread_bps", "volume", "volume_cont",
                                         "volume_auct", "trade_size",
                                         "quote_size", "n_syms", "n_trades"],
                             "labels": {"label": "", "price_bps": "Price (bps)",
                                        "volatility_bps": "Vol (bps)",
                                        "spread_bps": "Spread (bps)",
                                        "volume": "Volume",
                                        "volume_cont": "Continuous",
                                        "volume_auct": "Auction",
                                        "trade_size": "Trade size",
                                        "quote_size": "Quote size",
                                        "n_syms": "Names", "n_trades": "Trades"},
                             "formats": {"price_bps": ",.1f",
                                         "volatility_bps": ",.1f",
                                         "spread_bps": ",.2f", "volume": ",.0f",
                                         "volume_cont": ",.0f",
                                         "volume_auct": ",.0f",
                                         "trade_size": ",.0f",
                                         "quote_size": ",.0f", "n_syms": ",.0f",
                                         "n_trades": ",.0f"},
                         },
                         "width": 1.0},
                    ],
                    "height_in": 3.4,
                    "gap_above_in": 0.15,
                    "col_gap_in": 0.0,
                },
            ],
        }],
    }


if __name__ == "__main__":
    TARGET.write_text(json.dumps(build(), indent=2) + chr(10), encoding="utf-8")
    print(f"wrote {TARGET.name}")
