/ =============================================================================
/ jp_no_print_check_v2.q
/
/ One function.  Returns the active Japanese parent orders whose stock has not
/ printed anything on the tick feed yet today - i.e. the names that look like
/ they will only trade in the closing auction.  Run it before the close.
/
/   q)\l queries/jp_no_print_check_v2.q
/   q)h:hopen`:orderserver:5010
/   q)jpNoPrint h
/
/ If target / target_state happen to be in this same process, pass 0i.
/ =============================================================================

jpNoPrint:{[h]
  d:.z.D;
  / --- on the order server: Japanese parent orders whose CURRENT state is activated
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
  o:$[null h; f d; h(f;d)];
  / --- locally: drop anything the tick feed has already seen
  syms:exec distinct sym from o;
  seen:exec distinct sym from qatt where sym in syms;
  `sym xasc select from o where not sym in seen
 };

/ -----------------------------------------------------------------------------
/ Two things to keep in mind (see jp_no_print_check.q for the full version):
/
/   * assumes target`sym and qatt`sym use the same symbology.  If they do not,
/     everything comes back as a false positive.
/
/   * "no lines in qatt" means no quote AND no trade.  A JP stock that skips
/     the open and the continuous session is often still quoted, so it would
/     have rows in qatt and be missed here.  jp_no_print_check.q's
/     .jp.notTraded[] also catches quoted-but-zero-volume names.
/ -----------------------------------------------------------------------------
