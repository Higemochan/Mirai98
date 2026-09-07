"""86Box machine plugin for the Mirai98 web manager.

Adds a "box86" machine type backed by 86Box instead of QEMU: a Socket 7
board (tx97, 430TX) with a genuine Pentium MMX (pentium_p55c) and a 3Dfx
Voodoo2 in passthrough beside an S3 ViRGE/DX, for Windows 9x titles that
want a Glide-capable card rather than PC-98's own graphics.

86Box has no QMP and no built-in VNC/websocket server of its own the way
QEMU does, so this plugin does not build a QEMU argv at all: it registers
an *engine* (PluginAPI.add_engine) that start_instance/stop_instance/
is_running call in full instead of the usual QEMU flow. What it starts,
per instance:

  Xvfb :N (N = the instance's VNC display number, so it never collides
           with another instance's)         -- an off-screen X server
  86Box itself, QT_QPA_PLATFORM=xcb against that display -- the vnc
           platform's keyboard path needs a real /dev/input, which an
           LXC has none of, and drops every keystroke; xcb has no such
           requirement and is what a real X server gives it anyway
  x11vnc  -- mirrors that whole display out as a real VNC server, on
           the instance's usual VNC port, so the existing noVNC front
           end needs no changes at all
  a PulseAudio null sink, one per instance -- 86Box's OpenAL/PulseAudio
           backend (ALSOFT_DRIVERS=pulse, PULSE_SINK=<the sink>) renders
           into it instead of real hardware, which this box has none of
  ffmpeg   -- captures that sink's .monitor and encodes it live to Opus/
           WebM, over a private TCP port
  websockify -- wraps that TCP port as the audio websocket ports_of()
           hands out (audio_ws), which the front end's <audio>/MSE
           player connects to directly, the same way its video half
           talks to the ws port

Everything is looked up and torn down by pid, kept in <inst>/box86/pids
.json across a server restart the same way qemu.pid does for QEMU, so a
second instance's stop never has to guess which of several same-named
processes on the host is its own -- there is no "pkill -x 86Box" or
"pkill -x Xvfb" anywhere here, on purpose: those match every instance's
process at once.

MVP: one machine, a fixed hardware preset, a seed disk copied in on
first start. The matching front-end lives in web/plugins/box86.js.
"""

import configparser
import json
import os
import shutil
import socket
import subprocess
import time

# where the appliance keeps 86Box itself: the extracted AppImage and the
# ROM set it was launched from during development. A packaged appliance
# will want these under its own tree; this is the one place that changes.
BOX86_ROOT = os.environ.get("BOX86_ROOT", "/root/86box-poc")
BOX86_BIN = os.path.join(BOX86_ROOT, "squashfs-root", "AppRun")
BOX86_ROMS = os.path.join(BOX86_ROOT, "roms")
# a known-good Windows 95 install, copied in as an instance's disk the
# first time it starts, so a fresh instance boots to a desktop rather
# than "no bootable device" -- proof the console and input work, not
# just that 86Box came up
SEED_HDD = os.path.join(BOX86_ROOT, "vm", "hdd", "hdd0.vhd")

# a Socket 7 board of the Voodoo2 era, a genuine Pentium MMX (not the
# pre-MMX P54C the same package also offers), an S3 ViRGE/DX for the
# desktop and a Voodoo2 beside it in passthrough. Every id below is
# copied from 86Box's own machine_table.c/cpu_table.c/vid_table.c --
# none of it is a guess, and "pentium_p55c" in particular is the real
# internal_name; "pentium_mmx" is not a name 86Box knows at all.
CFG_TEMPLATE = """[General]
vid_renderer = qt_software

[Machine]
machine = tx97
cpu_family = pentium_p55c
cpu_multi = 3
cpu_speed = 200000000
cpu_use_dynarec = 1
fpu_type = internal
mem_size = 65536

[Input devices]
mouse_type = ps2

[Video]
gfxcard = virge_dx_pci
voodoo = 1

[S3 ViRGE/DX PCI]
bios = virge375_pci

[3Dfx Voodoo Graphics]
type = 2
framebuffer_memory = 4
texture_memory = 4
render_threads = 4
"""
# The disks live in their own section, [Hard disks] plus [Floppy and
# CD-ROM drives], written by _sync_disks below instead of living in this
# template: unlike everything above (which 86Box negotiates for itself
# after this seeds it once and is then left alone), what is in each
# drive is Mirai98's own to say, from the instance's own hdd1/fdd1/fdd2/
# cd fields the same way any PC-98 or Towns machine's are -- and has to
# be re-written every start to track whatever Storage was last told to
# attach, not just seeded once.
FDD_TYPE = "35_2hd"    # 3.5" 1.44M -- fdd.c's own internal_name, verified


def register(api):
    api.add_machine("box86", platform="dosv")
    api.add_engine("box86", {
        "on_start": lambda inst: on_start(api, inst),
        "on_stop": lambda inst: on_stop(api, inst),
        "is_up": lambda inst: is_up(api, inst),
        "pid": lambda inst: _load_pids(_box_dir(api, inst)).get("86box"),
    })


