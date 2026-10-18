"""Measuring a process, and the state of the host around it.

A timed command is started with fork/exec (subprocess.Popen) and reaped with
os.wait4, which gives the child's own CPU time and peak RSS from the kernel. The
wall time is taken with time.monotonic_ns around exactly that interval; a
wall-clock reading beside it detects a host that slept during the sample.
"""
import os
import platform
import resource
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from . import __version__

RSS_SCALE = 1 if sys.platform == 'darwin' else 1024   # ru_maxrss: bytes on macOS, KiB on Linux


class Measured(dict):
    """wall_s, user_s, sys_s, cpu_s, max_rss_mib, exit, suspended."""


def run_timed(argv, stdin=None, stdout=None, stderr=None, env=None, cwd=None, timeout=None, shell=False):
    """Run one command and measure it. stdin/stdout may be paths or file objects."""
    opened = []

    def handle(target, mode):
        if isinstance(target, (str, os.PathLike)):
            f = open(target, mode)
            opened.append(f)
            return f
        return target

    out_handle = handle(stdout, 'wb') if stdout is not None else subprocess.DEVNULL
    in_handle = handle(stdin, 'rb') if stdin is not None else subprocess.DEVNULL
    # stderr goes to a file, never a pipe: a chatty child must not block on a full pipe.
    err_handle = handle(stderr, 'wb') if stderr is not None else tempfile.TemporaryFile()
    if stderr is None:
        opened.append(err_handle)
    try:
        wall0, mono0 = time.time(), time.monotonic_ns()
        child = subprocess.Popen(argv, stdin=in_handle, stdout=out_handle, stderr=err_handle, env=env, cwd=cwd,
                                 shell=shell, start_new_session=True)
        expired = threading.Event()

        def kill():
            expired.set()
            try:
                os.killpg(child.pid, 9)
            except ProcessLookupError:
                pass
        timer = threading.Timer(timeout, kill) if timeout else None
        if timer:
            timer.daemon = True
            timer.start()
        _, status, usage = os.wait4(child.pid, 0)   # blocking: no polling inside the timed interval
        mono1, wall1 = time.monotonic_ns(), time.time()
        if timer:
            timer.cancel()
        child.returncode = os.waitstatus_to_exitcode(status)
        if expired.is_set():
            raise TimeoutError(f'{argv if isinstance(argv, str) else argv[0]} exceeded {timeout} s')
        error_text = ''
        if stderr is None:
            err_handle.seek(0)
            error_text = err_handle.read()[-2000:].decode(errors='replace')
    finally:
        for f in opened:
            f.close()
    wall = (mono1 - mono0) / 1e9
    return Measured(wall_s=wall, user_s=usage.ru_utime, sys_s=usage.ru_stime, cpu_s=usage.ru_utime + usage.ru_stime,
                    max_rss_mib=usage.ru_maxrss * RSS_SCALE / 1048576, exit=child.returncode, stderr=error_text,
                    suspended=abs((wall1 - wall0) - wall) > 2.0 + 0.05 * wall)


# -- host state ----------------------------------------------------------------

def load_per_core():
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        return None


def cpu_busy(interval=0.5):
    """Busy share of all CPUs over a short interval (Linux /proc/stat), else None."""
    def read():
        with open('/proc/stat') as f:
            parts = [int(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return sum(parts), idle
    try:
        t0, i0 = read()
        time.sleep(interval)
        t1, i1 = read()
        return 1.0 - (i1 - i0) / max(1, t1 - t0)
    except OSError:
        return None


def temperature_c():
    """Highest thermal-zone reading on Linux; None elsewhere."""
    best = None
    try:
        for zone in os.listdir('/sys/class/thermal'):
            try:
                with open(f'/sys/class/thermal/{zone}/temp') as f:
                    value = int(f.read().strip()) / 1000
                best = value if best is None else max(best, value)
            except (OSError, ValueError):
                continue
    except OSError:
        return None
    return best


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def snapshot(path='.'):
    return {'load_per_core': load_per_core(), 'temp_c': temperature_c(), 'free_bytes': free_bytes(path)}


def host_fingerprint():
    info = {'hostname': socket.gethostname(), 'system': platform.system(), 'release': platform.release(),
            'machine': platform.machine(), 'python': platform.python_version(), 'cpus': os.cpu_count(),
            'xlab': __version__}
    try:
        if sys.platform == 'darwin':
            info['cpu'] = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True,
                                         text=True, timeout=5).stdout.strip()
            info['memory_bytes'] = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True,
                                                      text=True, timeout=5).stdout.strip() or 0)
        else:
            with open('/proc/cpuinfo') as f:
                for line in f:
                    if line.lower().startswith(('model name', 'hardware', 'cpu model')):
                        info['cpu'] = line.split(':', 1)[1].strip()
                        break
            info['memory_bytes'] = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    info['nofile'] = soft
    return info


def tool_version(argv):
    """First line of a tool's version output, or None if it is not installed."""
    if not shutil.which(argv[0]):
        return None
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout or done.stderr).strip().splitlines()
    return text[0].strip() if text else ''
