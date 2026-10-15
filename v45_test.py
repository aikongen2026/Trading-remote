#!/usr/bin/env python3
from __future__ import annotations
import os, sqlite3, tempfile, threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import trading_bot
from adaptive_edge import AdaptiveRouter, ShadowLab, dynamic_universe, sector_confirmation
from intelligence_engine import IntelligenceEngine


def check(c,msg):
    if not c: raise AssertionError(msg)
    print('OK:',msg)

# Feed auto-detection: SIP when entitled, IEX on subscription rejection.
cfg=type('C',(),{'stock_feed':'auto'})()
a=trading_bot.AlpacaRest.__new__(trading_bot.AlpacaRest); a.cfg=cfg
a.quotes_latest=lambda syms,feed=None: {'SPY':{}} if feed=='sip' else {}
check(a.resolve_stock_feed()=='sip','auto feed selects SIP when available')

def reject(syms,feed=None):
    raise trading_bot.AlpacaApiError(422,'subscription does not permit SIP')
a.quotes_latest=reject
check(a.resolve_stock_feed()=='iex','auto feed falls back to IEX on SIP subscription rejection')

# Mean reversion waits for an oversold state plus reversal confirmation.
cl=[100.0]*25+[99.8,99.4,99.0,98.5,98.8]
sig,dbg=trading_bot.mean_reversion_signal(cl,98.8)
check(sig==1 and dbg.get('meanrev_z',0)<-1,'neutral mean-reversion route requires reversal confirmation')

# Adaptive routing.
class FakeIntel:
    global_risk=55; regime_score=50; daily_factor_score={'ABC':0}; group_momentum={'DEFAULT':0}; regime_returns={'SPY':0.002}
    def news_score(self,s): return 0
r=AdaptiveRouter(FakeIntel())
mode,_=r.route('ABC','scalp')
check(mode=='meanrev','neutral regime routes to mean reversion')
FakeIntel.regime_score=66; FakeIntel.daily_factor_score={'ABC':5}
mode,_=r.route('ABC','scalp')
check(mode=='trend','risk-on regime routes strong candidate to trend')

# Dynamic universe ranks moving/liquid names above sleepy/wide names.
now=datetime.now(timezone.utc).isoformat()
closes={'FAST':[100+i*.15 for i in range(30)],'SLOW':[100]*30,'WIDE':[100+i*.05 for i in range(30)]}
quotes={
 'FAST':{'bp':104.34,'ap':104.36,'t':now},
 'SLOW':{'bp':99.99,'ap':100.01,'t':now},
 'WIDE':{'bp':100,'ap':102,'t':now},
}
FakeIntel.regime_score=50; FakeIntel.global_risk=50; FakeIntel.daily_factor_score={'FAST':5,'SLOW':0,'WIDE':3}; FakeIntel.group_momentum={'DEFAULT':.01}
active,scores=dynamic_universe(['FAST','SLOW','WIDE'],closes,quotes,FakeIntel(),top_n=2)
check('FAST' in active and scores['FAST']>scores['WIDE'],'dynamic universe rewards movement/liquidity and penalizes wide spread')
sec=sector_confirmation(FakeIntel(),'ABC')
check('ok' in sec and 'score' in sec,'sector confirmation returns explicit gate and score')

# Order-flow imbalance: rising bid pressure should be positive.
ie=IntelligenceEngine.__new__(IntelligenceEngine)
ie.lock=threading.RLock(); ie.live_quotes={}; ie.live_bars={}; from collections import defaultdict,deque
ie.quote_history=defaultdict(lambda:deque(maxlen=30))
key='ABC'
for i in range(12):
    q={'bid':100+i*.01,'ask':100.02+i*.01,'bid_size':100+20*i,'ask_size':220-10*i,'ts':datetime.now(timezone.utc).isoformat(),'market':'STOCK'}
    ie.live_quotes[key]=q; ie.quote_history[key].append(q)
score,dbg=ie.microstructure_score('ABC','STOCK')
check(score>0 and dbg.get('ofi',0)>0,'enhanced OFI detects rising bid-side pressure')

# Champion/challenger is shadow-only and records outcomes.
with tempfile.TemporaryDirectory() as td:
    lab=ShadowLab(td)
    lab.observe(symbol='ABC',price=100,signal=1,intel={'score':75,'micro':3},plan={'probability_up':.61,'expected_value_bps':12},router_mode='trend',sector={'ok':True,'score':3},champion_allowed=True,bot_version='4.5.0-adaptive-edge',min_interval_sec=1)
    dbp=Path(td)/'learning_v3.sqlite'
    with closing(sqlite3.connect(dbp)) as db, db:
        n=db.execute('select count(*) from shadow_signals').fetchone()[0]
        db.execute('update shadow_signals set ts=?',( (datetime.now(timezone.utc)-timedelta(minutes=16)).isoformat(), ))
        db.commit()
    check(n>=3,'champion/challenger records multiple shadow models without orders')
    lab.update_outcomes('ABC',101)
    sm=lab.summary()
    check(any((x.get('samples') or 0)>0 for x in sm['models'].values()),'shadow models receive 15-minute outcomes')

# Revision tag survives into broker client_order_id.
cid=__import__('execution_guard').make_client_order_id('tbe-','STOCK','ABC')
check('v470' in cid,'new orders carry current revision tag')
print('V4.5 TEST PASSED')