# ------------------------------------------------------------- lifecycle

def _box_dir(api, inst):
    d = os.path.join(api.inst_dir(inst), "box86")
    os.makedirs(os.path.join(d, "hdd"), exist_ok=True)
    return d


def _disk_paths(api, inst, d):
    """(hdd, fdd1, fdd2, cd): each resolved the same way any PC-98 or
    Towns machine's own hdd1/fdd1/fdd2/cd would be, against this
    instance's own (dosv) Storage shelf. hdd1 alone still falls back to
    a private copy of the seed image when nothing was attached -- the
    one field this MVP still defaults on its own, so an instance nobody
    has touched Storage for still boots to something."""
    hdd = api.disk_path(inst, "hdd1")
    if not hdd:
        seed_dir = os.path.join(d, "hdd")
        os.makedirs(seed_dir, exist_ok=True)
        hdd = os.path.join(seed_dir, "hdd0.vhd")
        if not os.path.exists(hdd) and os.path.exists(SEED_HDD):
            shutil.copy2(SEED_HDD, hdd)
    return (hdd, api.disk_path(inst, "fdd1"), api.disk_path(inst, "fdd2"),
            api.disk_path(inst, "cd"))


def _sync_disks(api, inst, cfg_path, d):
    """Write this instance's own attached disks into its cfg. Read with
    configparser and written back whole, so [Machine]/[Video]/etc(
    whatever 86Box itself last negotiated there) survive untouched --
    only [Hard disks] and [Floppy and CD-ROM drives] are ever touched
    here, and every start, not just the first: Storage may have
    attached something different since the last one.
    """
    hdd, fdd1, fdd2, cd = _disk_paths(api, inst, d)
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if os.path.exists(cfg_path):
        cp.read(cfg_path, encoding="utf-8")
    if not cp.has_section("Hard disks"):
        cp.add_section("Hard disks")
    cp.set("Hard disks", "hdd_01_fn", hdd)
    cp.set("Hard disks", "hdd_01_ide_channel", "0:0")
    cp.set("Hard disks", "hdd_01_parameters", "63, 16, 4161, 0, ide")
    cp.set("Hard disks", "hdd_01_speed", "ramdisk")
    cp.set("Hard disks", "hdd_01_vhd_blocksize", "4096")
    fc = "Floppy and CD-ROM drives"
    if not cp.has_section(fc):
        cp.add_section(fc)
    for n, path in ((1, fdd1), (2, fdd2)):
        key_type, key_fn = "fdd_%02i_type" % n, "fdd_%02i_fn" % n
        cp.set(fc, key_type, FDD_TYPE if path else "none")
        if path:
            cp.set(fc, key_fn, path)
        elif cp.has_option(fc, key_fn):
            cp.remove_option(fc, key_fn)
    # sound_on=1 on the first drive unmutes CD audio, matching 86Box's
    # own default when this key is absent at all. The bus string here is
    # hdd_string_to_bus's own vocabulary, not the [Hard disks] one --
    # "ide" resolves to HDD_BUS_IDE, which a CD-ROM drive never matches
    # (config.c only reads cdrom_01_ide_channel/image_path once bus_type
    # == CDROM_BUS_ATAPI); "atapi" is the real, distinct string that
    # actually gets there. Confirmed live, 2026-09-07: "1, ide" left the
    # drive listed as "(Unknown Bus)" in 86Box's own Media menu, with no
    # channel and no image ever accepted.
    cp.set(fc, "cdrom_01_parameters", "1, atapi")
    cp.set(fc, "cdrom_01_ide_channel", "1:0")
    cp.set(fc, "cdrom_01_image_path", cd or "")
    with open(cfg_path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=True)


def _ensure_cfg(api, inst):
    """The instance's own 86box.cfg. Everything but the disks is
    written once (86Box rewrites its cfg on exit with whatever it
    actually negotiated, CPU speed included, the same way towns.py
    seeds a CMOS file once and then leaves the machine's own copy
    alone) -- the disks themselves are re-synced on every call, in
    _sync_disks, since which ones are attached is Storage's to say and
    can change between one start and the next.
    """
    d = _box_dir(api, inst)
    cfg_path = os.path.join(d, "86box.cfg")
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(CFG_TEMPLATE)
    _sync_disks(api, inst, cfg_path, d)
    return d


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
        return True
    except OSError:
        return False


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _sink_name(inst):
    return "box86_%d" % inst["index"]


