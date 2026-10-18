"""Decision trees: what runs next depends on what the last experiment found.

Every rule is written before anything runs, so a tree is a pre-registered research
plan; the conductor only follows it. A tree is TOML:

    id = "T1-compression"
    question = "Which codec settings should be the default for log archives?"
    start = ["screen"]

    [nodes.screen]
    spec = "experiments/C1-screen.toml"
    question = "Which settings are worth confirming?"

      [[nodes.screen.branch]]                  # the first rule that holds wins
      when = { arm = "zstd-*", verdict = "better" }
      next = ["confirm"]
      label = "a zstd setting beat the control"

      [[nodes.screen.branch]]
      otherwise = true
      next = []
      label = "nothing beat the control"

    [nodes.confirm]
    promote = "screen"       # re-run the control and the arms that satisfied the rule,
    blocks = 12              # as a confirmatory experiment with fresh blocks and seed
    id = "C1c-confirm"

A rule (`when`: a table, or a list of tables that must all hold) picks rows by `arm`,
`metric` (default: the primary) and `factors` (e.g. { corpus = "text*" }), all
fnmatch patterns, and then tests them:

    verdict = "better" | ["better", "equivalent"]     paired comparison with the control
    ratio_at_least / ratio_below = 1.1                arm / control
    speedup_at_least / speedup_below = 1.1            improvement factor (>1 is better either way)
    ratio_low_at_least / ratio_high_below             ends of the 95% interval
    blocks_at_least = 8
    value_at_least / value_below = 3.5                absolute (geometric mean) of the metric
    on_front = true                                   on the Pareto front of `pareto = [x, y]`

An arm qualifies if it passes in any picked cell, or with `all = true` in every picked
cell. The rule holds if some arm qualifies (with `all_arms = true`: if every picked arm
does). A branch whose `when` is a list holds if every rule holds, and the arms it
selects (what a `promote` node confirms) are those that qualified under every rule.

`hold = true` on a branch records "decision needed" instead of queueing anything.
A node listing itself in `next` runs again (up to `max_runs`), and is then judged on
all its runs with intervals widened for the extra look.
"""
import pathlib
import tomllib

from . import analyze, runs, spec as spec_mod, util
from .spec import matches

EFFECT_TESTS = ('verdict', 'ratio_at_least', 'ratio_below', 'speedup_at_least', 'speedup_below',
                'ratio_low_at_least', 'ratio_high_below', 'blocks_at_least')
VALUE_TESTS = ('value_at_least', 'value_below')
TERMINAL = ('complete', 'failed', 'cancelled')


class TreeError(ValueError):
    pass


def load(lab, path):
    path = lab.resolve(path)
    try:
        tree = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise TreeError(f'{path}: {error}') from None
    tree['_path'] = str(path)
    validate(lab, tree)
    return tree


def spec_path(lab, tree, node):
    base = pathlib.Path(tree['_path']).parent
    candidate = base / node['spec']
    return candidate if candidate.exists() else lab.resolve(node['spec'])


def validate(lab, tree, adapters=None):
    for key in ('id', 'start', 'nodes'):
        if key not in tree:
            raise TreeError(f'a tree needs `{key}`')
    if not util.SAFE.fullmatch(tree['id']):
        raise TreeError('a tree `id` must be a plain name')
    nodes = tree['nodes']
    for name in tree['start']:
        if name not in nodes:
            raise TreeError(f'{tree["id"]}: start node {name} is not defined')
    for name, node in nodes.items():
        where = f'{tree["id"]}.{name}'
        if not util.SAFE.fullmatch(name):
            raise TreeError(f'{where}: node names must be plain names')
        if ('spec' in node) == ('promote' in node):
            raise TreeError(f'{where}: a node has a `spec` or a `promote`, not both')
        if 'spec' in node:
            path = spec_path(lab, tree, node)
            if not path.exists():
                raise TreeError(f'{where}: no spec at {path}')
            try:
                spec_mod.load(path, adapters=adapters)
            except spec_mod.SpecError as error:
                raise TreeError(f'{where}: {error}') from None
        if 'promote' in node and node['promote'] not in nodes:
            raise TreeError(f'{where}: promotes unknown node {node["promote"]}')
        for branch in node.get('branch', []):
            if not branch.get('otherwise') and 'when' not in branch:
                raise TreeError(f'{where}: a branch needs `when` or `otherwise = true`')
            for target in branch.get('next', []):
                if target not in nodes:
                    raise TreeError(f'{where}: a branch leads to unknown node {target}')
            for rule in _rules(branch.get('when')):
                unknown = set(rule) - {'arm', 'metric', 'factors', 'all', 'all_arms', 'on_front', 'pareto', *EFFECT_TESTS,
                                       *VALUE_TESTS}
                if unknown:
                    raise TreeError(f'{where}: unknown rule keys {", ".join(sorted(unknown))}')


