/ =============================================================================
/ limit_up_down.q
/
/ One function.  Takes our currently ACTIVATED parent orders, looks up only
/ those names in qatt, and returns the ones whose stock has been stuck limit
/ up / limit down for more than N minutes, counting back from right now.
/
/ Covers every market we trade - most of APAC runs daily price limits (JP, KR,
/ TW, CN, TH, VN, MY, ID, IN, PH ...), each with its own band.  Venues with no
/ daily limit at all (HK, AU, SP, NZ) are excluded up front - see note 2.
/
/ A quote is treated as limit up/down when it is either
/   * LOCKED    - qbid and qask equal each other, or
/   * ONE SIDED - one side is zero and the other carries the limit price
/ and it counts only if the stock has had no normal two sided quote at any
/ point in the last N minutes.
/
/   q)\l queries/limit_up_down/limit_up_down.q
/   q)h:hopen`:orderserver:5010
/   q)limitUpDown[h;20]
/
/ target, target_state and target_stock must be reachable.  If they are NOT in
/ the process you are connected to, h has to be an open handle to the order
/ server - otherwise you will get 'target.  Pass 0i only if they are local.
/ Narrow to a market with the country / region columns in the result, e.g.
/   q)select from limitUpDown[h;20] where country=`JP
/   q)select from limitUpDown[h;20] where sym like "*.JP"
/ =============================================================================

limitUpDown:{[h;mins]
  now:.z.T;
  t0:now-60000*mins;
  d:.z.D;
  / Markets with NO daily price limit - a one sided or locked quote there is a
  / thin book, a stale quote, a halt or an auction imbalance, never a limit.
  / Edit this list if the book covers venues beyond APAC (US and Europe have no
  / daily limits either, only intraday collars).
  nl:("*.HK";"*.AU";"*.SP";"*.NZ");
  / --- on the order server: activated parent orders + their reference close.
  / adjclose/orgclose is the previous close the limit band is measured from,
  / and is what tells us which way a locked stock is pinned.
  / nl is passed in, not closed over - a lambda sent over IPC carries no
  / reference to the locals of the function that defined it.
  f:{[d;nl]
    t:select date,id_server,id_target,sym,trader,side,sidesign,size,algo,
        t_start,t_end
      from target where date=d, not any sym like/: nl;
    ids:exec distinct id_target from t;
    s:select state:last state, leave:last leave by date,id_server,id_target
      from target_state where date=d, id_target in ids;
    s:`date`id_server`id_target xkey select from (0!s) where state=`activated;
    / one row per target, so no aggregation needed here
    x:`date`id_server`id_target xkey select date,id_server,id_target,
        adjclose,orgclose,country,region,currency
      from target_stock where date=d, id_target in ids;
    select from ((t lj s) lj x) where not null state
    };
  / 0<h so that 0i, 0Ni and a real handle all behave.  `null h` alone would
  / send 0i down the IPC branch, and handle 0 is the current process.
  o:$[0<h; h(f;d;nl); f[d;nl]];
  if[0=count o; :o];
  / reference price: adjusted close, falling back to the unadjusted one
  o:update ref:orgclose^adjclose from o;
  / --- locally: quote history for OUR names only.  qatt`sym carries the `g
  / attribute, so putting sym first makes this an index lookup, not a day scan.
  syms:exec distinct sym from o;
  q:select time,sym,qbid:0^qbid,qask:0^qask,netChange:0^netChange
    from qatt where sym in syms, time<=now, (0<0^qbid)|0<0^qask;
  / locked, or one sided with a price on the surviving side
  q:update lim:((qbid=qask)&0<qbid)|((0=qbid)&0<qask)|((0=qask)&0<qbid) from q;
  / where the quote stands now, and the last time it was NOT in limit state.
  / A stock pinned at the limit often stops updating altogether, so we anchor
  / on the last normal quote rather than counting rows inside the window -
  / that way a completely static quote is still detected.
  k:select firstQuote:first time, lastQuote:last time, nquotes:count i,
      qbid:last qbid, qask:last qask, netChange:last netChange, lim:last lim,
      lastNormal:max ?[lim;0Nt;time]
    by sym from q;
  / in limit right now, and nothing normal anywhere in the lookback window
  k:select from (0!k) where lim, (null lastNormal)|lastNormal<t0;
  k:update kind:?[qbid=qask;`locked;`oneSided],
      forMins:"j"$(now-?[null lastNormal;firstQuote;lastNormal])%60000
    from k;
  / keep only the orders sitting on one of those names
  r:o ij `sym xkey delete lim from k;
  / DIRECTION.
  /   one sided is unambiguous: no offer => limit up, no bid => limit down.
  /   locked needs a reference - compare the locked price to the prev close.
  /   netChange is only a last resort: it comes from the last TRADED price, so
  /   it is 0 or null on exactly the stocks that have not traded, which is most
  /   of the ones we are hunting here.  `unknown means go and look.
  r:update pxLimit:?[0=qbid;qask;qbid] from r;
  r:update pctFromClose:100*(pxLimit-ref)%ref from r;
  r:update dir:?[0=qask;`up;?[0=qbid;`down;
      ?[pxLimit>ref;`up;?[pxLimit<ref;`down;
      ?[netChange>0;`up;?[netChange<0;`down;`unknown]]]]]] from r;
  / buying into a limit up, or selling into a limit down, is the painful side
  r:update blocked:((sidesign>0)&dir=`up)|((sidesign<0)&dir=`down) from r;
  `blocked`forMins xdesc delete orgclose,adjclose from r
 };

