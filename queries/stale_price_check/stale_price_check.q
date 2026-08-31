/ stale_price_check.q - every TAKE order under a live parent, with the touch it
/ should have been sitting on beside the price it was actually sent at.  Built
/ after ai3 sent orders priced off market data that had gone stale.
/
/   q)\l queries/stale_price_check/stale_price_check.q
/   q)h:hopen`:orderserver:5010
/   q)stalePriceVocab[h;10]                    / RUN THIS FIRST - see below
/   q)stalePriceCheck[h;10;5]                  / last 10 min, 5 ticks of slack
/   q)stalePriceCheck[h;10;0]                  / must be ON the touch exactly
/   q)count stalePriceCheck[h;10;5]            / 0 is the good answer
/
/ Run it FROM THE QUOTE SESSION.  qatt lives there, workorder0, target_state and
/ target_stock live on the order server, and an aj needs both sides local - so
/ the order half is a lambda shipped over h and joined here.  Pass 0i for h if
/ you happen to have both locally.
/
/ t_gen and qatt`time come off the same clock, so they compare directly.  No
/ timezone and no DST conversion anywhere in here, and that is deliberate.
/
/ WHY A TAKE ORDER IS THE SHARP TEST.  A take lifts the offer or hits the bid,
/ so its price is not a judgement call - it is dictated by the book at the
/ instant it was generated.  Sent at anything else, the book the algo was
/ looking at was not the book that existed.  That is a far tighter test than
/ holding an order price against the last print, which is what this query did
/ before and which cannot tell a stale quote from a wide spread.
/
/ So the reference is the FAR TOUCH, by side:
/     buy   -> qask     you lift the offer
/     sell  -> qbid     you hit the bid
/ SHORT SALES ARE DROPPED.  A short-sale price test can stop the order sitting
/ at the bid, and it would then read as off-touch for a reason that has nothing
/ to do with stale data.
/
/ ticks_off is signed and is simply how far the price sits ABOVE the touch, in
/ ticks: (order_price - touch) % ticksize, with ticksize off target_stock.
/ maxTicks flags on the absolute value.
/
/ AGGRESSIVE TAKES ARE DROPPED - a buy ABOVE the offer, a sell BELOW the bid.
/ Those cross and fill at the touch anyway, so they are deliberate aggression
/ rather than a book the algo misread.  Stale data shows up as the opposite: a
/ book that has moved away leaves the buy below the offer and the sell above
/ the bid, sitting there not filling.  That is the direction kept, so after the
/ filter ticks_off is <=0 on buys and >=0 on sells.
/
/ ONLY BREACHES COME BACK.  An order on the touch is the book behaving, so `ok
/ drops out and AN EMPTY TABLE IS THE GOOD ANSWER.  flag still says which kind
/ of finding each row is - off, noquote or notick.
/
/ THE SIDE AND VENUE VOCABULARIES ARE NOT KNOWN HERE.  venue is matched as
/ "contains TAKE" case-insensitively, and side is read off its text rather than
/ against an assumed enum.  Both raw values stay in the output so a wrong
/ reading is visible rather than silent, and stalePriceVocab lists what the
/ server actually uses - run it first.
/
/ lookback is IN MINUTES, and it bounds t_gen: how recently the workorder was
/ created.  It exists because reading the whole session out of qatt is too slow
/ to run live.  NOT named mins - that is the q keyword for running minimums,
/ and using it as a parameter gives a type error.
/
/ TWO WINDOWS, AND qatt's IS THE WIDER ONE.  The orders come from the last
/ `lookback` minutes; qatt is read from TWICE that far back, because an order
/ generated at the very start of the order window still needs quotes before it
/ to land on.  So `noquote` means "no two-sided quote in the scanned window"
/ rather than "never quoted today" - still a finding, since a name we are
/ taking on that has not quoted in 2x lookback is stale by any reading.

stalePriceCheck:{[h;lookback;maxTicks]
  now:.z.T;
  d:.z.D;
  / t0 bounds the orders; q0 bounds qatt, and has to reach further back so an
  / order at t0 still has something to asof onto
  t0:now-60000*lookback;
  q0:t0-60000*lookback;

  / ---- order server ---------------------------------------------------
  / One lambda, shipped whole.  target_state, workorder0 and target_stock all
  / carry date on the RDB as well as the HDB, so this body serves either side.
  f:{[d;t0]
    / the live book: latest state per parent, keep the activated ones
    s:0!select state:last state by date,id_server,id_target
      from target_state where date=d;
    ids:exec distinct id_target from s where state=`activated;
    / TAKE VENUES ONLY.  upper-cased because q's like is case sensitive and the
    / venue vocabulary is not known here.  the constraint sits last so it runs
    / on the rows the three cheap ones already left.
    w:select date,id_server,id_target,id_work,sequence,trader,sym,side,size,
        otype,state,venue,venuetype,t_gen,price
      from workorder0 where date=d, t_gen>=t0, id_target in ids,
        (upper string venue) like "*TAKE*";
    / ONE ROW PER CHILD, AND IT IS THE FIRST ONE.  workorder0 writes a row per
    / state change.  t_gen is stamped at generation and never moves, but price
    / IS rewritten - a chase repoints it - so taking the last row pairs a
    / repriced price with a generation timestamp and the comparison stops
    / meaning anything: right time, wrong price.
    / Everything describing the order AS GENERATED therefore comes off the
    / first row by sequence.  Only state is read from the last, because the
    / current state is the one thing you want current.
    w:`sequence xasc w;
    w:0!select id_target:first id_target, trader:first trader, sym:first sym,
        side:first side, size:first size, otype:first otype,
        venue:first venue, venuetype:first venuetype,
        t_gen:first t_gen, price:first price, state:last state
      by date,id_server,id_work from w;
    / a market order carries price 0 and has no price to hold against a touch
    w:select from w where 0<price;
    / SIDE READ OFF ITS TEXT, not against an assumed enum - the vocabulary is
    / not known here.  Anything beginning b buys and looks at the offer;
    / everything else sells and looks at the bid.  raw side stays in the output.
    w:update sidelc:lower string side from w;
    / short sales dropped - see the header
    w:update isshort:(sidelc like "*short*")|sidelc like "ss" from w;
    w:select from w where not isshort;
    w:update ref_side:?[sidelc like "b*";`ask;`bid] from w;
    / ticksize turns a price difference into ticks.  target_stock also carries
    / sym, so only the columns wanted are taken - an lj would overwrite it.
    ts:`date`id_server`id_target xkey select date,id_server,id_target,ticksize
      from target_stock
      where date=d, id_target in exec distinct id_target from w;
    delete sidelc,isshort from w lj ts
   };
  w:$[0<h; h(f;d;t0); f[d;t0]];
  / nothing to check: back out before touching qatt.  note this returns the
  / EMPTY ORDER TABLE, not an empty result with the columns below.
  if[0=count w; :w];

  / ---- quote server, local --------------------------------------------
  syms:exec distinct sym from w;
  / TWO-SIDED QUOTES ONLY.  a one-sided book has no far touch to take against,
  / and a zero on either side would read as a touch of nothing.
  / sym first: the RDB keeps `g#sym, and the where clause can only use that
  / attribute on the constraint it applies first.  the time cut then runs on
  / what survives, which is already a small fraction of the session.
  p:select time, sym, qbid, qask, ptime:time
    from qatt where sym in syms, time>=q0, 0<0^qbid, 0<0^qask;
  / aj wants the right side sorted by the join columns.  the filter above drops
  / the `g#sym the RDB keeps, so sort it back.
  p:`sym`time xasc p;
  / single day, and qatt has no date column on the RDB - so sym and time are
  / the whole key here.  the kmonitor version joins on date too.
  x:aj[`sym`time; update time:t_gen from w; p];

  / ---- the touch, and how far off it the order was ---------------------
  x:update quote_age_ms:"j"$t_gen-ptime from x;
  / the far touch: a buy lifts the offer, a sell hits the bid
  x:update touch:?[ref_side=`ask; qask; qbid] from x;
  / signed, and it means the same thing on both sides: how far the price sits
  / ABOVE the touch
  x:update ticks_off:?[0<ticksize; (price-touch)%ticksize; 0n] from x;
  x:update ticks_abs:abs ticks_off from x;
  / AGGRESSIVE TAKES ARE NOT THIS REPORT'S BUSINESS.  A buy above the offer or
  / a sell below the bid crosses, and it fills at the touch anyway - that is
  / deliberate aggression, not a book the algo misread.
  / Stale data shows up as the OPPOSITE: a book that has moved away leaves the
  / buy BELOW the offer and the sell ABOVE the bid, and the order sits there
  / not filling.  That is the direction this report keeps.
  / A null ticks_off is not aggressive either way - a null comparison is false -
  / so noquote and notick rows survive to be reported as untestable.
  x:update aggressive:((ref_side=`ask)&0<ticks_off)|((ref_side=`bid)&ticks_off<0)
    from x;
  x:select from x where not aggressive;
  / how long since the book last moved on that name - says whether this is an
  / order that was born bad or a name still being fed a frozen quote
  c:select now_ptime:last time by sym from p;
  x:x lj c;
  x:update now_age_ms:"j"$now-now_ptime from x;

  / ---- verdict --------------------------------------------------------
  / noquote and notick come first: both mean the test could not be RUN, which
  / is a different statement from the test passing
  x:update flag:?[null touch;`noquote;
      ?[null ticks_off;`notick;
      ?[maxTicks<ticks_abs;`off;`ok]]] from x;
  / BREACHES ONLY.  An order sitting on the touch is the whole book behaving,
  / and there is nothing to look at - so `ok drops out and an empty table is
  / the good answer.  flagged is therefore constant true and does not come
  / back as a column; flag still says WHICH kind of finding each row is.
  x:select from x where not flag=`ok;
  / untestable first - a row that could not be checked is worth seeing before
  / the ranked ones - then worst ticks.  ticks_abs is null on those rows, and
  / a bare xdesc would sort them to the bottom.
  r:`notest`ticks_abs xdesc update notest:flag in `noquote`notick from x;
  select date, id_server, id_target, id_work, trader, sym, side, ref_side,
      size, otype, venue, venuetype, state, t_gen, order_price:price,
      qbid, qask, touch, ticksize, ticks_off, ticks_abs, ptime, quote_age_ms,
      now_age_ms, flag
    from r
 };

/ stalePriceVocab[h;lookback] - the venue and side values the server actually
/ uses on live orders, with counts.  RUN THIS FIRST.
/
/   q)stalePriceVocab[h;10]
/
/ stalePriceCheck matches venue as "contains TAKE" and reads side off its text,
/ because neither vocabulary is known here.  This shows what is really there,
/ so a filter that silently matches nothing - or a side that silently reads as
/ a buy - is visible on the terminal instead of being inferred from an empty
/ report.  It deliberately does NOT apply the venue filter: that is the point.
stalePriceVocab:{[h;lookback]
  f:{[d;t0]
    s:0!select state:last state by date,id_server,id_target
      from target_state where date=d;
    ids:exec distinct id_target from s where state=`activated;
    w:select date,id_server,id_work,venue,venuetype,side
      from workorder0 where date=d, t_gen>=t0, id_target in ids;
    `orders xdesc 0!select orders:count distinct id_work
      by venue,venuetype,side from w
   };
  t0:.z.T-60000*lookback;
  $[0<h; h(f;.z.D;t0); f[.z.D;t0]]
 };

/ stalePriceRows[h;idWork] - every workorder0 row for one child, in write
/ order, with all four price fields beside each other.
/
/   q)stalePriceRows[h;5001i]
/
/ Reach for it when a number in the report looks wrong.  It shows whether price
/ moved over the order's life, which row the report takes (the first), and what
/ limit_target / limit_candidate hold on that same row - so a disagreement
/ names itself instead of having to be guessed at from one collapsed row.
stalePriceRows:{[h;idWork]
  f:{[d;idWork]
    `sequence xasc select sequence,time,t_gen,state,sym,side,size,otype,
        venue,venuetype,price,limit_target,limit_candidate,bps_candidate
      from workorder0 where date=d, id_work=idWork
   };
  $[0<h; h(f;.z.D;idWork); f[.z.D;idWork]]
 };
