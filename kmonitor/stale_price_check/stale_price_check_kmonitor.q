/ stale_price_check_kmonitor.q - every TAKE order under a live parent, with the
/ touch it should have been sitting on beside the price it was actually sent
/ at.  KdbMonitor version of queries/stale_price_check/stale_price_check.q.
/
/ Source of truth: build_dashboard.py turns the two blocks below into the
/ importable JSON.  Edit here, re-run the builder, re-import.
/
/ Two chained datasets - dataset 1 already carries every order field, so
/ dataset 2 finishes the job and there is no third hop back to the OMS:
/   live_takes   OMS   take orders under an activated parent, with ticksize
/   touch_check  QATT  the book at each t_gen, and how far off it they were
/
/ "OMS" and "QATT" are the environment names; set your own in the env= of each
/ DATASET header and nowhere else.  Details in README.md.
/
/ REAL TIME ONLY.  Both datasets are pinned to the real-time period, so both
/ always run against an RDB and always mean today.  That is why there is no
/ {{#historical}} branch in either block and no {{conn:...}} anywhere: a
/ dataset reaches past its own environment only to stitch a historical range
/ onto today, and there is no historical range here to stitch.
/
/ WHY A TAKE ORDER IS THE SHARP TEST.  A take lifts the offer or hits the bid,
/ so its price is dictated by the book at the instant it was generated, not
/ chosen.  Sent at anything else, the book the algo was looking at was not the
/ book that existed.  The reference is therefore the FAR TOUCH, by side:
/     buy   -> qask     you lift the offer
/     sell  -> qbid     you hit the bid
/ SHORT SALES ARE DROPPED: a short-sale price test can stop the order sitting
/ at the bid, and it would read as off-touch for a reason that has nothing to
/ do with stale data.
/
/ ticks_off is signed and means the same thing on both sides - how far the
/ price sits ABOVE the touch, in ticks, with ticksize off target_stock.
/ Max ticks flags on the absolute value.
/
/ AGGRESSIVE TAKES ARE DROPPED - a buy ABOVE the offer, a sell BELOW the bid.
/ Those cross and fill at the touch anyway, so they are deliberate aggression
/ rather than a book the algo misread.  Stale data shows up as the opposite: a
/ book that has moved away leaves the buy below the offer and the sell above
/ the bid, sitting there not filling.
/
/ ONLY BREACHES COME BACK.  An order on the touch is the book behaving, so `ok
/ drops out and AN EMPTY PANEL IS THE GOOD ANSWER.
/
/ THE SIDE AND VENUE VOCABULARIES ARE NOT KNOWN HERE.  venue is matched as
/ "contains TAKE" case-insensitively and side is read off its text, so both raw
/ values stay in the output where a wrong reading is visible.  The bare q ships
/ stalePriceVocab to list what the server really uses - run that first.
/
/ THE LOOKBACK IS IN MINUTES, and it bounds t_gen - how recently the workorder
/ was created.  It is what keeps this runnable on a refresh: reading the whole
/ session out of qatt is far too slow to do every thirty seconds.
/
/ TWO WINDOWS, AND qatt's IS THE WIDER ONE.  The orders come from the last
/ lookback minutes and qatt is read from TWICE that far back, because an order
/ generated at the very start of the order window still needs quotes before it
/ to land on.  So noquote means "no two-sided quote in the scanned window"
/ rather than "never quoted today".


/ ==== DATASET: live_takes | env=OMS ====
/ Take orders under a live parent, one row per child, with the tick size that
/ turns a price difference into ticks.
{[lookback]
  / dts is today and t0 is how far back t_gen may sit.  target_state,
  / workorder0 and target_stock all carry date on the RDB, so the constraint is
  / written exactly as it would be against the HDB and means one day either
  / way - which is what lets this lambda be lifted onto the HDB unchanged if
  / this dashboard is ever given a historical period.
  mk:{[dts;t0]
    s:0!select state:last state by date,id_server,id_target
      from target_state where date in dts;
    ids:exec distinct id_target from s where state=`activated;
    / TAKE VENUES ONLY.  upper-cased because q's like is case sensitive and the
    / venue vocabulary is not known here.  the constraint sits last so it runs
    / on the rows the three cheap ones already left.
    w:select date,id_server,id_target,id_work,sequence,trader,sym,side,size,
        otype,state,venue,venuetype,t_gen,price
      from workorder0 where date in dts, t_gen>=t0, id_target in ids,
        (upper string venue) like "*TAKE*";
    / ONE ROW PER CHILD, AND IT IS THE FIRST ONE.  t_gen is stamped at
    / generation and never moves, but price IS rewritten - a chase repoints it -
    / so the last row would pair a repriced price with a generation timestamp.
    / Only state is read from the last row, being the one thing worth current.
    w:`sequence xasc w;
    w:0!select id_target:first id_target, trader:first trader, sym:first sym,
        side:first side, size:first size, otype:first otype,
        venue:first venue, venuetype:first venuetype,
        t_gen:first t_gen, price:first price, state:last state
      by date,id_server,id_work from w;
    / a market order carries price 0 and has no price to hold against a touch
    w:select from w where 0<price;
    / SIDE READ OFF ITS TEXT, not against an assumed enum.  Anything beginning
    / b buys and looks at the offer; everything else sells and looks at the bid.
    w:update sidelc:lower string side from w;
    w:update isshort:(sidelc like "*short*")|sidelc like "ss" from w;
    w:select from w where not isshort;
    w:update ref_side:?[sidelc like "b*";`ask;`bid] from w;
    / target_stock also carries sym, so only the columns wanted are taken - an
    / lj would overwrite it
    ts:`date`id_server`id_target xkey select date,id_server,id_target,ticksize
      from target_stock
      where date in dts, id_target in exec distinct id_target from w;
    delete sidelc,isshort from w lj ts
   };
  / Today, bounded to the lookback.  One call, on the environment this
  / dataset's env= names - see the REAL TIME ONLY note at the top of the file.
  mk[enlist .z.D; .z.T-60000*lookback]
 }[{{param:lookback_mins}}]
