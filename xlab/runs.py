"""Turning a spec into a queued run, and everything done to a run from outside the
runner: cancelling it, opening a manual run's tasks, recording a manual result."""
import json
import pathlib
import secrets

from . import adapters as adapters_mod
from . import plan as plan_mod
from . import spec as spec_mod
from . import util


class RunError(ValueError):
    pass


def run_dir(lab, run_id):
    return lab.runs / run_id


def load_run_spec(lab, run_id, adapters=None):
    """The spec exactly as it was when the run was planned (the copy in its directory)."""
    text = (run_dir(lab, run_id) / 'spec.toml').read_text()
    meta = json.loads((run_dir(lab, run_id) / 'plan.json').read_text())
    spec = spec_mod.load(text=text, adapters=adapters)
    spec['source'] = meta.get('source')
    return spec


def load_plan(lab, run_id):
    return json.loads((run_dir(lab, run_id) / 'plan.json').read_text())


def enqueue(lab, store, source, seed=None, front=False, tree=None, node=None, adapters=None, text=None, lane=None):
    """Plan a spec and add it to the queue. `source` is a spec path (or a label when text is given)."""
    adapters = adapters or adapters_mod.load_all(lab)
    if text is None:
        path = lab.resolve(source)
        if not path.exists():
            raise RunError(f'no spec at {path}')
        text = path.read_text()
        source = str(path)
    spec = spec_mod.load(text=text, adapters=adapters)
    adapter = adapters[spec['adapter']]
    seed = seed if seed is not None else spec.get('seed') or plan_mod.new_seed()
    run_id = f'{util.stamp()}-{spec["id"]}-{secrets.token_hex(2)}'
    plan = plan_mod.make(spec, run_id, seed, automatic=adapter.automatic)
    plan['source'] = source
    plan['tree'], plan['node'], plan['lane'] = tree, node, lane
    plan['metrics'] = {k: m.as_dict() for k, m in adapter.metrics(spec).items()}
    folder = run_dir(lab, run_id)
    folder.mkdir(parents=True)
    util.write_atomic(folder / 'spec.toml', text)
    util.write_atomic(folder / 'plan.json', json.dumps(plan, indent=1))
    store.add_run({'id': run_id, 'experiment': spec['id'], 'kind': spec['kind'], 'adapter': spec['adapter'],
                   'automatic': adapter.automatic, 'state': 'queued',
                   'priority': store.front_priority() if front else 0.0, 'tree': tree, 'node': node,
                   'spec_sha256': spec['sha256'], 'seed': seed, 'planned': plan['planned_samples'],
                   'question': spec.get('question'), 'lane': lane})
    store.event('queued', f'{spec["id"]} queued ({plan["planned_samples"]} samples, seed {seed})', run_id)
    return run_id


def cancel(lab, store, run_id, reason='cancelled by operator'):
    run = store.run(run_id)
    if run is None:
        raise RunError(f'no run {run_id}')
    if run['state'] == 'queued':
        store.set_run(run_id, state='cancelled', ended=util.utc(), detail=reason)
    elif run['state'] == 'running':
        store.put('cancel', run_id)   # the runner stops after the current sample
    elif run['state'] == 'awaiting':
        for s in store.samples(run_id, state='awaiting'):
            store.write("UPDATE samples SET state = 'cancelled' WHERE id = ?", s['id'])
        store.set_run(run_id, state='cancelled', ended=util.utc(), detail=reason)
    else:
        raise RunError(f'{run_id} is already {run["state"]}')
    store.event('cancel', f'{run["experiment"]}: {reason}', run_id)


def _manual_record(plan, unit, instructions, state='awaiting'):
    cell = plan['cells'][unit['cell_index']]
    return {'schema': 1, 'run': plan['run'], 'experiment': plan['experiment'], 'unit': unit['unit'],
            'block': unit['block'], 'cell': cell['key'], 'cell_index': unit['cell_index'], 'factors': cell['factors'],
            'arm': unit['arm'], 'attempt': unit['attempt'], 'position': unit['position'], 'state': state,
            'instructions': instructions, 'metrics': {}}


def open_manual(lab, store, run_id, adapters):
    """Create a task for every planned sample of a manual run, in plan order."""
    plan = load_plan(lab, run_id)
    spec = load_run_spec(lab, run_id, adapters)
    adapter = adapters[spec['adapter']]
    for unit in plan_mod.manual_units(plan):
        cell = plan['cells'][unit['cell_index']]
        text = adapter.instructions(spec, unit['arm'], cell['factors'])
        store.add_sample(_manual_record(plan, unit, text))
    store.set_run(run_id, state='awaiting', started=util.utc())
    store.event('awaiting', f'{plan["experiment"]}: {plan["planned_samples"]} tasks are waiting to be recorded', run_id)


def tasks(store, run_id=None, limit=200):
    """Outstanding manual samples, earliest position first."""
    sql = ("SELECT s.*, r.experiment FROM samples s JOIN runs r ON r.id = s.run_id "
           "WHERE s.state = 'awaiting' AND r.state = 'awaiting'")
    args = []
    if run_id:
        sql += ' AND s.run_id = ?'
        args.append(run_id)
    sql += ' ORDER BY r.created, s.attempt, s.position LIMIT ?'
    rows = store.q(sql, *args, limit)
    for r in rows:
        record = json.loads(r['record'])
        r['instructions'] = record.get('instructions')
        r['factors'] = record.get('factors')
        r.pop('record', None)
    return rows


