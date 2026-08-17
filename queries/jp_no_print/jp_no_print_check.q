/ =============================================================================
/ jp_no_print_check.q
/
/ Purpose
/   Find parent orders on Japanese stocks that are still ACTIVE ("activated")
/   but whose stock has not printed anything on the tick feed yet today.
/
/   In Japan a stock can skip the opening auction and the whole continuous
/   session and only execute in the closing auction.  Run this BEFORE the
/   close to see which of the names we are working are in that situation.
/
/ Data
/   target        parent orders                  (order server  - REMOTE)
/   target_state  state snapshots per order      (order server  - REMOTE)
/   qatt          realtime quote/trade ticks     (this process  - LOCAL)
/
/ Usage
/   q)\l queries/jp_no_print_check.q
/   q).jp.h:hopen`:orderserver:5010     / leave as 0Ni if target/state are local
/   q).jp.run[]                         / suspect orders  (no lines in qatt)
/   q).jp.syms[]                        / just the symbol list
/   q).jp.scan[]                        / every active JP order + its tick status
/   q).jp.notTraded[]                   / no lines OR lines but zero volume
/
/ Read only - nothing here writes or amends any table.
/ =============================================================================


/ -----------------------------------------------------------------------------
/ Configuration
/ -----------------------------------------------------------------------------

.jp.h:0Ni;              / handle to the order server; 0Ni => tables are local
.jp.state:`activated;   / the state value that means "still working"
.jp.sfx:"*.JP";         / pattern matched against target`sym for Japan
.jp.dt:{.z.D};          / trading date  (.z.D = local date, .z.d = UTC date)

/ target`sym -> qatt`sym.  Identity by default.  If the tick feed uses a
/ different symbology than the order book, override this - target_stock
/ carries sym_flex / sym_tms / sym_mbpipe for exactly that purpose.  e.g.
/   .jp.map:(!) . flip exec (sym;sym_tms) from target_stock where date=.z.D
/   .jp.symmap:{.jp.map x}
.jp.symmap:{x};


/ -----------------------------------------------------------------------------
/ Remote side - executes on the ORDER SERVER
/
/ Self contained (touches only its arguments and target / target_state) so it
/ can be shipped over IPC without deploying anything on the far side.
/ Returns the parent orders whose CURRENT state is `activated.
/ -----------------------------------------------------------------------------

.jp.activeOrders:{[dt;sfx;stt]
  / Japanese parent orders for the date
  t:select date,id_server,id_target,sym,trader,basket,side,size,algo,otype,
      limit_price,t_start,t_end,p_start,p_end,doopen,doclose
    from target
    where date=dt, sym like sfx;
  if[0=count t; :t];
  / Latest state row per order.  target_state is appended in time order so
  / `last` is the current state.  Keyed on (date;id_server;id_target) because
  / id_target is only unique within a given server.
  ids:exec distinct id_target from t;
  s:select t_state:last time, state:last state, ack:last ack,
      leave:last leave, commit:last commit, avg_fill_price:last avg_fill_price
    by date,id_server,id_target
    from target_state
    where date=dt, id_target in ids;
  / keep only the orders that are currently active, then re-key for the join
  s:`date`id_server`id_target xkey select from (0!s) where state=stt;
  `sym xasc select from (t lj s) where not null state
 };


/ -----------------------------------------------------------------------------
/ Local side - executes against the realtime qatt table
/
/ Per symbol tick summary.  qatt`sym carries the `g attribute so the `in`
/ filter is an index lookup rather than a scan.
/ -----------------------------------------------------------------------------

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


/ -----------------------------------------------------------------------------
/ Orchestration
/ -----------------------------------------------------------------------------

/ fetch the active orders, remotely if a handle is configured
.jp.orders:{[dt]
  $[null .jp.h;
    .jp.activeOrders[dt;.jp.sfx;.jp.state];
    .jp.h(.jp.activeOrders;dt;.jp.sfx;.jp.state)]
 };

