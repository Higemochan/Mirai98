"""Drives as the appliance sees them: lsblk, /sys and /proc/mounts."""

import json
import os
import subprocess


def _mounted():
    """Device path -> where the host has it mounted."""
    out = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1 and parts[0].startswith("/dev/"):
                    out[parts[0]] = parts[1]
    except OSError:
        pass
    return out


def _sector(path):
    name = os.path.basename(path)
    for where in ("/sys/class/block/%s/queue/logical_block_size" % name,
                  "/sys/block/%s/queue/logical_block_size" % name):
        try:
            with open(where) as f:
                return int(f.read().strip()) or 512
        except (OSError, ValueError):
            pass
    return 512


def enumerate_drives():
    mounted = _mounted()
    out = []
    try:
        raw = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,RM,RO,MODEL,"
             "MOUNTPOINT"], capture_output=True, text=True,
            timeout=10).stdout
        blocks = json.loads(raw or "{}").get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        blocks = []

    def human(size):
        size = float(size or 0)
        for unit in ("B", "K", "M", "G", "T"):
            if size < 1024 or unit == "T":
                return ("%.1f%s" % (size, unit)).replace(".0", "")
            size /= 1024
        return ""

    def walk(node):
        path = node.get("path") or ""
        kind = node.get("type") or ""
        children = node.get("children") or []
        if kind in ("disk", "rom", "part"):
            busy = mounted.get(path, "") or node.get("mountpoint") or ""
            # a disk with a mounted partition is just as unavailable
            for child in children:
                busy = busy or mounted.get(child.get("path") or "", "") \
                       or (child.get("mountpoint") or "")
            size = int(node.get("size") or 0)
            out.append({
                "path": path,
                "size": human(size),
                "size_bytes": size,
                "sector": _sector(path),
                "type": "cdrom" if kind == "rom" else
                        ("fdd" if path.startswith("/dev/fd") else "hdd"),
                "removable": node.get("rm") in (True, "1"),
                "readonly": node.get("ro") in (True, "1"),
                "model": (node.get("model") or "").strip(),
                "busy": busy,
                # a loop device is the running system's own squashfs, and
                # / or /data being on it means the host lives there
                "system": path.startswith("/dev/loop") or
                          busy in ("/", "/data"),
            })
        for child in children:
            walk(child)

    for node in blocks:
        walk(node)
    # floppies rarely show up in lsblk unless there is media in the drive
    for n in range(4):
        path = "/dev/fd%d" % n
        if os.path.exists(path) and not any(d["path"] == path for d in out):
            out.append({"path": path, "size": "", "size_bytes": 0,
                        "sector": 512, "type": "fdd", "removable": True,
                        "readonly": False, "model": "floppy drive",
                        "busy": mounted.get(path, ""), "system": False})
    return out


def open_read(path):
    return open(path, "rb")


def open_write(path):
    # r+b, not wb: a device is not a file to be created, and truncating is
    # neither possible nor meant
    return open(path, "r+b")
