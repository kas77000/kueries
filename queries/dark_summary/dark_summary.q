/ dark_summary.q - shares and USD notional executed in DARK venues on a day,
/ by venue.  Runs entirely on the order server: workorder and target_stock,
/ no handle, no qatt.
/
/   q)\l queries/dark_summary/dark_summary.q
/   q)darkSummary .z.D          / or .z.D-1, or h(darkSummary;.z.D) from elsewhere
/
/ venue -> orders, syms, shares, notional_usd, pct_notional (2dp).
/ A venue is DARK when its name contains DARK or DRK - that match IS the
/ classification, not an approximation of it.

darkSummary:{[dt]
  dk:("*DARK*";"*DRK*");
  / make>0, so orders is children that FILLED, not children sent.  Drop it for
  / fill rates - or see dark_routed_executed.q, which does exactly that.
  w:select date,id_server,id_target,sym,venue,make,avg_fill_price
    from workorder
    where date=dt, make>0, any (upper venue) like/: dk;
  if[0=count w; :w];
  / fxlast lives in target_stock, one row per parent order
  ids:exec distinct id_target from w;
  x:`date`id_server`id_target xkey select date,id_server,id_target,
      fxlast,currency
    from target_stock where date=dt, id_target in ids;
  / avg_fill_price is what it filled at; price is what it was sent with.
  / fxlast is local -> USD.
  r:update notional_usd:make*avg_fill_price*fxlast from w lj x;
  s:0!select
      orders:count i,
      syms:count distinct sym,
      shares:sum make,
      notional_usd:sum notional_usd
    by venue from r;
  / notional stays at full precision; only the percentage is rounded, to 2dp.
  / "j"$ rounds to nearest, it does not truncate.
  s:update pct_notional:100*notional_usd%sum notional_usd from s;
  / a share of the DARK book, never of the day's total trading
  `notional_usd xdesc update pct_notional:0.01*"j"$100*pct_notional from s
 };
