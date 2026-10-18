"""Compression benchmarks: the trade-off between speed and compression ratio.

    adapter = "compression"
    primary = "compress_s"
    pareto = [["compress_mbps", "ratio"], ["decompress_mbps", "ratio"]]

    [options]
    decompress = true            # also time decompression (and always verify the round trip)
    cache = "warm"               # read the input before each sample; "cold" runs drop_caches first
    drop_caches = "sync && sudo purge"

    [factors]
    corpus = ["text-32m", "logs-32m", "mixed-32m"]

    [corpora.holochart]          # a real tree, packed to a tar once and pinned by digest
    path = "~/projects/holochartv2"

    [arms.zstd-3]
    codec = "zstd"
    level = 3
    threads = 1                  # optional; codecs without threads ignore it (and say so in validation)

    [[grid]]
    name = "xz-{level}"
    codec = "xz"
    level = [1, 6, 9]

Codecs: zstd (levels -7..22, `long = 27` for a long window), xz (0..9, `extreme = true`),
gzip (1..9), pigz (1..9, threads), bzip2 (1..9), brotli (0..11, `window = 24`), lz4 (1..12).
Each sample streams the corpus through the codec's own command-line tool from a file
to a file, timed with wait4 (wall, CPU, peak RSS). The output is then decompressed
(timed separately) and its SHA-256 checked against the corpus; a mismatch voids the
sample. Corpora are files in the lab; see xlab.corpora for the generated kinds.
"""
import json
import os
import pathlib
import shutil
import subprocess

from . import Adapter, Metric, Void, register
from .. import corpora, measure, util
from ..spec import SpecError


def _zstd(arm):
    level, threads = int(arm.get('level', 3)), int(arm.get('threads', 1))
    argv = ['zstd', '-q', '-c', f'-T{threads}']
    argv += [f'--fast={-level}'] if level < 0 else (['--ultra'] if level > 19 else []) + [f'-{level}']
    if arm.get('long'):
        argv.append(f'--long={int(arm["long"])}')
    return argv, ['zstd', '-q', '-d', '-c'] + ([f'--long={int(arm["long"])}'] if arm.get('long') else [])


def _xz(arm):
    level = f'-{int(arm.get("level", 6))}' + ('e' if arm.get('extreme') else '')
    return ['xz', '-q', '-c', level, f'-T{int(arm.get("threads", 1))}'], ['xz', '-q', '-d', '-c']


def _brotli(arm):
    argv = ['brotli', '-c', '-q', str(int(arm.get('level', 6)))]
    if arm.get('window'):
        argv += ['-w', str(int(arm['window']))]
    return argv, ['brotli', '-d', '-c']


CODECS = {
    'zstd': {'levels': (-7, 22), 'threads': True, 'build': _zstd, 'version': ['zstd', '--version'], 'ext': 'zst'},
    'xz': {'levels': (0, 9), 'threads': True, 'build': _xz, 'version': ['xz', '--version'], 'ext': 'xz'},
    'gzip': {'levels': (1, 9), 'threads': False, 'ext': 'gz', 'version': ['gzip', '--version'],
             'build': lambda a: (['gzip', '-c', '-n', f'-{int(a.get("level", 6))}'], ['gzip', '-d', '-c'])},
    'pigz': {'levels': (1, 9), 'threads': True, 'ext': 'gz', 'version': ['pigz', '--version'],
             'build': lambda a: (['pigz', '-c', '-n', f'-{int(a.get("level", 6))}', '-p', str(int(a.get('threads', 1)))],
                                 ['pigz', '-d', '-c'])},
    'bzip2': {'levels': (1, 9), 'threads': False, 'ext': 'bz2', 'version': ['bzip2', '--help'],
              'build': lambda a: (['bzip2', '-c', f'-{int(a.get("level", 9))}'], ['bzip2', '-d', '-c'])},
    'brotli': {'levels': (0, 11), 'threads': False, 'build': _brotli, 'version': ['brotli', '--version'], 'ext': 'br'},
    'lz4': {'levels': (1, 12), 'threads': False, 'ext': 'lz4', 'version': ['lz4', '--version'],
            'build': lambda a: (['lz4', '-q', '-c', f'-{int(a.get("level", 1))}'], ['lz4', '-q', '-d', '-c'])},
}

