"""Experiment specifications: what is compared (arms), under which conditions (cells),
how often (blocks), and how it is judged (primary metric, control arm, delta).

A spec is a TOML file. The adapter named in it says how one sample is taken.

    id = "C1-codec-screen"
    question = "Which codec settings trade speed for ratio best on text?"
    hypothesis = "zstd dominates xz except at the highest ratios"
    kind = "screening"             # screening | confirmatory | characterization
    adapter = "compression"
    primary = "compress_s"
    control = "zstd-3"
    blocks = 6
    delta = 0.05                   # the smallest ratio change that matters (5%)
    pareto = [["compress_mbps", "ratio"]]

    [factors]                      # cells are every combination of these
    corpus = ["text-32m", "logs-32m"]

    [arms.zstd-3]
    codec = "zstd"
    level = 3

    [[grid]]                       # or generate arms from a grid
    name = "xz-{level}"
    codec = "xz"
    level = [1, 6, 9]
"""
import copy
import fnmatch
import itertools
import json
import pathlib
import tomllib

from . import util

KINDS = ('screening', 'confirmatory', 'characterization')


class SpecError(ValueError):
    pass


def cell_key(factors):
    return ','.join(f'{k}={factors[k]}' for k in sorted(factors)) or 'all'


def expand_grid(grid):
    """One [[grid]] table -> arms. List values are crossed; `name` is a format template."""
    template = grid.get('name')
    if not template:
        raise SpecError('a [[grid]] needs a `name` template such as "zstd-{level}"')
    fixed = {k: v for k, v in grid.items() if k != 'name' and not isinstance(v, list)}
    varied = {k: v for k, v in grid.items() if k != 'name' and isinstance(v, list)}
    arms = {}
    keys = list(varied)
    for combo in itertools.product(*(varied[k] for k in keys)):
        params = dict(fixed, **dict(zip(keys, combo)))
        try:
            name = template.format(**params)
        except KeyError as missing:
            raise SpecError(f'grid name {template!r} uses {missing} which the grid does not define') from None
        arms[name] = params
    return arms


def load(path=None, text=None, adapters=None):
    """Load and validate a spec; returns a dict with arms and cells fully expanded."""
    if text is None:
        text = pathlib.Path(path).read_text()
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise SpecError(f'{path or "spec"}: {error}') from None
    return normalise(raw, source=str(path) if path else None, text=text, adapters=adapters)


def normalise(raw, source=None, text=None, adapters=None):
    spec = copy.deepcopy(raw)
    where = source or spec.get('id', 'spec')
    for key in ('id', 'adapter', 'primary'):
        if key not in spec:
            raise SpecError(f'{where}: a spec needs `{key}`')
    if not util.SAFE.fullmatch(spec['id']):
        raise SpecError(f'{where}: `id` must be a plain name (letters, digits, . _ -)')
    spec.setdefault('kind', 'screening')
    if spec['kind'] not in KINDS:
        raise SpecError(f'{where}: `kind` must be one of {", ".join(KINDS)}')
    spec.setdefault('blocks', 6)
    spec.setdefault('delta', 0.05)
    spec.setdefault('retries', 2)
    spec.setdefault('warmup', 0)
    spec.setdefault('min_blocks', 6)
    spec.setdefault('options', {})
    spec.setdefault('metrics', {})
    if not isinstance(spec['blocks'], int) or spec['blocks'] < 1:
        raise SpecError(f'{where}: `blocks` must be a positive integer')
    arms = dict(spec.get('arms', {}))
    for grid in spec.pop('grid', []):
        for name, params in expand_grid(grid).items():
            if name in arms:
                raise SpecError(f'{where}: arm {name} is defined twice')
            arms[name] = params
    if not arms:
        raise SpecError(f'{where}: a spec needs at least one arm ([arms.NAME] or [[grid]])')
    for name in arms:
        if not util.SAFE.fullmatch(name):
            raise SpecError(f'{where}: arm name {name!r} must be a plain name')
    spec['arms'] = arms
    control = spec.get('control')
    if control is not None and control not in arms:
        raise SpecError(f'{where}: control arm {control} is not one of the arms')
    if spec['kind'] in ('screening', 'confirmatory') and control is None and len(arms) > 1:
        raise SpecError(f'{where}: a {spec["kind"]} experiment needs a `control` arm')
    # cells
    if 'cells' in spec:
        cells = [dict(c) for c in spec['cells']]
    else:
        factors = spec.get('factors', {})
        keys = sorted(factors)
        for k in keys:
            if not isinstance(factors[k], list):
                factors[k] = [factors[k]]
        cells = [dict(zip(keys, combo)) for combo in itertools.product(*(factors[k] for k in keys))]
    if not cells:
        cells = [{}]
    keys = [cell_key(c) for c in cells]
    if len(set(keys)) != len(keys):
        raise SpecError(f'{where}: two cells have the same factors')
    spec['cells'] = [{'key': k, 'factors': c} for k, c in zip(keys, cells)]
    for pair in spec.get('pareto', []):
        if not (isinstance(pair, list) and len(pair) == 2):
            raise SpecError(f'{where}: each `pareto` entry is a pair of metric names')
    if adapters is not None:
        adapter = adapters.get(spec['adapter'])
        if adapter is None:
            raise SpecError(f'{where}: unknown adapter {spec["adapter"]!r} (have {", ".join(sorted(adapters))})')
        adapter.validate(spec)
        metrics = adapter.metrics(spec)
        if spec['primary'] not in metrics:
            raise SpecError(f'{where}: primary metric {spec["primary"]!r} is not one the adapter reports '
                            f'({", ".join(sorted(metrics))})')
        for pair in spec.get('pareto', []):
            for name in pair:
                if name not in metrics:
                    raise SpecError(f'{where}: pareto metric {name!r} is not reported')
    spec['source'] = source
    spec['sha256'] = util.sha256_bytes((text or util.canonical(raw)).encode())
    return spec


def to_raw(spec):
    """The spec as it would be written back to TOML (arms explicit, no grids)."""
    raw = {k: v for k, v in spec.items() if k not in ('cells', 'source', 'sha256')}
    raw['cells'] = [c['factors'] for c in spec['cells']] if spec['cells'] != [{'key': 'all', 'factors': {}}] else None
    raw.pop('factors', None)
    return {k: v for k, v in raw.items() if v is not None}


def matches(value, pattern):
    """fnmatch against one pattern or any of a list."""
    if pattern is None:
        return True
    patterns = pattern if isinstance(pattern, list) else [pattern]
    return any(fnmatch.fnmatchcase(str(value), str(p)) for p in patterns)


def summary(spec):
    return {'id': spec['id'], 'kind': spec['kind'], 'adapter': spec['adapter'], 'question': spec.get('question'),
            'hypothesis': spec.get('hypothesis'), 'primary': spec['primary'], 'control': spec.get('control'),
            'blocks': spec['blocks'], 'delta': spec['delta'], 'arms': list(spec['arms']),
            'cells': [c['key'] for c in spec['cells']], 'pareto': spec.get('pareto', [])}


def dumps(spec):
    return json.dumps(summary(spec), indent=1)
