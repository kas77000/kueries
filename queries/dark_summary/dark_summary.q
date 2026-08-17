/ =============================================================================
/ dark_summary.q
/
/ One function.  Summarises what we executed in DARK venues on a given day,
/ broken down by venue: shares done and notional converted to USD.
/
/ Runs entirely on the ORDER SERVER - it reads workorder and target_stock and
/ nothing else.  No handle, no qatt.  (The jp_no_print and limit_up_down
/ scripts take a handle only because they straddle two processes: order tables
/ on one side, realtime qatt on the other.  This one does not.)
/
/   q)\l queries/dark_summary/dark_summary.q
/   q)darkSummary .z.D        / today
/   q)darkSummary .z.D-1      / yesterday
/
/ If you ever want it from another process, send it over as usual:
/   q)h(darkSummary;.z.D)
/
/ Columns
/   venue           workorder`venue
/   orders          child orders that filled in that venue
/   syms            distinct stocks traded there
/   shares          sum of workorder`make
/   notional_usd    sum of make * avg_fill_price * fxlast, unrounded
/   pct_notional    share of the day's total dark notional, rounded to 2dp
/
/ A venue is DARK when its name contains DARK or DRK.  Every child order
/ executed in the dark carries that in the venue name, so the match is the
/ classification.
/ =============================================================================

darkSummary:{[dt]
  / venue name patterns that mean dark.  Matched case insensitively.
  dk:("*DARK*";"*DRK*");
  / dark fills for the day
  w:select date,id_server,id_target,sym,venue,make,avg_fill_price
    from workorder
    where date=dt, make>0, any (upper venue) like/: dk;
  if[0=count w; :w];
  / fxlast lives in target_stock, not workorder, so join it on per parent
  / order.  One row per target, so no aggregation needed.
  ids:exec distinct id_target from w;
  x:`date`id_server`id_target xkey select date,id_server,id_target,
      fxlast,currency
    from target_stock where date=dt, id_target in ids;
  / executed notional in local ccy, then in USD.  fxlast is local -> USD.
  r:update notional_usd:make*avg_fill_price*fxlast from w lj x;
  s:0!select
      orders:count i,
      syms:count distinct sym,
      shares:sum make,
      notional_usd:sum notional_usd
    by venue from r;
  / notional stays as it is; only the percentage is rounded, to 2dp
  s:update pct_notional:100*notional_usd%sum notional_usd from s;
  `notional_usd xdesc update pct_notional:0.01*"j"$100*pct_notional from s
 };

/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. HOW DARK IS DECIDED.  Every child order executed in the dark has DARK or
/    DRK in its venue name, so matching that name is the classification, not an
/    approximation of it.  Upper cased first so the test is case insensitive.
/    The patterns live in dk at the top of the function if a third ever needs
/    adding.
/
/ 2. WHICH PRICE.  Uses avg_fill_price, the average price the child order
/    actually filled at, not price, which is the price the order was sent with.
/    For executed notional avg_fill_price is the right one.
/
/ 3. FX.  fxlast is local -> USD, so notional_usd multiplies by it.
/
/ 4. ROUNDING.  Only pct_notional is rounded, to 2dp, and only at the end.
/    "j"$ rounds to nearest, it does not truncate.  notional_usd is left at
/    full precision so nothing downstream inherits a rounded figure.
/
/ 5. Only rows with make>0 are counted, so orders is child orders that actually
/    filled, not child orders sent.  Drop the make>0 constraint if you want
/    fill rates rather than an execution summary.
/
/ 6. pct_notional is each venue's share of the DARK total only, not of all
/    trading that day - this function never looks at lit venues.
/ -----------------------------------------------------------------------------
