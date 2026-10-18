"""Periodic local reports.

Every `[reports] every_hours` the lab writes LAB/reports/report-STAMP.{md,html,json}:
what ran in the window, how many samples were valid or void (and why), each touched
experiment's current analysis (paired ratios with intervals and verdicts, Pareto
fronts), decision-tree progress and decisions, outstanding manual tasks, the latest
AI insight if one was made, and the provenance needed to reproduce it.

The Markdown is for reading anywhere; the HTML embeds the same data with the
dashboard's chart code, so it opens offline as a single file.
"""
import collections
import json
from importlib import resources

from . import analyze, insight, measure, runs, standing, util
from .analyze import fmt, speedup
from .trees import Conductor


def build(lab, store, start, end, adapters=None):
    samples = store.samples(since=start, until=end)
    by_state = collections.Counter(s['state'] for s in samples)
    reasons = collections.Counter(s['void_reason'] for s in samples if s['state'] == 'void')
    touched = sorted({s['run_id'] for s in samples})
    ended = [r for r in store.runs(limit=500) if r['ended'] and start <= r['ended'] < end]
    experiments = {}
    for run_id in touched + [r['id'] for r in ended]:
        run = store.run(run_id)
        if run and run['experiment'] not in experiments:
            result = analyze.analyse(lab, store, run['experiment'], adapters=adapters)
            if result:
                experiments[run['experiment']] = result
    conductor = Conductor(lab, store, adapters)
    trees = [conductor.view(t['id']) for t in store.trees() if t['active'] or (t['updated'] or '') >= start]
    sentinels = standing.summary(lab, store, adapters)
    insights = [d for d in store.documents('insight', limit=20) if d['end'] and start < d['end'] <= end
                and d['data'] and d['data'].get('model')]
    events = [e for e in store.events(500, since=start) if e['utc'] < end]
    return {
        'lab': lab.name, 'window': {'start': start, 'end': end}, 'generated_utc': util.utc(),
        'host': measure.host_fingerprint(),
        'summary': {'samples': dict(by_state), 'runs_sampled': len(touched),
                    'runs_ended': collections.Counter(r['state'] for r in ended),
                    'queued': len(store.queue()), 'tasks_outstanding': len(runs.tasks(store, limit=10000)),
                    'runner': store.get('runner')},
        'void_reasons': dict(reasons.most_common(10)),
        'runs': [{'id': r['id'], 'experiment': r['experiment'], 'state': r['state'], 'tree': r['tree'],
                  'node': r['node'], 'counts': store.counts(r['id']), 'planned': r['planned'], 'started': r['started'],
                  'ended': r['ended'], 'detail': r['detail']} for r in (store.run(x) for x in touched) if r]
                + [{'id': r['id'], 'experiment': r['experiment'], 'state': r['state'], 'tree': r['tree'],
                    'node': r['node'], 'counts': store.counts(r['id']), 'planned': r['planned'],
                    'started': r['started'], 'ended': r['ended'], 'detail': r['detail']}
                   for r in ended if r['id'] not in touched],
        'experiments': experiments,
        'trees': [t for t in trees if t],
        'standing': sentinels,
        'insight': insights[0]['data'] if insights else None,
        'insight_window': {'start': insights[0]['start'], 'end': insights[0]['end']} if insights else None,
        'events': [{'utc': e['utc'], 'kind': e['kind'], 'text': e['text'], 'run': e['run_id']} for e in events
                   if e['kind'] not in ('sample',)][:200],
    }


# -- Markdown --------------------------------------------------------------------

