/ stale_price_check_kmonitor.q - every workorder under a live parent, with the
/ price the algo gave it beside the last print qatt actually had for that name
/ at the same instant.  KdbMonitor version of
/ queries/stale_price_check/stale_price_check.q.
/
/ Source of truth: build_dashboard.py turns the two blocks below into the
/ importable JSON.  Edit here, re-run the builder, re-import.
/
/ Two chained datasets - dataset 1 already carries every order field, so
/ dataset 2 finishes the job and there is no third hop back to the OMS:
/   live_orders  OMS     workorders under an activated parent
/   stale_check  QUOTES  the print qatt had at each t_gen, and the gap
/
/ "OMS" and "QUOTES" are placeholders - change them in env= AND in
/ {{conn:...:realtime}}.  Details in README.md.
/
/ A threshold of 0 turns that test OFF, so 0/0 is the calibration run: every
/ workorder, every number filled in, nothing flagged.
/
/ LIMIT ORDERS ONLY.  a market order carries price 0 and has no order price to
/ hold a print against, so it never leaves the order server.


/ ==== DATASET: live_orders | env=OMS ====
/ The live book and its children.  Collapsed to one row per workorder here so
/ dataset 2 joins against a clean set.
{[]
  / target_state and workorder0 both carry date on the RDB as well as the HDB,
  / so one lambda serves both servers
  mk:{[dts]
    s:0!select state:last state by date,id_server,id_target
      from target_state where date in dts;
    ids:exec distinct id_target from s where state=`activated;
    / NOT filtered on the child's own state - a workorder still in `init under
    / a live parent was priced off the same data as its activated siblings
    w:select date,id_server,id_target,id_work,sequence,trader,sym,side,size,
        otype,state,t_gen,price
      from workorder0 where date in dts, id_target in ids;
    / workorder0 writes a row per state change; sequence is the order the
    / server wrote them, and time can tie
    w:0!select by date,id_server,id_work from `sequence xasc w;
    / LIMIT ORDERS ONLY.  a market order carries price 0 - there is no order
    / price to hold a print against, and leaving it in reads as -10000 bps and
    / sorts to the top of every run.  0< also drops a null price.
    / after the collapse, so it is the order's CURRENT price that decides and
    / not some earlier row's.
    select from w where 0<price
   };
  {{#realtime}}mk enlist .z.D{{/realtime}}{{#historical}}{[mk]
     want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
     / asked of the ORDER hdb: it and the quote hdb are written down on their
     / own schedules, so one having today says nothing about the other
     hasToday:0<count select date from workorder0 where date=.z.D;
     stitch:(.z.D in want) and not hasToday;
     o:mk[$[stitch; want except .z.D; want]];
     if[stitch;
       c:hopen {{conn:OMS:realtime}};
       t:c(mk;enlist .z.D);
       hclose c;
       o:o uj t];
     o
    }[mk]{{/historical}}
 }[]
/ ==== END ====


/ ==== DATASET: stale_check | env=QUOTES ====
/ The whole order table comes across, because an aj needs both sides local.
{[w;minDevBps;minAgeMs]
  now:.z.T;
  / nothing activated: back out before touching qatt.  this hands the widgets
  / the empty ORDER table rather than an empty result with the columns they
  / name, so an empty book may show as a column error rather than as no rows.
  if[0=count w; :w];
  syms:exec distinct sym from w;
  / dates come from the orders, so this is already whatever dataset 1 found
  dts:exec distinct date from w;
  / the quote RDB has NO date column - it is today by definition - so unlike
  / the order side the two halves cannot share a lambda.  this is the RDB half.
  / PRINTS ONLY: qatt carries quote updates on the same table and their price
  / is null, and an asof onto one of those would date the order against a row
  / that never traded.
  rdb:{[syms]
    update date:.z.D from
      select time, sym, gen_price:price, ptime:time
        from qatt where sym in syms, 0<0^price
   };
  p:{{#realtime}}rdb syms{{/realtime}}{{#historical}}{[dts;syms;rdb]
      hasToday:0<count select date from qatt where date=.z.D;
      stitch:(.z.D in dts) and not hasToday;
      dd:$[stitch; dts except .z.D; dts];
      q:select date, time, sym, gen_price:price, ptime:time
        from qatt where date in dd, sym in syms, 0<0^price;
      if[stitch;
        c:hopen {{conn:QUOTES:realtime}};
        t:c(rdb;syms);
        hclose c;
        q:q uj t];
      q
     }[dts;syms;rdb]{{/historical}};
  / aj wants the right side sorted by the join columns.  date is IN the join
  / here, unlike the bare-q version: an exact match on date and sym, asof on
  / time, so a range can never date an order against another day's print.
  p:`date`sym`time xasc p;
  x:aj[`date`sym`time; update time:t_gen from w; p];

  / ---- the two prices, and the gap ------------------------------------
  x:update price_age_ms:"j"$t_gen-ptime from x;
  / price is a real limit price by now - market orders never left the order
  / server.  the guard left is gen_price: a name with no print has none.
  x:update dev_bps:?[0<gen_price;
      10000*(price-gen_price)%gen_price; 0n] from x;
  / where the tape is now: says whether this is an order that was born bad or
  / one still being fed bad data
  c:select now_price:last gen_price, now_ptime:last time by date,sym from p;
  x:x lj c;
  / "now" on a past date means that day's last print, read off the other names
  / in the frame - the same way a pinned stock's session end is read
  e:select sess:max time by date from p;
  x:x lj e;
  x:update now_age_ms:"j"$?[date=.z.D; now; sess]-now_ptime from x;
  x:update now_dev_bps:?[0<now_price;
      10000*(price-now_price)%now_price; 0n] from x;

  / ---- verdict --------------------------------------------------------
  / 0 turns a test off rather than making everything breach it
  x:update hit_price:(0<minDevBps)&minDevBps<=abs dev_bps,
      hit_age:(0<minAgeMs)&minAgeMs<=price_age_ms,
      noprint:null ptime from x;
  x:update flag:?[noprint;`noprint;
      ?[hit_price&hit_age;`both;
      ?[hit_price;`price;
      ?[hit_age;`age;`ok]]]] from x;
  x:update flagged:not flag=`ok from x;
  / a name we are working that has never printed is a finding, not an absence,
  / so it comes back whatever the thresholds say
  x:$[(0=minDevBps)&0=minAgeMs; x; select from x where flagged];
  / worst first, and the ones with nothing to compare against on top
  r:`noprint`sev xdesc update sev:abs dev_bps from x;
  select date, id_server, id_target, id_work, trader, sym, side, size, otype,
      state, t_gen, price, gen_price, dev_bps, abs_dev_bps:sev, ptime,
      price_age_ms, now_price, now_dev_bps, now_age_ms, flag, flagged
    from r
 }[{{table:live_orders}};{{param:min_dev_bps}};{{param:min_price_age_ms}}]
/ ==== END ====
