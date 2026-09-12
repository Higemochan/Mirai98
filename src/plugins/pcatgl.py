"""DOS/V PC with 3D acceleration (pcat-gl) -- a qemu-3dfx guest whose GL
output goes to a real X server rather than QEMU's own -vnc.

Where the plain "pcat" machine is an ordinary QEMU machine the core starts
with qemu_argv + -vnc + QMP, this one cannot be: qemu-3dfx draws its 3D
through GLX straight into an X window and never touches -vnc, so there is
nothing on the QEMU side for the core's VNC/QMP-driven start to show.  It
is therefore an *engine* (PluginAPI.add_engine, the same shape box86 uses),
and it owns a display stack of its own -- exactly glx-stack.sh's:

    weston (headless, GL renderer)     the compositor, on renderD128
      -> rootful Xwayland :N           a real X server QEMU's SDL draws to
        -> x11vnc (loopback)           exports that display as VNC
          -> websockify                wraps it as the console websocket
    qemu-3dfx  -display sdl            the guest, its GL landing on :N

Everything is derived from the instance's own index so two instances never
collide: DISPLAY :N and the VNC/WS/QMP ports come from ports_of, the
compositor socket from the index.  The QEMU runs with its cwd set to the
instance's own directory, where this writes a fresh mesagl.cfg every start
(the wrapper reads it from the cwd, and a cfg left in the wrong directory
was a real source of confusion, so the log is checked for proof it took).

If the display stack cannot be brought up, the instance is not left dead:
it falls back to a plain VGA (-vnc) start of the same binary, records why,
and the hardware card says so.
"""

import json
import os
import socket
import subprocess
import threading
import time


# The qemu-3dfx build.  Not fixed in code: a deploy points CONFIG
# ("qemu_3dfx" in mirai98.json) or the environment at whichever build is
# current (build-n/build-m/... are still moving), and this default is only
# the last resort so a bench with neither set still runs.
QEMU_3DFX_DEFAULT = ("/storage/work/kvm98/src/qemu-3dfx-0b399bd-fix/"
                     "build-n/qemu-system-i386")

# The GL display stack's own geometry.  Xwayland gets this, x11vnc exports
# whatever the guest actually draws inside it.
SCREEN_W = 1024
SCREEN_H = 768

# weston/Xwayland need a runtime dir; root's is the one that exists in the
# container, the same one glx-stack.sh uses.
XDG_RUNTIME_DIR = "/run/user/0"

PCATGL_MACHINE_LABEL = "DOS/V PC + 3dfx (GL)"
PCATGL_CPU_LABEL = "Pentium III (SSE/SSE2/SSE3/SSSE3)"
PCATGL_SOUND_LABEL = "Sound Blaster 16"
PCATGL_VGA_LABELS = {"std": "std (Bochs VBE)", "cirrus": "Cirrus Logic"}

# The no-PnP SeaBIOS, same as plain pcat: Win98 reads QEMU's PCI bus as a
# PnP BIOS and wedges a fresh install without it.
PCATGL_PC_BIOS = "/opt/mirai98-src/qemu-pc98towns/pc-bios"

_BOOT_LETTER = {"hd": "c", "fd": "a", "cd": "d"}

# mesagl.cfg defaults.  FpsLimit is exposed as a field (0 == unlimited);
# the other three are the settled 3dfx-on-KVM values.  Order is not
# significant to the wrapper, but is kept stable for a readable file.
MESAGL_DEFAULTS = (
    ("FpsLimit", "60"),
    ("AlwaysBlit", "1"),
    ("SwapWatchdogSec", "5"),
    ("ReadbackPresent", "1"),
)


