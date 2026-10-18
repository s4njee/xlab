"""The lab index in SQLite (WAL), shared by the runner, the conductor and the dashboard.

The run directory's samples.jsonl is the durable scientific record; every sample is
written there (fsynced) and here in the same step. The index exists so the runner,
trees, reports and dashboard can query without re-reading every file.
"""
import json
import sqlite3
import threading
import time

from . import util

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, experiment TEXT NOT NULL, kind TEXT, adapter TEXT, automatic INTEGER,
  state TEXT NOT NULL, priority REAL NOT NULL DEFAULT 0, created TEXT, started TEXT, ended TEXT,
  detail TEXT, tree TEXT, node TEXT, spec_sha256 TEXT, seed INTEGER, planned INTEGER,
  question TEXT, attempts INTEGER NOT NULL DEFAULT 0, lane TEXT);
CREATE INDEX IF NOT EXISTS runs_state ON runs(state, priority, created);
CREATE INDEX IF NOT EXISTS runs_experiment ON runs(experiment);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, unit TEXT NOT NULL, block INTEGER, cell TEXT, arm TEXT,
  attempt INTEGER, position INTEGER, state TEXT NOT NULL, started TEXT, ended TEXT, metrics TEXT,
  void_reason TEXT, record TEXT, UNIQUE(run_id, unit));
