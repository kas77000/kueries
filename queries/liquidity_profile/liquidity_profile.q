/ liquidity_profile.q - where a stock's liquidity sat during the day: one row
/ per intraday bucket, carrying that bucket's share of the day's volume.
/
/   q)\l queries/liquidity_profile/liquidity_profile.q
/   q).lp.show[`0700.HK;2026.08.25;00:10]      / bars, to read in the console
/   q).lp.profile[`0700.HK;2026.08.25;00:10]   / the numbers, to chart or save
/   q).lp.profile[`0700.HK;.lp.live;00:10]     / on the RDB: today, so far
/
/ TWO SERVERS, and now two of each.  .lp.buckets, .lp.profile and .lp.quotes
/ read qatt; .lp.execs, .lp.orders and .lp.tgt read the order tables.  Load
/ this file onto all four processes - the quote HDB, the quote RDB, the order
/ HDB, the order RDB - and call each function on the handle that has its
/ tables.  Nothing here opens a handle itself.
/
/ AUCTIONS ARE IN.  The open and close buckets carry the auction alongside the
/ continuous prints, so in HK, JP and AU the last bar is fat by construction.
/ That is real liquidity, and the point of a histogram is that you can see it
/ sitting there: a steady afternoon and a flat day with one enormous closing
/ auction look nothing alike here, where any single front/back number scores
/ them the same.
/
/ TIME IS THE PLANT'S CLOCK, not the exchange's.  qatt`time ran 8 hours ahead
/ of quoteTime (UTC) in the sample, i.e. HKT - so a HK name's buckets read as
/ Hong Kong local, and a Tokyo name's read an hour ahead of Tokyo.  Nothing is
/ converted here.
/
/ If a call fails with a bare `type or `length, run the stages in .lp.types,
/ .lp.cols, .lp.rows and .lp.buckets - see DIAGNOSIS at the foot of this file.

/ =============================================================================
/ THE ARGUMENTS ARE COERCED.  Both of these cost one line and remove the two
/ ways a caller's types can differ from the table's, each of which fails with
/ an error that names neither the column nor the argument.
/ =============================================================================

/ MILLISECONDS, ALWAYS.  "t"$00:10 is 00:10:00.000, so 00:10 and 00:10:00.000
/ both mean ten minutes.  Casting between temporal types converts units;
/ ARITHMETIC ON THEM DOES NOT.  00:10 is a minute carrying the underlying value
/ 10, and a time is a count of milliseconds, so an unchecked `00:10 xbar time`
/ buckets by ten MILLISECONDS.  The cast removes the trap.
.lp.bkt:{"t"$x};

/ qatt`sym is a symbol column, so `sym=s` needs s to be a symbol: hand it the
/ char vector "0700.HK" instead and q compares a column of N rows against a
/ list of 7 characters and answers `length, naming nothing.  A client sending a
/ string is the normal case rather than a mistake - pykx maps python bytes to a
/ char vector, which is what market_stats.q's `like` wants - so take either.
.lp.sym:{$[-11h=type x; x; `$x]};

/ =============================================================================
/ REAL TIME.  Pass .lp.live - an empty date list - as dt and the date
/ constraint is DROPPED, which is what an RDB wants: it holds today and only
/ today, so there is nothing left to constrain.  qatt in memory may not even
/ carry a date column to constrain against; on the HDB date is the PARTITION,
/ which is selectable but is not one of the row's stored columns either.
/
/ NOT date=.z.D.  .z.D is the SERVER's date, and this plant's clock runs ahead
/ of UTC (see the note on time above), so either side of midnight UTC the
/ server's date and the trading date are not the same day.  An RDB holds one
/ day by construction, so it needs no such guess - and a guess that is wrong
/ here returns nothing at all rather than failing.
/
/ Every reader below therefore has TWO branches, live and dated, with the same
/ columns in both.  CHANGE ONE AND CHANGE THE OTHER - the same arrangement
/ kmonitor/dark_summary uses, for the same reason.
/ =============================================================================

.lp.live:0#0Nd;
.lp.isLive:{$[0=count x; 1b; all null x]};

/ the server's own clock, so a live chart can say what "so far" means.  Read
/ off the process that answered, not off the machine running the script.
.lp.now:{([] date:enlist .z.D; time:enlist .z.T)};

/ what a day with no prints comes back as - typed, so the caller charts an
/ empty day rather than handling a special case
.lp.empty:([] bkt:0#0Nt; trades:0#0j; shares:0#0j; turnover:0#0n;
  pct:0#0n; cum_pct:0#0n);

/ =============================================================================
/ The worker, in two halves so a failure can be placed: .lp.buckets reads qatt
/ and aggregates, .lp.profile fills the gaps and takes the percentages.
/ =============================================================================

/ s is one sym.  dt is a date, or a list of dates for the shape of a typical
/ day - the counts then total across the dates, the percentages do not.
/ An empty general list means the name did not trade.
.lp.buckets:{[s;dt;bkt]
  ms:"j"$.lp.bkt bkt;
  sy:.lp.sym s;
  / price>0 and size>0 is the whole test for "this row is a print": every qatt
  / row is a transaction carrying the quote that stood at the time, so there
  / are no quote-only rows to exclude.  See market_stats.q note 8.
  t:$[.lp.isLive dt;
    select time,price,size from qatt where sym=sy, price>0, size>0;
    select time,price,size from qatt where date in dt, sym=sy, price>0, size>0];
  if[0=count t; :()];
  / BUCKETED IN MILLISECONDS, not with xbar against a temporal.  "j"$time is
  / the count of ms since midnight, div ms is the bucket's index, *ms is its
  / start and "t"$ puts it back on the clock - four steps in one unit, with no
  / cross type temporal arithmetic anywhere in them.  xbar would read as the
  / obvious thing to write here, and it is exactly what the comment on .lp.bkt
  / warns about: its two arguments have to already agree.
  / "j"$size before summing - size is an int, and a day of a heavily traded
  / small cap goes past the 2.1bn an int tops out at.
  0!select trades:count i, shares:sum "j"$size, turnover:sum price*"f"$size
    by bkt:"t"$ms*("j"$time) div ms from t
 };

.lp.profile:{[s;dt;bkt]
  r:.lp.buckets[s;dt;bkt];
  if[0=count r; :.lp.empty];
  ms:"j"$.lp.bkt bkt;
  / every bucket from the first print to the last, so the lunch break and a
  / dead hour read as empty bars rather than as rows that are not there.
  / In milliseconds again, for the same reason.
  lo:"j"$first r`bkt;
  hi:"j"$last r`bkt;
  r:([] bkt:"t"$lo+ms*til 1+(hi-lo) div ms) lj `bkt xkey r;
  r:update trades:0^trades, shares:0^shares, turnover:0f^turnover from r;
  tot:sum r`shares;
  update pct:100*shares%tot, cum_pct:100*(sums shares)%tot from r
 };

/ =============================================================================
/ THE QUOTE CURVE.  Every print's quote, at full resolution - NOT a bucket
/ average.  qatt has no quote-only rows, so this is the quote as it stood at
/ each transaction, which on a liquid name is a hundred thousand points or
/ more; the caller draws them as a line, not as marks, and says how many.
/
/ Rows with a side missing are dropped.  A trade print or a pre-open gap can
/ carry a zero or a null on one side, and one zero drags the curve to the
/ floor - the same rows limit_up_down.q has to exclude for the same reason.
/ =============================================================================

.lp.quotes:{[s;dt]
  sy:.lp.sym s;
  $[.lp.isLive dt;
    select time,qbid,qask from qatt where sym=sy, qbid>0, qask>0;
    select time,qbid,qask from qatt where date in dt, sym=sy, qbid>0, qask>0]
 };

/ =============================================================================
/ THE ORDER SIDE.  These three read an ORDER server, not a quote server - the
/ order HDB for a date, the order RDB for today.  Call each function on the
/ handle that has its tables, the way market_stats.q's fxOn is called on the
/ order server.
/
/ id_target is an int in all three tables, so it is cast rather than trusted:
/ a python int arrives as a long, and a long against an int column is one more
/ way to earn a `type that names nothing.
/ =============================================================================

.lp.tid:{"i"$x};

/ every fill of the target, unaggregated: the price we actually traded at, with
/ a time on it.  execution is the ONLY place a fill carries a time -
/ workorder`make is the child's total with no time - so both the stacked bars
/ and the fill marks come from here, off ONE read, which is why this returns
/ the rows rather than a per-bucket sum: two reads could disagree.
/
/ fillsize>0 drops the execution rows that are state changes rather than fills.
/ id_work links each fill back to the child order that made it.
.lp.execs:{[dt;idt]
  i:.lp.tid idt;
  $[.lp.isLive dt;
    select date,id_server,id_work,id_target,time,fillprice,fillsize
      from execution where id_target=i, fillsize>0;
    select date,id_server,id_work,id_target,time,fillprice,fillsize
      from execution where date in dt, id_target=i, fillsize>0]
 };

/ every child order of the target, with the price it showed and what became of
/ it.  state AND request both come back: the caller decides what a cancel looks
/ like, and cannot do that without seeing the vocabulary this server uses.
.lp.orders:{[dt;idt]
  i:.lp.tid idt;
  $[.lp.isLive dt;
    select date,id_server,id_work,id_target,time,t_transmit,t_on_market,
        sym,side,size,make,price,state,request
      from workorder where id_target=i;
    select date,id_server,id_work,id_target,time,t_transmit,t_on_market,
        sym,side,size,make,price,state,request
      from workorder where date in dt, id_target=i]
 };

/ the parent, for the sym and the side - so --id-target alone is enough to
/ name the stock, and giving both is checked rather than assumed
.lp.tgt:{[dt;idt]
  i:.lp.tid idt;
  $[.lp.isLive dt;
    select date,id_server,id_target,sym,side,size,limit_price
      from target where id_target=i;
    select date,id_server,id_target,sym,side,size,limit_price
      from target where date in dt, id_target=i]
 };

/ Same table with a bar drawn from pct and scaled to the busiest bucket, which
/ is a full bar whatever it is worth: read the bars for the shape of the day
/ and pct for what it was actually worth.
.lp.barw:40;
.lp.bar:{[mx;p] n:$[mx>0; "i"$.lp.barw*p%mx; 0i]; (n#"#"),(.lp.barw-n)#"."};
.lp.show:{[s;dt;bkt]
  r:.lp.profile[s;dt;bkt];
  if[0=count r; :r];
  update bar:.lp.bar[max pct] each pct from r
 };

/ =============================================================================
/ DIAGNOSIS.  q answers a mismatched argument with `type or `length and names
/ nothing - not the column, not the argument, not the line.  These run the same
/ pipeline in stages, each one safe to call on its own, so the stage that fails
/ IS the answer.  The script's --probe walks them in order.
/
/   q).lp.types[`0700.HK;2026.08.25;00:10]   / what q was actually handed
/   q).lp.cols[]                             / what qatt is made of
/   q).lp.rows[`0700.HK;2026.08.25]          / does the where clause run
/   q).lp.rows[`0700.HK;.lp.live]            / the same against an RDB
/   q)count .lp.buckets[`0700.HK;2026.08.25;00:10]    / does the bucketing
/ =============================================================================

/ cannot fail: it touches no table and coerces nothing
.lp.types:{[s;dt;bkt] `arg_sym`arg_dt`arg_bkt!(type s;type dt;type bkt)};

/ the columns this query depends on, as the process actually stores them.
/ date is in the list on purpose: whether it comes back is how you tell an HDB
/ partition from an RDB that has no date column at all.
.lp.cols:{exec c!t from 0!meta qatt where c in `date`time`sym`price`size};

/ the where clause on its own - a count, so nothing large comes back
.lp.rows:{[s;dt]
  sy:.lp.sym s;
  $[.lp.isLive dt;
    count select from qatt where sym=sy, price>0, size>0;
    count select from qatt where date in dt, sym=sy, price>0, size>0]
 };
