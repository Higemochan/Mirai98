# DOS/V (standard PC, KVM) machine plugin for the Mirai98 web manager.
#
# Registers the "pcat" machine: a plain PC/AT (QEMU i440FX + PIIX) run under
# KVM for a DOS/V Windows 9x guest, the same shape as the PC-98 and FM TOWNS
# machines -- a machine_argv builder, and the core does everything else
# (start/stop, the VNC console, QMP media swap, and the audio the VNC server
# carries).  It shares the "dosv" Storage shelf with box86; only the hard
# disk differs (a plain raw image here, not box86's 86Box VHD).
#
# Nothing in box86.py or pc98web.py is touched: a QEMU machine needs no
# engine of its own, so add_machine + machine_argv is the whole of it, plus
# the read-only hardware card (machine_shown) and one blank-disk format.

# The BIOS this boots is the QEMU fork's own pc-bios (bios-256k.bin,
# vgabios-*.bin); the fork does not put a standard-PC BIOS on its default
# search path, so the builder names it.  Overridable from mirai98.json
# ("pc_bios") without a code change.
PCAT_PC_BIOS = "/opt/mirai98-src/qemu-pc98towns/pc-bios"

PCAT_MACHINE_LABEL = "PC/AT (QEMU i440FX + PIIX)"
# SoftGPU's Win98 driver is an SSE3 build, so the vCPU advertises SSE3; the
# host has it under KVM and it is emulated under TCG.
PCAT_CPU_LABEL = "Pentium III (pentium3, +SSE/SSE2/SSE3)"
PCAT_SOUND_LABEL = "Sound Blaster 16 (ISA)"
PCAT_VGA_LABELS = {"std": "Bochs VBE (std)", "cirrus": "Cirrus CLGD5446"}

# boot values are "hd"/"fd"/"cd" -- a subset of FM TOWNS' own "boot" field,
# so its validator accepts them and this machine reuses that field rather
# than registering a colliding one.  QEMU's own -boot letters: a=floppy,
# c=first hard disk, d=CD-ROM.
_BOOT_LETTER = {"hd": "c", "fd": "a", "cd": "d"}


def register(api):
    api.add_machine("pcat", platform="dosv")
    api.machine_argv("pcat", lambda inst: pcat_argv(api, inst))
    api.machine_shown("pcat", lambda inst: pcat_hardware(api, inst))
    # "vga" is this machine's own field, registered so the record keeps it.
    # "boot" is not registered here: it is already a plugin field (FM TOWNS'),
    # and the values used here are a subset it accepts, so registering a
    # second validator would only risk clobbering the other one by load
    # order.  "net" is a core field (empty/nat/bridge, validated in the core
    # sanitize); a validator of our own would run against every machine, not
    # just this one, so it is left alone -- this machine uses "" (isolated)
    # or "nat".
    api.add_field("vga", lambda v: None if v in ("", "std", "cirrus")
                  else "unknown video card")
    # this machine always has SB16 sound and the QEMU BIOS; the stock
    # Sound/BIOS choices the wizard cannot replace are pinned here rather
    # than stored and ignored, and the PC-98/host-only fields are cleared
    api.machine_sanitize("pcat", pcat_sanitize)
    # a plain raw hard disk of this machine's own; box86's VHD is for
    # 86Box, and the one dosv shelf holds both, told apart by format
    api.disk_builder("dosv", "hdd", "pcat-raw", pcat_new_hard_disk)


def pcat_sanitize(record):
    """Pin the fixed choices and drop the settings this machine has no use
    for.  Its sound is always SB16 and its firmware the QEMU BIOS, so the
    stock "Sound"/"BIOS" values are forced (kept valid for the core's own
    checks); the shared folder and the RS-232C/parallel/GP-IB device paths
    are PC-98/host features it does not wire.  IDE disks, the CD, floppies,
    net (""/nat) and its own vga/boot are left as they are."""
    record["sound"] = "none"
    record["bios"] = "real"
    for key in ("mount", "serial", "parallel", "gpib"):
        record[key] = ""
    return None


