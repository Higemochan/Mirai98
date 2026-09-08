"""86Box machine plugin for the Mirai98 web manager.

Adds a "box86" machine type backed by 86Box instead of QEMU: a Slot 1
440BX board (p2bls, ASUS P2B-LS) with a real Pentium II (Klamath) at
266MHz and a 3Dfx Voodoo2 in passthrough beside an S3 ViRGE/DX, for
Windows 9x titles that want a Glide-capable card rather than PC-98's
own graphics.

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
import re
import shutil
import signal
import socket
import subprocess
import time

# where the appliance keeps 86Box's ROM set from development, and the
# binary itself: no longer the stock AppImage this MVP started from, but
# our own build of the same v6.0 source, from /root/86box-src, carrying
# the xinput2_mouse.cpp and qt_main.cpp patches described where they are
# applied (SIGUSR1 -> a real ACPI power-button press, and no re-centering
# warp for the VNC/XTEST pointer specifically). A packaged appliance will
# want these under its own tree; this is the one place that changes.
BOX86_ROOT = os.environ.get("BOX86_ROOT", "/root/86box-poc")
BOX86_BIN = os.environ.get("BOX86_BIN_PATH",
                           "/root/86box-src/build/src/86Box")
BOX86_ROMS = os.path.join(BOX86_ROOT, "roms")
# a known-good Windows 95 install, copied in as an instance's disk the
# first time it starts, so a fresh instance boots to a desktop rather
# than "no bootable device" -- proof the console and input work, not
# just that 86Box came up
SEED_HDD = os.path.join(BOX86_ROOT, "vm", "hdd", "hdd0.vhd")

# A Slot 1 440BX board of the Voodoo2 era (ASUS P2B-LS) and a real
# Pentium II (Klamath) at one of its own three genuine retail speed
# grades, 266MHz -- not Deschutes, whose own cpu_table.c entry for the
# same number is an out-of-spec underclock rather than a speed grade
# that chip ever actually shipped at; real Deschutes starts at 333MHz.
# An S3 ViRGE/DX for the desktop and a Voodoo2 beside it in passthrough,
# unchanged from before this board -- p2bls has no onboard video of its
# own to conflict with either (vid_device is NULL in machine_table.c).
# Every id below is copied from 86Box's own machine_table.c/cpu_table.c/
# config.c -- none of it is a guess: p2bls (MACHINE_TYPE_SLOT1,
# CPU_PKG_SLOT1, bus 50-112MHz/multi 1.5-8.0x, both comfortably covering
# 66.67MHz x4), pentium2_klamath's own "266" entry (rspeed=266666666,
# multi=4.0, CPU_REQUIRES_DYNAREC -- cpu_use_dynarec=1 stays mandatory,
# not just a performance choice), and config.c's own CPU match itself
# (an exact rspeed/multi equality against cpu_speed/cpu_multi, so these
# two numbers are the only ones that matter, never a rounded distance).
# p2bls's own ROM (roms/machines/p2bls/1014ls.003) is confirmed present
# against the exact path/filename m_at_slot1.c's own bios_load_linear
# call names, the same way tx97's single-file ROM directory already was.
CFG_TEMPLATE = """[General]
vid_renderer = qt_software
start_in_fullscreen = 1
video_fullscreen_scale = 0

[Machine]
machine = p2bls
cpu_family = pentium2_klamath
cpu_multi = 4
cpu_speed = 266666666
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
# fdd_load (src/floppy/fdd.c) picks a loader purely off the file's own
# extension against this fixed table (loaders[], same file) and, on no
# match, clears the drive's filename outright rather than refusing to
# start it or falling back to a generic raw loader -- confirmed live,
# 2026-09-08, by strace: a plain, valid, correctly-sized raw image named
# .raw (the PC-98/Towns shelf's own usual floppy extension) was opened,
# probed and closed once at boot and never touched again, because ".raw"
# is not one of these and 86Box silently drops it instead of erroring.
FDD_EXTS = {"001", "002", "003", "004", "005", "006", "007", "008", "009",
           "010", "12", "144", "360", "720", "86f", "bin", "cq", "cqm",
           "ddi", "dsk", "fdi", "fdf", "flp", "hdm", "ima", "imd", "img",
           "json", "mfm", "td0", "vfd", "xdf"}

