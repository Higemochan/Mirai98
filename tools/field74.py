#!/usr/bin/env python3
"""#74 field test: the REAL display stack, on scratch indices.

Two full stacks are brought up through the plugin's own
_start_display_stack -- real weston, real Xwayland, real x11vnc under its
respawn loop, real websockify -- each with a real qemu-3dfx drawing into
its own X server.  Index 77's QEMU is then killed with SIGSEGV and the
manager's status poll is imitated.

77 must go down whole.  78 must not be touched, nor must the protected
pids, nor must vm-0's own leftovers (which are alive right now).

Nothing writes to any real instance directory: the fake api's inst_dir is
a temporary directory, so /storage/pc98/vm/* is never opened.

Usage: field74.py <plugins/pcatgl.py>
"""
import importlib.util, os, signal, socket, subprocess, sys, tempfile, time

PLUGIN = os.path.abspath(sys.argv[1])
QEMU = ("/storage/work/kvm98/src/qemu-3dfx-0b399bd-fix/build-cd/"
        "qemu-system-i386")
ROOT = tempfile.mkdtemp(prefix="field74-")
A, B = 77, 78                                   # the two scratch indices
PORTS = {i: (5900 + i, 5810 + i, 5800 + i, 4720 + i) for i in (A, B)}
PROTECTED = [4037526, 4037541, 3331262, 4135251]
# vm-0's own leftovers, alive at the time of writing -- bystanders that
# must still be alive afterwards (this test never signals them)
VM0_LEFTOVERS = [2762918, 2762919, 2762920]

spec = importlib.util.spec_from_file_location("pcatgl_field", PLUGIN)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Api:
    os = os
    CONFIG = {"datadir": ROOT}
    def ports_of(self, inst):
        return PORTS[inst["index"]]
    def inst_dir(self, inst):
        d = os.path.join(ROOT, "vm-%d" % inst["index"])
        os.makedirs(d, exist_ok=True)
        return d

api = Api()
INST = {i: {"index": i, "name": "field-%d" % i, "machine": "pcat-gl"}
        for i in (A, B)}
DIR = {i: m._inst_dir(api, INST[i]) for i in (A, B)}

bad, started_pgids = [], []

def check(label, ok, detail=""):
    print("  %-56s %s%s" % (label, "PASS" if ok else "FAIL",
                            "" if ok else "  <- " + detail))
    if not ok:
        bad.append(label)

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

def pgroup(pid):
    """Every pid in this pid's process group -- catches the x11vnc the
    respawn loop forked, which pids.json never names."""
    out = []
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % e, "rb") as f:
                fields = f.read().rsplit(b")", 1)[1].split()
            if int(fields[2]) == pid:            # pgrp
                out.append(int(e))
        except (OSError, ValueError, IndexError):
            continue
    return out

def wait_until(pred, secs):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.25)
    return pred()