def register(api):
    api.add_machine("pcat-gl", platform="dosv")
    api.add_engine("pcat-gl", {
        "on_start": lambda inst: on_start(api, inst),
        "on_stop": lambda inst: on_stop(api, inst),
        "on_reset": lambda inst: pcatgl_reset(api, inst),
        "is_up": lambda inst: is_up(api, inst),
        "pid": lambda inst: _load_pids(_inst_dir(api, inst)).get("qemu"),
        "thumbnail": lambda inst, png: pcatgl_thumbnail(api, inst, png),
    })
    api.machine_sanitize("pcat-gl", pcatgl_sanitize)
    api.machine_shown("pcat-gl", lambda inst: pcatgl_hardware(api, inst))
    # "vga" and "fpslimit" are this machine's own fields.  "" is accepted
    # so the global-by-name validators stay harmless for other machines,
    # which never carry these fields.
    api.add_field("vga", lambda v: None if v in ("", "std", "cirrus")
                  else "unknown video card")
    api.add_field("fpslimit", _validate_fpslimit)
    # its disks share the one dosv shelf with box86 and plain pcat; a blank
    # raw disk is already offered there by pcat's own registration, so it
    # is not repeated here.


def _validate_fpslimit(v):
    if v in ("", None):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return "frame limit must be a whole number of FPS (0 = unlimited)"
    return None if n >= 0 else "frame limit cannot be negative"


def pcatgl_sanitize(record):
    """Pin the fixed choices and drop what this machine does not wire.
    Sound is always SB16, firmware the SeaBIOS, ACPI always on (the
    qemu-3dfx guest wrapper reads the ACPI PM timer at 0x608 as its time
    source and freezes without it), and accel always KVM (3dfx on TCG is
    pointless).  The PC-98/host-only fields are cleared."""
    record["sound"] = "none"
    record["bios"] = "real"
    record["accel"] = "kvm"
    record["acpi"] = "on"
    for key in ("mount", "serial", "parallel", "gpib"):
        record[key] = ""
    return None


def _inst_dir(api, inst):
    """This instance's own directory -- the QEMU cwd, where mesagl.cfg and
    the helper pid file and logs live.  Under the instance's own state
    dir (api.inst_dir, /storage/.../vm/vm-N), beside box86's, NOT under
    the deployment tree: a redeploy must not carry off or wipe a running
    instance's cfg, pids and logs."""
    d = api.os.path.join(api.inst_dir(inst), "pcatgl")
    api.os.makedirs(d, exist_ok=True)
    return d


def _qemu_bin(api):
    return (api.CONFIG.get("qemu_3dfx")
            or api.os.environ.get("QEMU_3DFX_PATH")
            or QEMU_3DFX_DEFAULT)


def _nopnp_bios(api):
    """The no-PnP SeaBIOS beside the data dir, or None when it is absent
    (then the stock BIOS is used and nothing is added)."""
    path = api.os.path.join(api.CONFIG["datadir"], "pc-bios",
                            "bios-nopnp.bin")
    return path if api.os.path.exists(path) else None


def _write_mesagl_cfg(api, inst, d):
    """Write mesagl.cfg into the QEMU cwd every start.

    The wrapper reads this from its current directory, so it must be the
    instance's own dir and must be rewritten each time (a stale cfg from
    another instance, or an old FpsLimit, is exactly the confusion this
    avoids).  FpsLimit comes from the instance; 0 is a valid value the
    wrapper reads as unlimited, so a present "0" is written through rather
    than treated as unset.
    """
    fps = inst.get("fpslimit")
    if fps in ("", None):
        fps = "60"
    lines = []
    for key, default in MESAGL_DEFAULTS:
        value = fps if key == "FpsLimit" else default
        lines.append("%s,%s" % (key, value))
    path = api.os.path.join(d, "mesagl.cfg")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _drive_args(api, inst):
    """IDE hard disks (index 0/1) and the CD (secondary master, index 2),
    plus both floppies.  The CD and the floppies exist even with an empty
    tray, so a disc or disk can be put in from the Media row over QMP
    without a restart -- exactly as plain pcat does, which is what makes
    the edit form's "can be swapped while running" true here too."""
    argv = []
    for index, key in enumerate([k for k in ("hdd1", "hdd2") if inst.get(k)]):
        argv += ["-drive", "if=ide,index=%d,%s"
                 % (index, api.drive_backing(api.disk_path(inst, key)))]
    cd = "if=ide,index=2,media=cdrom,readonly=on"
    if inst.get("cd"):
        cd += "," + api.drive_backing(api.disk_path(inst, "cd"))
    argv += ["-drive", cd]
    for index, key in enumerate(("fdd1", "fdd2")):
        drive = "if=floppy,index=%d" % index
        if inst.get(key):
            drive += "," + api.drive_backing(api.disk_path(inst, key))
        argv += ["-drive", drive]
    return argv


