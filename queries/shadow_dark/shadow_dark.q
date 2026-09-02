/ shadow_dark.q - per SHADOW parent order: shares routed into the dark, shares
/ executed, and the volume that was there to trade against.  See README.md.
/
/   q)\l queries/shadow_dark/shadow_dark.q
/   q).shd.hs:hopen `:statsserver:port
/   q)shadowDark[.z.D-5;.z.D-1]

/ ---- CONFIG ---------------------------------------------------------------

.shd.hw:0;    / workorder, target, VENUEMAP.  0 = the server you are on
.shd.hs:0;    / work_list.  Not the order server - set this to a handle

.shd.darkCategories:`Dark`Pmid;   / VENUEMAP category.  `Dark alone drops midpoint

/ Cancels that still count as a real dark attempt.  Everything else dropped -
/ goal_change, need_shares, target_modify, stop_to_finish, cancel_for_eod,
/ scheduler_halt, churn_prevent - is the parent moving, not the venue.
.shd.keepReasons:`rotate_venue`chase_price`chase_liquidity`adverse_mkt`over_duration;

/ ---------------------------------------------------------------------------

.shd.run:{[h;f;a] $[0=h; f . a; h ((enlist f),a)]};

shadowDark:{[d0;d1]
  w:.shd.run[.shd.hw;{[d0;d1;cats;keep]
    if[not `VENUEMAP in key `.;
      '"VENUEMAP not here - set .shd.hw:hopen `:orderserver:port"];
    tg:`date`id_server`id_target xkey select date,id_server,id_target,algo
      from target where date within (d0;d1), (upper algo)=`SHADOW;
    / t_on_market>0 drops the children that never reached a venue
    w:select date,time,id_server,id_target,id_work,sym,venue,state,size,make,
        onmkt_adv1t,t_off_market
      from workorder
      where date within (d0;d1), t_on_market>0;
    w:w lj `venue xkey select venue,category from VENUEMAP;
    w:select from w where category in cats;
    / "cxl:rotate_venue" -> `rotate_venue
    w:update killreason:{`$last ":" vs string x} each lower state from w;
    w:select from w
      where (make>0) or (t_off_market>0) and killreason in keep;
    / ij, not lj - a child whose parent is not SHADOW drops out
    `date`time xasc (w ij tg)
    };(d0;d1;.shd.darkCategories;.shd.keepReasons)];
  if[0=count w; :w];
  v:.shd.run[.shd.hs;{[d0;d1]
    if[not `work_list in tables `.;
      '"work_list not here - set .shd.hs:hopen `:statsserver:port"];
    select date,id_server,id_target,id_work,vvalid,vvalida
      from work_list where date within (d0;d1), algo=`SHADOW
    };(d0;d1)];
  r:w lj `date`id_server`id_target`id_work xkey v;
  / sorted by time above, so last IS the latest child.  id_server stays in the
  / by clause so two servers cannot share an id_target, then drops out.
  s:0!select
      children:count i,
      shares_routed:sum size,
      shares_executed:sum make,
      adv1t_last:last onmkt_adv1t,
      vvalid:sum vvalid,
      vvalida:sum vvalida
    by date,id_server,id_target,sym from r;
  `date xasc `shares_routed xdesc delete id_server from s
 };

/ shares_routed counts the same shares once per send - a parent that rotated
/ forty times routed forty times its size.  count_chaseprice is NOT the chase
/ counter, it holds minExecSize.  Both explained in README.md.
