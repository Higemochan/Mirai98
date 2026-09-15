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
import re
import shutil
import socket
import subprocess
import threading
import time


# The qemu-3dfx build.  Not fixed in code: a deploy points CONFIG
# ("qemu_3dfx" in mirai98.json) or the environment at whichever build is
# current (build-n/build-m/... are still moving), and this default is only
# the last resort so a bench with neither set still runs.
QEMU_3DFX_DEFAULT = ("/storage/work/kvm98/src/qemu-3dfx-0b399bd-fix/"
                     "build-cd/qemu-system-i386")

# How often x11vnc looks at the captured window (-wait) and how long it
# holds a captured frame before sending it (-defer), in milliseconds.  Both
# are fixed rather than derived from the instance's fps: see the measurement
# table where they are used.
VNC_POLL_MS = 10
VNC_DEFER_MS = 1

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

# The SoundFont the MPU-401 (pc98-midi) synthesises through.  Overridable
# from mirai98.json ("pcatgl_soundfont"); the device tolerates a missing
# file, so this is not gated on existence.
PCATGL_SOUNDFONT = "/opt/mirai98/soundfonts/kgs88-v1.97.sf2"

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
    # the console's own pointer relay for FPS mouselook; see
    # pcatgl_fps_input for why a game needs it and the desktop does not
    api.instance_action("pcat-gl", "fps-input",
                        lambda inst, data: pcatgl_fps_input(api, inst, data))
    # "vga" and "fpslimit" are this machine's own fields.  "" is accepted
    # so the global-by-name validators stay harmless for other machines,
    # which never carry these fields.  NB: "vga" is also registered by
    # pcat.py with the identical validator; PLUGIN_FIELDS is keyed by name,
    # so whichever loads last wins -- kept identical on purpose, and if one
    # side's accepted set ever changes the other must change with it.
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
    fps = _console_fps(inst)
    lines = []
    for key, default in MESAGL_DEFAULTS:
        value = str(fps) if key == "FpsLimit" else default
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


def _midi_supported(qemu_bin):
    """True if this binary has the pc98-midi device (MPU-401 + FluidSynth).
    Only the CD/MIDI builds carry it; adding -device pc98-midi to one
    without it (e.g. build-s) fails to start, so it is gated on this probe
    (one cheap `-device pc98-midi,help` per start; no VM is launched)."""
    try:
        out = subprocess.run([qemu_bin, "-device", "pc98-midi,help"],
                             capture_output=True, timeout=10)
        return b"soundfont" in out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return False


def _ide_has_audiodev(qemu_bin):
    """True if this binary's piix3-ide takes an audiodev property -- the
    fork's standard IDE that plays a CD's audio tracks (CD-DA) into an
    audiodev.  Adding -global piix3-ide.audiodev to a binary without the
    property fails to start, so it is gated on this probe (one cheap
    `-device piix3-ide,help` per start; no VM is launched)."""
    try:
        out = subprocess.run([qemu_bin, "-device", "piix3-ide,help"],
                             capture_output=True, timeout=10)
        return b"audiodev" in out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return False


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
        "-audiodev", "pa,id=snd",
        "-device", "sb16,audiodev=snd",
        # an absolute pointer for the VNC console: the guest's PS/2 mouse is
        # relative, so a VNC/xdotool click at an absolute position lands at
        # the wrong place (moves work, clicks miss).  usb-tablet reports
        # absolute coordinates (UHCI is on the PIIX3, enabled by -usb), so a
        # click hits where it is aimed.  Always on -- not a toggle.
        "-usb",
        "-device", "usb-tablet",
        # QMP stays on loopback whatever LOOPBACK is: it is unauthenticated
        # and the production service runs without --loopback, so binding
        # the host address would put it on the LAN.  _qmp_command connects
        # to 127.0.0.1 to match.
        "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
    ]
    # the fork's standard IDE plays CD-DA into an audiodev; the PIIX3
    # controller takes it as -global piix3-ide.audiodev, sharing the sb16
    # audiodev id.  Gated: only builds whose piix3-ide has the property
    # accept it (a binary without it fails to start).
    if _ide_has_audiodev(_qemu_bin(api)):
        argv += ["-global", "piix3-ide.audiodev=snd"]
    # MPU-401 (pc98-midi) synthesised by FluidSynth, on the CD/MIDI builds.
    # Its defaults are PC-98 values -- iobase 0xe0d0, irq 6 -- and irq 6 is
    # the standard-PC floppy, so override to a free port and IRQ (SB16 is
    # irq 5, and there is no NIC or other ISA IRQ user here).  iobase is
    # 0x210, not the more usual 0x330: Win98's own bundled "Music Quest
    # MPU-401 Compatible" driver only declares one Basic Configuration,
    # 0x210/IRQ9, and has no UI to point it anywhere else -- 0x330 left the
    # device unreachable (Code 24) with no way for the guest to find it.
    # IRQ 9 matches what the guest's driver declares, and it has to: the
    # bundled "Music Quest MPU-401 Compatible" offers one Basic
    # Configuration and no way to pick another, so moving the device to
    # IRQ10 only left the two disagreeing -- measured live 2026-09-13,
    # the wizard still asked for IRQ9 and the reboot still died.
    # IRQ9 is also the ACPI SCI here, and sharing it stops Win98's ACPI
    # restart from completing: the guest falls through to a soft-off, so
    # QEMU sees SHUTDOWN/guest-shutdown and exits(0) on every reboot once
    # the driver is bound (with no driver bound the same reboot raised
    # RESET/guest-reset and QEMU stayed up).  The fix belongs on the SCI
    # side, not here.
    # audiodev=snd is required, not optional.  pc98-midi takes
    # DEFINE_AUDIO_PROPERTIES, and hw/audio/pc98-midi.c's realize spells out
    # what an unset one means: "Without an audiodev the card is still there
    # for the guest to program; it simply has nowhere to play" -- and
    # midi_synth_open leaves the synthesiser silent.  Measured live
    # 2026-09-13: with it unset, `info qtree` showed pc98-midi audiodev ""
    # next to sb16's "snd", the guest bound the driver and Media Player
    # played a 30 s MIDI, and parec on pcatgl_6.monitor recorded 18 s of
    # exact silence (rms 0, peak 0).  It shares the single pa audiodev with
    # SB16, so MIDI reaches the browser over #52's path.  Gated: build-s has
    # no pc98-midi and would refuse to start with it.
    if _midi_supported(_qemu_bin(api)):
        argv += ["-device",
                 "pc98-midi,audiodev=snd,iobase=0x210,irq=9,soundfont=%s"
                 % (api.CONFIG.get("pcatgl_soundfont") or PCATGL_SOUNDFONT)]
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


