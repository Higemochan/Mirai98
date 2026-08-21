"""FM TOWNS machine plugin for the Mirai98 web manager.

Adds a "towns" machine type whose QEMU command line boots the FM TOWNS
BIOS from its real ROM set with the disc as the built-in CD-ROM, up to
two floppy images in the internal 3-mode drives and up to four SCSI hard
disk images (SCSI IDs 0-3).  The TOWNS machine carries its own
timer/CRTC/CD-ROM/FDC/SCSI/sound, so none of the PC-98 ISA add-on devices
are attached.  Create, start, snapshot and console are the unchanged core
flow; only the command line differs.

Per machine it also keeps a CMOS file (the battery-backed settings the
Towns OS SETUP writes: drive registration, boot options) beside vm.xml,
seeded from the ROM set's towns.cmos (or, on request, towns.cmos.hdd, a
real machine's copy for images taken from it) and reset on demand.

The matching front-end lives in web/plugins/towns.js (machine option,
Towns defaults, the list badge, its hardware form and the relative-pointer
capture).
"""

import shutil

# the boot device the SYSROM is told to use, as the key held at power-on
# ("" = the ROM's own order: floppy, CD-ROM, then hard disk)
BOOT_KEYS = {"": None, "cd": "CD", "fd": "F0", "hd": "H0"}

# On the real machine you hold a key combination down while it starts
# and the system ROM reads it to pick where to boot from.  A reset is
# where the ROM looks, so the same thing is offered on Restart: the
# machine keeps the choice in a property, and it applies to the reset
# that follows and to that one only.
RESET_KEYS = ("", "CD", "F0", "F1", "H0", "H1", "H2", "H3", "H4",
              "ICM", "DEBUG", "FAST", "SLOW")

# the MIDI card is off unless asked for: a few titles will not run with
# one fitted.  "synth" renders what the card is sent with a SoundFont
# and mixes it into the machine's sound, which is what reaches the
# browser over the console connection.  The field itself is the core's
# (pc98web.MIDI_MODES); what a machine fits for it is its own business.
MIDI_MODES = {"": None, "synth": "on"}

# How fast the emulated CPU is allowed to run.  Left empty the
# translator runs flat out, which on this hardware lands somewhere near
# 50 MIPS and keeps a host core busy the whole time the machine is on -
# an FM TOWNS guest never idles, it polls.  A shift pins the CPU to
# 2**-shift instructions per nanosecond and lets the host sleep for the
# rest of each slice, so the wall clock (and everything the guest times
# against it) stays right while the host does proportionally less work.
# Measured here: shift 5 ~31 MIPS at ~72% of a core, 6 ~16 MIPS at ~41%,
# 7 ~8 MIPS at ~24%.  Below shift 5 the host cannot keep the pace up and
# the machine falls into slow motion, which is audible.
CPU_SPEEDS = {"": None, "high": 5, "mid": 6, "low": 7}

# Which game port holds the pad.  The pad takes port A and the mouse port
# B, as on a real machine: titles read port A for a pad, and one that finds
# a mouse there steers itself as the mouse moves.  The Towns OS finds its
# mouse in port B just as well -- measured, once a bug that fed host motion
# to port A whatever was in it had been fixed.  So there is no trade to
# make in the usual case, and the other arrangements are here for the
# titles that want them.
PAD_PORTS = {"": None, "b": "b", "a": "a", "both": "both", "off": "off"}


def register(api):
    api.add_machine("towns")
    api.machine_argv("towns", lambda inst: towns_argv(api, inst))
    api.add_field("boot", lambda v: None if v in BOOT_KEYS
                  else "boot must be empty, cd, fd or hd")
    api.add_field("cmos", lambda v: None if v in CMOS_SEEDS
                  else "cmos must be empty or real")
    api.add_field("cpu", lambda v: None if v in CPU_SPEEDS
                  else "cpu must be empty, high, mid or low")
    api.add_field("pad", lambda v: None if v in PAD_PORTS
                  else "pad must be empty, a, b, both or off")
    api.machine_sanitize("towns", towns_sanitize)
    api.instance_action("towns", "reset-cmos",
                        lambda inst, data: towns_reset_cmos(api, inst))
    api.instance_action("towns", "boot-reset",
                        lambda inst, data: towns_boot_reset(api, inst, data))
    api.instance_action("towns", "pad-port",
                        lambda inst, data: towns_pad_port(api, inst, data))


# where a machine's CMOS starts from when it has none yet: "" = the ROM
# set's towns.cmos (nothing registered; the Towns OS SETUP registers hard
# disks, as on a new machine), "real" = towns.cmos.hdd beside it, a copy of
# a real machine's CMOS with its own disks registered - only for images
# taken from that machine
CMOS_SEEDS = {"": "towns.cmos", "real": "towns.cmos.hdd"}


def _qmp_ok(reply):
    """QMP answers with either a return or an error; no answer at all is a
    machine that could not be reached."""
    return isinstance(reply, dict) and "return" in reply


