"""Experiments that are not run on the computer: a person, an instrument or a field
trial takes each sample, and the result is recorded afterwards.

    adapter = "manual"
    primary = "rise_pct"
    blocks = 8                     # e.g. eight baking days
    instructions = "Bake one loaf with {arm.flour}; proof it {cell.proof_h} h."

    [metrics.rise_pct]
    unit = "%"
    better = "higher"
    [metrics.crumb_score]
    unit = "1-10"
    better = "higher"

    [factors]
    proof_h = [4, 12]

    [arms.bread-flour]
    flour = "bread flour"
    [arms.all-purpose]
    flour = "all-purpose flour"
    instructions = "Use 2% less water with all-purpose flour."   # added to the shared text

The plan is the same randomized block design as for automatic benchmarks: each block
is one replicate of a cell (one day, one plot, one batch), and the arm order within it
is drawn from the seed. The runner turns every planned sample into a task; results are
entered on the dashboard's Tasks view, with `xlab record`, or from a CSV with
`xlab import`. Voiding a task (a dropped sample, a failed instrument) re-plans its block.
"""
import types

from . import Adapter, declared_metrics, register
from ..spec import SpecError


@register
class Manual(Adapter):
    name = 'manual'
    automatic = False
    description = 'Samples taken by people or instruments off the computer and recorded later.'

    def validate(self, spec):
        if not spec.get('metrics'):
            raise SpecError(f'{spec["id"]}: a manual experiment declares its metrics in [metrics.NAME] tables')

    def metrics(self, spec):
        return declared_metrics(spec)

    def instructions(self, spec, arm, cell):
        params = spec['arms'][arm]
        values = {'arm': types.SimpleNamespace(**params), 'cell': types.SimpleNamespace(**cell), 'arm_name': arm}
        parts = []
        for template in (spec.get('instructions'), params.get('instructions')):
            if template:
                try:
                    parts.append(template.format(**values))
                except (AttributeError, KeyError, IndexError):
                    parts.append(template)
        if not parts:
            settings = ', '.join(f'{k} = {v}' for k, v in params.items() if k != 'instructions')
            conditions = ', '.join(f'{k} = {v}' for k, v in cell.items())
            parts.append(f'Arm {arm}' + (f' ({settings})' if settings else '')
                         + (f' under {conditions}' if conditions else '') + '.')
        return ' '.join(parts)
