"""Adapters say how one sample of an experiment is taken.

An adapter is the only benchmark-specific code in xlab. Everything else (plans,
randomization, the runner, gates, statistics, trees, reports, the dashboard)
works for any adapter. To add a benchmark, subclass Adapter and register it:

    from xlab.adapters import Adapter, Metric, register

    @register
    class Sprint(Adapter):
        name = 'sprint'
        def metrics(self, spec):
            return {'time_s': Metric('time_s', 's', 'lower', 'Sprint time')}
        def measure(self, ctx, arm, cell):
            ...
            return {'time_s': 11.2}

Adapters are found by name: the built-ins below, plus any module listed in the
lab's `[lab] adapters = ["mypackage.myadapter"]` or dropped into `LAB/adapters/*.py`.

`automatic = False` marks an adapter whose samples are taken outside the computer
(a person, an instrument, a field trial). The runner then turns every planned
sample into a task with instructions; its results are recorded later through the
dashboard, `xlab record` or `xlab import`.
"""
import dataclasses
import importlib
import importlib.util
import pathlib
import sys

REGISTRY = {}


class Void(Exception):
    """A sample cannot be used, for a reason that does not depend on its result.

    Raising it records the sample as void (with the reason, never a value) and
    the block is tried again in a fresh order."""


@dataclasses.dataclass
class Metric:
    name: str
    unit: str | None = None
    better: str | None = None          # "lower", "higher" or None (descriptive only)
    label: str | None = None
    positive: bool = True              # strictly positive: ratios and geometric means apply
    description: str | None = None

    def as_dict(self):
        return dataclasses.asdict(self)


def declared_metrics(spec):
    """Metrics declared in the spec's [metrics.NAME] tables."""
    out = {}
    for name, table in spec.get('metrics', {}).items():
        out[name] = Metric(name, table.get('unit'), table.get('better'), table.get('label'),
                           table.get('positive', True), table.get('description'))
    return out


@dataclasses.dataclass
class Context:
    """What an adapter may use while it runs. Paths are per run."""
    lab: object                       # xlab.config.Lab
    run_id: str
    spec: dict
    run_dir: pathlib.Path
    scratch: pathlib.Path
    log: object                       # callable(text)
    state: dict = dataclasses.field(default_factory=dict)  # adapter-owned, per run

    @property
    def options(self):
        return self.spec.get('options', {})


class Adapter:
    name = 'base'
    automatic = True
    description = ''

    def validate(self, spec):
        """Raise xlab.spec.SpecError for a spec this adapter cannot run."""

    def metrics(self, spec):
        """{name: Metric} for every metric a sample reports."""
        return declared_metrics(spec)

    def fingerprint(self, ctx):
        """What identifies the system under test (tool versions, hardware). Checked at
        every block: a change mid-run voids the block and fails the run."""
        return {}

    def preflight(self, ctx):
        """Cheap checks that the system under test is what the spec says, before anything is built
        or measured. Returns a list of problems (empty when all is well); any problem fails the run
        before its first sample. Must not write outside ctx.scratch."""
        return []

    def prepare_run(self, ctx):
        """Untimed, once per run before the first sample (build inputs, stage data)."""

    def prepare_sample(self, ctx, arm, cell):
        """Untimed, before each sample (reset state, warm or drop caches)."""

    def measure(self, ctx, arm, cell):
        """Take one sample and return {metric: value}. Raise Void to discard it."""
        raise NotImplementedError

    def finish_sample(self, ctx, arm, cell, metrics):
        """Untimed, after each sample (verify outputs, clean up). Raise Void to discard it."""

    def finish_run(self, ctx):
        """Untimed, once after the last sample."""

    def instructions(self, spec, arm, cell):
        """Plain-language instructions for a manual sample."""
        return None

    def describe(self):
        return {'name': self.name, 'automatic': self.automatic, 'description': self.description}


def register(cls):
    REGISTRY[cls.name] = cls
    return cls


def load_all(lab=None):
    """Instances of every known adapter, keyed by name."""
    from . import command, compression, manual, simulated  # noqa: F401  (registration)
    if lab is not None:
        for module in lab.config.get('lab', {}).get('adapters', []):
            importlib.import_module(module)
        folder = lab.root / 'adapters'
        if folder.is_dir():
            for path in sorted(folder.glob('*.py')):
                name = f'xlab_lab_adapter_{path.stem}'
                if name in sys.modules:
                    continue
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
    return {name: cls() for name, cls in REGISTRY.items()}
