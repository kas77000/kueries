/ limit_up_down_v2.q - same detection as limit_up_down.q, reshaped into a
/ monitoring blotter with a workorder rollup per parent: how much is done, how
/ many children went out, and where the last one went.
/
/   q)\l queries/limit_up_down/limit_up_down_v2.q
/   q)h:hopen`:orderserver:5010
/   q)limitUpDownV2[h;20]                      / pass 0i if the tables are local
/
/ Named V2 so it can sit alongside limitUpDown without overwriting it.
/ Everything in limit_up_down.q's comments still applies.
/
/ beta is target`beta, the algo parameter next to alpha/gamma/delta - NOT
/ target_stock`beta, the market beta.  Add that under another name if you meant
/ it.  since is what you asked to call "from", which is a q keyword and cannot
/ be read back in a select.

/ lookback is in minutes.  NOT named mins - that is the q keyword for running
/ minimums, and using it as a parameter gives a type error.
limitUpDownV2:{[h;lookback]
  now:.z.T;
  t0:now-60000*lookback;
  d:.z.D;
  / markets with no daily price limit - see limit_up_down.q
  nl:("*.HK";"*.AU";"*.SP";"*.NZ");
  f:{[d;nl]
    t:select date,id_server,id_target,sym,basket,side,size,algo,beta
      from target where date=d, not any sym like/: nl;
    ids:exec distinct id_target from t;
    s:select state:last state by date,id_server,id_target
      from target_state where date=d, id_target in ids;
    s:`date`id_server`id_target xkey select from (0!s) where state=`activated;
    / closes only: target_stock also has sym and beta, and lj would overwrite
    x:`date`id_server`id_target xkey select date,id_server,id_target,
        adjclose,orgclose
      from target_stock where date=d, id_target in ids;
    / one row per parent, aggregated here so only one row comes back.  t_gen is
    / when the child was generated - t_start, t_transmit, t_oes_send and
    / t_on_market are there if you would rather measure from the market.
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
  o:update ref:orgclose^adjclose from o;
  syms:exec distinct sym from o;
  qt:select time,sym,qbid:0^qbid,qask:0^qask,netChange:0^netChange
    from qatt where sym in syms, time<=now, (0<0^qbid)|0<0^qask;
  qt:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from qt;
  / anchored on the last NORMAL quote, not on rows inside the window: a pinned
  / stock often stops quoting altogether
  k:select firstQuote:first time, qbid:last qbid, qask:last qask,
      netChange:last netChange, lim:last lim,
      lastNormal:max ?[lim;0Nt;time]
    by sym from qt;
  k:select from (0!k) where lim, (null lastNormal)|lastNormal<t0;
  / just after the last normal quote, or the first quote of the day if it has
  / never been two sided
  k:update since:?[null lastNormal;firstQuote;lastNormal] from k;
  r:o ij `sym xkey delete lim,firstQuote,lastNormal from k;
  r:update pxLimit:?[0=qbid;qask;qbid] from r;
  / one sided settles itself; locked needs the close; netChange is a last
  / resort because it comes off the last traded price
  r:update dir:?[0=qask;`up;?[0=qbid;`down;
      ?[pxLimit>ref;`up;?[pxLimit<ref;`down;
      ?[netChange>0;`up;?[netChange<0;`down;`unknown]]]]]] from r;
  / exec_qty and splits are zero filled; the timestamps stay null so "no
  / children yet" reads as that rather than as midnight - a parent stuck at the
  / limit with splits=0 has never had a child out at all
  `since xasc select id_target, sym, basket, side, dir, algo, beta, size,
      exec_qty:0^exec_qty, splits:0^splits,
      first_workorder, last_workorder, qbid, qask, since, latest_venue
    from r
 };
