"""Analysis of an experiment's runs.

Unit of observation: a complete block, i.e. one attempt of one (run, replicate, cell)
in which every arm gave a valid sample. Within a block each arm's value is divided by
the control's, and the log of that ratio is one observation. Across blocks:

  estimate     exp(mean log-ratio), with the paired t interval
  robust       Hodges-Lehmann estimate and its distribution-free interval
  p            exact (or sampled) sign-flip permutation test of no difference
  verdict      better / worse / equivalent / inconclusive, from the interval and the
               smallest effect that matters (delta), only when both intervals agree
               and there are at least `min_blocks` blocks (else "insufficient")

Confirmatory experiments widen every interval to control the family-wise error over
their primary comparisons (Bonferroni) and report Holm-adjusted p-values. Screening
experiments do neither and their verdicts are leads, not claims. An experiment
analysed more than once (a tree node adding blocks) widens by its number of looks.

Absolutes (geometric means and intervals of every metric per cell and arm) and the
Pareto front of each declared metric pair are reported for every kind.
"""
import collections
import json
import math
import statistics

from . import adapters as adapters_mod
from . import runs as runs_mod
from . import stats


def complete_blocks(samples, arms):
    """{(run, block, cell): {arm: metrics}} using the latest attempt in which every arm is valid."""
    attempts = collections.defaultdict(dict)
    for s in samples:
        if s['state'] != 'valid' or str(s['unit']).startswith('w'):
            continue
        attempts[(s['run_id'], s['block'], s['cell'], s['attempt'])][s['arm']] = s['metrics']
    blocks = {}
    for (run, block, cell, attempt), got in sorted(attempts.items(), key=lambda kv: kv[0][3]):
        if set(got) >= set(arms):
            blocks[(run, block, cell)] = got
    return blocks


def runs_for(store, experiment, run_ids=None):
    if run_ids:
        return [store.run(r) for r in run_ids if store.run(r)]
    rows = [r for r in store.runs(experiment=experiment, limit=1000)]
    if not rows:
        return []
    latest = rows[0]['spec_sha256']   # runs are newest first; analyse those sharing the latest spec
    return [r for r in rows if r['spec_sha256'] == latest]


