/ shadow_dark.q - one row per SHADOW parent order: the shares we routed into
/ the dark, the shares that came back, and the volume that was there to trade
/ against while we sat in it.
/
/   q)\l queries/shadow_dark/shadow_dark.q
/   q)hw:hopen `:orderserver:port      / workorder, target, VENUEMAP
/   q)hs:hopen `:statsserver:port      / work_list
/   q)shadowDark[hw;hs;.z.D-5;.z.D-1]
/
/ TWO handles, because work_list is not on the order server - the same split
/ cleandirty.q makes.  Both queries go out as lambdas, config included, so
/ neither server needs anything installed on it.
/
/ Meant for completed days: onmkt_adv1t on a day still trading is a partial
/ volume, so do not pass today as the end date.
/
/ One row is one id_target, so a rejected-and-replaced order appears once per
/ attempt.  README.md explains every column and every setting below.

/ ---------------------------------------------------------------------------
/ CONFIG - the whole judgement of this query is these two settings.  They are
/ passed to the server with the query, so editing them here is enough.
/ ---------------------------------------------------------------------------

/ Which venues count as dark.  This is VENUEMAP's own classification, not a
/ match on the venue's name: a name test misses *-PMID, which is dark, and
/ breaks when a broker is renamed.  Drop `Pmid to count only true dark pools.
.shd.darkCategories:`Dark`Pmid;

/ A child that never filled still counts as a real dark attempt when the algo
/ cancelled it for one of these reasons.  The order server writes its kill
/ reason into state as "state:killreason", which is where these names come from.
/   rotate_venue     moved off a cold venue on the rotation cycle - by design
/   chase_price      pulled and re-priced after the market moved
/   chase_liquidity  freed to replenish a venue that IS filling
/   adverse_mkt      pulled because the market went against us
/   over_duration    sat its allotted time and timed out
/ Every other cancel is dropped, because the PARENT moved and not the venue -
/ goal_change, need_shares, target_modify, stop_to_finish, cancel_for_eod,
/ scheduler_halt, churn_prevent.  Counting those blames a venue for a decision
/ taken above it.
.shd.keepReasons:`rotate_venue`chase_price`chase_liquidity`adverse_mkt`over_duration;

/ ---------------------------------------------------------------------------

shadowDark:{[hw;hs;d0;d1]
  w:hw({[d0;d1;cats;keep]
    if[not `VENUEMAP in key `.;
      '"VENUEMAP not found - this is not the order server"];
    tg:`date`id_server`id_target xkey select date,id_server,id_target,algo
      from target where date within (d0;d1), (upper algo)=`SHADOW;
    / t_on_market>0 is where every dark query on this desk starts: it drops the
    / children that were rejected or never transmitted, which had no venue
    w:select date,time,id_server,id_target,id_work,sym,venue,state,size,make,
        onmkt_adv1t,t_off_market
      from workorder
      where date within (d0;d1), t_on_market>0;
    / venue -> category.  Two columns only, so nothing else can collide.
    w:w lj `venue xkey select venue,category from VENUEMAP;
    w:select from w where category in cats;
    / "cxl:rotate_venue" -> `rotate_venue.  A state carrying no kill reason
    / keeps its whole name and so matches nothing in keep, which is correct.
    w:update killreason:{`$last ":" vs string x} each lower state from w;
    / filled, or cancelled for a reason we accept and off the market by the end
    w:select from w
      where (make>0) or (t_off_market>0) and killreason in keep;
    / ij, not lj - a child whose parent is not SHADOW drops out
    `date`time xasc (w ij tg)
    };d0;d1;.shd.darkCategories;.shd.keepReasons);
  if[0=count w; :w];
  / work_list is one row per CHILD, and carries the volume that printed at or
  / through that child's own limit while it rested.  date is in the key: over a
  / range, id_work alone repeats across days.
  v:hs({[d0;d1]select date,id_server,id_target,id_work,vvalid,vvalida
      from work_list where date within (d0;d1), algo=`SHADOW};d0;d1);
  r:w lj `date`id_server`id_target`id_work xkey v;
  / sorted by time above, so last IS the latest child of that parent.
  / id_server stays in the by clause so two servers cannot share an id_target,
  / then drops out of the result.
  s:0!select
      shares_routed:sum size,
      shares_executed:sum make,
      adv1t_last:last onmkt_adv1t,
      vvalid:sum vvalid,
      vvalida:sum vvalida
    by date,id_server,id_target,sym from r;
  / stable sorts, so this reads date ascending, biggest order first in each
  `date xasc `shares_routed xdesc delete id_server from s
 };

/ count_chaseprice is NOT the chase counter - the order server writes
/ minExecSize into that column.  The kill reason in state is the only place a
/ chase is recorded.
