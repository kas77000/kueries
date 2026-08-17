/ =============================================================================
/ dark_summary.q
/
/ One function.  Summarises what we executed in DARK venues on a given day,
/ broken down by venue: shares done and notional converted to USD.
/
/   q)\l queries/dark_summary/dark_summary.q
/   q)h:hopen`:orderserver:5010
/   q)darkSummary[h;.z.D]        / today
/   q)darkSummary[h;.z.D-1]      / yesterday
/
/ Pass 0i for h if workorder / target_stock are in this same process.
/
/ Columns
/   venue           workorder`venue
/   orders          child orders that filled in that venue
/   syms            distinct stocks traded there
/   shares          sum of workorder`make
/   notional_usd    sum of make * avg_fill_price * fxlast
/   pct_notional    share of the day's total dark notional
/   missing_fx      fills with no fxlast - temporary, see note 3
/
/ A venue is DARK when its name contains DARK or DRK.  Every child order
/ executed in the dark carries that in the venue name, so the match is the
/ classification.
/ =============================================================================

darkSummary:{[h;dt]
  / venue name patterns that mean dark.  Matched case insensitively.
  dk:("*DARK*";"*DRK*");
  / --- on the order server: dark fills for the day, plus the fx rate.
  / fxlast lives in target_stock, not workorder, so it has to be joined on
  / per parent order.
  f:{[d;dk]
    w:select date,id_server,id_target,sym,venue,make,avg_fill_price
      from workorder
      where date=d, make>0, any (upper venue) like/: dk;
    if[0=count w; :w];
    ids:exec distinct id_target from w;
    / one row per target, so no aggregation needed
    x:`date`id_server`id_target xkey select date,id_server,id_target,
        fxlast,currency
      from target_stock where date=d, id_target in ids;
    w lj x
    };
  r:$[0<h; h(f;dt;dk); f[dt;dk]];
  if[0=count r; :r];
  / executed notional in local ccy, then in USD.  fxlast is local -> USD.
  r:update notional_usd:make*avg_fill_price*fxlast from r;
  s:0!select
      orders:count i,
      syms:count distinct sym,
      shares:sum make,
      notional_usd:sum notional_usd,
      missing_fx:sum null fxlast    / temporary - see note 3, delete this line
    by venue from r;
  `notional_usd xdesc update pct_notional:100*notional_usd%sum notional_usd
    from s
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
/    missing_fx is in the output on purpose while you confirm fxlast really is
/    always populated.  It matters because q's sum treats null as zero: a fill
/    with no rate would add shares but no notional, so the venue would quietly
/    under-report rather than error.  missing_fx makes that visible instead.
/
/    Once it has read 0 for long enough to trust, delete the marked line in the
/    select and the column disappears - nothing else depends on it.
/
/ 4. Only rows with make>0 are counted, so orders is child orders that actually
/    filled, not child orders sent.  Drop the make>0 constraint if you want
/    fill rates rather than an execution summary.
/
/ 5. pct_notional is each venue's share of the DARK total only, not of all
/    trading that day - this function never looks at lit venues.
/ -----------------------------------------------------------------------------
