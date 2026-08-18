/ =============================================================================
/ dark_routed_executed.q
/
/ One function.  Splits our DARK activity into what we ROUTED and what we
/ ACTUALLY EXECUTED, by venue, so the two can be compared side by side.
/
/ Feeds two pie charts: pct_routed and pct_executed each add to 100 across
/ venues, so either column drops straight into a pie.  The interesting story is
/ usually the gap between them - a venue taking 30% of the flow and returning
/ 8% of the fills is a different venue from one doing 8% and 8%.  fill_rate
/ names that gap directly.
/
/ Runs entirely on the ORDER SERVER - reads workorder and target_stock only.
/
/   q)\l queries/dark_summary/dark_routed_executed.q
/   q)darkRoutedExecuted .z.D        / today
/   q)darkRoutedExecuted .z.D-1      / yesterday
/
/ Columns
/   venue              workorder`venue
/   orders_routed      child orders sent to that venue
/   orders_filled      how many of them got any fill at all
/   shares_routed      sum of workorder`size
/   shares_executed    sum of workorder`make
/   notional_routed    sum of size * px_routed * fxlast     (see note 2)
/   notional_executed  sum of make * avg_fill_price * fxlast
/   pct_routed         venue's share of total routed notional, 2dp
/   pct_executed       venue's share of total executed notional, 2dp
/   fill_rate          notional_executed / notional_routed, 2dp
/
/ A venue is DARK when its name contains DARK or DRK.  Every child order
/ executed in the dark carries that in the venue name, so the match is the
/ classification.
/ =============================================================================

darkRoutedExecuted:{[dt]
  / venue name patterns that mean dark.  Matched case insensitively.
  dk:("*DARK*";"*DRK*");
  / every dark child order for the day - NOT filtered on make>0, because the
  / ones that never filled are exactly what makes routed differ from executed
  w:select date,id_server,id_target,sym,venue,size,price,make,avg_fill_price,
      transmit_lastprice
    from workorder
    where date=dt, any (upper venue) like/: dk;
  if[0=count w; :w];
  / fxlast lives in target_stock, not workorder, so join it on per parent
  / order.  One row per target, so no aggregation needed.
  ids:exec distinct id_target from w;
  x:`date`id_server`id_target xkey select date,id_server,id_target,
      fxlast,currency
    from target_stock where date=dt, id_target in ids;
  r:w lj x;
  / price used to value the ROUTED quantity: the price the child was sent
  / with, falling back to the last trade at transmit time where there was no
  / usable limit (market and pegged orders).  fxlast is local -> USD.
  r:update px_routed:transmit_lastprice^?[price>0;price;0n] from r;
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
  / percentages off the unrounded notionals, then rounded for display
  s:update
      pct_routed:100*notional_routed%sum notional_routed,
      pct_executed:100*notional_executed%sum notional_executed,
      fill_rate:?[0<notional_routed;100*notional_executed%notional_routed;0n]
    from s;
  `notional_routed xdesc update
      pct_routed:0.01*"j"$100*pct_routed,
      pct_executed:0.01*"j"$100*pct_executed,
      fill_rate:0.01*"j"$100*fill_rate
    from s
 };

/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. ROUTED INCLUDES EVERYTHING SENT, filled or not, cancelled or not.  That is
/    the point of the split: dark_summary.q keeps only make>0 because it is an
/    execution report, this one deliberately does not.  Run both and
/    notional_executed here should match notional_usd there.
/
/ 2. HOW ROUTED NOTIONAL IS PRICED is a judgement call, since an order that
/    never filled has no fill price.  This uses the price the child was sent
/    with, falling back to transmit_lastprice - the last trade at the moment of
/    transmission - wherever price is null or zero, which is what a market or
/    pegged order looks like.  workorder also carries limit_target,
/    limit_candidate, transmit_bidprice and transmit_askprice if you would
/    rather value it another way; it is the one line defining px_routed.
/
/ 3. fill_rate is notional executed over notional routed, so it is money
/    weighted rather than order weighted.  orders_filled / orders_routed gives
/    the order weighted version if you want both, and the two can differ a lot
/    when one venue gets the big orders.
/
/ 4. THE TWO PIES HAVE DIFFERENT DENOMINATORS.  pct_routed is a share of dark
/    routed notional, pct_executed a share of dark executed notional, and each
/    sums to 100 on its own.  They are not comparable as levels, only as
/    shapes - which is the comparison worth making.
/
/ 5. fill_rate is null rather than infinite for a venue with zero routed
/    notional, which happens if every order there had no usable price.
/
/ 6. Only DARK venues are here, so every percentage is a share of the dark
/    book, never of the day's total trading.
/ -----------------------------------------------------------------------------
