/ jp_no_print_check_v2.q - active JP parent orders whose stock has not printed
/ on the tick feed yet today, i.e. the names that look like they will only
/ trade in the closing auction.  Run before the close.
/
/   q)\l queries/jp_no_print/jp_no_print_check_v2.q
/   q)h:hopen`:orderserver:5010
/   q)jpNoPrint h
/
/ h must reach target and target_state, or you get 'target.  Pass 0i if they
/ are local.

jpNoPrint:{[h]
  d:.z.D;
  / on the order server: JP parents whose CURRENT state is activated
  f:{[d]
    t:select date,id_server,id_target,sym,trader,side,size,algo,
        t_start,t_end,doopen,doclose
      from target where date=d, sym like "*.JP";
    s:select state:last state, leave:last leave, t_state:last time
      by date,id_server,id_target
      from target_state
      where date=d, id_target in exec distinct id_target from t;
    s:`date`id_server`id_target xkey select from (0!s) where state=`activated;
    select from (t lj s) where not null state
    };
  / 0<h, not null h: null 0i is false and handle 0 is the current process
  o:$[0<h; h(f;d); f d];
  / locally: drop anything the tick feed has already seen
  syms:exec distinct sym from o;
  seen:exec distinct sym from qatt where sym in syms;
  `sym xasc select from o where not sym in seen
 };