def _rules(when):
    if when is None:
        return []
    return when if isinstance(when, list) else [when]


# -- rule evaluation --------------------------------------------------------------

def _picked(rows, rule, primary):
    metric = rule.get('metric', primary)
    out = []
    for row in rows:
        if row.get('metric', metric) != metric and 'metric' in row:
            continue
        if not matches(row['arm'], rule.get('arm')):
            continue
        factors = rule.get('factors', {})
        if any(not matches(row.get('factors', {}).get(k), v) for k, v in factors.items()):
            continue
        out.append(row)
    return out


def _passes(rule, row):
    checks = []
    if 'verdict' in rule:
        wanted = rule['verdict'] if isinstance(rule['verdict'], list) else [rule['verdict']]
        checks.append(row.get('verdict') in wanted)
    ratio, speed = row.get('ratio'), analyze.speedup(row) if 'ratio' in row else None
    for key, value, op in (('ratio_at_least', ratio, 'ge'), ('ratio_below', ratio, 'lt'),
                           ('speedup_at_least', speed, 'ge'), ('speedup_below', speed, 'lt'),
                           ('ratio_low_at_least', row.get('low'), 'ge'), ('ratio_high_below', row.get('high'), 'lt'),
                           ('blocks_at_least', row.get('blocks'), 'ge'),
                           ('value_at_least', row.get('gmean', row.get('mean')), 'ge'),
                           ('value_below', row.get('gmean', row.get('mean')), 'lt')):
        if key in rule:
            checks.append(value is not None and (value >= rule[key] if op == 'ge' else value < rule[key]))
    if 'on_front' in rule:
        checks.append(bool(row.get('front')) == bool(rule['on_front']))
    return all(checks) if checks else False


def evaluate(rule, result):
    """(holds, arms that passed, evidence rows) for one rule against an analysis."""
    primary = result['primary']
    if any(k in rule for k in EFFECT_TESTS):
        rows = _picked(result['effects'], rule, primary)
    elif any(k in rule for k in VALUE_TESTS):
        rows = _picked(result['absolutes'], rule, primary)
    elif 'on_front' in rule:
        pair = (rule.get('pareto') or (result['pareto'][0]['x'], result['pareto'][0]['y'])) if result['pareto'] else None
        rows = []
        for front in result['pareto']:
            if pair and [front['x'], front['y']] == list(pair):
                rows += [dict(p, factors=front['factors'], metric=primary) for p in front['points']]
        rows = _picked(rows, rule, primary)
    else:
        raise TreeError(f'rule {rule} tests nothing')
    by_arm = {}
    for r in rows:
        by_arm.setdefault(r['arm'], []).append((r, _passes(rule, r)))
    if rule.get('all'):     # an arm qualifies only if it passes in every picked cell
        chosen = [a for a, checks in by_arm.items() if all(ok for _, ok in checks)]
    else:                   # an arm qualifies if it passes in any picked cell
        chosen = [a for a, checks in by_arm.items() if any(ok for _, ok in checks)]
    holds = bool(chosen) and (len(chosen) == len(by_arm) if rule.get('all_arms') else True)
    evidence = [r for a in chosen for r, ok in by_arm[a] if ok]
    return holds, sorted(chosen), evidence


def choose(node, result):
    """The first branch that holds: (index, branch, selected arms, evidence)."""
    for i, branch in enumerate(node.get('branch', [])):
        if branch.get('otherwise'):
            return i, branch, [], []
        selected, evidence, ok = None, [], True
        for rule in _rules(branch['when']):
            holds, arms, rows = evaluate(rule, result)
            if not holds:
                ok = False
                break
            selected = arms if selected is None else sorted(set(selected) & set(arms))
            evidence += rows
            if not selected:
                ok = False      # no single arm satisfies every rule of this branch
                break
        if ok:
            return i, branch, selected or [], evidence
    return None, None, [], []


# -- the conductor -------------------------------------------------------------------

