"""A synthetic benchmark with known effects, for checking the statistics and trying the lab.

    adapter = "simulated"
    primary = "time_s"
    [options]
    sd = 0.04            # log-normal noise of each sample (about 4%)
    drift = 0.002        # slow multiplicative drift per sample, which blocking should cancel
    void_rate = 0.02     # share of samples that fail, to exercise retries
    sleep_s = 0.0        # make each sample take real time

    [arms.base]
    mean = 1.0
    [arms.faster]
    mean = 1.0
    effect = 0.9         # true ratio to base: 10% less time

A cell factor named `scale` multiplies every arm's mean in that cell.
"""
import math
import random
import time

from . import Adapter, Metric, Void, register


@register
class Simulated(Adapter):
    name = 'simulated'
    description = 'Synthetic samples with known effects (log-normal noise, drift, random voids).'

    def metrics(self, spec):
        return {'time_s': Metric('time_s', 's', 'lower', 'Time'),
                'throughput': Metric('throughput', 'ops/s', 'higher', 'Throughput'),
                'quality': Metric('quality', 'score', 'higher', 'Quality')}

    def prepare_run(self, ctx):
        ctx.state['count'] = 0

    def measure(self, ctx, arm, cell):
        options = ctx.options
        params = ctx.spec['arms'][arm]
        ctx.state['count'] = ctx.state.get('count', 0) + 1
        n = ctx.state['count']
        # from the plan's seed and the session (a resumed run must not repeat its earlier draws),
        # so a run with a given seed is reproducible anywhere
        rnd = random.Random(f'{ctx.state.get("seed", ctx.run_id)}:{ctx.state.get("session", 1)}:{n}:{arm}')
        if rnd.random() < options.get('void_rate', 0.0):
            raise Void('simulated failure')
        if options.get('sleep_s'):
            time.sleep(options['sleep_s'])
        scale = float(cell.get('scale', 1.0))
        drift = math.exp(options.get('drift', 0.0) * n)
        value = params.get('mean', 1.0) * params.get('effect', 1.0) * scale * drift
        value *= math.exp(rnd.gauss(0.0, options.get('sd', 0.05)))
        quality = params.get('quality', 50.0) * math.exp(rnd.gauss(0.0, options.get('sd', 0.05) / 2))
        return {'time_s': value, 'throughput': 1.0 / value, 'quality': quality}