def _listen_inodes(port):
    """Socket inodes LISTENing on 127.0.0.1:<port>, from /proc/net/tcp.

    Reading the kernel's own table answers "is this port actually taken",
    which a command-line scan cannot: a helper that forked, was re-exec'd,
    or was adopted by init still holds its socket no matter what its argv
    looks like now.
    """
    want = "%08X:%04X" % (0x0100007F, port)      # 127.0.0.1, big-endian hex
    inodes = set()
    for path in ("/proc/net/tcp",):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                next(fh, None)
                for line in fh:
                    col = line.split()
                    if len(col) < 10:
                        continue
                    # st == 0A is TCP_LISTEN; a bound-but-closing socket
                    # (TIME_WAIT) has no owner to kill and frees itself.
                    if col[3] != "0A":
                        continue
                    if col[1] == want or col[1].endswith(":%04X" % port):
                        inodes.add(col[9])
        except (OSError, StopIteration):
            pass
    return inodes


def _port_holders(port):
    """Pids holding a LISTEN socket on <port>, by matching /proc/*/fd."""
    inodes = _listen_inodes(port)
    if not inodes:
        return []
    want = set("socket:[%s]" % i for i in inodes)
    mine = os.getpid()
    held = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        try:
            fds = os.listdir("/proc/%d/fd" % pid)
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink("/proc/%d/fd/%s" % (pid, fd)) in want:
                    held.append(pid)
                    break
            except OSError:
                continue
    return held


def _free_ports(ports, log):
    """Make sure this instance's own ports are actually free before the new
    generation binds them.

    The cmdline sweep above catches helpers it can recognise; this catches
    whatever is left by asking the kernel who is LISTENing.  It is safe to
    be blunt here because every port is index-derived: nothing but this
    instance's own leftovers can be sitting on them.
    """
    stuck = []
    for port in ports:
        for pid in _port_holders(port):
            if pid not in stuck:
                stuck.append(pid)
                try:
                    cmd = open("/proc/%d/cmdline" % pid, "rb").read()
                    cmd = cmd.replace(b"\0", b" ").decode("utf-8", "replace")
                except OSError:
                    cmd = "?"
                log.write(("[pcatgl] port %d still held by pid %d: %s\n"
                           % (port, pid, cmd[:200])).encode())
    if not stuck:
        return
    log.flush()
    for pid in stuck:
        for sig in (15,):
            try:
                os.killpg(os.getpgid(pid), sig)
            except OSError:
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(_port_holders(p) for p in ports):
            break
        time.sleep(0.2)
    for port in ports:
        for pid in _port_holders(port):
            try:
                os.kill(pid, 9)
                log.write(("[pcatgl] port %d: SIGKILL pid %d\n"
                           % (port, pid)).encode())
            except OSError:
                pass
    log.flush()


def _wl_socket(index):
    return "wl-pcatgl-%d" % index


def _fallback_marker(d):
    return os.path.join(d, "gl_fallback")


AUDIO_RATE = 44100          # s16le stereo, the worklet's rate


