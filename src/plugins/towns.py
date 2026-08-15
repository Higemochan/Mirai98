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


def register(api):
    api.add_machine("towns")
    api.machine_argv("towns", lambda inst: towns_argv(api, inst))
    api.add_field("boot", lambda v: None if v in BOOT_KEYS
                  else "boot must be empty, cd, fd or hd")
    api.add_field("cmos", lambda v: None if v in CMOS_SEEDS
                  else "cmos must be empty or real")
    api.machine_sanitize("towns", towns_sanitize)
    api.instance_action("towns", "reset-cmos",
                        lambda inst, data: towns_reset_cmos(api, inst))


# where a machine's CMOS starts from when it has none yet: "" = the ROM
# set's towns.cmos (nothing registered; the Towns OS SETUP registers hard
# disks, as on a new machine), "real" = towns.cmos.hdd beside it, a copy of
# a real machine's CMOS with its own disks registered - only for images
# taken from that machine
CMOS_SEEDS = {"": "towns.cmos", "real": "towns.cmos.hdd"}


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
    (they would be stored but never used), memory must suit the machine
    (2 MiB or more, whole MiB) and it always runs under TCG."""
    for key in ("hdd1", "hdd2", "mount", "net", "serial", "parallel", "gpib"):
        record[key] = ""
    record["bios"] = "real"
    record["sound"] = "none"
    record["accel"] = "tcg"
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
        accel, api.win_short(towns_cmos(api, inst, towns_roms)))
    bootkey = BOOT_KEYS.get(inst.get("boot") or "")
    if bootkey:
        machine += ",bootkey=%s" % bootkey
    argv = [cfg["qemu"],
            "-M", machine,
            "-m", inst.get("memory") or "16M",
            "-L", api.win_short(towns_roms),
            "-L", api.win_short(cfg["datadir"]),
            "-display", "none",
            "-audiodev", "none,id=snd",
            "-vnc", vncarg,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    if inst.get("snapshot"):
        argv.append("-snapshot")
    # the built-in CD-ROM drive always exists (empty tray without an image)
    # so a disc can be put in from the Media row while the machine runs
    cd = "if=ide,index=2,media=cdrom"
    if inst.get("cd"):
        cd += ",format=raw,file=%s" % api.win_short(api.disk_path(inst, "cd"))
    argv += ["-drive", cd]
    # both internal floppy drives always exist (so a disk can be put in
    # from the Media row while the machine runs); an image is a raw dump
    # or a D77/D88 file, told apart by the emulated controller
    for index, key in enumerate(("fdd1", "fdd2")):
        drive = "if=floppy,index=%d" % index
        if inst.get(key):
            drive += ",format=raw,file=%s" % api.win_short(
                api.disk_path(inst, key))
        argv += ["-drive", drive]
    # SCSI hard disks: SCSI ID = unit (Towns OS drive letters follow the
    # CMOS registration, D: onwards on the stock towns.cmos.hdd)
    for unit, key in enumerate(("scsi1", "scsi2", "scsi3", "scsi4")):
        if inst.get(key):
            argv += ["-drive", "if=scsi,bus=0,unit=%d,format=raw,file=%s" % (
                unit, api.win_short(api.disk_path(inst, key)))]
    if inst.get("extra"):
        argv += inst["extra"].split()
    return argv
