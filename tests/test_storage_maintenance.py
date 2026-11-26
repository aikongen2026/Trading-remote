"""Legacy duplicate snapshots must not permanently prevent PAPER entries."""
import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from storage_maintenance import archive_legacy_edge_snapshots


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'alpaca_edge_v49.sqlite'
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript('''
                CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,kind TEXT NOT NULL,symbol TEXT,payload TEXT NOT NULL);
                CREATE INDEX idx_edge_events_kind_ts ON events(kind,ts);
                CREATE TABLE news(article_id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE overnight_shadow(ts TEXT,symbol TEXT,payload TEXT,PRIMARY KEY(ts,symbol));
            ''')
            for i in range(500):
                conn.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                             ('2026-11-26', 'DECISION', 'BTCUSD', json.dumps({'event_type':'SNAPSHOT','action':'WAIT_INTEL','context':'abc'*1000,'n':i})))
            for payload in ({'event_type':'ENTRY','action':'BUY','order_id':'entry-1'},
                            {'event_type':'EXIT','action':'SELL'},
                            {'event_type':'SNAPSHOT','order_id':'keep-1'},
                            {'event_type':'SNAPSHOT','action':'BUY'},
                            {'event_type':'SNAPSHOT','action':'WAIT','client_order_id':'keep-2'}):
                conn.execute('INSERT INTO events(ts,kind,symbol,payload) VALUES(?,?,?,?)',
                             ('2026-11-26', 'DECISION', 'AEP', json.dumps(payload)))
            conn.execute("INSERT INTO events(ts,kind,payload) VALUES('x','TRADE_UPDATE','broker-data')")
            conn.execute("INSERT INTO events(ts,kind,payload) VALUES('x','DECISION','invalid-json')")
            conn.execute("INSERT INTO news VALUES('a','news')")
            conn.execute("INSERT INTO overnight_shadow VALUES('x','SPY','shadow')")
        self.original = self.rows()
        self.other = self.root / 'broker_history.sqlite'
        self.other.write_bytes(b'untouched-broker-history')
        (self.root/'control.json').write_text('{"armed":true,"paused":false}')

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT * FROM events ORDER BY id').fetchall()

    def test_lossless_archive_keeps_executions_news_indexes_and_sequence(self):
        result = archive_legacy_edge_snapshots(self.root)
        self.assertEqual(result['state'], 'archived')
        self.assertEqual(result['archived_rows'], 500)
        self.assertGreater(result['reclaimed_mb'], 1)
        self.assertEqual(self.rows(), self.original[500:])
        archive = self.root / 'edge_snapshot_archives' / result['archive']
        raw = gzip.decompress(archive.read_bytes())
        moved = [tuple(json.loads(line)['row']) for line in raw.splitlines()]
        self.assertEqual(moved, self.original[:500])
        manifest = json.loads(Path(str(archive)+'.json').read_text())
        self.assertEqual(manifest['sha256_uncompressed'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(manifest['rows'], 500)
        self.assertEqual(self.other.read_bytes(), b'untouched-broker-history')
        self.assertEqual((self.root/'control.json').read_text(), '{"armed":true,"paused":false}')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(conn.execute('SELECT * FROM news').fetchall(), [('a','news')])
            self.assertEqual(conn.execute('SELECT * FROM overnight_shadow').fetchall(), [('x','SPY','shadow')])
            self.assertIn('idx_edge_events_kind_ts', [r[1] for r in conn.execute('PRAGMA index_list(events)')])
            conn.execute("INSERT INTO events(ts,kind,payload) VALUES('x','EDGE_START','{}')")
            self.assertEqual(conn.execute('SELECT MAX(id) FROM events').fetchone()[0], 508)
        self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'not_needed')
        self.assertEqual(len(list(archive.parent.glob('*.gz'))), 1)

    def test_low_space_and_failed_verification_leave_original_rows(self):
        free = shutil.disk_usage(self.root)
        with patch('storage_maintenance.shutil.disk_usage', return_value=type(free)(free.total,free.used,16*1024*1024)):
            self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'deferred')
        self.assertEqual(self.rows(), self.original)
        with patch('storage_maintenance._archive_digest', return_value=('bad', 0)):
            self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'deferred')
        self.assertEqual(self.rows(), self.original)
        self.assertFalse(list(self.root.glob('.edge-archive-*')))

    def test_unrecognized_schema_refuses_replacement(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE future_evidence(id INTEGER PRIMARY KEY, payload TEXT)')
        self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'deferred')
        self.assertEqual(self.rows(), self.original)

    def test_busy_wal_writer_refuses_replacement(self):
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("INSERT INTO events(ts,kind,payload) VALUES('x','EDGE_START','{}')")
            self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'deferred')
            writer.rollback()
        self.assertEqual(self.rows(), self.original)

    def test_archived_highest_id_is_never_reused(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO events(id,ts,kind,payload) VALUES(5000,'x','DECISION',?)",
                         (json.dumps({'event_type':'SNAPSHOT','action':'WAIT'}),))
        self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'archived')
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO events(ts,kind,payload) VALUES('x','EDGE_START','{}')")
            self.assertEqual(conn.execute('SELECT MAX(id) FROM events').fetchone()[0], 5001)

    def test_legacy_wait_snapshots_are_archived_but_unknown_and_buy_are_retained(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for action in ('WAIT_INTEL:60', 'HOLD', 'BUY', 'UNKNOWN_LEGACY'):
                conn.execute("INSERT INTO events(ts,kind,payload) VALUES('x','DECISION',?)",
                             (json.dumps({'action':action}),))
        result = archive_legacy_edge_snapshots(self.root)
        self.assertEqual(result['state'], 'archived')
        self.assertEqual(result['archived_rows'], 502)
        self.assertEqual(len(self.rows()), 9)
        self.assertEqual([json.loads(row[-1])['action'] for row in self.rows()[-2:]], ['BUY','UNKNOWN_LEGACY'])

    def test_session_observations_move_losslessly_and_broker_updates_stay(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT INTO events(ts,kind,payload) VALUES('x',?,?)",
                             [('SESSION_CLOCK_V3','clock-observation'),('SESSION_CALENDAR_V3','calendar-observation')])
        expected = self.rows()
        result = archive_legacy_edge_snapshots(self.root)
        self.assertEqual(result['state'], 'archived')
        self.assertEqual(result['archived_rows'], 502)
        self.assertEqual(self.rows(), self.original[500:])
        with gzip.open(self.root/'edge_snapshot_archives'/result['archive'], 'rt') as stream:
            archived = [tuple(json.loads(line)['row']) for line in stream]
        self.assertEqual(archived, expected[:500] + expected[-2:])

    def test_old_overnight_rows_are_archived_with_latest_per_symbol_retained(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany('INSERT INTO overnight_shadow VALUES(?,?,?)',
                             [('2026-11-24','BTCUSD','old-shadow'),('2026-11-26','BTCUSD','latest-shadow')])
        result = archive_legacy_edge_snapshots(self.root)
        self.assertEqual(result['state'], 'archived')
        self.assertEqual(result['archived_tables']['overnight_shadow'], 1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT ts,payload FROM overnight_shadow WHERE symbol='BTCUSD'").fetchall(),
                             [('2026-11-26','latest-shadow')])
        archive = self.root/'edge_snapshot_archives'/result['archive']
        with gzip.open(archive, 'rt') as stream:
            rows = [json.loads(line) for line in stream]
        self.assertEqual([r['row'] for r in rows if r['table']=='overnight_shadow'],
                         [['2026-11-24','BTCUSD','old-shadow']])
        manifest = json.loads(Path(str(archive)+'.json').read_text())
        self.assertEqual(manifest['columns_by_table']['overnight_shadow'], ['ts','symbol','payload'])

    def test_interrupted_publication_keeps_original_and_reuses_archive(self):
        import storage_maintenance
        replace = storage_maintenance.os.replace
        def fail_database(src, dst):
            if Path(dst) == self.db:
                raise OSError('simulated interruption')
            return replace(src, dst)
        with patch('storage_maintenance.os.replace', side_effect=fail_database):
            self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'deferred')
        self.assertEqual(self.rows(), self.original)
        self.assertEqual(archive_legacy_edge_snapshots(self.root)['state'], 'archived')
        self.assertEqual(len(list((self.root/'edge_snapshot_archives').glob('*.gz'))), 1)

    def test_startup_order_precedes_every_writer(self):
        source = Path(__file__).resolve().parents[1].joinpath('cloud_app.py').read_text()
        main = source[source.index('def main()'):]
        self.assertLess(main.index('backup.restore()'), main.index('archive_legacy_edge_snapshots(trading_bot.STATE_DIR)'))
        for start in ('initialise_state()', 'ALPACA_EDGE.start()', 'SAXO_SIM.start()', 'start_engine()', 'backup.start()'):
            self.assertLess(main.index('archive_legacy_edge_snapshots(trading_bot.STATE_DIR)'), main.index(start))


if __name__ == '__main__':
    unittest.main()
