"""`xlab up`: the runner, the tree conductor, the report and insight schedules and the
dashboard, in one long-lived process.

Only one runner may work a lab at a time (a lock file in LAB/data). Stopping the
process (Ctrl-C, SIGTERM, a reboot) ends the current sample cleanly; the run is
resumed at its first unfinished block next time. To keep it running across reboots,
use the launchd or systemd unit that `xlab service` prints.
"""
import fcntl
import os
import signal
import sys
import threading
import traceback

from . import adapters as adapters_mod
from . import insight, report, util
from .runner import Runner
from .trees import Conductor


class LockHeld(RuntimeError):
    pass


def take_lock(lab, name):
    path = lab.data / f'{name}.lock'
    handle = open(path, 'a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise LockHeld(f'another xlab {name} is running on this lab ({path})') from None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def housekeeping(lab, stop, adapters, log):
    store = lab.store()
    conductor = Conductor(lab, store, adapters)
    every = lab.section('conductor')['every_s']
    while not stop.is_set():
        for name, job in (('conductor', conductor.tick), ('insight', lambda: insight.tick(lab, store, adapters, log)),
                          ('report', lambda: report.tick(lab, store, adapters, log))):
            try:
                result = job()
                if name == 'report' and result:
                    log(f'report written: {result}')
            except Exception as error:
                log(f'{name} error: {error!r}\n{traceback.format_exc()}')
                store.event('error', f'{name}: {str(error)[:300]}')
        stop.wait(every)


def up(lab, runner=True, dashboard=True, host=None, port=None):
    def log(text):
        print(f'{util.utc()} {text}', flush=True)
    stop = threading.Event()
    locks = [take_lock(lab, 'daemon')]
    if runner:
        locks.append(take_lock(lab, 'runner'))

    def finish(*_):
        log('stopping after the current sample')
        stop.set()
    signal.signal(signal.SIGINT, finish)
    signal.signal(signal.SIGTERM, finish)
    adapters = adapters_mod.load_all(lab)
    threads = []
    if runner:
        work = Runner(lab, lab.store(), adapters, log=log, stop=stop)
        threads.append(threading.Thread(target=work.loop, name='runner', daemon=True))
    threads.append(threading.Thread(target=housekeeping, args=(lab, stop, adapters, log), name='housekeeping',
                                    daemon=True))
    server = None
    if dashboard:
        from .dashboard.server import make_server
        settings = lab.section('dashboard')
        server = make_server(lab, host or settings['host'], port or settings['port'], adapters)
        threads.append(threading.Thread(target=server.serve_forever, name='dashboard', daemon=True))
        log(f'dashboard on http://{server.server_address[0]}:{server.server_address[1]}/')
    for t in threads:
        t.start()
    log(f'xlab up: lab {lab.name} at {lab.root}')
    try:
        while not stop.wait(1.0):
            pass
    finally:
        if server:
            server.shutdown()
        for t in threads:
            if t.name == 'runner':
                t.join(timeout=600)
        for handle in locks:
            handle.close()
    return 0


def service(lab, executable=None):
    """A launchd plist (macOS) or systemd user unit (Linux) that keeps `xlab up` running."""
    exe = executable or sys.executable
    if sys.platform == 'darwin':
        label = f'dev.xlab.{lab.root.name}'
        return label + '.plist', f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{exe}</string><string>-m</string><string>xlab</string>
    <string>--lab</string><string>{lab.root}</string><string>up</string></array>
  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>{os.path.dirname(os.path.dirname(__file__))}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{lab.data}/daemon.log</string>
  <key>StandardErrorPath</key><string>{lab.data}/daemon.log</string>
</dict></plist>
"""
    return 'xlab.service', f"""[Unit]
Description=xlab experiment lab ({lab.root})
After=network-online.target

[Service]
ExecStart={exe} -m xlab --lab {lab.root} up
Environment=PYTHONPATH={os.path.dirname(os.path.dirname(__file__))}
Restart=on-failure
KillMode=mixed
TimeoutStopSec=900

[Install]
WantedBy=default.target
"""