def _qemu_argv(api, inst, ports, gl):
    """The qemu-3dfx command line.  gl=True routes output to the X server
    (SDL, no -vnc); gl=False is the fallback -- the same binary and disks,
    but a plain -vnc console (with the websocket and SB16 mix the core
    console expects) so the instance still runs, and is reachable, when
    the GL stack could not be brought up.  ACPI is on either way (the
    guest wrapper needs it)."""
    vnc, ws, qmp_port, _audio = ports
    display = vnc - 5900
    host = "127.0.0.1" if api.LOOPBACK else "0.0.0.0"
    vga = inst.get("vga") or "std"
    boot = _BOOT_LETTER.get(inst.get("boot") or "hd", "c")
    argv = [_qemu_bin(api),
            "-L", api.win_short(PCATGL_PC_BIOS),
            "-L", api.win_short(api.CONFIG["datadir"])]
    nopnp = _nopnp_bios(api)
    if nopnp:
        argv += ["-bios", api.win_short(nopnp)]
    argv += [
        # ACPI on: the qemu-3dfx guest wrapper reads the ACPI PM timer
        # (I/O 0x608) as its time source and freezes without a PIIX4 PM
        "-M", "pc-i440fx-9.2,acpi=on,accel=kvm:tcg",
        "-cpu", "pentium3,+sse2,+sse3,+ssse3",
        "-m", inst.get("memory") or "256M",
        "-rtc", "base=localtime",
        "-vga", vga + ",retrace=precise",
        "-audiodev", "none,id=snd",
        "-device", "sb16,audiodev=snd",
        # QMP stays on loopback whatever LOOPBACK is: it is unauthenticated
        # and the production service runs without --loopback, so binding
        # the host address would put it on the LAN.  _qmp_command connects
        # to 127.0.0.1 to match.
        "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
    ]
    if gl:
        # GL output to the X server; SDL is qemu-3dfx's GLX carrier
        argv += ["-display", "sdl"]
    else:
        # fallback: an ordinary VNC console with the browser websocket and
        # the SB16 mix on the stream, the same shape plain pcat uses, so
        # the core console (ports_of's ws) connects and on_start's
        # _port_open(ws) wait is satisfied rather than timing out
        argv += ["-display", "none",
                 "-vnc", "%s:%d,websocket=%d,audiodev=snd"
                 % (host, display, ws)]
    if inst.get("snapshot"):
        # the record's Snapshot flag: throw guest writes away at shutdown
        # rather than letting them reach the original disk
        argv.append("-snapshot")
    argv += _drive_args(api, inst)
    # net "" is isolated; net "nat" is an RTL8139 behind QEMU's own NAT
    # (Win98 has a driver).  bridge is not offered by this machine.
    if inst.get("net") == "nat":
        argv += ["-netdev", "user,id=lan", "-device", "rtl8139,netdev=lan"]
    else:
        # given no -nic/-netdev, QEMU fits a default NIC on a slirp user
        # net that can reach the host; an isolated guest must not have one
        argv += ["-nic", "none"]
    argv += ["-boot", "order=%s" % boot]
    if inst.get("extra"):
        argv += inst["extra"].split()
    return argv


# --- helper process bookkeeping (same shape as box86) ----------------------

def _pids_path(d):
    return os.path.join(d, "pids.json")


