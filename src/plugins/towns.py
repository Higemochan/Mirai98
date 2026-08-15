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
seeded from the ROM set's towns.cmos - the .hdd variant when the machine
has a SCSI disk, so the drives are registered from the first boot.

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
    api.machine_sanitize("towns", towns_sanitize)


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
    """This machine's CMOS file, created from the right seed if missing."""
    os = api.os
    path = os.path.join(api.inst_dir(inst), "towns.cmos")
    if not os.path.exists(path):
        has_hd = any(inst.get(k) for k in ("scsi1", "scsi2", "scsi3", "scsi4"))
        seeds = [os.path.join(towns_roms, "towns.cmos.hdd")] if has_hd else []
        seeds.append(os.path.join(towns_roms, "towns.cmos"))
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
