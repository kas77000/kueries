/ shadow_dark.q - one row per SHADOW parent order: the shares we routed into
/ the dark, the shares that came back, and the volume that traded while we
/ were in there.  Runs on the order server, no handle.
/
/   q)\l queries/shadow_dark/shadow_dark.q
/   q)shadowDark[.z.D-5;.z.D-1]
/
/ Meant for completed days - onmkt_adv1t on a day still trading is a partial
/ volume, so do not pass today as the end date.
/
/ algo lives on target and everything else on workorder, so this is a join on
/ date/id_server/id_target.  A venue containing DARK or DRK is the dark
/ classification, same match as dark_summary.q.
/
/ One row is one id_target, so a rejected-and-replaced order appears once per
/ attempt.

shadowDark:{[d0;d1]
  dk:("*DARK*";"*DRK*");
  tg:`date`id_server`id_target xkey select date,id_server,id_target,algo
    from target where date within (d0;d1), (upper algo)=`SHADOW;
  / no make>0 filter: the children that never filled are exactly what makes
  / routed differ from executed
  w:select date,time,id_server,id_target,sym,venue,size,make,onmkt_adv1t
    from workorder
    where date within (d0;d1), any (upper venue) like/: dk;
  / ij, not lj - a child whose parent is not SHADOW drops out
  r:`date`time xasc (w ij tg);
  / sorted by time above, so last IS the latest child of that parent
  / id_server stays in the by clause so two servers cannot share an id_target,
  / then drops out of the result
  s:0!select
      shares_routed:sum size,
      shares_executed:sum make,
      adv1t_last:last onmkt_adv1t
    by date,id_server,id_target,sym from r;
  / stable sorts, so this reads date ascending, biggest order first in each
  `date xasc `shares_routed xdesc delete id_server from s
 };
