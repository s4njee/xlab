"""Drafting decision trees from a research question, with an AI model.

`xlab tree draft "QUESTION" --adapter compression` asks the configured model to turn
a question into a pre-registered plan: a tree of experiments with the rules between
them, and the spec of every experiment. The draft is parsed and validated exactly as
a hand-written one (unknown metrics, arms, codecs or rule keys are rejected, and the
errors are sent back once for a repair). It lands in LAB/drafts/TREE-ID/ and is never
queued by itself: read it, edit it, then `xlab tree start drafts/TREE-ID/tree.toml`.
"""
import json
import sys
import tomllib

from . import adapters as adapters_mod
from . import ai, spec as spec_mod, trees, util

SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['rationale', 'tree_toml', 'specs'],
    'properties': {
        'rationale': {'type': 'string'},
        'tree_toml': {'type': 'string'},
        'specs': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['filename', 'toml'],
            'properties': {'filename': {'type': 'string'}, 'toml': {'type': 'string'}}}},
    },
}

SYSTEM = """You design experiments for an automated lab. Given a research question, write a pre-registered decision tree and the experiment specs it runs, in the lab's TOML formats documented below.

Design principles:
- Start with a cheap screening experiment that covers the plausible settings; follow with a confirmatory experiment (a `promote` node with more blocks) only for what the screen selects.
- Every experiment has one primary metric that answers its question, a control arm that is the current default or baseline, and a delta (the smallest ratio change worth acting on).
- Choose cells (factors) that test whether a finding generalises (e.g. different inputs), and keep the number of arms x cells modest so a block finishes in reasonable time.
- Rules must refer only to arms, metrics and factors that the specs define.
- Use 6 blocks for screening and 12 or more for confirmation unless the question needs otherwise.
- Spec filenames are plain names ending in .toml; the tree refers to them by that filename.
- Return TOML only inside the JSON string fields; no Markdown fences.

The user message holds the question, the adapter's documentation and metrics, and the format references."""


def _doc(cls):
    module = sys.modules.get(cls.__module__)
    return (module.__doc__ or '') if module else ''


def context(lab, adapter_name, adapters):
    adapter = adapters[adapter_name]
    from . import spec as spec_doc, trees as tree_doc
    return {
        'adapter': {'name': adapter_name, 'description': adapter.describe(), 'documentation': _doc(type(adapter))},
        'spec_format': spec_doc.__doc__, 'tree_format': tree_doc.__doc__,
        'existing_experiments': sorted(p.stem for p in lab.experiments.glob('*.toml')) if lab.experiments.exists() else [],
    }


def check(lab, draft, adapters):
    """Write a draft to a temporary place and validate it; returns (tree_id, errors)."""
    errors = []
    try:
        tree = tomllib.loads(draft['tree_toml'])
    except tomllib.TOMLDecodeError as error:
        return None, [f'tree TOML: {error}']
    tree_id = tree.get('id', 'draft')
    if not util.SAFE.fullmatch(str(tree_id)):
        return None, ['the tree id must be a plain name']
    folder = lab.root / 'drafts' / str(tree_id)
    folder.mkdir(parents=True, exist_ok=True)
    for item in draft['specs']:
        name = item['filename']
        if not util.SAFE.fullmatch(name) or not name.endswith('.toml'):
            errors.append(f'spec filename {name!r} must be a plain name ending in .toml')
            continue
        try:
            spec_mod.load(text=item['toml'], adapters=adapters)
        except spec_mod.SpecError as error:
            errors.append(f'{name}: {error}')
        util.write_atomic(folder / name, item['toml'])
    util.write_atomic(folder / 'tree.toml', draft['tree_toml'])
    if not errors:
        try:
            trees.load(lab, folder / 'tree.toml')
        except trees.TreeError as error:
            errors.append(str(error))
    return tree_id, errors


def draft(lab, question, adapter_name, adapters=None, log=print):
    adapters = adapters or adapters_mod.load_all(lab)
    if adapter_name not in adapters:
        raise ValueError(f'unknown adapter {adapter_name}')
    settings = lab.section('ai')
    if not ai.configured(settings):
        raise ai.AIError('no AI provider is configured ([ai] provider in lab.toml)')
    user = json.dumps({'question': question, **context(lab, adapter_name, adapters)})
    answer, meta = ai.complete(settings, SYSTEM, user, SCHEMA, name='tree_draft')
    tree_id, errors = check(lab, answer, adapters)
    if errors:
        log('draft had problems; asking for a repair: ' + '; '.join(errors))
        repair = json.dumps({'question': question, 'previous_draft': answer, 'validation_errors': errors,
                             **context(lab, adapter_name, adapters)})
        answer, meta = ai.complete(settings, SYSTEM + '\n\nFix every validation error in the previous draft.',
                                   repair, SCHEMA, name='tree_draft')
        tree_id, errors = check(lab, answer, adapters)
    folder = lab.root / 'drafts' / str(tree_id or 'draft')
    util.write_atomic(folder / 'DRAFT.md', f"# Draft tree {tree_id}\n\nQuestion: {question}\n\nModel: {meta['model']}\n\n"
                      f"{answer['rationale']}\n\n" + ('Validation errors:\n' + '\n'.join(f'- {e}' for e in errors)
                                                     if errors else 'Validated: the tree and every spec load.\n'))
    return folder, errors
