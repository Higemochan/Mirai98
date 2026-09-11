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

_BOOT_LETTER = {"hdd": "c", "fdd": "a", "cd": "d"}


def register(api):
    api.add_machine("pcat", platform="dosv")
    api.machine_argv("pcat", lambda inst: pcat_argv(api, inst))
    api.machine_shown("pcat", lambda inst: pcat_hardware(api, inst))
    # the machine-specific choices, so the record keeps them; unknown
    # values are turned back rather than silently kept
    api.add_field("vga", lambda v: None if v in ("", "std", "cirrus")
                  else "unknown video card")
    api.add_field("boot", lambda v: None if v in ("", "hdd", "fdd", "cd")
                  else "unknown boot device")
    api.add_field("net", lambda v: None if v in ("", "off", "nat")
                  else "unknown network mode")
    # a plain raw hard disk of this machine's own; box86's VHD is for
    # 86Box, and the one dosv shelf holds both, told apart by format
    api.disk_builder("dosv", "hdd", "pcat-raw", pcat_new_hard_disk)


def pcat_argv(api, inst):
    os = api.os
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
    boot = _BOOT_LETTER.get(inst.get("boot") or "hdd", "c")
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


def pcat_hardware(api, inst):
    """The read-only hardware card's facts, put on the record as its own
    `hardware` field.  A QEMU machine keeps no config file -- the record
    and the argv are the configuration -- so the board, CPU, video and
    memory come straight from the instance's fields and the fixed machine.

    The one thing worth asking the running machine is which accelerator it
    actually got: `accel=kvm:tcg` falls back to translation when KVM is
    unavailable, and the card should say what is really running rather than
    what was asked for.
    """
    vga = inst.get("vga") or "std"
    out = {
        "machine": PCAT_MACHINE_LABEL,
        "cpu": PCAT_CPU_LABEL,
        "video": PCAT_VGA_LABELS.get(vga, vga),
        "memory": inst.get("memory") or "256M",
        "sound": PCAT_SOUND_LABEL,
    }
    requested = "KVM" if inst.get("accel", "kvm") == "kvm" else "TCG"
    accel = None
    if api.is_running(inst):
        reply = api.qmp(inst, "query-kvm")
        if reply and isinstance(reply.get("return"), dict):
            enabled = reply["return"].get("enabled")
            if enabled is True:
                accel = "KVM (host CPU)"
            elif enabled is False:
                # asked for KVM, running translated -- the fallback fired
                accel = "TCG (translated%s)" % (
                    ", KVM unavailable" if requested == "KVM" else "")
        if accel is None:
            accel = requested + " (running)"
    else:
        accel = requested
    out["accel"] = accel
    return out


def _parse_size(text):
    """Bytes from a size like "2G" / "512M"; 2 GB when it cannot be read."""
    text = str(text).strip().upper()
    mult = 1
    if text.endswith("G"):
        mult, text = 1 << 30, text[:-1]
    elif text.endswith("M"):
        mult, text = 1 << 20, text[:-1]
    elif text.endswith("K"):
        mult, text = 1 << 10, text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return 2 << 30


def pcat_new_hard_disk(dest, data):
    """A blank hard disk for the PC/AT machine: a sparse raw image, sized
    from the create form (2 GB by default).  Win98's own FDISK/FORMAT
    partitions and formats it, as on a real machine.  Kept raw (not qcow2)
    so no qemu-img is needed to make one; drive_backing reads it as raw.
    """
    size = _parse_size(data.get("size") or "2G")
    with open(dest, "wb") as f:
        f.truncate(size)
