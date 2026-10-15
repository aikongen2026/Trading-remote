"""Offline regressions for broker evidence, recovery, cutoffs and risk sizing."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from broker_history import BrokerHistory
from execution_guard import ExecutionGuard
from profit_engine import ProfitEngine
from runtime_audit import EntryConfirmation
from state_storage import StateBackup, FILES
from trade_export import _fetch_orders, _fetch_activities, _learning_rows, _shadow_rows

NOW=datetime(2026,10,15,12,tzinfo=timezone.utc)

class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_binary_partial_fills_deduplicate_without_secrets(self):
        history=BrokerHistory(self.root)
        history.ingest({'stream':'listening','data':{'streams':['trade_updates']}})
        event={'stream':'trade_updates','data':{'event':'partial_fill','execution_id':'e1',
            'timestamp':NOW.isoformat(),'qty':'1','price':'100','secret':'never-store',
            'order':{'id':'o1','symbol':'MSFT','status':'partially_filled','api_key':'never-store'}}}
        history.ingest(json.dumps(event).encode())
        event['data']['order']['status']='filled'
        history.ingest(event)
        event['data']['execution_id']='e2'
        history.ingest(event)
        evidence=history.snapshot((NOW-timedelta(days=1)).isoformat(),NOW.isoformat())
        self.assertEqual(len(evidence['trade_updates']),2)
        self.assertNotIn('never-store',json.dumps(evidence))
        self.assertEqual(history.summary()['stream'],'CONNECTED')
        self.assertTrue(history.reconcile_needed.is_set())

    def test_unauthorized_stream_is_not_connected(self):
        history=BrokerHistory(self.root)
        with self.assertRaises(PermissionError):
            history.ingest({'stream':'authorization','data':{'status':'unauthorized'}})
        self.assertEqual(history.summary()['stream'],'AUTH_ERROR')

    def test_restart_backfill_is_idempotent_and_keeps_old_evidence(self):
        api=Mock(cfg=SimpleNamespace(trading_base='test'))
        order={'id':'o','submitted_at':(NOW-timedelta(days=1)).isoformat()}
        fee={'id':'fee','activity_type':'FEE','created_at':NOW.isoformat(),'date':'2026-10-01','net_amount':'-0.04'}
        api._req.side_effect=lambda method,url,**kw: [order] if url.endswith('/orders') else [fee]
        history=BrokerHistory(self.root)
        history.sync(api,NOW)
        BrokerHistory(self.root).sync(api,NOW)
        with closing(sqlite3.connect(history.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM orders').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM activities').fetchone()[0],1)
        self.assertTrue(history.summary()['sync_complete'])
        self.assertEqual(len(history.snapshot(NOW.isoformat(),NOW.isoformat())['activities']),1)

    def test_incomplete_pagination_is_an_error(self):
        api=Mock(cfg=SimpleNamespace(trading_base='test'))
        api._req.return_value=[{'id':str(n),'submitted_at':NOW.isoformat()} for n in range(500)]
        with self.assertRaisesRegex(RuntimeError,'ambiguous'):
            _fetch_orders(api,NOW-timedelta(days=1),until=NOW)
        api._req.return_value=[{'id':str(n),'transaction_time':NOW.isoformat()} for n in range(100)]
        with self.assertRaisesRegex(RuntimeError,'stalled'):
            _fetch_activities(api,NOW-timedelta(days=1),NOW)

    def test_export_cutoffs_compare_instants_including_timezone_offsets(self):
        path=self.root/'learning_v3.sqlite'
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE decisions(id INTEGER PRIMARY KEY,ts TEXT)')
            db.execute('CREATE TABLE shadow_signals(id INTEGER PRIMARY KEY,ts TEXT)')
            for table in ('decisions','shadow_signals'):
                db.executemany('INSERT INTO '+table+'(ts) VALUES(?)',[
                    ('2026-10-15T13:59:00+02:00',),('2026-10-15T12:00:01+00:00',),('2026-10-14T00:00:00Z',)])
        since=NOW-timedelta(hours=1)
        self.assertEqual(len(_learning_rows(self.root,since,NOW)),1)
        self.assertEqual(len(_shadow_rows(self.root,since.isoformat(),NOW.isoformat())),1)

    def test_slow_loop_confirmation_resets_only_on_invalid_signal_or_long_gap(self):
        gate=EntryConfirmation()
        self.assertFalse(gate.ready('MSFT','trend',True,0))
        self.assertFalse(gate.ready('MSFT','trend',True,6))
        self.assertFalse(gate.ready('MSFT','trend',True,12))
        self.assertTrue(gate.ready('MSFT','trend',True,18))
        self.assertFalse(gate.ready('MSFT','trend',False,19))
        self.assertFalse(gate.ready('MSFT','trend',True,20))
        self.assertFalse(gate.ready('MSFT','trend',True,300))

    def test_unknown_buy_survives_restart_and_signal_reset(self):
        guard=ExecutionGuard(self.temp.name)
        guard.block_entry('STOCK','MSFT','entry_submitting','cid')
        guard=ExecutionGuard(self.temp.name)
        guard.rearm_entry('STOCK','MSFT')
        self.assertTrue(guard.entry_blocked('STOCK','MSFT'))
        api=Mock()
        api.get_order_by_client_order_id.side_effect=TimeoutError
        for n in (100,120,180):
            with patch('execution_guard.time.time',return_value=NOW.timestamp()+n):
                guard.reconcile_entries(api,[])
        self.assertTrue(guard.entry_blocked('STOCK','MSFT'))
        guard.reconcile_entries(api,[{'id':'broker-order','client_order_id':'cid'}])
        self.assertEqual(guard.state['entry_latches']['STOCK:MSFT']['reason'],'entry_submitted')

    def test_absent_buy_requires_three_successful_probes_and_grace(self):
        guard=ExecutionGuard(self.temp.name)
        guard.block_entry('STOCK','MSFT','entry_submitting','cid')
        row=guard.state['entry_latches']['STOCK:MSFT']
        row['blocked_at']=NOW.isoformat()
        api=Mock(); api.get_order_by_client_order_id.return_value=None
        for n in (0,11,22):
            with patch('execution_guard.time.time',return_value=NOW.timestamp()+n): guard.reconcile_entries(api,[])
        self.assertTrue(guard.entry_blocked('STOCK','MSFT'))
        with patch('execution_guard.time.time',return_value=NOW.timestamp()+61): guard.reconcile_entries(api,[])
        self.assertFalse(guard.entry_blocked('STOCK','MSFT'))

    def test_one_share_survives_only_when_reduced_caps_allow_it(self):
        engine=ProfitEngine(self.temp.name)
        args=dict(market='STOCK',symbol='MSFT',price=100,closes=[100]*60,spread_bps=1,
                  intelligence={'score':65,'entry_threshold':60,'global_risk':75},strategy='trend',
                  profile='active',account_equity=100000,max_stock_notional_usd=1000)
        plan=engine.plan(**args)
        self.assertEqual(plan.qty,1)
        args['max_stock_notional_usd']=150
        self.assertEqual(engine.plan(**args).qty,0)
        args['max_stock_notional_usd']=1000
        args['stock_risk_pct_equity']=0.000001
        self.assertEqual(engine.plan(**args).qty,0)

    def test_crypto_user_edge_threshold_is_enforced(self):
        engine=ProfitEngine(self.temp.name)
        args=dict(market='CRYPTO',symbol='BTC/USD',price=100,closes=[100]*60,spread_bps=0,
                  intelligence={'score':85,'entry_threshold':60},strategy='trend',profile='active',
                  account_equity=100000,crypto_taker_fee_bps=0)
        self.assertNotIn('negative_ev',engine.plan(**args,crypto_min_net_edge_bps=0).reason)
        self.assertIn('negative_ev',engine.plan(**args,crypto_min_net_edge_bps=1000).reason)

class FakePostgres:
    def __init__(self): self.rows={}
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,args=None):
        if sql.startswith('SELECT'): return SimpleNamespace(fetchall=lambda:[(n,b,d) for (ns,n),(b,d) in self.rows.items() if ns==args[0]])
        if sql.startswith('INSERT'):
            ns,n,b,d=args; self.rows[(ns,n)]=(b,d)
        return self

class BackupTests(unittest.TestCase):
    def test_real_sqlite_checkpoint_restore_without_secrets_or_control_state(self):
        pg=FakePostgres()
        with tempfile.TemporaryDirectory() as source,tempfile.TemporaryDirectory() as dest:
            with closing(sqlite3.connect(str(Path(source)/FILES[0]))) as db,db:
                db.execute('CREATE TABLE proof(value TEXT)'); db.execute("INSERT INTO proof VALUES('retained')")
            (Path(source)/'control.json').write_text('{"armed":true,"secret":"never-backup"}')
            backup=StateBackup(source,connect=lambda:pg); backup.restore(); backup.checkpoint()
            self.assertTrue(backup.status['last_success_utc'])
            self.assertEqual(len(pg.rows),1)
            restored=StateBackup(dest,connect=lambda:pg); restored.restore()
            self.assertTrue(restored.status['restored'])
            self.assertFalse((Path(dest)/'control.json').exists())
            with closing(sqlite3.connect(str(Path(dest)/FILES[0]))) as db,db:
                self.assertEqual(db.execute('SELECT value FROM proof').fetchone()[0],'retained')
                db.execute("UPDATE proof SET value='newer'")
            restored.restore()
            with closing(sqlite3.connect(str(Path(dest)/FILES[0]))) as db:
                self.assertEqual(db.execute('SELECT value FROM proof').fetchone()[0],'newer')

    def test_corrupt_checkpoint_fails_closed_and_does_not_install_database(self):
        pg=FakePostgres()
        with tempfile.TemporaryDirectory() as dest:
            backup=StateBackup(dest,connect=lambda:pg)
            pg.rows[(backup.namespace,FILES[0])]=(b'invalid','wrong-digest')
            backup.restore()
            self.assertFalse(backup.status['restored'])
            self.assertFalse((Path(dest)/FILES[0]).exists())
            backup.checkpoint()
            self.assertIsNone(backup.status['last_success_utc'])

if __name__=='__main__': unittest.main(verbosity=2)
