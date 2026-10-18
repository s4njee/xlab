"""xlab tests: python3 -m unittest discover -s tests  (standard library only)."""
import http.server
import json
import os
import pathlib
import random
import shutil
import sys
import tempfile
import threading
import tomllib
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from xlab import ai, analyze, config, corpora, insight, plan, runs, spec, stats, trees, util  # noqa: E402
from xlab.runner import Runner  # noqa: E402

SIM = """
id = "{id}"
adapter = "simulated"
primary = "time_s"
control = "base"
kind = "{kind}"
blocks = {blocks}
delta = 0.03
pareto = [["throughput", "quality"]]
[options]
sd = 0.03
void_rate = {void_rate}
[factors]
scale = [1, 2]
[arms.base]
quality = 50
[arms.faster]
effect = 0.85
quality = 40
[arms.same]
quality = 60
"""


def sim(id='S', kind='screening', blocks=8, void_rate=0.0):
    return SIM.format(id=id, kind=kind, blocks=blocks, void_rate=void_rate)


class LabCase(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix='xlab-test-'))
        (self.dir / 'lab.toml').write_text('[lab]\nname = "t"\n[conductor]\nevery_s = 0.1\n')
        (self.dir / 'experiments').mkdir()
        (self.dir / 'trees').mkdir()
        self.lab = config.Lab(self.dir)
        self.store = self.lab.store()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text)
        return path

    def work(self):
        Runner(self.lab, self.store, log=lambda t: None).work_until_empty()


class Stats(unittest.TestCase):
    def test_t_quantile(self):
        self.assertAlmostEqual(stats.t_quantile(0.975, 10), 2.228139, places=5)
        self.assertAlmostEqual(stats.t_quantile(0.975, 1), 12.7062, places=3)
        self.assertAlmostEqual(stats.t_quantile(0.05, 30), -1.697261, places=5)

    def test_hodges_lehmann(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        est, lo, hi = stats.hodges_lehmann(values)
        self.assertAlmostEqual(est, 4.5)
        self.assertLess(lo, est)
        self.assertGreater(hi, est)
        self.assertEqual(stats.hodges_lehmann([1.0, 2.0])[1], None)   # too few for a 95% interval

    def test_sign_flip(self):
        self.assertAlmostEqual(stats.sign_flip_p([1.0] * 6), 2 / 64)
        self.assertEqual(stats.sign_flip_p([1.0, -1.0]), 1.0)

    def test_holm(self):
        self.assertEqual(stats.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])

    def test_classify(self):
        self.assertEqual(stats.classify(0.8, 0.9, 0.05, 'lower'), 'better')
        self.assertEqual(stats.classify(0.8, 0.9, 0.05, 'higher'), 'worse')
        self.assertEqual(stats.classify(0.97, 1.02, 0.05, 'lower'), 'equivalent')
        self.assertEqual(stats.classify(0.9, 1.1, 0.05, 'lower'), 'inconclusive')

    def test_pareto(self):
        pts = [(1, 1), (2, 0.5), (0.5, 0.5), (2, 1)]
        self.assertEqual(stats.pareto_front(pts, 'higher', 'higher'), [3])
        self.assertEqual(sorted(stats.pareto_front(pts, 'lower', 'higher')), [0, 2])

    def test_coverage(self):
        """About 95% of t intervals around a known mean should cover it."""
        rng, cover = random.Random(3), 0
        for _ in range(400):
            values = [rng.gauss(1.0, 0.2) for _ in range(8)]
            _, lo, hi = stats.t_interval(values)
            cover += lo <= 1.0 <= hi
        self.assertGreater(cover / 400, 0.91)
        self.assertLess(cover / 400, 0.99)


