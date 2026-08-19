/ market_stats.q - the six market statistics behind the Market Statistics page:
/ Price, Volatility, Spread, Volume, Trade Size, Quote Size.
/
/ Two views of the same six metrics, so one renderer draws both:
/   .ms.intraday[h;dts;`HK;`shares]    one row per 10 minute bucket per date
/   .ms.daily   [h;dts;`HK;`notional]  one row per date
/
/   q)\l queries/market_stats/market_stats.q
/   q)h:hopen`:orderserver:5010            / only needed for `notional
/   q).ms.intraday[h;.z.D-1;`HK;`shares]
/   q).ms.daily[h;.z.D-4+til 5;`JP;`notional]
/
/ Runs where qatt is.  h is a handle to the ORDER server and is used for one
/ thing only - fxlast, for the notional unit.  Pass 0i for `shares.
/
/ Market wide: the universe is every sym on the feed carrying the country's
/ suffix, which really means every name we subscribe to.  It is not an index.

/ =============================================================================
/ CONFIG.  .ms.sess is exchange hours - knowable, but it goes out of date, so
/ run .ms.probeSession.  Nothing else here is a vendor vocabulary.
/ =============================================================================

/ EVERY qatt ROW IS A TRANSACTION.  It lists all the prints and carries the
/ prevailing quote alongside each one; typ holds "U" before the open and is
/ empty for the session, so it is not used at all.  price>0 and size>0 is the
/ whole test, and it needs no vendor vocabulary.
/ intraday bucket
.ms.bkt:00:10;
/ AUCTION vs CONTINUOUS is decided by the clock, not by a sale condition code:
/ (last moment of the opening auction; first moment of the closing auction),
/ both bounds INCLUSIVE.
/
/ EVERYTHING BELOW IS IN HKT (UTC+8), because that is what qatt`time is: the
/ SCB-R.TB sample ran 8 hours ahead of quoteTime, which is UTC.  Written in HKT
/ the windows compare against time directly, with no conversion.  The comment on
/ each line gives the exchange's own local time so the two can be checked.
/
/ .ms.tz shifts every window at once, for the day the plant moves zone or you
/ want to read them in something other than HKT.  One number, not a table.
.ms.tz:00:00;
.ms.sess:`AU`JP`HK`IN!(
  (08:10;14:10);   / ASX   10:10 / 16:10 Sydney AEST   - SEE NOTE 10, DST
  (08:00;14:25);   / TSE   09:00 / 15:25 Tokyo
  (09:29;16:00);   / HKEX  09:29 / 16:00 Hong Kong     - HKT is local here
  (11:45;23:59));  / NSE   09:15 Mumbai; 23:59 = never, no closing auction
/ AUSTRALIA AND DAYLIGHT SAVING.  Sydney runs AEDT from the first Sunday in
/ October to the first Sunday in April; Hong Kong never does, so the ASX session
/ moves an hour against qatt`time twice a year.  Nowhere else in APAC observes
/ it, so AU is the only row that needs a date.
/ kdb dates count from 2000.01.01, a SATURDAY, so date mod 7 is 0 on a Saturday
/ and 1 on a Sunday.
.ms.firstSun:{x+(1-x mod 7)mod 7};
.ms.aedt:{[d]
  y:string `year$d;
  (d>=.ms.firstSun "D"$y,".10.01")|d<.ms.firstSun "D"$y,".04.01"
 };