def on_start(api, inst):
    index = inst["index"]
    vnc, ws, _qmp, audio_ws = api.ports_of(inst)
    display_num = vnc - 5900
    d = _ensure_cfg(api, inst)
    sink = _sink_name(inst)
    # 86Box's own audio capture bridges over a private TCP port that
    # only websockify and ffmpeg ever touch; picking it off the same
    # index keeps two instances' bridges apart the same way their other
    # ports are
    audio_tcp = 4620 + index

    pids = {}

    def spawn(key, argv, **kw):
        proc = subprocess.Popen(argv, start_new_session=True, **kw)
        pids[key] = proc.pid
        return proc

    log_path = os.path.join(d, "box86.log")
    log = open(log_path, "ab")
    try:
        spawn("xvfb", ["Xvfb", ":%d" % display_num,
                       "-screen", "0", "1024x768x24"],
              stdout=log, stderr=log)
        for _ in range(30):
            if os.path.exists("/tmp/.X11-unix/X%d" % display_num):
                break
            time.sleep(0.1)

        subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             "sink_name=%s" % sink,
             "sink_properties=device.description=%s" % sink],
            capture_output=True, timeout=5, check=False)

        env = dict(os.environ, DISPLAY=":%d" % display_num,
                   QT_QPA_PLATFORM="xcb",
                   ALSOFT_DRIVERS="pulse",
                   PULSE_SINK=sink)
        box_proc = spawn("86box",
                         [BOX86_BIN, "-P", d, "-R", BOX86_ROMS],
                         cwd=d, env=env, stdout=log, stderr=log)

        spawn("x11vnc",
              ["x11vnc", "-display", ":%d" % display_num, "-forever",
               "-shared", "-rfbport", str(vnc), "-nopw", "-q"],
              stdout=log, stderr=log)
        spawn("websockify_video",
              ["websockify", str(ws), "127.0.0.1:%d" % vnc],
              stdout=log, stderr=log)
        # ffmpeg's own "-listen 1" serves exactly one TCP client for its
        # whole life: once that client (websockify_audio, on the far side
        # of the browser's own connection) goes away, its write fails
        # with a broken pipe and ffmpeg exits rather than waiting for a
        # next one -- confirmed live, 2026-09-07: a single browser
        # connect/disconnect cycle left the bridge permanently dead until
        # the whole instance was restarted. A tiny shell loop respawns it
        # the moment that happens, so the next connection is only ever a
        # fraction of a second behind, not gone for good; it shares
        # ffmpeg's own process group (this whole line runs under one
        # spawn(), one setsid()), so stopping this instance's "ffmpeg"
        # entry by process GROUP, not just its top pid, takes the loop
        # and whichever ffmpeg it is currently running down together.
        spawn("ffmpeg",
              ["bash", "-c",
               "while true; do ffmpeg -nostdin -loglevel error "
               "-f pulse -i '%s.monitor' -c:a libopus -b:a 64k "
               "-f webm -listen 1 tcp://127.0.0.1:%d; sleep 0.2; done"
               % (sink, audio_tcp)],
              stdout=log, stderr=log)
        spawn("websockify_audio",
              ["websockify", str(audio_ws), "127.0.0.1:%d" % audio_tcp],
              stdout=log, stderr=log)
    except OSError as exc:
        _kill_pids(pids)
        log.close()
        return "failed: %s" % exc

    _save_pids(d, pids)

    for _ in range(40):
        if box_proc.poll() is not None:
            log.close()
            _kill_pids(pids)
            _save_pids(d, {})
            return "failed: 86Box exited during startup, see box86.log"
        if is_up(api, inst):
            log.close()
            return "started"
        time.sleep(0.25)
    log.close()
    return "started (slow to come up)"


def on_stop(api, inst):
    d = _box_dir(api, inst)
    pids = _load_pids(d)
    _kill_pids(pids)
    _save_pids(d, {})
    subprocess.run(["pactl", "unload-module",
                    _find_sink_module(_sink_name(inst))],
                   capture_output=True, timeout=5, check=False) \
        if _find_sink_module(_sink_name(inst)) else None
    return "stopped"


def is_up(api, inst):
    d = _box_dir(api, inst)
    pids = _load_pids(d)
    return _alive(pids.get("86box"))


def _kill_pids(pids):
    """SIGTERM every process this instance started, then SIGKILL whatever
    is still standing after a moment -- in pid order they were started,
    so 86Box (the one told to actually shut down cleanly) is asked
    first, before Xvfb pulls the display out from under it.

    By process GROUP, not just the one pid spawn() handed back: every
    entry here called its own setsid() (spawn's start_new_session=True),
    so its pid is also its pgid, and killpg reaches a child that pid
    itself spawned without becoming a group leader of its own -- the
    "ffmpeg" entry is actually a small shell loop respawning ffmpeg
    each time it exits, and killing only the shell would leave whichever
    ffmpeg it had just started running on its own.
    """
    for key in ("86box", "websockify_audio", "ffmpeg",
                "websockify_video", "x11vnc", "xvfb"):
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


def _find_sink_module(sink_name):
    try:
        out = subprocess.run(["pactl", "list", "short", "modules"],
                             capture_output=True, text=True,
                             timeout=5, check=False).stdout
    except OSError:
        return None
    for line in out.splitlines():
        if "module-null-sink" in line and ("sink_name=%s" % sink_name) in line:
            return line.split("\t", 1)[0]
    return None
