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
import threading
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
# SEED_HDD's own match: the NVR (CMOS) 86Box wrote once Windows had
# finished detecting every device on the machine that seed was actually
# installed and booted on. A seed's own hdd0.vhd and its own nvr/*.nvr
# are one matched set, not two independent files -- the NVR holds a
# checksum computed over the hardware configuration 86Box saw at the
# time, and the moment that stops matching what CFG_TEMPLATE's own
# [Machine] section actually describes (a different board, a different
# CPU -- this project's own p2bls/PII-266 switch is the real example),
# every boot re-detects the "changed" hardware from a blank NVR again:
# a CMOS checksum error is exactly what a mismatched or absent NVR looks
# like. Re-seeding onto different hardware means taking both again from
# a machine that actually finished booting on the new board, never
# swapping one half of the pair on its own.
SEED_NVR_DIR = os.path.join(BOX86_ROOT, "vm", "nvr")

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
# FDD_TYPE's own CMOS byte value: a real BIOS reads drive type off CMOS
# offset 0x10 (high nibble A:, low nibble B:), not off 86Box's own cfg
# at all -- 0x4 there is a real, standard AT CMOS 1.44M floppy type, the
# same fact FDD_TYPE states for 86Box's own [Floppy and CD-ROM drives]
# section. The two have to keep agreeing with each other: _sync_disks
# gives both drives FDD_TYPE in 86box.cfg unconditionally now, but a
# real BIOS never looks there for whether a drive exists at all -- only
# _patch_fdd_nvr, below, giving both nibbles this value in NVR, does.
# Changing one without the other is exactly the bug this pair exists to
# rule out: 86box.cfg saying a drive is there while CMOS still says
# otherwise (or the reverse) is what a real BIOS reads as changed
# hardware, at best, or simply does not surface a drive its own BIOS
# never agreed existed, at worst -- confirmed live, 2026-09-08, for B:
# specifically: cfg alone (this file's own earlier fix) was already
# enough for A: (the seed's own CMOS already said 0x4 there), but never
# made B: appear to the guest at all, its own CMOS nibble still 0.
FDD_CMOS = 0x44        # both nibbles: A: and B: both a 1.44M drive
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
        "on_reset": lambda inst: box86_reset(api, inst),
        "is_up": lambda inst: is_up(api, inst),
        "pid": lambda inst: _load_pids(_box_dir(api, inst)).get("86box"),
        "thumbnail": lambda inst, png: box86_thumbnail(api, inst, png),
    })
    api.machine_sanitize("box86", box86_sanitize)
    api.instance_action("box86", "swap-media",
                        lambda inst, data: box86_swap_media(api, inst, data))
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
    has touched Storage for still boots to something.

    _seed_nvr runs regardless of whether hdd1 needed defaulting here:
    an instance from before CFG_TEMPLATE's own [Machine] last changed
    (p2bls/PII-266, in particular) has its own real hdd1 attached
    already, an OS installed on it and everything, and still needs
    that new machine's own NVR seeded the same way a brand new
    instance's does -- _sync_machine (_ensure_cfg) is what actually
    moves such an instance onto that new [Machine] block; without its
    own matching NVR already sitting there by the time that runs, the
    exact same CMOS-checksum-error/hardware-changed prompt patching
    only 86box.cfg and not NVR already caused once this file over
    (FDD_CMOS's own history, above) would recur for the machine itself
    this time instead of just a floppy drive.
    """
    hdd = api.disk_path(inst, "hdd1")
    if not hdd:
        seed_dir = os.path.join(d, "hdd")
        os.makedirs(seed_dir, exist_ok=True)
        hdd = os.path.join(seed_dir, "hdd0.vhd")
        if not os.path.exists(hdd) and os.path.exists(SEED_HDD):
            shutil.copy2(SEED_HDD, hdd)
    _seed_nvr(d)
    return (hdd, api.disk_path(inst, "fdd1"), api.disk_path(inst, "fdd2"),
            api.disk_path(inst, "cd"))


def _seed_nvr(d):
    """SEED_HDD's own match (see its comment): every *.nvr in
    SEED_NVR_DIR, copied into this instance's own nvr/ (86Box's own
    -P-relative convention) the first time it starts -- same semantics
    as SEED_HDD's own copy just above, one file at a time: only if this
    instance does not already have one of its own by that name. Every
    *.nvr there, not one named for the machine: 86Box itself only ever
    reads the one that matches CFG_TEMPLATE's own [Machine] (any other
    sitting there unread is harmless), and naming just one here would
    mean a code change here every time that changes again. *.bin is
    deliberately excluded -- SEED_NVR_DIR may hold other 86Box state
    that is not a machine's CMOS at all.

    Called every start regardless of hdd1 (_disk_paths, above), not
    just the first time an instance has no hdd1 of its own: an
    instance already on some earlier [Machine] never gets a second
    chance at this on its own otherwise, its own nvr/ never growing
    the new machine's own file until _sync_machine (_ensure_cfg)
    actually moves it there -- and by then it is too late, that move
    already having happened with nothing of this new machine's own in
    NVR to go with it. An instance's own already-present NVR for
    whatever [Machine] it is still on now (tx97, for one real one)
    is never touched here -- this only ever adds a file that was not
    there before, one instance may go on holding more than one
    machine's own NVR at once, each simply unread by 86Box except
    whichever one its own current [Machine] actually names.
    """
    if not os.path.isdir(SEED_NVR_DIR):
        return
    dest_dir = os.path.join(d, "nvr")
    os.makedirs(dest_dir, exist_ok=True)
    for name in os.listdir(SEED_NVR_DIR):
        if not name.lower().endswith(".nvr"):
            continue
        src = os.path.join(SEED_NVR_DIR, name)
        dest = os.path.join(dest_dir, name)
        if os.path.isfile(src) and not os.path.exists(dest):
            shutil.copy2(src, dest)


def _patch_fdd_nvr(d):
    """FDD_CMOS's own match (see its own comment, beside FDD_TYPE):
    every *.nvr this instance actually has in its own nvr/ -- freshly
    seeded just above, or already there from before this existed (a
    real production instance's own, in particular) -- patched to say
    both floppy drives are a real 1.44M each, offset 0x10 in the
    standard AT CMOS layout every 86Box machine's own NVR uses
    regardless of which one it is (high nibble A:, low nibble B:).
    86box.cfg's own FDD_TYPE (_sync_disks) already states that fact
    unconditionally for both drives, but a real BIOS never reads
    86Box's own cfg for whether a drive exists at all -- only CMOS, so
    without this A: alone (whose seed CMOS already happened to say
    0x4) was ever visible to a guest; B: never was.

    Every start, not just the first, the same reason _sync_disks' own
    fields already are (a plugin's own instance record can say
    something different than what an existing NVR still has); the size
    guard and the byte-0x10 check together make this a no-op the moment
    it no longer has anything to do, not something to special-case
    around calling on every start regardless.
    """
    nvr_dir = os.path.join(d, "nvr")
    if not os.path.isdir(nvr_dir):
        return
    for name in os.listdir(nvr_dir):
        if not name.lower().endswith(".nvr"):
            continue
        path = os.path.join(nvr_dir, name)
        try:
            with open(path, "rb") as f:
                data = bytearray(f.read())
        except OSError:
            continue
        if len(data) < 0x30 or data[0x10] == FDD_CMOS:
            continue
        data[0x10] = FDD_CMOS
        # AT CMOS's own checksum: a big-endian sum of bytes 0x10-0x2d,
        # stored at 0x2e/0x2f -- the exact same "CMOS checksum error"
        # screen a mismatched configuration change already shows
        # (confirmed live, 2026-09-08: patching 0x10 alone, without
        # this, triggers exactly that, an F1 prompt on every boot).
        checksum = sum(data[0x10:0x2e]) & 0xFFFF
        data[0x2e] = (checksum >> 8) & 0xFF
        data[0x2f] = checksum & 0xFF
        try:
            with open(path, "wb") as f:
                f.write(data)
        except OSError:
            pass


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


def _sync_machine(cfg_path):
    """Move an existing instance's own [Machine] block onto whatever
    CFG_TEMPLATE's own currently says -- the same block a brand new
    instance's own cfg already gets, once, from CFG_TEMPLATE itself
    (_ensure_cfg), but an existing instance's own never revisits again
    on its own once written: 86Box's own on-exit rewrite keeps
    whatever it last negotiated there forever after, this project's
    own board/CPU choice included, past whenever CFG_TEMPLATE's own
    idea of that choice next changes (p2bls/PII-266, replacing tx97,
    is the real example this exists for).

    The whole block, every key CFG_TEMPLATE's own [Machine] names,
    not merely `machine` on its own: cpu_family/cpu_multi/cpu_speed
    left at some earlier board's own Socket 7 values while `machine`
    alone moved to a Slot 1 one is a real, worse mismatch than simply
    never having moved at all -- confirmed live, fc, 2026-09-08.
    Compared and, if different, replaced key by key rather than by
    just re-adding the section wholesale, so an unrelated section
    already elsewhere in the same file keeps its own place in it; an
    instance already on a matching [Machine] -- including a fresh one,
    the moment after CFG_TEMPLATE itself just wrote it -- is left
    untouched rather than rewriting the file to the same bytes it
    already has.

    NVR is a separate, already-solved concern of its own, not this
    function's: _seed_nvr (_disk_paths, called before this from
    _ensure_cfg) already guarantees the new [Machine]'s own NVR exists
    in this instance's own nvr/ by the time this runs, and an existing
    [Machine]'s own NVR is never deleted by anything in this file --
    both still there, whichever one 86Box's own new [Machine] line,
    once this writes it, actually names next boot.
    """
    template_cp = configparser.ConfigParser(interpolation=None)
    template_cp.optionxform = str
    template_cp.read_string(CFG_TEMPLATE)
    wanted = dict(template_cp.items("Machine"))

    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    if os.path.exists(cfg_path):
        cp.read(cfg_path, encoding="utf-8")
    if cp.has_section("Machine") and dict(cp.items("Machine")) == wanted:
        return
    if not cp.has_section("Machine"):
        cp.add_section("Machine")
    else:
        for key in list(cp.options("Machine")):
            cp.remove_option("Machine", key)
    for key, value in wanted.items():
        cp.set("Machine", key, value)
    with open(cfg_path, "w", encoding="utf-8") as f:
        cp.write(f, space_around_delimiters=True)


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
    fdd_real = {}
    for n, path in ((1, fdd1), (2, fdd2)):
        path = _fdd_compatible_path(d, n, path)
        fdd_real[n] = path
        key_type, key_fn = "fdd_%02i_type" % n, "fdd_%02i_fn" % n
        # The drive itself and whatever media is in it are two different
        # things on a real PC, and 86Box keeps them that way: fdd_0N_type
        # "none" does not mean "empty", it means no such drive exists at
        # all. Setting it that way whenever nothing was attached at boot
        # left the drive with nothing for a live-swap's own fdd_0N_fn
        # (written later, box86_swap_media) to insert anything into at
        # all -- a floppy plugged in afterward was silently ignored,
        # "DISK BOOT FAILURE" the only visible result for A:, confirmed
        # live, 2026-09-08: a real bootable floppy, live-swapped into a
        # box86 instance that had started with A: empty, never got read
        # at all. Both drives are now FDD_TYPE unconditionally, real
        # regardless of whether a disk happens to be in either one right
        # now -- the same as a real machine's own floppy drives, whether
        # or not there is a disk in them; fdd_0N_fn (whether this key
        # exists at all) is what actually says whether media is loaded.
        # (Confirmed live, 2026-09-08: 86Box does not update NVR/CMOS
        # from fdd_0N_type at all, so this alone raises no hardware-
        # changed prompt the way changing the machine/CPU does -- but it
        # also means B: existing here is not yet something the guest's
        # own BIOS can see or use: real drive presence, to a real BIOS,
        # is a CMOS fact, and the seed's own CMOS byte for B: still says
        # none regardless of what this section says. A:'s own CMOS byte
        # already says present, which is why this alone was already
        # enough to fix A:. Giving the guest a real B: too needs CMOS-
        # level work of its own -- a separate, later concern, not this
        # function's.)
        cp.set(fc, key_type, FDD_TYPE)
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
    # exactly what was just written, already through
    # _fdd_compatible_path: on_start seeds media.ctl from this, and the
    # two have to agree or 86Box's own watcher primes on a file that
    # describes a machine other than the one it just loaded
    return (fdd_real[1], fdd_real[2], cd or "")


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
    """The instance's own 86box.cfg. Most of it is written once (86Box
    rewrites its cfg on exit with whatever it actually negotiated, CPU
    speed included, the same way towns.py seeds a CMOS file once and
    then leaves the machine's own copy alone) -- the disks, whether a
    MIDI module is fitted, and [Machine] itself are all re-synced on
    every call regardless (_sync_disks, _sync_midi, _sync_machine),
    since Storage/the instance record can change what the first two
    say between one start and the next the same way _sync_machine's
    own CFG_TEMPLATE can change what it says about the third -- an
    existing instance still on some earlier one of those (tx97, for a
    real one) is exactly who _sync_machine exists to move, on its own,
    onto whatever CFG_TEMPLATE currently names instead.
    """
    d = _box_dir(api, inst)
    cfg_path = os.path.join(d, "86box.cfg")
    if not os.path.exists(cfg_path):
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(CFG_TEMPLATE)
    fdd1, fdd2, cd = _sync_disks(api, inst, cfg_path, d)
    # unconditional, every start, for every instance -- not nested under
    # _sync_disks' own hdd1-empty branch (where _seed_nvr lives): an
    # instance from before either of these existed has its own nvr/
    # already, never touched by _seed_nvr's own "only if not already
    # there" copy, and still needs its own CMOS byte patched exactly the
    # same way a freshly seeded one does
    _patch_fdd_nvr(d)
    # after _sync_disks (whose own _disk_paths already guaranteed, via
    # _seed_nvr, that whatever [Machine] this is about to name has its
    # own NVR waiting in nvr/ already) and after _patch_fdd_nvr (so
    # that seeded NVR already has its own FDD_CMOS byte right, same as
    # every other NVR here, before 86Box ever gets a chance to read it
    # against a [Machine] naming it for the first time)
    _sync_machine(cfg_path)
    _sync_midi(api, inst, cfg_path)
    # Seed media.ctl with all three drives as this start actually left
    # them, before 86Box is up to read it. Two reasons, both real: the
    # watcher adopts the first file it sees without acting on it, so it
    # must describe the machine that was just loaded (a file left over
    # from the previous run does not); and _write_media_ctl can only
    # restate a drive it has ever been told about, so without this the
    # very first swap of each drive would still be a one-key file --
    # exactly the shape that loses a change when two land inside one
    # poll interval.
    _write_media_ctl(d, {"fdd1": fdd1, "fdd2": fdd2, "cd": cd})
    return d


# ------------------------------------------------------- live media swap
#
# 86Box itself has no QMP to ask for this the way a QEMU machine's own
# drives can be swapped while it runs (pc98web.py's own /api/instances/
# <name>/media) -- this fork's own small patch gives it an equivalent:
# a QTimer on the GUI thread polls this instance's own media.ctl for its
# seq to have gone up since it last looked, and when it has, calls its
# own existing MediaMenu::floppyMount/cdromMount/eject for whatever key
# the file names. box86.py only ever writes this file; 86Box only ever
# reads it. hdd1 is deliberately not part of this at all: 86Box has no
# live swap of its own for a hard disk either way, so that stays exactly
# what it always was -- stop the machine, change it in Edit.
LIVE_MEDIA_DEVICES = {"fdd1": "fdd", "fdd2": "fdd", "cd": "cdrom"}


def _media_ctl_path(d):
    return os.path.join(d, "media.ctl")


# The order keys are written in. Fixed only so the file reads the way a
# person would expect to find it; 86Box's own watcher parses by name.
_MEDIA_CTL_KEYS = ("seq", "fdd1", "fdd1seq", "fdd2", "fdd2seq",
                   "cd", "cdseq", "reset")


def _read_media_ctl(d):
    """Whatever media.ctl currently says, as a plain dict -- the file is
    its own state store, so a writer that only knows about the one drive
    it is changing can still restate the other two verbatim.
    """
    state = {}
    try:
        with open(_media_ctl_path(d), encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                eq = line.find("=")
                if eq > 0:
                    state[line[:eq].strip()] = line[eq + 1:]
    except OSError:
        pass
    return state


def _write_media_ctl(d, updates, reset=False):
    """Restate every drive this file has ever been told about, with only
    the named ones actually changing. Written to a .tmp and moved into
    place with os.replace, atomic on the same filesystem, so 86Box's own
    polling never reads a half-written file.

    Restating all of them is what makes this safe: the watcher polls at
    its own interval and only ever sees the file as it stands when it
    looks, so two writes landing inside one interval leave it reading
    the second one only. A file that named just the drive it changed
    lost the first change outright when that happened -- confirmed live,
    2026-09-08: fdd1 and fdd2 swapped 0.2s apart, both answered
    "swapped", and only fdd2 reached 86box.cfg at all, leaving the
    instance record claiming a disk the machine did not have.

    Each drive carries its own counter, and 86Box acts on a drive only
    when that counter moves. Restating a drive whose counter has not
    moved is therefore free -- which matters, because remounting one
    that did not change raises a disk-change the guest has no business
    seeing. Bumping a counter while leaving the path alone is still how
    "put that same disc back in" is expressed, which comparing paths
    could never do. path="" means eject; a drive left out of `updates`
    keeps whatever it holds.

    reset=True bumps a counter of its own instead, which the same
    watcher turns into pc_reset_hard() (86Box's own Hard Reset).
    """
    state = _read_media_ctl(d)

    def _n(key):
        try:
            return int(state.get(key, 0))
        except ValueError:
            return 0

    state["seq"] = str(_n("seq") + 1)
    for key, path in updates.items():
        state[key] = path
        state[key + "seq"] = str(_n(key + "seq") + 1)
    if reset:
        state["reset"] = str(_n("reset") + 1)

    ctl = _media_ctl_path(d)
    tmp = ctl + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for key in _MEDIA_CTL_KEYS:
            if key in state:
                f.write("%s=%s\n" % (key, state[key]))
    os.replace(tmp, ctl)


def box86_swap_media(api, inst, data):
    """POST /api/instances/<name>/x/swap-media -- {"device": "fdd1" |
    "fdd2" | "cd", "name": "<Storage name>" | ""}. Live while running
    (the whole point); refused otherwise, the same as pc98/towns' own
    /media endpoint already is -- there is nothing for 86Box's own
    watcher to act on if it is not up to poll media.ctl at all, and a
    swap taking effect only the next start would be silently wrong
    instead of merely refused.

    A floppy's own path is put through _fdd_compatible_path the same
    way _sync_disks already does for the offline path -- fdd.c's own
    loaders[] table (FDD_EXTS) covers plenty of real floppy extensions
    (.flp, .ima, .fdi, and more) beyond just .img, and the one it does
    not (.raw, a PC-98/Towns shelf's own usual name) still works here
    exactly as it already does offline, via a symlink in this
    instance's own box86/ directory rather than a name it refuses
    outright. This was a real 400 here before -- rejecting anything
    that was not already literally named .img, box86.js's own picker
    filtered the same way to match -- when the only actual constraint
    was ever fdd.c's own table, not this one extension out of it.

    The instance record's own device field is updated here too, right
    alongside media.ctl: floppyMount/cdromMount both call 86Box's own
    config_save() internally (confirmed live), so 86box.cfg already
    reflects the swap the moment it happens -- but _sync_disks (this
    file) rewrites [Hard disks]/[Floppy and CD-ROM drives] from the
    instance record on every start regardless, and would otherwise
    silently revert a live swap the next time this instance starts,
    since nothing here would know it had ever happened.
    """
    if not api.is_running(inst):
        return 409, "the machine is not running"
    device = str(data.get("device") or "")
    kind = LIVE_MEDIA_DEVICES.get(device)
    if kind is None:
        return 400, "device must be one of %s" % "/".join(LIVE_MEDIA_DEVICES)
    d = _box_dir(api, inst)
    name = str(data.get("name") or "").strip()
    path = ""
    if name:
        probe = dict(inst)
        probe[device] = name
        path = api.disk_path(probe, device)
        if not os.path.exists(path):
            return 404, "%s: %s does not exist" % (device, path)
    if kind == "fdd":
        # slot: fdd1 -> 1, fdd2 -> 2, the same numbering _sync_disks'
        # own (1, fdd1), (2, fdd2) pairing already uses. Handles path=""
        # (eject) on its own too -- _fdd_compatible_path's first line.
        path = _fdd_compatible_path(d, int(device[-1]), path)
    _write_media_ctl(d, {device: path})
    inst[device] = name
    api.save_instance(inst)
    return {"result": "swapped"}


def box86_reset(api, inst):
    """The Restart button, for a machine with no QMP to ask. Goes over
    the same media.ctl the drives do: 86Box's own watcher turns a moved
    reset counter into pc_reset_hard(), which raises hard_reset_pending
    for the emulator loop to find -- the very lever 86Box's own Hard
    Reset menu action pulls.

    Stopping and starting the process would reach the same end state and
    needs no 86Box patch at all, and is still the wrong answer: on_stop
    cannot bring a guest down cleanly (its ACPI power button is only
    delivered if the guest enabled PWRBTN_EN, which Windows 95 -- no
    ACPI at all -- never does), so every Restart would be a kill, and
    every Restart would come back up into ScanDisk. Confirmed live,
    2026-09-08. It is also ~16s of waiting plus a full boot, against a
    reset the machine does on its own in the time a real reset button
    takes.

    Refused while not running, the same as a swap: there is nothing
    polling media.ctl to act on it, and a reset that quietly took effect
    at the next start instead would be worse than a refusal.
    """
    if not api.is_running(inst):
        return "not running"
    _write_media_ctl(_box_dir(api, inst), {}, reset=True)
    return "reset"


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

        # noVNC delivers keystrokes as X keysyms, not raw scancodes --
        # what symbol a given key produces is entirely Xvfb's own
        # keymap's business, independent of whichever physical keyboard
        # (US or JIS) is actually doing the typing on the viewer's own
        # end. Xvfb's own default keymap is "us", and on a JIS layout
        # `:` sits where US has `;`, sharing its own key with `+`
        # instead -- so a JIS-typing user's `:` press was read by Xvfb,
        # still believing "us", as `+`, `:` itself unreachable at all.
        # Confirmed live, fc, 2026-09-08: setting this Xvfb's own
        # keymap to jp106/jp fixes `: @ \ ; + * ^ | =` (9 symbols) the
        # same way regardless of which physical keyboard is typing,
        # since noVNC's keysym-based input never depended on that to
        # begin with -- only Xvfb's own belief about the layout did.
        # `_` and `~` (JIS's own shifted digit-row keys) are not fixed
        # by this alone -- left open, fc's own follow-up.
        #
        # Must run here, after 86Box's own client has connected -- not
        # right after Xvfb's own socket file first appears, where an
        # earlier version of this same fix originally sat. Confirmed
        # live, 2026-09-08, reproducing the exact race directly: against
        # a bare Xvfb with no client connected at all, setxkbmap reports
        # success (exit 0, no error text) but the change never actually
        # takes, even retried for a full second, even waited out for
        # five more on top of that with no retry at all -- Xvfb's own
        # XKB extension apparently never durably commits a change with
        # nobody connected to observe it. The exact same command,
        # against the exact same display, succeeds immediately once
        # 86Box's own window can be found here -- the one existing,
        # already-relied-upon (by x11vnc's own -id, just below) signal
        # in this function that a real client is actually connected. A
        # host with no setxkbmap at all, or a 86Box that never got this
        # far (win_id still None), must not fail the whole start over a
        # keymap it can then simply leave at Xvfb's own default.
        #
        # setxkbmap's own exit code is still not proof of anything by
        # itself, even here: this is the third time in this same file
        # that exact command has reported success -- exit 0, no error
        # text of its own at all -- while actually changing nothing
        # (the .raw rejection and an old build's own live-swap, both
        # elsewhere in this file's own history, were the first two).
        # So this reads its own change back with -query rather than
        # trusting its own exit code, and retries a handful of times,
        # 0.2s apart, on the chance the very first attempt still lands
        # in whatever this file's own window-wait loop, above, does not
        # quite fully close out either -- logged, not fatal, if every
        # attempt still leaves it unconfirmed: a keymap this console
        # then simply starts at Xvfb's own default over is still a far
        # smaller failure than not starting at all over one.
        disp = ":%d" % display_num
        try:
            for _attempt in range(5):
                subprocess.run(
                    ["setxkbmap", "-display", disp,
                     "-model", "jp106", "-layout", "jp"],
                    capture_output=True, timeout=5, check=False)
                q = subprocess.run(
                    ["setxkbmap", "-display", disp, "-query"],
                    capture_output=True, timeout=5, check=False)
                if b"layout:" in q.stdout and b"jp" in q.stdout:
                    break
                time.sleep(0.2)
            else:
                log.write(b"[box86] setxkbmap: jp106/jp keymap did not"
                          b" take after retries\n")
        except OSError:
            pass

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

    # is_up alone (86Box's own pid alive) says nothing about whether
    # websockify_video is actually ready to accept a browser's own
    # connection yet -- it was only ever spawned a moment ago, above,
    # never waited on the way x11vnc's own window (win_id) already is.
    # pc98web.py/towns.py's own instances never needed this: QMP itself
    # is that same wait, inherent in is_up there. Only "listening"
    # (which spawn() alone already guarantees nothing at all about, and
    # even accept()-ing a raw TCP connect still isn't proof it can
    # actually service one) is what _port_open, below, confirms by
    # opening and immediately dropping a real connection of its own --
    # a plugin's own "started" is claimed only once a browser's own
    # first connection attempt, arriving right after, would not have
    # been the one still finding it not there yet.
    for _ in range(40):
        if box_proc.poll() is not None:
            log.close()
            _kill_pids(pids)
            _save_pids(d, {})
            return "failed: 86Box exited during startup, see box86.log"
        if is_up(api, inst) and _port_open(ws):
            log.close()
            return "started"
        time.sleep(0.25)
    log.close()
    return "started (slow to come up)"


_stopping = set()


def on_stop(api, inst):
    """Ask 86Box's own guest to shut down first (a real ACPI power
    button press, over SIGUSR1 -- a patch to our own build, since 86Box
    offers no QMP-like control socket of its own to ask for one any
    other way), and only fall back to _kill_pids's SIGTERM/SIGKILL
    escalation for whatever is left once that either finishes or times
    out. A machine simply killed mid-run never gets to tell its own OS
    it is shutting down, and a guest that actually understands ACPI
    boots back into its own crash recovery next time as a direct
    result of skipping this -- confirmed live, 2026-09-08.

    That guest has to actually be an ACPI one for any of this to do
    anything at all, though: Windows 95 itself is APM, not ACPI, and
    this SIGUSR1 patch only ever raises the latter -- confirmed live
    by fc, 2026-09-08, that a Win95 guest never reacts to it in any
    way. is_up on a Win95 guest in particular staying true for most or
    all of the 15s wait below is not, on its own, proof of a real
    guest-side shutdown genuinely in progress the way it would be for
    a guest that does speak ACPI -- _kill_pids' own SIGTERM/SIGKILL
    escalation, past that wait, is what actually always ends up doing
    the work there regardless. Real APM support for a Win95 guest,
    should that ever matter enough on its own to be worth adding, is
    not this.

    Runs in a background thread: the wait above is up to 15s on its
    own, plus _kill_pids' own up-to-5s escalation on top -- up to 20s
    the pc98web server, single-threaded, otherwise spent unable to
    answer any other instance's request at all, this instance's own
    stop button included. is_up keeps reading this instance as running
    for as long as that thread actually takes, exactly as it already
    does for any other action still genuinely in flight -- a list view
    only shows it stopped once a refresh lands after the thread's own
    _save_pids(d, {}) actually runs, not the moment this returns.

    _stopping (module-level, keyed by instance index) exists only to
    stop a second stop request arriving before the first's own thread
    has finished from starting a second, redundant shutdown sequence
    of the same instance concurrently with the first -- a real risk
    once this is no longer one request blocking the whole server from
    accepting a second one. A CPython set's own add/discard/`in` are
    each one bytecode-level op already atomic under the GIL, same as
    the rest of this module's plain dict/set state -- no lock beyond
    that is needed for a plain membership guard like this one.
    """
    index = inst["index"]
    if index in _stopping:
        return "already stopping"
    d = _box_dir(api, inst)

    def _shutdown():
        try:
            _stop_now(d, inst)
        finally:
            _stopping.discard(index)

    _stopping.add(index)
    threading.Thread(target=_shutdown, daemon=True).start()
    return "shutting down"


def _stop_now(d, inst):
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