# Standard MS-DOS FAT12 floppy layouts (the same ones FORMAT.COM has laid
# out on real PC/AT hardware since the 1980s -- box86 is a stock PC/AT
# FDC regardless of which board CFG_TEMPLATE names, not FM TOWNS' or
# PC-98's own non-standard media, so these are the ordinary IBM-
# compatible geometries, not something to invent per platform the way
# towns.py's TOWNS_FLOPPIES table has to.
# Same 8-tuple shape as that table: bytes/sector, sectors/cluster, root
# entries, total sectors, media byte, sectors/FAT, sectors/track, heads.
BOX86_FLOPPIES = {
    "box86-144": (512, 1, 224, 2880, 0xf0, 9, 18, 2),   # 3.5" 1.44M
    "box86-120": (512, 1, 224, 2400, 0xf9, 7, 15, 2),   # 5.25" 1.2M
    "box86-720": (512, 2, 112, 1440, 0xf9, 3, 9, 2),    # 3.5" 720K
    "box86-360": (512, 2, 112, 720, 0xfd, 2, 9, 2),     # 5.25" 360K
}


def box86_new_floppy(dest, data):
    """An empty FAT12 floppy image, .img so fdd.c's own loaders[] table
    (FDD_EXTS above) opens it directly -- no _fdd_compatible_path
    symlink needed for one made here."""
    import struct
    fmt = str(data.get("format") or "")
    bps, spc, root, total, media, spf, spt, heads = BOX86_FLOPPIES[fmt]
    label = (str(data.get("label") or "NO NAME").upper()[:11]).ljust(11)
    boot = bytearray(bps)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", boot, 11, bps, spc, 1, 2, root, total,
                     media, spf, spt, heads, 0, 0)
    boot[36] = 0x00                       # drive number
    boot[38] = 0x29                       # extended boot signature
    struct.pack_into("<I", boot, 39, 0x12345678)
    boot[43:54] = label.encode("ascii", "replace")
    boot[54:62] = b"FAT12   "
    boot[bps - 2:bps] = b"\x55\xaa"
    fat = bytearray(spf * bps)
    fat[0:3] = bytes([media, 0xff, 0xff])
    root_dir = bytearray(root * 32)
    root_dir[0:11] = label.encode("ascii", "replace")
    root_dir[11] = 0x08                   # volume label entry
    image = bytearray(total * bps)
    image[0:bps] = boot
    off = bps
    for _ in range(2):
        image[off:off + len(fat)] = fat
        off += len(fat)
    image[off:off + len(root_dir)] = root_dir
    with open(dest, "wb") as f:
        f.write(image)


def hdd_chs(size_mb):
    """86Box's own geometry for a size, not invented here: the exact
    algorithm hdd_image_calc_chs (src/disk/hdd_image.c) uses, itself
    the Virtual Hard Disk Image Format Specification's own CHS
    calculation appendix. A freshly made image's own hdd_01_parameters
    (see _sync_disks) has to agree with what 86Box would compute for
    it, or the two disagreeing on where the disk ends is exactly the
    kind of mismatch a flat, headerless image has no other way to
    catch.
    """
    ts = size_mb << 11
    if ts > 65535 * 16 * 255:
        ts = 65535 * 16 * 255
    if ts >= 65535 * 16 * 63:
        spt, heads = 255, 16
        cth = ts // spt
    else:
        spt = 17
        cth = ts // spt
        heads = (cth + 1023) // 1024
        if heads < 4:
            heads = 4
        if cth >= (heads * 1024) or heads > 16:
            spt, heads = 31, 16
            cth = ts // spt
        if cth >= (heads * 1024):
            spt, heads = 63, 16
            cth = ts // spt
    cyl = cth // heads
    return cyl, heads, spt


def box86_new_hard_disk(dest, data):
    """A blank IDE hard disk image: all zeros, sized to exactly what
    hdd_chs's own geometry for the requested size adds up to (not just
    the requested megabytes rounded down) -- to partition and format
    from the guest OS, as on real hardware. .img, never .vhd: the
    latter needs a real footer (image_is_vhd, hdd_image.c, picks the
    container purely off that one extension) an all-zero file does not
    have, and this shelf's own images already are not the SEED_HDD.vhd
    box86.py copies in on a fresh instance's own first start."""
    cyl, heads, spt = hdd_chs(int(data.get("size") or 40))
    with open(dest, "wb") as f:
        f.truncate(cyl * heads * spt * 512)


