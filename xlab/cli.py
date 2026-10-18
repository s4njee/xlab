"""xlab command line. Run `xlab --help` or `xlab COMMAND --help`."""
import argparse
import datetime
import json
import pathlib
import shutil
import sys
from importlib import resources

from . import __version__
from . import adapters as adapters_mod
from . import analyze, config, corpora, insight, plan, report, runs, spec as spec_mod, trees, util


def lab_of(args):
    return config.Lab(args.lab) if args.lab else config.find_lab()


def cmd_init(args):
    root = pathlib.Path(args.dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    examples = resources.files('xlab') / 'examples'
    chosen = ['simulated', 'compression', 'manual', 'command'] if args.example == 'all' else [args.example]
    if not (root / 'lab.toml').exists():
        (root / 'lab.toml').write_text((examples / 'lab.toml').read_text().replace('NAME', args.name or root.name))
    for name in chosen:
        source = examples / name
        for kind in ('experiments', 'trees'):
            folder = source / kind
            if not folder.is_dir():
                continue
            (root / kind).mkdir(exist_ok=True)
            for f in folder.iterdir():
                target = root / kind / f.name
                if not target.exists():
                    target.write_text(f.read_text())
    config.Lab(root)
    print(f'lab ready at {root}')
    print(f'  xlab --lab {root} up          # runner, trees, reports and the dashboard')
    return 0


def cmd_check(args):
    lab = lab_of(args)
    adapters = adapters_mod.load_all(lab)
    path = lab.resolve(args.file)
    text = path.read_text()
    if 'nodes' in text and '[nodes' in text:
        tree = trees.load(lab, path)
        trees.validate(lab, tree, adapters)
        print(f'tree {tree["id"]}: {len(tree["nodes"])} nodes, starts at {", ".join(tree["start"])} — valid')
    else:
        s = spec_mod.load(path, adapters=adapters)
        metrics = adapters[s['adapter']].metrics(s)
        print(f'{s["id"]}: {s["kind"]} on {s["adapter"]}, {len(s["arms"])} arms x {len(s["cells"])} cells x '
              f'{s["blocks"]} blocks = {len(s["arms"]) * len(s["cells"]) * s["blocks"]} samples — valid')
        print(f'  arms: {", ".join(s["arms"])}')
        print(f'  cells: {", ".join(c["key"] for c in s["cells"])}')
        print(f'  metrics: {", ".join(f"{k}{" *" if k == s["primary"] else ""}" for k in metrics)}')
    return 0


def cmd_plan(args):
    lab = lab_of(args)
    s = spec_mod.load(lab.resolve(args.file), adapters=adapters_mod.load_all(lab))
    print(plan.render(plan.make(s, 'preview', args.seed or s.get('seed') or 1), limit=args.limit))
    return 0


def cmd_enqueue(args):
    lab = lab_of(args)
    store = lab.store()
    for _ in range(args.count):
        run_id = runs.enqueue(lab, store, args.file, seed=args.seed, front=args.front)
        print(run_id)
    return 0


def cmd_queue(args):
    lab = lab_of(args)
    store = lab.store()
    runner = store.get('runner') or {}
    print(f'runner: {runner.get("state", "not running")}' + (f' — {runner["detail"]}' if runner.get('detail') else '')
          + (f' (heartbeat {runner["heartbeat"]})' if runner.get('heartbeat') else ''))
    if store.get('paused'):
        print('paused: the runner starts no new run until `xlab resume`')
    if store.get('halted_until'):
        print(f'halted until {store.get("halted_until")}')
    for title, rows in (('running', store.runs(state='running')), ('queued', store.queue()),
                        ('awaiting results', store.runs(state='awaiting'))):
        if rows:
            print(f'\n{title}:')
            for r in rows:
                c = store.counts(r['id'])
                print(f'  {r["id"]}  {c.get("valid", 0)}/{r["planned"]} valid, {c.get("void", 0)} void'
                      + (f', {c.get("awaiting")} to record' if c.get('awaiting') else '')
                      + (f'  [{r["tree"]}/{r["node"]}]' if r['tree'] else ''))
    recent = [r for r in store.runs(limit=10) if r['state'] in ('complete', 'failed', 'cancelled')]
    if recent:
        print('\nrecent:')
        for r in recent[:6]:
            print(f'  {r["id"]}  {r["state"]}' + (f': {r["detail"]}' if r['detail'] else ''))
    return 0


def cmd_work(args):
    from .runner import Runner
    from .daemon import take_lock
    lab = lab_of(args)
    lock = take_lock(lab, 'runner')
    try:
        Runner(lab).work_until_empty()
        from .trees import Conductor
        conductor = Conductor(lab, lab.store())
        while args.trees and conductor.tick():
            Runner(lab).work_until_empty()
    finally:
        lock.close()
    return 0


def cmd_up(args):
    from .daemon import up
    return up(lab_of(args), runner=not args.no_runner, dashboard=not args.no_dashboard, host=args.host, port=args.port)


def cmd_control(args):
    lab = lab_of(args)
    store = lab.store()
    if args.command == 'pause':
        store.put('paused', True)
        store.event('control', 'runner paused')
        print('paused: the current run finishes, then nothing new starts')
    elif args.command == 'resume':
        store.put('paused', False)
        store.put('halted_until', None)
        store.event('control', 'runner resumed')
        print('resumed')
    elif args.command == 'cancel':
        runs.cancel(lab, store, args.run)
        print(f'{args.run}: cancelled')
    return 0


def cmd_tasks(args):
    lab = lab_of(args)
    store = lab.store()
    rows = runs.tasks(store, args.run, limit=args.limit)
    if args.csv:
        import csv
        metrics = {}
        for r in rows:
            metrics.setdefault(r['run_id'], runs.load_plan(lab, r['run_id']).get('metrics', {}))
        names = sorted({m for v in metrics.values() for m in v})
        writer = csv.writer(sys.stdout)
        writer.writerow(['unit', 'position', 'block', 'arm', 'cell', 'instructions', *names, 'void_reason', 'note'])
        for r in rows:
            writer.writerow([r['unit'], r['position'] + 1, r['block'] + 1, r['arm'], r['cell'], r['instructions'] or '',
                             *([''] * len(names)), '', ''])
        return 0
    if not rows:
        print('no manual tasks are waiting')
    for r in rows:
        print(f'{r["run_id"]}  {r["unit"]}  #{r["position"] + 1}  {r["arm"]}  [{r["cell"]}]')
        if r['instructions']:
            print(f'    {r["instructions"]}')
    return 0


def cmd_record(args):
    lab = lab_of(args)
    metrics = {}
    for pair in args.values:
        if '=' not in pair:
            raise SystemExit(f'expected METRIC=VALUE, got {pair!r}')
        k, v = pair.split('=', 1)
        metrics[k] = v
    record = runs.record_manual(lab, lab.store(), args.run, args.unit, metrics, args.void, args.note, by='cli')
    print(f'{args.unit}: {record["state"]}')
    return 0


def cmd_import(args):
    lab = lab_of(args)
    print(f'recorded {runs.import_csv(lab, lab.store(), args.run, args.file)} samples')
    return 0


def cmd_analyze(args):
    lab = lab_of(args)
    result = analyze.analyse(lab, lab.store(), args.experiment, run_ids=args.run or None, control=args.control,
                             looks=args.looks)
    if result is None:
        print(f'no runs of {args.experiment}')
        return 1
    print(analyze.to_json(result) if args.json else analyze.render_text(result, args.metric))
    return 0


def cmd_tree(args):
    lab = lab_of(args)
    store = lab.store()
    conductor = trees.Conductor(lab, store)
    if args.action == 'start':
        state = conductor.start(args.target, restart=args.restart)
        print(f'tree {state["id"]} started: ' + ', '.join(f'{k} {v["state"]}' for k, v in state['nodes'].items()
                                                         if v['state'] != 'pending'))
    elif args.action == 'advance':
        print(f'{conductor.tick()} decision(s) made')
    elif args.action == 'status':
        for row in store.trees():
            if args.target and row['id'] != args.target:
                continue
            view = conductor.view(row['id'])
            print(f'{row["id"]}: {"done" if view.get("done") else "in progress"}' +
                  (f' — {view["question"]}' if view.get('question') else ''))
            for n in view.get('nodes', []):
                decision = (n['decision'] or {}).get('label')
                print(f'  {n["id"]:<16} {n["state"]:<10}' + (f' -> {decision}' if decision else '')
                      + ''.join(f'  [{r["id"]} {r["valid"]}/{r["planned"]}]' for r in n['runs']))
    elif args.action == 'draft':
        from . import generate
        if not args.adapter:
            raise SystemExit('--adapter is required for a draft')
        folder, errors = generate.draft(lab, args.target, args.adapter)
        print(f'draft written to {folder}')
        if errors:
            print('it still has problems:\n  ' + '\n  '.join(errors))
            return 1
        print(f'review it, then: xlab tree start {folder.relative_to(lab.root)}/tree.toml')
    return 0


def cmd_report(args):
    lab = lab_of(args)
    store = lab.store()
    end = util.now()
    hours = args.hours or lab.section('reports')['every_hours']
    path = report.generate(lab, store, util.utc(end - datetime.timedelta(hours=hours)), util.utc(end))
    print(path)
    return 0


def cmd_insight(args):
    lab = lab_of(args)
    store = lab.store()
    end = util.now()
    hours = args.hours or lab.section('ai')['every_hours']
    start = util.utc(end - datetime.timedelta(hours=hours))
    if args.evidence:
        print(json.dumps(insight.window_evidence(lab, store, start, util.utc(end)), indent=1, default=str))
        return 0
    doc = insight.generate(lab, store, start, util.utc(end))
    data = doc['data']
    print(data['overview'])
    for f in data['findings']:
        print(f'\n- {f["title"]}: {f["finding"]}\n  evidence: {f["evidence"]}\n  caveat: {f["caveat"]}')
    return 0


def cmd_corpus(args):
    lab = lab_of(args)
    if args.action == 'list':
        for f in sorted(lab.corpora.glob('*.json')):
            m = json.loads(f.read_text())
            print(f'{m["name"]:<20} {util.human_bytes(m["bytes"]):>10}  {m.get("kind", "tree")}  {m["sha256"][:16]}')
        return 0
    for name in args.names:
        path, manifest = corpora.ensure(lab, name)
        print(f'{name}: {path} {util.human_bytes(manifest["bytes"])} sha256 {manifest["sha256"][:16]}... '
              f'(zlib-3 ratio of first 4 MiB {corpora.sample_ratio(path):.2f})')
    return 0


def cmd_adapters(args):
    lab = lab_of(args)
    for name, adapter in sorted(adapters_mod.load_all(lab).items()):
        d = adapter.describe()
        print(f'{name:<12} {"automatic" if d["automatic"] else "manual   "}  {d["description"]}')
        for codec, info in d.get('codecs', {}).items():
            print(f'    {codec:<7} levels {info["levels"][0]}..{info["levels"][1]}'
                  f'{"  threads" if info["threads"] else ""}{"" if info["installed"] else "  (not installed)"}')
    return 0


def cmd_standing(args):
    from . import standing
    lab = lab_of(args)
    store = lab.store()
    if args.action == 'add':
        if not args.target or not args.every_hours:
            raise SystemExit('usage: xlab standing add SPEC --every-hours N')
        entry = standing.add(lab, store, args.target, args.every_hours)
        print(f'{entry["id"]}: a fresh run goes to the front of the queue every {entry["every_hours"]:g} h '
              f'(the first as soon as the runner is free)')
    elif args.action == 'remove':
        try:
            standing.remove(store, args.target)
        except KeyError:
            raise SystemExit(f'xlab: {args.target} is not a standing run') from None
        print(f'{args.target}: removed')
    else:
        rows = standing.summary(lab, store)
        if not rows:
            print('no standing runs; add one with: xlab standing add SPEC --every-hours N')
        for s_ in rows:
            print(f'{s_["id"]}: every {s_["every_hours"]:g} h, {len(s_["runs"])} finished run(s), '
                  f'next {s_["next_utc"] or "as soon as the runner is free"}')
            for r in s_['runs'][-args.limit:]:
                cells = '; '.join(f'{c["cell"]}: control {analyze.fmt(c["control_gmean"])} cv '
                                  f'{(c["cv"] or 0):.1%}' + ''.join(f', {e["arm"]} {analyze.fmt(e["ratio"])}x'
                                                                    for e in c['effects']) for c in r['cells'])
                print(f'  {(r["ended"] or "")[:16]}  {cells}')
            for d in s_['drift']:
                print(f'  drift {d["cell"]}: {d["ratio_to_median"]:.3f}x the median of {d["previous_runs"]} earlier '
                      f'runs, z {d["z"]:.1f}' + ('  <- FLAGGED' if d['flagged'] else ''))
    return 0


def cmd_preflight(args):
    import tempfile
    from .adapters import Context
    lab = lab_of(args)
    adapters = adapters_mod.load_all(lab)
    s = spec_mod.load(lab.resolve(args.file), adapters=adapters)
    with tempfile.TemporaryDirectory(dir=lab.scratch) as folder:
        ctx = Context(lab, 'preflight', s, pathlib.Path(folder), pathlib.Path(folder), print)
        problems = adapters[s['adapter']].preflight(ctx)
    for problem in problems:
        print(f'PROBLEM {problem}')
    print('preflight passed' if not problems else f'preflight failed: {len(problems)} problem(s)')
    return 1 if problems else 0


def cmd_selftest(args):
    from . import selftest
    result = selftest.run(args.experiments, args.blocks, args.sd, log=None if args.json else print)
    print(json.dumps(result, indent=1) if args.json else selftest.render(result))
    return 0 if result['passed'] else 1


def cmd_service(args):
    from .daemon import service
    name, text = service(lab_of(args))
    print(f'# save as {name}', file=sys.stderr)
    if name.endswith('.plist'):
        print(f'# then: cp {name} ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/{name}', file=sys.stderr)
    else:
        print(f'# then: cp {name} ~/.config/systemd/user/ && systemctl --user enable --now xlab && '
              f'loginctl enable-linger $USER', file=sys.stderr)
    print(text)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog='xlab', description='Decision-tree experiment lab.')
    parser.add_argument('--lab', help='lab directory (default: XLAB_HOME, or the nearest lab.toml upward)')
    parser.add_argument('--version', action='version', version=f'xlab {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('init', help='create a lab with example specs and trees')
    p.add_argument('dir', nargs='?', default='.')
    p.add_argument('--name')
    p.add_argument('--example', default='all', choices=['all', 'simulated', 'compression', 'manual', 'command'])
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser('check', help='validate a spec or a tree')
    p.add_argument('file')
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser('plan', help='show the randomized plan a spec would get')
    p.add_argument('file')
    p.add_argument('--seed', type=int)
    p.add_argument('--limit', type=int, default=40)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser('enqueue', help='plan a spec and add it to the queue')
    p.add_argument('file')
    p.add_argument('--front', action='store_true', help='run it next')
    p.add_argument('--seed', type=int)
    p.add_argument('--count', type=int, default=1, help='queue N independent runs (fresh seeds)')
    p.set_defaults(fn=cmd_enqueue)

    p = sub.add_parser('queue', aliases=['status'], help='what is running and what waits')
    p.set_defaults(fn=cmd_queue)

    p = sub.add_parser('work', help='run the queue in the foreground until it is empty')
    p.add_argument('--trees', action='store_true', help='also advance trees and keep going while they queue runs')
    p.set_defaults(fn=cmd_work)

    p = sub.add_parser('up', help='runner + trees + reports + AI analysis + dashboard, until stopped')
    p.add_argument('--no-runner', action='store_true')
    p.add_argument('--no-dashboard', action='store_true')
    p.add_argument('--host')
    p.add_argument('--port', type=int)
    p.set_defaults(fn=cmd_up)

    for name, text in (('pause', 'start no new run'), ('resume', 'resume (also clears a halt)')):
        p = sub.add_parser(name, help=text)
        p.set_defaults(fn=cmd_control)
    p = sub.add_parser('cancel', help='cancel a run (a running one stops after its current sample)')
    p.add_argument('run')
    p.set_defaults(fn=cmd_control)

    p = sub.add_parser('tasks', help='manual samples waiting to be recorded')
    p.add_argument('run', nargs='?')
    p.add_argument('--limit', type=int, default=200)
    p.add_argument('--csv', action='store_true', help='print a CSV to fill in and `xlab import`')
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser('record', help='record one manual sample: xlab record RUN UNIT metric=value ...')
    p.add_argument('run')
    p.add_argument('unit')
    p.add_argument('values', nargs='*')
    p.add_argument('--void', metavar='REASON', help='void the sample instead (its block is re-planned)')
    p.add_argument('--note')
    p.set_defaults(fn=cmd_record)

    p = sub.add_parser('import', help='record manual samples from a CSV (see `xlab tasks --csv`)')
    p.add_argument('run')
    p.add_argument('file')
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser('analyze', aliases=['analyse'], help="an experiment's paired comparisons and Pareto fronts")
    p.add_argument('experiment', nargs='?')
    p.add_argument('--run', action='append', help='analyse only these runs')
    p.add_argument('--metric')
    p.add_argument('--control')
    p.add_argument('--looks', type=int, default=1)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser('tree', help='decision trees: start, status, advance, draft')
    p.add_argument('action', choices=['start', 'status', 'advance', 'draft'])
    p.add_argument('target', nargs='?', help='tree file (start), tree id (status) or question (draft)')
    p.add_argument('--restart', action='store_true')
    p.add_argument('--adapter', help='for draft: the adapter the experiments use')
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser('report', help='write a report now')
    p.add_argument('--hours', type=float, help='window length (default: [reports] every_hours)')
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser('insight', help='run the AI analysis now')
    p.add_argument('--hours', type=float)
    p.add_argument('--evidence', action='store_true', help='print the evidence that would be sent, and stop')
    p.set_defaults(fn=cmd_insight)

    p = sub.add_parser('corpus', help='build or list corpora')
    p.add_argument('action', choices=['build', 'list'])
    p.add_argument('names', nargs='*')
    p.set_defaults(fn=cmd_corpus)

    p = sub.add_parser('adapters', help='list adapters')
    p.set_defaults(fn=cmd_adapters)

    p = sub.add_parser('standing', help='sentinel runs: a short spec repeated on a schedule (noise floor, drift)')
    p.add_argument('action', choices=['add', 'list', 'remove'])
    p.add_argument('target', nargs='?', help='spec file (add) or spec id (remove)')
    p.add_argument('--every-hours', type=float)
    p.add_argument('--limit', type=int, default=8, help='runs to show per standing spec (list)')
    p.set_defaults(fn=cmd_standing)

    p = sub.add_parser('preflight', help="check a spec's system under test before running it")
    p.add_argument('file')
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser('selftest', help='check the statistics on synthetic experiments with known effects')
    p.add_argument('--experiments', type=int, default=60)
    p.add_argument('--blocks', type=int, default=8)
    p.add_argument('--sd', type=float, default=0.04)
    p.add_argument('--json', action='store_true')
    p.set_defaults(fn=cmd_selftest)

    p = sub.add_parser('service', help='print a launchd/systemd unit that keeps `xlab up` running')
    p.set_defaults(fn=cmd_service)

    args = parser.parse_args(argv)
    try:
        return args.fn(args) or 0
    except (spec_mod.SpecError, trees.TreeError, runs.RunError, corpora.CorpusError) as error:
        print(f'xlab: {error}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        from .ai import AIError
        from .daemon import LockHeld
        if isinstance(error, (AIError, LockHeld)):
            print(f'xlab: {error}', file=sys.stderr)
            return 2
        raise


if __name__ == '__main__':
    sys.exit(main())