def analyse(lab, store, experiment=None, run_ids=None, control=None, looks=1, adapters=None):
    adapters = adapters or adapters_mod.load_all(lab)
    rows = runs_for(store, experiment, run_ids)
    if not rows:
        return None
    rows.sort(key=lambda r: r['created'])
    spec = runs_mod.load_run_spec(lab, rows[-1]['id'], adapters)
    adapter = adapters[spec['adapter']]
    metric_defs = adapter.metrics(spec)
    arms = list(spec['arms'])
    control = control or spec.get('control')
    kind = spec['kind']
    samples = []
    for r in rows:
        samples += store.samples(r['id'])
    blocks = complete_blocks(samples, arms)
    alpha = stats.ALPHA / max(1, looks)
    cells = [c['key'] for c in spec['cells']]
    factors = {c['key']: c['factors'] for c in spec['cells']}

    # effects
    effects = []
    if control in arms:
        for cell in cells:
            for arm in arms:
                if arm == control:
                    continue
                for name, metric in metric_defs.items():
                    if not metric.positive:
                        continue
                    diffs = []
                    for (run, block, c), got in blocks.items():
                        if c != cell:
                            continue
                        a, b = got[arm].get(name), got[control].get(name)
                        if a and b and a > 0 and b > 0:
                            diffs.append(math.log(a / b))
                    effects.append({'cell': cell, 'factors': factors[cell], 'arm': arm, 'control': control,
                                    'metric': name, 'better': metric.better, 'primary': name == spec['primary'],
                                    'blocks': len(diffs), 'diffs': diffs})
        primary = [e for e in effects if e['primary']]
        if kind == 'confirmatory' and primary:
            alpha = alpha / len(primary)
        for e in effects:
            diffs = e.pop('diffs')
            e.update(_effect(diffs, alpha, spec['delta'], e['better'], spec['min_blocks']))
        if kind == 'confirmatory':
            adjusted = stats.holm([e['p'] for e in primary])
            for e, p in zip(primary, adjusted):
                e['p_holm'] = p

    # absolutes from every valid sample
    values = collections.defaultdict(list)
    counts = collections.Counter()
    reasons = collections.Counter()
    for s in samples:
        counts[s['state']] += 1
        if s['state'] == 'void':
            reasons[s['void_reason']] += 1
        if s['state'] != 'valid' or str(s['unit']).startswith('w'):
            continue
        for name, value in s['metrics'].items():
            values[(s['cell'], s['arm'], name)].append(value)
    absolutes = []
    for (cell, arm, name), vals in sorted(values.items()):
        d = stats.describe(vals)
        absolutes.append({'cell': cell, 'factors': factors.get(cell, {}), 'arm': arm, 'metric': name, **d})
    lookup = {(a['cell'], a['arm'], a['metric']): a for a in absolutes}

    # pareto fronts per declared pair and cell
    fronts = []
    for x_name, y_name in spec.get('pareto', []):
        bx, by = metric_defs[x_name].better, metric_defs[y_name].better
        for cell in cells:
            points = []
            for arm in arms:
                ax, ay = lookup.get((cell, arm, x_name)), lookup.get((cell, arm, y_name))
                if ax and ay:
                    points.append({'arm': arm, 'x': ax.get('gmean', ax['mean']), 'y': ay.get('gmean', ay['mean']),
                                   'x_low': ax.get('gmean_low'), 'x_high': ax.get('gmean_high'),
                                   'y_low': ay.get('gmean_low'), 'y_high': ay.get('gmean_high'), 'n': ax['n']})
            on = set(stats.pareto_front([(p['x'], p['y']) for p in points], bx, by))
            for i, p in enumerate(points):
                p['front'] = i in on
            fronts.append({'cell': cell, 'factors': factors[cell], 'x': x_name, 'y': y_name, 'better_x': bx,
                           'better_y': by, 'points': points})

    # noise floor of the control (or the only arm)
    noise = []
    for cell in cells:
        ref = control or arms[0]
        a = lookup.get((cell, ref, spec['primary']))
        if a:
            noise.append({'cell': cell, 'arm': ref, 'metric': spec['primary'], 'cv': a.get('cv'), 'n': a['n']})

    verdicts = collections.Counter(e['verdict'] for e in effects if e['primary'])
    return {
        'experiment': spec['id'], 'question': spec.get('question'), 'hypothesis': spec.get('hypothesis'),
        'kind': kind, 'adapter': spec['adapter'], 'primary': spec['primary'], 'control': control,
        'delta': spec['delta'], 'min_blocks': spec['min_blocks'], 'alpha': alpha, 'looks': looks,
        'spec_sha256': spec['sha256'], 'arms': arms, 'arm_params': spec['arms'],
        'cells': [{'key': c, 'factors': factors[c]} for c in cells],
        'metrics': {k: m.as_dict() for k, m in metric_defs.items()},
        'runs': [{'id': r['id'], 'state': r['state'], 'created': r['created'], 'started': r['started'],
                  'ended': r['ended'], 'seed': r['seed']} for r in rows],
        'blocks': len(blocks), 'planned': sum(r['planned'] or 0 for r in rows), 'counts': dict(counts),
        'void_reasons': dict(reasons.most_common(10)), 'effects': effects, 'absolutes': absolutes,
        'pareto': fronts, 'noise': noise, 'verdicts': dict(verdicts),
    }


