"""The dashboard's HTTP server: static page, JSON API and a server-sent event stream.

It binds to loopback by default. Every write (recording a manual result, pausing the
runner, cancelling a run, starting a tree) needs a JSON body and the `X-XLab: 1`
header, which a cross-site form cannot send.

    GET  /                      the page
    GET  /api/state             runner, queue, recent runs, trees, tasks, events, reports
    GET  /api/run/RUN           one run: plan, every sample, its own analysis
    GET  /api/experiment/ID     an experiment's analysis over its runs
    GET  /api/report/ID         a stored report (HTML)
    GET  /api/stream            `version` events whenever the lab's data changes
    POST /api/record            {run, unit, metrics | void_reason, note}
    POST /api/control           {action: pause | resume | cancel, run}
    POST /api/enqueue           {spec: "experiments/X.toml", front: false}
    POST /api/tree/start        {path: "trees/T.toml"}
"""
import http.server
import json
import mimetypes
import pathlib
import threading
import time
import urllib.parse
from importlib import resources

from .. import adapters as adapters_mod
from .. import analyze, runs, standing, util
from ..trees import Conductor

STATIC = resources.files('xlab.dashboard') / 'static'


class App:
    def __init__(self, lab, adapters=None):
        self.lab = lab
        self.store = lab.store()
        self.adapters = adapters or adapters_mod.load_all(lab)
        self.conductor = Conductor(lab, self.store, self.adapters)
        self.cache = {}
        self.lock = threading.Lock()

    def cached(self, key, make):
        version = self.store.version()
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit[0] == version:
                return hit[1]
        value = make()
        with self.lock:
            self.cache[key] = (version, value)
            if len(self.cache) > 200:
                self.cache.clear()
        return value

    # -- views -----------------------------------------------------------------------
    def run_summary(self, r):
        c = self.store.counts(r['id'])
        return {**r, 'valid': c.get('valid', 0), 'void': c.get('void', 0), 'awaiting': c.get('awaiting', 0),
                'warmup': c.get('warmup', 0)}

    def state(self):
        store = self.store
        runner = store.get('runner') or {'state': 'not running'}
        if runner.get('heartbeat'):
            age = (util.now() - util.parse_utc(runner['heartbeat'])).total_seconds()
            runner['age_s'] = age
            if age > max(120, self.lab.section('runner')['poll_s'] * 6) and runner['state'] not in ('stopped',):
                runner['state_reported'], runner['state'] = runner['state'], 'stale'
        current = None
        running = store.runs(state='running', limit=1)
        if running:
            r = running[0]
            plan = runs.load_plan(self.lab, r['id'])
            samples = store.samples(r['id'], limit=400)
            current = {'run': self.run_summary(r), 'primary': plan['primary'], 'control': plan['control'],
                       'arms': plan['arms'], 'metrics': plan.get('metrics', {}),
                       'cells': plan['cells'], 'blocks': len(plan['sequence']),
                       'samples': [{k: s[k] for k in ('unit', 'block', 'cell', 'arm', 'attempt', 'state', 'ended',
                                                     'void_reason')} | {'value': s['metrics'].get(plan['primary'])}
                                   for s in samples],
                       'analysis': self.cached(('run', r['id']), lambda: self._run_analysis(r['id']))}
        experiments = store.q('SELECT experiment, MAX(created) AS last, COUNT(*) AS runs FROM runs '
                              'GROUP BY experiment ORDER BY last DESC')
        trees = [v for v in (self.conductor.view(t['id']) for t in store.trees()) if v]
        reports = [{k: d[k] for k in ('id', 'start', 'end', 'generated', 'data')} for d in store.documents('report', 50)]
        # standing summaries analyse every sentinel run: recompute only when one finishes
        sig = store.one("SELECT COUNT(*) AS n, MAX(ended) AS last FROM runs WHERE lane = 'standing' AND state = 'complete'")
        key = ('standing', sig['n'], sig['last'], json.dumps(store.get('standing', {}), sort_keys=True))
        with self.lock:
            sentinels = self.cache.get(key)
        if sentinels is None:
            sentinels = standing.summary(self.lab, store, self.adapters)
            with self.lock:
                self.cache[key] = sentinels
        insights = store.documents('insight', 1)
        tasks = runs.tasks(store, limit=100)
        task_metrics = {r: runs.load_plan(self.lab, r).get('metrics', {}) for r in {t['run_id'] for t in tasks}}
        return {
            'version': store.version(), 'now': util.utc(), 'lab': {'name': self.lab.name, 'root': str(self.lab.root)},
            'runner': runner, 'paused': bool(store.get('paused')), 'halted_until': store.get('halted_until'),
            'queue': [self.run_summary(r) for r in store.queue()],
            'runs': [self.run_summary(r) for r in store.runs(limit=40)],
            'current': current, 'trees': trees, 'standing': sentinels, 'tasks': tasks, 'task_metrics': task_metrics,
            'events': store.events(120), 'reports': reports,
            'insight': insights[0] if insights else None, 'experiments': experiments,
            'specs': sorted(str(p.relative_to(self.lab.root)) for p in self.lab.experiments.rglob('*.toml'))
            if self.lab.experiments.exists() else [],
            'tree_files': sorted(str(p.relative_to(self.lab.root)) for p in self.lab.trees.rglob('*.toml'))
            if self.lab.trees.exists() else [],
            'ai': {'provider': self.lab.section('ai').get('provider'), 'every_hours': self.lab.section('ai')['every_hours']},
            'report_every_hours': self.lab.section('reports')['every_hours'],
        }

    def _run_analysis(self, run_id):
        try:
            return analyze.analyse(self.lab, self.store, run_ids=[run_id], adapters=self.adapters)
        except Exception as error:
            return {'error': str(error)}

    def run_detail(self, run_id):
        r = self.store.run(run_id)
        if r is None:
            return None
        plan = runs.load_plan(self.lab, run_id)
        samples = self.store.samples(run_id)
        env_path = runs.run_dir(self.lab, run_id) / 'env.json'
        return {'run': self.run_summary(r), 'plan': {k: plan[k] for k in plan if k != 'sequence'},
                'sequence_length': len(plan['sequence']), 'samples': samples,
                'env': json.loads(env_path.read_text()) if env_path.exists() else None,
                'analysis': self.cached(('run', run_id), lambda: self._run_analysis(run_id))}

    def experiment(self, name):
        return self.cached(('experiment', name), lambda: analyze.analyse(self.lab, self.store, name,
                                                                          adapters=self.adapters))

    # -- writes ------------------------------------------------------------------------
    def post(self, path, body):
        if path == '/api/record':
            record = runs.record_manual(self.lab, self.store, body['run'], body['unit'], body.get('metrics') or {},
                                        body.get('void_reason') or None, body.get('note') or None,
                                        by=body.get('by') or 'dashboard', adapters=self.adapters)
            return {'ok': True, 'state': record['state']}
        if path == '/api/control':
            action = body.get('action')
            if action == 'pause':
                self.store.put('paused', True)
                self.store.event('control', 'runner paused from the dashboard')
            elif action == 'resume':
                self.store.put('paused', False)
                self.store.put('halted_until', None)
                self.store.event('control', 'runner resumed from the dashboard')
            elif action == 'cancel':
                runs.cancel(self.lab, self.store, body['run'], 'cancelled from the dashboard')
            else:
                raise ValueError(f'unknown action {action!r}')
            return {'ok': True}
        if path == '/api/enqueue':
            target = self.lab.resolve(body['spec']).resolve()
            if self.lab.root not in target.parents:
                raise ValueError('specs must be inside the lab')
            run_id = runs.enqueue(self.lab, self.store, str(target), front=bool(body.get('front')),
                                  adapters=self.adapters)
            return {'ok': True, 'run': run_id}
        if path == '/api/tree/start':
            target = self.lab.resolve(body['path']).resolve()
            if self.lab.root not in target.parents:
                raise ValueError('trees must be inside the lab')
            self.conductor.start(target, restart=bool(body.get('restart')))
            return {'ok': True}
        raise KeyError(path)


