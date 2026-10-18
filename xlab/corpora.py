"""Deterministic input data, pinned by digest.

A corpus is one file. It is either generated from (kind, size, seed), so every host
builds byte-identical data, or packed from a real directory into a tar with a fixed
member order and neutral ownership. Either way its SHA-256 goes into every sample
record, and a spec can pin the digest it expects (`sha256 = "..."`) so an input that
drifted is refused rather than silently measured.

Generated kinds, from easy to hard to compress:

    zeros   one repeated byte; a ceiling for any codec
    logs    structured log lines (timestamps, levels, ids, paths, latencies)
    json    newline-delimited JSON records
    text    word-level English-like prose with a Zipf vocabulary
    floats  little-endian float64 telemetry: a noisy random walk
    mixed   1 MiB segments of the kinds above, interleaved
    random  incompressible bytes (SHAKE-256)

Names of the form KIND-SIZE (`text-32m`, `logs-256k`, `random-1g`) need no definition.
"""
import hashlib
import json
import math
import os
import pathlib
import random
import re
import struct
import tarfile

from . import util

GENERATOR = 1
KINDS = ('zeros', 'logs', 'json', 'text', 'floats', 'mixed', 'random')
SHORTHAND = re.compile(r'(?P<kind>[a-z]+)-(?P<size>[0-9]+)(?P<unit>[kmg])')
CHUNK = 1 << 20


class CorpusError(ValueError):
    pass


def definition(name, table=None):
    if table:
        return dict(table)
    match = SHORTHAND.fullmatch(name)
    if not match or match['kind'] not in KINDS:
        raise CorpusError(f'corpus {name!r} is not defined; use KIND-SIZE (e.g. text-32m, kinds: {", ".join(KINDS)}) '
                          f'or a [corpora.{name}] table with kind/size or path')
    return {'kind': match['kind'], 'size': f'{match["size"]}{match["unit"].upper()}iB'}


# -- generators: each yields byte chunks; all randomness comes from the seed -----

def _syllables(rng, n):
    onsets = ['', 'b', 'c', 'd', 'f', 'g', 'h', 'k', 'l', 'm', 'n', 'p', 'r', 's', 't', 'v', 'w', 'th', 'st', 'pr',
              'tr', 'ch', 'sh', 'gr', 'pl']
    vowels = ['a', 'e', 'i', 'o', 'u', 'ea', 'ou', 'ai', 'io']
    codas = ['', '', 'n', 'r', 's', 't', 'l', 'nd', 'st', 'ng', 'rt', 'm']
    words, seen = [], set()
    while len(words) < n:
        w = ''.join(rng.choice(onsets) + rng.choice(vowels) + rng.choice(codas) for _ in range(rng.choice((1, 1, 2, 2, 3))))
        if w not in seen:
            seen.add(w)
            words.append(w)
    return words


def gen_text(rng):
    words = _syllables(rng, 4000)
    weights = list(_cumulative(1.0 / (i + 1) ** 1.07 for i in range(len(words))))
    punctuation = ['.', '.', '.', ',', ',', ';', '?', '!']
    while True:
        picks = rng.choices(words, cum_weights=weights, k=20000)
        out, sentence = [], 0
        for i, w in enumerate(picks):
            if sentence == 0:
                w = w.capitalize()
            sentence += 1
            if sentence > 6 and rng.random() < 0.12:
                out.append(w + rng.choice(punctuation) + ('\n\n' if rng.random() < 0.08 else ' '))
                sentence = 0
            else:
                out.append(w + ' ')
        yield ''.join(out).encode()


def _cumulative(values):
    total = 0.0
    for v in values:
        total += v
        yield total


def gen_logs(rng):
    services = ['api', 'auth', 'billing', 'search', 'worker', 'gateway', 'scheduler']
    paths = ['/v1/items', '/v1/items/{id}', '/v1/users/{id}', '/v1/search', '/healthz', '/v1/orders', '/v1/login']
    levels = ['INFO'] * 20 + ['DEBUG'] * 6 + ['WARN'] * 3 + ['ERROR']
    t = 1_700_000_000_000
    while True:
        lines = []
        for _ in range(4000):
            t += rng.randint(0, 40)
            seconds, ms = divmod(t, 1000)
            path = rng.choice(paths).replace('{id}', str(rng.randint(1, 99999)))
            lines.append(f'{seconds}.{ms:03d} {rng.choice(levels):5} {rng.choice(services)}[{rng.randint(100, 120)}] '
                         f'req={rng.getrandbits(48):012x} {rng.choice(("GET", "GET", "GET", "POST", "PUT"))} {path} '
                         f'status={rng.choice((200, 200, 200, 200, 201, 204, 304, 400, 404, 500))} '
                         f'dur_ms={rng.expovariate(1 / 35):.1f}\n')
        yield ''.join(lines).encode()


