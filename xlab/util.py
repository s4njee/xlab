"""Shared helpers: time stamps, durable writes, hashing and a small TOML writer."""
import datetime
import hashlib
import json
import os
import pathlib
import re

SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def utc(moment=None):
    """ISO-8601 UTC with milliseconds, e.g. 2026-10-18T12:00:00.000Z."""
    return (moment or now()).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_utc(text):
    return datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))


def stamp(moment=None):
    """Compact UTC stamp for identifiers: 20261018T120000Z."""
    return (moment or now()).strftime('%Y%m%dT%H%M%SZ')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)


def write_atomic(path, text):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'w') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_durable(path, record):
    """Append one JSON line and fsync it, so a power cut loses at most a torn last line."""
    with open(path, 'a') as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path):
    out = []
    try:
        with open(path) as handle:
            for line in handle:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn final line from an interrupted write
    except FileNotFoundError:
        pass
    return out


SIZE = re.compile(r'\s*([0-9.]+)\s*([KMGT]?)(i?)B?\s*', re.I)


def parse_size(value):
    """'64MiB' -> 67108864, '10MB' -> 10000000, 4096 -> 4096."""
    if isinstance(value, int):
        return value
    match = SIZE.fullmatch(str(value))
    if not match:
        raise ValueError(f'not a size: {value!r}')
    number, unit, binary = float(match[1]), match[2].upper(), match[3]
    power = 'KMGT'.index(unit) + 1 if unit else 0
    return int(number * (1024 if binary or not unit else 1000) ** power) if unit else int(number)


def human_bytes(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(n) < 1024 or unit == 'TiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


# -- TOML writer (enough for specs and trees) ---------------------------------

def _toml_key(key):
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else json.dumps(key)


def _toml_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_toml_value(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{ ' + ', '.join(f'{_toml_key(k)} = {_toml_value(v)}' for k, v in value.items()) + ' }'
    raise TypeError(f'cannot write {type(value).__name__} to TOML')


def to_toml(data, prefix=''):
    """Scalars and inline values first, then sub-tables, then arrays of tables."""
    lines, tables, arrays = [], [], []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict) and value and all(isinstance(v, dict) for v in value.values()) or (
                isinstance(value, dict) and prefix == '' and key in ('arms', 'nodes', 'metrics', 'corpora', 'factors',
                                                                     'options', 'gates')):
            tables.append((key, value))
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value) and key in ('grid', 'branch', 'cells'):
            arrays.append((key, value))
        else:
            lines.append(f'{_toml_key(key)} = {_toml_value(value)}')
    out = '\n'.join(lines)
    for key, value in tables:
        name = f'{prefix}{_toml_key(key)}'
        if all(isinstance(v, dict) for v in value.values()) and value:
            for sub, table in value.items():
                out += f'\n\n[{name}.{_toml_key(sub)}]\n' + to_toml(table, f'{name}.{_toml_key(sub)}.')
        else:
            out += f'\n\n[{name}]\n' + to_toml(value, f'{name}.')
    for key, items in arrays:
        for item in items:
            out += f'\n\n[[{prefix}{_toml_key(key)}]]\n' + to_toml(item, f'{prefix}{_toml_key(key)}.')
    return out.strip() + '\n'