def markdown(data):
    w = data['window']
    s = data['summary']
    out = [f"# {data['lab']} report", '',
           f"Window {w['start']} to {w['end']} (UTC). Generated {data['generated_utc']} on {data['host']['hostname']}.", '']
    samples = s['samples']
    out.append(f"**Samples:** {samples.get('valid', 0)} valid, {samples.get('void', 0)} void, "
               f"{samples.get('warmup', 0)} warm-up, across {s['runs_sampled']} run(s). "
               f"**Queue:** {s['queued']} waiting. **Manual tasks outstanding:** {s['tasks_outstanding']}.")
    runner = s.get('runner') or {}
    if runner:
        out.append(f"**Runner:** {runner.get('state')} (heartbeat {runner.get('heartbeat')})"
                   + (f" — {runner['detail']}" if runner.get('detail') else ''))
    if data['void_reasons']:
        out += ['', '**Void samples:** ' + '; '.join(f'{n}× {r}' for r, n in data['void_reasons'].items())]
    if data['insight']:
        ins = data['insight']
        model = (ins.get('model') or {}).get('model')
        out += ['', '## AI analysis', '', f"_Written by {model} from this lab's numbers for "
                f"{data['insight_window']['start'][:16]}–{data['insight_window']['end'][:16]}. Check it against the "
                f"tables below._", '', ins['overview'], '']
        for f in ins['findings']:
            out += [f"- **{f['title']}.** {f['finding']} _Evidence:_ {f['evidence']} _Caveat:_ {f['caveat']} "
                    f"({', '.join(f['experiments'])})"]
        if ins['anomalies']:
            out += ['', '**Anomalies**', ''] + [f"- **{a['title']}.** {a['detail']}" for a in ins['anomalies']]
        if ins['next_experiments']:
            out += ['', '**Suggested next experiments**', ''] + [
                f"- {n['question']} — {n['rationale']} _Design:_ {n['design']}" for n in ins['next_experiments']]
        if ins['limitations']:
            out += ['', '**Limitations:** ' + ' '.join(ins['limitations'])]
    if data['runs']:
        out += ['', '## Runs', '', '| Run | Experiment | State | Valid / planned | Void | Tree |', '|---|---|---|---|---|---|']
        for r in data['runs']:
            c = r['counts']
            out.append(f"| `{r['id']}` | {r['experiment']} | {r['state']} | {c.get('valid', 0)} / {r['planned']} | "
                       f"{c.get('void', 0)} | {(r['tree'] or '') + ('/' + r['node'] if r['node'] else '')} |")
    for name, result in data['experiments'].items():
        out += ['', f'## {name}', '']
        if result.get('question'):
            out.append(f"**Question:** {result['question']}")
        if result.get('hypothesis'):
            out.append(f"**Hypothesis:** {result['hypothesis']}")
        out.append(f"{result['kind'].capitalize()}, adapter `{result['adapter']}`, {result['blocks']} complete blocks "
                   f"from {len(result['runs'])} run(s); primary metric `{result['primary']}`, smallest effect that "
                   f"matters {result['delta']:.0%}, verdicts need {result['min_blocks']} blocks.")
        effects = [e for e in result['effects'] if e['primary']]
        if effects:
            unit = result['metrics'][result['primary']]
            out += ['', f"Ratio of `{result['primary']}` to **{result['control']}** ({unit['better']} is better), "
                    f"{(1 - result['alpha']):.1%} interval:", '',
                    '| Cell | Arm | Ratio | Interval | Improvement | Blocks | p | Verdict |',
                    '|---|---|---|---|---|---|---|---|']
            for e in effects:
                out.append(f"| {e['cell']} | {e['arm']} | {fmt(e['ratio'])} | [{fmt(e['low'])}, {fmt(e['high'])}] | "
                           f"{fmt(speedup(e))}× | {e['blocks']} | {fmt(e['p'], 2)} | {e['verdict']} |")
        for front in result['pareto']:
            mx, my = result['metrics'][front['x']], result['metrics'][front['y']]
            out += ['', f"Pareto front, `{front['x']}` ({mx['unit']}, {mx['better']} is better) against "
                    f"`{front['y']}` ({my['unit']}, {my['better']} is better), cell {front['cell']}:", '',
                    f"| Arm | {front['x']} | {front['y']} | On front | n |", '|---|---|---|---|---|']
            for p in sorted(front['points'], key=lambda p: -p['x'] if front['better_x'] == 'higher' else p['x']):
                out.append(f"| {p['arm']} | {fmt(p['x'])} | {fmt(p['y'])} | {'yes' if p['front'] else ''} | {p['n']} |")
        if not effects and not result['pareto']:
            rows = [a for a in result['absolutes'] if a['metric'] == result['primary']]
            out += ['', '| Cell | Arm | Geometric mean | Interval | n | CV |', '|---|---|---|---|---|---|']
            for a in rows:
                out.append(f"| {a['cell']} | {a['arm']} | {fmt(a.get('gmean', a.get('mean')))} | "
                           f"[{fmt(a.get('gmean_low'))}, {fmt(a.get('gmean_high'))}] | {a['n']} | {fmt(a.get('cv'), 2)} |")
    for tree in data['trees']:
        if 'nodes' not in tree:
            continue
        out += ['', f"## Tree {tree['id']}", '']
        if tree.get('question'):
            out.append(f"**Question:** {tree['question']}")
        out.append('Done.' if tree['done'] else 'In progress.')
        out += ['', '| Node | State | Question | Decision |', '|---|---|---|---|']
        for n in tree['nodes']:
            out.append(f"| {n['id']} | {n['state']} | {n['question'] or ''} | {(n['decision'] or {}).get('label', '')} |")
        recent = [d for d in tree['decisions'] if d['utc'] >= data['window']['start']]
        if recent:
            out += ['', 'Decisions in this window:', ''] + [f"- {d['utc'][:16]} {d['node'] or ''}: {d['text']}" for d in recent]
    for s in data.get('standing', []):
        out += ['', f"## Sentinel {s['id']}", '', f"Every {s['every_hours']:g} h; {len(s['runs'])} finished run(s)."]
        if s['runs']:
            last = s['runs'][-1]
            unit = last['unit'] or ''
            out += ['', f"| Run ended | Cell | Control ({last['control']}) {last['primary']} | CV | Ratios to control |",
                    '|---|---|---|---|---|']
            for r in s['runs'][-8:]:
                for c in r['cells']:
                    ratios = ', '.join(f"{e['arm']} {fmt(e['ratio'])}× [{fmt(e['low'], 3)}, {fmt(e['high'], 3)}]"
                                       for e in c['effects'])
                    out.append(f"| {(r['ended'] or '')[:16]} | {c['cell']} | {fmt(c['control_gmean'])} {unit} | "
                               f"{fmt((c['cv'] or 0) * 100, 2)}% | {ratios} |")
        for d in s['drift']:
            out.append(f"\n{'**Drift flagged:** ' if d['flagged'] else 'Drift check: '}{d['cell']} control at "
                       f"{d['ratio_to_median']:.3f}× its median over {d['previous_runs']} earlier runs "
                       f"(robust z {d['z']:.1f}).")
    notable = [e for e in data['events'] if e['kind'] in ('failed', 'halt', 'warning', 'error', 'cancelled', 'block')]
    if notable:
        out += ['', '## Problems', ''] + [f"- {e['utc'][:19]} {e['kind']}: {e['text']}" for e in notable[:50]]
    out += ['', '## Provenance', '', f"xlab {data['host'].get('xlab')}, Python {data['host'].get('python')}, "
            f"{data['host'].get('system')} {data['host'].get('release')} {data['host'].get('machine')}"
            + (f", {data['host']['cpu']}" if data['host'].get('cpu') else '') + '. Each run directory holds the exact '
            'spec, the plan with its seed, the host and tool fingerprints, and every sample record.', '']
    return '\n'.join(out)


