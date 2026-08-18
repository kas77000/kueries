/ limit_up_down.q - our ACTIVATED parent orders whose stock has been stuck
/ limit up / limit down for more than N minutes, counting back from now.
/
/   q)\l queries/limit_up_down/limit_up_down.q
/   q)h:hopen`:orderserver:5010
/   q)limitUpDown[h;20]                        / pass 0i if the tables are local
/   q)select from limitUpDown[h;20] where country=`JP
/
/ A quote counts as limit up/down when it is LOCKED (qbid=qask) or ONE SIDED
/ (one side zero, the other carrying the limit price), and only if the stock
/ has had no normal two-sided quote anywhere in the last N minutes.
/
/ pctFromClose is the sanity check: a genuine limit sits AT the band, so
/ something locked at +0.1% is a locked market, not a limit.  Bands differ a
/ lot across APAC - verify against your own reference data.

/ lookback is in minutes.  NOT named mins - that is the q keyword for running
/ minimums, and using it as a parameter gives a type error.
limitUpDown:{[h;lookback]
  now:.z.T;
  t0:now-60000*lookback;
  d:.z.D;
  / markets with NO daily price limit: one sided or locked there is a thin
  / book, a stale quote, a halt or an auction imbalance, never a limit.  A
  / blacklist, so a new venue without limits is a false positive until added.
  nl:("*.HK";"*.AU";"*.SP";"*.NZ");
  / nl is passed in, not closed over: a lambda sent over IPC carries no
  / reference to the locals of the function that defined it
  f:{[d;nl]
    t:select date,id_server,id_target,sym,trader,side,sidesign,size,algo,
        t_start,t_end
      from target where date=d, not any sym like/: nl;
    ids:exec distinct id_target from t;
    s:select state:last state, leave:last leave by date,id_server,id_target
      from target_state where date=d, id_target in ids;
    s:`date`id_server`id_target xkey select from (0!s) where state=`activated;
    / one row per target, so no aggregation needed
    x:`date`id_server`id_target xkey select date,id_server,id_target,
        adjclose,orgclose,country,region,currency
      from target_stock where date=d, id_target in ids;
    select from ((t lj s) lj x) where not null state
    };
  / 0<h, not null h: null 0i is false and handle 0 is the current process
  o:$[0<h; h(f;d;nl); f[d;nl]];
  if[0=count o; :o];
  / the previous close every APAC band is measured from
  o:update ref:orgclose^adjclose from o;
  / qatt`sym carries the `g attribute, so naming our syms is an index lookup
  syms:exec distinct sym from o;
  / rows with nothing on either side are trade prints or pre-open gaps - they
  / would read as one sided and break the streak
  q:select time,sym,qbid:0^qbid,qask:0^qask,netChange:0^netChange
    from qatt where sym in syms, time<=now, (0<0^qbid)|0<0^qask;
  q:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from q;
  / anchored on the last NORMAL quote rather than on rows inside the window: a
  / pinned stock often stops quoting altogether, and would have none
  k:select firstQuote:first time, lastQuote:last time, nquotes:count i,
      qbid:last qbid, qask:last qask, netChange:last netChange, lim:last lim,
      lastNormal:max ?[lim;0Nt;time]
    by sym from q;
  / in limit now, and nothing normal anywhere in the window.  A null lastNormal
  / means it has been one sided or locked since its first quote of the day.
  k:select from (0!k) where lim, (null lastNormal)|lastNormal<t0;
  k:update kind:?[qbid=qask;`locked;`oneSided],
      forMins:"j"$(now-?[null lastNormal;firstQuote;lastNormal])%60000
    from k;
  / same symbology assumption as jp_no_print: if target`sym and qatt`sym
  / disagree, this returns nothing at all
  r:o ij `sym xkey delete lim from k;
  r:update pxLimit:?[0=qbid;qask;qbid] from r;
  r:update pctFromClose:100*(pxLimit-ref)%ref from r;
  / one sided settles itself; locked needs the close.  netChange is a last
  / resort only - it comes off the last TRADED price, so it is 0 or null on
  / exactly the stocks being hunted.  `unknown means go and look.
  r:update dir:?[0=qask;`up;?[0=qbid;`down;
      ?[pxLimit>ref;`up;?[pxLimit<ref;`down;
      ?[netChange>0;`up;?[netChange<0;`down;`unknown]]]]]] from r;
  / buying into a limit up, or selling into a limit down, is the painful side.
  / Assumes sidesign is +1 buy / -1 sell.
  r:update blocked:((sidesign>0)&dir=`up)|((sidesign<0)&dir=`down) from r;
  `blocked`forMins xdesc delete orgclose,adjclose from r
 };
