import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from state_storage import StateBackup
from trading_bot import merge_fresh_stream_quotes


class MarketDataTests(unittest.TestCase):
    def test_fresh_stream_quote_replaces_stale_rest(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(seconds=120)).isoformat()
        new = (now - timedelta(seconds=1)).isoformat()
        intel = SimpleNamespace(lock=threading.RLock(), live_quotes={
            'BTCUSD': {'bid': 100.0, 'ask': 100.2, 'ts': new},
        })
        merged = merge_fresh_stream_quotes(
            {'BTC/USD': {'bp': 90.0, 'ap': 91.0, 't': old}},
            ['BTC/USD'], intel, 30,
        )
        self.assertEqual(merged['BTC/USD']['bp'], 100.0)

    def test_old_stream_quote_is_not_marked_fresh(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        intel = SimpleNamespace(lock=threading.RLock(), live_quotes={
            'BTCUSD': {'bid': 100.0, 'ask': 100.2, 'ts': old},
        })
        self.assertEqual(
            merge_fresh_stream_quotes({}, ['BTC/USD'], intel, 30), {},
        )


class SnapshotTests(unittest.TestCase):
    def test_local_snapshot_is_consistent_and_contains_no_control_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / 'learning_v3.sqlite') as db:
                db.execute('CREATE TABLE checks (id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO checks VALUES (1)')
            (root / 'control.json').write_text('{"armed":true}')
            backup = StateBackup(root)
            backup.local_snapshot()
            files = list((root / 'evidence_snapshots').glob('*.gz'))
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].name.startswith('learning_v3.sqlite'))
            self.assertTrue(__import__('gzip').decompress(files[0].read_bytes()).startswith(b'SQLite format 3'))
            self.assertFalse(any('control' in p.name for p in files))




class CryptoStreamHandshakeTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self, incoming):
            self.incoming=list(incoming)
            self.sent=[]
        def recv(self):
            return self.incoming.pop(0)
        def send(self, payload):
            self.sent.append(__import__('json').loads(payload))
        def settimeout(self, timeout):
            pass

    def test_authenticated_and_acknowledged_before_connected(self):
        from intelligence_engine import IntelligenceEngine
        engine=object.__new__(IntelligenceEngine)
        engine.cfg=SimpleNamespace(key='fake-paper-key',secret='fake-paper-secret')
        ws=self.FakeSocket([
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","quotes":["BTC/USD"],"bars":["BTC/USD"]}]',
        ])
        count=engine._crypto_stream_handshake(ws,['BTC/USD'])
        self.assertEqual(count,1)
        self.assertEqual(ws.sent[0]['action'],'auth')
        self.assertEqual(ws.sent[1]['action'],'subscribe')

    def test_rejected_auth_never_subscribes(self):
        from intelligence_engine import IntelligenceEngine
        engine=object.__new__(IntelligenceEngine)
        engine.cfg=SimpleNamespace(key='fake-paper-key',secret='fake-paper-secret')
        ws=self.FakeSocket([
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"error","msg":"authentication failed"}]',
        ])
        with self.assertRaises(PermissionError):
            engine._crypto_stream_handshake(ws,['BTC/USD'])
        self.assertEqual(len(ws.sent),1)

    def test_empty_quote_ack_is_not_connected(self):
        from intelligence_engine import IntelligenceEngine
        engine=object.__new__(IntelligenceEngine)
        engine.cfg=SimpleNamespace(key='fake-paper-key',secret='fake-paper-secret')
        ws=self.FakeSocket([
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            '[{"T":"subscription","quotes":[],"bars":["BTC/USD"]}]',
        ])
        with self.assertRaises(RuntimeError):
            engine._crypto_stream_handshake(ws,['BTC/USD'])


if __name__ == '__main__':
    unittest.main()