def _load_pids(d):
    try:
        with open(_pids_path(d), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_pids(d, pids):
    try:
        with open(_pids_path(d), "w", encoding="utf-8") as f:
            json.dump(pids, f)
    except OSError:
        pass


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # a zombie answers kill(pid, 0) like a live process; read the state so
    # an exited-but-unreaped helper does not read as running for good
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read()
        state = data[data.rindex(b")") + 2:data.rindex(b")") + 3]
        return state != b"Z"
    except (OSError, ValueError):
        return True


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wl_socket(index):
    return "wl-pcatgl-%d" % index


def _fallback_marker(d):
    return os.path.join(d, "gl_fallback")


def _sweep_orphans(d, index, display_num, vnc, qmp_port, log):
    """Kill anything still holding this instance's display, compositor
    socket or ports.  Reaching on_start means the core found this instance
    not running, so every match is a leftover of a previous generation --
    every token is derived from this instance's own index.  They survive a
    restart of the manager because each helper is in its own session.

    Number-final tokens carry a trailing space so "-display :2 " does not
    match ":21"; the joined cmdline's final NUL becomes that space, so a
    number at the very end still matches.
    """
    tokens = (
        "%s " % _wl_socket(index),          # weston --socket=wl-pcatgl-N
        "Xwayland :%d " % display_num,      # the X server
        "-display :%d " % display_num,      # x11vnc's -display
        "-rfbport %d " % vnc,               # x11vnc
        "127.0.0.1:%d " % vnc,              # websockify -> x11vnc
        # qemu's QMP arg is "tcp:127.0.0.1:<port>,server=on,..." -- the
        # trailing comma, not a space, is what follows the port here, so
        # the space-terminated form never matched and a leftover QEMU
        # (still holding the disk) went unswept.
        "tcp:127.0.0.1:%d," % qmp_port,     # qemu QMP
    )
    mine = os.getpid()
    signaled = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        try:
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if not cmd or not any(t in cmd for t in tokens):
            continue
        try:
            os.killpg(pid, 15)
            signaled.append(pid)
            log.write(("[pcatgl] swept orphan pid %d: %s\n"
                       % (pid, cmd[:200])).encode())
            log.flush()
        except OSError:
            pass
    return signaled


def _kill_pids(pids):
    """SIGTERM every helper this instance started, then SIGKILL whatever is
    left after a moment.  QEMU first (so the guest is asked to stop before
    the X server it draws into is pulled away), then out to the compositor.
    By process GROUP: each helper called setsid (start_new_session), and a
    supervisor-loop entry is a shell whose child would outlive the shell.
    """
    for key in ("qemu", "websockify", "x11vnc", "xwayland", "weston"):
        pid = pids.get(key)
        if pid and _alive(pid):
            try:
                os.killpg(pid, 15)
            except OSError:
                pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(_alive(pid) for pid in pids.values()):
            return
        time.sleep(0.2)
    for pid in pids.values():
        if _alive(pid):
            try:
                os.killpg(pid, 9)
            except OSError:
                pass


def _reap(pids, timeout=5):
    """Wait for a set of already-TERMed pids to exit, then SIGKILL any that
    are still standing -- used after the orphan sweep so a new generation
    does not start on top of a previous one's dying processes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_alive(p) for p in pids):
            return
        time.sleep(0.2)
    for p in pids:
        if _alive(p):
            try:
                os.killpg(p, 9)
            except OSError:
                pass


def _pid_runs_qemu(pid):
    """True if pid is alive and its command line still names our QEMU.
    pids.json survives a restart, so a recycled pid could otherwise read
    as a running instance and refuse every start with 'already running'."""
    if not _alive(pid):
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return b"qemu" in f.read()
    except OSError:
        return True     # unreadable: do not claim it died


# --- start -----------------------------------------------------------------

def _wait_for(path, tries=50, interval=0.2):
    for _ in range(tries):
        if os.path.exists(path):
            return True
        time.sleep(interval)
    return False


def _start_display_stack(api, inst, d, ports, pids, log):
    """Bring up weston -> Xwayland -> x11vnc -> websockify.  Returns None on
    success, or a short reason string on failure (the caller falls back to
    a plain VNC start).  Nothing here is fatal to the process; a reason
    just means the GL path is not available this time."""
    index = inst["index"]
    vnc, ws, _qmp, _audio = ports
    display_num = vnc - 5900
    wlsock = _wl_socket(index)
    env = dict(os.environ, XDG_RUNTIME_DIR=XDG_RUNTIME_DIR)

    def spawn(key, argv, extra_env=None):
        e = env if extra_env is None else dict(env, **extra_env)
        proc = subprocess.Popen(argv, stdout=log, stderr=log, env=e,
                                start_new_session=True)
        pids[key] = proc.pid
        return proc

    try:
        os.makedirs(XDG_RUNTIME_DIR, exist_ok=True)
        os.chmod(XDG_RUNTIME_DIR, 0o700)
    except OSError:
        pass

    spawn("weston", ["weston", "--backend=headless-backend.so",
                     "--renderer=gl", "--width=%d" % SCREEN_W,
                     "--height=%d" % SCREEN_H, "--idle-time=0",
                     "--socket=%s" % wlsock,
                     "--log=%s" % os.path.join(d, "weston.log")])
    if not _wait_for(os.path.join(XDG_RUNTIME_DIR, wlsock)):
        return "weston compositor socket never appeared"

    # Xwayland alone is told which compositor to attach to (WAYLAND_DISPLAY);
    # the base env has none, so no other helper inherits it.
    spawn("xwayland", ["Xwayland", ":%d" % display_num,
                       "-geometry", "%dx%d" % (SCREEN_W, SCREEN_H)],
          extra_env={"WAYLAND_DISPLAY": wlsock})
    if not _wait_for("/tmp/.X11-unix/X%d" % display_num):
        return "Xwayland :%d never appeared" % display_num

    # x11vnc quits at once if it sees WAYLAND_DISPLAY (it mistakes the
    # session for Wayland).  The base env never carries it -- only
    # Xwayland's own extra_env did, per-child -- so x11vnc is spawned with
    # the base env and never sees it.
    spawn("x11vnc", ["x11vnc", "-display", ":%d" % display_num,
                     "-rfbport", str(vnc), "-listen", "127.0.0.1",
                     "-noipv6", "-forever", "-shared", "-nopw", "-q"])

    spawn("websockify", ["websockify", str(ws), "127.0.0.1:%d" % vnc])
    return None


def _cfg_confirmed(log_path):
    """True if this generation's qemu-3dfx log shows the mesagl.cfg took --
    the FpsLimit line or ReadbackPresent being enabled, both written when
    the guest creates its first GL context.  The log is rotated at start
    (on_start), so the file read here holds only the current run; a cfg in
    the wrong directory silently does nothing, so this is checked rather
    than assumed."""
    try:
        with open(log_path, "rb") as f:
            data = f.read()
    except OSError:
        return False
    return (b"FpsLimit [" in data) or (b"ReadbackPresent enabled" in data)


def on_start(api, inst):
    index = inst["index"]
    ports = api.ports_of(inst)
    vnc, ws, qmp_port, _audio = ports
    display_num = vnc - 5900
    d = _inst_dir(api, inst)

    try:
        os.remove(_fallback_marker(d))
    except OSError:
        pass

    log_path = os.path.join(d, "pcatgl.log")
    # keep one generation back: this generation's log then stands alone, so
    # the cfg-applied marker read from it (pcatgl_hardware) belongs to this
    # run and not a previous one, and the file cannot grow without bound.
    try:
        if os.path.exists(log_path):
            os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    log = open(log_path, "ab")
    log.write(("\n--- %s\n" % time.strftime("%F %T")).encode())
    log.flush()

    pids = {}

    def spawn(key, argv, env=None, cwd=None):
        proc = subprocess.Popen(argv, stdout=log, stderr=log, env=env,
                                cwd=cwd, start_new_session=True)
        pids[key] = proc.pid
        return proc

    # a previous generation still on this display/ports would make the new
    # helpers attach to leftovers; clear them first (loud, in the log) and
    # wait for them to actually exit before spawning -- otherwise _wait_for
    # below finds the old compositor/X sockets still present and the new
    # x11vnc/QEMU attach to a dying server
    swept = _sweep_orphans(d, index, display_num, vnc, qmp_port, log)
    if swept:
        _reap(swept)

    _write_mesagl_cfg(api, inst, d)

    # bring up the GL display stack; on failure, fall back to plain VNC
    reason = _start_display_stack(api, inst, d, ports, pids, log)
    gl = reason is None
    if not gl:
        log.write(("[pcatgl] GL stack unavailable (%s); "
                   "falling back to plain VGA\n" % reason).encode())
        log.flush()
        _kill_pids(pids)
        pids = {}
        try:
            with open(_fallback_marker(d), "w", encoding="utf-8") as f:
                f.write(reason + "\n")
        except OSError:
            pass

    argv = _qemu_argv(api, inst, ports, gl)
    log.write((" ".join(argv) + "\n").encode())
    log.flush()

    qemu_env = None
    qemu_cwd = d
    if gl:
        # GL output to the X server; SDL on x11, WAYLAND_DISPLAY absent so
        # SDL uses X and not the compositor directly
        qemu_env = dict(os.environ, DISPLAY=":%d" % display_num,
                        SDL_VIDEODRIVER="x11", XDG_RUNTIME_DIR=XDG_RUNTIME_DIR)
        qemu_env.pop("WAYLAND_DISPLAY", None)

    try:
        qemu_proc = spawn("qemu", argv, env=qemu_env, cwd=qemu_cwd)
    except OSError as exc:
        _kill_pids(pids)
        log.close()
        return "failed: %s" % exc

    # wait for it to come up or die; then confirm the cfg took (GL only)
    result = "started"
    for _ in range(40):
        if qemu_proc.poll() is not None:
            _kill_pids(pids)
            _save_pids(d, {})
            log.close()
            return "failed: qemu-3dfx exited during startup, see pcatgl.log"
        if is_up(api, inst) and _port_open(ws):
            break
        time.sleep(0.25)
    else:
        result = "started (slow to come up)"

    _save_pids(d, pids)
    # The mesagl.cfg-applied marker (FpsLimit [ N FPS ] / ReadbackPresent
    # enabled) is not written until the guest loads the wrapper DLL and
    # creates a GL context -- long after this returns and after Win98 has
    # booted -- so it is pointless to poll for it here.  pcatgl_hardware
    # reads this generation's log lazily and reports "applied" or "not yet
    # (3D unused)" once it appears.
    log.close()
    return result


# --- stop / reset ----------------------------------------------------------

_stopping = set()


def _stop_now(api, inst):
    d = _inst_dir(api, inst)
    pids = _load_pids(d)
    qmp_port = api.ports_of(inst)[2]
    qemu = pids.get("qemu")
    # ask QEMU to quit over QMP first; "quit" ends QEMU and flushes on its
    # own, which is what stop means here (Win98 is APM, so a guest-side
    # powerdown would do nothing).  Give it room to finish that flush
    # before anything is forced -- a KILL mid write-back to a raw disk
    # corrupts it -- then fall back to _kill_pids for the display stack
    # (and for QEMU too, if QMP was unreachable).
    try:
        _qmp_command(qmp_port, "quit")
    except OSError:
        pass
    deadline = time.time() + 15
    while qemu and _alive(qemu) and time.time() < deadline:
        time.sleep(0.25)
    _kill_pids(pids)
    _save_pids(d, {})


def on_stop(api, inst):
    """Stop in a background thread so a slow teardown does not block the
    single-threaded manager; _stopping keeps a second stop of the same
    instance from starting a second teardown."""
    index = inst["index"]
    if index in _stopping:
        return "already stopping"

    def _shutdown():
        try:
            _stop_now(api, inst)
        finally:
            _stopping.discard(index)

    _stopping.add(index)
    threading.Thread(target=_shutdown, daemon=True).start()
    return "shutting down"


def pcatgl_reset(api, inst):
    """A guest reset over QMP; the display stack is left running so the
    console stays connected across the reboot."""
    qmp_port = api.ports_of(inst)[2]
    try:
        _qmp_command(qmp_port, "system_reset")
        return "reset"
    except OSError as exc:
        return "reset failed: %s" % exc


def is_up(api, inst):
    return _pid_runs_qemu(_load_pids(_inst_dir(api, inst)).get("qemu"))


# --- QMP (a minimal client; the core never speaks to an engine over QMP) ---

def _qmp_command(port, command, arguments=None):
    """Open the QMP port, negotiate capabilities, run one command, close.
    Raises OSError if the port is not there (a stopped or fallback-less
    instance) -- callers treat that as nothing to do."""
    with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
        f = s.makefile("rwb")
        f.readline()                      # the greeting
        f.write(b'{"execute":"qmp_capabilities"}\n')
        f.flush()
        f.readline()
        msg = {"execute": command}
        if arguments:
            msg["arguments"] = arguments
        f.write((json.dumps(msg) + "\n").encode())
        f.flush()
        return f.readline()


# --- thumbnail -------------------------------------------------------------

def pcatgl_thumbnail(api, inst, png):
    """This instance's screen.  In GL mode the guest fills the X display,
    so the root window of :N is captured with ImageMagick; in the VNC
    fallback there is no X display, so QMP's own screendump is used
    instead.  Writes png if it can, leaves it alone if it cannot."""
    d = _inst_dir(api, inst)
    vnc, _ws, qmp_port, _audio = api.ports_of(inst)
    display_num = vnc - 5900
    if os.path.exists(_fallback_marker(d)):
        raw = png + ".ppm"
        try:
            _qmp_command(qmp_port, "screendump", {"filename": raw})
            subprocess.run(["convert", raw, "-resize", "320x", png],
                           capture_output=True, timeout=5, check=False)
        except OSError:
            pass
        finally:
            try:
                os.remove(raw)
            except OSError:
                pass
        return
    raw = png + ".xwd.png"
    env = dict(os.environ, DISPLAY=":%d" % display_num)
    try:
        subprocess.run(["import", "-window", "root", raw],
                       env=env, capture_output=True, timeout=5, check=False)
        subprocess.run(["convert", raw, "-resize", "320x", png],
                       capture_output=True, timeout=5, check=False)
    except OSError:
        pass
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


# --- hardware card ---------------------------------------------------------

def pcatgl_hardware(api, inst):
    """The read-only hardware facts, wrapped as the record's `hardware`
    field (shown() merges into the record, so a bare dict would clobber
    the machine name -- box86/pcat wrap it the same way)."""
    d = _inst_dir(api, inst)
    vga = inst.get("vga") or "std"
    fps = inst.get("fpslimit")
    if fps in ("", None):
        fps = "60"
    hw = {
        "machine": PCATGL_MACHINE_LABEL,
        "cpu": PCATGL_CPU_LABEL,
        "video": PCATGL_VGA_LABELS.get(vga, vga) + " (3dfx GL, retrace=precise)",
        "memory": inst.get("memory") or "256M",
        "sound": PCATGL_SOUND_LABEL,
        "bios": ("SeaBIOS (PnP disabled)" if _nopnp_bios(api)
                 else "SeaBIOS (default)"),
        "acpi": "ACPI enabled",
        "fpslimit": ("unlimited" if str(fps) == "0" else "%s FPS" % fps),
    }
    fb = _fallback_marker(d)
    running = api.is_running(inst)
    if running and os.path.exists(fb):
        try:
            with open(fb, encoding="utf-8") as f:
                reason = f.read().strip()
        except OSError:
            reason = ""
        hw["display"] = ("plain VGA over VNC -- GL unavailable"
                         + (": %s" % reason if reason else ""))
        # no GL stack in the fallback, so there is no mesagl.cfg to apply
    else:
        hw["display"] = "3dfx GL via weston + Xwayland + x11vnc"
        if running:
            # the marker only appears once the guest has made a GL context,
            # so before any 3D runs "not yet" is normal, not a fault
            hw["cfg"] = ("mesagl.cfg applied"
                         if _cfg_confirmed(os.path.join(d, "pcatgl.log"))
                         else "mesagl.cfg not yet confirmed (3D not used yet)")
    return {"hardware": hw}
