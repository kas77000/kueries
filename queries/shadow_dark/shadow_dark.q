/ shadow_dark.q - per SHADOW parent order: shares routed into the dark, shares
/ executed, and the volume traded while we were in there.  See README.md.
/ Runs on the order server, no handle.
/
/   q)\l queries/shadow_dark/shadow_dark.q
/   q)shadowDark[.z.D-5;.z.D-1]      / or h(shadowDark;d0;d1) from elsewhere

/ ---- CONFIG ---------------------------------------------------------------

.shd.darkCategories:`Dark`Pmid;   / VENUEMAP category.  `Dark alone drops midpoint

/ Cancels that still count as a real dark attempt.  Everything else dropped -
/ goal_change, need_shares, target_modify, stop_to_finish, cancel_for_eod,
/ scheduler_halt, churn_prevent - is the parent moving, not the venue.
.shd.keepReasons:`rotate_venue`chase_price`chase_liquidity`adverse_mkt`over_duration;

/ ---------------------------------------------------------------------------

shadowDark:{[d0;d1]
  if[not `VENUEMAP in key `.; '"VENUEMAP not here - not the order server"];
  tg:`date`id_server`id_target xkey select date,id_server,id_target,algo
    from target where date within (d0;d1), (upper algo)=`SHADOW;
  / t_on_market>0 drops the children that never reached a venue
  w:select date,time,id_server,id_target,sym,venue,state,size,make,
      onmkt_adv1t,t_off_market
    from workorder
    where date within (d0;d1), t_on_market>0;
  w:w lj `venue xkey select venue,category from VENUEMAP;
  w:select from w where category in .shd.darkCategories;
  / "cxl:rotate_venue" -> `rotate_venue
  w:update killreason:{`$last ":" vs string x} each lower state from w;
  w:select from w
    where (make>0) or (t_off_market>0) and killreason in .shd.keepReasons;
  / ij, not lj - a child whose parent is not SHADOW drops out
  r:`date`time xasc (w ij tg);
  / sorted by time above, so last IS the latest child.  id_server stays in the
  / by clause so two servers cannot share an id_target, then drops out.
  s:0!select
      children:count i,
      shares_routed:sum size,
      shares_executed:sum make,
      adv1t_last:last onmkt_adv1t
    by date,id_server,id_target,sym from r;
  `date xasc `shares_routed xdesc delete id_server from s
 };

/ shares_routed counts the same shares once per send - a parent that rotated
/ forty times routed forty times its size.  count_chaseprice is NOT the chase
/ counter, it holds minExecSize.  Both explained in README.md.
