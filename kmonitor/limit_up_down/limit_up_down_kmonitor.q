/ =============================================================================
/ limit_up_down_kmonitor.q
/
/ The KdbMonitor version of limit_up_down.  Same detection as
/ queries/limit_up_down/limit_up_down_v2.q, rebuilt as three chained datasets
/ so it can be dropped into a dashboard, reworked so that it answers
/ HISTORICALLY as well as in real time, and stitched across the RDB and the HDB
/ so a range ending today is complete.
/
/ THIS FILE IS THE SOURCE OF TRUTH.  build_dashboard.py reads the three blocks
/ below and writes limit_up_down_kmonitor_dashboard.json, which is what you
/ import in Dashboards -> Import.  Edit the q here, re-run the builder,
/ re-import.
/
/ What changed against v2
/   * ALL orders, activated or not.  target_state is still joined, but as a
/     `state` COLUMN rather than as a filter, so an order that was never
/     activated - or was cancelled while its stock was pinned - is still on the
/     blotter and says so.
/   * lasted_mins: how long the limit up/down status ran, start to finish,
/     rather than "it is still going and has been for at least N minutes".
/     Episodes that have ENDED are found too, which is what makes the
/     historical mode worth having.
/   * date is carried through everything, so a historical range covering
/     several days groups per day rather than smearing them together.
/
/ ----------------------------------------------------------------------------
/ WHICH SERVER ANSWERS WHAT
/
/   Real-time selected            -> the RDB, today, nothing else asked.
/   A range not reaching today    -> the HDB, nothing else asked.
/   A range that includes today   -> the HDB for the range, AND the RDB for
/                                    today, unioned - but only if the HDB has
/                                    not been written down for today yet.
/
/ KdbMonitor sends a dataset to ONE server: the period decides which.  So on a
/ historical period each query lands on its HDB and reaches back to its RDB
/ itself, through {{conn:ENV:realtime}} - the ENV:kind form of the handle
/ token, which names one side of an environment whatever period is running.
/
/ THE SAFEGUARD is `hasToday`, and each dataset asks it of ITS OWN server: the
/ order HDB and the quote HDB are written down on their own schedules, and one
/ having today says nothing about the other.  Where the HDB already holds
/ today, the range covers it and stitching would count today twice, so the RDB
/ is never opened.  The question is asked of the table as a whole rather than
/ of the rows being hunted, so a day with nothing to report is not mistaken for
/ a day that is missing.
/
/ This needs each HDB process to be able to reach its RDB.  If it cannot, hopen
/ throws and the panel shows the error - which is the right failure: a silently
/ short answer is the thing being fixed here.
/ ----------------------------------------------------------------------------
/
/ The three datasets, in order.  Each one narrows the next.
/   1. order_syms   OMS     the names we have orders on today / over the range
/   2. limit_state  QUOTES  those names' limit up/down episodes, out of qatt
/   3. blotter      OMS     the orders on the names that had an episode
/
/ ENV NAMES.  "OMS" and "QUOTES" below are placeholders - change them to your
/ own environment names in Admin, in both places per block: the env= in the
/ header and the {{conn:...:realtime}} inside.  Both environments need a
/ real-time AND a historical server registered for any of this to resolve.
/
/ {{...}} TOKENS are KdbMonitor's, not q's, and are filled in before the query
/ is sent:
/   {{#historical}}..{{/historical}}  kept only when the period is historical
/   {{#realtime}}..{{/realtime}}      kept only when the period is real time
/   {{date_from}} {{date_to}}         the chosen range, as 2026.08.18
/   {{order_syms.sym}}                that column of an earlier dataset, as a q list
/   {{table:limit_state}}             a whole earlier dataset, as a q table
/   {{param:min_mins}}                the reader's minimum-duration setting
/   {{conn:OMS:realtime}}             that RDB's `:host:port, in either period
/ =============================================================================


/ ==== DATASET: order_syms | env=OMS ====
/ Every name we have a parent order on, minus the markets that have no daily
/ price limit at all.  Small, and it is only here to keep the qatt scan in
/ dataset 2 down to our own names.
{[nl]
  / target carries a date column on the RDB as well as the HDB, so one lambda
  / taking a list of dates runs on either server.
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
/ One row per name per day: its longest limit up/down episode, and how long it
/ lasted.  Everything else on the dashboard hangs off this.
{[syms;minMins]
  now:.z.T;
  / The quote RDB has NO date column - it holds one session - so unlike every
  / other table here the two halves cannot share a lambda.  This is the RDB
  / half: the whole query in real time, and today's slice when stitching.  qatt
  / `sym carries the `g attribute, so naming the syms is an index lookup rather
  / than a scan of the day.
  rdb:{[syms]
    update date:.z.D from
      select time,sym,qbid,qask from qatt where sym in syms
   };
  qt:{{#realtime}}rdb syms{{/realtime}}{{#historical}}{[syms;rdb]
      want:{{date_from}}+til 1+{{date_to}}-{{date_from}};
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
  / A row with nothing on either side is not a quote - it is a trade print or a
  / pre-open gap - and it would otherwise read as one sided and break a run.
  qt:select date,time,sym,qbid:0^qbid,qask:0^qask from qt where (0<0^qbid)|0<0^qask;
  qt:`date`sym`time xasc qt;
  / Locked, or one sided with a price on the surviving side.
  qt:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from qt;
  / Run id: a counter that ticks over every time the flag flips, within a name
  / and a day.  This is what turns a quote stream into episodes.
  qt:update run:sums differ lim by date,sym from qt;
  r:0!select started:first time, lastq:last time, quotes:count i,
      qbid:last qbid, qask:last qask, lim:first lim
    by date,sym,run from qt;
  / Market, off the sym suffix.  A stock pinned at the limit usually stops
  / quoting altogether, so the end of its session has to be read off the other
  / names in the same market rather than off its own last quote.
  r:update mkt:`$last each "." vs/: string sym from r;
  r:update sessionEnd:max lastq by date,mkt from r;
  / An episode ends when the next run begins.  The last run of the day has no
  / next one, so it was still open when the data ran out.
  r:update nextStart:next started by date,sym from r;
  e:select from r where lim;
  e:update ongoing:null nextStart from e;
  / An episode still open on TODAY's rows runs to now; one still open on an
  / earlier day ran to the end of that market's session.  Keyed off the row's
  / own date rather than off the period, so a stitched frame - today beside
  / last week - gets both right in the same pass.
  e:update ended:?[ongoing;?[date=.z.D;now;sessionEnd];nextStart] from e;
  e:update lasted_mins:"j"$(ended-started)%60000 from e;
  / How many separate episodes that name had that day, counted before the
  / collapse below.  A name that flickered in and out reads very differently
  / from one that locked once and stayed there, and this is what says so.
  e:update episodes:count i by date,sym from e;
  / Longest episode per name per day: sort ascending, then take the last row of
  / each group, which is what a bare select-by returns.
  e:0!select by date,sym from `lasted_mins xasc e;
  select date,sym,mkt,
      kind:?[qbid=qask;`locked;`oneSided],
      dir0:?[0=qask;`up;?[0=qbid;`down;`unknown]],
      qbid,qask,started,ended,lasted_mins,ongoing,episodes,quotes
    from e where lasted_mins>=minMins
 }[{{order_syms.sym}};{{param:min_mins}}]
/ ==== END ====


/ ==== DATASET: blotter | env=OMS ====
/ The monitoring blotter: every order, activated or not, on a name that had an
/ episode - with the episode joined on beside it.
{[lim]
  syms:exec distinct sym from lim;
  dts:exec distinct date from lim;
  / One assembly, parameterised by the dates wanted.  Every table it touches
  / carries a date column on the RDB as well as the HDB, so the same lambda
  / runs on either server.
  mk:{[dts;syms]
    t:select date,id_server,id_target,sym,basket,side,size,algo,beta
      from target where date in dts, sym in syms;
    ids:exec distinct id_target from t;
    / State as a COLUMN, not as a filter - that is the change from v2.  A
    / parent with no state row at all comes back null, which is worth seeing.
    s:`date`id_server`id_target xkey select state:last state
      by date,id_server,id_target
      from target_state where date in dts, id_target in ids;
    / Only the closes are taken from target_stock: it also has sym and beta,
    / and lj would let those overwrite the ones from target.  One row per
    / target, so no aggregation.
    x:`date`id_server`id_target xkey select date,id_server,id_target,adjclose,orgclose
      from target_stock where date in dts, id_target in ids;
    / One row per parent: what the children have done so far.
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
  / dts comes from the episodes rather than from the period, so it is already
  / whatever dataset 2 actually found - today included, if it stitched.
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
  / Direction.  One sided settles itself and dataset 2 has already said so; a
  / locked quote needs the previous close, which only exists on this side.
  r:update pxLimit:?[0=qbid;qask;qbid] from r;
  r:update dir:?[dir0<>`unknown;dir0;
      ?[pxLimit>ref;`up;?[pxLimit<ref;`down;`unknown]]] from r;
  `lasted_mins xdesc select date,id_target,sym,mkt,basket,side,state,dir,algo,beta,size,
      exec_qty:0^exec_qty, splits:0^splits,
      first_workorder, last_workorder,
      kind, qbid, qask, started, ended, lasted_mins, ongoing, episodes, latest_venue
    from r
 }[{{table:limit_state}}]
/ ==== END ====


/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. THE STITCH, and why each dataset checks its own server.  The order HDB and
/    the quote HDB are written down on their own schedules, so one having
/    today's partition says nothing about whether the other does.  Each dataset
/    therefore asks hasToday of the server it is running on, and reaches for
/    its own RDB independently.  A day where the quotes are written but the
/    orders are not - or the reverse - comes out right either way.
/
/ 2. WHY qatt IS THE ODD ONE.  Every other table here carries a date column on
/    the real-time side as well as the historical one, so one lambda taking a
/    list of dates serves both servers.  The quote RDB holds a single session
/    and has no date column at all, so its half is a separate lambda that
/    stamps date:.z.D on the way out.  Check that your HDB really does expose
/    the quote history under the name qatt; if it is called something else,
/    that one word is the only edit.
/
/    Those lambdas are sent over the handle as serialized q and carry no
/    reference to the locals of the function that defined them, which is why
/    syms and nl are passed as arguments rather than closed over.
/
/ 3. lasted_mins IS START TO FINISH.  An episode starts at its first limit
/    quote and ends at the first normal two-sided quote after it.  Where there
/    is no such quote the episode was still open when the data ran out, and
/    ongoing says so: it is then measured to now if the row is today's, and to
/    the end of that market's session if it is an earlier day.  That test is on
/    the ROW's date, not on the period, which is what lets a stitched frame
/    hold today and last week side by side and get both right.  ended is never
/    null - an ongoing episode carries the as-of time - so the column keeps its
/    type when the frame goes back to the OMS as a q table for the join.
/
/    The start is the first LIMIT quote, not the last normal one.  The truth is
/    somewhere between the two, and this is the conservative end of it: the
/    number under-reports rather than over-reports.
/
/ 4. THE SESSION END, for an earlier day, is the last quote seen on ANY of our
/    names in the same market that day - not the pinned name's own last quote,
/    which is usually early in the morning and would report a five-hour lock as
/    twenty minutes.  It is a proxy for the close and only as good as the
/    coverage: a market where we hold one order, on the name that is pinned,
/    has nothing else to read the session end from and will under-report.
/
/ 5. ONE ROW PER NAME PER DAY - the longest episode.  episodes counts how many
/    there were, so a name that went in and out five times is distinguishable
/    from one that locked once and stayed there; quotes is how many quote
/    updates arrived during the episode that was kept, and a genuinely pinned
/    stock has very few.
/
/ 6. min_mins IS A DASHBOARD PARAMETER, default 20, and it lives inside the
/    query - so changing it re-queries rather than re-filtering what is already
/    in hand.  That is deliberate: it also decides which names reach dataset 3,
/    and a threshold applied after the join would leave the blotter showing
/    orders whose episode had already been ruled out.
/
/ 7. MARKETS WITH NO PRICE LIMIT (HK, AU, SP, NZ) are excluded in dataset 1, so
/    everything downstream inherits it.  Same blacklist as limit_up_down.q, and
/    the same caveat: a new venue without limits is a false positive until it
/    is added to the list.
/
/ 8. dir0 is dataset 2's verdict from the quote alone - up when there is no
/    offer, down when there is no bid, unknown when it is locked on both sides.
/    Dataset 3 finishes it against adjclose/orgclose, which is the only place
/    that reference price exists.  netChange is not used at all here: it comes
/    off the last traded price and is therefore 0 or null on exactly the stocks
/    being hunted.
/
/ 9. THE JOIN runs on the OMS side, with the episode table sent along inside
/    the query as a literal - {{table:limit_state}}.  That is the small table, a
/    handful of names, and doing it this way keeps the qatt lookup indexed to
/    our own syms rather than scanning the market.  If the episode list ever
/    got large the trade would reverse.
/
/ 10. A PARTIAL WRITEDOWN IS THE ONE CASE hasToday GETS WRONG.  It reads as
/     "the HDB has today", so the RDB is not consulted and whatever had not
/     been written yet is missing.  A writedown that publishes the partition
/     only when it is complete - the normal arrangement - is not affected.
/
/ 11. SYMBOLOGY.  Same assumption as every other script here: target`sym and
/     qatt`sym spell the same stock the same way.  If they do not, dataset 2
/     comes back empty and so does everything after it.
/ -----------------------------------------------------------------------------
