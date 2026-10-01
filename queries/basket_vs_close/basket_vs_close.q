{[s]
  / Eligible targets
  t:select from target where basket like s, sym like "*.IN";

  / Latest state and make per target
  ts:select state:last state, exec_qty:last make by id_target
    from target_state where id_target in t`id_target;

  / Keep only activated targets
  live:exec id_target from ts where state=`activated;
  t:select from t where id_target in live;

  / Latest value per (id_target,cname)
  tc:select last dvalue, last svalue by id_target,cname
    from `tupd xasc
    select id_target,cname,dvalue,svalue,tupd
    from target_column
    where id_target in t`id_target,
      cname in `VPCT`PVOLUME`EFFPVOLUME`ESTMOC`ESTVRES`ALGMOC`PVWAP`EST_STIME;

  / Pivot cname -> columns
  tcp:0!select
      VPCT:first dvalue where cname=`VPCT,
      PVOLUME:first dvalue where cname=`PVOLUME,
      EFFPVOLUME:first dvalue where cname=`EFFPVOLUME,
      ESTMOC:first dvalue where cname=`ESTMOC,
      ESTVRES:first dvalue where cname=`ESTVRES,
      ALGMOC:first dvalue where cname=`ALGMOC,
      PVWAP:first dvalue where cname=`PVWAP,
      EST_STIME:first svalue where cname=`EST_STIME
    by id_target
    from tc;

  / make and avg_fill_price are cumulative: last row per child holds its totals
  w:select last make, last avg_fill_price by id_target,id_work
    from workorder where id_target in t`id_target;
  / filled or partially filled children only, size-weighted
  w:select wo_count:count i, wo_qty:sum make, avg_px:make wavg avg_fill_price
    by id_target from w where make>0, avg_fill_price>0;

  / Previous close
  em:`sym xkey select sym, prev_close:PX_LAST
    from equity_master where sym in t`sym;

  / Final result
  r:(t lj `id_target xkey tcp) lj `id_target xkey select id_target,exec_qty from ts;
  r:(r lj w) lj em;
  / buy below / sell above the close; 0b when there is no fill or no close
  update better_than_close:(avg_px>0)&(prev_close>0)&0<sidesign*prev_close-avg_px
    from r
  }["*CALPERS*"]
