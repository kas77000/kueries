# Dark routed vs executed — KdbMonitor dashboard

Where our dark flow was **routed** against where it actually **executed**, by
venue, for one country or the whole book. Two pies, then two tables with the
values behind them. Same arithmetic as
`queries/dark_summary/dark_routed_executed.q`, generalised from one day to
whatever period the reader picks.

| File | What it is |
| --- | --- |
| `dark_routed_executed_kmonitor.q` | **The source of truth.** The dataset query, with the reasoning. |
| `build_dashboard.py` | Reads the `.q` and writes the JSON, so the two cannot drift. |
| `dark_routed_executed_kmonitor_dashboard.json` | Generated. This is what you import. |

## Installing it

1. Change `OMS` to whatever your order-server environment is called in Admin —
   in the `env=` of the `/ ==== DATASET: … ====` header **and** in
   `{{conn:OMS:realtime}}` inside the query.
2. `python build_dashboard.py`
3. KdbMonitor → **Dashboards → Import** → pick
   `dark_routed_executed_kmonitor_dashboard.json`.

## The country picker

`ALL` is the whole dark book; picking a market re-cuts both pies and both
tables. It **never goes back to the server**: the query returns one row per
country and venue and counts every fill twice — once under its own country and
once under `ALL` — so both are already in the frame and the pick is a filter on
rows the dashboard is holding.

That is also what keeps the numbers honest. Shares are worked out per country in
the q, so a frame filtered to one market already adds to 100. And `syms` is a
distinct count, which does not add — a name traded in two markets would be
double-counted by any roll-up done after the fact, so `ALL` is built from the
fills rather than from the per-country totals.

**Country is the sym suffix** — `7203.JP` is `JP`, and Singapore is `SP` — not
`target_stock.country`. The suffix convention is the one we know; the country
codes in the reference data may not spell a market the same way. So the picker
offers exchange suffixes, which is what the desk means by country here. A sym
carrying no suffix goes to `unknown` rather than returning itself, which would
otherwise put a stock name in the dropdown as though it were a market.

## Reading the two pies

**They have different denominators.** One is a share of routed notional, the
other of executed. They are comparable as *shapes*, not as levels — which is
exactly the comparison worth making. A venue taking 30% of the flow and
returning 8% of the fills is a different venue from one doing 8% and 8%.

`fill_rate` in the Executed table names that gap directly. It is **money**
weighted; `orders_filled / orders_routed` is the order-weighted version, and the
two diverge when one venue gets the big orders.

Unlike `dark_summary`, this does **not** filter on `make>0` — the children that
never filled are precisely what makes routed differ from executed.
`notional_executed` here reconciles with `notional_usd` there, country for
country.

## Which server answers what

| Period selected | Where the rows come from |
| --- | --- |
| Real-time | the RDB, today. Nothing else is asked. |
| A range **not** reaching today | the HDB. Nothing else is asked. |
| A range that **includes** today | the HDB for the range **plus** the RDB for today, unioned — unless the HDB already holds today, in which case the HDB answers alone. |

Same arrangement as the other two dashboards here, guarded by `hasToday` so a
written-down today is never counted twice. See `dark_summary/README.md` for the
longer note.

Editing the q means editing the `.q` and re-running the builder. Editing the
JSON by hand works once and is lost the next time anyone regenerates it.