def bring_up(i):
    vnc, ws, qmp, audio_ws = PORTS[i]
    log = open(os.path.join(DIR[i], "pcatgl.log"), "ab")
    pids = {}
    reason = m._start_display_stack(api, INST[i], DIR[i], PORTS[i], pids, log)
    if reason:
        raise SystemExit("  stack %d would not come up: %s" % (i, reason))
    env = dict(os.environ, DISPLAY=":%d" % (vnc - 5900),
               SDL_VIDEODRIVER="x11", XDG_RUNTIME_DIR=m.XDG_RUNTIME_DIR)
    env.pop("WAYLAND_DISPLAY", None)
    p = subprocess.Popen(
        [QEMU, "-M", "pc", "-m", "64", "-vga", "std", "-display", "sdl",
         "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp],
        stdout=log, stderr=log, env=env, start_new_session=True)
    pids["qemu"] = p.pid
    m._save_pids(DIR[i], pids)
    started_pgids.extend(pids.values())
    return pids, log

try:
    print("PRE-FLIGHT")
    free = [p for i in (A, B) for p in PORTS[i] + (4620 + i,)]
    check("all %d scratch ports free" % len(free),
          not any(bound(p) for p in free),
          str([p for p in free if bound(p)]))
    check("no X77/X78, no wl-pcatgl-77/78",
          not any(os.path.exists(p) for p in
                  ["/tmp/.X11-unix/X%d" % A, "/tmp/.X11-unix/X%d" % B,
                   os.path.join(m.XDG_RUNTIME_DIR, m._wl_socket(A)),
                   os.path.join(m.XDG_RUNTIME_DIR, m._wl_socket(B))]))
    toks = (m._instance_tokens(A, A, PORTS[A][0], PORTS[A][2])
            + m._instance_tokens(B, B, PORTS[B][0], PORTS[B][2]))
    hits = []
    for e in os.listdir("/proc"):
        if not e.isdigit() or int(e) == os.getpid():
            continue
        c = m._cmdline(int(e))
        if c and any(t in c for t in toks):
            hits.append(int(e))
    check("nothing on the box matches a 77/78 token", not hits, str(hits))
    check("all %d protected pids alive" % len(PROTECTED),
          all(alive(p) for p in PROTECTED),
          str([p for p in PROTECTED if not alive(p)]))
    print("  vm-0 leftovers alive: %s"
          % [p for p in VM0_LEFTOVERS if alive(p)])
    if bad:
        raise SystemExit("pre-flight failed; nothing started")

    print("\nBRING UP two real stacks + two real QEMU")
    pidsA, logA = bring_up(A)
    pidsB, logB = bring_up(B)
    for i, pids in ((A, pidsA), (B, pidsB)):
        print("  %d: %s" % (i, {k: v for k, v in pids.items()}))
    check("X%d appeared" % A, wait_until(
        lambda: os.path.exists("/tmp/.X11-unix/X%d" % A), 20))
    check("X%d appeared" % B, wait_until(
        lambda: os.path.exists("/tmp/.X11-unix/X%d" % B), 20))
    check("x11vnc found the QEMU window and bound %d" % PORTS[A][0],
          wait_until(lambda: bound(PORTS[A][0]), 40), "never listened")
    check("x11vnc found the QEMU window and bound %d" % PORTS[B][0],
          wait_until(lambda: bound(PORTS[B][0]), 40), "never listened")
    check("websockify %d bound" % PORTS[A][1], bound(PORTS[A][1]))
    check("is_up(%d) is True" % A, m.is_up(api, INST[A]) is True)
    check("is_up(%d) is True" % B, m.is_up(api, INST[B]) is True)
    groupA = {k: pgroup(v) for k, v in pidsA.items()}
    groupB = {k: pgroup(v) for k, v in pidsB.items()}
    allA = sorted({p for g in groupA.values() for p in g})
    allB = sorted({p for g in groupB.values() for p in g})
    print("  %d owns %d processes (incl. forked x11vnc/websockify children)"
          % (A, len(allA)))
    print("  %d owns %d processes" % (B, len(allB)))

    print("\nKILL: SIGSEGV to QEMU %d (pid %d)" % (A, pidsA["qemu"]))
    os.kill(pidsA["qemu"], signal.SIGSEGV)
    time.sleep(1.0)
    print("  imitating the manager's status poll for up to 40s")
    end = time.time() + 40
    while time.time() < end:
        m.is_up(api, INST[A])
        m.is_up(api, INST[B])
        if m._load_pids(DIR[A]) == {} and not any(alive(p) for p in allA):
            break
        time.sleep(0.5)

    print("\nASSERT: instance %d is gone" % A)
    for k, v in pidsA.items():
        check("  %s (pid %d) gone" % (k, v), not alive(v), "still alive")
    survivors = [p for p in allA if alive(p)]
    check("  every process of the group is gone (%d)" % len(allA),
          not survivors, "survivors: %s" % survivors)
    check("  port %d free" % PORTS[A][0], not bound(PORTS[A][0]))
    check("  port %d free" % PORTS[A][1], not bound(PORTS[A][1]))
    check("  /tmp/.X11-unix/X%d gone" % A,
          wait_until(lambda: not os.path.exists("/tmp/.X11-unix/X%d" % A), 10))
    check("  pids.json cleared", m._load_pids(DIR[A]) == {},
          repr(m._load_pids(DIR[A])))

    print("ASSERT: instance %d is untouched" % B)
    for k, v in pidsB.items():
        check("  %s (pid %d) alive" % (k, v), alive(v), "it was killed")
    check("  every process of the group alive (%d)" % len(allB),
          all(alive(p) for p in allB),
          "lost: %s" % [p for p in allB if not alive(p)])
    check("  port %d still bound" % PORTS[B][0], bound(PORTS[B][0]))
    check("  port %d still bound" % PORTS[B][1], bound(PORTS[B][1]))
    check("  X%d still there" % B, os.path.exists("/tmp/.X11-unix/X%d" % B))
    check("  pids.json still names its generation",
          m._load_pids(DIR[B]).get("qemu") == pidsB["qemu"])
    check("  is_up(%d) still True" % B, m.is_up(api, INST[B]) is True)

    print("ASSERT: bystanders untouched")
    for p in PROTECTED:
        check("  protected pid %d alive" % p, alive(p), "IT WAS KILLED")
    for p in VM0_LEFTOVERS:
        check("  vm-0 leftover pid %d alive" % p, alive(p), "IT WAS KILLED")

    print("\nCLEANUP: take %d down the same way" % B)
    os.kill(pidsB["qemu"], signal.SIGKILL)
    time.sleep(0.5)
    m._crash_teardown(api, INST[B], "field test cleanup")
    check("  %d fully gone" % B, wait_until(
        lambda: not any(alive(p) for p in allB), 20),
        "survivors: %s" % [p for p in allB if alive(p)])
finally:
    for pid in started_pgids:
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    for pid in started_pgids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
    import shutil
    shutil.rmtree(ROOT, ignore_errors=True)

print()
print("FAILED: %s" % "; ".join(bad) if bad else "ALL PASS")
sys.exit(1 if bad else 0)
