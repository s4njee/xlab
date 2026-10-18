"""Randomized complete block plans.

A block is one replicate of one cell: every arm measured once, back to back, in an
order drawn from the run's seed. Comparisons are made within a block, so slow drift
(a warming room, a filling disk, a busy hour) cancels out of each paired ratio.
Blocks of different cells are interleaved replicate by replicate, again in a seeded
order, so no cell is measured only early or only late.

If a sample in a block is void, the whole block attempt is discarded and the block is
tried again with a fresh order (up to `retries` more times). A block that never
completes contributes nothing; it is never patched with a sample from another attempt.

Every order is a pure function of (seed, replicate, cell, attempt), so a plan can be
reproduced and audited from its seed alone.
"""
import random

from . import util


def rng(seed, *parts):
    return random.Random(':'.join(str(p) for p in (seed, *parts)))


def arm_order(seed, replicate, cell_index, attempt, arms):
    order = list(arms)
    rng(seed, 'arms', replicate, cell_index, attempt).shuffle(order)
    return order


def block_sequence(seed, blocks, n_cells):
    """[(replicate, cell_index)] in execution order."""
    sequence = []
    for replicate in range(blocks):
        cells = list(range(n_cells))
        rng(seed, 'cells', replicate).shuffle(cells)
        sequence += [(replicate, c) for c in cells]
    return sequence


def unit_id(replicate, cell_index, attempt, arm):
    return f'b{replicate:03d}.c{cell_index}.t{attempt}.{arm}'


def warmup_id(cell_index, arm, k=0):
    return f'w{k}.c{cell_index}.{arm}'


def new_seed():
    return random.SystemRandom().randrange(1, 2 ** 31)


def make(spec, run_id, seed, automatic=True):
    arms = list(spec['arms'])
    cells = spec['cells']
    sequence = block_sequence(seed, spec['blocks'], len(cells))
    plan = {
        'schema': 1, 'run': run_id, 'experiment': spec['id'], 'kind': spec['kind'], 'adapter': spec['adapter'],
        'automatic': automatic, 'spec_sha256': spec['sha256'], 'seed': seed, 'created_utc': util.utc(),
        'blocks': spec['blocks'], 'retries': spec['retries'], 'warmup': spec['warmup'],
        'arms': arms, 'control': spec.get('control'), 'primary': spec['primary'], 'delta': spec['delta'],
        'cells': [{'index': i, 'key': c['key'], 'factors': c['factors']} for i, c in enumerate(cells)],
        'sequence': [{'replicate': r, 'cell': c,
                      'orders': [arm_order(seed, r, c, t, arms) for t in range(spec['retries'] + 1)]}
                     for r, c in sequence],
        'planned_samples': spec['blocks'] * len(cells) * len(arms),
    }
    return plan


def manual_units(plan):
    """For a manual run: every first-attempt unit in the order it should be done."""
    units, position = [], 0
    for step in plan['sequence']:
        for arm in step['orders'][0]:
            units.append({'unit': unit_id(step['replicate'], step['cell'], 0, arm), 'block': step['replicate'],
                          'cell': plan['cells'][step['cell']]['key'], 'cell_index': step['cell'], 'arm': arm,
                          'attempt': 0, 'position': position})
            position += 1
    return units


def render(plan, limit=40):
    """Human-readable plan, for `xlab plan`."""
    lines = [f"{plan['experiment']}  seed {plan['seed']}  {plan['blocks']} blocks x {len(plan['cells'])} cells x "
             f"{len(plan['arms'])} arms = {plan['planned_samples']} samples (+ up to {plan['retries']} retries per block)"]
    for cell in plan['cells']:
        lines.append(f"  c{cell['index']}: {cell['key']}")
    for i, step in enumerate(plan['sequence'][:limit]):
        lines.append(f"  block {step['replicate']:>3} c{step['cell']}: {' '.join(step['orders'][0])}")
    if len(plan['sequence']) > limit:
        lines.append(f"  ... {len(plan['sequence']) - limit} more blocks")
    return '\n'.join(lines)