RELAY_SRC = r'''"""Fan one audio stream out to however many listeners there are.

ffmpeg's own "-listen 1" served exactly one client and stopped listening
while it did, so a second consumer could not connect at all, and a
disconnect left ~2.1s with nothing listening (measured, 2026-09-09). Any
probe of that port therefore shut the browser out for as long as it held
the slot -- an observer effect that made the audio look flaky on its own.
This listens instead: never stops listening, never exits when a client
leaves, serves as many as turn up.

Two stream shapes, because the two have different join rules:

  pcm  -- raw s16le stereo. A listener may join anywhere, so long as it
          joins on a frame boundary: four bytes in, left and right. Half
          a frame in and the channels stay crossed for the rest of the
          connection. There is nothing else to replay.

  webm -- only decodable from its initialisation segment (everything
          before the first Cluster), so that is kept and replayed to
          each new client, which then starts at the NEXT cluster
          boundary. Mid-cluster is no better than no header at all.

Either way a consumer that stops reading is dropped at a hard cap rather
than buffered without bound: a client that far behind is going to
reconnect anyway, and the memory belongs to everyone else.
"""
import os
import select
import socket
import sys

PORT = int(sys.argv[1])
MODE = sys.argv[2] if len(sys.argv) > 2 else "webm"
CLUSTER = b"\x1f\x43\xb6\x75"          # EBML id of a WebM Cluster
FRAME = 4                              # s16le stereo: 2 bytes x 2 channels
MAXQ = 2 * 1024 * 1024                 # per client, then it is dropped
CHUNK = 65536

srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", PORT))
srv.listen(8)
srv.setblocking(False)

src = sys.stdin.buffer
os.set_blocking(src.fileno(), False)

init = b""          # webm: everything before the first Cluster
have_init = MODE == "pcm"
tail = b""          # webm: carry, so a Cluster id split across reads is found
pos = 0             # pcm: bytes of stream seen, to find a frame boundary
clients = {}        # sock -> [queue bytes, started bool]


def drop(sock, why):
    clients.pop(sock, None)
    try:
        sock.close()
    except OSError:
        pass
    sys.stderr.write("relay: dropped a client (%s)\n" % why)
    sys.stderr.flush()


while True:
    writers = [s for s, st in clients.items() if st[0]]
    r, w, _ = select.select([srv, src] + list(clients), writers, [], 1.0)

    if srv in r:
        try:
            sock, _addr = srv.accept()
            sock.setblocking(False)
            clients[sock] = [b"", False]
        except OSError:
            pass

    for sock in list(clients):
        if sock in r:
            try:
                if not sock.recv(4096):
                    drop(sock, "closed")
            except OSError:
                drop(sock, "recv failed")

    if src in r:
        try:
            data = src.read(CHUNK)
        except OSError:
            data = b""
        if data is None:
            data = b""
        if data == b"":
            break                      # the source went away; the loop restarts it

        if MODE == "pcm":
            for sock, st in clients.items():
                if st[1]:
                    st[0] += data
                else:
                    # join on a frame boundary, never mid-frame
                    skip = (FRAME - (pos % FRAME)) % FRAME
                    if skip < len(data):
                        st[0] = data[skip:]
                        st[1] = True
            pos += len(data)
        else:
            if not have_init:
                init += data
                idx = init.find(CLUSTER)
                if idx >= 0:
                    have_init, data, init = True, init[idx:], init[:idx]
                else:
                    data = b""
            if have_init and data:
                hay = tail + data
                boundary = hay.find(CLUSTER)
                tail = hay[-3:]
                for sock, st in clients.items():
                    if st[1]:
                        st[0] += data
                    elif boundary >= 0:
                        st[0] = init + hay[boundary:]
                        st[1] = True

        for sock, st in list(clients.items()):
            if len(st[0]) > MAXQ:
                drop(sock, "too far behind")

    for sock in w:
        st = clients.get(sock)
        if not st or not st[0]:
            continue
        try:
            n = sock.send(st[0])
            st[0] = st[0][n:]
        except OSError:
            drop(sock, "send failed")
'''