/ -----------------------------------------------------------------------------
/ Notes
/
/ 1. DIRECTION.  One sided quotes settle it on their own.  For a LOCKED quote
/    the direction comes from pxLimit vs ref (adjclose, or orgclose if that is
/    null) - the previous close every APAC band is measured from.  netChange is
/    kept only as a fallback for names with no reference price, because it is
/    derived from the last traded price and is therefore 0 or null on a stock
/    that has not printed - the normal state for something locked at the limit.
/    Anything we still cannot call comes back as `unknown rather than guessed.
/
/ 2. MARKETS WITHOUT PRICE LIMITS are excluded up front by the nl list at the
/    top of the function - Hong Kong, Australia, Singapore (.SP) and New
/    Zealand have no daily limit on stocks, so anything one sided or locked
/    there is a thin or stale market, a halt or an auction imbalance, and would
/    be a false positive.  The filter is on the sym SUFFIX rather than the
/    country column, because the suffix convention is known and the country
/    codes in target_stock may not use the same spelling.  country and region
/    still come through in the result for slicing.
/
/    It is a blacklist, so a new venue with no limits is a false positive until
/    it is added.  If you would rather fail safe, invert it to a whitelist of
/    the venues that DO have limits - at the cost of silently dropping any
/    market missing from the list.
/
/ 3. pctFromClose is the sanity check, and it matters more across APAC than in
/    any single market because the bands differ a lot - roughly 10% in Taiwan
/    and mainland China, 30% in Korea and Thailand, and a stepped yen amount in
/    Japan.  Verify against your own reference data rather than trusting those
/    numbers.  The useful property is simply that a genuine limit sits AT the
/    band: something locked at +0.1% is a locked market, not a limit.
/
/ 4. blocked flags the orders that actually cannot get done: a buy order
/    (sidesign>0) on a stock locked limit UP, or a sell (sidesign<0) on a
/    stock locked limit DOWN.  The other combination is still worth seeing -
/    you are on the right side of it - so it is returned too, just sorted
/    below.  This assumes sidesign is +1 buy / -1 sell; check that holds.
/
/ 5. forMins is how long the stock has been in limit state - measured from its
/    last normal two sided quote, or from its first quote of the day if it has
/    never been normal today.  Everything returned has forMins > mins.
/
/ 6. lastNormal null means the stock has been one sided or locked since its
/    very first quote.  That will also pick up names one sided right through a
/    pre-open indicative phase, which is usually what you want, but it is worth
/    knowing they are in there.
/
/ 7. Rows where BOTH sides are zero are dropped before anything else.  They
/    carry no quote (trade only prints, pre-open gaps) and would otherwise
/    look one sided and break the streak.
/
/ 8. Same symbology caveat as jp_no_print_check.q - qatt`sym may not match
/    target`sym.  If it does not, this returns nothing at all.
/ -----------------------------------------------------------------------------
