"""Read-only Alpaca trade stream and restart/backfill archive. Never sends orders."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

ORDER_FIELDS = {'id','client_order_id','symbol','asset_class','side','type','order_type','order_class',
                'qty','filled_qty','filled_avg_price','limit_price','stop_price','status','submitted_at',
                'filled_at','canceled_at','expired_at','updated_at','created_at','time_in_force'}

def clean_order(order):
    out = {k:v for k,v in order.items() if k in ORDER_FIELDS}
    if isinstance(order.get('legs'), list):
        out['legs'] = [clean_order(o) for o in order['legs'] if isinstance(o,dict)]
    return out

class BrokerHistory:
    _instances = {}
    _instances_lock = threading.Lock()

    def __init__(self, state_dir):
        self.path = str(Path(state_dir)/'broker_history.sqlite')
        self.lock = threading.RLock()
        self.reconcile_needed = threading.Event()
        self.reconcile_needed.set()
        self.stop = threading.Event()
        self.health = {'stream':'STARTING', 'last_event_utc':None, 'last_sync_utc':None,
                       'sync_complete':False, 'last_error':'', 'backfill_days':35}
        self.enabled=True
        try:
            with closing(sqlite3.connect(self.path,timeout=5)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE IF NOT EXISTS trade_events(id TEXT PRIMARY KEY,ts TEXT,payload TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,payload TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,payload TEXT)')
        except Exception as exc:
            self.enabled=False
            self.health.update(stream='ARCHIVE_ERROR',last_error=type(exc).__name__)

    def ingest(self, message):
        if isinstance(message, bytes): message=message.decode('utf-8')
        if isinstance(message, str): message=json.loads(message)
        if isinstance(message, list):
            for item in message: self.ingest(item)
            return
        if not isinstance(message,dict): return
        stream=message.get('stream'); data=message.get('data') or {}
        if stream=='authorization':
            if data.get('status')!='authorized':
                self.health['stream']='AUTH_ERROR'
                raise PermissionError('Trade stream authorization rejected')
            return
        if stream=='listening':
            self.health['stream']='CONNECTED' if 'trade_updates' in data.get('streams',[]) else 'SUBSCRIPTION_ERROR'
            return
        if stream!='trade_updates' or not isinstance(data,dict) or not isinstance(data.get('order'),dict): return
        row={k:data[k] for k in ('event','timestamp','execution_id','price','qty','position_qty') if k in data}
        row['order']=clean_order(data['order'])
        raw=json.dumps(row,sort_keys=True,separators=(',',':'))
        identity=(str(row['order'].get('id'))+'|'+str(row.get('event'))+'|'+str(row['execution_id'])) if row.get('execution_id') else raw
        eid=hashlib.sha256(identity.encode()).hexdigest()
        ts=str(row.get('timestamp') or datetime.now(timezone.utc).isoformat())
        with self.lock, closing(sqlite3.connect(self.path,timeout=5)) as db, db:
            db.execute('INSERT OR IGNORE INTO trade_events VALUES(?,?,?)',(eid,ts,raw))
            order=row['order']
            if order.get('id'):
                db.execute('INSERT OR REPLACE INTO orders VALUES(?,?)',(order['id'],json.dumps(order)))
        self.health['last_event_utc']=ts
        self.reconcile_needed.set()  # REST positions/orders remain execution authority.

    def sync(self, api, now=None):
        from trade_export import _fetch_orders, _fetch_activities
        now=now or datetime.now(timezone.utc)
        # Full bounded backfill catches orders changed while the process was asleep.
        orders=_fetch_orders(api,now-timedelta(days=35),until=now)
        activities=_fetch_activities(api,now-timedelta(days=35),now)
        with self.lock, closing(sqlite3.connect(self.path,timeout=5)) as db, db:
            db.executemany('INSERT OR REPLACE INTO orders VALUES(?,?)',
                           [(o['id'],json.dumps(clean_order(o))) for o in orders if o.get('id')])
            db.executemany('INSERT OR REPLACE INTO activities VALUES(?,?)',
                           [(a['id'],json.dumps(a)) for a in activities if a.get('id')])
        self.health.update(last_sync_utc=now.isoformat(),sync_complete=True,last_error='')

    def snapshot(self, since, until):
        start=datetime.fromisoformat(str(since).replace('Z','+00:00'))
        end=datetime.fromisoformat(str(until).replace('Z','+00:00'))
        def within(value):
            try: return start <= datetime.fromisoformat(str(value).replace('Z','+00:00')) <= end
            except (ValueError,TypeError): return False
        with closing(sqlite3.connect(self.path,timeout=5)) as db:
            events=[json.loads(raw) for ts,raw in db.execute('SELECT ts,payload FROM trade_events') if within(ts)]
            activities=[json.loads(row[0]) for row in db.execute('SELECT payload FROM activities')]
        return {'trade_updates':events, 'activities': [a for a in activities if within(a.get('transaction_time') or a.get('created_at') or a.get('date'))]}

    def summary(self):
        return dict(self.health)

    def start(self, api, cfg):
        if not self.enabled: return
        if getattr(self,'started',False): return
        self.started=True
        with self._instances_lock:
            previous=self._instances.get(self.path)
            if previous is not None: previous.stop.set()
            self._instances[self.path]=self
        def backfill():
            while not self.stop.is_set():
                try: self.sync(api)
                except Exception as exc:
                    self.health.update(sync_complete=False,last_error=type(exc).__name__)
                self.stop.wait(300)
        def stream():
            import websocket
            url='wss://paper-api.alpaca.markets/stream' if cfg.paper else 'wss://api.alpaca.markets/stream'
            while not self.stop.is_set():
                ws=None
                try:
                    self.health['stream']='CONNECTING'
                    ws=websocket.create_connection(url,timeout=10)
                    ws.send(json.dumps({'action':'auth','key':cfg.key,'secret':cfg.secret}))
                    auth=ws.recv(); self.ingest(auth)
                    # Authentication must be positively confirmed before subscribing.
                    parsed=json.loads(auth.decode() if isinstance(auth,bytes) else auth)
                    if isinstance(parsed,list): parsed=next((m for m in parsed if m.get('stream')=='authorization'),{})
                    if parsed.get('stream')!='authorization' or parsed.get('data',{}).get('status')!='authorized':
                        raise PermissionError('Missing authorization acknowledgement')
                    ws.send(json.dumps({'action':'listen','data':{'streams':['trade_updates']}}))
                    self.reconcile_needed.set()
                    while not self.stop.is_set():
                        try: self.ingest(ws.recv())
                        except websocket.WebSocketTimeoutException:
                            ws.ping()
                except Exception as exc:
                    self.health['stream']='AUTH_ERROR' if isinstance(exc,PermissionError) else 'RECONNECTING'
                    self.health['last_error']=type(exc).__name__
                finally:
                    if ws is not None: ws.close()
                self.stop.wait(10)
        threading.Thread(target=backfill,name='broker-history',daemon=True).start()
        threading.Thread(target=stream,name='broker-trade-updates',daemon=True).start()

def archived_evidence(state_dir,since,until):
    path=Path(state_dir)/'broker_history.sqlite'
    if not path.exists(): return {'trade_updates':[], 'activities':[], 'warning':'Broker archive not yet available'}
    try: return BrokerHistory(state_dir).snapshot(since,until)
    except Exception as exc: return {'trade_updates':[], 'activities':[], 'warning':type(exc).__name__}
