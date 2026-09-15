#!/usr/bin/env python3
"""Prove #74: when QEMU ends without being asked to, this instance's
display stack is taken down -- and nothing else is.

Real processes, real signals, real listening socket, on scratch ports far
above any instance that exists (index 77 -> 5977/5887/5877/4797/4697).
PulseAudio is stubbed out so the host's real sinks are never touched.

Usage: smoke_crashreap.py <plugins/pcatgl.py> [fakehelper.py]
Exit:  0 pass, 1 a case failed
"""
import importlib.util, json, os, signal, socket, subprocess, sys, tempfile
import time, types

PLUGIN = os.path.abspath(sys.argv[1])
FAKE = os.path.abspath(sys.argv[2] if len(sys.argv) > 2
                       else os.path.join(os.path.dirname(PLUGIN),
                                         "fakehelper.py"))
INDEX = 77
VNC, WS, QMP, AUDIO_WS = 5977, 5887, 5877, 4797
AUDIO_TCP = 4620 + INDEX
ROOT = tempfile.mkdtemp(prefix="crashreap-")

spec = importlib.util.spec_from_file_location("pcatgl_crashtest", PLUGIN)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# never speak to the host's PulseAudio
m._pactl = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="",
                                                 stderr="")
m._find_sink_modules = lambda name: []
m.CRASH_POLL = 0.2                      # so _watch_for_crash is testable

class Api:
    os = os
    CONFIG = {"datadir": ROOT}
    def ports_of(self, inst):
        return (VNC, WS, QMP, AUDIO_WS)
    def inst_dir(self, inst):
        d = os.path.join(ROOT, "vm-%d" % inst["index"])
        os.makedirs(d, exist_ok=True)
        return d

api = Api()
inst = {"index": INDEX, "name": "CrashReap", "machine": "pcat-gl"}
D = m._inst_dir(api, inst)

# argv fragments that make each fake look like the helper it stands in for
ARGV = {
    "weston":           ["--socket=wl-pcatgl-%d" % INDEX],
    "xwayland":         ["Xwayland", ":%d" % INDEX],
    "x11vnc":           ["bash-loop", m._vncloop_marker(INDEX)],
    "websockify":       ["--listen", str(WS), "127.0.0.1:%d" % VNC],
    "websockify_audio": [str(AUDIO_WS), "127.0.0.1:%d" % AUDIO_TCP],
    "audio_relay":      ["parec", "%s.audiorelay" % m._sink_for_index(INDEX)],
    "qemu":             ["-qmp", "tcp:127.0.0.1:%d,server=on" % QMP],
}
STRANGER = ["a-process-that-is-none-of-pcatgls-business"]

started = []

