#!/usr/bin/env python3
"""#74 side by side: QEMU is killed, then the manager polls status the way
it does with a browser tab open.  Reports what is still standing.

Usage: repro74.py <plugins/pcatgl.py> <fakehelper.py>
"""
import importlib.util, os, shutil, signal, socket, subprocess, sys
import tempfile, time, types

PLUGIN, FAKE = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
INDEX, VNC, WS, QMP, AUDIO_WS = 77, 5977, 5887, 5877, 4797
ROOT = tempfile.mkdtemp(prefix="repro74-")
spec = importlib.util.spec_from_file_location("p", PLUGIN)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m._pactl = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="",
                                                 stderr="")
m._find_sink_modules = lambda n: []

class Api:
    os = os
    CONFIG = {"datadir": ROOT}
    def ports_of(self, i): return (VNC, WS, QMP, AUDIO_WS)
    def inst_dir(self, i):
        d = os.path.join(ROOT, "vm-%d" % i["index"])
        os.makedirs(d, exist_ok=True)
        return d

api, inst = Api(), {"index": INDEX, "name": "R", "machine": "pcat-gl"}
D = m._inst_dir(api, inst)
ARGV = {"weston": ["--socket=wl-pcatgl-%d" % INDEX],
        "xwayland": ["Xwayland", ":%d" % INDEX],
        "x11vnc": ["bash-loop", m._vncloop_marker(INDEX)],
        "websockify": ["--listen", str(WS), "127.0.0.1:%d" % VNC],
        "websockify_audio": [str(AUDIO_WS), "127.0.0.1:%d" % (4620 + INDEX)],
        "audio_relay": ["parec", "%s.audiorelay" % m._sink_for_index(INDEX)],
        "qemu": ["-qmp", "tcp:127.0.0.1:%d,server=on" % QMP]}
procs, pids = [], {}
for k, a in ARGV.items():
    p = subprocess.Popen([sys.executable, FAKE] + a, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    procs.append(p)
    pids[k] = p.pid
m._save_pids(D, pids)
time.sleep(0.6)

def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            d = f.read()
        return d[d.rindex(b")") + 2:d.rindex(b")") + 3] != b"Z"
    except (OSError, ValueError):
        return True

def bound(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port)); return False
    except OSError:
        return True
    finally:
        s.close()

print("  %s" % os.path.basename(PLUGIN))
print("  stack up: %d helpers, port %d bound: %s"
      % (len(pids) - 1, WS, bound(WS)))
os.kill(pids["qemu"], signal.SIGSEGV)
time.sleep(0.4)
print("  QEMU killed with SIGSEGV; polling is_up() 12x over 6s "
      "(the manager's status poll)")
for _ in range(12):
    m.is_up(api, inst)
    time.sleep(0.5)
standing = [k for k, p in pids.items() if k != "qemu" and alive(p)]
print("  STILL STANDING: %d/%d  %s"
      % (len(standing), len(pids) - 1, " ".join(sorted(standing)) or "(none)"))
print("  port %d still bound: %s" % (WS, bound(WS)))
print("  pids.json: %s" % ("cleared" if m._load_pids(D) == {} else "still names a generation"))
for p in procs:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        p.wait(timeout=2)
    except Exception:
        pass
shutil.rmtree(ROOT, ignore_errors=True)