METRICS = {
    'compress_s': Metric('compress_s', 's', 'lower', 'Compression time'),
    'compress_cpu_s': Metric('compress_cpu_s', 's', 'lower', 'Compression CPU time', positive=False),
    'compress_rss_mib': Metric('compress_rss_mib', 'MiB', 'lower', 'Compression peak memory'),
    'compress_mbps': Metric('compress_mbps', 'MB/s', 'higher', 'Compression speed (input MB per second)'),
    'decompress_s': Metric('decompress_s', 's', 'lower', 'Decompression time'),
    'decompress_cpu_s': Metric('decompress_cpu_s', 's', 'lower', 'Decompression CPU time', positive=False),
    'decompress_rss_mib': Metric('decompress_rss_mib', 'MiB', 'lower', 'Decompression peak memory'),
    'decompress_mbps': Metric('decompress_mbps', 'MB/s', 'higher', 'Decompression speed (output MB per second)'),
    'output_bytes': Metric('output_bytes', 'B', 'lower', 'Compressed size'),
    'ratio': Metric('ratio', 'x', 'higher', 'Compression ratio (input / output)'),
}


@register
class Compression(Adapter):
    name = 'compression'
    description = 'Command-line compressors on pinned corpora: time, CPU, memory, ratio, verified round trip.'

    def validate(self, spec):
        for name, arm in spec['arms'].items():
            codec = CODECS.get(arm.get('codec'))
            if codec is None:
                raise SpecError(f'{spec["id"]}: arm {name} needs `codec`, one of {", ".join(CODECS)}')
            low, high = codec['levels']
            if 'level' in arm and not low <= int(arm['level']) <= high:
                raise SpecError(f'{spec["id"]}: arm {name}: {arm["codec"]} levels are {low}..{high}')
            if int(arm.get('threads', 1)) != 1 and not codec['threads']:
                raise SpecError(f'{spec["id"]}: arm {name}: {arm["codec"]} is single-threaded; drop `threads`')
        for cell in spec['cells']:
            if 'corpus' not in cell['factors']:
                raise SpecError(f'{spec["id"]}: every cell needs a `corpus` factor')
            name = cell['factors']['corpus']
            try:
                corpora.definition(name, spec.get('corpora', {}).get(name))
            except corpora.CorpusError as error:
                raise SpecError(f'{spec["id"]}: {error}') from None

    def metrics(self, spec):
        out = dict(METRICS)
        if not spec.get('options', {}).get('decompress', True):
            out = {k: v for k, v in out.items() if not k.startswith('decompress')}
        return out

    def fingerprint(self, ctx):
        seen = sorted({arm['codec'] for arm in ctx.spec['arms'].values()})
        return {codec: measure.tool_version(CODECS[codec]['version']) for codec in seen}

    def preflight(self, ctx):
        problems = []
        versions = self.fingerprint(ctx)
        for codec, version in versions.items():
            if version is None:
                problems.append(f'{codec} is not installed on this host')
        # every arm's exact command line must round-trip a small buffer with the installed tools
        sample = (b'xlab preflight ' * 4096) + bytes(range(256)) * 64
        for name, params in ctx.spec['arms'].items():
            if versions.get(params['codec']) is None:
                continue
            compress, decompress = CODECS[params['codec']]['build'](params)
            try:
                packed = subprocess.run(compress + list(params.get('extra', [])), input=sample, capture_output=True,
                                        timeout=60)
                restored = subprocess.run(decompress, input=packed.stdout, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as error:
                problems.append(f'arm {name}: {error}')
                continue
            if packed.returncode or restored.returncode or restored.stdout != sample:
                detail = (packed.stderr or restored.stderr).decode(errors='replace').strip()[-200:]
                problems.append(f'arm {name}: `{" ".join(compress)}` does not round-trip'
                                + (f' ({detail})' if detail else ''))
        # inputs: definitions, sources and pins; and room on disk for them and for the outputs
        need, largest = 0, 0
        for name in sorted({c['factors']['corpus'] for c in ctx.spec['cells']}):
            table = ctx.spec.get('corpora', {}).get(name)
            try:
                definition = corpora.definition(name, table)
            except corpora.CorpusError as error:
                problems.append(str(error))
                continue
            manifest_path = ctx.lab.corpora / f'{name}.json'
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
            if 'path' in definition:
                source = pathlib.Path(definition['path']).expanduser()
                if not source.is_dir():
                    problems.append(f'corpus {name}: source {source} is not a directory')
                size = manifest['bytes'] if manifest else sum(
                    f.stat().st_size for f in source.rglob('*') if f.is_file() and not f.is_symlink()) if source.is_dir() else 0
            else:
                size = util.parse_size(definition['size'])
            if definition.get('sha256') and manifest and manifest['sha256'] != definition['sha256']:
                problems.append(f'corpus {name}: the built corpus has digest {manifest["sha256"][:16]}..., the spec pins '
                                f'{definition["sha256"][:16]}...')
            if not (manifest and (ctx.lab.corpora / f'{name}.dat').exists()):
                need += size
            largest = max(largest, size)
        free = measure.free_bytes(ctx.lab.data)
        wanted = need + 2 * largest + (1 << 30)
        if free is not None and free < wanted:
            problems.append(f'{util.human_bytes(free)} free, but the corpora and outputs need about '
                            f'{util.human_bytes(wanted)}')
        return problems

    def prepare_run(self, ctx):
        for codec, version in self.fingerprint(ctx).items():
            if version is None:
                raise RuntimeError(f'{codec} is not installed on this host')
        ctx.state['corpora'] = {}
        for cell in ctx.spec['cells']:
            name = cell['factors']['corpus']
            if name not in ctx.state['corpora']:
                path, manifest = corpora.ensure(ctx.lab, name, ctx.spec.get('corpora', {}).get(name), ctx.log)
                ctx.state['corpora'][name] = (path, manifest)
        manifests = {k: m for k, (_, m) in ctx.state['corpora'].items()}
        util.write_atomic(ctx.run_dir / 'corpora.json', json.dumps(manifests, indent=1))

    def prepare_sample(self, ctx, arm, cell):
        path, _ = ctx.state['corpora'][cell['corpus']]
        cache = cell.get('cache', ctx.options.get('cache', 'warm'))
        if cache == 'cold':
            command = ctx.options.get('drop_caches')
            if not command:
                raise Void('a cold-cache cell needs options.drop_caches')
            if os.system(command) != 0:
                raise Void('drop_caches failed')
        else:
            with open(path, 'rb') as f:   # warm: the input is in the page cache before timing
                while f.read(1 << 22):
                    pass

    def measure(self, ctx, arm, cell):
        params = ctx.spec['arms'][arm]
        codec = CODECS[params['codec']]
        compress, decompress = codec['build'](params)
        path, manifest = ctx.state['corpora'][cell['corpus']]
        packed = ctx.scratch / f'out.{codec["ext"]}'
        restored = ctx.scratch / 'restored'
        try:
            c = measure.run_timed(compress + list(params.get('extra', [])), stdin=path, stdout=packed,
                                  timeout=ctx.options.get('timeout_s', 3600))
            if c['exit']:
                raise Void(f'{params["codec"]} exited {c["exit"]}: {c["stderr"].strip()[-200:]}')
            if c['suspended']:
                raise Void('the host was suspended during compression')
            size_in, size_out = manifest['bytes'], packed.stat().st_size
            out = {'compress_s': c['wall_s'], 'compress_cpu_s': c['cpu_s'], 'compress_rss_mib': c['max_rss_mib'],
                   'compress_mbps': size_in / 1e6 / c['wall_s'], 'output_bytes': size_out,
                   'ratio': size_in / max(1, size_out), '_argv': compress, '_input_sha256': manifest['sha256']}
            d = measure.run_timed(decompress, stdin=packed, stdout=restored, timeout=ctx.options.get('timeout_s', 3600))
            if d['exit']:
                raise Void(f'{params["codec"]} -d exited {d["exit"]}: {d["stderr"].strip()[-200:]}')
            if d['suspended']:
                raise Void('the host was suspended during decompression')
            if restored.stat().st_size != size_in or util.sha256_file(restored) != manifest['sha256']:
                raise Void('the round trip did not reproduce the input')
            if ctx.options.get('decompress', True):
                out.update(decompress_s=d['wall_s'], decompress_cpu_s=d['cpu_s'], decompress_rss_mib=d['max_rss_mib'],
                           decompress_mbps=size_in / 1e6 / d['wall_s'])
            return out
        except TimeoutError as error:
            raise Void(str(error)) from None
        finally:
            for f in (packed, restored):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass

    def describe(self):
        return {**super().describe(), 'codecs': {k: {'levels': v['levels'], 'threads': v['threads'],
                                                       'installed': bool(shutil.which(v['version'][0]))}
                                                 for k, v in CODECS.items()}}