def register(api):
    api.add_machine("box86", platform="dosv")
    api.add_engine("box86", {
        "on_start": lambda inst: on_start(api, inst),
        "on_stop": lambda inst: on_stop(api, inst),
        "is_up": lambda inst: is_up(api, inst),
        "pid": lambda inst: _load_pids(_box_dir(api, inst)).get("86box"),
        "thumbnail": lambda inst, png: box86_thumbnail(api, inst, png),
    })
    api.machine_sanitize("box86", box86_sanitize)
    for fmt in BOX86_FLOPPIES:
        api.disk_builder("dosv", "fdd", fmt, box86_new_floppy)
    api.disk_builder("dosv", "hdd", "box86-hdd", box86_new_hard_disk)


def box86_sanitize(record):
    """What a box86 record may hold: hdd1/fdd1/fdd2/cd are all
    _sync_disks ever reads (see _disk_paths); hdd2 and the four SCSI
    slots are QEMU-machine fields that would sit there validated and
    saved but never actually reach 86Box's cfg at all.

    midi is deliberately left alone here: sanitize() (pc98web.py) has
    already validated it against MIDI_MODES for every machine before
    this ever runs, and _sync_midi below is what gives "synth" its own
    real meaning for box86 -- a standalone MPU-401 into 86Box's own
    FluidSynth, not pc98/towns' MPU-PC98II -- so unlike the QEMU-only
    fields above, this is one box86 actually reads and must keep.
    """
    for key in ("hdd2", "scsi1", "scsi2", "scsi3", "scsi4"):
        record[key] = ""
    return None


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


def _fdd_compatible_path(d, slot, path):
    """A path box86.py can actually put in fdd_0<slot>_fn: unchanged if
    its own extension is already one fdd.c's loaders[] table knows,
    otherwise a symlink to it, in this instance's own box86/ directory,
    under a name whose extension (.img) that table does know -- so a
    dosv-shelf image keeps the name Storage gave it (a PC-98/Towns-style
    .raw among them) and 86Box still opens it. Purely an internal
    implementation detail of what this instance hands 86Box: Storage's
    own bookkeeping (disk_path, used_by) still only ever knows the real
    shelf file, never this symlink.
    """
    if not path:
        return ""
    if os.path.splitext(path)[1].lstrip(".").lower() in FDD_EXTS:
        return path
    link = os.path.join(d, "fdd%d.img" % slot)
    try:
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(path, link)
    except OSError:
        return path
    return link


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
    # A real VHD (SEED_HDD's own copy) carries its own footer and 86Box
    # reads its geometry from that, ignoring this string entirely once
    # loaded (hdd_image.c) -- but a flat .img made by box86_new_hard_disk
    # has no such footer, and needs this to be right, computed the same
    # way 86Box's own hdd_chs would for whatever size the attached file
    # actually is; sizing it off the file rather than trusting some
    # fixed value is what makes either case correct without telling
    # them apart here at all.
    try:
        size_mb = os.path.getsize(hdd) >> 20 if hdd else 0
    except OSError:
        size_mb = 0
    cyl, heads, spt = hdd_chs(size_mb or 40)
    cp.set("Hard disks", "hdd_01_fn", hdd)
    cp.set("Hard disks", "hdd_01_ide_channel", "0:0")
    cp.set("Hard disks", "hdd_01_parameters",
           "%d, %d, %d, 0, ide" % (spt, heads, cyl))
    cp.set("Hard disks", "hdd_01_speed", "ramdisk")
    cp.set("Hard disks", "hdd_01_vhd_blocksize", "4096")
    fc = "Floppy and CD-ROM drives"
    if not cp.has_section(fc):
        cp.add_section(fc)
    for n, path in ((1, fdd1), (2, fdd2)):
        path = _fdd_compatible_path(d, n, path)
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


