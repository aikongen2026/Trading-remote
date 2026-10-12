from datetime import datetime, timezone, timedelta
from pathlib import Path
import trade_export

now=datetime.now(timezone.utc)
class Cfg: trading_base='https://x'; paper=True
class Fake:
    cfg=Cfg()
    def _req(self, method, url, params=None):
        return [
          {'id':'s1','activity_type':'FILL','transaction_time':(now-timedelta(hours=1)).isoformat(),'price':'105','qty':'1','side':'sell','symbol':'AAA','order_id':'os1'},
          {'id':'b1','activity_type':'FILL','transaction_time':(now-timedelta(hours=2)).isoformat(),'price':'100','qty':'1','side':'buy','symbol':'AAA','order_id':'ob1'},
          {'id':'s2','activity_type':'FILL','transaction_time':(now-timedelta(hours=3)).isoformat(),'price':'99','qty':'1','side':'sell','symbol':'BBB','order_id':'os2'},
          {'id':'b2','activity_type':'FILL','transaction_time':(now-timedelta(hours=4)).isoformat(),'price':'100','qty':'1','side':'buy','symbol':'BBB','order_id':'ob2'},
        ]
r=trade_export.realized_winning_trades(Fake(),1)
assert r['winner_count']==1 and r['trades'][0]['symbol']=='AAA'
assert abs(r['gross_profit_usd']-5.0)<1e-9
src=Path('control_server.py').read_text(encoding='utf-8')
for needle in ['/api/profit_trades','profit_details_1','profit_details_3','profit_details_7','toggleProfitTrades(7)']:
    assert needle in src, needle
print('OK: profitable fill dropdown data')
print('OK: 1/3/7 day expandable result rows')
print('V4.4.1 TEST PASSED')