def pcat_argv(api, inst):
    cfg = api.CONFIG
    # ports_of hands back a fourth port (box86's own audio websocket);
    # a QEMU machine carries its audio on the VNC stream, so it is dropped
    vnc, ws, qmp_port, _audio_ws = api.ports_of(inst)
    display = vnc - 5900
    # KVM when asked, translation when not -- and, spelled kvm:tcg, KVM
    # that falls back to translation instead of refusing to start when the
    # host cannot give it
    accel = "kvm:tcg" if inst.get("accel", "kvm") == "kvm" else "tcg"
    host = "127.0.0.1" if api.LOOPBACK else "0.0.0.0"
    pcbios = cfg.get("pc_bios") or PCAT_PC_BIOS
    vga = inst.get("vga") or "std"
    boot = _BOOT_LETTER.get(inst.get("boot") or "hd", "c")
    argv = [
        cfg["qemu"],
        # the standard-PC BIOS and vgabios first, then the fork's own data
        "-L", api.win_short(pcbios),
        "-L", api.win_short(cfg["datadir"]),
        # ACPI off for now: this Win98 SE goes through APM, and the ACPI
        # path is still being brought up on the isolated bench
        "-M", "pc-i440fx-9.2,acpi=off,accel=%s" % accel,
        "-cpu", "pentium3,+sse2,+sse3,+ssse3",
        "-m", inst.get("memory") or "256M",
        # the calendar clock follows the host's local time, so the guest
        # shows the wall clock rather than UTC
        "-rtc", "base=localtime",
        # No -k: this guest was installed with a 101 (US) keyboard, so
        # QEMU's default en-us VNC keymap matches it.  PC-98's own -k ja is
        # for its JIS keyboard and would put the wrong characters here.
        "-vga", vga,
        "-display", "none",
        # SB16 plays into a null backend; the VNC server captures that mix
        # (audiodev=snd on -vnc) and streams it to the browser, the same
        # path PC-98 and Towns sound takes
        "-audiodev", "none,id=snd",
        "-device", "sb16,audiodev=snd",
        "-vnc", "%s:%d,websocket=%d,audiodev=snd" % (host, display, ws),
        "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port,
        "-boot", "order=%s" % boot,
    ]
    if inst.get("snapshot"):
        argv.append("-snapshot")
    # IDE: primary master/slave are the hard disks (index 0/1), the CD is
    # the secondary master (index 2).  drive_backing reads the file's own
    # format, so a .qcow2 comes up as qcow2 and a .img/.raw as raw.
    for index, key in enumerate([k for k in ("hdd1", "hdd2") if inst.get(k)]):
        argv += ["-drive", "if=ide,index=%d,%s"
                 % (index, api.drive_backing(api.disk_path(inst, key)))]
    # the CD drive exists even with an empty tray, so a disc can be put in
    # from the Media row (over QMP) without a restart
    cd = "if=ide,index=2,media=cdrom,readonly=on"
    if inst.get("cd"):
        cd += "," + api.drive_backing(api.disk_path(inst, "cd"))
    argv += ["-drive", cd]
    # both floppy drives always exist, for the same reason as the CD
    for index, key in enumerate(("fdd1", "fdd2")):
        drive = "if=floppy,index=%d" % index
        if inst.get(key):
            drive += "," + api.drive_backing(api.disk_path(inst, key))
        argv += ["-drive", drive]
    if inst.get("net") == "nat":
        # QEMU's own NAT behind an RTL8139, which Win98 has a driver for;
        # nothing is asked of the host, so this needs no bridge
        argv += ["-netdev", "user,id=lan",
                 "-device", "rtl8139,netdev=lan"]
    if inst.get("extra"):
        argv += inst["extra"].split()
    return argv


# the effective accelerator, cached per running instance.  The listing asks
# for the hardware card several times a minute, and query-kvm over QMP (a 3 s
# timeout) is too much to pay each time; it does not change within a run, so
# it is asked once and kept, and the entry is dropped when the instance is
# not running so the next start asks again.
_ACCEL_CACHE = {}


def pcat_hardware(api, inst):
    """The read-only hardware card's facts, wrapped as the record's own
    `hardware` field.  The wrap matters: shown() merges the plugin's answer
    into the record, so a bare {"machine": ...} would overwrite the record's
    real machine name and break every lookup by it (box86.py wraps it the
    same way).

    A QEMU machine keeps no config file -- the record and the argv are the
    configuration -- so board/CPU/video/memory come from the instance's own
    fields and the fixed machine.  The one thing worth asking the running
    machine is which accelerator it actually got: `accel=kvm:tcg` falls back
    to translation when KVM is unavailable, and the card should say what is
    really running rather than what was asked for.
    """
    vga = inst.get("vga") or "std"
    hw = {
        "machine": PCAT_MACHINE_LABEL,
        "cpu": PCAT_CPU_LABEL,
        "video": PCAT_VGA_LABELS.get(vga, vga),
        "memory": inst.get("memory") or "256M",
        "sound": PCAT_SOUND_LABEL,
    }
    requested = "KVM" if inst.get("accel", "kvm") == "kvm" else "TCG"
    # keyed on the requested accelerator too, so a stop -> edit (kvm<->tcg)
    # -> start with no listing in between does not read a stale answer
    key = (inst.get("name"), inst.get("accel", "kvm"))
    if api.is_running(inst):
        accel = _ACCEL_CACHE.get(key)
        if accel is None:
            reply = api.qmp(inst, "query-kvm")
            if reply and isinstance(reply.get("return"), dict):
                enabled = reply["return"].get("enabled")
                if enabled is True:
                    accel = "KVM (host CPU)"
                elif enabled is False:
                    # asked for KVM, running translated: the fallback fired
                    accel = "TCG (translated%s)" % (
                        ", KVM unavailable" if requested == "KVM" else "")
            if accel is not None:
                # only a definite query-kvm answer is cached; a transient
                # failure shows the request and is asked again next time
                _ACCEL_CACHE[key] = accel
            else:
                accel = requested + " (running)"
    else:
        _ACCEL_CACHE.pop(key, None)
        accel = requested
    hw["accel"] = accel
    return {"hardware": hw}


def pcat_new_hard_disk(dest, data):
    """A blank hard disk for the PC/AT machine: a sparse raw image, sized in
    megabytes from the create form the same way every other shelf builder
    reads it (the shelf's own default of 40 is small -- a Win98 install
    wants a few hundred).  Win98 FDISK/FORMAT partitions and formats it, as
    on a real machine.  Kept raw so no qemu-img is needed; drive_backing
    reads any non-qcow2 file as raw.
    """
    megabytes = int(data.get("size") or 40)
    with open(dest, "wb") as f:
        f.truncate(megabytes << 20)
