# basket_vs_close

Live (activated) `.IN` targets of a basket, with their pivoted `target_column`
values, the average fill price of their work orders, the previous close, and
whether the fills beat that close.

Run it on the **order server** (qStudio): paste `basket_vs_close.q` and change
the pattern on the last line (`["*CALPERS*"]`).

## Columns added

| column | meaning |
|---|---|
| `wo_count` | work orders with at least one fill (filled or partially filled) |
| `wo_qty` | shares filled across those work orders |
| `avg_px` | `make wavg avg_fill_price` over those work orders — size-weighted, so a 10-share fill does not count as much as a 10,000-share one |
| `prev_close` | `equity_master.PX_LAST` |
| `better_than_close` | `1b` when a buy averaged **below** the close or a sell **above** it (`sidesign*(prev_close-avg_px) > 0`) |

## Notes

- `workorder` writes several rows per child as its state changes. `make` and
  `avg_fill_price` are cumulative (`FlexOrderStream.addOMSWorkOrder` writes
  `makes()` and `avgPrice()`), so the **last row per `id_work`** carries the
  child's totals. Summing every row would count the same fills more than once.
- "Filled or partially filled" is `make>0`, not a match on the `state` text.
  That catches a child cancelled after a partial fill, whatever its state reads.
- `better_than_close` is `0b`, not null, when a target has no fills or no
  close (q booleans have no null). Check `avg_px` / `prev_close` before reading
  a `0b` as "worse".
- `equity_master` is joined on `sym`. If a row comes back with an empty
  `prev_close`, the target's `sym` is not that table's `sym`. Try
  `sym_flextrade`.
- The original version filtered `basket like "*CALPERS*"` inside the lambda, so
  the `s` argument did nothing. It now uses `basket like s`.