/ ==== END ====


/ ==== DATASET: touch_check | env=QATT ====
/ The whole order table comes across, because an aj needs both sides local.
{[w;lookback;maxTicks]
  now:.z.T;
  / nothing to check: back out before touching qatt.  this hands the widgets
  / the empty ORDER table rather than an empty result with the columns they
  / name, so an empty book may show as a column error rather than as no rows.
  if[0=count w; :w];
  syms:exec distinct sym from w;
  / THE QUOTE RDB HAS NO date COLUMN - it is today by definition - so the date
  / the aj below joins on is stamped on here rather than read.  It matches the
  / date dataset 1 put on its rows, which is .z.D on the same clock.
  / TWO-SIDED QUOTES ONLY: a one-sided book has no far touch to take against,
  / and a zero on either side would read as a touch of nothing.
  / sym first: the RDB keeps `g#sym, and the where clause can only use that
  / attribute on the constraint it applies first.  the time cut then runs on
  / what survives, which is already a small fraction of the session.
  rdb:{[syms;q0]
    update date:.z.D from
      select time, sym, qbid, qask, ptime:time
        from qatt where sym in syms, time>=q0, 0<0^qbid, 0<0^qask
   };
  p:rdb[syms; .z.T-2*60000*lookback];
  / aj wants the right side sorted by the join columns.  date is IN the join
  / rather than left out: both sides carry today and agree about it, and
  / keeping it there is what would let a historical period be added later
  / without an order ever being dated against another day's book.
  p:`date`sym`time xasc p;
  x:aj[`date`sym`time; update time:t_gen from w; p];

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
  / order that was born bad or a name still being fed a frozen quote.  This
  / dataset is real-time only, so "now" is this second and nothing else: on a
  / historical period it would have to be that day's last quote instead.
  c:select now_ptime:last time by date,sym from p;
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
 }[{{table:live_takes}};{{param:lookback_mins}};{{param:max_ticks}}]
/ ==== END ====