CREATE INDEX IF NOT EXISTS samples_run ON samples(run_id, state);
CREATE INDEX IF NOT EXISTS samples_ended ON samples(ended);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, utc TEXT NOT NULL, kind TEXT NOT NULL, run_id TEXT, text TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS trees (
  id TEXT PRIMARY KEY, path TEXT, active INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL, updated TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, start TEXT, end TEXT, generated TEXT, path TEXT,
  data TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

TERMINAL = ('complete', 'failed', 'cancelled')


def _row(row):
    return dict(row) if row is not None else None


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=30000')
        self.db.executescript(SCHEMA)
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(runs)')}
        if 'lane' not in columns:      # labs made before standing runs existed
            self.db.execute('ALTER TABLE runs ADD COLUMN lane TEXT')

    # -- plumbing ---------------------------------------------------------------
    def q(self, sql, *args):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def one(self, sql, *args):
        with self.lock:
            return _row(self.db.execute(sql, args).fetchone())

    def write(self, sql, *args):
        with self.lock:
            for attempt in range(5):
                try:
                    self.db.execute('BEGIN IMMEDIATE')
                    try:
                        cursor = self.db.execute(sql, args)
                        self.db.execute("INSERT INTO kv(key, value) VALUES('version', '1') ON CONFLICT(key) "
                                        "DO UPDATE SET value = CAST(value AS INTEGER) + 1")
                        self.db.execute('COMMIT')
                        return cursor
                    except BaseException:
                        self.db.execute('ROLLBACK')
                        raise
                except sqlite3.OperationalError as error:
                    if 'locked' not in str(error) or attempt == 4:
                        raise
                    time.sleep(0.2 * (attempt + 1))

    def version(self):
        row = self.one("SELECT value FROM kv WHERE key = 'version'")
        return int(row['value']) if row else 0

    # -- key/value control --------------------------------------------------------
    def get(self, key, default=None):
        row = self.one('SELECT value FROM kv WHERE key = ?', key)
        return json.loads(row['value']) if row else default

    def put(self, key, value):
        self.write('INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                   key, json.dumps(value, default=str))

    # -- events -----------------------------------------------------------------------
    def event(self, kind, text, run_id=None, **data):
        self.write('INSERT INTO events(utc, kind, run_id, text, data) VALUES(?, ?, ?, ?, ?)',
                   util.utc(), kind, run_id, text, json.dumps(data, default=str) if data else None)

    def events(self, limit=100, since=None):
        if since:
            rows = self.q('SELECT * FROM events WHERE utc >= ? ORDER BY id DESC LIMIT ?', since, limit)
        else:
            rows = self.q('SELECT * FROM events ORDER BY id DESC LIMIT ?', limit)
        for r in rows:
            r['data'] = json.loads(r['data']) if r['data'] else None
        return rows

    # -- runs --------------------------------------------------------------------------
    def add_run(self, run):
        self.write('INSERT INTO runs(id, experiment, kind, adapter, automatic, state, priority, created, tree, node, '
                   'spec_sha256, seed, planned, question, lane) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   run['id'], run['experiment'], run['kind'], run['adapter'], int(run['automatic']), run['state'],
                   run.get('priority', 0.0), util.utc(), run.get('tree'), run.get('node'), run['spec_sha256'],
                   run['seed'], run['planned'], run.get('question'), run.get('lane'))

    def run(self, run_id):
        return self.one('SELECT * FROM runs WHERE id = ?', run_id)

    def runs(self, state=None, experiment=None, limit=200):
        sql, args = 'SELECT * FROM runs WHERE 1=1', []
        if state:
            states = [state] if isinstance(state, str) else list(state)
            sql += f' AND state IN ({",".join("?" * len(states))})'
            args += states
        if experiment:
            sql += ' AND experiment = ?'
            args.append(experiment)
        sql += ' ORDER BY created DESC LIMIT ?'
        return self.q(sql, *args, limit)

    def queue(self):
        return self.q("SELECT * FROM runs WHERE state = 'queued' ORDER BY priority, created")

    def front_priority(self):
        row = self.one("SELECT MIN(priority) AS p FROM runs WHERE state = 'queued'")
        return (row['p'] if row and row['p'] is not None else 0.0) - 1.0

    def set_run(self, run_id, **fields):
        if not fields:
            return
        names = ', '.join(f'{k} = ?' for k in fields)
        self.write(f'UPDATE runs SET {names} WHERE id = ?', *fields.values(), run_id)

    # -- samples ------------------------------------------------------------------------
    def add_sample(self, record):
        self.write('INSERT INTO samples(run_id, unit, block, cell, arm, attempt, position, state, started, ended, '
                   'metrics, void_reason, record) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id, unit) DO UPDATE '
                   'SET state = excluded.state, started = excluded.started, ended = excluded.ended, '
                   'metrics = excluded.metrics, void_reason = excluded.void_reason, record = excluded.record',
                   record['run'], record['unit'], record['block'], record['cell'], record['arm'], record['attempt'],
                   record['position'], record['state'], record.get('started_utc'), record.get('ended_utc'),
                   json.dumps(record.get('metrics') or {}), record.get('void_reason'), json.dumps(record, default=str))

    def samples(self, run_id=None, state=None, since=None, until=None, experiment=None, limit=None):
        sql = 'SELECT s.* FROM samples s'
        args, where = [], []
        if experiment:
            sql += ' JOIN runs r ON r.id = s.run_id'
            where.append('r.experiment = ?')
            args.append(experiment)
        if run_id:
            where.append('s.run_id = ?')
            args.append(run_id)
        if state:
            states = [state] if isinstance(state, str) else list(state)
            where.append(f's.state IN ({",".join("?" * len(states))})')
            args += states
        if since:
            where.append('s.ended >= ?')
            args.append(since)
        if until:
            where.append('s.ended < ?')
            args.append(until)
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY s.id'
        if limit:
            sql = f'SELECT * FROM ({sql.replace(" ORDER BY s.id", " ORDER BY s.id DESC")} LIMIT {int(limit)}) ORDER BY id'
        rows = self.q(sql, *args)
        for r in rows:
            r['metrics'] = json.loads(r['metrics']) if r['metrics'] else {}
        return rows

    def sample(self, run_id, unit):
        row = self.one('SELECT * FROM samples WHERE run_id = ? AND unit = ?', run_id, unit)
        if row:
            row['metrics'] = json.loads(row['metrics']) if row['metrics'] else {}
            row['record'] = json.loads(row['record']) if row['record'] else {}
        return row

    def counts(self, run_id):
        rows = self.q('SELECT state, COUNT(*) AS n FROM samples WHERE run_id = ? GROUP BY state', run_id)
        return {r['state']: r['n'] for r in rows}

    # -- trees --------------------------------------------------------------------------
    def tree(self, tree_id):
        row = self.one('SELECT * FROM trees WHERE id = ?', tree_id)
        if row:
            row['state'] = json.loads(row['state'])
        return row

    def trees(self):
        rows = self.q('SELECT * FROM trees ORDER BY updated DESC')
        for r in rows:
            r['state'] = json.loads(r['state'])
        return rows

    def save_tree(self, tree_id, path, state, active=True):
        self.write('INSERT INTO trees(id, path, active, state, updated) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE '
                   'SET path = excluded.path, active = excluded.active, state = excluded.state, updated = excluded.updated',
                   tree_id, str(path), int(active), json.dumps(state, default=str), util.utc())

    # -- documents (reports and insights) ---------------------------------------------
    def add_document(self, doc_id, kind, start, end, path=None, data=None, error=None):
        self.write('INSERT INTO documents(id, kind, start, end, generated, path, data, error) VALUES(?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET path = excluded.path, data = excluded.data, error = excluded.error, '
                   'generated = excluded.generated', doc_id, kind, start, end, util.utc(), str(path) if path else None,
                   json.dumps(data, default=str) if data is not None else None, error)

    def documents(self, kind, limit=100):
        rows = self.q('SELECT * FROM documents WHERE kind = ? ORDER BY end DESC LIMIT ?', kind, limit)
        for r in rows:
            r['data'] = json.loads(r['data']) if r['data'] else None
        return rows

    def document(self, doc_id):
        row = self.one('SELECT * FROM documents WHERE id = ?', doc_id)
        if row:
            row['data'] = json.loads(row['data']) if row['data'] else None
        return row
