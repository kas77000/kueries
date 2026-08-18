/ dark_summary_kmonitor.q - shares and USD notional executed in dark venues.
/ KdbMonitor version of queries/dark_summary/dark_summary.q, generalised from
/ one day to the period the reader picks.
/
/ Source of truth: build_dashboard.py turns the blocks below into the importable
/ JSON.  Edit here, re-run the builder, re-import.  Details in README.md.
/
/ "OMS" is a placeholder - change it in env= AND in {{conn:OMS:realtime}}.


/ ==== DATASET: dark_by_venue | env=OMS ====
/ darkSummary itself: one row per venue over the period.
{[dk]
  / --- SOURCING: identical in both blocks of this file, change it in both ---
  / dark fills for a list of dates.  Both tables carry date on the RDB and the
  / HDB, so this one lambda runs on either server.
  mk:{[dts;dk]
    w:select date,id_server,id_target,sym,venue,make,avg_fill_price
      from workorder where date in dts, make>0, any (upper venue) like/: dk;
    ids:exec distinct id_target from w;
    x:`date`id_server`id_target xkey select date,id_server,id_target,fxlast
      from target_stock where date in dts, id_target in ids;
    / fxlast is local -> USD
    select date,venue,sym,make,notional_usd:make*avg_fill_price*fxlast
      from w lj x
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
/ The same rows by date instead of by venue.  A second pass over the same slice
/ of workorder - delete this and the widgets reading it if you only want venues.
{[dk]
  / --- SOURCING: identical in both blocks of this file, change it in both ---
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
  / syms is a distinct count and does not add, which is why this is a second
  / query rather than a roll-up of the one above
  `date xasc 0!select
      venues:count distinct venue,
      orders:count i,
      syms:count distinct sym,
      shares:sum make,
      notional_usd:sum notional_usd
    by date from r
 }[("*DARK*";"*DRK*")]
/ ==== END ====
