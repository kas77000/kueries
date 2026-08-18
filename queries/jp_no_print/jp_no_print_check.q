/ jp_no_print_check.q - Japanese parent orders on stocks that have not traded
/ yet today, i.e. the names that look like they will only go in the closing
/ auction.  Run before the close.  jp_no_print_check_v2.q is the one-function
/ version; this one is broken up so each piece can be run on its own.
/
/   q)\l queries/jp_no_print/jp_no_print_check.q
/   q).jp.h:hopen`:orderserver:5010     / leave 0Ni if the tables are local
/   q).jp.run[]                         / no ticks at all
/   q).jp.notTraded[]                   / quoted but never printed - see below
/   q).jp.scan[]                        / everything, with a status column
/
/ .jp.run is the strict answer: nothing in qatt at all.  .jp.notTraded is
/ usually the one you want - a stock skipping the open is normally still
/ QUOTED, so it has rows but no volume.

/ --- config -----------------------------------------------------------------
.jp.h:0Ni;              / handle to the order server; 0Ni => tables are local
.jp.state:`activated;   / the state value that means "still working"
.jp.sfx:"*.JP";         / pattern matched against target`sym for Japan
.jp.dt:{.z.D};          / trading date  (.z.D = local date, .z.d = UTC date)
/ target`sym -> qatt`sym.  Identity until proven otherwise; target_stock also
/ carries sym_flex, sym_tms and sym_mbpipe if the feeds disagree.
.jp.symmap:{x};

/ --- runs on the order server -----------------------------------------------
/ Parents whose CURRENT state is stt - the last target_state row per order,
/ not any row that ever said it.
.jp.activeOrders:{[dt;sfx;stt]
  t:select date,id_server,id_target,sym,trader,basket,side,size,algo,otype,
      limit_price,t_start,t_end,p_start,p_end,doopen,doclose
    from target
    where date=dt, sym like sfx;
  if[0=count t; :t];
  ids:exec distinct id_target from t;
  s:select t_state:last time, state:last state, ack:last ack,
      leave:last leave, commit:last commit, avg_fill_price:last avg_fill_price
    by date,id_server,id_target
    from target_state
    where date=dt, id_target in ids;
  s:`date`id_server`id_target xkey select from (0!s) where state=stt;
  `sym xasc select from (t lj s) where not null state
 };

/ --- runs locally, against the tick feed ------------------------------------
/ Keyed on qsym so it can be joined straight onto the orders.  totalVolume and
/ trdCount are read rather than guessing which typ values mean a trade.
.jp.qattStats:{[syms]
  syms:distinct syms;
  `qsym xcol 0!select
      nrows:count i,
      vol:0^max totalVolume,
      ntrd:0^max trdCount,
      firstTick:min time,
      lastTick:max time,
      lastPrice:last lastPrice
    by sym from qatt where sym in syms
 };

/ 0Ni => local; a real handle => send activeOrders across as a lambda
.jp.orders:{[dt]
  $[null .jp.h;
    .jp.activeOrders[dt;.jp.sfx;.jp.state];
    .jp.h(.jp.activeOrders;dt;.jp.sfx;.jp.state)]
 };

/ noTicks: nothing in qatt.  quotedNotTraded: quoted, but no volume and no
/ trades - the usual shape of a stock heading for the close only.
.jp.scan:{[]
  o:.jp.orders .jp.dt[];
  if[0=count o; :o];
  o:update qsym:.jp.symmap each sym from o;
  r:o lj `qsym xkey .jp.qattStats exec distinct qsym from o;
  r:update nrows:0^nrows, vol:0^vol, ntrd:0^ntrd from r;
  update status:?[0=nrows;`noTicks;?[(0=vol)&0=ntrd;`quotedNotTraded;`trading]] from r
 };

.jp.run:{[] select from .jp.scan[] where status=`noTicks };
.jp.notTraded:{[] select from .jp.scan[] where status in `noTicks`quotedNotTraded };
.jp.syms:{[] exec distinct sym from .jp.run[] };
