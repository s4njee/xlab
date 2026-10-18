"""Does the analysis find what is there, and nothing that is not?

`xlab selftest` runs many synthetic experiments through the real runner and analysis
(the simulated adapter, in a throwaway lab) where the truth is known: against a
control, one arm is identical (ratio 1), one is 3% faster (at the edge of delta)
and one is 10% faster. Samples carry log-normal noise, slow drift and random voids,
so blocking and retries are exercised too. It then checks:

    coverage     share of 95% intervals that contain the true ratio (t and Hodges-Lehmann)
    false        share of null-arm verdicts that claim a difference (better or worse)
    power        share of 10%-arm verdicts that say "better"

Coverage and false verdicts are the calibration checks: the test fails if t coverage
is below 0.92 or false verdicts exceed 5%. Power depends on the design (blocks, noise,
delta), so it only has a floor (60%) that catches an analysis that never finds anything;
it is reported beside the power of the t interval alone, which shows what requiring the
robust interval to agree costs. At the defaults (8 blocks, 4% noise, drift) that is
about 75% against 85%: use more blocks where a 10% effect must not be missed. Every run uses a fixed seed, so the result is the same on every machine. Run it
after any change to stats.py, analyze.py, plan.py or the runner.
"""
import collections
import pathlib
import shutil
import tempfile

from . import adapters as adapters_mod
from . import analyze, config, runs, stats
from .runner import Runner

TRUTH = {'null': 1.0, 'edge': 0.97, 'faster': 0.90}

SPEC = """
id = "selftest"
kind = "screening"
adapter = "simulated"
primary = "time_s"
control = "base"
blocks = {blocks}
min_blocks = {blocks}
delta = 0.03
[options]
sd = {sd}
drift = 0.003
void_rate = 0.03
[arms.base]
[arms.null]
effect = 1.0
[arms.edge]
effect = 0.97
[arms.faster]
effect = 0.90
"""

LIMITS = {'coverage': 0.92, 'false': 0.05, 'power': 0.60}


def run(experiments=60, blocks=8, sd=0.04, log=None, keep=None):
    root = pathlib.Path(keep) if keep else pathlib.Path(tempfile.mkdtemp(prefix='xlab-selftest-'))
    root.mkdir(parents=True, exist_ok=True)
    try:
        (root / 'lab.toml').write_text('[lab]\nname = "selftest"\n')
        lab = config.Lab(root)
        store = lab.store()
        adapters = adapters_mod.load_all(lab)
        text = SPEC.format(blocks=blocks, sd=sd)
        ids = [runs.enqueue(lab, store, 'selftest', text=text, seed=seed, adapters=adapters)
               for seed in range(1, experiments + 1)]
        Runner(lab, store, adapters, log=lambda t: None).work_until_empty()
        per_arm = {arm: collections.Counter() for arm in TRUTH}
        for i, run_id in enumerate(ids):
            if log and (i + 1) % 20 == 0:
                log(f'analysed {i + 1}/{len(ids)} experiments')
            result = analyze.analyse(lab, store, run_ids=[run_id], adapters=adapters)
            for e in result['effects']:
                if not e['primary'] or e['arm'] not in TRUTH:
                    continue
                c, truth = per_arm[e['arm']], TRUTH[e['arm']]
                c['n'] += 1
                c['t_cover'] += e['low'] is not None and e['low'] <= truth <= e['high']
                c['hl_cover'] += e['hl_low'] is not None and e['hl_low'] <= truth <= e['hl_high']
                c['verdict:' + e['verdict']] += 1
                if e['blocks'] >= blocks:
                    c['t_only:' + stats.classify(e['low'], e['high'], 0.03, e['better'])] += 1
        return summarise(per_arm, experiments, blocks, sd)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def summarise(per_arm, experiments, blocks, sd):
    arms = {}
    for arm, c in per_arm.items():
        n = c['n'] or 1
        arms[arm] = {'true_ratio': TRUTH[arm], 'intervals': c['n'], 't_coverage': c['t_cover'] / n,
                     'hl_coverage': c['hl_cover'] / n,
                     'verdicts': {k.split(':', 1)[1]: v / n for k, v in sorted(c.items()) if k.startswith('verdict:')},
                     't_only': {k.split(':', 1)[1]: v / n for k, v in sorted(c.items()) if k.startswith('t_only:')}}
    total = sum(c['n'] for c in per_arm.values()) or 1
    coverage = sum(c['t_cover'] for c in per_arm.values()) / total
    null = arms['null']['verdicts']
    false = null.get('better', 0) + null.get('worse', 0)
    power = arms['faster']['verdicts'].get('better', 0)
    checks = [{'name': 't interval coverage', 'value': coverage, 'limit': f'>= {LIMITS["coverage"]}',
               'ok': coverage >= LIMITS['coverage']},
              {'name': 'false verdicts on the identical arm', 'value': false, 'limit': f'<= {LIMITS["false"]}',
               'ok': false <= LIMITS['false']},
              {'name': 'power for a 10% effect', 'value': power, 'limit': f'>= {LIMITS["power"]}',
               'ok': power >= LIMITS['power'],
               'note': f't interval alone: {arms["faster"]["t_only"].get("better", 0):.0%}'}]
    return {'experiments': experiments, 'blocks': blocks, 'sd': sd, 'arms': arms, 'checks': checks,
            'passed': all(c['ok'] for c in checks)}


def render(result):
    lines = [f"{result['experiments']} synthetic experiments, {result['blocks']} blocks each, noise sd {result['sd']}, "
             f"delta 3%, with drift and 3% voids", '',
             f"{'arm':<8} {'truth':>6} {'t cover':>8} {'HL cover':>9}  verdicts"]
    for arm, a in result['arms'].items():
        verdicts = ', '.join(f'{k} {v:.0%}' for k, v in a['verdicts'].items())
        lines.append(f"{arm:<8} {a['true_ratio']:>6.2f} {a['t_coverage']:>8.1%} {a['hl_coverage']:>9.1%}  {verdicts}")
    lines.append('')
    for c in result['checks']:
        lines.append(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['value']:.1%} (needs {c['limit']})"
                     + (f"; {c['note']}" if c.get('note') else ''))
    lines.append('selftest passed' if result['passed'] else 'selftest FAILED')
    return '\n'.join(lines)