# -- HTML ---------------------------------------------------------------------------

def html(data):
    static = resources.files('xlab.dashboard') / 'static'
    css = (static / 'style.css').read_text()
    charts = (static / 'charts.js').read_text()
    views = (static / 'views.js').read_text()
    report_js = (static / 'report.js').read_text()
    payload = json.dumps(data, default=str).replace('</', '<\\/')
    title = f"{data['lab']} report {data['window']['end'][:16]}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{css}</style></head>
<body class="report"><main id="report"></main>
<script id="data" type="application/json">{payload}</script>
<script>{charts}</script><script>{views}</script><script>{report_js}</script></body></html>"""


def generate(lab, store, start, end, adapters=None):
    data = build(lab, store, start, end, adapters)
    name = f'report-{util.stamp(util.parse_utc(end))}'
    folder = lab.reports
    util.write_atomic(folder / f'{name}.json', json.dumps(data, indent=1, default=str))
    util.write_atomic(folder / f'{name}.md', markdown(data))
    util.write_atomic(folder / f'{name}.html', html(data))
    summary = {'samples': data['summary']['samples'], 'experiments': list(data['experiments']),
               'runs': len(data['runs']), 'insight': bool(data['insight'])}
    store.add_document(name, 'report', start, end, path=folder / f'{name}.html', data=summary)
    store.event('report', f'report written: reports/{name}.md')
    return folder / f'{name}.md'


def tick(lab, store, adapters=None, log=print):
    window = insight.due(store, 'report', lab.section('reports')['every_hours'])
    if not window:
        return None
    store.put('report_last_attempt', util.utc())
    try:
        return generate(lab, store, *window, adapters=adapters)
    except Exception as error:
        store.event('error', f'report failed: {str(error)[:300]}')
        log(f'report failed: {error}')
        return None
