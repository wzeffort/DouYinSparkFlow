"""No-browser Linux contract checks; safe in a network-none disposable container."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from spark_console.browser_runtime import BrowserSlot, admit_and_run

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    slot = BrowserSlot(root/'browser.lock')
    marker = root/'child.pid'
    # Both parent and grandchild ignore SIGTERM. Cleanup must escalate, reap,
    # then release admission. No network, database or real browser is involved.
    grandchild = 'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'
    child = (
        'import subprocess,sys,signal,time;from pathlib import Path;'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN);'
        f'p=subprocess.Popen([sys.executable,"-c",{grandchild!r}],start_new_session=True);'
        f'Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)'
    )
    outcome = admit_and_run(slot, [sys.executable,'-c',child], .8, lambda:False, lambda:None)
    assert outcome == 'job_timeout', outcome
    pid = int(marker.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError('grandchild survived cleanup')
    assert slot.acquire()
    second = subprocess.run([sys.executable,'-c',
        'from spark_console.browser_runtime import BrowserSlot;import sys;'
        's=BrowserSlot(sys.argv[1]);sys.exit(9 if s.acquire() else 0)',str(root/'browser.lock')])
    assert second.returncode == 0
    slot.release()
    done = root/'done'
    outcome = admit_and_run(slot,[sys.executable,'-c',f'from pathlib import Path;Path({str(done)!r}).touch()'],
                            5,lambda:False,lambda:None)
    assert outcome == 'completed' and done.exists()
    cancel_at = time.monotonic() + .8
    outcome = admit_and_run(slot,[sys.executable,'-c',child],30,
                            lambda:time.monotonic() >= cancel_at,lambda:None)
    assert outcome == 'cancelled'
    try:
        os.kill(int(marker.read_text()),0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError('cancelled detached grandchild survived')
    assert slot.acquire()
    slot.release()
print('LINUX_LOCK_TIMEOUT_DESCENDANT_CLEANUP_OK')