def _effect(diffs, alpha, delta, better, min_blocks):
    n = len(diffs)
    out = {'ratio': None, 'low': None, 'high': None, 'hl': None, 'hl_low': None, 'hl_high': None, 'p': None,
           'sd_log': None, 'blocks_needed': None, 'verdict': 'insufficient'}
    if n == 0:
        return out
    mean, lo, hi = stats.t_interval(diffs, alpha)
    hl, hlo, hhi = stats.hodges_lehmann(diffs, alpha)
    out.update(ratio=math.exp(mean), low=math.exp(lo) if lo is not None else None,
               high=math.exp(hi) if hi is not None else None, hl=math.exp(hl),
               hl_low=math.exp(hlo) if hlo is not None else None, hl_high=math.exp(hhi) if hhi is not None else None,
               p=stats.sign_flip_p(diffs))
    if n > 1:
        sd = statistics.stdev(diffs)
        out['sd_log'] = sd
        out['blocks_needed'] = stats.blocks_needed(sd, delta) if sd > 0 else None
    if n >= min_blocks:
        t_verdict = stats.classify(out['low'], out['high'], delta, better)
        if out['hl_low'] is not None and stats.classify(out['hl_low'], out['hl_high'], delta, better) != t_verdict:
            out['verdict'] = 'inconclusive'
        else:
            out['verdict'] = t_verdict
    return out


def speedup(effect):
    """The improvement factor as people read it: >1 is better, whatever the metric's direction."""
    if effect.get('ratio') is None:
        return None
    return 1.0 / effect['ratio'] if effect.get('better') == 'lower' else effect['ratio']


def to_json(result):
    return json.dumps(result, indent=1, default=str)


# -- plain-text rendering ------------------------------------------------------------

def fmt(value, digits=4):
    if value is None:
        return '—'
    if isinstance(value, float):
        if value != 0 and (abs(value) >= 1e5 or abs(value) < 1e-3):
            return f'{value:.3g}'
        return f'{value:.{digits}g}'
    return str(value)


def render_text(result, metric=None):
    metric = metric or result['primary']
    lines = [f"{result['experiment']} ({result['kind']}, {result['adapter']}): {result['blocks']} complete blocks "
             f"from {len(result['runs'])} run(s); samples {result['counts']}"]
    if result.get('question'):
        lines.append(f"Q: {result['question']}")
    effects = [e for e in result['effects'] if e['metric'] == metric]
    if effects:
        lines.append(f"\n{metric}: ratio to {result['control']} (95% CI{', Bonferroni' if result['kind'] == 'confirmatory' else ''}),"
                     f" delta {result['delta']:.0%}, better = {effects[0]['better']}")
        width = max(len(e['arm']) for e in effects)
        for cell in result['cells']:
            lines.append(f"  [{cell['key']}]")
            for e in (e for e in effects if e['cell'] == cell['key']):
                lines.append(f"    {e['arm']:<{width}}  {fmt(e['ratio'])}  [{fmt(e['low'])}, {fmt(e['high'])}]  "
                             f"n={e['blocks']}  p={fmt(e['p'], 2)}  {e['verdict']}")
    for front in result['pareto']:
        lines.append(f"\nPareto {front['x']} vs {front['y']} [{front['cell']}]:")
        for p in sorted(front['points'], key=lambda p: p['x']):
            lines.append(f"    {'*' if p['front'] else ' '} {p['arm']:<20} {front['x']}={fmt(p['x'])}  "
                         f"{front['y']}={fmt(p['y'])}")
    if not effects and not result['pareto']:
        lines.append(f'\n{metric} by arm (geometric mean, 95% CI):')
        for a in (a for a in result['absolutes'] if a['metric'] == metric):
            lines.append(f"  [{a['cell']}] {a['arm']}: {fmt(a.get('gmean', a['mean']))} "
                         f"[{fmt(a.get('gmean_low'))}, {fmt(a.get('gmean_high'))}] n={a['n']}")
    if result['void_reasons']:
        lines.append('\nVoid samples: ' + '; '.join(f'{n}x {r}' for r, n in result['void_reasons'].items()))
    return '\n'.join(lines)
