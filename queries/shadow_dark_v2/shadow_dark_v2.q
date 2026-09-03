/ shadow_dark_v2.q - SHADOW dark trading over a PERIOD, not per order: how much
/ of what we routed went dark, how much of that filled, and the market volume
/ in those names while we were in there.  See README.md.
/ Runs on the order server, no handle.
/
/   q)\l queries/shadow_dark_v2/shadow_dark_v2.q
/   q)shadowDarkPeriod[2026.08.01;2026.08.31]

/ ---- CONFIG ---------------------------------------------------------------

/ Which stocks.  "*.JP" = Japan, "*.HK" = Hong Kong, "*" = everything.
.shd.symLike:"*.JP";

/ Rows in the result.  () = one row for the whole period, `sym = per stock,
/ `date = per day, `date`sym = both.
.shd.groupBy:`symbol$();

.shd.darkCategories:`Dark`Pmid;   / VENUEMAP category.  `Dark alone drops midpoint

/ 1b drops cancels that were the parent moving rather than a venue decision -
/ applied to lit as well, so the split stays like-for-like.  0b (default) counts
/ every child that reached a venue, which is the honest share-of-effort split.
.shd.keepFilter:0b;
.shd.keepReasons:`rotate_venue`chase_price`chase_liquidity`adverse_mkt`over_duration;

/ ---------------------------------------------------------------------------

/ Most shares resting in the dark at any one instant - see
/ ../shadow_dark/shares_in_dark.md.
.shd.peak:{[on;off;sz]
  (max sz) | max sums exec d from `t`d xasc ([]t:on,off; d:sz,neg sz)};

.shd.pct:{[a;b] ?[b>0; 0.01*"j"$1e4*a%b; 0n]};

/ sum every non-key column, whatever the chosen grain
.shd.roll:{[t;g] c:cols[t] except g;
  0!?[t; (); $[count g; g!g; 0b]; c!{(sum;x)}each c]};

shadowDarkPeriod:{[d0;d1]
  if[not `VENUEMAP in key `.; '"VENUEMAP not here - not the order server"];
  g:(),.shd.groupBy;
  tg:`date`id_server`id_target xkey select date,id_server,id_target,algo,
      target_size:"j"$size
    from target where date within (d0;d1), (upper algo)=`SHADOW;
  / "j"$ is not cosmetic - size and make are 32-bit ints and sum wraps at 2^31,
  / which over a month turns routed_dark negative
  / t_on_market>0 drops the children that never reached a venue, lit or dark
  w:select date,time,id_server,id_target,sym,venue,state,
      size:"j"$size, make:"j"$make, onmkt_adv1t:"j"$onmkt_adv1t,
      t_on_market,t_off_market
    from workorder
    where date within (d0;d1), t_on_market>0, sym like .shd.symLike;
  w:w lj `venue xkey select venue,category from VENUEMAP;
  / ij, not lj - a child whose parent is not SHADOW drops out
  w:`date`time xasc (w ij tg);
  w:update dark:category in .shd.darkCategories from w;
  if[.shd.keepFilter;
    w:update killreason:{`$last ":" vs string x} each lower state from w;
    w:select from w where (make>0) or killreason in .shd.keepReasons];
  / one row per parent: the lit/dark split of its own children
  p:0!select
      target_qty:first target_size,
      children:count i,
      routed_dark:sum size*dark,
      routed_lit:sum size*not dark,
      exec_dark:sum make*dark,
      exec_lit:sum make*not dark
    by date,id_server,id_target,sym from w;
  / peak and adv1t come from the dark children alone, and need a duration
  d:select from w where dark, t_off_market>0;
  pd:`date`id_server`id_target`sym xkey 0!select
      shares_in_dark:.shd.peak[t_on_market;t_off_market;size],
      adv1t:last onmkt_adv1t
    by date,id_server,id_target,sym from d;
  p:update shares_in_dark:0^shares_in_dark, adv1t:0^adv1t from p lj pd;
  / max, not sum: two parents in one name on one day read the same tape
  b:0!select
      targets:count i, target_qty:sum target_qty, children:sum children,
      routed_dark:sum routed_dark, routed_lit:sum routed_lit,
      exec_dark:sum exec_dark, exec_lit:sum exec_lit,
      shares_in_dark:sum shares_in_dark,
      mkt_volume:max adv1t
    by date,sym from p;
  if[count k:`date`sym except g; b:![b;();0b;k]];   / drop the grain not kept
  s:.shd.roll[b;g];
  s:update
      dark_pct_routed:.shd.pct[routed_dark;routed_dark+routed_lit],
      dark_pct_traded:.shd.pct[exec_dark;exec_dark+exec_lit],
      dark_pct_exec:.shd.pct[exec_dark;routed_dark],
      dark_pct_vol:.shd.pct[exec_dark;mkt_volume]
    from s;
  s:(g,`targets`target_qty`children`routed_dark`routed_lit`dark_pct_routed,
     `exec_dark`exec_lit`dark_pct_traded`dark_pct_exec,
     `shares_in_dark`mkt_volume`dark_pct_vol) xcols s;
  $[`date in g; `date xasc s; count g; `routed_dark xdesc s; s]
 };

/ routed_dark and routed_lit both count the same shares once per send, so
/ dark_pct_routed is a share of EFFORT, not of the order.  shares_in_dark is
/ the un-inflated figure.  Explained in README.md.