class Specs(unittest.TestCase):
    def test_grid_and_cells(self):
        s = spec.load(text=sim().replace('[arms.same]\nquality = 60', '[[grid]]\nname = "g-{k}"\nk = [1, 2]'))
        self.assertIn('g-1', s['arms'])
        self.assertEqual([c['key'] for c in s['cells']], ['scale=1', 'scale=2'])

    def test_errors(self):
        with self.assertRaises(spec.SpecError):
            spec.load(text=sim().replace('control = "base"', 'control = "nope"'))
        from xlab import adapters
        with self.assertRaises(spec.SpecError):
            spec.load(text=sim().replace('primary = "time_s"', 'primary = "nope"'), adapters=adapters.load_all())

    def test_plan_is_reproducible(self):
        s = spec.load(text=sim())
        a, b = plan.make(s, 'r', 42), plan.make(s, 'r', 42)
        self.assertEqual(a['sequence'], b['sequence'])
        self.assertNotEqual(a['sequence'], plan.make(s, 'r', 43)['sequence'])
        for step in a['sequence']:
            for order in step['orders']:
                self.assertEqual(sorted(order), sorted(s['arms']))

    def test_toml_writer_round_trip(self):
        s = spec.load(text=sim())
        again = spec.load(text=util.to_toml(spec.to_raw(s)))
        self.assertEqual(again['arms'], s['arms'])
        self.assertEqual(again['cells'], s['cells'])


class Running(LabCase):
    def test_finds_known_effect(self):
        self.write('experiments/s.toml', sim(blocks=10, void_rate=0.05))
        run_id = runs.enqueue(self.lab, self.store, 'experiments/s.toml', seed=5)
        self.work()
        self.assertEqual(self.store.run(run_id)['state'], 'complete')
        result = analyze.analyse(self.lab, self.store, 'S')
        by = {(e['arm'], e['cell']): e for e in result['effects'] if e['primary']}
        for cell in ('scale=1', 'scale=2'):
            self.assertEqual(by[('faster', cell)]['verdict'], 'better')
            self.assertLess(by[('faster', cell)]['low'], 0.85 * 1.04)
            self.assertNotEqual(by[('same', cell)]['verdict'], 'better')
        records = util.read_jsonl(self.lab.runs / run_id / 'samples.jsonl')
        self.assertEqual(len(records), sum(self.store.counts(run_id).values()))
        self.assertTrue(all(r['spec_sha256'] and r['fingerprint'] for r in records))

    def test_resume_after_interruption(self):
        self.write('experiments/s.toml', sim(blocks=4))
        run_id = runs.enqueue(self.lab, self.store, 'experiments/s.toml', seed=1)
        runner = Runner(self.lab, self.store, log=lambda t: None)
        original = runner._sample
        count = {'n': 0}

        def flaky(*a, **k):
            count['n'] += 1
            if count['n'] == 7:
                runner.stop.set()
            return original(*a, **k)
        runner._sample = flaky
        runner.work_until_empty()
        self.assertEqual(self.store.run(run_id)['state'], 'queued')
        self.work()
        self.assertEqual(self.store.run(run_id)['state'], 'complete')
        result = analyze.analyse(self.lab, self.store, 'S')
        self.assertEqual(result['blocks'], 8)

    def test_all_void_fails_run(self):
        self.write('experiments/s.toml', sim(blocks=3, void_rate=1.0))
        run_id = runs.enqueue(self.lab, self.store, 'experiments/s.toml')
        self.work()
        self.assertEqual(self.store.run(run_id)['state'], 'failed')

    def test_cancel_queued(self):
        self.write('experiments/s.toml', sim())
        run_id = runs.enqueue(self.lab, self.store, 'experiments/s.toml')
        runs.cancel(self.lab, self.store, run_id)
        self.assertEqual(self.store.run(run_id)['state'], 'cancelled')


TREE = """
id = "T"
start = ["screen"]
[nodes.screen]
spec = "../experiments/s.toml"
  [[nodes.screen.branch]]
  when = { verdict = "better", all = true }
  next = ["confirm"]
  label = "won"
  [[nodes.screen.branch]]
  otherwise = true
  next = []
[nodes.confirm]
promote = "screen"
id = "S-confirm"
blocks = 8
"""


