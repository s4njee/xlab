"""The lab: a directory holding its configuration, specs, trees, data and reports.

    LAB/lab.toml          settings (below)
    LAB/experiments/      experiment specs (*.toml)
    LAB/trees/            decision trees (*.toml)
    LAB/adapters/         optional lab-specific adapters (*.py)
    LAB/data/xlab.db      the index: runs, samples, queue, trees, events (SQLite, WAL)
    LAB/data/runs/RUN/    the record: spec.toml, plan.json, env.json, samples.jsonl, run.log
    LAB/data/scratch/     per-run working space, removed when a run ends
    LAB/data/corpora/     generated inputs, pinned by digest
    LAB/reports/          periodic reports (.md, .html, .json) and AI insights
"""
import copy
import os
import pathlib
import tomllib

DEFAULTS = {
    'lab': {'name': 'xlab', 'adapters': []},
    'runner': {'poll_s': 5.0, 'halt_after_failures': 3, 'halt_minutes': 60, 'keep_scratch': False},
    'gates': {'max_load_per_core': None, 'settle_timeout_s': 120, 'max_wait_h': 12, 'max_temp_c': None, 'min_free_gib': 1.0},
    'reports': {'every_hours': 6.0, 'keep': None},
    'ai': {'provider': 'none', 'model': None, 'every_hours': 6.0, 'max_tokens': 16000, 'effort': 'medium',
           'timeout_s': 300, 'key_file': '~/.config/xlab/keys.json', 'base_url': None},
    'dashboard': {'host': '127.0.0.1', 'port': 8710},
    'conductor': {'every_s': 20.0},
}


def merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


class Lab:
    def __init__(self, root):
        self.root = pathlib.Path(root).expanduser().resolve()
        path = self.root / 'lab.toml'
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        self.config = merge(DEFAULTS, raw)
        self.data = self.root / 'data'
        self.runs = self.data / 'runs'
        self.scratch = self.data / 'scratch'
        self.corpora = self.data / 'corpora'
        self.reports = self.root / 'reports'
        self.experiments = self.root / 'experiments'
        self.trees = self.root / 'trees'
        for folder in (self.data, self.runs, self.scratch, self.corpora, self.reports):
            folder.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data / 'xlab.db'

    def section(self, name):
        return self.config.get(name, {})

    @property
    def name(self):
        return self.config['lab']['name']

    def resolve(self, path):
        """A path in a spec or tree: absolute, ~, or relative to the lab."""
        p = pathlib.Path(os.path.expanduser(str(path)))
        return p if p.is_absolute() else self.root / p

    def store(self):
        from .store import Store
        return Store(self.db_path)


def find_lab(start=None):
    """The lab named by XLAB_HOME, else the nearest directory upward holding lab.toml, else cwd."""
    if os.environ.get('XLAB_HOME'):
        return Lab(os.environ['XLAB_HOME'])
    here = pathlib.Path(start or os.getcwd()).resolve()
    for folder in (here, *here.parents):
        if (folder / 'lab.toml').exists():
            return Lab(folder)
    return Lab(here)