class Conductor:
    def __init__(self, lab, store, adapters=None):
        self.lab, self.store, self.adapters = lab, store, adapters

    def start(self, path, restart=False):
        tree = load(self.lab, path)
        existing = self.store.tree(tree['id'])
        if existing and existing['active'] and not restart:
            raise TreeError(f'tree {tree["id"]} is already active (use --restart to begin again)')
        state = {'id': tree['id'], 'question': tree.get('question'), 'started_utc': util.utc(), 'done': False,
                 'nodes': {name: {'state': 'pending', 'runs': [], 'decision': None} for name in tree['nodes']},
                 'decisions': []}
        self.store.save_tree(tree['id'], tree['_path'], state)
        self.store.event('tree', f'tree {tree["id"]} started')
        for name in tree['start']:
            self._queue(tree, state, name, 'start')
        self._save(tree, state)
        return state

    def _save(self, tree, state):
        self.store.save_tree(tree['id'], tree['_path'], state, active=not state['done'])

    def _log(self, tree, state, kind, node, text, **extra):
        entry = {'utc': util.utc(), 'kind': kind, 'node': node, 'text': text, **extra}
        state['decisions'].append(entry)
        self.store.event('tree', f'{tree["id"]}/{node}: {text}' if node else f'{tree["id"]}: {text}')

    def _queue(self, tree, state, name, why):
        node, ns = tree['nodes'][name], state['nodes'][name]
        if len(ns['runs']) >= node.get('max_runs', 1):
            self._log(tree, state, 'skip', name, f'not run again: it has had its {node.get("max_runs", 1)} run(s)')
            return
        try:
            if 'spec' in node:
                run_id = runs.enqueue(self.lab, self.store, str(spec_path(self.lab, tree, node)), tree=tree['id'],
                                      node=name, adapters=self.adapters)
            else:
                text = self._promoted(tree, state, name, node)
                run_id = runs.enqueue(self.lab, self.store, f'generated:{tree["id"]}/{name}', text=text,
                                      tree=tree['id'], node=name, adapters=self.adapters)
        except Exception as error:
            ns['state'] = 'failed'
            self._log(tree, state, 'error', name, f'could not queue: {error}')
            return
        ns['runs'].append(run_id)
        ns['state'] = 'queued'
        self._log(tree, state, 'queue', name, f'queued {run_id} ({why})', run=run_id)

    def _promoted(self, tree, state, name, node):
        parent = state['nodes'][node['promote']]
        if not parent['runs']:
            raise TreeError(f'{node["promote"]} has no run to promote')
        base = runs.load_run_spec(self.lab, parent['runs'][-1], self.adapters)
        raw = spec_mod.to_raw(base)
        chosen = (parent.get('decision') or {}).get('arms') or []
        if node.get('arms'):
            chosen = [a for a in raw['arms'] if matches(a, node['arms'])] if not chosen else \
                [a for a in chosen if matches(a, node['arms'])]
        control = raw.get('control')
        keep = [a for a in raw['arms'] if a == control or a in chosen]
        if len(keep) < 2 and control:
            raise TreeError(f'nothing to confirm against {control}: no arm was selected')
        raw['arms'] = {a: raw['arms'][a] for a in keep}
        raw['id'] = node.get('id', f'{base["id"]}-confirm')
        raw['kind'] = node.get('kind', 'confirmatory')
        raw['blocks'] = node.get('blocks', base['blocks'] * 2)
        raw['question'] = node.get('question', raw.get('question'))
        raw['promoted_from'] = {'tree': tree['id'], 'node': node['promote'], 'run': parent['runs'][-1]}
        raw.pop('seed', None)
        if node.get('options'):
            raw['options'] = {**raw.get('options', {}), **node['options']}
        text = (f'# Generated by tree {tree["id"]} node {name}: confirms {", ".join(a for a in keep if a != control)}\n'
                f'# against {control} from run {parent["runs"][-1]}.\n' + util.to_toml(raw))
        folder = self.lab.experiments / 'generated'
        folder.mkdir(parents=True, exist_ok=True)
        util.write_atomic(folder / f'{raw["id"]}.toml', text)
        return text

    def tick(self):
        """Advance every active tree once. Returns the number of decisions made."""
        made = 0
        for row in self.store.trees():
            if not row['active']:
                continue
            try:
                tree = load(self.lab, row['path'])
            except TreeError as error:
                self.store.event('error', f'tree {row["id"]}: {error}')
                continue
            state = row['state']
            for name, ns in state['nodes'].items():
                if ns['state'] != 'queued':
                    continue
                run = self.store.run(ns['runs'][-1])
                if run is None or run['state'] not in TERMINAL:
                    continue
                if run['state'] != 'complete':
                    ns['state'] = run['state']
                    self._log(tree, state, run['state'], name, f'run {run["id"]} {run["state"]}: '
                              f'{run["detail"] or "no detail"}; the branch ends here')
                    made += 1
                    continue
                self._decide(tree, state, name)
                made += 1
            if not any(ns['state'] == 'queued' for ns in state['nodes'].values()):
                if not state['done']:
                    state['done'] = True
                    state['ended_utc'] = util.utc()
                    self._log(tree, state, 'done', None, 'every branch has reached its end')
            self._save(tree, state)
        return made

    def _decide(self, tree, state, name):
        node, ns = tree['nodes'][name], state['nodes'][name]
        result = analyze.analyse(self.lab, self.store, run_ids=ns['runs'], looks=len(ns['runs']),
                                 adapters=self.adapters)
        if not node.get('branch'):
            ns['state'] = 'decided'
            ns['decision'] = {'utc': util.utc(), 'branch': None, 'label': 'end', 'arms': [], 'verdicts': result['verdicts']}
            self._log(tree, state, 'decide', name, f'finished ({_verdict_text(result)}); no branches follow')
            return
        index, branch, arms, evidence = choose(node, result)
        if branch is None:
            ns['state'] = 'decided'
            ns['decision'] = {'utc': util.utc(), 'branch': None, 'label': 'no rule held', 'arms': []}
            self._log(tree, state, 'decide', name, 'no branch held and there is no `otherwise`; the branch ends')
            return
        label = branch.get('label') or branch.get('note') or ('otherwise' if branch.get('otherwise') else f'rule {index + 1}')
        ns['decision'] = {'utc': util.utc(), 'branch': index, 'label': label, 'note': branch.get('note'), 'arms': arms,
                          'next': branch.get('next', []), 'verdicts': result['verdicts'],
                          'evidence': [{k: e.get(k) for k in ('arm', 'cell', 'metric', 'ratio', 'low', 'high', 'verdict',
                                                               'blocks', 'gmean', 'x', 'y')} for e in evidence[:12]]}
        if branch.get('hold'):
            ns['state'] = 'held'
            self._log(tree, state, 'hold', name, f'{label}: decision needed (branch is on hold)')
            return
        ns['state'] = 'decided'
        self._log(tree, state, 'decide', name, f'{label}' + (f' -> {", ".join(branch.get("next", []))}'
                                                              if branch.get('next') else ' (end)'), arms=arms)
        for target in branch.get('next', []):
            if target == name or state['nodes'][target]['state'] == 'pending':
                self._queue(tree, state, target, f'{name}: {label}')

    def view(self, tree_id):
        """Nodes and edges for the dashboard and reports."""
        row = self.store.tree(tree_id)
        if row is None:
            return None
        try:
            tree = load(self.lab, row['path'])
        except TreeError:
            return {'id': tree_id, 'error': 'tree file is missing or invalid', 'state': row['state']}
        state = row['state']
        nodes, edges = [], []
        for name, node in tree['nodes'].items():
            ns = state['nodes'].get(name, {'state': 'pending', 'runs': [], 'decision': None})
            runs_info = []
            for run_id in ns['runs']:
                r = self.store.run(run_id)
                if r:
                    c = self.store.counts(run_id)
                    runs_info.append({'id': run_id, 'state': r['state'], 'valid': c.get('valid', 0),
                                      'planned': r['planned']})
            nodes.append({'id': name, 'kind': 'confirm' if 'promote' in node else 'experiment',
                          'question': node.get('question'), 'spec': node.get('spec'), 'promote': node.get('promote'),
                          'start': name in tree['start'], 'state': ns['state'], 'runs': runs_info,
                          'decision': ns.get('decision')})
            taken = (ns.get('decision') or {}).get('branch')
            for i, branch in enumerate(node.get('branch', [])):
                label = branch.get('label') or branch.get('note') or ('otherwise' if branch.get('otherwise') else
                                                                      _rule_text(branch.get('when')))
                for target in branch.get('next', []):
                    edges.append({'from': name, 'to': target, 'label': label,
                                  'taken': None if taken is None and ns['state'] not in ('decided', 'held') else taken == i})
            if 'promote' in node:
                edges.append({'from': node['promote'], 'to': name, 'label': 'promotes', 'taken': None, 'promote': True})
        return {'id': tree_id, 'question': tree.get('question'), 'active': bool(row['active']), 'done': state['done'],
                'started_utc': state.get('started_utc'), 'ended_utc': state.get('ended_utc'), 'nodes': nodes,
                'edges': [e for e in edges if not e.get('promote') or not any(
                    x['from'] == e['from'] and x['to'] == e['to'] for x in edges if not x.get('promote'))],
                'decisions': state['decisions'][-50:]}


def _verdict_text(result):
    v = result.get('verdicts') or {}
    return ', '.join(f'{n} {k}' for k, n in v.items()) or 'no paired comparisons'


def _rule_text(when):
    parts = []
    for rule in _rules(when):
        parts.append(' '.join(f'{k}={v}' for k, v in rule.items()))
    return ' & '.join(parts)[:60]