/ every active JP order, joined to what the tick feed has seen so far.
/   status = `noTicks          nothing at all in qatt
/            `quotedNotTraded  quoted, but zero volume / zero trade count
/            `trading          has traded
.jp.scan:{[]
  o:.jp.orders .jp.dt[];
  if[0=count o; :o];
  o:update qsym:.jp.symmap each sym from o;
  r:o lj `qsym xkey .jp.qattStats exec distinct qsym from o;
  r:update nrows:0^nrows, vol:0^vol, ntrd:0^ntrd from r;
  update status:?[0=nrows;`noTicks;?[(0=vol)&0=ntrd;`quotedNotTraded;`trading]] from r
 };

/ THE ANSWER: active JP orders whose stock has no lines in qatt at all.
.jp.run:{[] select from .jp.scan[] where status=`noTicks };

/ Wider net: nothing in qatt, or lines but nothing has actually traded.
/ In practice this is the one to watch - a name can be quoted all morning
/ and still not print until the closing auction.
.jp.notTraded:{[] select from .jp.scan[] where status in `noTicks`quotedNotTraded };

/ just the symbols, for pasting into a blotter
.jp.syms:{[] exec distinct sym from .jp.run[] };


/ =============================================================================
/ Ad hoc version - same logic, no setup.  Run from the tick process with `h`
/ already open onto the order server:
/
/   o:h({[dt;sfx;stt]
/     t:select date,id_server,id_target,sym,side,size,algo,t_start,t_end,
/         doopen,doclose from target where date=dt, sym like sfx;
/     ids:exec distinct id_target from t;
/     s:select state:last state, leave:last leave by date,id_server,id_target
/       from target_state where date=dt, id_target in ids;
/     s:`date`id_server`id_target xkey select from (0!s) where state=stt;
/     select from (t lj s) where not null state
/     };.z.D;"*.JP";`activated);
/
/   seen:exec distinct sym from qatt where sym in exec distinct sym from o;
/   select from o where not sym in seen
/
/ =============================================================================


/ =============================================================================
/ Notes / assumptions - worth a read before you trust the output
/
/ 1. SYMBOLOGY.  The query assumes target`sym and qatt`sym use the same
/    symbology.  If they do not, every name looks like `noTicks and the result
/    is worthless.  Sanity check on a normal day:
/       q)count select from qatt where sym in exec distinct sym from .jp.orders .jp.dt[]
/    If that comes back 0, set .jp.symmap from target_stock's sym_flex /
/    sym_tms / sym_mbpipe columns.
/
/ 2. qatt HAS NO date COLUMN, so it is taken to be the intraday/RDB table
/    holding today only.  If you point this at an HDB, add `date=dt` to the
/    where clause in .jp.qattStats.
/
/ 3. "LATEST STATE" relies on target_state being appended in time order per
/    order, which is the normal tick convention.  If that is not guaranteed on
/    your server, sort first - at the cost of a full day sort.
/
/ 4. qatt MIXES QUOTES AND TRADES (it has typ, cond and trade fields next to
/    qbid/qask).  "no lines in qatt" therefore means no quote AND no trade.
/    A Japanese name that skips the open and the continuous session will often
/    still be quoted, which is why .jp.notTraded[] exists and is the more
/    reliable signal.  The volume test uses totalVolume/trdCount rather than
/    filtering on typ, so it does not depend on knowing the feed's typ values.
/
/ 5. TIMEZONE.  .z.D is the process's local date.  If the tick process does not
/    run on Tokyo time, set .jp.dt to return the JP trading date instead.
/
/ 6. CLOSE-ONLY ORDERS.  Orders deliberately aimed at the close (doclose=1,
/    doopen=0, or a t_start near the close) show up as `noTicks too - that is
/    expected, not an anomaly.  doopen / doclose / t_start / t_end are in the
/    output so you can filter them:
/       q)select from .jp.run[] where doopen=1
/
/ 7. The active state value is parameterised via .jp.state in case `activated`
/    is spelled differently on your server.
/ =============================================================================
