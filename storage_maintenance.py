"""Offline, lossless archival of legacy duplicate Edge decision snapshots.

Call only at startup, after restore and before ANY database writer starts.
The source is never edited: build and verify a smaller SQLite replacement and
a gzip JSONL archive first. Broker history, learning, orders and credentials
are untouched. A failed/space-limited attempt leaves the original in place.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

MIB = 1024 * 1024
RESERVE = 32 * MIB


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _encoded(row):
    return (json.dumps(list(row), ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


def _snapshot(row, columns):
    item = dict(zip(columns, row))
    if item.get('kind') in ('SESSION_CLOCK_V3', 'SESSION_CALENDAR_V3'):
        return True  # immutable observations, never execution instructions
    if item.get('kind') != 'DECISION':
        return False
    try:
        payload = json.loads(item.get('payload') or '')
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get('event_type') not in (None, 'SNAPSHOT'):
        return False
    # Retain even redundant snapshots that mention execution evidence.
    if any(payload.get(k) for k in ('order_id', 'client_order_id', 'entry_client_order_id', 'group_id')):
        return False
    action = str(payload.get('action') or '').upper()
    if action.startswith(('BUY', 'SELL', 'EXIT')):
        return False
    if payload.get('event_type') is None:
        # Pre-event_type journals used explicit WAIT/HOLD actions for repeated
        # observations. Unknown legacy records remain in SQLite.
        return action.startswith(('WAIT', 'HOLD', 'NO_SIGNAL', 'SKIP_', 'TEST_MODE',
                                  'RENDER_USAGE_GUARD', 'STORAGE_BUDGET_GUARD', 'MARKET_CLOSED'))
    return True


def _space(root):
    if shutil.disk_usage(root).free < RESERVE:
        raise OSError('maintenance_reserve')


def _sync_file(path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _archive_digest(path):
    digest, count = hashlib.sha256(), 0
    with gzip.open(path, 'rb') as stream:
        for line in stream:
            json.loads(line)
            digest.update(line)
            count += 1
    return digest.hexdigest(), count


def archive_legacy_edge_snapshots(state_dir):
    """Return safe size/count diagnostics; never disable the disk guard."""
    root = Path(state_dir)
    source = root / 'alpaca_edge_v49.sqlite'
    result = {'state': 'not_needed', 'archived_rows': 0, 'reclaimed_mb': 0.0}
    if not source.is_file():
        return result
    try:
        _space(root)
        before = source.stat().st_size
        with closing(sqlite3.connect(str(source), timeout=3)) as original:
            # No running readers/writers are allowed during replacement. SQLite
            # checks this transition; refuse if a connection is using WAL.
            if original.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
                raise RuntimeError('database_not_offline')
            original.execute('BEGIN EXCLUSIVE')
            schema = original.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid").fetchall()
            tables = [name for kind, name, _ in schema if kind == 'table']
            if set(tables) != {'events', 'news', 'overnight_shadow'} or any(k not in {'table', 'index'} for k, _, _ in schema):
                raise ValueError('unrecognized_edge_schema')
            columns = [r[1] for r in original.execute('PRAGMA table_info(events)')]
            if not {'id', 'ts', 'kind', 'symbol', 'payload'}.issubset(columns):
                raise ValueError('unrecognized_event_columns')
            inventory = {kind: {'rows': count, 'payload_mb': round(size / MIB, 2)}
                         for kind, count, size in original.execute('SELECT kind,COUNT(*),COALESCE(SUM(LENGTH(payload)),0) FROM events GROUP BY kind')}
            print('EDGE_STORAGE_INVENTORY ' + json.dumps(inventory, sort_keys=True), flush=True)
            latest_shadow = dict(original.execute('SELECT symbol,MAX(ts) FROM overnight_shadow GROUP BY symbol'))
            # A cheap first scan avoids rewriting the database on later boots.
            found = any(_snapshot(row, columns) for row in original.execute("SELECT * FROM events WHERE kind IN ('DECISION','SESSION_CLOCK_V3','SESSION_CALENDAR_V3')"))
            shadow_count = original.execute('SELECT COUNT(*) FROM overnight_shadow').fetchone()[0]
            if not found and shadow_count <= len(latest_shadow):
                return result
            with tempfile.TemporaryDirectory(prefix='.edge-archive-', dir=root) as work:
                staged = Path(work) / 'retained.sqlite'
                packed = Path(work) / 'snapshots.jsonl.gz'
                expected, archived, archive_hash = {}, 0, hashlib.sha256()
                archive_columns, archive_counts = {}, {}
                with closing(sqlite3.connect(str(staged))) as target, gzip.open(packed, 'wb', compresslevel=6) as archive:
                    target.execute('PRAGMA journal_mode=DELETE')
                    target.execute('PRAGMA synchronous=FULL')
                    for kind, _, sql in schema:
                        if kind == 'table':
                            target.execute(sql)
                    for table in tables:
                        result['stage'] = 'copy_' + table
                        rows, digest = 0, hashlib.sha256()
                        cols = [r[1] for r in original.execute('PRAGMA table_info(' + _quote(table) + ')')]
                        archive_columns[table] = cols
                        insert = 'INSERT INTO ' + _quote(table) + ' VALUES(' + ','.join('?' for _ in cols) + ')'
                        for index, row in enumerate(original.execute('SELECT * FROM ' + _quote(table) + ' ORDER BY rowid')):
                            if index % 128 == 0:
                                target.commit()
                                archive.flush()
                                _space(root)
                            encoded = _encoded(row)
                            shadow = dict(zip(cols, row)) if table == 'overnight_shadow' else {}
                            cold_shadow = bool(shadow) and shadow.get('ts') != latest_shadow.get(shadow.get('symbol'))
                            if (table == 'events' and _snapshot(row, cols)) or cold_shadow:
                                record = (json.dumps({'table':table, 'row':list(row)}, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
                                archive.write(record)
                                archive_hash.update(record)
                                archived += 1
                                archive_counts[table] = archive_counts.get(table, 0) + 1
                                result['archived_rows'] = archived
                            else:
                                target.execute(insert, row)
                                digest.update(encoded)
                                rows += 1
                                result['retained_rows_so_far'] = rows
                        expected[table] = (digest.hexdigest(), rows)
                    # Preserve AUTOINCREMENT watermarks, including archived IDs.
                    target.execute('DELETE FROM sqlite_sequence')
                    for row in original.execute('SELECT name,seq FROM sqlite_sequence'):
                        target.execute('INSERT INTO sqlite_sequence VALUES(?,?)', row)
                    target.commit()
                    for kind, _, sql in schema:
                        if kind == 'index':
                            _space(root)
                            target.execute(sql)
                    target.commit()
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('replacement_integrity')
                    for table, want in expected.items():
                        digest, count = hashlib.sha256(), 0
                        for row in target.execute('SELECT * FROM ' + _quote(table) + ' ORDER BY rowid'):
                            digest.update(_encoded(row))
                            count += 1
                        if (digest.hexdigest(), count) != want:
                            raise ValueError('retained_rows_mismatch')
                if _archive_digest(packed) != (archive_hash.hexdigest(), archived):
                    raise ValueError('archive_verification')
                _space(root)
                if staged.stat().st_size + packed.stat().st_size >= before:
                    return {**result, 'state': 'no_space_gain'}
                archives = root / 'edge_snapshot_archives'
                archives.mkdir(exist_ok=True)
                name = 'snapshots-' + archive_hash.hexdigest()[:24] + '.jsonl.gz'
                final_archive = archives / name
                _sync_file(staged)
                _sync_file(packed)
                if final_archive.exists():
                    if _archive_digest(final_archive) != (archive_hash.hexdigest(), archived):
                        raise ValueError('existing_archive_mismatch')
                else:
                    os.replace(packed, final_archive)
                manifest = {'format': 'edge-observation-rows-v2', 'columns': columns,
                            'columns_by_table': archive_columns, 'rows_by_table': archive_counts,
                            'rows': archived, 'sha256_uncompressed': archive_hash.hexdigest(),
                            'created_at': datetime.now(timezone.utc).isoformat(),
                            'source': source.name, 'retained_tables': {k: v[1] for k, v in expected.items()}}
                pending = archives / (name + '.json.tmp')
                pending.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
                _sync_file(pending)
                os.replace(pending, archives / (name + '.json'))
                # Archive and manifest must be durable before removing the old
                # SQLite inode. The original remains locked until replacement.
                fd = os.open(archives, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                after = staged.stat().st_size
                os.replace(staged, source)
                result.update(state='archived', archived_rows=archived,
                              archived_tables=archive_counts,
                              archive=name, retained_events=expected['events'][1],
                              reclaimed_mb=round((before - after - final_archive.stat().st_size) / MIB, 2))
        fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, sqlite3.Error, ValueError, TypeError, RuntimeError) as exc:
        result.update(state='deferred', error=type(exc).__name__)
        if isinstance(exc, OSError):
            result['space_reserve_reached'] = str(exc) == 'maintenance_reserve'
            result['errno'] = exc.errno
    # Only numbers/status are logged, never payloads or account credentials.
    print('EDGE_STORAGE_MAINTENANCE ' + json.dumps(result, sort_keys=True), flush=True)
    return result