def record_manual(lab, store, run_id, unit, metrics=None, void_reason=None, note=None, by=None, adapters=None,
                  measured_utc=None):
    """Record the result of one manual task (or void it, which re-opens its block)."""
    adapters = adapters or adapters_mod.load_all(lab)
    run = store.run(run_id)
    if run is None:
        raise RunError(f'no run {run_id}')
    if run['state'] != 'awaiting':
        raise RunError(f'{run_id} is {run["state"]}, not awaiting results')
    sample = store.sample(run_id, unit)
    if sample is None:
        raise RunError(f'{run_id} has no sample {unit}')
    if sample['state'] != 'awaiting':
        raise RunError(f'{unit} is already {sample["state"]}')
    spec = load_run_spec(lab, run_id, adapters)
    defined = adapters[spec['adapter']].metrics(spec)
    record = dict(sample['record'])
    if void_reason:
        record.update(state='void', void_reason=str(void_reason), metrics={})
    else:
        values = {}
        for name, value in (metrics or {}).items():
            if name not in defined:
                raise RunError(f'{name} is not a metric of {spec["id"]} (have {", ".join(defined)})')
            try:
                values[name] = float(value)
            except (TypeError, ValueError):
                raise RunError(f'{name} must be a number, not {value!r}') from None
            if defined[name].positive and values[name] <= 0:
                raise RunError(f'{name} must be positive')
        missing = [m for m in defined if m not in values]
        if not values or spec['primary'] in missing:
            raise RunError(f'the primary metric {spec["primary"]} is required')
        record.update(state='valid', metrics=values, void_reason=None)
    record.update(ended_utc=measured_utc or util.utc(), recorded_utc=util.utc(), note=note, recorded_by=by)
    record.setdefault('started_utc', record['ended_utc'])
    util.append_durable(run_dir(lab, run_id) / 'samples.jsonl', record)
    store.add_sample(record)
    plan = load_plan(lab, run_id)
    if void_reason:
        _reopen_block(lab, store, plan, spec, record, adapters)
    store.event('sample', f'{run["experiment"]} {record["arm"]} {record["cell"]}: '
                + (f'void ({void_reason})' if void_reason else
                   ', '.join(f'{k}={v:g}' for k, v in record['metrics'].items())), run_id)
    refresh_manual(store, run_id)
    return record


def _reopen_block(lab, store, plan, spec, record, adapters):
    """A void manual sample discards its block attempt; plan the next attempt, if any is left."""
    block, cell_index, attempt = record['block'], record['cell_index'], record['attempt']
    for s in store.samples(record['run'], state='awaiting'):
        if s['block'] == block and s['cell'] == record['cell'] and s['attempt'] == attempt:
            store.write("UPDATE samples SET state = 'cancelled' WHERE id = ?", s['id'])
    if attempt >= plan['retries']:
        store.event('block', f'{plan["experiment"]} block {block} {record["cell"]} abandoned after '
                    f'{attempt + 1} attempts', record['run'])
        return
    order = plan_mod.arm_order(plan['seed'], block, cell_index, attempt + 1, plan['arms'])
    adapter = adapters[spec['adapter']]
    factors = plan['cells'][cell_index]['factors']
    base = max((s['position'] or 0) for s in store.samples(record['run'])) + 1
    for i, arm in enumerate(order):
        unit = {'unit': plan_mod.unit_id(block, cell_index, attempt + 1, arm), 'block': block,
                'cell_index': cell_index, 'arm': arm, 'attempt': attempt + 1, 'position': base + i}
        store.add_sample(_manual_record(plan, unit, adapter.instructions(spec, arm, factors)))


def refresh_manual(store, run_id=None):
    """Mark awaiting runs complete once nothing is left to record."""
    runs = [store.run(run_id)] if run_id else store.runs(state='awaiting')
    for run in runs:
        if run and run['state'] == 'awaiting' and not store.counts(run['id']).get('awaiting'):
            store.set_run(run['id'], state='complete', ended=util.utc())
            store.event('complete', f'{run["experiment"]}: every task recorded', run['id'])


def import_csv(lab, store, run_id, path, adapters=None):
    """Record manual results from a CSV with a `unit` column and one column per metric
    (optional: void_reason, note, measured_utc). Returns the number recorded."""
    import csv
    count = 0
    with open(pathlib.Path(path)) as handle:
        for row in csv.DictReader(handle):
            unit = row.pop('unit')
            void = row.pop('void_reason', '') or None
            note = row.pop('note', '') or None
            when = row.pop('measured_utc', '') or None
            for k in ('arm', 'cell', 'block', 'position', 'instructions'):
                row.pop(k, None)
            metrics = {k: v for k, v in row.items() if v not in ('', None)}
            record_manual(lab, store, run_id, unit, metrics, void, note, by=f'import:{pathlib.Path(path).name}',
                          adapters=adapters, measured_utc=when)
            count += 1
    return count
