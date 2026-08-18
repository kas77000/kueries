/ dark_routed_executed_kmonitor.q - dark flow ROUTED vs EXECUTED, by venue,
/ cut by country.  KdbMonitor version of queries/dark_summary/dark_routed_executed.q.
/
/ Source of truth: build_dashboard.py turns the block below into the importable
/ JSON.  Edit here, re-run the builder, re-import.  Details in README.md.
/
/ "OMS" is a placeholder - change it in env= AND in {{conn:OMS:realtime}}.


/ ==== DATASET: dark_routed | env=OMS ====
{[dk]
  / dark children for a list of dates.  Both tables carry date on the RDB and
  / the HDB, so this one lambda runs on either server.
  mk:{[dts;dk]
    / no make>0 filter: the children that never filled are what makes routed
    / differ from executed
    w:select date,id_server,id_target,sym,venue,size,price,make,avg_fill_price,
        transmit_lastprice
      from workorder where date in dts, any (upper venue) like/: dk;
    ids:exec distinct id_target from w;
    x:`date`id_server`id_target xkey select date,id_server,id_target,fxlast
      from target_stock where date in dts, id_target in ids;
    r:w lj x;
    / routed quantity is valued at the price the child was sent with, falling
    / back to the last trade at transmit time for market and pegged orders
    r:update px_routed:transmit_lastprice^?[price>0;price;0n] from r;
    / country is the SYM SUFFIX - 7203.JP is JP, Singapore is SP
    r:update country:`$last each "." vs/: string sym from r;
    / without this a sym with no suffix returns itself and lands in the picker
    r:update country:?[sym like "*.*";country;`unknown] from r;
    select date, country, venue, sym, size, make,
        notional_routed:size*px_routed*fxlast,
        notional_executed:make*avg_fill_price*fxlast
      from r
   };
  r:{{#realtime}}mk[enlist .z.D;dk]{{/realtime}}{{#historical}}{[dk;mk]
      want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
      / has the HDB been written down for today?  asked of workorder as a
      / whole, so a day with no dark fills does not read as a day that is gone
      hasToday:0<count select date from workorder where date=.z.D;
      / today comes off the RDB only when the HDB has not got it - otherwise
      / the range already covers it and stitching would double count
      stitch:(.z.D in want) and not hasToday;
      r:mk[$[stitch; want except .z.D; want]; dk];
      if[stitch;
        c:hopen {{conn:OMS:realtime}};
        t:c(mk;enlist .z.D;dk);
        hclose c;
        r:r uj t];
      r
     }[dk;mk]{{/historical}};
  / every fill counted twice, once under its country and once under `ALL, so
  / one group-by yields both and count distinct sym stays exact for each
  r:r uj update country:`ALL from r;
  s:0!select
      orders_routed:count i,
      orders_filled:sum make>0,
      syms:count distinct sym,
      shares_routed:sum size,
      shares_executed:sum make,
      notional_routed:sum notional_routed,
      notional_executed:sum notional_executed
    by country,venue from r;
  / shares are WITHIN a country, so a frame filtered to one market already
  / adds to 100 and the dashboard has nothing left to work out
  s:update
      pct_routed:100*notional_routed%sum notional_routed,
      pct_executed:100*notional_executed%sum notional_executed
    by country from s;
  / null rather than infinite where a venue routed nothing valuable
  s:update
      fill_rate:?[0<notional_routed;100*notional_executed%notional_routed;0n]
    from s;
  / notionals stay as they are; only the percentages are rounded
  s:update
      pct_routed:0.01*"j"$100*pct_routed,
      pct_executed:0.01*"j"$100*pct_executed,
      fill_rate:0.01*"j"$100*fill_rate
    from s;
  s:`notional_routed xdesc s;
  / `ALL first, stated rather than left to the alphabet: that ordering is what
  / makes it the picker's first entry and the value the dashboard opens on
  (select from s where country=`ALL),select from s where country<>`ALL
 }[("*DARK*";"*DRK*")]
/ ==== END ====
