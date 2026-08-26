/ liquidity_profile.q - where a stock's liquidity sat during the day: one row
/ per intraday bucket, carrying that bucket's share of the day's volume.
/
/   q)\l queries/liquidity_profile/liquidity_profile.q
/   q).lp.show[`0700.HK;2026.08.25;00:10]      / bars, to read in the console
/   q).lp.profile[`0700.HK;2026.08.25;00:10]   / the numbers, to chart or save
/
/ Runs where qatt is.  No handle, no order tables, no FX - one name in one
/ currency needs no rate.
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

/ THE BUCKET IS CAST, NOT USED RAW.  "t"$00:10 is 00:10:00.000, so 00:10 and
/ 00:10:00.000 both mean ten minutes.  Casting between temporal types converts
/ units; ARITHMETIC ON THEM DOES NOT.  00:10 is a minute, carrying the
/ underlying value 10, and a time is a count of milliseconds - so an unchecked
/ `00:10 xbar time` buckets by ten MILLISECONDS.  The cast removes the trap.
.lp.bkt:{"t"$x};

/ THE SYM IS COERCED.  qatt`sym is a symbol column, so `sym=s` needs s to be a
/ symbol: hand it the char vector "0700.HK" instead and q compares a column of
/ N rows against a list of 7 characters and answers 'length, naming nothing.
/ A client sending a string is the normal case, not a mistake - pykx maps
/ python bytes to a char vector, which is what market_stats.q's `like` wants -
/ so take either and convert here.
.lp.sym:{$[-11h=type x; x; `$x]};

/ what a day with no prints comes back as - typed, so the caller charts an
/ empty day rather than handling a special case
.lp.empty:([] bkt:0#0Nt; trades:0#0j; shares:0#0j; turnover:0#0n;
  pct:0#0n; cum_pct:0#0n);

/ s is one sym.  dt is a date, or a list of dates for the shape of a typical
/ day - the counts then total across the dates, the percentages do not.
.lp.profile:{[s;dt;bkt]
  b:.lp.bkt bkt;
  s:.lp.sym s;
  / price>0 and size>0 is the whole test for "this row is a print": every qatt
  / row is a transaction carrying the quote that stood at the time, so there
  / are no quote-only rows to exclude.  See market_stats.q note 8.
  t:select time,price,size from qatt where date in dt, sym=s, price>0, size>0;
  if[0=count t; :.lp.empty];
  / "j"$size before summing - size is an int, and a day of a heavily traded
  / small cap goes past the 2.1bn an int tops out at
  r:0!select trades:count i, shares:sum "j"$size, turnover:sum price*"f"$size
    by bkt:b xbar time from t;
  / every bucket from the first print to the last, so the lunch break and a
  / dead hour read as empty bars rather than as rows that are not there.
  / Built in longs - milliseconds - for the same reason .lp.bkt casts: time
  / arithmetic against a bucket is only safe once both are the same unit.
  lo:"j"$first r`bkt;
  hi:"j"$last r`bkt;
  ms:"j"$b;
  r:([] bkt:"t"$lo+ms*til 1+(hi-lo) div ms) lj `bkt xkey r;
  r:update trades:0^trades, shares:0^shares, turnover:0f^turnover from r;
  tot:sum r`shares;
  update pct:100*shares%tot, cum_pct:100*(sums shares)%tot from r
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