class Handler(http.server.BaseHTTPRequestHandler):
    app = None
    server_version = 'xlab'

    def log_message(self, *args):
        pass

    def send_json(self, value, status=200):
        data = json.dumps(value, default=str).encode()
        self.send_response(status)
        self.send_header('content-type', 'application/json')
        self.send_header('cache-control', 'no-store')
        self.send_header('content-length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, data, kind):
        self.send_response(200)
        self.send_header('content-type', kind)
        self.send_header('content-length', str(len(data)))
        self.send_header('cache-control', 'no-cache')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        app = self.app
        try:
            if path in ('/', '/index.html'):
                return self.send_bytes((STATIC / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            if path.startswith('/static/'):
                name = path[len('/static/'):]
                if '/' in name or name.startswith('.'):
                    return self.send_error(404)
                f = STATIC / name
                if not f.is_file():
                    return self.send_error(404)
                return self.send_bytes(f.read_bytes(), (mimetypes.guess_type(name)[0] or 'application/octet-stream')
                                       + ('; charset=utf-8' if name.endswith(('.js', '.css', '.html')) else ''))
            if path == '/api/state':
                return self.send_json(app.cached('state', app.state))
            if path.startswith('/api/run/'):
                detail = app.run_detail(path.rsplit('/', 1)[1])
                return self.send_json(detail) if detail else self.send_json({'error': 'no such run'}, 404)
            if path.startswith('/api/experiment/'):
                result = app.experiment(path.rsplit('/', 1)[1])
                return self.send_json(result) if result else self.send_json({'error': 'no runs'}, 404)
            if path.startswith('/api/report/'):
                doc = app.store.document(path.rsplit('/', 1)[1])
                if not doc or not doc['path'] or not pathlib.Path(doc['path']).exists():
                    return self.send_error(404)
                return self.send_bytes(pathlib.Path(doc['path']).read_bytes(), 'text/html; charset=utf-8')
            if path == '/api/stream':
                return self.stream()
        except Exception as error:
            return self.send_json({'error': f'{type(error).__name__}: {error}'}, 500)
        self.send_error(404)

    def stream(self):
        self.send_response(200)
        self.send_header('content-type', 'text/event-stream')
        self.send_header('cache-control', 'no-store')
        self.end_headers()
        last, quiet = None, 0
        try:
            while True:
                version = self.app.store.version()
                if version != last:
                    self.wfile.write(f'event: version\ndata: {version}\n\n'.encode())
                    self.wfile.flush()
                    last, quiet = version, 0
                else:
                    quiet += 1
                    if quiet >= 15:
                        self.wfile.write(b': keep-alive\n\n')
                        self.wfile.flush()
                        quiet = 0
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        if self.headers.get('x-xlab') != '1' or 'json' not in (self.headers.get('content-type') or ''):
            return self.send_json({'error': 'writes need a JSON body and the X-XLab: 1 header'}, 403)
        try:
            length = int(self.headers.get('content-length') or 0)
            body = json.loads(self.rfile.read(min(length, 1 << 20)) or b'{}')
            return self.send_json(self.app.post(urllib.parse.urlparse(self.path).path, body))
        except KeyError as missing:
            return self.send_json({'error': f'missing {missing}'}, 400)
        except Exception as error:
            return self.send_json({'error': str(error)}, 400)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(lab, host='127.0.0.1', port=8710, adapters=None):
    handler = type('BoundHandler', (Handler,), {'app': App(lab, adapters)})
    return Server((host, port), handler)
