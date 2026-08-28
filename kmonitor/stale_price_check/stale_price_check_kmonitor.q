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
/
/ THE LOOKBACK IS REAL-TIME ONLY, and it is in minutes.  Live it bounds t_gen -
/ how recently the workorder was created - because reading the whole session out
/ of qatt is too slow to run on a refresh.  On a historical period it is passed
/ as 00:00:00.000, ie no bound, because "the last 10 minutes" cannot mean
/ anything on a past date: the reader already bounded that frame with the dates.
/
/ TWO WINDOWS, AND qatt's IS THE WIDER ONE.  Live, the orders come from the last
/ `lookback` minutes and qatt is read from TWICE that far back, because an order
/ generated at the very start of the order window still needs prints before it
/ to land on.  `noprint` therefore means "no print in the scanned window" rather
/ than "never traded today" - still a finding, since a name that has not printed
/ in 2x lookback is stale by any reading.


/ ==== DATASET: live_orders | env=OMS ====
/ The live book and its children.  Collapsed to one row per workorder here so
/ dataset 2 joins against a clean set.
{[lookback]
  / target_state and workorder0 both carry date on the RDB as well as the HDB,
  / so one lambda serves both servers.  t0 is how far back t_gen may sit;
  / 00:00:00.000 is the no-bound value the historical branch passes.
  mk:{[dts;t0]
    s:0!select state:last state by date,id_server,id_target
      from target_state where date in dts;
    ids:exec distinct id_target from s where state=`activated;
    / NOT filtered on the child's own state - a workorder still in `init under
    / a live parent was priced off the same data as its activated siblings
    / price, limit_target and limit_candidate all come back.  which of the
    / three is the price the algo actually decided on has never been verified
    / against a server, and reading `price` gave an order at 175 against a tape
    / at 113.6 - so the report carries all three and lets the tape say which.
    w:select date,id_server,id_target,id_work,sequence,trader,sym,side,size,
        otype,state,t_gen,price,limit_target,limit_candidate
      from workorder0 where date in dts, t_gen>=t0, id_target in ids;
    / ONE ROW PER CHILD, AND IT IS THE FIRST ONE.  workorder0 writes a row per
    / state change.  t_gen is stamped at generation and never moves, but price
    / IS rewritten - a chase repoints it - so taking the last row pairs a
    / repriced price with a generation timestamp and the comparison below stops
    / meaning anything: right time, wrong price.
    / Everything describing the order AS GENERATED therefore comes off the
    / first row by sequence.  Only state is read from the last, because the
    / current state is the one thing you want current.
    w:`sequence xasc w;
    w:0!select id_target:first id_target, trader:first trader, sym:first sym,
        side:first side, size:first size, otype:first otype,
        t_gen:first t_gen, price:first price,
        limit_target:first limit_target, limit_candidate:first limit_candidate,
        state:last state
      by date,id_server,id_work from w;
    / LIMIT ORDERS ONLY.  a market order carries price 0 - there is no order
    / price to hold a print against, and leaving it in reads as -10000 bps and
    / sorts to the top of every run.  0< also drops a null price.
    select from w where 0<price
   };
  {{#realtime}}mk[enlist .z.D; .z.T-60000*lookback]{{/realtime}}{{#historical}}{[mk]
     want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
     / asked of the ORDER hdb: it and the quote hdb are written down on their
     / own schedules, so one having today says nothing about the other
     hasToday:0<count select date from workorder0 where date=.z.D;
     stitch:(.z.D in want) and not hasToday;
     o:mk[$[stitch; want except .z.D; want]; 00:00:00.000];
     if[stitch;
       c:hopen {{conn:OMS:realtime}};
       t:c(mk;enlist .z.D;00:00:00.000);
       hclose c;
       o:o uj t];
     o
    }[mk]{{/historical}}
 }[{{param:lookback_mins}}]
/ ==== END ====


/ ==== DATASET: stale_check | env=QUOTES ====
/ The whole order table comes across, because an aj needs both sides local.
{[w;lookback;minDevBps;minAgeMs]
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
  / sym first: the RDB keeps `g#sym, and the where clause can only use that
  / attribute on the constraint it applies first.  the time cut then runs on
  / what survives, which is already a small fraction of the session.
  rdb:{[syms;q0]
    update date:.z.D from
      select time, sym, gen_price:price, ptime:time
        from qatt where sym in syms, time>=q0, 0<0^price
   };
  p:{{#realtime}}rdb[syms; .z.T-2*60000*lookback]{{/realtime}}{{#historical}}{[dts;syms;rdb]
      hasToday:0<count select date from qatt where date=.z.D;
      stitch:(.z.D in dts) and not hasToday;
      dd:$[stitch; dts except .z.D; dts];
      q:select date, time, sym, gen_price:price, ptime:time
        from qatt where date in dd, sym in syms, 0<0^price;
      if[stitch;
        c:hopen {{conn:QUOTES:realtime}};
        t:c(rdb;syms;00:00:00.000);
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
  / renamed on the way out so the two can never be read for each other:
  / order_price is ours, qatt_price is the tape's
  select date, id_server, id_target, id_work, trader, sym, side, size, otype,
      state, t_gen, order_price:price, limit_target, limit_candidate,
      qatt_price:gen_price, dev_bps,
      abs_dev_bps:sev, ptime, price_age_ms, qatt_price_now:now_price,
      now_dev_bps, now_age_ms, flag, flagged
    from r
 }[{{table:live_orders}};{{param:lookback_mins}};{{param:min_dev_bps}};{{param:min_price_age_ms}}]
/ ==== END ====
