/ =============================================================================
/ limit_up_down_v2.q
/
/ Same detection as limit_up_down.q, reshaped into a monitoring blotter.
/ Adds a workorder rollup per parent so you can see how much has actually been
/ done, how many child orders went out, and where the last one went.
/
/   q)\l queries/limit_up_down/limit_up_down_v2.q
/   q)h:hopen`:orderserver:5010
/   q)limitUpDownV2[h;20]
/
/ Named limitUpDownV2 so it can sit alongside limitUpDown without one
/ silently overwriting the other.  Pass 0i for h if the tables are local.
/
/ Columns
/   id_target        parent order id
/   sym              stock
/   basket           target`basket
/   side             target`side
/   dir              `up / `down / `unknown - which way the stock is pinned
/   algo             target`algo
/   beta             target`beta          <- see note 1, there are two betas
/   size             parent order size
/   exec_qty         sum of workorder`make for this parent
/   splits           count distinct workorder`id_work for this parent
/   first_workorder  earliest workorder`t_gen, null if none yet
/   last_workorder   latest workorder`t_gen, null if none yet
/   qbid             latest bid while in limit state
/   qask             latest ask while in limit state
/   since            when the limit state started   <- see note 2, you asked
/                    for "from", which is a q keyword
/   latest_venue     venue of the most recent workorder
/ =============================================================================

/ lookback is in minutes.  NOT named mins - that is the q keyword for running
/ minimums, and using it as a parameter gives a type error.
limitUpDownV2:{[h;lookback]
  now:.z.T;
  t0:now-60000*lookback;
  d:.z.D;
  / Markets with NO daily price limit - a one sided or locked quote there is a
  / thin book, a stale quote, a halt or an auction imbalance, never a limit.
  nl:("*.HK";"*.AU";"*.SP";"*.NZ");
  / --- on the order server: activated parents, reference close, workorder roll
  f:{[d;nl]
    t:select date,id_server,id_target,sym,basket,side,size,algo,beta
      from target where date=d, not any sym like/: nl;
    ids:exec distinct id_target from t;
    s:select state:last state by date,id_server,id_target
      from target_state where date=d, id_target in ids;
    s:`date`id_server`id_target xkey select from (0!s) where state=`activated;
    / only adjclose/orgclose are taken from target_stock - it also has sym and
    / beta, and lj would let those overwrite the ones from target
    x:`date`id_server`id_target xkey select date,id_server,id_target,
        adjclose,orgclose
      from target_stock where date=d, id_target in ids;
    / one row per parent: what the children have done so far
    w:`date`id_server`id_target xkey select
        exec_qty:sum make,
        splits:count distinct id_work,
        first_workorder:min t_gen,
        last_workorder:max t_gen,
        latest_venue:last venue
      by date,id_server,id_target
      from workorder where date=d, id_target in ids;
    select from (((t lj s) lj x) lj w) where not null state
    };
  o:$[0<h; h(f;d;nl); f[d;nl]];
  if[0=count o; :o];
  / reference price: adjusted close, falling back to the unadjusted one
  o:update ref:orgclose^adjclose from o;
  / --- locally: quote history for OUR names only.  qatt`sym carries the `g
  / attribute, so putting sym first makes this an index lookup, not a day scan.
  syms:exec distinct sym from o;
  qt:select time,sym,qbid:0^qbid,qask:0^qask,netChange:0^netChange
    from qatt where sym in syms, time<=now, (0<0^qbid)|0<0^qask;
  / locked, or one sided with a price on the surviving side
  qt:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from qt;
  / where the quote stands now, and the last time it was NOT in limit state.
  / A stock pinned at the limit often stops updating altogether, so we anchor
  / on the last normal quote rather than counting rows inside the window.
  k:select firstQuote:first time, qbid:last qbid, qask:last qask,
      netChange:last netChange, lim:last lim,
      lastNormal:max ?[lim;0Nt;time]
    by sym from qt;
  / in limit right now, and nothing normal anywhere in the lookback window
  k:select from (0!k) where lim, (null lastNormal)|lastNormal<t0;
  / when the limit state started: just after the last normal quote, or from the
  / first quote of the day if it has never been two sided
  k:update since:?[null lastNormal;firstQuote;lastNormal] from k;
  / keep only the parents sitting on one of those names
  r:o ij `sym xkey delete lim,firstQuote,lastNormal from k;
  / qask empty => nothing offered => limit up.  qbid empty => limit down.
  / locked on both sides needs the previous close to break the tie.
  r:update pxLimit:?[0=qbid;qask;qbid] from r;
  r:update dir:?[0=qask;`up;?[0=qbid;`down;
      ?[pxLimit>ref;`up;?[pxLimit<ref;`down;
      ?[netChange>0;`up;?[netChange<0;`down;`unknown]]]]]] from r;
  / longest stuck first
  `since xasc select id_target, sym, basket, side, dir, algo, beta, size,
      exec_qty:0^exec_qty, splits:0^splits,
      first_workorder, last_workorder, qbid, qask, since, latest_venue
    from r
 };

/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. WHICH beta?  Both target and target_stock have a beta column.  This takes
/    target`beta - the algo parameter that sits next to alpha / gamma / delta -
/    because you listed it right after algo.  If you meant the stock's market
/    beta, that is target_stock`beta: add it to the target_stock select above
/    under a different name (e.g. stock_beta:beta) so it does not collide, and
/    swap it into the final column list.
/
/ 2. "from" IS A q KEYWORD, so a column called from cannot be written or read
/    back in a select without the parser choking on it - the same trap as mins.
/    The column is called since instead.  Rename it if you have a better word,
/    just not from.
/
/ 3. first_workorder / last_workorder use workorder`t_gen, when the child order
/    was generated.  workorder also carries t_start, t_transmit, t_oes_send and
/    t_on_market if you would rather measure from when it actually reached the
/    market.  Both are null when no workorder exists yet, which is the case
/    worth watching: a parent stuck at the limit with splits=0 has never had a
/    child order out at all.
/
/ 4. exec_qty is sum of workorder`make, and splits is count distinct id_work,
/    both aggregated on the order server so only one row per parent comes back.
/    exec_qty and splits are zero filled; the two timestamps are deliberately
/    left null so "no workorders yet" stays visible rather than reading as
/    midnight.
/
/ 5. latest_venue is the venue of the last workorder row for the parent, which
/    assumes workorder is appended in time order - the normal tick convention,
/    and the same assumption the state lookup makes.
/
/ 6. blocked and forMins from limit_up_down.q are not in this output, since you
/    listed the columns you want.  forMins is since vs now if you want it back;
/    blocked needs sidesign added to the target select.
/
/ 7. Everything in limit_up_down.q's notes still applies - direction inference,
/    the pctFromClose sanity check being absent here, venues with no price
/    limits, and the symbology assumption between target`sym and qatt`sym.
/ -----------------------------------------------------------------------------
