"""FM TOWNS machine plugin for the Mirai98 web manager.

Adds a "towns" machine type whose QEMU command line boots the FM TOWNS
BIOS from its real ROM set and feeds the disc as the CD-ROM.  The TOWNS
machine carries its own timer/CRTC/CD-ROM/sound, so none of the PC-98
ISA add-on devices are attached.  Create, start, snapshot and console are
the unchanged core flow; only the command line differs.

The matching front-end lives in web/plugins/towns.js (machine option,
Towns defaults, the list badge and the relative-pointer capture).
"""


def register(api):
    api.add_machine("towns")
    api.machine_argv("towns", lambda inst: towns_argv(api, inst))


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
    argv = [cfg["qemu"],
            # audiodev reaches the machine so the on-board YM2612/RF5C68 and
            # the CD-DA voice all open on the backend the VNC server streams
            "-M", "towns,accel=%s,audiodev=snd" % accel,
            "-m", inst.get("memory") or "16M",
            "-L", api.win_short(towns_roms),
            "-L", api.win_short(cfg["datadir"]),
            "-display", "none",
            "-audiodev", "none,id=snd",
            "-vnc", vncarg,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    if inst.get("snapshot"):
        argv.append("-snapshot")
    if inst.get("cd"):
        argv += ["-cdrom", api.win_short(api.disk_path(inst, "cd"))]
    if inst.get("extra"):
        argv += inst["extra"].split()
    return argv
