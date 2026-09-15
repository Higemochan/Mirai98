#!/usr/bin/env python3
"""Run pcatgl.on_start for real, with the outside world replaced, and judge
it by what it DID rather than by which lines it touched.

A NameError only exists when the line runs, so neither py_compile nor a diff
review can see one; this runs it.  And it asserts on behaviour -- that the
audio websockify was spawned with its numeric port -- because that is the
thing #69 got wrong, and an assertion on a line number would rot the first
time anyone edits the file.

Hermetic: no process is spawned, no port is bound, no signal is sent, and
nothing outside a temporary directory is written.

Usage: smoke_onstart.py <plugins/pcatgl.py>
Exit:  0 pass, 1 on_start raised, 2 on_start returned but did the wrong thing
"""
import importlib.util, os, shutil, subprocess, sys, tempfile, types

path = os.path.abspath(sys.argv[1])
ROOT = tempfile.mkdtemp(prefix="smoke-pcatgl-")
RUNTIME = os.path.join(ROOT, "runtime")
os.makedirs(RUNTIME, exist_ok=True)

spawned = []                       # every argv the plugin tried to launch

class FakeProc:
    _next = 900000
    def __init__(self, argv, *a, **k):
        FakeProc._next += 1
        self.pid = FakeProc._next
        spawned.append(list(argv))
    def poll(self):  return None    # "still running", so on_start carries on
    def wait(self, timeout=None): return 0

subprocess.Popen = FakeProc
subprocess.run = lambda *a, **k: types.SimpleNamespace(
    returncode=0, stdout=b"", stderr=b"")
shutil.which = lambda name: "/usr/bin/" + name   # take the optional branches
os.killpg = lambda *a: None
os.kill = lambda *a: None
def _no_child(*a, **k):
    raise ChildProcessError(10, "No child processes")
os.waitpid = _no_child

SRCDIR = None
if not path.endswith(".py"):                     # a loader needs the suffix
    SRCDIR = tempfile.mkdtemp(prefix="smoke-src-")
    tmp = os.path.join(SRCDIR, "under_test.py")
    shutil.copyfile(path, tmp)
    path = tmp

def cleanup():
    shutil.rmtree(ROOT, ignore_errors=True)
    if SRCDIR:
        shutil.rmtree(SRCDIR, ignore_errors=True)
spec = importlib.util.spec_from_file_location("pcatgl_under_test", path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

import time as _time
_time.sleep = lambda s: None
m.time.sleep = lambda s: None
m.XDG_RUNTIME_DIR = RUNTIME                      # keep /run/user/0 untouched
for name, repl in [("_wait_for", lambda *a, **k: True),
                   ("_sweep_orphans", lambda *a, **k: []),
                   ("_free_ports", lambda *a, **k: None),
                   ("_reap", lambda *a, **k: None),
                   ("_reap_exited", lambda *a, **k: None),
                   ("_kill_pids", lambda *a, **k: None),
                   ("_qmp_command", lambda *a, **k: {}),
                   ("_alive", lambda pid: True),
                   ("_pid_matches", lambda *a, **k: False)]:
    if hasattr(m, name):
        setattr(m, name, repl)

VNC, WS, QMP, AUDIO_WS = 5920, 5830, 4820, 4720

class Api:
    os = os
    CONFIG = {"datadir": ROOT, "pcatgl_soundfont": ""}
    LOOPBACK = True
    def ports_of(self, inst):   return (VNC, WS, QMP, AUDIO_WS)
    def inst_dir(self, inst):
        d = os.path.join(ROOT, "vm-%d" % inst["index"])
        os.makedirs(d, exist_ok=True)
        return d
    def disk_path(self, *a, **k):     return os.path.join(ROOT, "disk.img")
    def drive_backing(self, *a, **k): return os.path.join(ROOT, "disk.img")
    def win_short(self, s, *a, **k):  return s

open(os.path.join(ROOT, "disk.img"), "wb").write(b"\0" * 512)
inst = {"index": 0, "name": "SmokeTest", "memory": "256M", "snapshot": False,
        "vga": "std", "boot": "hd", "cd": "", "net": "", "extra": "",
        "fpslimit": 60, "machine": "pcat-gl",
        "disks": [{"dev": "hdd1", "ref": "disk.img"}]}

try:
    result = m.on_start(Api(), inst)
except BaseException as exc:                     # noqa: BLE001 - report any
    print("  FAIL: on_start raised %s: %s" % (type(exc).__name__, exc))
    cleanup()
    sys.exit(1)

print("  on_start returned %r, %d spawn(s)" % (result, len(spawned)))

def spawned_websockify(port, target_port):
    """A websockify launched for `port`, forwarding to `target_port`."""
    for argv in spawned:
        if (len(argv) >= 3 and os.path.basename(argv[0]) == "websockify"
                and argv[1] == str(port)
                and argv[2].endswith(":%d" % target_port)):
            return argv
    return None

bad = []
console = spawned_websockify(WS, VNC)
audio = spawned_websockify(AUDIO_WS, 4620 + inst["index"])
print("  console websockify %s" % (console or "NOT SPAWNED"))
print("  audio   websockify %s" % (audio or "NOT SPAWNED"))
if not console:
    bad.append("the console websockify was never spawned for %d->%d" % (WS, VNC))
if not audio:
    bad.append("the audio websockify was never spawned for %d->%d "
               "(this is what #69 broke)" % (AUDIO_WS, 4620 + inst["index"]))
if not any(os.path.basename(a[0]).startswith("qemu-system") for a in spawned):
    bad.append("QEMU itself was never spawned")

cleanup()
if bad:
    for b in bad:
        print("  FAIL: %s" % b)
    sys.exit(2)
print("  PASS")
