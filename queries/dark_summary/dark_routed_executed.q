/ dark_routed_executed.q - our DARK activity split into what we ROUTED and what
/ actually EXECUTED, by venue.  Runs on the order server, no handle.
/
/   q)\l queries/dark_summary/dark_routed_executed.q
/   q)darkRoutedExecuted .z.D
/
/ pct_routed and pct_executed each add to 100 across venues, so either drops
/ straight into a pie.  The gap between them is the point - a venue taking 30%
/ of the flow and returning 8% of the fills is a different venue from one doing
/ 8% and 8% - and fill_rate names it.
/
/ notional_executed here reconciles with notional_usd in dark_summary.q.

darkRoutedExecuted:{[dt]
  dk:("*DARK*";"*DRK*");
  / NO make>0 filter: the children that never filled are exactly what makes
  / routed differ from executed
  w:select date,id_server,id_target,sym,venue,size,price,make,avg_fill_price,
      transmit_lastprice
    from workorder
    where date=dt, any (upper venue) like/: dk;
  if[0=count w; :w];
  / fxlast lives in target_stock, one row per parent order
  ids:exec distinct id_target from w;
  x:`date`id_server`id_target xkey select date,id_server,id_target,
      fxlast,currency
    from target_stock where date=dt, id_target in ids;
  r:w lj x;
  / an order that never filled has no fill price, so ROUTED is valued at the
  / price the child was sent with, falling back to the last trade at transmit
  / time for market and pegged orders.  workorder also carries limit_target,
  / limit_candidate, transmit_bidprice and transmit_askprice if you would
  / rather value it another way - this is the only line that decides.
  r:update px_routed:transmit_lastprice^?[price>0;price;0n] from r;
  / fxlast is local -> USD
  r:update
      notional_routed:size*px_routed*fxlast,
      notional_executed:make*avg_fill_price*fxlast
    from r;
  s:0!select
      orders_routed:count i,
      orders_filled:sum make>0,
      shares_routed:sum size,
      shares_executed:sum make,
      notional_routed:sum notional_routed,
      notional_executed:sum notional_executed
    by venue from r;
  / fill_rate is MONEY weighted; orders_filled%orders_routed is the order
  / weighted version, and the two diverge when one venue gets the big orders.
  / Null rather than infinite where a venue routed nothing valuable.
  s:update
      pct_routed:100*notional_routed%sum notional_routed,
      pct_executed:100*notional_executed%sum notional_executed,
      fill_rate:?[0<notional_routed;100*notional_executed%notional_routed;0n]
    from s;
  / notionals stay at full precision; only the percentages are rounded.
  / Shares of the DARK book only - nothing here looks at a lit venue.
  `notional_routed xdesc update
      pct_routed:0.01*"j"$100*pct_routed,
      pct_executed:0.01*"j"$100*pct_executed,
      fill_rate:0.01*"j"$100*fill_rate
    from s
 };
