/ stale_price_check.q - every workorder under a live parent, with the price the
/ algo gave it beside the last print qatt actually had for that name at the
/ same instant.  Built after ai3 sent orders priced off market data that had
/ gone stale, to see which workorders that is still happening to.
/
/   q)\l queries/stale_price_check/stale_price_check.q
/   q)h:hopen`:orderserver:5010
/   q)stalePriceCheck[h;10;0;0]                / last 10 min, everything ranked
/   q)stalePriceCheck[h;10;25;5000]            / last 10 min, breaches only
/
/ Run it FROM THE QUOTE SESSION.  qatt lives there, workorder0 and target_state
/ live on the order server, and an aj needs both sides local - so the order half
/ is a lambda shipped over h and joined here.  Pass 0i for h if you happen to
/ have both locally.
/
/ t_gen and qatt`time come off the same clock, so they compare directly.  No
/ timezone and no DST conversion anywhere in here, and that is deliberate.
/
/ LIMIT ORDERS ONLY.  a market order carries price 0 and has no order price to
/ hold a print against, so it never leaves the order server.
/
/ A threshold of 0 turns that test OFF.  stalePriceCheck[h;10;0;0] is therefore
/ the calibration run: every workorder in the window, every number filled in,
/ flag reading `ok on all of them because no threshold was set.  Read dev_bps
/ and price_age_ms directly on that run, decide what "bad" looks like on this
/ book, then pass those numbers.
/
/ lookback is IN MINUTES, and it bounds t_gen: how recently the workorder was
/ created.  It exists because reading the whole session out of qatt is too slow
/ to run live.  NOT named mins - that is the q keyword for running minimums,
/ and using it as a parameter gives a type error.
/
/ TWO WINDOWS, AND qatt's IS THE WIDER ONE.  The orders come from the last
/ `lookback` minutes; qatt is read from TWICE that far back, because an order
/ generated at the very start of the order window still needs prints before it
/ to land on.  So `noprint` here means "no print in the scanned window" rather
/ than "never traded today" - which is still a finding, since a name that has
/ not printed in 2x lookback is stale by any reading.

stalePriceCheck:{[h;lookback;minDevBps;minAgeMs]
  now:.z.T;
  d:.z.D;
  / t0 bounds the orders; q0 bounds qatt, and has to reach further back so an
  / order at t0 still has something to asof onto
  t0:now-60000*lookback;
  q0:t0-60000*lookback;

  / ---- order server ---------------------------------------------------
  / One lambda, shipped whole.  target_state and workorder0 both carry date
  / on the RDB as well as the HDB, so this same body serves either side.
  f:{[d;t0]
    / the live book: latest state per parent, keep the activated ones
    s:0!select state:last state by date,id_server,id_target
      from target_state where date=d;
    ids:exec distinct id_target from s where state=`activated;
    / every child of those parents.  NOT filtered on the child's own state -
    / a workorder still in `init under a live parent was priced off the same
    / data as its activated siblings.
    w:select date,id_server,id_target,id_work,sequence,trader,sym,side,size,
        otype,state,t_gen,price
      from workorder0 where date=d, t_gen>=t0, id_target in ids;
    / workorder0 writes a row per state change, so collapse to one row per
    / child.  sequence is the order the server wrote them; time can tie.
    w:0!select by date,id_server,id_work from `sequence xasc w;
    / LIMIT ORDERS ONLY.  a market order carries price 0 - there is no order
    / price to hold a print against, and leaving it in reads as -10000 bps and
    / sorts to the top of every run.  0< also drops a null price.
    / after the collapse, so it is the order's CURRENT price that decides and
    / not some earlier row's.
    select from w where 0<price
   };
  w:$[0<h; h(f;d;t0); f[d;t0]];
  / nothing activated: back out before touching qatt.  note this returns the
  / EMPTY ORDER TABLE, not an empty result with the columns below - same as
  / limit_up_down_v2 does.  fine at a prompt, worth knowing about.
  if[0=count w; :w];

  / ---- quote server, local --------------------------------------------
  syms:exec distinct sym from w;
  / PRINTS ONLY.  qatt carries quote updates on the same table and their price
  / is null - an asof onto one of those would date the order against a row that
  / never traded.  ptime is time under another name, because aj keeps the LEFT
  / table's time and the print's own timestamp is the whole point of the age.
  / sym first: the RDB keeps `g#sym, and the where clause can only use that
  / attribute on the constraint it applies first.  the time cut then runs on
  / what survives, which is already a small fraction of the session.
  p:select time, sym, gen_price:price, ptime:time
    from qatt where sym in syms, time>=q0, 0<0^price;
  / aj wants the right side sorted by the join columns.  the filter above drops
  / the `g#sym the RDB keeps, so sort it back.
  p:`sym`time xasc p;
  / single day, and qatt has no date column on the RDB - so sym and time are
  / the whole key here.  the kmonitor version joins on date too.
  x:aj[`sym`time; update time:t_gen from w; p];

  / ---- the two prices, and the gap ------------------------------------
  x:update price_age_ms:"j"$t_gen-ptime from x;
  / price is a real limit price by now - market orders never left the order
  / server.  the guard left is gen_price: a name with no print has none.
  x:update dev_bps:?[0<gen_price;
      10000*(price-gen_price)%gen_price; 0n] from x;
  / where the tape is now: says whether this is an order that was born bad or
  / one still being fed bad data
  c:select now_price:last gen_price, now_ptime:last time by sym from p;
  x:x lj c;
  x:update now_age_ms:"j"$now-now_ptime from x;
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
 };
