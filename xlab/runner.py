"""The runner: works the queue one run at a time, unattended.

For every sample of an automatic run it
  1. checks the system under test has not changed (the adapter's fingerprint),
  2. waits for the host to settle (load, temperature, free disk: the lab's [gates]),
  3. lets the adapter prepare (untimed), measure (timed) and verify (untimed),
  4. writes one self-describing record, fsynced, before the next sample starts.

A void sample ends its block attempt; the block is retried in a fresh order. A run
interrupted by a crash or reboot is resumed at its first unfinished block. A run that
fails does not stop the queue, but `halt_after_failures` failures in a row pause the
runner for `halt_minutes`, so a broken host does not burn through the backlog.

Manual runs never occupy the runner: it opens their tasks and moves on.
"""
import datetime
import json
import os
import shutil
import socket
import threading
import time
import traceback

from . import adapters as adapters_mod
from . import measure, runs, standing, util
from .adapters import Context, Void


class Cancelled(Exception):
    pass


def fingerprint_sha(fp):
    return util.sha256_bytes(util.canonical(fp).encode())[:16]


class Runner:
    def __init__(self, lab, store=None, adapters=None, log=None, stop=None):
        self.lab = lab
        self.store = store or lab.store()
        self.adapters = adapters or adapters_mod.load_all(lab)
        self.settings = lab.section('runner')
        self.gates = lab.section('gates')
        self.stop = stop or threading.Event()
        self._log = log or (lambda text: print(f'{util.utc()} {text}', flush=True))
        self.current = None

    # -- status ----------------------------------------------------------------------
    def log(self, text, run_id=None):
        self._log(text)
        if run_id:
            with open(runs.run_dir(self.lab, run_id) / 'run.log', 'a') as f:
                f.write(f'{util.utc()} {text}\n')

    def status(self, state, detail=None, **extra):
        self.store.put('runner', {'state': state, 'detail': detail, 'run': self.current, 'heartbeat': util.utc(),
                                  'pid': os.getpid(), 'host': socket.gethostname(), **extra})

    # -- queue -----------------------------------------------------------------------
    def recover(self):
        """Runs left 'running' by a runner that died go back to the front of the queue."""
        for run in self.store.runs(state='running'):
            self.store.set_run(run['id'], state='queued', priority=self.store.front_priority(),
                               detail='resumed after interruption')
            self.store.event('resume', f'{run["experiment"]}: interrupted run requeued', run['id'])

    def halted(self):
        until = self.store.get('halted_until')
        if until and util.parse_utc(until) > util.now():
            return until
        if until:
            self.store.put('halted_until', None)
        return None

    def next_run(self):
        if self.store.get('paused'):
            return None
        queue = self.store.queue()
        return queue[0] if queue else None

    def step(self):
        """Do one unit of work. Returns False when there was nothing to do."""
        runs.refresh_manual(self.store)
        halted = self.halted()
        if halted:
            self.status('halted', f'too many failures in a row; retrying after {halted}')
            return False
        if not self.store.get('paused'):
            for run_id in standing.queue_due(self.lab, self.store, self.adapters):
                self.log(f'standing run queued: {run_id}')
        run = self.next_run()
        if run is None:
            self.status('paused' if self.store.get('paused') else 'idle')
            return False
        if not run['automatic']:
            runs.open_manual(self.lab, self.store, run['id'], self.adapters)
            return True
        self.execute(run['id'])
        if run.get('lane') == 'standing':
            standing.check(self.lab, self.store, run['id'], self.adapters)
        return True

    def loop(self):
        self.recover()
        self.log('runner started')
        while not self.stop.is_set():
            try:
                worked = self.step()
            except Exception as error:   # never let one bad run kill the runner
                self.log(f'runner error: {error!r}\n{traceback.format_exc()}')
                worked = False
            if not worked:
                self.stop.wait(self.settings['poll_s'])
        self.status('stopped')

    def work_until_empty(self):
        self.recover()
        while not self.stop.is_set() and self.step():
            pass

    # -- one run -------------------------------------------------------------------------
    def execute(self, run_id):
        lab, store = self.lab, self.store
        plan = runs.load_plan(lab, run_id)
        spec = runs.load_run_spec(lab, run_id, self.adapters)
        adapter = self.adapters[spec['adapter']]
        folder = runs.run_dir(lab, run_id)
        scratch = lab.scratch / run_id
        scratch.mkdir(parents=True, exist_ok=True)
        self.current = run_id
        run = store.run(run_id)
        store.set_run(run_id, state='running', started=run['started'] or util.utc(), attempts=run['attempts'] + 1)
        store.event('start', f'{spec["id"]} started', run_id)
        self.log(f'start {run_id}', run_id)
        ctx = Context(lab, run_id, spec, folder, scratch, lambda text: self.log(text, run_id))
        ctx.state['seed'], ctx.state['session'] = plan['seed'], run['attempts'] + 1
        outcome, detail = 'complete', None
        try:
            self._preflight(ctx, adapter, plan, spec)
            adapter.prepare_run(ctx)
            fp = {'host': measure.host_fingerprint(), 'adapter': adapter.fingerprint(ctx)}
            env_path = folder / 'env.json'
            history = json.loads(env_path.read_text()) if env_path.exists() else []
            history.append({'utc': util.utc(), 'fingerprint': fp, 'sha': fingerprint_sha(fp), 'gates': self.gates})
            util.write_atomic(env_path, json.dumps(history, indent=1))
            if len(history) > 1 and history[-2]['sha'] != history[-1]['sha']:
                store.event('warning', f'{spec["id"]}: the system under test changed between sessions of this run '
                            f'({history[-2]["sha"]} -> {history[-1]["sha"]})', run_id)
            self._warmup(ctx, adapter, plan, spec, fp)
            abandoned = self._blocks(ctx, adapter, plan, spec, fp)
            if abandoned:
                detail = f'{abandoned} block(s) never completed'
            adapter.finish_run(ctx)
        except Cancelled:
            outcome, detail = 'cancelled', 'cancelled by operator'
        except StopRequested:
            outcome = 'stopped'
        except RunFailed as failure:
            outcome, detail = 'failed', str(failure)
        except Exception as error:
            outcome, detail = 'failed', f'{type(error).__name__}: {error}'
            self.log(traceback.format_exc(), run_id)
        finally:
            self.current = None
            if not self.settings.get('keep_scratch'):
                shutil.rmtree(scratch, ignore_errors=True)
        if outcome == 'stopped':
            store.set_run(run_id, state='queued', priority=store.front_priority(), detail='stopped; will resume')
            self.log(f'stopped {run_id}; it will resume', run_id)
            return
        store.set_run(run_id, state=outcome, ended=util.utc(), detail=detail)
        store.event(outcome, f'{spec["id"]} {outcome}' + (f': {detail}' if detail else ''), run_id)
        self.log(f'{outcome} {run_id}' + (f': {detail}' if detail else ''), run_id)
        failures = store.get('consecutive_failures', 0)
        failures = failures + 1 if outcome == 'failed' else 0
        store.put('consecutive_failures', failures)
        if failures >= self.settings['halt_after_failures']:
            until = util.utc(util.now() + datetime.timedelta(minutes=self.settings['halt_minutes']))
            store.put('halted_until', until)
            store.put('consecutive_failures', 0)
            store.event('halt', f'{failures} failed runs in a row: the runner waits until {until}')

    def _preflight(self, ctx, adapter, plan, spec):
        """Prove the run is what it says before building or measuring anything."""
        problems = []
        if spec['sha256'] != plan['spec_sha256']:
            problems.append('the spec in the run directory does not match the planned one')
        problems += adapter.preflight(ctx)
        util.write_atomic(ctx.run_dir / 'preflight.json', json.dumps(
            {'utc': util.utc(), 'problems': problems}, indent=1))
        if problems:
            for p in problems:
                ctx.log(f'preflight: {p}')
            raise RunFailed('preflight: ' + '; '.join(problems[:3]) + (f' (+{len(problems) - 3} more)'
                                                                         if len(problems) > 3 else ''))

    def _done_blocks(self, run_id):
        """{(replicate, cell_key)} with one attempt in which every arm is valid; and attempts used."""
        by_attempt, used = {}, {}
        for s in self.store.samples(run_id, state=('valid', 'void')):
            if s['unit'].startswith('w'):
                continue
            key = (s['block'], s['cell'])
            used[key] = max(used.get(key, -1), s['attempt'])
            if s['state'] == 'valid':
                by_attempt.setdefault((key, s['attempt']), set()).add(s['arm'])
        return by_attempt, used

    def _warmup(self, ctx, adapter, plan, spec, fp):
        for k in range(spec.get('warmup', 0)):
            for cell in plan['cells']:
                for arm in plan['arms']:
                    unit = f'w{k}.c{cell["index"]}.{arm}'
                    if self.store.sample(ctx.run_id, unit):
                        continue
                    self._sample(ctx, adapter, plan, spec, fp, unit, -1, cell, arm, 0, 0, warmup=True)

    def _blocks(self, ctx, adapter, plan, spec, fp):
        by_attempt, used = self._done_blocks(ctx.run_id)
        arms = set(plan['arms'])
        complete = {key for (key, _), got in by_attempt.items() if got == arms}
        abandoned, void_streak = 0, 0
        total = len(plan['sequence'])
        for index, step in enumerate(plan['sequence']):
            cell = plan['cells'][step['cell']]
            key = (step['replicate'], cell['key'])
            if key in complete:
                continue
            first = used.get(key, -1) + 1      # an interrupted attempt is never reused
            done = False
            for attempt in range(first, plan['retries'] + 1):
                self._check_control(ctx.run_id)
                current = adapter.fingerprint(ctx)
                if fingerprint_sha(current) != fingerprint_sha(fp['adapter']):
                    raise RunFailed(f'the system under test changed mid-run: {fp["adapter"]} -> {current}')
                self.status('running', f'{spec["id"]}: block {index + 1}/{total} ({cell["key"]}), attempt {attempt + 1}',
                            block=index + 1, blocks=total)
                ok = True
                for position, arm in enumerate(step['orders'][attempt]):
                    unit = f'b{step["replicate"]:03d}.c{step["cell"]}.t{attempt}.{arm}'
                    record = self._sample(ctx, adapter, plan, spec, fp, unit, step['replicate'], cell, arm,
                                          attempt, position)
                    if record['state'] != 'valid':
                        ok = False
                        break
                    self._check_control(ctx.run_id)
                if ok:
                    done, void_streak = True, 0
                    break
                void_streak += 1
                valid_so_far = self.store.counts(ctx.run_id).get('valid', 0)
                if not valid_so_far and void_streak >= 3:
                    last = self.store.samples(ctx.run_id, state='void', limit=1)
                    reason = last[-1]['void_reason'] if last else 'unknown'
                    raise RunFailed(f'every sample so far was void (last: {reason})')
            if not done:
                abandoned += 1
                self.store.event('block', f'{spec["id"]} block {step["replicate"]} {cell["key"]} abandoned after '
                                 f'{plan["retries"] + 1} attempts', ctx.run_id)
        return abandoned

    def _check_control(self, run_id):
        if self.store.get('cancel') == run_id:
            self.store.put('cancel', None)
            raise Cancelled()
        if self.stop.is_set():
            raise StopRequested()

    # -- one sample ------------------------------------------------------------------------
    def _settle(self, ctx):
        """Wait until the host is quiet. Returns (snapshot, seconds waited).

        No sample has started yet, so a busy host is not a void sample: the runner waits,
        says so, and only gives up on the run after `max_wait_h` hours."""
        g = self.gates
        start, warned = time.monotonic(), False
        while True:
            snap = measure.snapshot(ctx.scratch)
            problems = []
            if g.get('max_load_per_core') is not None and snap['load_per_core'] is not None \
                    and snap['load_per_core'] > g['max_load_per_core']:
                problems.append(f'load {snap["load_per_core"]:.2f}/core > {g["max_load_per_core"]}')
            if g.get('max_temp_c') is not None and snap['temp_c'] is not None and snap['temp_c'] > g['max_temp_c']:
                problems.append(f'temperature {snap["temp_c"]:.0f} C > {g["max_temp_c"]}')
            if g.get('min_free_gib') is not None and snap['free_bytes'] is not None \
                    and snap['free_bytes'] < g['min_free_gib'] * 2 ** 30:
                raise RunFailed(f'only {util.human_bytes(snap["free_bytes"])} free on the scratch disk')
            waited = time.monotonic() - start
            if not problems:
                if warned:
                    self.store.event('settled', f'host settled after {waited:.0f} s', ctx.run_id)
                return snap, waited
            if waited > g.get('settle_timeout_s', 120) and not warned:
                warned = True
                self.store.event('warning', 'waiting for the host to settle: ' + '; '.join(problems), ctx.run_id)
            if waited > g.get('max_wait_h', 12) * 3600:
                raise RunFailed('the host has not settled for ' + f'{waited / 3600:.1f} h: ' + '; '.join(problems))
            self.status('waiting', 'host busy: ' + '; '.join(problems))
            if self.stop.wait(5.0):
                raise StopRequested()

    def _sample(self, ctx, adapter, plan, spec, fp, unit, replicate, cell, arm, attempt, position, warmup=False):
        record = {'schema': 1, 'run': ctx.run_id, 'experiment': spec['id'], 'unit': unit, 'block': replicate,
                  'cell': cell['key'], 'cell_index': cell['index'], 'factors': cell['factors'], 'arm': arm,
                  'arm_params': spec['arms'][arm], 'attempt': attempt, 'position': position, 'warmup': warmup,
                  'seed': plan['seed'], 'spec_sha256': spec['sha256'], 'fingerprint': fingerprint_sha(fp),
                  'metrics': {}, 'details': {}, 'void_reason': None}
        record['started_utc'] = util.utc()
        try:
            before, waited = self._settle(ctx)
            record['gates'] = {'before': before, 'settle_wait_s': round(waited, 3)}
            adapter.prepare_sample(ctx, arm, cell['factors'])
            wall0, mono0 = time.time(), time.monotonic()
            values = adapter.measure(ctx, arm, cell['factors'])
            elapsed = time.monotonic() - mono0
            if abs((time.time() - wall0) - elapsed) > 2.0 + 0.05 * elapsed:
                raise Void('the host was suspended during the sample')
            record['elapsed_s'] = elapsed
            metrics = {k: v for k, v in values.items() if not k.startswith('_')}
            record['details'] = {k[1:]: v for k, v in values.items() if k.startswith('_')}
            adapter.finish_sample(ctx, arm, cell['factors'], metrics)
            defined = adapter.metrics(spec)
            for name, value in metrics.items():
                if not isinstance(value, (int, float)) or value != value:
                    raise Void(f'{name} is not a number: {value!r}')
            primary = metrics.get(spec['primary'])
            if primary is not None and defined[spec['primary']].positive and primary <= 0:
                raise Void(f'{spec["primary"]} must be positive, got {primary}')
            if spec['primary'] not in metrics:
                raise Void(f'the adapter did not report the primary metric {spec["primary"]}')
            record['metrics'] = metrics
            record['state'] = 'warmup' if warmup else 'valid'
            record['gates']['after'] = measure.snapshot(ctx.scratch)
        except Void as reason:
            record['state'], record['void_reason'] = 'void', str(reason)
        except (Cancelled, StopRequested, RunFailed):
            raise
        except Exception as error:
            record['state'], record['void_reason'] = 'void', f'adapter error: {type(error).__name__}: {error}'
            ctx.log(traceback.format_exc())
        record['ended_utc'] = util.utc()
        if warmup and record['state'] == 'void':
            record['state'] = 'warmup'
        util.append_durable(ctx.run_dir / 'samples.jsonl', record)
        self.store.add_sample(record)
        shown = record['metrics'].get(spec['primary'])
        ctx.log(f'{unit} {record["state"]}' + (f' {spec["primary"]}={shown:.6g}' if shown is not None else '')
                + (f' ({record["void_reason"]})' if record['void_reason'] else ''))
        return record


class RunFailed(Exception):
    pass


class StopRequested(Exception):
    pass