/ the (open;close) bounds for a country ON A GIVEN DATE, in HKT
.ms.sessOn:{[ctry;d]
  s:.ms.tz+.ms.sess ctry;
  $[(ctry=`AU)&.ms.aedt d; s-01:00; s]
 };

/ what a range with no prints in it comes back as - typed, so the panels read as
/ zero rather than as broken
.ms.empty:([] date:0#0Nd; bkt:0#0Nt; country:0#`; unit:0#`;
  price_bps:0#0n; volatility_bps:0#0n; spread_bps:0#0n;
  volume:0#0n; volume_cont:0#0n; volume_auct:0#0n;
  trade_size:0#0n; quote_size:0#0n; n_syms:0#0j; n_trades:0#0j);

/ country -> sym suffix.  Add the rest of APAC here; nothing else needs editing.
.ms.mkt:`AU`JP`HK`IN!("*.AU";"*.JP";"*.HK";"*.IN");

/ Which clock is qatt`time on?  In the sample it ran 8 hours ahead of
/ quoteTime, so it is the PLANT's zone, not the exchange's - a Thai print at
/ 09:59 Bangkok is stamped 10:59 here.  .ms.sess must be written in this clock.
.ms.probeClock:{[dts;ctry]
  sfx:.ms.mkt ctry;
  select n:count i, first_time:min time, last_time:max time,
      trade_lag:avg time-tradeTime, quote_offset:avg time-quoteTime
    from qatt where date in dts, sym like sfx, price>0, size>0
 };

/ Where your prints actually cluster, in 5 minute buckets.  The opening and
/ closing auctions show up as volume spikes with continuous trading between
/ them, so .ms.sess can be set from evidence rather than from a hours table that
/ may be a year out of date.
.ms.probeSession:{[dts;ctry]
  sfx:.ms.mkt ctry;
  0!select n:count i, shares:sum size, names:count distinct sym
    by 00:05 xbar time from qatt
    where date in dts, sym like sfx, price>0, size>0
 };

/ =============================================================================
/ FX.  Only touched for the notional unit.  fxlast is local -> USD and lives
/ per parent order, so it is taken per (date,sym) and anything the book did not
/ trade falls back to that country's median for the date.
/ =============================================================================

.ms.fxOn:{[dts;sfx]
  x:select fxlast:last fxlast by date,sym from target_stock
    where date in dts, sym like sfx, fxlast>0;
  0!x
 };

/ fx as a TABLE, so the core never needs a handle: the script and the dashboard
/ each fetch it their own way and pass it in.  0i => fetch it here.
.ms.fx:{[h;dts;sfx] $[0<h; h(.ms.fxOn;dts;sfx); .ms.fxOn[dts;sfx]]};

/ =============================================================================
/ The worker.  intraday=1b buckets the day; intraday=0b collapses bkt to a
/ constant so the SAME group-by yields one row per date - the daily figures are
/ computed over the whole day, not averaged from the buckets.
/ =============================================================================

.ms.stats:{[fx;dts;ctry;unit;intraday]
  sfx:.ms.mkt ctry;
  if[null sfx; '"unknown market: ",string ctry];
  if[not unit in `shares`notional; '"unit must be `shares or `notional"];
  / shares -> plain mean across names; notional -> volume weighted
  wgt:unit=`notional;
  / ONE scan.  Every qatt row is a transaction that carries the prevailing
  / quote alongside it - there are no quote-only rows - so all six metrics come
  / off the same rows and the quote metrics are measured AT EXECUTION.
  t:select date,time,sym,price,size,qbid,qask,qbsize,qasize from qatt
    where date in dts, sym like sfx, price>0, size>0;
  if[0=count t; :$[intraday; .ms.empty; delete bkt from .ms.empty]];
  t:update bkt:$[intraday;.ms.bkt xbar time;00:00] from t;
  / shares needs no rate at all; notional falls back to the range median, and to
  / 1f only when the book traded nothing in this market - see note 5.
  fx:$[unit=`shares; ([] date:0#0Nd; sym:0#`; fxlast:0#0n); 0!fx];
  fxd:$[count fx; med exec fxlast from fx; 1f];
  t:update fxlast:fxd^fxlast from t lj `date`sym xkey fx;
  / shares: one share is one unit.  notional: shares * price * fx.
  t:update qty:$[unit=`shares; "f"$size; size*price*fxlast] from t;
  / the quote at that print, valued each side on its own
  t:update qqty:$[unit=`shares;
      0.5*"f"$qbsize+qasize;
      0.5*fxlast*(qbsize*qbid)+qasize*qask] from t;
  t:update spr:10000*(qask-qbid)%0.5*qask+qbid from t;
  / auction by the clock, with the bounds resolved PER DATE so a range spanning
  / a daylight saving switch is right on both sides of it
  dd:asc exec distinct date from t;
  sb:.ms.sessOn[ctry] each dd;
  t:t lj `date xkey ([] date:dd; sopen:sb[;0]; sclose:sb[;1]);
  t:update auction:(time<=sopen)|time>=sclose from t;
  / --- per name, per bucket.  Ordered first: prev crosses trades, not buckets.
  t:`date`sym`time xasc t;
  t:update ret:10000*(price%prev price)-1 by date,sym from t;
  ps:0!select
      px:10000*((last price)%first price)-1,
      vol:dev ret,
      spread:avg spr,
      tsz:avg qty,
      qsz:avg qqty,
      w:sum qty,
      ntrd:count i,
      vcont:sum qty*not auction,
      vauct:sum qty*auction
    by date,bkt,sym from t;
  / --- across the market.  Volume sums; everything else is a mean over names,
  / weighted by that name's own volume when the unit is notional.
  r:0!select
      price_bps:$[wgt; w wavg px; avg px],
      volatility_bps:$[wgt; w wavg vol; avg vol],
      spread_bps:$[wgt; w wavg spread; avg spread],
      trade_size:$[wgt; w wavg tsz; avg tsz],
      quote_size:$[wgt; w wavg qsz; avg qsz],
      volume_cont:sum vcont,
      volume_auct:sum vauct,
      n_syms:count distinct sym,
      n_trades:sum ntrd
    by date,bkt from ps;
  r:update volume:volume_cont+volume_auct from r;
  r:update unit:unit, country:ctry from r;
  `date`bkt xasc select date, bkt, country, unit,
      price_bps, volatility_bps, spread_bps,
      volume, volume_cont, volume_auct, trade_size, quote_size,
      n_syms, n_trades
    from r
 };

/ Core, for a caller that already holds the fx table (the chart script, the
/ dashboard).  Pass an empty table for `shares.
.ms.intradayWith:{[fx;dts;ctry;unit] .ms.stats[fx;dts;ctry;unit;1b]};
.ms.dailyWith:{[fx;dts;ctry;unit] delete bkt from .ms.stats[fx;dts;ctry;unit;0b]};

/ Interactive, for a q session with a handle to the order server.  0i if the
/ order tables are local, or if unit is `shares and no rate is needed.
.ms.intraday:{[h;dts;ctry;unit]
  .ms.intradayWith[.ms.fx[h;dts;.ms.mkt ctry];dts;ctry;unit]};
.ms.daily:{[h;dts;ctry;unit]
  .ms.dailyWith[.ms.fx[h;dts;.ms.mkt ctry];dts;ctry;unit]};

/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. PRICE is per bucket, not cumulative: 10000*(last%first)-1 within the
/    bucket, per name, then averaged across names.  Each bar stands alone.
/
/ 2. VOLATILITY is dev of trade-to-trade returns inside the bucket, in bps, per
/    name and then averaged.  No annualisation and no sqrt(time) scaling, so it
/    is comparable across buckets of equal length but not against a daily vol.
/    ret is computed by date,sym BEFORE bucketing, so the first trade of a
/    bucket returns against the last trade of the previous one rather than
/    against itself.
/
/ 3. SPREAD AND QUOTE SIZE ARE MEASURED AT EXECUTION.  qatt has no quote-only
/    rows - every row is a transaction carrying the prevailing quote - so these
/    are the spread and displayed size AT THE MOMENT SOMETHING TRADED, averaged
/    over prints.  That is not the time weighted quoted spread: a minute with
/    two hundred prints counts two hundred times, an idle minute not at all.
/    For a desk it is arguably the more useful of the two - it is the spread
/    that was actually there to be crossed - but it is not what a market data
/    vendor means by "average spread", so do not compare it to one.
/
/ 3a. ACROSS NAMES the mean follows the unit.  `shares gives every name the same
/    weight; `notional weights each name by its own volume, so the market's
/    price, volatility, spread and sizes read as what the money actually paid
/    rather than as what the average listing did.  Each metric is weighted by
/    the rows it came from - the trade metrics by traded volume, the quote
/    metrics by quoted volume - so a name that quotes heavily and trades rarely
/    does not get a large say in the spread it never had to cross.
/    Volume itself always sums; weighting a total would be meaningless.
/
/ 4. THE UNIT changes what volume, trade_size and quote_size MEAN, and nothing
/    else - price, volatility and spread are always bps.  Under `notional a
/    quote is valued at its own side, (qbsize*qbid + qasize*qask)%2.
/
/ 5. FX is per (date,sym) from target_stock, so it only covers names the book
/    has traded; everything else takes the median across the whole range.
/    A market where we traded nothing has no rate at all and falls back to 1,
/    which would be silently wrong - check n_syms against the fx coverage
/    before trusting a notional figure for a market we are not active in.
/
/ 6. DAILY IS NOT AN AVERAGE OF THE BUCKETS.  intraday=0b collapses bkt to a
/    constant, so every figure is computed over the whole day in one pass.
/    Averaging bucket means would weight a thin 09:30 bucket the same as a busy
/    one.  Volume still sums, which is why the daily tab reads in millions.
/
/ 7. THE UNIVERSE is every sym on the feed with the country's suffix - so it is
/    every name we subscribe to, not the exchange's full list and not an index.
/    Say so on any chart that leaves the desk.
/
/ 8. ONE SCAN, because qatt has no quote-only rows.  A sample of SCB-R.TB shows
/    trdCount and totalVolume advancing on every row while qbid/qask/qbsize/
/    qasize repeat - one print per row, each carrying the quote that stood at
/    the time.  So the trade and quote metrics share a pass, and there is no
/    quote stream to time weight against.
/
/    Note trdSeq was CONSTANT across that burst while trdCount incremented, so
/    trdCount is the per-print counter here - worth knowing if you ever need to
/    deduplicate.
/
/ 9. AUCTION IS DECIDED BY THE CLOCK, not by a sale condition code - cond is a
/    vendor vocabulary that differs by market and by feed, where session hours
/    are a published fact.  The cost is that a print stamped inside the window
/    is called an auction whether it was one or not: a late report of a
/    continuous trade, or an off-book cross printed after the close, lands in
/    the auction bucket.  Run .ms.probeSession and set the bounds from where the
/    spikes actually are, not from a published hours table.
/
/ 10. AUSTRALIA IS RESOLVED PER DATE.  .ms.sess holds the AEST bounds; on a date
/    inside AEDT (first Sunday in October to first Sunday in April) .ms.sessOn
/    takes an hour off, because Sydney moves and Hong Kong does not.  The bounds
/    are joined onto the rows by date, so a range that crosses the switch is
/    right on both sides of it rather than an hour out for half of it.
/
/    Two things this does NOT handle: a market other than AU adopting DST, and
/    the switch weekend itself, where the change lands at 02:00 Sydney on a
/    Sunday - no equity session, so the day boundary is enough.
/
/ 11. INDIA HAS NO CLOSING AUCTION HERE, by choice: its close bound is 23:59, a
/    time no print reaches, so nothing after the open is ever classified as an
/    auction.  NSE closes on a VWAP of the last half hour rather than a single
/    price auction, so there is no clean spike to bound anyway.
/ -----------------------------------------------------------------------------