def _write_relay(d):
    path = os.path.join(d, "audiorelay.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(RELAY_SRC)
    return path


def _pulse_env(base=None):
    """A copy of `base` (os.environ by default) that can actually reach
    PulseAudio.

    libpulse finds the daemon at $XDG_RUNTIME_DIR/pulse/native, and a
    systemd unit is started with no XDG_RUNTIME_DIR at all -- there is no
    login session behind it to have made one. Confirmed live, 2026-09-09:
    from mirai98.service's own environment `pactl info` answers
    "Connection failure: Connection refused", while the identical call
    with XDG_RUNTIME_DIR=/run/user/0 answers normally, and the socket is
    right there at /run/user/0/pulse/native the whole time.

    Every single thing box86 does with audio went through that failure:
    the null sink was never created (pactl could not connect), 86Box
    itself had nowhere to play into, ffmpeg could not open the sink's
    monitor, and the sink was never unloaded on stop either. That is why
    a box86 machine has never made a sound -- not a missing sound card,
    not the browser, not the codec: nothing was ever connected to
    PulseAudio at all. It also explains the box86_N sinks left lying
    around from instances that no longer exist: those were made by a
    server started from a login shell, which does have the variable.

    Only filled in when the caller has neither PULSE_SERVER nor
    XDG_RUNTIME_DIR already, and only when that socket really is there,
    so a host that puts PulseAudio somewhere else is left alone.
    """
    env = dict(os.environ if base is None else base)
    if env.get("PULSE_SERVER") or env.get("XDG_RUNTIME_DIR"):
        return env
    runtime = "/run/user/%d" % os.getuid()
    if os.path.exists(os.path.join(runtime, "pulse", "native")):
        env["XDG_RUNTIME_DIR"] = runtime
    return env


def _pactl(args, timeout=5):
    """One pactl call that can reach the daemon, with its result kept.

    Returns the CompletedProcess. Callers that care whether it worked
    have to look -- the sink load used to be fired with check=False and
    its result dropped on the floor, which is how a daemon that refused
    every connection stayed invisible for as long as it did.
    """
    return subprocess.run(["pactl"] + list(args), capture_output=True,
                          timeout=timeout, check=False, env=_pulse_env(),
                          text=True)


def _find_sink_modules(sink_name):
    """Every module-null-sink loaded under this name, newest last.

    Plural on purpose: PulseAudio happily loads the same sink_name twice
    and renames the sinks (box86_8, box86_8.2, box86_8.3 ...), so "the"
    module for a name is not a thing. Returning one of them, as this
    used to, left the rest behind for good -- three had piled up on the
    host by the time anyone looked (2026-09-09).
    """
    try:
        out = _pactl(["list", "short", "modules"]).stdout or ""
    except OSError:
        return []
    found = []
    for line in out.splitlines():
        if "module-null-sink" in line and ("sink_name=%s" % sink_name) in line:
            found.append(line.split("\t", 1)[0])
    return found


def _sink_for_index(index):
    """This instance's own PulseAudio null sink name."""
    return "pcatgl_%d" % index


def _sink_name(inst):
    return _sink_for_index(inst["index"])


def _console_fps(inst):
    """The console frame rate (FPS) this instance targets: the mesagl guest
    cap (FpsLimit) follows it.  Read from the "fpslimit" field (kept for
    back-compat); default 60, clamped to 20-75.  Below ~20 is choppy; above
    ~75 only adds host readback (glReadPixels) load for frames the guest
    (~60) never makes.

    x11vnc's capture rate no longer follows it -- see VNC_POLL_MS /
    VNC_DEFER_MS, which are set from what the capture path was measured to
    carry rather than from what the guest is asked to draw."""
    v = inst.get("fpslimit")
    try:
        n = 60 if v in ("", None) else int(v)
    except (TypeError, ValueError):
        n = 60
    return max(20, min(75, n))


def _vncloop_marker(index):
    """A stable, per-instance token for the x11vnc respawn loop's command
    line, so the orphan sweep and _kill_pids can find it (a bare
    'x11vnc -id 0x..' carries nothing tied to this instance).  The
    '.vncloop' suffix keeps it text-final, so it cannot prefix-collide with
    another index."""
    return "pcatgl-%d.vncloop" % index


# The QEMU SDL top-level window, matched by WM_CLASS "qemu-system-i386"
# (both the instance and class field xwininfo -tree prints).  The title is
# not used: it carries a grab hint ("QEMU - Press Ctrl-Alt-G to exit grab")
# that changes.  Largest match wins, over QEMU's smaller child surfaces.
_QEMU_WIN_RE = re.compile(
    r'^\s*(0x[0-9a-fA-F]+)\s+"[^"]*":\s*\([^)]*"qemu-system-i386"\)\s+'
    r'(\d+)x(\d+)\+', re.MULTILINE)

_FINDWIN_SRC = """import re, subprocess, sys
WIN_RE = re.compile(%(pattern)r, re.MULTILINE)
try:
    out = subprocess.run(
        ["xwininfo", "-root", "-tree", "-display", sys.argv[1]],
        capture_output=True, text=True, timeout=5, check=False).stdout
except OSError:
    sys.exit(1)
best, best_area = None, 0
for m in WIN_RE.finditer(out):
    w, h = int(m.group(2)), int(m.group(3))
    if w > 50 and h > 50 and w * h > best_area:
        best, best_area = m.group(1), w * h
if best:
    print(best)
"""


def _write_findwin(d):
    path = os.path.join(d, "findwin.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_FINDWIN_SRC % {"pattern": _QEMU_WIN_RE.pattern})
    return path


def _instance_tokens(index, display_num, vnc, qmp_port):
    """Command-line fragments that belong to this instance and to no other.

    Every one is derived from this instance's own index, and that is what
    makes both the start-time sweep and the crash teardown safe to fire
    without first asking who a process belongs to.

    Number-final tokens carry a trailing space so "-display :2 " does not
    match ":21"; the joined cmdline's final NUL becomes that space, so a
    number at the very end still matches.
    """
    return (
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
        _vncloop_marker(index),             # the x11vnc respawn loop
        "%s.monitor" % _sink_for_index(index),     # parec on the sink
        "%s.audiorelay" % _sink_for_index(index),  # the relay loop
        "127.0.0.1:%d " % (4620 + index),          # websockify_audio -> relay
    )


def _cmdline(pid):
    """This pid's command line as text, "" for a zombie (which has none),
    None if /proc could not be read at all.  The caller that decides
    whether to signal wants those two apart: an empty one is a process on
    its way out, an unreadable one is a process this cannot identify."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return None


def _sweep_orphans(d, index, display_num, vnc, qmp_port, log):
    """Kill anything still holding this instance's display, compositor
    socket or ports.  Reaching on_start means the core found this instance
    not running, so every match is a leftover of a previous generation.
    They survive a restart of the manager because each helper is in its
    own session.
    """
    tokens = _instance_tokens(index, display_num, vnc, qmp_port)
    mine = os.getpid()
    signaled = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        cmd = _cmdline(pid)
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
    for key in ("qemu", "websockify_audio", "audio_relay",
                "websockify", "x11vnc", "xwayland", "weston"):
        pid = pids.get(key)
        if pid and _alive(pid):
            try:
                os.killpg(pid, 15)
            except OSError:
                pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not any(_alive(pid) for pid in pids.values()):
            # Gone, but not collected: _alive reads a zombie as dead, so
            # this is the path taken exactly when every one of them is
            # one.  The next on_start used to be what cleared them --
            # after a crash teardown there may not be one for days.
            _reap_exited(pids.values())
            return
        time.sleep(0.2)
    for pid in pids.values():
        if _alive(pid):
            try:
                os.killpg(pid, 9)
            except OSError:
                pass
    _reap_exited(pids.values())


def _kill_pids_verified(pids, tokens, log):
    """_kill_pids, but only for pids that still look like this instance's
    own helpers.

    pids.json outlives the manager, so a pid a previous generation recorded
    can have been recycled into something unrelated by the time a crash is
    noticed -- and the crash path, unlike a stop somebody asked for, fires
    on its own.  A pid whose command line no longer carries any of this
    instance's tokens is left alone; one that still does is this
    instance's, whatever else has changed since.  A zombie has no command
    line and needs no signal: _reap_exited is what it wants.
    """
    keep = {}
    for key, pid in pids.items():
        if not pid or not _alive(pid):
            continue
        cmd = _cmdline(pid)
        if cmd and any(t in cmd for t in tokens):
            keep[key] = pid
        elif cmd is not None:
            log.write(("[pcatgl] pid %d (recorded as %s) carries none of "
                       "this instance's tokens; left alone\n"
                       % (pid, key)).encode())
    log.flush()
    _kill_pids(keep)
    return keep


def _reap(pids, timeout=5):
    """Wait for a set of already-TERMed pids to exit, then SIGKILL any that
    are still standing -- used after the orphan sweep so a new generation
    does not start on top of a previous one's dying processes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(_alive(p) for p in pids):
            _reap_exited(pids)
            return
        time.sleep(0.2)
    for p in pids:
        if _alive(p):
            try:
                os.killpg(p, 9)
            except OSError:
                pass
    _reap_exited(pids)


def _reap_exited(pids):
    """Clear the kernel entries of helpers that have already exited.

    Nothing here waits: a pid still running answers WNOHANG and is left
    alone.  Only pids this plugin recorded are asked about -- never
    waitpid(-1), and never SIGCHLD=SIG_IGN.  The manager reads
    Popen.poll()/.returncode in several places, and both of those broader
    strokes would let an exit status be collected out from under one of
    those handles.  These pids carry no such handle: spawn() keeps
    proc.pid and lets the Popen go, and QEMU's is local to on_start and
    out of scope by the time any of this runs.
    """
    for pid in pids:
        if not pid:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass        # not ours: pids.json outlived a manager restart
        except OSError:
            pass


def _pid_matches(pid, needle):
    """True if pid is alive and its command line contains needle (bytes).
    pids.json survives a restart, so a recycled pid could otherwise read
    as a running instance and refuse every start with 'already running'.
    The caller passes this instance's own "tcp:127.0.0.1:<qmp>," -- the
    QMP arg is index-specific and path-independent, so it cannot match an
    unrelated QEMU (or a differently-built one) the way a bare "qemu" in
    the binary path could."""
    if not _alive(pid):
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return needle in f.read()
    except OSError:
        return True     # unreadable: do not claim it died


# --- start -----------------------------------------------------------------

def _wait_for(path, tries=50, interval=0.2):
    for _ in range(tries):
        if os.path.exists(path):
            return True
        time.sleep(interval)
    return False


class _DisplayHelperLaunchError(Exception):
    """A helper's own Popen() never started (binary missing, not
    executable, ...).  Carries which helper so the fallback reason names
    it; _start_display_stack's own try/except is the only place this is
    ever caught."""
    def __init__(self, key, cause):
        super().__init__(key, cause)
        self.key = key
        self.cause = cause


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
        try:
            proc = subprocess.Popen(argv, stdout=log, stderr=log, env=e,
                                    start_new_session=True)
        except OSError as exc:
            # covers FileNotFoundError (binary missing) along with any
            # other launch-time OSError (e.g. permission denied); pids
            # already has whatever earlier helpers in this stack did
            # start, and the caller's own fallback cleans those up.
            raise _DisplayHelperLaunchError(key, exc) from exc
        pids[key] = proc.pid
        return proc

    try:
        os.makedirs(XDG_RUNTIME_DIR, exist_ok=True)
        os.chmod(XDG_RUNTIME_DIR, 0o700)
    except OSError:
        pass

    try:
        spawn("weston", ["weston", "--backend=headless-backend.so",
                         "--renderer=gl", "--width=%d" % SCREEN_W,
                         "--height=%d" % SCREEN_H, "--idle-time=0",
                         "--socket=%s" % wlsock,
                         "--log=%s" % os.path.join(d, "weston.log")])
        if not _wait_for(os.path.join(XDG_RUNTIME_DIR, wlsock)):
            return "weston compositor socket never appeared"

        # Xwayland alone is told which compositor to attach to
        # (WAYLAND_DISPLAY); the base env has none, so no other helper
        # inherits it.
        spawn("xwayland", ["Xwayland", ":%d" % display_num,
                           "-geometry", "%dx%d" % (SCREEN_W, SCREEN_H)],
              extra_env={"WAYLAND_DISPLAY": wlsock})
        if not _wait_for("/tmp/.X11-unix/X%d" % display_num):
            return "Xwayland :%d never appeared" % display_num

        # x11vnc tracks the QEMU SDL window (-id), not the whole 1024x768
        # root: the guest draws a smaller window centred in the root (e.g.
        # 640x480 at +192+144), so exporting the root would show it framed
        # in black margins that noVNC's scaleViewport only enlarges.  The
        # window is found by WM_CLASS "qemu-system-i386", largest match.
        #
        # x11vnc -id exits when that window goes away, and QEMU replaces its
        # top-level on a guest video-mode change (and when GL first makes
        # its context -- the XID changes).  So it runs under a respawn loop
        # that re-finds the window each time round -- which is also how the
        # console follows a resolution change.  No window yet (QEMU starts
        # after this) is not an error, only a reason to wait and look again.
        # The base env carries no WAYLAND_DISPLAY (only Xwayland's child
        # did), so x11vnc does not mistake the session for Wayland.
        if not shutil.which("x11vnc"):
            # the respawn loop would spin forever finding no binary -- no
            # video, a retry every second, yet on_start still "started".
            # Fall to the plain-VGA path (which needs no x11vnc), the same
            # discipline a missing display-stack binary already gets.
            return "x11vnc not found"
        findwin = _write_findwin(d)
        # x11vnc's poll (-wait) and send defer (-defer) used to be driven
        # from the target frame rate, both set to a whole frame interval.
        # -defer is how long x11vnc sits on a frame it has ALREADY captured
        # before putting it on the wire, so a frame-long defer spends a
        # frame period of delay on every frame and drops most of them.
        #
        # Measured on CT209 for #62 (2026-09-14), GL source presenting
        # 62-77fps into a 1024x768 window, counting the FramebufferUpdates
        # a client actually received (not what x11vnc reports about itself):
        #
        #     -wait 16 -defer 16   21.9 updates/s   <- what this used to do
        #     -wait 16 -defer  1   34.9
        #     -wait 10 -defer  1   44.8             <- what it does now
        #     -wait  5 -defer  1   56.8
        #
        # 10ms rather than the 5ms that scores highest: the guests this
        # console exists for present well under 60 (Final Reality sits near
        # 29, bounded by the guest's own 3D throughput, not by this path),
        # so 44.8 is already headroom over anything they make, while 5ms
        # costs about 20 more points of host CPU polling for frames that
        # were never drawn.  Neither figure caps the guest; they are what
        # this path can carry of the frames that do exist.
        # -cursor most: this guest draws no cursor into the framebuffer (the
        # SDL/host draws a sprite x11vnc's -id capture never sees), so noVNC
        # would only show its fallback dot.  -cursor most fetches the real
        # X cursor shape via XFIXES; x11vnc's default CursorShapeUpdates then
        # sends it to noVNC as the Cursor pseudo-encoding, and with the
        # usb-tablet absolute pointer it tracks 1:1.
        spawn("x11vnc", ["bash", "-c",
              "VNCLOOP=%s; while true; do "
              "W=$(python3 %s ':%d' 2>/dev/null); "
              "if [ -n \"$W\" ]; then "
              "x11vnc -display ':%d' -id \"$W\" -forever -shared "
              "-rfbport %d -listen 127.0.0.1 -noipv6 -nopw -q "
              "-wait %d -defer %d -cursor most; "
              "fi; sleep 1; done"
              % (_vncloop_marker(index), findwin, display_num,
                 display_num, vnc, VNC_POLL_MS, VNC_DEFER_MS)])

        spawn("websockify", ["websockify", str(ws), "127.0.0.1:%d" % vnc])
    except _DisplayHelperLaunchError as exc:
        return "display helper %s failed to launch: %s" % (exc.key, exc.cause)
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


# Indices inside on_start.  The crash teardown stands off while one is
# here: is_up is called from inside the start, to watch QEMU come up, and
# pids.json still names the previous generation until the last lines of it
# -- so without this the teardown would fire on a start that is going
# perfectly well, against a recorded set whose recycled pids could by then
# belong to the generation being built.
#
# A plain set needs no lock of its own: the core calls start_instance with
# its global _lock held (pc98web.py, the /start and /resume dispatch), and
# that lock is not reentrant, so two on_start calls cannot overlap and the
# discard below cannot lift another start's guard.
_starting = set()


def on_start(api, inst):
    index = inst["index"]
    _starting.add(index)
    try:
        return _start_now(api, inst)
    finally:
        _starting.discard(index)


def _start_now(api, inst):
    index = inst["index"]
    ports = api.ports_of(inst)
    vnc, ws, qmp_port, audio_ws = ports
    display_num = vnc - 5900
    d = _inst_dir(api, inst)

    # A guest that powered itself off leaves QEMU exited but unreaped:
    # nothing waited on it, and the orphan sweep further down cannot see
    # it either, since a zombie's /proc/<pid>/cmdline is empty and so
    # matches no command line.  The pids this instance recorded last time
    # are exactly the ones to ask about, and asking costs nothing once
    # they are gone.
    _reap_exited(_load_pids(d).values())

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
    # Preflight: the sweep matches on command lines, so anything it cannot
    # recognise -- a forked x11vnc child adopted by init is the one seen in
    # practice -- keeps its listening socket and the next start loops on
    # "could not obtain listening port".  Ask the kernel instead.
    # Both audio ports, not just the relay it feeds: ports_of hands back
    # the websocket (AUDIO_WS_BASE + index) as well, and that is the one
    # seen surviving in practice -- a websockify whose x11vnc has gone
    # keeps its port bound, and the next start has nowhere to listen.
    _free_ports((vnc, ws, qmp_port, audio_ws, 4620 + index), log)

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

    # Console audio: QEMU plays into a per-instance PulseAudio null sink
    # (-audiodev pa + PULSE_SINK below); parec reads that sink's monitor as
    # raw s16le, a relay fans it out, and websockify wraps it as the audio
    # websocket the browser's worklet reads -- box86's path, since x11vnc
    # carries no audio and QEMU's own VNC audio is off (vncAudio false).
    # In effect in the fallback (-vnc) mode too.
    sink = _sink_name(inst)
    audio_tcp = 4620 + index
    # audio needs parec (to read the sink monitor) and websockify (to wrap
    # it for the browser); if either is missing, disable sound only and let
    # video carry on rather than failing the start.
    audio_ok = bool(shutil.which("parec") and shutil.which("websockify"))
    if audio_ok:
        for stale in _find_sink_modules(sink):
            _pactl(["unload-module", stale])
        loaded = _pactl(["load-module", "module-null-sink",
                         "sink_name=%s" % sink,
                         "sink_properties=device.description=%s" % sink])
        if loaded.returncode != 0:
            log.write(("[pcatgl] pactl load-module failed (rc=%d): %s\n"
                       % (loaded.returncode,
                          (loaded.stderr or "").strip())).encode())
            log.flush()
        relay = _write_relay(d)
        spawn("audio_relay", ["bash", "-c",
              "AUDIORELAY=%s; while true; do "
              "parec --device='%s.monitor' --format=s16le --rate=%d "
              "--channels=2 --latency-msec=20 --raw "
              "| python3 %s %d pcm; sleep 0.2; done"
              % ("%s.audiorelay" % sink, sink, AUDIO_RATE, relay, audio_tcp)],
              env=_pulse_env())
        spawn("websockify_audio",
              ["websockify", str(audio_ws), "127.0.0.1:%d" % audio_tcp])
    else:
        log.write(b"[pcatgl] audio disabled: parec or websockify not found;"
                  b" video continues\n")
        log.flush()

    argv = _qemu_argv(api, inst, ports, gl)
    log.write((" ".join(argv) + "\n").encode())
    log.flush()

    # QEMU's audio is a PulseAudio client (-audiodev pa) routed to this
    # instance's null sink: _pulse_env gives it the runtime dir a systemd
    # unit lacks, PULSE_SINK names the sink.
    qemu_env = dict(_pulse_env())
    if audio_ok:
        # route QEMU's pa output to this instance's sink; with no pipeline
        # there is nothing to route to, so leave pa on the default
        qemu_env["PULSE_SINK"] = sink
    qemu_cwd = d
    if gl:
        # GL output to the X server; SDL on x11, WAYLAND_DISPLAY absent so
        # SDL uses X and not the compositor directly
        qemu_env.update(DISPLAY=":%d" % display_num, SDL_VIDEODRIVER="x11",
                        XDG_RUNTIME_DIR=XDG_RUNTIME_DIR)
        qemu_env.pop("WAYLAND_DISPLAY", None)

    try:
        qemu_proc = spawn("qemu", argv, env=qemu_env, cwd=qemu_cwd)
    except OSError as exc:
        _kill_pids(pids)
        # the previous generation is still what pids.json holds until the
        # save below, and QEMU never started this time: clear it, or the
        # crash watch reads a stale set as a generation to take down
        _save_pids(d, {})
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
    # QEMU ending on its own reaches none of the helpers -- every one of
    # them is its own session -- so nothing here would notice.  Watch it.
    if pids.get("qemu"):
        threading.Thread(target=_watch_for_crash,
                         args=(api, inst, pids["qemu"], qmp_port),
                         daemon=True).start()
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

# Indices whose crash teardown is running, and the lock that hands it out.
# The core serves on a ThreadingHTTPServer, so several requests are answered
# at once and is_up with them; two status polls arriving together would
# otherwise both get past a bare membership test.
_crash_sweeping = set()
_crash_lock = threading.Lock()

CRASH_POLL = 5.0            # seconds between checks of a running QEMU


def _claim_crash(index):
    """Take the crash teardown for this instance, if nothing else holds
    it: a teardown already running, a stop somebody asked for, or a start
    in progress all outrank it."""
    with _crash_lock:
        if (index in _crash_sweeping or index in _stopping
                or index in _starting):
            return False
        _crash_sweeping.add(index)
        return True


def _tear_down_stack(api, inst, d, pids, why, log):
    """Signal this instance's own helpers, free its ports, drop its sink.

    Split out so _crash_teardown's own finally can take the generation off
    the record whatever happens in here.
    """
    index = inst["index"]
    vnc, ws, qmp_port, audio_ws = api.ports_of(inst)
    display_num = vnc - 5900
    log.write(("[pcatgl] %s: QEMU is gone (%s); taking this instance's "
               "display stack down\n"
               % (time.strftime("%F %T"), why)).encode())
    log.flush()
    # QEMU itself first: nothing waited for it, so it is a zombie until
    # something does, and a zombie holds its pid against the next
    # generation.
    _reap_exited(pids.values())
    _kill_pids_verified(pids, _instance_tokens(index, display_num, vnc,
                                               qmp_port), log)
    # websockify forks a child per browser connection and that child
    # inherits the listening socket; x11vnc can leave one adopted by init.
    # killpg reaches the first (it is in the group of the parent it was
    # forked from) but not the second, so ask the kernel who still holds
    # the ports rather than trusting the record.
    _free_ports((vnc, ws, qmp_port, audio_ws, 4620 + index), log)
    for stale in _find_sink_modules(_sink_name(inst)):
        _pactl(["unload-module", stale])


def _crash_teardown(api, inst, why):
    """Take this instance's display stack down after QEMU ended on its own.

    _stop_now is the only other place this happens, and it is reached only
    when somebody asks for a stop.  A QEMU that dies by itself -- SIGSEGV
    is the case in hand -- leaves weston holding the compositor socket, the
    x11vnc loop respawning on the display, and both websockify holding
    their ports; each is its own session, so QEMU's death reaches none of
    them.  Until this, the next on_start was the only thing that swept
    them, which could be days away, and in the meantime the console still
    answers -- with the guest's last frame -- and the ports stay taken.
    """
    index = inst["index"]
    if not _claim_crash(index):
        return
    try:
        d = _inst_dir(api, inst)
        pids = _load_pids(d)
        if not pids:
            return
        try:
            log = open(os.path.join(d, "pcatgl.log"), "ab")
        except OSError:
            return
        try:
            _tear_down_stack(api, inst, d, pids, why, log)
        finally:
            # Whatever went wrong on the way, the generation must come off
            # the record: is_up would otherwise call this again on the next
            # status poll, and the one after that, for as long as anyone is
            # looking.  Anything that survived is the next on_start's to
            # sweep -- exactly where it stood before any of this.
            _save_pids(d, {})
            log.close()
    finally:
        _crash_sweeping.discard(index)


def _start_crash_teardown(api, inst, why):
    """Off the caller's thread.

    is_up is reached both with the core's global _lock held (the action
    endpoints take it around the whole dispatch) and without it (the status
    endpoints take it only to find the instance).  A teardown takes seconds,
    and _lock is not reentrant, so spending them here would wedge every
    other endpoint for as long as it ran.
    """
    if inst["index"] in _crash_sweeping:
        return
    threading.Thread(target=_crash_teardown, args=(api, inst, why),
                     daemon=True).start()


def _watch_for_crash(api, inst, qemu_pid, qmp_port):
    """Notice QEMU ending on its own, and take the stack down with it.

    Keyed on the pid of the generation that armed it: a later start writes
    a different pid into pids.json and this returns without touching the
    new generation.  It polls rather than waiting on the Popen -- that
    handle belongs to on_start, and the manager reads poll()/returncode
    elsewhere, so the exit status is not this thread's to collect.
    _reap_exited does that, once, from the teardown.
    """
    needle = ("tcp:127.0.0.1:%d," % qmp_port).encode()
    d = _inst_dir(api, inst)
    index = inst["index"]
    while True:
        time.sleep(CRASH_POLL)
        if _load_pids(d).get("qemu") != qemu_pid:
            return              # stopped, or a newer generation took over
        if index in _stopping:
            return              # on_stop is already taking it down
        if _pid_matches(qemu_pid, needle):
            continue
        _crash_teardown(api, inst,
                        "pid %d ended without being asked to" % qemu_pid)
        return


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
    delivered = True
    try:
        _qmp_command(qmp_port, "quit")
    except OSError:
        delivered = False
    # only wait for a clean self-exit if quit actually reached QEMU; if QMP
    # was unreachable it is not going to quit on its own, so go to TERM now
    # rather than idling the full 15s
    if delivered:
        deadline = time.time() + 15
        while qemu and _alive(qemu) and time.time() < deadline:
            time.sleep(0.25)
    _kill_pids(pids)
    # drop this instance's null sink, or a stale one lingers on the host
    for stale in _find_sink_modules(_sink_name(inst)):
        _pactl(["unload-module", stale])
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
    qmp_port = api.ports_of(inst)[2]
    pids = _load_pids(_inst_dir(api, inst))
    up = _pid_matches(pids.get("qemu"),
                      ("tcp:127.0.0.1:%d," % qmp_port).encode())
    if not up and pids:
        # A generation is on record but its QEMU is not there: a crash the
        # watch thread did not catch, which is what a restart of this
        # manager leaves behind -- the watch does not survive one, and the
        # orphans do.  The core asks this on every status poll, so this is
        # where a restarted manager finds them.
        _start_crash_teardown(api, inst, "recorded QEMU gone, seen by is_up")
    return up


# --- FPS mouselook ---------------------------------------------------------

# What each instance's console last said its buttons were, so a mask can be
# turned into the press and release transitions QEMU's input layer wants.
# Keyed by index; cleared when the mask goes back to 0, which the console
# sends when the toggle goes off, so nothing is left held down in the guest.
_fps_buttons = {}

# DOM button order, which is what capturePointer's mask is built from.
_FPS_BUTTON = {0: "left", 1: "middle", 2: "right"}

# One flush is a single animation frame's worth of movement.  Anything
# larger did not come from a hand on a mouse.
_FPS_MAX_DELTA = 4096


def pcatgl_fps_input(api, inst, data):
    """One flush of the console's captured pointer, as RELATIVE motion.

    The console already holds true relative deltas -- Pointer Lock gives
    movementX/Y -- but RFB can only carry an absolute position, so the
    normal path integrates them, x11vnc warps the X pointer there, and the
    guest's usb-tablet reports a coordinate.  A game that looks around by
    reading the cursor, taking its distance from centre and warping it
    back (SiN, and every Quake-era engine) gets nothing back from its own
    warp: the tablet still reports where the host pointer is, the next
    read is the same large distance, and the view pins to an edge.

    So this hands the deltas to the guest's PS/2 mouse instead, which is
    always there on this machine type alongside the tablet (query-mice:
    "QEMU PS/2 Mouse", absolute false), over QMP -- which takes x11vnc, X
    and SDL out of the input path altogether, and with them the warp
    fighting that a relative grab on the X side would bring back.

    Dispatched with the core's global _lock held, like every plugin
    action, so the QMP timeout here is short on purpose: a machine that
    has stopped answering must not hold that lock for a second on every
    frame.  The console stops sending after one failure.
    """
    try:
        dx = int(data.get("dx") or 0)
        dy = int(data.get("dy") or 0)
        mask = int(data.get("buttons") or 0)
    except (TypeError, ValueError):
        return (400, "dx, dy and buttons must be whole numbers")
    if abs(dx) > _FPS_MAX_DELTA or abs(dy) > _FPS_MAX_DELTA:
        return (400, "a frame of movement is not that large")

    events = []
    if dx:
        events.append({"type": "rel", "data": {"axis": "x", "value": dx}})
    if dy:
        events.append({"type": "rel", "data": {"axis": "y", "value": dy}})
    index = inst["index"]
    was = _fps_buttons.get(index, 0)
    if mask != was:
        for bit, button in sorted(_FPS_BUTTON.items()):
            if (mask ^ was) & (1 << bit):
                events.append({"type": "btn",
                               "data": {"button": button,
                                        "down": bool(mask & (1 << bit))}})
        if mask:
            _fps_buttons[index] = mask
        else:
            _fps_buttons.pop(index, None)
    if not events:
        return {"result": "nothing to send"}

    qmp_port = api.ports_of(inst)[2]
    try:
        _qmp_command(qmp_port, "input-send-event", {"events": events},
                     timeout=0.3)
    except OSError as exc:
        # the console turns itself off on this rather than repeating it
        return (503, "the machine is not answering: %s" % exc)
    return {"result": "sent", "events": len(events)}


# --- QMP (a minimal client; the core never speaks to an engine over QMP) ---

def _qmp_command(port, command, arguments=None, timeout=1):
    """Open the QMP port, negotiate capabilities, run one command, close.
    Raises OSError if the port is not there (a stopped or fallback-less
    instance) -- callers treat that as nothing to do.

    A fresh connection every time, and deliberately so: QEMU serves one
    QMP client at a time on a socket, so a connection held open for the
    console's pointer would leave stop, reset and the thumbnail with
    nowhere to go -- measured 2026-09-15, the second client is accepted
    and then never greeted.  The connect and capability exchange cost
    about 1.08 ms on loopback, against 0.78 ms for a held-open one: not a
    trade worth taking the monitor for.
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
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
    fps = _console_fps(inst)
    hw = {
        "machine": PCATGL_MACHINE_LABEL,
        "cpu": PCATGL_CPU_LABEL,
        "video": PCATGL_VGA_LABELS.get(vga, vga) + " (3dfx GL, retrace=precise)",
        "memory": inst.get("memory") or "256M",
        "sound": PCATGL_SOUND_LABEL,
        "bios": ("SeaBIOS (PnP disabled)" if _nopnp_bios(api)
                 else "SeaBIOS (default)"),
        "acpi": "ACPI enabled",
        "fpslimit": "%d FPS" % fps,
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