class Trees(LabCase):
    def test_tree_promotes_winners(self):
        self.write('experiments/s.toml', sim(blocks=8))
        self.write('trees/t.toml', TREE)
        conductor = trees.Conductor(self.lab, self.store)
        conductor.start('trees/t.toml')
        for _ in range(4):
            self.work()
            conductor.tick()
        view = conductor.view('T')
        nodes = {n['id']: n for n in view['nodes']}
        self.assertTrue(view['done'])
        self.assertEqual(nodes['screen']['decision']['arms'], ['faster'])
        confirm = runs.load_run_spec(self.lab, nodes['confirm']['runs'][0]['id'])
        self.assertEqual(sorted(confirm['arms']), ['base', 'faster'])
        self.assertEqual(confirm['kind'], 'confirmatory')

    def test_rule_semantics(self):
        result = {'primary': 'm', 'pareto': [], 'absolutes': [], 'effects': [
            {'arm': 'a', 'metric': 'm', 'factors': {'c': 'x'}, 'verdict': 'better', 'ratio': 0.8, 'better': 'lower'},
            {'arm': 'a', 'metric': 'm', 'factors': {'c': 'y'}, 'verdict': 'worse', 'ratio': 1.2, 'better': 'lower'},
            {'arm': 'b', 'metric': 'm', 'factors': {'c': 'x'}, 'verdict': 'better', 'ratio': 0.9, 'better': 'lower'},
            {'arm': 'b', 'metric': 'm', 'factors': {'c': 'y'}, 'verdict': 'better', 'ratio': 0.9, 'better': 'lower'}]}
        self.assertEqual(trees.evaluate({'verdict': 'better'}, result)[1], ['a', 'b'])
        self.assertEqual(trees.evaluate({'verdict': 'better', 'all': True}, result)[1], ['b'])
        self.assertFalse(trees.evaluate({'verdict': 'better', 'all': True, 'all_arms': True}, result)[0])
        self.assertEqual(trees.evaluate({'speedup_at_least': 1.2}, result)[1], ['a'])
        self.assertEqual(trees.evaluate({'verdict': 'better', 'factors': {'c': 'y'}}, result)[1], ['b'])

    def test_invalid_tree(self):
        self.write('experiments/s.toml', sim())
        self.write('trees/bad.toml', TREE.replace('next = ["confirm"]', 'next = ["missing"]'))
        with self.assertRaises(trees.TreeError):
            trees.load(self.lab, 'trees/bad.toml')


MANUAL = """
id = "M"
adapter = "manual"
primary = "yield_g"
control = "a"
blocks = 3
[metrics.yield_g]
unit = "g"
better = "higher"
[arms.a]
[arms.b]
"""


class Manual(LabCase):
    def test_record_and_void(self):
        self.write('experiments/m.toml', MANUAL)
        run_id = runs.enqueue(self.lab, self.store, 'experiments/m.toml', seed=2)
        self.work()
        self.assertEqual(self.store.run(run_id)['state'], 'awaiting')
        tasks = runs.tasks(self.store, run_id)
        self.assertEqual(len(tasks), 6)
        with self.assertRaises(runs.RunError):
            runs.record_manual(self.lab, self.store, run_id, tasks[0]['unit'], {'nope': 1})
        runs.record_manual(self.lab, self.store, run_id, tasks[0]['unit'], void_reason='dropped it')
        tasks = runs.tasks(self.store, run_id)
        self.assertEqual(len(tasks), 6)            # the voided block was planned again
        self.assertTrue(any(t['attempt'] == 1 for t in tasks))
        for t in tasks:
            runs.record_manual(self.lab, self.store, run_id, t['unit'], {'yield_g': 10 if t['arm'] == 'a' else 12})
        self.assertEqual(self.store.run(run_id)['state'], 'complete')
        result = analyze.analyse(self.lab, self.store, 'M')
        self.assertEqual(result['blocks'], 3)
        self.assertAlmostEqual(result['effects'][0]['ratio'], 1.2)


class Corpora(LabCase):
    def test_deterministic(self):
        a, ma = corpora.ensure(self.lab, 'logs-64k', log=lambda t: None)
        digest = ma['sha256']
        a.unlink()
        (self.lab.corpora / 'logs-64k.json').unlink()
        _, mb = corpora.ensure(self.lab, 'logs-64k', log=lambda t: None)
        self.assertEqual(digest, mb['sha256'])
        self.assertEqual(mb['bytes'], 65536)
        with self.assertRaises(corpora.CorpusError):
            corpora.ensure(self.lab, 'x', {'kind': 'text', 'size': '1KiB', 'sha256': '00'}, log=lambda t: None)