# "synth" (inst["midi"]) is the same abstract choice pc98/towns' own midi
# field already means -- MIDI_MODES, pc98web.py, untouched here -- box86
# just wires it to different, real hardware of its own: a standalone
# MPU-401 (snd_mpu401.c, independent of any sound card) into 86Box's own
# built-in FluidSynth MIDI-out device (midi_fluidsynth.c). Both confirmed
# against 86Box's own source (/root/86box-src), not guessed:
#   - config.c's load_sound()/save_sound() read and write midi_device and
#     mpu401_standalone as plain [Sound] keys, the same section sndcard
#     already lives in; mpu401_standalone=1 alone is enough for 86Box to
#     add the device itself (sound.c: "if (mpu401_standalone_enable)
#     mpu401_device_add();"), no other wiring needed.
#   - each device's own settings live in a section named after its own
#     device_t.name verbatim -- the same pattern CFG_TEMPLATE's own
#     [S3 ViRGE/DX PCI] and [3Dfx Voodoo Graphics] sections already use --
#     giving "FluidSynth" (sound_font) and "Roland MPU-IPC-T" (base, irq,
#     receive_input), the device_t names snd_mpu401.c/midi_fluidsynth.c
#     themselves declare for the standalone (non-MCA) ISA device and the
#     synth device respectively.
#   - FluidSynth's rendered PCM reaches 86Box's own central mixer through
#     the same generic path any other sound source does (sound.c:
#     "midi_poll();", called every mix cycle) -- already flowing out
#     through the very PulseAudio null-sink/ffmpeg capture _sink_name/
#     _spawn wire up per instance, so none of that needs touching either.
MPU401_SECTION = "Roland MPU-IPC-T"    # snd_mpu401.c's own device_t.name


def _sync_midi(api, inst, cfg_path):
    """Like _sync_disks: read whole, touch only what this owns, write
    whole back, every start -- whether this is on is Storage's own
    instance field to say and can change between one start and the
    next, the same as which disks are attached.
    """
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if os.path.exists(cfg_path):
        cp.read(cfg_path, encoding="utf-8")
    if not cp.has_section("Sound"):
        cp.add_section("Sound")
    if inst.get("midi") == "synth":
        cp.set("Sound", "midi_device", "fluidsynth")
        cp.set("Sound", "mpu401_standalone", "1")
        if not cp.has_section("FluidSynth"):
            cp.add_section("FluidSynth")
        # The same shared SoundFont pc98/towns' own MIDI synth already
        # plays through (pc98web.py/towns.py: cfg.get("soundfont")) --
        # read from config, never hardcoded, so this resolves correctly
        # on the scratch server's own config too, not just production's.
        cp.set("FluidSynth", "sound_font", api.CONFIG.get("soundfont") or "")
        if not cp.has_section(MPU401_SECTION):
            cp.add_section(MPU401_SECTION)
        cp.set(MPU401_SECTION, "base", "0x330")
        cp.set(MPU401_SECTION, "irq", "2")
        # No real MIDI-in device exists to feed this; 1 (86Box's own
        # default) would just have it probe for one that is never there.
        cp.set(MPU401_SECTION, "receive_input", "0")
    else:
        for key in ("midi_device", "mpu401_standalone"):
            if cp.has_option("Sound", key):
                cp.remove_option("Sound", key)
        for section in ("FluidSynth", MPU401_SECTION):
            if cp.has_section(section):
                cp.remove_section(section)
    with open(cfg_path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=True)


def _ensure_cfg(api, inst):
    """The instance's own 86box.cfg. Everything but the disks and MIDI
    is written once (86Box rewrites its cfg on exit with whatever it
    actually negotiated, CPU speed included, the same way towns.py
    seeds a CMOS file once and then leaves the machine's own copy
    alone) -- the disks and whether a MIDI module is fitted are
    re-synced on every call (_sync_disks, _sync_midi), since both are
    Storage/the instance record's own to say and can change between
    one start and the next.
    """
    d = _box_dir(api, inst)
    cfg_path = os.path.join(d, "86box.cfg")
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(CFG_TEMPLATE)
    _sync_disks(api, inst, cfg_path, d)
    _sync_midi(api, inst, cfg_path)
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


# start_in_fullscreen (CFG_TEMPLATE, [General]) hides 86Box's own menu
# bar and toolbar, but on a bare Xvfb with no window manager at all
# there is nobody to grant its _NET_WM_STATE_FULLSCREEN request either
# -- the top-level window is left at its natural, guest-resolution-
# sized geometry (640x472+0+0 for a 640x480 VGA mode, confirmed live
# via xwininfo, 2026-09-07) inside the larger 1024x768 Xvfb screen
# x11vnc used to export whole. That gap is exactly the "black bars"
# a real-browser test found: noVNC's canvas spanned the full 1024x768,
# so a click anywhere past the guest's own small corner of it landed
# nowhere near what the user was looking at.
#
# The fix is not to make 86Box's window bigger (there is still no WM
# to ask), but to stop exporting anything past it: x11vnc's -id tracks
# one window's own real pixels instead of the whole display, and (per
# its own -help text) engages the same -xrandr mechanism to follow
# that window if it resizes later -- a guest switching to a taller
# SVGA mode mid-session keeps working, with no static clip rectangle
# to fall out of date. With the canvas now exactly the window's own
# client area, a click's canvas coordinates and the guest's own screen
# coordinates are the same numbers -- confirmed live: 2026-09-07.
_WIN_RE = re.compile(
    r'^\s*(0x[0-9a-fA-F]+)\s+"[^"]*":\s*\([^)]*"86Box"\)\s+'
    r'(\d+)x(\d+)\+', re.MULTILINE)


