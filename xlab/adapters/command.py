"""Any benchmark that is a command line.

    adapter = "command"
    primary = "wall_s"

    [options]
    timeout_s = 600
    setup = "make -C {lab}/bench"            # untimed, once per run
    before = "rm -f {scratch}/out"           # untimed, before each sample
    after = "cmp {scratch}/out {lab}/ref"    # untimed, after each sample: nonzero exit voids it
    teardown = ""                            # untimed, once at the end
    metrics_from = "stdout-json"             # also read {"metric": value} from the last stdout line
    fingerprint = ["sort --version"]         # outputs that identify the system under test
    env = { LC_ALL = "C" }

    [factors]
    size = [1000, 100000]

    [arms.gnu]
    command = "sort -n {lab}/data/{cell.size}.txt -o {scratch}/out"
    [arms.parallel]
    command = "sort -n --parallel=4 {lab}/data/{cell.size}.txt -o {scratch}/out"

    [metrics.items_per_s]                    # metrics a command prints itself
    unit = "1/s"
    better = "higher"

Commands run through /bin/sh. Placeholders: {arm.KEY} and {cell.KEY} (the arm's and
cell's parameters), {scratch} (the run's working directory), {lab}, {run} and {arm_name}.
The timed metrics come from the kernel (wait4): wall_s, cpu_s, user_s, sys_s, max_rss_mib.
"""
import json
import os
import subprocess
import types

from . import Adapter, Metric, Void, declared_metrics, register
from .. import measure
from ..spec import SpecError

TIMED = {
    'wall_s': Metric('wall_s', 's', 'lower', 'Wall time'),
    'cpu_s': Metric('cpu_s', 's', 'lower', 'CPU time (user + system)'),
    'user_s': Metric('user_s', 's', 'lower', 'User CPU', positive=False),
    'sys_s': Metric('sys_s', 's', 'lower', 'System CPU', positive=False),
    'max_rss_mib': Metric('max_rss_mib', 'MiB', 'lower', 'Peak resident memory'),
}


def fill(template, ctx, arm, cell):
    values = {'arm': types.SimpleNamespace(**ctx.spec['arms'][arm]) if arm else None,
              'cell': types.SimpleNamespace(**cell) if cell is not None else None,
              'scratch': str(ctx.scratch), 'lab': str(ctx.lab.root), 'run': ctx.run_id, 'arm_name': arm}
    try:
        return template.format(**values)
    except (AttributeError, KeyError) as error:
        raise Void(f'cannot fill {template!r}: {error}') from None


def shell(ctx, template, arm=None, cell=None, what='command'):
    command = fill(template, ctx, arm, cell)
    env = dict(os.environ, **{k: str(v) for k, v in ctx.options.get('env', {}).items()})
    done = subprocess.run(command, shell=True, capture_output=True, text=True, env=env, cwd=ctx.scratch,
                          timeout=ctx.options.get('timeout_s', 3600))
    if done.returncode:
        raise Void(f'{what} exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}')
    return done.stdout


@register
class Command(Adapter):
    name = 'command'
    description = 'Times any shell command with wait4; optional setup, verification and self-reported metrics.'

    def validate(self, spec):
        for name, arm in spec['arms'].items():
            if 'command' not in arm:
                raise SpecError(f'{spec["id"]}: arm {name} needs a `command`')

    def metrics(self, spec):
        return {**TIMED, **declared_metrics(spec)}

    def fingerprint(self, ctx):
        out = {}
        for command in ctx.options.get('fingerprint', []):
            done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            out[command] = (done.stdout or done.stderr).strip()[:500]
        return out

    def preflight(self, ctx):
        problems = []
        for name, arm in ctx.spec['arms'].items():
            for cell in ctx.spec['cells']:
                try:
                    fill(arm['command'], ctx, name, cell['factors'])
                except Void as error:
                    problems.append(f'arm {name}, cell {cell["key"]}: {error}')
        for command in ctx.options.get('fingerprint', []):
            done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
            if done.returncode:
                problems.append(f'fingerprint command `{command}` exited {done.returncode}')
        return problems

    def prepare_run(self, ctx):
        if ctx.options.get('setup'):
            shell(ctx, ctx.options['setup'], what='setup')

    def prepare_sample(self, ctx, arm, cell):
        if ctx.options.get('before'):
            shell(ctx, ctx.options['before'], arm, cell, what='before')

    def measure(self, ctx, arm, cell):
        params = ctx.spec['arms'][arm]
        command = fill(params['command'], ctx, arm, cell)
        env = dict(os.environ, **{k: str(v) for k, v in {**ctx.options.get('env', {}), **params.get('env', {})}.items()})
        stdout = ctx.scratch / 'stdout'
        try:
            m = measure.run_timed(command, shell=True, stdout=stdout, env=env, cwd=ctx.scratch,
                                  timeout=params.get('timeout_s', ctx.options.get('timeout_s', 3600)))
        except TimeoutError as error:
            raise Void(str(error)) from None
        if m['exit']:
            raise Void(f'command exited {m["exit"]}: {m["stderr"].strip()[-300:]}')
        if m['suspended']:
            raise Void('the host was suspended during the command')
        out = {k: m[k] for k in TIMED}
        if ctx.options.get('metrics_from') == 'stdout-json':
            lines = [l for l in stdout.read_text(errors='replace').splitlines() if l.strip()]
            try:
                reported = json.loads(lines[-1]) if lines else {}
            except json.JSONDecodeError:
                raise Void('the last line of output is not a JSON object of metrics') from None
            out.update({k: v for k, v in reported.items() if isinstance(v, (int, float))})
        out['_command'] = command
        return out

    def finish_sample(self, ctx, arm, cell, metrics):
        if ctx.options.get('after'):
            shell(ctx, ctx.options['after'], arm, cell, what='verification')

    def finish_run(self, ctx):
        if ctx.options.get('teardown'):
            shell(ctx, ctx.options['teardown'], what='teardown')
