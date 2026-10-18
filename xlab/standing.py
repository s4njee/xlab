"""Standing (sentinel) runs: the same short experiment, repeated on a schedule.

    xlab standing add experiments/S0-sentinel.toml --every-hours 6

Every `every_hours` the runner puts a fresh run of the spec at the front of the queue,
whatever else is waiting (a run in progress is never interrupted). The repeated
measurement gives two things no single experiment can:

  * the noise floor at each hour of the day: the control arm's coefficient of
    variation, and, if the spec has two identical arms, how far their ratio strays
    from 1 when nothing differs;
  * drift detection: if the control's geometric mean in the latest run is far from
    its median over earlier runs (robust z above 3 and further than the spec's
    delta), the host or the system under test has changed, and every comparison
    made since deserves a second look. A warning event is raised once per run.
"""
import datetime
import math
import statistics

from . import analyze, runs, spec as spec_mod, util

KEY = 'standing'


def entries(store):
    return store.get(KEY, {}) or {}


def add(lab, store, path, every_hours, adapters=None):
    target = lab.resolve(path)
    s = spec_mod.load(target, adapters=adapters)
    if every_hours <= 0:
        raise ValueError('every_hours must be positive')
    current = entries(store)
    current[s['id']] = {'id': s['id'], 'path': str(target), 'every_hours': float(every_hours),
                        'added_utc': util.utc(), 'last_queued_utc': None, 'runs': current.get(s['id'], {}).get('runs', [])}
    store.put(KEY, current)
    store.event('standing', f'{s["id"]} will run every {every_hours:g} h')
    return current[s['id']]


def remove(store, spec_id):
    current = entries(store)
    if spec_id not in current:
        raise KeyError(spec_id)
    del current[spec_id]
    store.put(KEY, current)
    store.event('standing', f'{spec_id} is no longer a standing run')


def next_due(entry):
    if not entry.get('last_queued_utc'):
        return None
    return util.parse_utc(entry['last_queued_utc']) + datetime.timedelta(hours=entry['every_hours'])


def queue_due(lab, store, adapters=None, now=None):
    """Queue (at the front) every standing spec whose interval has passed. Returns run ids."""
    now = now or util.now()
    current = entries(store)
    queued = []
    for spec_id, entry in current.items():
        due = next_due(entry)
        if due is not None and due > now:
            continue
        waiting = [r for r in entry.get('runs', []) if (store.run(r) or {}).get('state') in ('queued', 'running')]
        if waiting:
            continue            # the previous one has not run yet: do not pile them up
        try:
            run_id = runs.enqueue(lab, store, entry['path'], front=True, adapters=adapters, lane='standing')
        except Exception as error:
            store.event('error', f'standing {spec_id}: {error}')
            entry['last_queued_utc'] = util.utc(now)    # do not retry every poll
            continue
        entry['last_queued_utc'] = util.utc(now)
        entry['runs'] = (entry.get('runs', []) + [run_id])[-500:]
        queued.append(run_id)
    if queued:
        store.put(KEY, current)
    return queued


def history(lab, store, entry, adapters=None):
    """One row per finished run: per cell, the control's geometric mean and CV, and every arm's
    ratio to the control (for identical twin arms, the noise floor)."""
    rows = []
    for run_id in entry.get('runs', []):
        run = store.run(run_id)
        if not run or run['state'] != 'complete':
            continue
        try:
            result = analyze.analyse(lab, store, run_ids=[run_id], adapters=adapters)
        except Exception:
            continue
        if not result:
            continue
        control = result['control'] or result['arms'][0]
        cells = []
        for cell in result['cells']:
            a = next((x for x in result['absolutes'] if x['cell'] == cell['key'] and x['arm'] == control
                      and x['metric'] == result['primary']), None)
            effects = [{'arm': e['arm'], 'ratio': e['ratio'], 'low': e['low'], 'high': e['high']}
                       for e in result['effects'] if e['primary'] and e['cell'] == cell['key']]
            cells.append({'cell': cell['key'], 'control_gmean': a.get('gmean') if a else None,
                          'cv': a.get('cv') if a else None, 'n': a['n'] if a else 0, 'effects': effects})
        rows.append({'run': run_id, 'ended': run['ended'], 'primary': result['primary'], 'control': control,
                     'delta': result['delta'], 'unit': result['metrics'][result['primary']]['unit'], 'cells': cells})
    return rows


def drift(rows, min_previous=3, z_limit=3.0):
    """For the latest run, per cell: control value against the median of the earlier runs."""
    if len(rows) < min_previous + 1:
        return []
    latest, earlier = rows[-1], rows[:-1]
    out = []
    for cell in latest['cells']:
        now = cell['control_gmean']
        past = [c['control_gmean'] for r in earlier for c in r['cells'] if c['cell'] == cell['cell'] and c['control_gmean']]
        if not now or len(past) < min_previous:
            continue
        logs = [math.log(v) for v in past]
        centre = statistics.median(logs)
        mad = statistics.median(abs(v - centre) for v in logs) * 1.4826
        ratio = math.exp(math.log(now) - centre)
        z = (math.log(now) - centre) / mad if mad > 0 else (0.0 if ratio == 1 else math.inf)
        flagged = abs(z) > z_limit and abs(math.log(ratio)) > math.log1p(latest['delta'])
        out.append({'cell': cell['cell'], 'run': latest['run'], 'ratio_to_median': ratio, 'z': z, 'flagged': flagged,
                    'previous_runs': len(past)})
    return out


def summary(lab, store, adapters=None):
    out = []
    for spec_id, entry in entries(store).items():
        rows = history(lab, store, entry, adapters)
        due = next_due(entry)
        out.append({'id': spec_id, 'path': entry['path'], 'every_hours': entry['every_hours'],
                    'last_queued_utc': entry.get('last_queued_utc'), 'next_utc': util.utc(due) if due else None,
                    'runs': rows, 'drift': drift(rows)})
    return out


def check(lab, store, run_id, adapters=None):
    """After a standing run completes: warn once if its control has drifted."""
    for spec_id, entry in entries(store).items():
        if run_id not in entry.get('runs', []):
            continue
        for d in drift(history(lab, store, entry, adapters)):
            if d['flagged'] and d['run'] == run_id:
                store.event('warning', f'sentinel {spec_id} drifted in {d["cell"]}: control at '
                            f'{d["ratio_to_median"]:.3f}x its median over {d["previous_runs"]} earlier runs '
                            f'(robust z {d["z"]:.1f})', run_id)
