#!/usr/bin/env python3
from __future__ import annotations
import io, json, zipfile, tempfile
from pathlib import Path
import trade_export
from execution_guard import make_client_order_id


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print('OK:', msg)

cid=make_client_order_id('tbe-','STOCK','AAPL')
check(cid.startswith('tbe-sv442-'), 'new client order ids include revision tag')
check(trade_export._client_version_tag(cid)=='v442', 'revision tag can be recovered from client order id')

legacy_files={
 '00_manifest.json': {'export_schema':'tradingbot-analysis-v1','bot_version':'4.4.2-ai-audit','range_start_utc':'2026-10-12T10:00:00Z','range_end_utc':'2026-10-12T11:00:00Z','days':1,'paper':True,'errors':[]},
 '01_summary.json': {'selected_days':1},
 '02_account_snapshot.json': {'equity':'1000'},
 '03_positions_snapshot.json': [],
 '04_orders.json': [
   {'id':'b','symbol':'ABC','client_order_id':'tbe-sv442-ABC-123','order_type':'market','order_class':'bracket','legs':[]},
   {'id':'s','symbol':'ABC','client_order_id':'tbx-sv442-ABC-456','order_type':'market','order_class':'','legs':[]},
 ],
 '05_activities.json': [
   {'transaction_time':'2026-10-12T10:01:00Z','activity_type':'FILL','symbol':'ABC','side':'buy','qty':'1','price':'100','order_id':'b'},
   {'transaction_time':'2026-10-12T10:10:00Z','activity_type':'FILL','symbol':'ABC','side':'sell','qty':'1','price':'101','order_id':'s'},
 ],
 '08_portfolio_history.json': {},
 '10_status_snapshot.json': {'cloud_runtime':{'render':True,'always_on_expected':False},'intelligence':{'global_risk':75,'regime_label':'NEUTRAL','source_health':{'alpaca_news':'CONNECTED'}}},
 '11_control_snapshot.json': {}, '12_execution_state.json': {}, '13_trade_plans.json': {},
}

def jb(x): return json.dumps(x).encode()
mem=io.BytesIO()
with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
    for n,v in legacy_files.items(): z.writestr(n,jb(v))
    z.writestr('06_fills.csv','transaction_time,activity_type,symbol,side,qty,price,order_id\n2026-10-12T10:01:00Z,FILL,ABC,buy,1,100,b\n2026-10-12T10:10:00Z,FILL,ABC,sell,1,101,s\n')
    z.writestr('07_fees.csv','')
    z.writestr('08_portfolio_history.csv','')
    z.writestr('09_bot_decisions.csv','symbol,ts,action\nABC,2026-10-12T10:01:00Z,BUY 1 BRACKET\n')
orig=trade_export.build_analysis_export
trade_export.build_analysis_export=lambda *a,**k:(mem.getvalue(),legacy_files['00_manifest.json'])
try:
    
    class FakeBars:
        cfg=type('C',(),{'stock_feed':'iex'})()
        def bars(self,*a,**k): return {'ABC':[{'t':'2026-10-12T10:01:00Z','h':100.5,'l':99.8,'c':100.1},{'t':'2026-10-12T10:10:00Z','h':101.2,'l':100.8,'c':101.0}]}
        def crypto_bars(self,*a,**k): return {}
    blob,manifest=trade_export.build_ai_analysis_export(1,'.','4.4.2-ai-audit',api=FakeBars())
finally:
    trade_export.build_analysis_export=orig
with zipfile.ZipFile(io.BytesIO(blob)) as z:
    check(z.namelist()==['AI_ANALYSE.json'],'AI export contains one uploadable analysis file')
    p=json.loads(z.read('AI_ANALYSE.json'))
check(p['format']=='TradingBot-AI-Audit-v2','AI payload schema present')
check(p['diagnostics']['all_matched_trades']['gross_pnl_usd']==1.0,'AI diagnostics calculate matched trade P/L')
check(p['completed_trades'][0]['entry_version_tag']=='v442','AI trade ledger carries version attribution')
check('orders_raw' in p and 'bot_decisions_raw' in p and 'source_health' in p['diagnostics'],'AI file contains raw evidence plus diagnostics')
print('V4.4.2 TEST PASSED')
