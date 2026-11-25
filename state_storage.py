"""Mounted-disk selection and optional PostgreSQL checkpoints of audit databases.

Only evidence databases are backed up. Secrets and trading START/STOP/exit intents
are never restored from a stale backup. Broker reconciliation owns live state.
"""
from __future__ import annotations
import gzip
import shutil
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from datetime import timedelta

FILES=('learning_v3.sqlite','runtime_audit.sqlite','broker_history.sqlite')
MAX_BYTES=64*1024*1024

def resolve_state_dir(here):
    configured=(os.getenv('TRADINGBOT_STATE_DIR') or '').strip()
    render_mode=(os.getenv('RENDER') or '').strip().lower() in {'1','true','yes','on'}
    persistent_root=Path('/var/data')
    # Render persistent disks are mounted at /var/data, but os.path.ismount()
    # is not reliable in every container/runtime. Prefer the known disk path
    # whenever it exists on Render so journals survive deploys/restarts.
    if configured:
        target=Path(configured)
    elif render_mode and persistent_root.exists():
        target=persistent_root/'tradingbot'
    elif os.path.ismount('/var/data'):
        target=persistent_root/'tradingbot'
    else:
        target=Path(here)
    target.mkdir(parents=True,exist_ok=True)
    return target

def health(state_dir):
    p=Path(state_dir)/'storage_backup_health.json'
    try: return json.loads(p.read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return {'configured':bool(os.getenv('TRADINGBOT_DATABASE_URL')), 'restored':False,
                'last_success_utc':None,'last_error':'','checkpoint_interval_sec':300}

class StateBackup:
    def __init__(self,state_dir,connect=None):
        self.state=Path(state_dir)
        self.url=os.getenv('TRADINGBOT_DATABASE_URL','')
        mode='paper' if os.getenv('ALPACA_PAPER','true').lower() in {'true','1','yes'} else 'live'
        self.namespace=os.getenv('TRADINGBOT_BACKUP_NAMESPACE',os.getenv('RENDER_SERVICE_ID','tradingbot'))+':'+mode
        self.connect_override=connect
        self.status={'configured':bool(self.url or connect),'restored':False,'last_success_utc':None,
                     'last_error':'','checkpoint_interval_sec':300}
        self.stop=threading.Event()

    def _save_health(self):
        path=self.state/'storage_backup_health.json'
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.status),encoding='utf-8'); os.replace(tmp,path)

    def _connect(self):
        if self.connect_override: return self.connect_override()
        import psycopg
        return psycopg.connect(self.url,connect_timeout=10,options='-c statement_timeout=15000')

    def _schema(self,db):
        db.execute('CREATE TABLE IF NOT EXISTS tradingbot_state_backup(namespace TEXT,name TEXT,payload BYTEA,digest TEXT,updated_at TIMESTAMPTZ DEFAULT now(),PRIMARY KEY(namespace,name))')

    def restore(self):
        if not self.status['configured']:
            self.status['restored']=True; self._save_health(); return
        try:
            with self._connect() as db:
                self._schema(db)
                rows=db.execute('SELECT name,payload,digest FROM tradingbot_state_backup WHERE namespace=%s',(self.namespace,)).fetchall()
            for name,blob,digest in rows:
                if name not in FILES: raise ValueError('Unexpected backup filename')
                target=self.state/name
                if target.exists(): continue  # A running local database is never overwritten.
                with gzip.GzipFile(fileobj=__import__('io').BytesIO(bytes(blob))) as compressed:
                    raw=compressed.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES or hashlib.sha256(raw).hexdigest()!=digest: raise ValueError('Invalid checkpoint')
                tmp=target.with_suffix('.restore')
                try:
                    tmp.write_bytes(raw)
                    with closing(sqlite3.connect(str(tmp))) as check:
                        if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError('Invalid SQLite checkpoint')
                    os.replace(tmp,target)
                finally:
                    if tmp.exists(): tmp.unlink()
            self.status.update(restored=True,last_error='')
        except Exception as exc:
            # Do not include connection URLs/credentials in status or exports.
            self.status.update(restored=False,last_error=type(exc).__name__)
        self._save_health()

    def checkpoint(self):
        if not self.status['configured'] or not self.status['restored']: return
        try:
            snapshots=[]
            with tempfile.TemporaryDirectory(dir=self.state) as td:
                for name in FILES:
                    path=self.state/name
                    if not path.exists(): continue
                    target=Path(td)/name
                    with closing(sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True,timeout=5)) as src, closing(sqlite3.connect(str(target))) as dst:
                        src.backup(dst)
                    if target.stat().st_size>MAX_BYTES: raise ValueError('Checkpoint size limit')
                    raw=target.read_bytes()
                    snapshots.append((name,gzip.compress(raw,compresslevel=1),hashlib.sha256(raw).hexdigest()))
            with self._connect() as db:
                self._schema(db)
                for name,blob,digest in snapshots:
                    db.execute('INSERT INTO tradingbot_state_backup(namespace,name,payload,digest) VALUES(%s,%s,%s,%s) ON CONFLICT(namespace,name) DO UPDATE SET payload=EXCLUDED.payload,digest=EXCLUDED.digest,updated_at=now()',
                               (self.namespace,name,blob,digest))
            self.status.update(last_success_utc=datetime.now(timezone.utc).isoformat(),last_error='')
        except Exception as exc: self.status['last_error']=type(exc).__name__
        self._save_health()

    def local_snapshot(self):
        """Keep rotating verified SQLite evidence snapshots on Render's mounted disk.

        These are local recovery copies, NOT an offsite disaster-recovery backup.
        Live orders, credentials and execution intents are never snapshotted.
        """
        if (os.getenv('RENDER') or '').lower() in {'true','1','yes'} and not self.state.resolve().is_relative_to(Path('/var/data').resolve()):
            self.status['local_snapshot_error']='not_persistent_mount'
            self._save_health()
            return
        target_dir=self.state/'evidence_snapshots'
        target_dir.mkdir(parents=True, exist_ok=True)
        now=datetime.now(timezone.utc)
        marker=target_dir/'last_success.json'
        try:
            last=json.loads(marker.read_text(encoding='utf-8'))
            stamp=datetime.fromisoformat(last.get('timestamp',''))
            if (now-stamp).total_seconds()<3600:
                return
        except (OSError,ValueError,TypeError):
            pass
        try:
            names=[]
            stamp=now.strftime('%Y%m%dT%H%M%SZ')
            for name in FILES:
                path=self.state/name
                if not path.is_file():
                    continue
                from resource_policy import disk_budget
                budget = disk_budget(self.state, refresh=True)
                # Keep old verified snapshots when a new staging copy would
                # consume the disk reserve. Do not resize or delete evidence.
                if path.stat().st_size > MAX_BYTES or budget['remaining_mb'] * 1024 * 1024 < path.stat().st_size + 8 * 1024 * 1024:
                    raise ValueError('Snapshot storage budget')
                with tempfile.TemporaryDirectory(dir=target_dir) as tmpdir:
                    staged=Path(tmpdir)/name
                    with closing(sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True,timeout=5)) as src, closing(sqlite3.connect(str(staged))) as dst:
                        src.backup(dst)
                    if staged.stat().st_size>MAX_BYTES:
                        raise ValueError('Snapshot size limit')
                    with closing(sqlite3.connect(str(staged))) as check:
                        if check.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                            raise ValueError('Snapshot integrity failed')
                    out=target_dir/(name+'.'+stamp+'.gz')
                    pending=out.with_suffix(out.suffix+'.tmp')
                    with staged.open('rb') as source, gzip.open(pending, 'wb', compresslevel=1) as dest:
                        shutil.copyfileobj(source, dest, length=256*1024)
                    os.replace(pending,out)
                    names.append(out.name)
            if not names:
                self.status['local_snapshot_error']='no_database_files'
            else:
                marker_tmp=marker.with_suffix('.tmp')
                marker_tmp.write_text(json.dumps({'timestamp':now.isoformat(),'files':names}),encoding='utf-8')
                os.replace(marker_tmp,marker)
                self.status.update(local_snapshot_utc=now.isoformat(),local_snapshot_error='')
                # Six snapshots per database, max ~six hours of local recovery.
                for name in FILES:
                    old=sorted(target_dir.glob(name+'.*.gz'))
                    for item in old[:-6]:
                        item.unlink(missing_ok=True)
        except Exception as exc:
            self.status['local_snapshot_error']=type(exc).__name__
        self._save_health()

    def start(self):
        def run():
            while not self.stop.is_set():
                # Restore is a startup operation. Never overwrite a database after
                # the engine has opened it; a failed restore requires a restart.
                self.checkpoint()
                self.local_snapshot()
                self.stop.wait(300)
        threading.Thread(target=run,name='audit-checkpoints',daemon=True).start()