def launch(args):
    p = subprocess.Popen([sys.executable, FAKE] + args,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    started.append(p)
    return p.pid

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        return data[data.rindex(b")") + 2:data.rindex(b")") + 3] != b"Z"
    except (OSError, ValueError):
        return True

def build(keys, stranger_key=None):
    """Start a generation of fakes and write pids.json, as on_start does."""
    pids = {}
    for k in keys:
        pids[k] = launch(ARGV[k])
    if stranger_key:
        pids[stranger_key] = launch(STRANGER)
    m._save_pids(D, pids)
    time.sleep(0.5)                     # let them bind/settle
    return pids

def teardown_all():
    for p in started:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            pass

def port_taken(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()

def wait_until(pred, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.1)
    return pred()

bad = []
def check(label, ok, detail=""):
    print("  %-58s %s%s" % (label, "PASS" if ok else "FAIL",
                            "" if ok else "  <- " + detail))
    if not ok:
        bad.append(label)

# The scratch ports must actually be free.  A gate that quietly skipped
# when they were not would be a hole, so say so and fail instead.
_busy = [p for p in (VNC, WS, QMP, AUDIO_WS, AUDIO_TCP) if port_taken(p)]
if _busy:
    print("  FAIL: scratch ports already in use: %s" % _busy)
    print("  this test needs %d/%d/%d/%d/%d free"
          % (VNC, WS, QMP, AUDIO_WS, AUDIO_TCP))
    sys.exit(1)

try:
    # --- A: the whole stack goes down when QEMU is killed ----------------
    print("A. crash teardown takes the whole stack down")
    pids = build(list(ARGV))
    check("the fake websockify holds port %d" % WS, port_taken(WS))
    os.kill(pids["qemu"], signal.SIGSEGV)          # the case in hand
    time.sleep(0.3)
    m._crash_teardown(api, inst, "test: SIGSEGV")
    for k, pid in pids.items():
        check("%s (pid %d) is gone" % (k, pid), wait_until(
            lambda pid=pid: not alive(pid)), "still alive")
    check("port %d is free again" % WS, wait_until(
        lambda: not port_taken(WS)), "still bound")
    check("pids.json is cleared", m._load_pids(D) == {},
          repr(m._load_pids(D)))
    check("no zombie left behind", all(
        not os.path.exists("/proc/%d" % p) or
        open("/proc/%d/stat" % p, "rb").read().rsplit(b")", 1)[1].split()[0]
        != b"Z" for p in pids.values()), "a zombie survived")
    teardown_all(); started.clear()

    # --- B: a recycled pid is left alone --------------------------------
    print("B. a recorded pid that is no longer ours is left alone")
    pids = build(["qemu", "websockify"], stranger_key="weston")
    stranger = pids["weston"]
    os.kill(pids["qemu"], signal.SIGKILL)
    time.sleep(0.3)
    m._crash_teardown(api, inst, "test: recycled pid")
    check("the real websockify (pid %d) is gone" % pids["websockify"],
          wait_until(lambda: not alive(pids["websockify"])), "still alive")
    check("the stranger (pid %d) SURVIVES" % stranger,
          alive(stranger), "it was killed -- the guard failed")
    teardown_all(); started.clear()

    # --- C: a start or a stop in progress outranks the teardown ---------
    print("C. a start or a stop in progress outranks the teardown")
    pids = build(["qemu", "weston"])
    os.kill(pids["qemu"], signal.SIGKILL)
    time.sleep(0.3)
    m._starting.add(INDEX)
    m._crash_teardown(api, inst, "test: during start")
    check("weston survives while the index is starting",
          alive(pids["weston"]), "torn down during a start")
    m._starting.discard(INDEX)
    m._stopping.add(INDEX)
    m._crash_teardown(api, inst, "test: during stop")
    check("weston survives while the index is stopping",
          alive(pids["weston"]), "torn down during a stop")
    m._stopping.discard(INDEX)
    m._crash_teardown(api, inst, "test: unguarded")
    check("weston goes once neither holds the index",
          wait_until(lambda: not alive(pids["weston"])), "still alive")
    teardown_all(); started.clear()

    # --- D: is_up notices on its own ------------------------------------
    print("D. is_up notices a crashed generation and schedules the teardown")
    pids = build(["qemu", "weston", "websockify"])
    check("is_up is True while QEMU is there", m.is_up(api, inst) is True)
    os.kill(pids["qemu"], signal.SIGSEGV)
    time.sleep(0.3)
    check("is_up is False once QEMU is gone", m.is_up(api, inst) is False)
    check("weston is taken down by the is_up hook", wait_until(
        lambda: not alive(pids["weston"])), "still alive")
    check("pids.json is cleared by the is_up hook", wait_until(
        lambda: m._load_pids(D) == {}), repr(m._load_pids(D)))
    teardown_all(); started.clear()

    # --- E: the watch thread notices without anyone asking --------------
    print("E. the armed watch notices with nobody polling")
    pids = build(["qemu", "weston", "websockify"])
    import threading
    threading.Thread(target=m._watch_for_crash,
                     args=(api, inst, pids["qemu"], QMP), daemon=True).start()
    time.sleep(0.5)
    check("nothing happens while QEMU is alive", alive(pids["weston"]))
    os.kill(pids["qemu"], signal.SIGSEGV)
    check("weston is taken down by the watch", wait_until(
        lambda: not alive(pids["weston"])), "still alive")
    check("port %d is free again" % WS, wait_until(
        lambda: not port_taken(WS)), "still bound")
    teardown_all(); started.clear()

    # --- F: a watch armed for an older generation never fires -----------
    print("F. a watch armed for an older generation keeps its hands off")
    old = build(["qemu", "weston"])
    old_qemu = old["qemu"]
    threading.Thread(target=m._watch_for_crash,
                     args=(api, inst, old_qemu, QMP), daemon=True).start()
    time.sleep(0.4)
    os.kill(old_qemu, signal.SIGKILL)
    new = build(["qemu", "weston", "websockify"])   # overwrites pids.json
    time.sleep(1.5)
    check("the new generation's weston is untouched", alive(new["weston"]))
    check("the new generation's QEMU is untouched", alive(new["qemu"]))
    check("pids.json still names the new generation",
          m._load_pids(D).get("qemu") == new["qemu"],
          repr(m._load_pids(D)))
    teardown_all(); started.clear()
finally:
    teardown_all()
    import shutil
    shutil.rmtree(ROOT, ignore_errors=True)

print()
if bad:
    print("FAILED: %d case(s): %s" % (len(bad), "; ".join(bad)))
    sys.exit(1)
print("ALL PASS")
