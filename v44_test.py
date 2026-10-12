#!/usr/bin/env python3
from __future__ import annotations
import json, tempfile
from pathlib import Path
from datetime import datetime, timezone

import trading_bot
from execution_guard import ExecutionGuard
from profit_engine import ProfitEngine
from trade_export import _trade_ledger


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print('OK:', msg)

check(trading_bot.AlpacaRest._order_price_str('COP',126.2868)=='126.29','stock limit prices round to valid cents')
check(trading_bot.AlpacaRest._order_price_str('PENNY',0.123456)=='0.1235','sub-dollar stock prices use four decimals')
check(trading_bot.AlpacaRest._order_price_str('BTC/USD',85123.12345678).startswith('85123.123456'),'crypto precision retained')

# Stop-out cooldown can see nested filled bracket stop legs.
now=datetime.now(timezone.utc).isoformat()
orders=[{'symbol':'AAPL','legs':[{'symbol':'AAPL','side':'sell','type':'stop','status':'filled','filled_at':now}]}]
check(trading_bot.recent_filled_stop_count(orders,'AAPL',1800)==1,'recent bracket stop-out detected')

# Risk tier 70-79 must reduce stock size/risk exposure.
with tempfile.TemporaryDirectory() as td:
    pe=ProfitEngine(td)
    closes=[100+i*0.03 for i in range(80)]
    plan=pe.plan(market='STOCK',symbol='TEST',price=102.4,closes=closes,spread_bps=2,
        intelligence={'score':90,'entry_threshold':60,'global_risk':75,'regime':55,'news':10,'relative':10,'long_factor':5,'event_blackout':False,'risk_veto':False},
        strategy='scalp',profile='active',account_equity=100000,base_stock_qty=2)
    check(plan.volatility_scale==0.5,'global risk 75 cuts stock sizing to 50%')
    check(plan.stop_loss_pct>=0.0025,'noise-adjusted stop has 0.25% floor')
    check(plan.reward_risk>=1.5,'stock plan requires at least 1.5 reward/risk')

# Known 422 rejection must not be treated as unknown/reconcile mystery.
class Reject422(RuntimeError):
    status_code=422
class FakeApi:
    def submit_order(self,*a,**k): raise Reject422('invalid order')
    def cancel_order(self,*a,**k): return None
    def get_order_by_client_order_id(self,*a,**k): return None
with tempfile.TemporaryDirectory() as td:
    g=ExecutionGuard(td)
    g.start_exit('STOCK','AAPL',1,'STOP_LOSS')
    # no protection => first advance can submit
    res=g.advance_exit(FakeApi(),[],market='STOCK',symbol='AAPL',position_qty=1,reason='STOP_LOSS',regular_market_open=True)
    check(res=='EXIT_REJECTED','HTTP 422 becomes definitive EXIT_REJECTED')
    check(g.get_exit('STOCK','AAPL').get('phase')=='rejected_known','422 stored as known rejection')

ledger=_trade_ledger([
    {'transaction_time':'2026-10-01T10:00:00Z','symbol':'ABC','side':'buy','qty':'2','price':'100','order_id':'b'},
    {'transaction_time':'2026-10-01T10:05:00Z','symbol':'ABC','side':'sell','qty':'2','price':'101','order_id':'s'},
])
check(len(ledger)==1 and abs(float(ledger[0]['gross_pnl_usd'])-2.0)<1e-9,'analysis export creates FIFO trade ledger')

src=Path('control_server.py').read_text(encoding='utf-8')
check('st_cloud' in src and 'RENDER FREE' in src,'dashboard exposes Cloud 24/7 status')
rp=Path('render.yaml')
if rp.exists():
    ry=rp.read_text(encoding='utf-8')
    check('plan: 0.5c-512mb' in ry and 'mountPath: /var/data' in ry,'Render blueprint uses paid always-on compute and persistent disk')
print('V4.4 TEST PASSED')
