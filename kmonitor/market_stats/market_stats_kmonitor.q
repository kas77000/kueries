/ market_stats_kmonitor.q - the six Market Statistics panels as a dashboard:
/ Price, Volatility, Spread, Volume, Trade Size, Quote Size.
/
/ Source of truth: build_dashboard.py turns the blocks below into the importable
/ JSON.  Edit here, re-run the builder, re-import.  Details in README.md.
/
/ Same arithmetic as queries/market_stats/market_stats.q - restated here because
/ a dashboard dataset is one self-contained query sent to one server, so it
/ cannot call .ms.*.  If you change a metric, change it in BOTH.
/
/ Three reader controls, all of which go back to the server:
/   country  AU | JP | HK | IN     unit  shares | notional     view  intraday | daily
/
/ ONE dataset serves both views: `view` decides whether the bucket column is the
/ 10 minute bar or a constant, and `label` is the x axis either way - so the six
/ charts are drawn once and the dropdown reshapes them.
/
/ "OMS" and "QUOTES" are placeholders - change them in env= per block.


/ ==== DATASET: fx | env=OMS ====
/ fxlast per name, for the notional unit.  Empty and unused for shares - the
/ query still runs, it just returns nothing to join.
{[ctry]
  sfx:"*.",string ctry;
  select fxlast:last fxlast by date,sym from target_stock
    where {{#historical}}date within ({{date_from}};{{date_to}}){{/historical}}{{#realtime}}date=.z.D{{/realtime}},
      sym like sfx, fxlast>0
 }[{{param:country}}]
/ ==== END ====


/ ==== DATASET: stats | env=QUOTES ====
/ One row per bucket (intraday) or per date (daily), market wide.
{[ctry;unit;view;fx]
  / EVERY qatt ROW IS A TRANSACTION carrying the prevailing quote alongside it -
  / there are no quote-only rows - so one scan yields all six metrics and the
  / quote metrics are measured AT EXECUTION.  typ is unused: it holds "U" before
  / the open and is empty for the session.
  sfx:"*.",string ctry;
  intraday:view=`intraday;
  / shares -> plain mean across names; notional -> volume weighted
  wgt:unit=`notional;
  / auction vs continuous by the CLOCK.  ALL IN HKT, which is what qatt`time is;
  / the comment gives each exchange's own local time.  Keep in step with .ms.sess
  / in queries/market_stats/market_stats.q - the same table written twice.
  /   AU 10:10/16:10 Sydney AEST    JP 09:00/15:25 Tokyo
  /   HK 09:29/16:00 local          IN 09:15 Mumbai, 23:59 = no closing auction
  base:(`AU`JP`HK`IN!((08:10;14:10);(08:00;14:25);(09:29;16:00);(11:45;23:59)))ctry;
  / Sydney runs AEDT from the first Sunday in October to the first Sunday in
  / April and Hong Kong never does, so AU shifts an hour twice a year.  kdb
  / dates count from 2000.01.01, a Saturday, so date mod 7 is 1 on a Sunday.
  fsun:{x+(1-x mod 7)mod 7};
  sessOn:{[base;ctry;fsun;d]
    y:string `year$d;
    $[(ctry=`AU)&(d>=fsun "D"$y,".10.01")|d<fsun "D"$y,".04.01"; base-01:00; base]
   }[base;ctry;fsun];
  t:select date,time,sym,price,size,qbid,qask,qbsize,qasize from qatt
    where {{#historical}}date within ({{date_from}};{{date_to}}), {{/historical}}
      sym like sfx, price>0, size>0;
  {{#realtime}}t:update date:.z.D from t;{{/realtime}}
  t:update bkt:$[intraday;00:10 xbar time;00:00] from t;
  fx:$[unit=`shares; ([] date:0#0Nd; sym:0#`; fxlast:0#0n); 0!fx];
  fxd:$[count fx; med exec fxlast from fx; 1f];
  t:update fxlast:fxd^fxlast from t lj `date`sym xkey fx;
  t:update qty:$[unit=`shares; "f"$size; size*price*fxlast] from t;
  t:update qqty:$[unit=`shares;0.5*"f"$qbsize+qasize;
      0.5*fxlast*(qbsize*qbid)+qasize*qask] from t;
  t:update spr:10000*(qask-qbid)%0.5*qask+qbid from t;
  / bounds resolved PER DATE, so a range crossing the switch is right either side
  dd:asc exec distinct date from t;
  sb:sessOn each dd;
  t:t lj `date xkey ([] date:dd; sopen:sb[;0]; sclose:sb[;1]);
  t:update auction:(time<=sopen)|time>=sclose from t;
  / ordered before prev, so a return is against the previous TRADE
  t:`date`sym`time xasc t;
  t:update ret:10000*(price%prev price)-1 by date,sym from t;
  ps:0!select px:10000*((last price)%first price)-1, vol:dev ret,
      spread:avg spr, tsz:avg qty, qsz:avg qqty, w:sum qty, ntrd:count i,
      vcont:sum qty*not auction, vauct:sum qty*auction
    by date,bkt,sym from t;
  / volume sums; the rest are means over names, volume weighted for notional
  r:0!select price_bps:$[wgt; w wavg px; avg px],
      volatility_bps:$[wgt; w wavg vol; avg vol],
      spread_bps:$[wgt; w wavg spread; avg spread],
      trade_size:$[wgt; w wavg tsz; avg tsz],
      quote_size:$[wgt; w wavg qsz; avg qsz],
      volume_cont:sum vcont, volume_auct:sum vauct,
      n_syms:count distinct sym, n_trades:sum ntrd
    by date,bkt from ps;
  r:update volume:volume_cont+volume_auct from r;
  r:`date`bkt xasc r;
  / one x axis column for both views, so the charts never change shape
  r:update label:`$$[intraday; 5#'string bkt; string date] from r;
  select label, date, price_bps, volatility_bps, spread_bps,
      volume, volume_cont, volume_auct, trade_size, quote_size,
      n_syms, n_trades
    from r
 }[{{param:country}};{{param:unit}};{{param:view}};{{table:fx}}]
/ ==== END ====


/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. THE THREE CONTROLS ALL RE-QUERY.  country and unit change what is read and
/    how it is valued; view changes the grain.  None of them can be a transform.
/
/ 2. VOLUME IS NOT STACKED HERE.  The bar widget takes one y, so the panel shows
/    the total and the table below carries volume_cont and volume_auct.  The
/    chart script in scripts/market_stats draws the stacked version.
/
/ 3. label IS THE X AXIS for both views - "09:30" intraday, "2026.07.28" daily -
/    so the same six charts serve the dropdown.  Rows are sorted by date,bkt
/    before it is built, and the widgets do not re-sort.
/
/ 4. ONE SCAN.  qatt lists every transaction and carries the prevailing quote on
/    each row, so spread and quote size are measured AT EXECUTION rather than
/    time weighted over a quote stream.  typ is unused.
/
/ 4a. SESSION WINDOWS ARE IN HKT and India has no closing auction bound (23:59
/    is never reached).  Australia is resolved PER DATE: base holds the AEST
/    bounds and sessOn takes an hour off inside AEDT, because Sydney moves and
/    Hong Kong does not.  AU is the only APAC market that observes it.
/
/ 4a. AUCTION IS BY THE CLOCK.  sess must match the exchange's hours and the
/    zone qatt`time is stamped in; run .ms.probeSession to see where the
/    volume spikes actually are.  Keep it in step with .ms.sess in the
/    queries file - they are the same table written twice.
/
/ 5. Everything in queries/market_stats/market_stats.q's notes applies: price is
/    per bucket rather than cumulative, volatility is dev of trade-to-trade
/    returns in bps, spread is a plain mean over quote rows, the universe is
/    every name on the feed with that suffix, and daily is computed over the
/    whole day rather than averaged from the buckets.
/ -----------------------------------------------------------------------------