def towns_boot_reset(api, inst, data):
    """Restart with a boot-key combination held down.

    The machine holds the keys for exactly one reset and forgets them
    itself (its bootkey-once property), so nothing here has to time a
    release, and a machine left holding a key is not a state we can end
    up in.
    """
    if not isinstance(data, dict):
        return 400, "expected a JSON object"
    key = data.get("key")
    key = key.upper() if isinstance(key, str) else "" if key is None else None
    if key not in RESET_KEYS:
        return 400, "boot key must be one of %s" % ", ".join(
            k or "(none)" for k in RESET_KEYS)
    if not api.is_running(inst):
        return 409, "start it first"

    reply = api.qmp(inst, "qom-set", {"path": "/machine",
                                      "property": "bootkey-once",
                                      "value": key})
    if reply is None:
        return 502, "the machine did not answer"
    if not _qmp_ok(reply):
        detail = (reply.get("error") or {}).get("desc", "refused")
        return 502, ("the machine would not take the boot key (%s); if it "
                     "was started before the emulator was updated, stop and "
                     "start it again" % detail)
    if not _qmp_ok(api.qmp(inst, "system_reset")):
        return 502, "the machine took the boot key but would not restart"
    return {"result": "restarting", "key": key}


def towns_pad_port(api, inst, data):
    """Move the pad between the game ports, while the machine runs.

    A pad and the mouse cannot share port A, and which of them wants it
    depends on the disc in the drive, so this is a thing to change in the
    middle of playing rather than only before starting.  The machine
    takes it live; the record keeps it, so the next start begins the same
    way round.
    """
    if not isinstance(data, dict):
        return 400, "expected a JSON object"
    port = data.get("port")
    port = "" if port is None else str(port)
    if port not in PAD_PORTS:
        return 400, "the pad goes in port a or b, in both, or nowhere (off)"
    inst["pad"] = port
    api.save_instance(inst)
    if not api.is_running(inst):
        return {"result": "kept", "port": port}
    reply = api.qmp(inst, "qom-set", {"path": "/machine/towns-gameport",
                                      "property": "pad",
                                      "value": PAD_PORTS[port] or "b"})
    if reply is None:
        return 502, "the machine did not answer"
    if "error" in reply:
        # a machine started before the emulator learned the trick has no
        # such property; say which of the two it is
        return 409, ("%s -- if it was started before the emulator was "
                     "updated, stop and start it again"
                     % reply["error"].get("desc", "refused"))
    return {"result": "moved", "port": port}


def towns_reset_cmos(api, inst):
    """Drop the machine's CMOS so the next start seeds it afresh."""
    os = api.os
    if api.is_running(inst):
        return (409, "shut it down first")
    path = os.path.join(api.inst_dir(inst), "towns.cmos")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as err:
        return (500, "could not remove the CMOS: %s" % err)
    return {"result": "cmos reset", "seed": CMOS_SEEDS.get(
        inst.get("cmos") or "", "towns.cmos")}
    for fmt in TOWNS_FLOPPIES:
        api.disk_builder("fdd", fmt, towns_new_floppy)
    api.disk_builder("hdd", "towns", towns_new_hard_disk)


# FM TOWNS floppy layouts (MS-DOS FAT12 as the Towns FORMAT command lays
# them out): bytes/sector, sectors/cluster, root entries, total sectors,
# media byte, sectors/FAT, sectors/track, heads
TOWNS_FLOPPIES = {
    "towns-1.23": (1024, 1, 192, 1232, 0xfe, 2, 8, 2),   # 2HD 1.23 MB
    "towns-1.44": (512, 1, 224, 2880, 0xf0, 9, 18, 2),   # 2HD 1.44 MB
    "towns-720":  (512, 2, 112, 1440, 0xf9, 3, 9, 2),    # 2DD 720 KB
    "towns-640":  (512, 2, 112, 1280, 0xfb, 2, 8, 2),    # 2DD 640 KB
}


