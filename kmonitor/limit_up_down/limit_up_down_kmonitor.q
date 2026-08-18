/ limit_up_down_kmonitor.q - every order, activated or not, on a stock that was
/ locked or one-sided, and how long that lasted.  KdbMonitor version of
/ queries/limit_up_down/limit_up_down_v2.q.
/
/ Source of truth: build_dashboard.py turns the three blocks below into the
/ importable JSON.  Edit here, re-run the builder, re-import.
/
/ Three chained datasets, each narrowing the next:
/   order_syms   OMS     names we have orders on
/   limit_state  QUOTES  their limit up/down episodes, out of qatt
/   blotter      OMS     the orders on the names that had one
/
/ "OMS" and "QUOTES" are placeholders - change them in env= AND in
/ {{conn:...:realtime}}.  Details in README.md.


/ ==== DATASET: order_syms | env=OMS ====
/ Our names, minus the markets with no daily price limit.  Only here to keep
/ the qatt scan in dataset 2 down to our own book.
{[nl]
  / target carries date on the RDB too, so one lambda serves both servers
  mk:{[dts;nl]
    select distinct sym from target
      where date in dts, not any sym like/: nl
   };
  {{#realtime}}mk[enlist .z.D;nl]{{/realtime}}{{#historical}}{[nl;mk]
     want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
     hasToday:0<count select date from target where date=.z.D;
     stitch:(.z.D in want) and not hasToday;
     s:mk[$[stitch; want except .z.D; want]; nl];
     if[stitch;
       c:hopen {{conn:OMS:realtime}};
       t:c(mk;enlist .z.D;nl);
       hclose c;
       s:distinct s,t];
     s
    }[nl;mk]{{/historical}}
 }[("*.HK";"*.AU";"*.SP";"*.NZ")]
/ ==== END ====


/ ==== DATASET: limit_state | env=QUOTES ====
/ One row per name per day: its longest episode, and how long it lasted.
{[syms;minMins]
  now:.z.T;
  / the quote RDB has NO date column, so unlike every other table here the two
  / halves cannot share a lambda.  this is the RDB half.
  rdb:{[syms]
    update date:.z.D from
      select time,sym,qbid,qask from qatt where sym in syms
   };
  qt:{{#realtime}}rdb syms{{/realtime}}{{#historical}}{[syms;rdb]
      want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
      / asked of the QUOTE hdb: it and the order hdb are written down on their
      / own schedules, so one having today says nothing about the other
      hasToday:0<count select date from qatt where date=.z.D;
      stitch:(.z.D in want) and not hasToday;
      dts:$[stitch; want except .z.D; want];
      q:select date,time,sym,qbid,qask from qatt where date in dts, sym in syms;
      if[stitch;
        c:hopen {{conn:QUOTES:realtime}};
        t:c(rdb;syms);
        hclose c;
        q:q uj t];
      q
     }[syms;rdb]{{/historical}};
  / nothing on either side is not a quote - it would read as one sided
  qt:select date,time,sym,qbid:0^qbid,qask:0^qask from qt where (0<0^qbid)|0<0^qask;
  qt:`date`sym`time xasc qt;
  / locked, or one sided with a price on the surviving side
  qt:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from qt;
  / a counter that ticks over whenever the flag flips: quotes -> episodes
  qt:update run:sums differ lim by date,sym from qt;
  r:0!select started:first time, lastq:last time, quotes:count i,
      qbid:last qbid, qask:last qask, lim:first lim
    by date,sym,run from qt;
  / a pinned stock stops quoting, so its session end is read off the other
  / names in its market rather than off its own last quote
  r:update mkt:`$last each "." vs/: string sym from r;
  r:update sessionEnd:max lastq by date,mkt from r;
  / an episode ends when the next run begins; the day's last run has no next
  r:update nextStart:next started by date,sym from r;
  e:select from r where lim;
  e:update ongoing:null nextStart from e;
  / keyed off the ROW's date, not the period, so a stitched frame gets today
  / and last week right in one pass
  e:update ended:?[ongoing;?[date=.z.D;now;sessionEnd];nextStart] from e;
  e:update lasted_mins:"j"$(ended-started)%60000 from e;
  / counted before the collapse below
  e:update episodes:count i by date,sym from e;
  / longest episode per name per day: a bare select-by takes the last row
  e:0!select by date,sym from `lasted_mins xasc e;
  / dir0 is the verdict from the quote alone; dataset 3 finishes it
  select date,sym,mkt,
      kind:?[qbid=qask;`locked;`oneSided],
      dir0:?[0=qask;`up;?[0=qbid;`down;`unknown]],
      qbid,qask,started,ended,lasted_mins,ongoing,episodes,quotes
    from e where lasted_mins>=minMins
 }[{{order_syms.sym}};{{param:min_mins}}]
/ ==== END ====


/ ==== DATASET: blotter | env=OMS ====
/ Every order on a name that had an episode, with the episode beside it.
{[lim]
  syms:exec distinct sym from lim;
  / dates come from the episodes, so this is already whatever dataset 2 found
  dts:exec distinct date from lim;
  / every table here carries date on the RDB too, so one lambda serves both
  mk:{[dts;syms]
    t:select date,id_server,id_target,sym,basket,side,size,algo,beta
      from target where date in dts, sym in syms;
    ids:exec distinct id_target from t;
    / state as a COLUMN, not a filter - that is the change from v2
    s:`date`id_server`id_target xkey select state:last state
      by date,id_server,id_target
      from target_state where date in dts, id_target in ids;
    / closes only: target_stock also has sym and beta, and lj would overwrite
    x:`date`id_server`id_target xkey select date,id_server,id_target,adjclose,orgclose
      from target_stock where date in dts, id_target in ids;
    w:`date`id_server`id_target xkey select
        exec_qty:sum make,
        splits:count distinct id_work,
        first_workorder:min t_gen,
        last_workorder:max t_gen,
        latest_venue:last venue
      by date,id_server,id_target
      from workorder where date in dts, id_target in ids;
    update ref:orgclose^adjclose from (((t lj s) lj x) lj w)
   };
  o:{{#realtime}}mk[dts;syms]{{/realtime}}{{#historical}}{[dts;syms;mk]
      hasToday:0<count select date from target where date=.z.D;
      stitch:(.z.D in dts) and not hasToday;
      o:mk[$[stitch; dts except .z.D; dts]; syms];
      if[stitch;
        c:hopen {{conn:OMS:realtime}};
        t:c(mk;enlist .z.D;syms);
        hclose c;
        o:o uj t];
      o
     }[dts;syms;mk]{{/historical}};
  r:o ij `date`sym xkey lim;
  r:update pxLimit:?[0=qbid;qask;qbid] from r;
  / a locked quote needs the previous close, which only exists on this side
  r:update dir:?[dir0<>`unknown;dir0;
      ?[pxLimit>ref;`up;?[pxLimit<ref;`down;`unknown]]] from r;
  `lasted_mins xdesc select date,id_target,sym,mkt,basket,side,state,dir,algo,beta,size,
      exec_qty:0^exec_qty, splits:0^splits,
      first_workorder, last_workorder,
      kind, qbid, qask, started, ended, lasted_mins, ongoing, episodes, latest_venue
    from r
 }[{{table:limit_state}}]
/ ==== END ====
