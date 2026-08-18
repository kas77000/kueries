/ =============================================================================
/ dark_summary_kmonitor.q
/
/ The KdbMonitor version of queries/dark_summary/dark_summary.q.  Same
/ arithmetic, same definition of a dark venue, same rounding - reshaped as
/ dashboard datasets, generalised from ONE DAY to the period the reader picks,
/ and stitched across the RDB and the HDB so a range ending today is complete.
/
/ THIS FILE IS THE SOURCE OF TRUTH.  build_dashboard.py reads the blocks below
/ and writes dark_summary_kmonitor_dashboard.json, which is what you import in
/ Dashboards -> Import.  Edit the q here, re-run the builder, re-import.
/
/ ----------------------------------------------------------------------------
/ WHICH SERVER ANSWERS WHAT
/
/   Real-time selected            -> the RDB, today, nothing else asked.
/   A range not reaching today    -> the HDB, nothing else asked.
/   A range that includes today   -> the HDB for the range, AND the RDB for
/                                    today, unioned - but only if the HDB has
/                                    not been written down for today yet.
/
/ KdbMonitor sends a dataset to ONE server: the period decides which.  So on a
/ historical period the query lands on the HDB and reaches back to the RDB
/ itself, through {{conn:OMS:realtime}} - the ENV:kind form of the handle
/ token, which names one side of an environment whatever period is running.
/
/ THE SAFEGUARD is `hasToday`.  If the HDB already holds today, the range
/ covers it and stitching would count today twice; the RDB is then never
/ opened.  It asks whether workorder has ANY rows for today, not whether it
/ has dark fills, so a genuinely dark-free day is not mistaken for a day that
/ has not been written down.
/
/ This needs the HDB process to be able to reach the RDB.  If it cannot,
/ hopen throws and the panel shows the error - which is the right failure: a
/ silently short answer is the thing being fixed here.
/ ----------------------------------------------------------------------------
/
/ The two datasets.  Both read the OMS - workorder and target_stock and nothing
/ else, exactly as darkSummary does.  No quote server.
/   1. dark_by_venue  darkSummary itself: one row per venue over the period
/   2. dark_by_day    the same rows aggregated by date instead, for the trend
/
/ ENV NAME.  "OMS" below is a placeholder - change it to whatever your order
/ server environment is called in Admin, in THREE places per block: the env= in
/ the header, and the {{conn:OMS:realtime}} inside.  It needs a real-time AND a
/ historical server registered for any of this to resolve.
/
/ {{...}} TOKENS are KdbMonitor's, not q's, and are filled in before sending:
/   {{#historical}}..{{/historical}}  kept only when the period is historical
/   {{#realtime}}..{{/realtime}}      kept only when the period is real time
/   {{date_from}} {{date_to}}         the chosen range, as 2026.08.18
/   {{conn:OMS:realtime}}             the RDB's `:host:port, in either period
/ =============================================================================


/ ==== DATASET: dark_by_venue | env=OMS ====
/ darkSummary, over the chosen period: shares done and notional in USD, by dark
/ venue, with each venue's share of the dark total.
{[dk]
  / --- SOURCING.  Identical in both blocks of this file; change it in both. ---
  / One definition of the dark-fill pull, parameterised by the dates wanted.
  / workorder and target_stock both carry a date column on the RDB and on the
  / HDB, so the same lambda runs on either server - which is what makes the
  / stitch a matter of choosing dates rather than writing the query twice.
  mk:{[dts;dk]
    / make>0 keeps this an EXECUTION report - children sent to a dark venue and
    / never filled are not here.  See dark_routed_executed.q for that split.
    w:select date,id_server,id_target,sym,venue,make,avg_fill_price
      from workorder where date in dts, make>0, any (upper venue) like/: dk;
    / fxlast lives in target_stock, not workorder, so join it on per parent
    / order.  One row per target, so no aggregation needed.
    ids:exec distinct id_target from w;
    x:`date`id_server`id_target xkey select date,id_server,id_target,fxlast
      from target_stock where date in dts, id_target in ids;
    / executed notional in local ccy, then in USD.  fxlast is local -> USD.
    select date,venue,sym,make,notional_usd:make*avg_fill_price*fxlast
      from w lj x
   };
  r:{{#realtime}}mk[enlist .z.D;dk]{{/realtime}}{{#historical}}{[dk;mk]
      want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
      / Has the HDB been written down for today?  Asked of workorder as a
      / whole, not of dark fills: a day with no dark activity must not read as
      / a day that is missing.  Costs one column of one partition, and only
      / when that partition exists at all.
      hasToday:0<count select date from workorder where date=.z.D;
      / Today comes off the RDB only when the HDB has not got it.  If it has,
      / it is already inside the range and stitching would double count.
      stitch:(.z.D in want) and not hasToday;
      r:mk[$[stitch; want except .z.D; want]; dk];
      if[stitch;
        c:hopen {{conn:OMS:realtime}};
        t:c(mk;enlist .z.D;dk);
        hclose c;
        r:r uj t];
      r
     }[dk;mk]{{/historical}};
  / --- END SOURCING ---
  s:0!select
      orders:count i,
      syms:count distinct sym,
      shares:sum make,
      notional_usd:sum notional_usd
    by venue from r;
  / notional stays as it is; only the percentage is rounded, to 2dp
  s:update pct_notional:100*notional_usd%sum notional_usd from s;
  `notional_usd xdesc update pct_notional:0.01*"j"$100*pct_notional from s
 }[("*DARK*";"*DRK*")]
/ ==== END ====


/ ==== DATASET: dark_by_day | env=OMS ====
/ The same rows, aggregated by date instead of by venue.  Everything down to
/ END SOURCING is word for word the block above - if you change the definition
/ of a dark fill, or the stitch, change it in both.  Delete this dataset and
/ the row of widgets that reads it if you only want the venue view; it is a
/ second pass over the same slice of workorder.
{[dk]
  / --- SOURCING.  Identical in both blocks of this file; change it in both. ---
  mk:{[dts;dk]
    w:select date,id_server,id_target,sym,venue,make,avg_fill_price
      from workorder where date in dts, make>0, any (upper venue) like/: dk;
    ids:exec distinct id_target from w;
    x:`date`id_server`id_target xkey select date,id_server,id_target,fxlast
      from target_stock where date in dts, id_target in ids;
    select date,venue,sym,make,notional_usd:make*avg_fill_price*fxlast
      from w lj x
   };
  r:{{#realtime}}mk[enlist .z.D;dk]{{/realtime}}{{#historical}}{[dk;mk]
      want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
      hasToday:0<count select date from workorder where date=.z.D;
      stitch:(.z.D in want) and not hasToday;
      r:mk[$[stitch; want except .z.D; want]; dk];
      if[stitch;
        c:hopen {{conn:OMS:realtime}};
        t:c(mk;enlist .z.D;dk);
        hclose c;
        r:r uj t];
      r
     }[dk;mk]{{/historical}};
  / --- END SOURCING ---
  `date xasc 0!select
      venues:count distinct venue,
      orders:count i,
      syms:count distinct sym,
      shares:sum make,
      notional_usd:sum notional_usd
    by date from r
 }[("*DARK*";"*DRK*")]
/ ==== END ====


/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. HOW DARK IS DECIDED, unchanged: every child order executed in the dark has
/    DARK or DRK in its venue name, so matching that name IS the classification
/    rather than an approximation of it.  Upper cased first so the test is case
/    insensitive.  The patterns are the dk argument at the foot of each block -
/    if a third ever needs adding, add it in both.
/
/ 2. WHICH PRICE, unchanged: avg_fill_price, what the child order actually
/    filled at, not price, which is what it was sent with.
/
/ 3. ROUNDING, unchanged: only pct_notional is rounded, to 2dp, and only at the
/    end.  "j"$ rounds to nearest, it does not truncate.  notional_usd is left
/    at full precision so nothing downstream inherits a rounded figure - the
/    display formats in the dashboard round for the eye only.
/
/ 4. WHY THE SAME LAMBDA RUNS ON BOTH SERVERS.  Every table this file touches
/    carries a date column on the real-time side as well as the historical one,
/    so mk takes a list of dates and does not care which server it is on.  That
/    is not true of qatt - the quote RDB has no date column - which is why
/    limit_up_down_kmonitor.q has to write its two halves separately.
/
/    mk is sent over the handle as a serialized lambda and carries no reference
/    to the locals of the function that defined it, which is why dk is passed
/    as an argument rather than closed over.
/
/ 5. WHAT A PERIOD DOES TO pct_notional.  Over a range it is each venue's share
/    of the dark notional for the WHOLE range, not an average of its daily
/    shares.  A venue that took everything on one quiet day and nothing since
/    reads small, which is the honest answer to "where did our dark flow go".
/    The daily view is dark_by_day, and the pie is deliberately not per day.
/
/ 6. syms IS EXACT AT EACH GRAIN, which is why this is two queries rather than
/    one plus a roll-up in pandas: distinct counts do not add.  Summing each
/    day's distinct symbols would count a name traded on three days as three.
/
/ 7. A PARTIAL WRITEDOWN IS THE ONE CASE hasToday GETS WRONG.  It reads as
/    "the HDB has today", so the RDB is not consulted and whatever had not been
/    written yet is missing.  A writedown that publishes the partition only
/    when it is complete - the normal arrangement - is not affected.
/
/ 8. ONLY DARK VENUES ARE HERE, so every percentage is a share of the dark book
/    and never of the day's total trading.  Nothing in this file looks at a lit
/    venue, so "we did 38% in the dark" is NOT a question it can answer.
/
/ 9. AN EMPTY PERIOD - no dark fills at all - comes back as an empty table with
/    its columns intact, so the panels read as zero rather than as broken.
/ -----------------------------------------------------------------------------