def gen_json(rng):
    names = _syllables(rng, 800)
    i = 0
    while True:
        rows = []
        for _ in range(2000):
            i += 1
            rows.append(json.dumps({'id': i, 'name': rng.choice(names).capitalize() + ' ' + rng.choice(names).capitalize(),
                                    'active': rng.random() < 0.8, 'score': round(rng.gauss(50, 15), 2),
                                    'tags': rng.sample(names[:60], rng.randint(0, 4)),
                                    'created': 1_700_000_000 + i * 37, 'balance': rng.randint(-5000, 200000) / 100},
                                   separators=(',', ':')) + '\n')
        yield ''.join(rows).encode()


def gen_floats(rng):
    value, step = 0.0, 0
    while True:
        out = bytearray()
        for _ in range(8192):
            step += 1
            value += rng.gauss(0.0, 0.01) + 0.002 * math.sin(step / 500)
            out += struct.pack('<dd', step * 0.01, round(value, 4))
        yield bytes(out)


def gen_random(rng):
    key = rng.getrandbits(256).to_bytes(32, 'little')
    counter = 0
    while True:
        yield hashlib.shake_256(key + counter.to_bytes(8, 'little')).digest(CHUNK)
        counter += 1


def gen_zeros(rng):
    while True:
        yield b'\0' * CHUNK


def gen_mixed(rng):
    makers = [gen_text, gen_logs, gen_json, gen_floats, gen_random]
    streams = [make(random.Random(f'{rng.random()}:{i}')) for i, make in enumerate(makers)]
    buffers = [b''] * len(streams)
    while True:
        i = rng.randrange(len(streams))
        while len(buffers[i]) < CHUNK:
            buffers[i] += next(streams[i])
        yield buffers[i][:CHUNK]
        buffers[i] = buffers[i][CHUNK:]


GENERATORS = {'text': gen_text, 'logs': gen_logs, 'json': gen_json, 'floats': gen_floats, 'random': gen_random,
              'zeros': gen_zeros, 'mixed': gen_mixed}


def generate(path, kind, size, seed):
    rng = random.Random(f'xlab-corpus:{GENERATOR}:{kind}:{seed}')
    digest, written = hashlib.sha256(), 0
    temporary = pathlib.Path(str(path) + '.partial')
    with open(temporary, 'wb') as out:
        for chunk in GENERATORS[kind](rng):
            chunk = chunk[:size - written]
            out.write(chunk)
            digest.update(chunk)
            written += len(chunk)
            if written >= size:
                break
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)
    return digest.hexdigest()


def pack_directory(source, path):
    """A tar of a real tree with sorted members and neutral ownership; returns its SHA-256."""
    source = pathlib.Path(source).expanduser()
    if not source.is_dir():
        raise CorpusError(f'{source} is not a directory')

    def neutral(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info
    temporary = pathlib.Path(str(path) + '.partial')
    with tarfile.open(temporary, 'w', format=tarfile.PAX_FORMAT) as tar:
        for root, dirs, files in os.walk(source, followlinks=False):
            dirs.sort()
            for name in [*sorted(dirs), *sorted(files)]:
                full = pathlib.Path(root) / name
                tar.add(full, arcname=str(full.relative_to(source)), recursive=False, filter=neutral)
    os.replace(temporary, path)
    return util.sha256_file(path)


def ensure(lab, name, table=None, log=print):
    """Path and manifest of a corpus, building it if needed and checking its digest."""
    spec = definition(name, table)
    folder = lab.corpora
    data, manifest_path = folder / f'{name}.dat', folder / f'{name}.json'
    wanted = {'name': name, 'generator': GENERATOR, **{k: spec[k] for k in ('kind', 'size', 'seed', 'path') if k in spec}}
    if 'path' in spec:
        wanted['path'] = str(pathlib.Path(spec['path']).expanduser())
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    fresh = manifest is not None and data.exists() and all(manifest.get(k) == v for k, v in wanted.items()) \
        and data.stat().st_size == manifest['bytes']
    if fresh and 'path' not in spec:
        fresh = util.sha256_file(data) == manifest['sha256']
    if not fresh:
        if 'path' in spec:
            log(f'packing corpus {name} from {wanted["path"]}')
            digest = pack_directory(wanted['path'], data)
        else:
            if spec.get('kind') not in GENERATORS:
                raise CorpusError(f'corpus {name}: unknown kind {spec.get("kind")!r}')
            size = util.parse_size(spec['size'])
            log(f'generating corpus {name} ({spec["kind"]}, {util.human_bytes(size)})')
            digest = generate(data, spec['kind'], size, spec.get('seed', 0))
        manifest = {**wanted, 'bytes': data.stat().st_size, 'sha256': digest, 'created_utc': util.utc()}
        util.write_atomic(manifest_path, json.dumps(manifest, indent=1))
    if spec.get('sha256') and spec['sha256'] != manifest['sha256']:
        raise CorpusError(f'corpus {name} has digest {manifest["sha256"][:16]}..., but the spec pins '
                          f'{spec["sha256"][:16]}...')
    return data, manifest


def sample_ratio(path, level=3, sample=4 << 20):
    """A quick zlib ratio of the first few MiB: a hint of compressibility for reports."""
    import zlib
    with open(path, 'rb') as f:
        data = f.read(sample)
    return len(data) / max(1, len(zlib.compress(data, level)))