class Dashboard(LabCase):
    def test_api(self):
        from xlab.dashboard.server import make_server
        self.write('experiments/m.toml', MANUAL)
        run_id = runs.enqueue(self.lab, self.store, 'experiments/m.toml')
        self.work()
        server = make_server(self.lab, '127.0.0.1', 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        try:
            state = json.loads(urllib.request.urlopen(base + '/api/state').read())
            self.assertEqual(len(state['tasks']), 6)
            self.assertIn('yield_g', state['task_metrics'][run_id])
            page = urllib.request.urlopen(base + '/').read()
            self.assertIn(b'charts.js', page)
            unit = state['tasks'][0]['unit']
            body = json.dumps({'run': run_id, 'unit': unit, 'metrics': {'yield_g': 5}}).encode()
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(urllib.request.Request(base + '/api/record', body,
                                                              {'content-type': 'application/json'}))
            self.assertEqual(caught.exception.code, 403)
            reply = urllib.request.urlopen(urllib.request.Request(
                base + '/api/record', body, {'content-type': 'application/json', 'x-xlab': '1'}))
            self.assertEqual(json.loads(reply.read())['state'], 'valid')
        finally:
            server.shutdown()


class AI(LabCase):
    def test_insight_with_fake_provider(self):
        seen = {}

        class Fake(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['content-length'])))
                seen['key'], seen['body'] = self.headers.get('x-api-key'), body
                evidence = json.loads(body['messages'][0]['content'])
                exp = next(iter(evidence['experiments']))
                answer = {'overview': 'ok', 'findings': [{'title': 't', 'finding': 'f', 'evidence': 'e', 'caveat': 'c',
                                                          'experiments': [exp], 'runs': []}],
                          'anomalies': [], 'next_experiments': [], 'limitations': []}
                data = json.dumps({'model': body['model'], 'stop_reason': 'end_turn',
                                   'content': [{'type': 'text', 'text': json.dumps(answer)}]}).encode()
                self.send_response(200)
                self.send_header('content-length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Fake)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.write('experiments/s.toml', sim(blocks=2))
        runs.enqueue(self.lab, self.store, 'experiments/s.toml')
        self.work()
        self.lab.config['ai'].update(provider='anthropic', base_url=f'http://127.0.0.1:{server.server_address[1]}/',
                                     key_file=str(self.dir / 'keys.json'))
        (self.dir / 'keys.json').write_text('{"anthropic": "k"}')
        end = util.now()
        os.environ.pop('ANTHROPIC_API_KEY', None)   # the key must come from key_file here
        doc = insight.generate(self.lab, self.store, util.utc(end.replace(year=end.year - 1)), util.utc(end))
        server.shutdown()
        self.assertEqual(doc['data']['findings'][0]['experiments'], ['S'])
        self.assertEqual(seen['body']['model'], 'claude-opus-5-5')
        self.assertEqual(seen['body']['output_config']['format']['type'], 'json_schema')
        self.assertEqual(seen['key'], 'k')
        with self.assertRaises(ai.AIError):
            insight.validate({'overview': 'x', 'findings': [{'experiments': ['nope'], 'runs': []}]},
                             {'experiments': {'S': {'runs': []}}, 'runs': []})


class Standing(LabCase):
    def test_schedule_and_drift(self):
        from xlab import standing
        self.write('experiments/s.toml', sim(id='S0', blocks=4))
        standing.add(self.lab, self.store, 'experiments/s.toml', every_hours=6)
        first = standing.queue_due(self.lab, self.store)
        self.assertEqual(len(first), 1)
        self.assertEqual(standing.queue_due(self.lab, self.store), [])        # not due again yet
        self.assertEqual(self.store.run(first[0])['lane'], 'standing')
        self.work()
        later = util.now() + __import__('datetime').timedelta(hours=7)
        second = standing.queue_due(self.lab, self.store, now=later)
        self.assertEqual(len(second), 1)
        self.assertEqual(self.store.queue()[0]['id'], second[0])              # at the front
        self.work()
        rows = standing.summary(self.lab, self.store)[0]['runs']
        self.assertEqual(len(rows), 2)
        self.assertEqual({e['arm'] for e in rows[0]['cells'][0]['effects']}, {'faster', 'same'})
        # drift: a steady series, then a 30% jump
        steady = [{'run': f'r{i}', 'delta': 0.03, 'cells': [{'cell': 'c', 'control_gmean': 1.0 + 0.01 * (i % 3)}]}
                  for i in range(6)]
        self.assertFalse(standing.drift(steady)[0]['flagged'])
        jump = steady + [{'run': 'r9', 'delta': 0.03, 'cells': [{'cell': 'c', 'control_gmean': 1.3}]}]
        self.assertTrue(standing.drift(jump)[0]['flagged'])

    def test_runner_queues_standing_first(self):
        from xlab import standing
        self.write('experiments/s.toml', sim(blocks=2))
        self.write('experiments/z.toml', sim(id='S0', blocks=2))
        runs.enqueue(self.lab, self.store, 'experiments/s.toml')
        standing.add(self.lab, self.store, 'experiments/z.toml', every_hours=1)
        runner = Runner(self.lab, self.store, log=lambda t: None)
        runner.step()
        done = [r for r in self.store.runs() if r['state'] == 'complete']
        self.assertEqual([r['experiment'] for r in done], ['S0'])


class Preflight(LabCase):
    def test_compression_catches_bad_arm_before_sampling(self):
        if not shutil.which('zstd'):
            self.skipTest('zstd is not installed')
        self.write('experiments/c.toml', """
id = "C"
adapter = "compression"
primary = "compress_s"
control = "good"
blocks = 2
[factors]
corpus = ["text-64k"]
[arms.good]
codec = "zstd"
level = 3
[arms.bad]
codec = "zstd"
level = 3
extra = ["--no-such-flag"]
""")
        run_id = runs.enqueue(self.lab, self.store, 'experiments/c.toml')
        self.work()
        run = self.store.run(run_id)
        self.assertEqual(run['state'], 'failed')
        self.assertIn('arm bad', run['detail'])
        self.assertEqual(self.store.counts(run_id), {})                     # no sample was taken
        self.assertTrue((self.lab.runs / run_id / 'preflight.json').exists())

    def test_command_template_error(self):
        from xlab import adapters
        from xlab.adapters import Context
        s = spec.load(text="""
id = "K"
adapter = "command"
primary = "wall_s"
control = "a"
[factors]
n = [1]
[arms.a]
command = "echo {cell.missing}"
[arms.b]
command = "echo {cell.n}"
""", adapters=adapters.load_all())
        ctx = Context(self.lab, 'p', s, self.dir, self.dir, lambda t: None)
        problems = adapters.load_all()['command'].preflight(ctx)
        self.assertEqual(len(problems), 1)
        self.assertIn('arm a', problems[0])


class Selftest(unittest.TestCase):
    def test_calibration(self):
        from xlab import selftest
        result = selftest.run(experiments=24)
        checks = {c['name']: c for c in result['checks']}
        self.assertTrue(checks['false verdicts on the identical arm']['ok'])
        self.assertGreater(checks['t interval coverage']['value'], 0.85)
        self.assertGreater(checks['power for a 10% effect']['value'], 0.5)
        self.assertEqual(selftest.run(experiments=4)['arms'], selftest.run(experiments=4)['arms'])   # reproducible


class Examples(LabCase):
    def test_examples_validate(self):
        from xlab import adapters
        found = 0
        root = pathlib.Path(__file__).resolve().parents[1] / 'xlab' / 'examples'
        for path in root.glob('*/experiments/*.toml'):
            spec.load(path, adapters=adapters.load_all())
            found += 1
        for path in root.glob('*/trees/*.toml'):
            tree = tomllib.loads(path.read_text())
            tree['_path'] = str(path)
            trees.validate(self.lab, tree, adapters.load_all())
            found += 1
        self.assertGreaterEqual(found, 8)


if __name__ == '__main__':
    unittest.main()