def _find_box_window(display_num):
    """The real, on-screen 86Box top-level -- not the 1x1 placeholder
    window or the 3x3 "Qt Selection Owner" xwininfo -tree also lists
    under the same WM_CLASS, which are 86Box's own Qt frontend's
    business and never what a viewer should see. Picked by WM_CLASS
    ("86Box", the second, class element xwininfo -tree prints) and a
    minimum size, not by title -- the title is "<vmname> - 86Box
    <version>" and vmname is whatever this instance's own 86box.cfg
    [General] vmname happens to be (or absent entirely).
    """
    try:
        out = subprocess.run(
            ["xwininfo", "-root", "-tree", "-display", ":%d" % display_num],
            capture_output=True, text=True, timeout=5, check=False).stdout
    except OSError:
        return None
    best, best_area = None, 0
    for m in _WIN_RE.finditer(out):
        w, h = int(m.group(2)), int(m.group(3))
        if w > 50 and h > 50 and w * h > best_area:
            best, best_area = m.group(1), w * h
    return best


def box86_thumbnail(api, inst, png):
    """This instance's own screen, the same job thumbnail() (pc98web.py)
    otherwise does with QMP's screendump -- 86Box has no QMP at all, so
    this captures the same X11 window x11vnc itself was told to track
    (_find_box_window, re-run fresh rather than remembered from on_start:
    cheap, and right even if that window were ever replaced) instead.
    Writes png directly if it can; leaves it alone (thumbnail() already
    treats a missing file as "no thumbnail yet") if it can't.
    """
    vnc, _ws, _qmp, _audio_ws = api.ports_of(inst)
    display_num = vnc - 5900
    win_id = _find_box_window(display_num)
    if not win_id:
        return
    raw = png + ".xwd.png"
    env = dict(os.environ, DISPLAY=":%d" % display_num)
    try:
        subprocess.run(["import", "-window", win_id, raw],
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

        # Wait for 86Box's own top-level window to exist before
        # starting x11vnc, so it can be told to track that window (-id)
        # instead of the whole Xvfb screen -- see _find_box_window.
        # A window that never shows up (a startup failure of some kind)
        # is not fatal here: falling back to exporting the whole
        # display keeps this instance debuggable instead of wedging
        # the start on a window that will never appear.
        win_id = None
        for _ in range(50):
            win_id = _find_box_window(display_num)
            if win_id or box_proc.poll() is not None:
                break
            time.sleep(0.2)

        x11vnc_argv = ["x11vnc", "-display", ":%d" % display_num]
        if win_id:
            x11vnc_argv += ["-id", win_id]
        x11vnc_argv += ["-forever", "-shared", "-rfbport", str(vnc),
                        "-nopw", "-q"]
        spawn("x11vnc", x11vnc_argv, stdout=log, stderr=log)
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
    """Ask 86Box's own guest to shut down first (a real ACPI/APM power
    button press, over SIGUSR1 -- a patch to our own build, since 86Box
    offers no QMP-like control socket of its own to ask for one any
    other way), and only fall back to _kill_pids's SIGTERM/SIGKILL
    escalation for whatever is left once that either finishes or times
    out.  A machine simply killed mid-run never gets to tell its own OS
    it is shutting down, and Windows in particular boots back into its
    own crash recovery next time as a direct result -- confirmed live,
    2026-09-08.
    """
    d = _box_dir(api, inst)
    pids = _load_pids(d)
    box_pid = pids.get("86box")
    if box_pid and _alive(box_pid):
        try:
            os.kill(box_pid, signal.SIGUSR1)
        except OSError:
            pass
        else:
            deadline = time.time() + 15
            while time.time() < deadline and _alive(box_pid):
                time.sleep(0.3)
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