def towns_new_floppy(dest, data):
    """An empty FAT12 floppy image in one of the FM TOWNS layouts."""
    import struct
    fmt = str(data.get("format") or "")
    bps, spc, root, total, media, spf, spt, heads = TOWNS_FLOPPIES[fmt]
    label = (str(data.get("label") or "NO NAME").upper()[:11]).ljust(11)
    boot = bytearray(bps)
    boot[0:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"FMTOWNS "
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


def towns_new_hard_disk(dest, data):
    """A blank SCSI hard disk image for an FM TOWNS: all zeros, to be
    initialised (partitions + format) by the Towns OS SETUP / HD
    utility, the way a new drive was on the real machine."""
    megabytes = int(data.get("size") or 40)
    with open(dest, "wb") as f:
        f.truncate(megabytes << 20)


def towns_sanitize(record):
    """What a TOWNS record may hold: the PC-98-only settings are dropped
    (they would be stored but never used) and memory must suit the machine
    (2 MiB or more, whole MiB).  Either accelerator is allowed, though KVM
    pays for every device access with a trip out of the guest, so it is
    not automatically the faster of the two here."""
    for key in ("hdd1", "hdd2", "mount", "net", "serial", "parallel", "gpib"):
        record[key] = ""
    record["bios"] = "real"
    record["sound"] = "none"
    mem = record["memory"].upper()
    units = {"K": 1, "M": 1024, "G": 1024 * 1024}
    if not mem or mem[-1] not in units or not mem[:-1].isdigit():
        return "memory must be like 4M or 16M"
    kib = int(mem[:-1]) * units[mem[-1]]
    if kib < 2048 or kib % 1024:
        return "FM TOWNS memory must be 2M or more, in whole megabytes"
    return None


def towns_cmos(api, inst, towns_roms):
    """This machine's CMOS file, created from the chosen seed if missing."""
    os = api.os
    path = os.path.join(api.inst_dir(inst), "towns.cmos")
    if not os.path.exists(path):
        seeds = [os.path.join(towns_roms,
                              CMOS_SEEDS.get(inst.get("cmos") or "",
                                             "towns.cmos")),
                 os.path.join(towns_roms, "towns.cmos")]
        for seed in seeds:
            if os.path.exists(seed):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copyfile(seed, path)
                break
    return path


def towns_argv(api, inst):
    os = api.os
    cfg = api.CONFIG
    vnc, ws, qmp_port = api.ports_of(inst)
    display = vnc - 5900
    accel = "kvm:tcg" if inst.get("accel", "tcg") == "kvm" else "tcg"
    towns_roms = cfg.get("towns_roms") or os.path.join(cfg["roms"], "towns")
    host = "127.0.0.1" if api.LOOPBACK else "0.0.0.0"
    # the VNC server captures the guest's audio mix (CD-DA, FM, PCM) off the
    # 'snd' audiodev and streams it to the browser over the same WebSocket
    vncarg = "%s:%d,websocket=%d,audiodev=snd" % (host, display, ws)
    # audiodev reaches the machine so the on-board YM2612/RF5C68 and the
    # CD-DA voice all open on the backend the VNC server streams; the CMOS
    # file keeps the guest's SETUP; a boot key overrides the ROM's order
    machine = "towns,accel=%s,audiodev=snd,cmos=%s" % (
        accel, api.qemu_file(towns_cmos(api, inst, towns_roms)))
    pad = PAD_PORTS.get(inst.get("pad") or "")
    if pad:
        machine += ",pad=%s" % pad
    bootkey = BOOT_KEYS.get(inst.get("boot") or "")
    if bootkey:
        machine += ",bootkey=%s" % bootkey
    # the MIDI card's synthesiser opens on the same audiodev as the rest,
    # so its music arrives in step with the FM and PCM channels
    if MIDI_MODES.get(inst.get("midi") or ""):
        machine += ",midi=on"
        if cfg.get("soundfont"):
            machine += ",soundfont=%s" % api.qemu_file(cfg["soundfont"])
    argv = [cfg["qemu"],
            "-M", machine,
            "-m", inst.get("memory") or "16M",
            "-L", api.win_short(towns_roms),
            "-L", api.win_short(cfg["datadir"]),
            "-display", "none",
            "-audiodev", "none,id=snd",
            # the calendar clock follows the host's local time, so Towns OS
            # shows the wall clock rather than UTC
            "-rtc", "base=localtime",
            "-vnc", vncarg,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    # Pinning the instruction rate needs the translator, so a machine
    # asking for KVM keeps host speed and its full core.
    shift = CPU_SPEEDS.get(inst.get("cpu") or "")
    if shift and accel == "tcg":
        argv += ["-icount", "shift=%d,sleep=on,align=on" % shift]
    if inst.get("snapshot"):
        argv.append("-snapshot")
    # the built-in CD-ROM drive always exists (empty tray without an image)
    # so a disc can be put in from the Media row while the machine runs
    cd = "if=ide,index=2,media=cdrom"
    if inst.get("cd"):
        cd += "," + api.drive_backing(api.disk_path(inst, "cd"))
    argv += ["-drive", cd]
    # both internal floppy drives always exist (so a disk can be put in
    # from the Media row while the machine runs); an image is a raw dump
    # or a D77/D88 file, told apart by the emulated controller
    for index, key in enumerate(("fdd1", "fdd2")):
        drive = "if=floppy,index=%d" % index
        if inst.get(key):
            drive += "," + api.drive_backing(
                api.disk_path(inst, key))
        argv += ["-drive", drive]
    # SCSI hard disks: SCSI ID = unit (Towns OS drive letters follow the
    # CMOS registration, D: onwards on the stock towns.cmos.hdd)
    for unit, key in enumerate(("scsi1", "scsi2", "scsi3", "scsi4")):
        if inst.get(key):
            argv += ["-drive", "if=scsi,bus=0,unit=%d,%s" % (
                unit, api.drive_backing(api.disk_path(inst, key)))]
    if inst.get("extra"):
        argv += inst["extra"].split()
    return argv
