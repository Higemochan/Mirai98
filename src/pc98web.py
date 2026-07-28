#!/usr/bin/env python3

"""
Mirai98 Hypervisor Platform OS
Copyright (C) 2026 Awe Morris

Manages a fleet of QEMU PC-98 instances from a browser: create, start,
stop, reconfigure, and watch each machine's screen through noVNC.
QEMU's own VNC server speaks WebSocket, so the browser connects to it
directly and this process never touches the pixel path.

  pc98web.py [--host=0.0.0.0] [--port=8098] [--config=pc98web.json]

Storage follows one rule tree, so every feature knows where things are:

  <root>/
    disks/
      hdd/  fdd/  cdrom/
    vm/
      vm-0/
        vm.xml          the machine; disks are named by their file name
        qemu.log        under disks/<type>/, or by an explicit path
        thumb.png

Creating and converting images is delegated to virtpc98.py, which must
sit next to this script.

It runs on two kinds of host, and knows which one it is on:

  Linux    the Mirai98 appliance itself, where it also owns the host's
           network, the boot medium and the persistent system image
  Windows  a plain program, where it manages machines and images and
           leaves the host alone

Either way the first run asks where the machines should live and what
the password should be, and nothing else happens until it is answered.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = os.name == "nt"
PLATFORM = "windows" if WINDOWS else "linux"

# where the program and the things it ships with are: a frozen build has
# no source file to sit beside
PROG = os.path.dirname(os.path.abspath(sys.executable)) \
    if getattr(sys, "frozen", False) else HERE
# and where the user's own things go: the directory they started it in
BASE = os.getcwd() if WINDOWS else HERE

# ports are BASE + the instance's index, which instances keep for life
VNC_DISPLAY_BASE = 20            # display :20 = tcp 5920
WEBSOCKET_BASE = 5820
QMP_BASE = 4820

THUMB_AGE = 5                    # seconds a screen thumbnail stays fresh

MEMORY_CHOICES = ("640K", "2M", "4M", "8M", "16M", "32M", "64M", "128M",
                  "256M", "512M", "1G", "2G")

if WINDOWS:
    DEFAULTS = {
        "qemu": os.path.join(PROG, "qemu-system-i386.exe"),
        "roms": os.path.join(PROG, "roms"),
        # the directory that holds keymaps/, which the VNC server reads
        "datadir": PROG,
        "novnc": os.path.join(PROG, "novnc"),
        "root": os.path.join(BASE, "pc98"),
        # where the settings live: the directory the program was started
        # in, which is the one the user picked by starting it there
        "boot": BASE,
        "legacy": os.path.join(BASE, "instances.json"),
    }
else:
    DEFAULTS = {
        "qemu": os.path.expanduser("~/codex/qemu-system-i386"),
        "roms": os.path.expanduser("~/codex/roms-Xa7C9W"),
        "datadir": os.path.expanduser("~/codex"),
        "root": os.path.expanduser("~/codex/pc98"),
        # on the appliance this is the stick's own partition, which is
        # readable whatever else is or is not attached
        "boot": os.path.expanduser("~/codex"),
        "novnc": os.path.join(HERE, "novnc"),
        # a pre-rule instances.json is folded into the tree once, at startup
        "legacy": os.path.join(HERE, "instances.json"),
    }

CONFIG = dict(DEFAULTS)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DISK_KEYS = ("hdd1", "hdd2", "cd", "fdd1", "fdd2",
             "scsi1", "scsi2", "scsi3", "scsi4")
SCSI_KEYS = ("scsi1", "scsi2", "scsi3", "scsi4")
DISK_DIR = {"hdd1": "hdd", "hdd2": "hdd", "fdd1": "fdd", "fdd2": "fdd",
            "cd": "cdrom"}
NETWORKS = ("", "nat", "bridge")
MACHINES = ("pc9821", "pc9801")
# the ROM set a real machine has; anything not uploaded falls back to the
# compatible ROMs that ship with the appliance
ROM_FILES = ("pc98bios.bin", "pc98itf.bin", "pc98ide.bin", "pc98scsi.bin",
             "pc98pci.bin", "pc98font.bin", "pc98basic.bin")
BIOSES = ("compat", "real")
ACCELS = ("kvm", "tcg")
# a machine has one sound board, the way a real one did: the 86 board
# with its FM and PCM, the built-in Windows Sound System, or nothing
SOUNDS = {"86": (True, False), "wss": (False, True),
          "none": (False, False)}
DEFAULT_SOUND = "86"
# what the earlier two-board settings become
SOUND_ALIASES = {"opna+wss": "86", "opna": "86", "": DEFAULT_SOUND}


def sound_of(inst):
    """The board a machine asks for, whatever era its config is from."""
    value = inst.get("sound") or ""
    return SOUND_ALIASES.get(value, value if value in SOUNDS
                             else DEFAULT_SOUND)


# ------------------------------------------------------- the rule tree

def disks_root(kind):
    return os.path.join(CONFIG["root"], "disks", kind)


def vm_root():
    return os.path.join(CONFIG["root"], "vm")


def inst_dir(index):
    return os.path.join(vm_root(), "vm-%d" % index)


def ensure_tree():
    for path in [vm_root()] + [disks_root(k) for k in ("hdd", "fdd",
                                                       "cdrom")]:
        os.makedirs(path, exist_ok=True)


def disk_dir_of(key):
    """Which disks/ subdirectory a device's images live in."""
    return DISK_DIR.get(key, "hdd")            # SCSI units are plain disks


def disk_path(inst, key):
    """A device's image file: a bare name lives in the rule tree, and
    anything with a separator in it is taken as a path of its own,
    which is how a real drive like /dev/sr0 gets through."""
    value = inst.get(key) or ""
    if not value:
        return ""
    if "/" in value or value.startswith("~"):
        return os.path.expanduser(value)
    return os.path.join(disks_root(disk_dir_of(key)), value)


def is_device(path):
    return path.startswith("/dev/") or path.startswith("\\\\.\\")


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def host_drives():
    """Real drives the host could hand to a guest, and whether they are
    free to hand over: anything mounted stays with the host."""
    mounted = {}
    for path in ("/proc/mounts",):
        try:
            with open(path) as f:
                for line in f:
                    parts = line.split()
                    if len(parts) > 1 and parts[0].startswith("/dev/"):
                        mounted[parts[0]] = parts[1]
        except OSError:
            pass
    out = []
    try:
        raw = subprocess.run(
            ["lsblk", "-J", "-o", "NAME,PATH,SIZE,TYPE,RM,RO,MODEL,"
             "MOUNTPOINT"], capture_output=True, text=True,
            timeout=10).stdout
        blocks = json.loads(raw or "{}").get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        blocks = []

    def walk(node, parent=None):
        path = node.get("path") or ""
        kind = node.get("type") or ""
        if kind in ("disk", "rom", "part"):
            busy = mounted.get(path, "") or node.get("mountpoint") or ""
            children = node.get("children") or []
            # a disk whose partition is mounted is just as unavailable
            for child in children:
                busy = busy or mounted.get(child.get("path") or "", "")
            out.append({
                "path": path,
                "size": node.get("size") or "",
                "type": "cdrom" if kind == "rom" else
                        ("fdd" if path.startswith("/dev/fd") else "hdd"),
                "removable": node.get("rm") in (True, "1"),
                "readonly": node.get("ro") in (True, "1"),
                "model": (node.get("model") or "").strip(),
                "busy": busy,
                "system": path.startswith("/dev/loop") or
                          busy in ("/", "/data"),
            })
        for child in node.get("children") or []:
            walk(child, node)

    for node in blocks:
        walk(node)
    # floppies rarely show up in lsblk unless media is in the drive
    for n in range(4):
        path = "/dev/fd%d" % n
        if os.path.exists(path) and not any(d["path"] == path
                                            for d in out):
            out.append({"path": path, "size": "", "type": "fdd",
                        "removable": True, "readonly": False,
                        "model": "floppy drive", "busy": mounted.get(path, ""),
                        "system": False})
    return out


def busy_drives(inst):
    """Which of a machine's drives the host is currently using."""
    trouble = []
    by_path = {d["path"]: d for d in host_drives()}
    for key in DISK_KEYS:
        path = disk_path(inst, key)
        if not path or not is_device(path):
            continue
        drive = by_path.get(path)
        if drive is None:
            trouble.append("%s: %s is not there" % (key, path))
        elif drive["busy"]:
            trouble.append("%s: %s is mounted on %s"
                           % (key, path, drive["busy"]))
    return trouble


def serial_ports():
    """Host serial ports a guest could be given."""
    out = []
    for pattern in ("/dev/ttyS", "/dev/ttyUSB", "/dev/ttyACM"):
        for n in range(8):
            path = "%s%d" % (pattern, n)
            if not os.path.exists(path):
                continue
            if pattern == "/dev/ttyS":
                # most ttyS nodes are phantoms; a real one has a type
                try:
                    with open("/sys/class/tty/%s/type"
                              % os.path.basename(path)) as f:
                        if f.read().strip() == "0":
                            continue
                except OSError:
                    continue
            out.append(path)
    return out


DISK_KINDS = ("hdd", "fdd", "cdrom")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def disk_contents(kind, name):
    """What is inside an image, read through virtpc98's FAT reader."""
    import virtpc98
    path = os.path.join(disks_root(kind), name)
    files = []
    total = 0

    def note(line):
        # the extractor talks in "  NAME (n bytes)" and "  NAME/"
        text = line.strip()
        if not text or text.startswith(("partitions:", "extracted",
                                        "volume:")):
            return
        # "1: MS-DOS 6.20   at byte 69632" is the partition table talking
        if re.match(r"^\d+: .* at byte \d+$", text):
            return
        nonlocal total
        if text.endswith("/"):
            files.append({"name": text, "size": None})
            return
        size = None
        if text.endswith("bytes)") and "(" in text:
            try:
                size = int(text.rsplit("(", 1)[1].split()[0])
                total += size
            except ValueError:
                pass
            text = text.rsplit("(", 1)[0].strip()
        files.append({"name": text, "size": size})

    out = tempfile.mkdtemp(prefix="pc98-peek-")
    try:
        virtpc98.image_to_folder(path, out, floppy=(kind == "fdd"),
                                 partition=1, log=note)
        return {"files": files[:2000], "bytes": total}
    finally:
        shutil.rmtree(out, ignore_errors=True)


def disk_zip(kind, name, log=lambda *a: None):
    """A ZIP of everything inside the image, built in a temp file."""
    import virtpc98
    import zipfile
    path = os.path.join(disks_root(kind), name)
    folder = tempfile.mkdtemp(prefix="pc98-zip-")
    handle, archive = tempfile.mkstemp(prefix="pc98-zip-", suffix=".zip")
    os.close(handle)
    try:
        virtpc98.image_to_folder(path, folder, floppy=(kind == "fdd"),
                                 partition=1, log=log)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _dirs, names in os.walk(folder):
                for f in names:
                    full = os.path.join(root, f)
                    zf.write(full, os.path.relpath(full, folder))
        return archive
    except Exception:
        os.remove(archive)
        raise
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def zip_into_disk(kind, name, archive, log=lambda *a: None):
    """Unpack a ZIP into an image, replacing files of the same name."""
    import virtpc98
    import zipfile
    path = os.path.join(disks_root(kind), name)
    folder = tempfile.mkdtemp(prefix="pc98-unzip-")
    try:
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                # a ZIP may name anything at all; keep it inside folder
                target = os.path.realpath(os.path.join(folder, member))
                if not target.startswith(os.path.realpath(folder)):
                    raise ValueError("%s escapes the archive" % member)
            zf.extractall(folder)
        virtpc98.folder_into_image(folder, path, partition=1, log=log)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def rom_catalog():
    """The real ROM set, and which parts of it are actually here."""
    out = []
    for name in ROM_FILES:
        full = os.path.join(CONFIG["roms"], name)
        try:
            st = os.stat(full)
            out.append({"name": name, "present": True, "size": st.st_size,
                        "mtime": int(st.st_mtime)})
        except OSError:
            out.append({"name": name, "present": False, "size": 0,
                        "mtime": 0})
    return out


def disk_catalog():
    """Every image in the tree, with who is using it."""
    instances = load_instances()
    out = {}
    for kind in DISK_KINDS:
        entries = []
        root = disks_root(kind)
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            full = os.path.join(root, name)
            if (name.startswith(".") or name.endswith(".part")
                    or not os.path.isfile(full)):
                continue
            st = os.stat(full)
            used = sorted(i["name"] for i in instances
                          if any(disk_path(i, k) == full
                                 for k in DISK_KEYS))
            entries.append({"name": name, "size": st.st_size,
                            "mtime": int(st.st_mtime), "used_by": used})
        out[kind] = entries
    return out


# ------------------------------------------------------------ the fleet

_lock = threading.Lock()
_procs = {}                      # name -> Popen, for children we started
_cpu_cache = {}                  # name -> (pid, ticks, when)


def load_instance(index):
    path = os.path.join(inst_dir(index), "vm.xml")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    inst = {"name": root.findtext("name") or root.get("name")
                    or "vm-%d" % index, "index": index,
            "memory": root.findtext("memory") or "64M",
            "snapshot": (root.findtext("snapshot") or "no") == "yes",
            "net": root.findtext("net") or "",
            "machine": root.findtext("machine") or "pc9821",
            "bios": root.findtext("bios") or "compat",
            "accel": root.findtext("accel") or "kvm",
            "sound": root.findtext("sound") or DEFAULT_SOUND,
            "serial": root.findtext("serial") or "",
            "parallel": root.findtext("parallel") or "",
            "gpib": root.findtext("gpib") or "",
            "mount": root.findtext("mount") or "",
            "extra": root.findtext("extra") or ""}
    for key in DISK_KEYS:
        inst[key] = ""
    for disk in root.findall("disk"):
        dev = disk.get("dev", "")
        if dev in DISK_KEYS:
            inst[dev] = disk.get("ref", "")
    return inst


def save_instance(inst):
    root = ET.Element("vm", name=inst["name"])
    # also as a field of its own: the file is meant to be readable, and
    # editable, by whoever opens the stick on another machine
    ET.SubElement(root, "name").text = inst["name"]
    ET.SubElement(root, "machine").text = inst.get("machine") or "pc9821"
    ET.SubElement(root, "bios").text = inst.get("bios") or "compat"
    ET.SubElement(root, "accel").text = inst.get("accel") or "kvm"
    ET.SubElement(root, "memory").text = inst.get("memory") or "64M"
    ET.SubElement(root, "sound").text = sound_of(inst)
    ET.SubElement(root, "snapshot").text = ("yes" if inst.get("snapshot")
                                            else "no")
    if inst.get("net"):
        ET.SubElement(root, "net").text = inst["net"]
    for key in ("serial", "parallel", "gpib"):
        if inst.get(key):
            ET.SubElement(root, key).text = inst[key]
    if inst.get("mount"):
        ET.SubElement(root, "mount").text = inst["mount"]
    if inst.get("extra"):
        ET.SubElement(root, "extra").text = inst["extra"]
    for key in DISK_KEYS:
        if inst.get(key):
            ET.SubElement(root, "disk", dev=key, ref=inst[key])
    tree = ET.ElementTree(root)
    ET.indent(tree)
    os.makedirs(inst_dir(inst["index"]), exist_ok=True)
    path = os.path.join(inst_dir(inst["index"]), "vm.xml")
    with open(path, "w", encoding="utf-8") as f:
        tree.write(f, encoding="unicode")
        f.flush()
        os.fsync(f.fileno())      # the power may go before the cache does


def load_instances():
    out = []
    try:
        names = os.listdir(vm_root())
    except OSError:
        return out
    for name in names:
        m = re.match(r"^vm-(\d+)$", name)
        if m:
            inst = load_instance(int(m.group(1)))
            if inst:
                out.append(inst)
    out.sort(key=lambda i: i["index"])
    return out


def find_instance(instances, name):
    for inst in instances:
        if inst["name"] == name:
            return inst
    return None


def next_index(instances):
    used = {inst["index"] for inst in instances}
    index = 0
    while index in used:
        index += 1
    return index


def ports_of(inst):
    index = inst["index"]
    return (5900 + VNC_DISPLAY_BASE + index, WEBSOCKET_BASE + index,
            QMP_BASE + index)


def sanitize(data, taken_names=()):
    """A clean instance record from browser input, or (None, complaint)."""
    name = str(data.get("name", ""))
    if not NAME_RE.match(name):
        return None, "name must be 1-32 letters, digits, - or _"
    if name in taken_names:
        return None, "name already used"
    record = {"name": name,
              "memory": str(data.get("memory") or "64M"),
              "snapshot": bool(data.get("snapshot")),
              "net": str(data.get("net") or ""),
              "machine": str(data.get("machine") or "pc9821"),
              "bios": str(data.get("bios") or "compat"),
              "accel": str(data.get("accel") or "kvm"),
              "sound": SOUND_ALIASES.get(str(data.get("sound") or ""),
                                         str(data.get("sound") or "")),
              "extra": str(data.get("extra") or "")}
    if record["net"] not in NETWORKS:
        return None, "net must be empty, nat or bridge"
    if record["machine"] not in MACHINES:
        return None, "machine must be one of %s" % "/".join(MACHINES)
    if record["bios"] not in BIOSES:
        return None, "bios must be compat or real"
    if record["accel"] not in ACCELS:
        return None, "accel must be kvm or tcg"
    if record["sound"] not in SOUNDS:
        return None, "sound must be one of %s" % "/".join(SOUNDS)
    # ports that hang off a real device node: RS-232C, the parallel port
    # driven through an FT245RL in bit-bang mode, and GP-IB
    for key, label in (("serial", "serial port"),
                       ("parallel", "parallel port"),
                       ("gpib", "GP-IB adapter")):
        record[key] = str(data.get(key) or "").strip()
        if record[key] and not record[key].startswith("/dev/"):
            return None, "the %s must be a device like /dev/ttyUSB0" % label
    record["mount"] = str(data.get("mount") or "").strip()
    if record["mount"]:
        folder = os.path.expanduser(record["mount"])
        if not os.path.isdir(folder):
            return None, "%s is not a folder on this host" % folder
    for key in DISK_KEYS:
        record[key] = str(data.get(key) or "").strip()
        if record[key]:
            path = disk_path(record, key)
            if not os.path.exists(path):
                return None, "%s: %s does not exist" % (key, path)
    # a machine with no disks at all is fine: it lands in N88 BASIC,
    # which lives in ROM
    return record, None


def seed_fleet():
    """A machine to try on the very first boot.

    The appliance ships a FreeDOS(98) image in seed/; the first time it
    runs it copies that into disks/ and defines a machine around it, so
    a fresh stick has something to press Start on.
    """
    if load_instances() or read_settings().get("seeded"):
        return
    seed = os.path.join(CONFIG["datadir"], "seed")
    made = False
    for name in sorted(os.listdir(seed)) if os.path.isdir(seed) else []:
        if not name.lower().endswith((".raw", ".img", ".hdi", ".qcow2")):
            continue
        dest = os.path.join(disks_root("hdd"), name)
        if not os.path.exists(dest):
            shutil.copy2(os.path.join(seed, name), dest)
        record = {"name": os.path.splitext(name)[0][:32],
                  "index": next_index(load_instances()),
                  "memory": "16M", "snapshot": False, "net": "",
                  "machine": "pc9821", "bios": "compat", "accel": "kvm",
                  "sound": DEFAULT_SOUND, "extra": "", "hdd1": name}
        for key in DISK_KEYS:
            record.setdefault(key, "")
        save_instance(record)
        say("seeded machine %s from %s" % (record["name"], name), "system")
        made = True
    write_settings({"seeded": True})
    return made


def migrate_legacy():
    """Fold a pre-rule instances.json into the tree, once."""
    legacy = CONFIG.get("legacy")
    if not legacy or not os.path.exists(legacy) or os.listdir(vm_root()):
        return
    try:
        with open(legacy, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return
    for inst in old:
        for key in DISK_KEYS:
            path = inst.get(key) or ""
            if path and os.path.isabs(path) and os.path.exists(path):
                dest = os.path.join(disks_root(disk_dir_of(key)),
                                    os.path.basename(path))
                if not os.path.exists(dest):
                    try:
                        os.link(path, dest)
                    except OSError:
                        shutil.copy2(path, dest)
                inst[key] = os.path.basename(path)
        for key in SCSI_KEYS:
            inst.setdefault(key, "")
        save_instance(inst)
        print("migrated %s -> vm-%d" % (inst["name"], inst["index"]))
    os.rename(legacy, legacy + ".migrated")


# -------------------------------------------------------------- control

def qmp(inst, command, arguments=None, timeout=3.0):
    """One QMP command against an instance; None when unreachable."""
    _vnc, _ws, port = ports_of(inst)
    try:
        with socket.create_connection(("127.0.0.1", port),
                                      timeout=timeout) as sock:
            f = sock.makefile("rw", encoding="utf-8", newline="\n")
            f.readline()                            # greeting
            commands = [{"execute": "qmp_capabilities"},
                        {"execute": command,
                         "arguments": arguments or {}}]
            for cmd in commands:
                f.write(json.dumps(cmd) + "\n")
                f.flush()
                while True:
                    line = f.readline()
                    if not line:
                        return None
                    msg = json.loads(line)
                    if "return" in msg or "error" in msg:
                        break
            return msg
    except OSError:
        return None


def is_running(inst):
    """The QMP port answers: even after this server restarts, a machine
    started by an earlier run is still found and controllable."""
    _vnc, _ws, port = ports_of(inst)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def pid_of(inst):
    """The QEMU pid, even for one an earlier server run started."""
    proc = _procs.get(inst["name"])
    if proc is not None and proc.poll() is None:
        return proc.pid
    _vnc, _ws, port = ports_of(inst)
    try:
        out = subprocess.run(
            ["pgrep", "-f", "qmp tcp:127.0.0.1:%d," % port],
            capture_output=True, text=True, timeout=5).stdout.split()
        return int(out[0]) if out else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def usage_of(inst):
    """CPU %% of one guest CPU and resident bytes, from /proc.

    A PC-98 guest has no channel for reporting how it feels inside, so
    the host's view of the QEMU process is what there is.
    """
    pid = pid_of(inst)
    if pid is None:
        return None
    try:
        with open("/proc/%d/stat" % pid) as f:
            fields = f.read().rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])   # utime + stime
        with open("/proc/%d/statm" % pid) as f:
            rss = int(f.read().split()[1]) * os.sysconf("SC_PAGESIZE")
    except (OSError, IndexError, ValueError):
        return None
    now = time.time()
    cpu = None
    previous = _cpu_cache.get(inst["name"])
    if previous and previous[0] == pid and now > previous[2]:
        hz = os.sysconf("SC_CLK_TCK")
        cpu = (ticks - previous[1]) / hz / (now - previous[2]) * 100
        cpu = round(max(cpu, 0.0), 1)
    _cpu_cache[inst["name"]] = (pid, ticks, now)
    return {"pid": pid, "cpu": cpu, "rss": rss}


def drive_backing(path):
    """format= and file=, with the format read off the extension; QEMU
    told format=raw would happily show a guest the qcow2 container."""
    fmt = "qcow2" if path.lower().endswith(".qcow2") else "raw"
    return "format=%s,file=%s" % (fmt, path)


_host_cpu = {}                   # last /proc/stat totals, for the delta


def windows_usage():
    """The same vitals on Windows, from the kernel's own counters."""
    import ctypes
    out = {"cores": os.cpu_count()}

    class MemStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        status = MemStatus()
        status.dwLength = ctypes.sizeof(MemStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            out["mem_total"] = status.ullTotalPhys
            out["mem_used"] = status.ullTotalPhys - status.ullAvailPhys
    except OSError:
        pass
    try:
        idle, kernel, user = (ctypes.c_ulonglong(), ctypes.c_ulonglong(),
                              ctypes.c_ulonglong())
        if ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel),
                ctypes.byref(user)):
            total = kernel.value + user.value      # kernel time includes idle
            if _host_cpu:
                d_total = total - _host_cpu["total"]
                d_idle = idle.value - _host_cpu["idle"]
                if d_total > 0:
                    out["cpu"] = round(max(0.0, 100.0 * (d_total - d_idle)
                                           / d_total), 1)
            _host_cpu.update(total=total, idle=idle.value)
        out["uptime"] = int(ctypes.windll.kernel32.GetTickCount64() / 1000)
    except OSError:
        pass
    try:
        usage = shutil.disk_usage(CONFIG["root"])
        out["disk_total"] = usage.total
        out["disk_free"] = usage.free
    except OSError:
        pass
    return out


def host_usage():
    """The appliance's own vitals: CPU busy percent, memory, uptime."""
    if WINDOWS:
        return windows_usage()
    out = {}
    try:
        with open("/proc/stat") as f:
            parts = [int(v) for v in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        if _host_cpu:
            d_total = total - _host_cpu["total"]
            d_idle = idle - _host_cpu["idle"]
            if d_total > 0:
                out["cpu"] = round(max(0.0, 100.0 * (d_total - d_idle)
                                       / d_total), 1)
        _host_cpu.update(total=total, idle=idle)
        out["cores"] = os.cpu_count()
    except (OSError, IndexError, ValueError):
        pass
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) * 1024
        out["mem_total"] = info.get("MemTotal", 0)
        out["mem_used"] = info["MemTotal"] - info["MemAvailable"]
    except (OSError, KeyError, ValueError):
        pass
    try:
        with open("/proc/uptime") as f:
            out["uptime"] = int(float(f.read().split()[0]))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/loadavg") as f:
            out["load"] = f.read().split()[:3]
    except OSError:
        pass
    try:
        stat = os.statvfs(CONFIG["root"])
        out["disk_total"] = stat.f_blocks * stat.f_frsize
        out["disk_free"] = stat.f_bavail * stat.f_frsize
    except OSError:
        pass
    return out


# ------------------------------------------------------ the host's network

# ---------------------------------------------------------------- the log

LOG_MAX = 1 << 20                # trim once the file passes a megabyte
LOG_KEEP = 1000                  # lines kept when it does
_log_lock = threading.Lock()


def log_path():
    # beside the machines: a log is bulk, and belongs with the bulk
    return os.path.join(os.path.dirname(CONFIG["root"]), "mirai98.log")


def storage_root():
    """The directory the machines and images live under.

    It is the root of whichever filesystem was chosen, so there is one
    path to remember and nothing to look for underneath it.
    """
    return os.path.dirname(CONFIG["root"])


# what a log line can be about, so the reader can narrow it down
LOG_KINDS = ("system", "vm", "disk", "network", "web")


def say(text, kind="system"):
    """One line in the system log, and on stderr for whoever is watching."""
    line = "%s [%s] %s" % (time.strftime("%F %T"), kind, text)
    sys.stderr.write(line + "\n")
    try:
        with _log_lock:
            path = log_path()
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(path) > LOG_MAX:
                with open(path, encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-LOG_KEEP:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(tail)
    except OSError:
        pass                     # a log that cannot be written is not fatal


def start_log():
    """Every boot starts with a clean page."""
    try:
        os.makedirs(os.path.dirname(log_path()), exist_ok=True)
        with open(log_path(), "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass
    say("Mirai98 started")


def read_log(limit=500, kind=""):
    try:
        with open(log_path(), encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    if kind:
        tag = "[%s]" % kind
        lines = [ln for ln in lines if tag in ln.split(" ", 3)[2:3]]
    return lines[-limit:]


# ------------------------------------------------------------- the lock

_sessions = set()


def password_set():
    return bool(read_settings().get("password"))


def shadow_hash():
    """Root's stored password hash, or "" if it has none."""
    try:
        with open("/etc/shadow") as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == "root":
                    hash_ = parts[1]
                    return "" if hash_ in ("", "!", "!!", "*") else hash_
    except OSError:
        pass
    return ""


def restore_password():
    """Put root's password back after a live boot.

    A live system starts from the squashfs every time, so /etc/shadow
    forgets.  The hash chpasswd made is kept beside the settings and put
    back here, or the web would ask for a password the shell no longer
    wants.
    """
    if WINDOWS:
        return
    saved = read_settings().get("shadow")
    if not saved or shadow_hash():
        return
    out = subprocess.run(["usermod", "-p", saved, "root"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode:
        say("could not restore root's password: %s"
            % (out.stderr.strip() or out.returncode))
        return
    subprocess.run(["systemctl", "restart", "mirai98-terminal"],
                   capture_output=True)
    say("restored root's password from the settings")


def save_verifier(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    write_settings({"password": {"salt": salt.hex(), "hash": digest.hex()}})


def set_password(password):
    """Set root's password, and keep a verifier for the web side.

    On the appliance the password is root's own, so the shell asks for
    the same one.  The web cannot read a shadow hash back, so it keeps
    its own verifier beside the settings.  Changing root's password from
    a shell leaves that verifier behind, which is worth saying out loud.

    On Windows there is no root to speak of, so the verifier is all
    there is, and it guards this page and nothing else.
    """
    if WINDOWS:
        if password:
            save_verifier(password)
        else:
            write_settings({"password": None})
        _sessions.clear()
        say("console password %s" % ("changed" if password else "cleared"))
        return
    if password:
        out = subprocess.run(["chpasswd"], input="root:%s\n" % password,
                             capture_output=True, text=True, timeout=30)
        if out.returncode:
            raise OSError(out.stderr.strip() or "chpasswd refused")
        save_verifier(password)
        # a live boot forgets /etc/shadow, so keep the hash it just made
        write_settings({"shadow": shadow_hash()})
    else:
        out = subprocess.run(["passwd", "-d", "root"], capture_output=True,
                             text=True, timeout=30)
        if out.returncode:
            raise OSError(out.stderr.strip() or "passwd refused")
        write_settings({"password": None, "shadow": None})
    _sessions.clear()
    subprocess.run(["systemctl", "restart", "mirai98-terminal"],
                   capture_output=True)
    say("root password %s" % ("changed" if password else "cleared"),
        "system")


def password_ok(password):
    saved = read_settings().get("password") or {}
    if not saved:
        return True
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     bytes.fromhex(saved["salt"]), 200_000)
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(digest.hex(), saved.get("hash", ""))


def settings_path():
    """One JSON file on the boot medium: it says which drive holds the
    machines, so it cannot live on that drive."""
    return os.path.join(CONFIG["boot"], "mirai98.json")


def read_settings():
    """The settings, or the copy kept beside them.

    FAT32 on a stick that gets pulled can leave the file empty, and an
    empty file here would look exactly like a machine nobody has set up
    yet, so the previous version is always kept.
    """
    for path in (settings_path(), settings_path() + ".bak"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except (OSError, ValueError):
            continue
    return {}


def write_settings(update):
    """Save the settings, and make sure they are really on the medium.

    This file says where everything else is, and the machine it runs on
    is one somebody will unplug.  It is worth the fsync.
    """
    data = read_settings()
    data.update(update)
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = json.dumps(data, indent=2)
    # keep the version that is already there: if this write is torn, that
    # copy is what the next boot reads
    try:
        if os.path.getsize(path) > 0:
            shutil.copyfile(path, path + ".bak")
            sync_file(path + ".bak")
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # the rename itself has to reach the medium, and on FAT that means
    # asking for the file and its directory by hand
    sync_file(path)
    if not WINDOWS:
        try:
            fd = os.open(os.path.dirname(path), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    return data


def sync_file(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# ------------------------------------------------------- the first run

def setup_done():
    """Whether anyone has ever answered the two questions."""
    return bool(read_settings().get("setup"))


def grown_by():
    """How much room the first boot found on the stick, in MB, if it
    grew the data partition to take it."""
    try:
        with open("/run/mirai98-grown") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def windows_drives():
    """Every drive letter a folder could be made on."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    out = []
    mask = kernel32.GetLogicalDrives()
    for n in range(26):
        if not mask & (1 << n):
            continue
        root = "%s:\\" % chr(ord("A") + n)
        kind = kernel32.GetDriveTypeW(root)
        if kind in (4, 5):                    # network shares and CD drives
            continue
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            continue                          # a card reader with no card
        if usage.total < (1 << 30):           # a floppy is not a data drive
            continue
        label = ctypes.create_unicode_buffer(261)
        fstype = ctypes.create_unicode_buffer(261)
        try:
            kernel32.GetVolumeInformationW(root, label, 261, None, None,
                                           None, fstype, 261)
        except OSError:
            pass
        out.append({"id": root, "path": root, "label": label.value,
                    "fstype": (fstype.value or "").lower(),
                    "size": usage.total, "free": usage.free,
                    "note": FS_NOTES.get((fstype.value or "").lower(), ""),
                    "removable": kind == 2})
    return out


def data_choices():
    """Where the machines could live, other than where they are."""
    if WINDOWS:
        return windows_drives()
    out = []
    for volume in volumes():
        if volume["mountpoint"] in ("/", "/boot-data", "/storage",
                                    MEDIUM) or not volume["uuid"]:
            continue
        # toram leaves the medium unmounted, so the labels are what says
        # this partition is ours and not somewhere to put machines
        if volume["label"] in (BOOT_LABEL, DATA_LABEL):
            continue
        out.append({"id": volume["uuid"], "path": volume["path"],
                    "label": volume["label"], "fstype": volume["fstype"],
                    "size": volume["size"], "free": 0,
                    "note": volume["note"],
                    "removable": volume["removable"]})
    return out


def default_storage():
    """What staying put means, said in one line."""
    if WINDOWS:
        return {"path": BASE, "what": "where this program was started"}
    stick = "the boot medium"
    grown = grown_by()
    if grown:
        stick = "the boot medium, grown by %s" % human_mb(grown)
    # the medium is wherever the settings are: that is the one partition
    # this system always has
    return {"path": CONFIG["boot"], "what": stick}


def human_mb(megabytes):
    return ("%.1f GB" % (megabytes / 1024.0)) if megabytes >= 1024 \
        else "%d MB" % megabytes


def use_storage(path):
    """Point everything at a directory, from this moment on."""
    CONFIG["root"] = os.path.join(path, "pc98")
    CONFIG["roms"] = os.path.join(path, "roms")
    ensure_tree()
    os.makedirs(CONFIG["roms"], exist_ok=True)


def mount_data(volume):
    """Mount a chosen filesystem where the init script would have."""
    mount = "/mnt/data"
    os.makedirs(mount, exist_ok=True)
    if not os.path.ismount(mount):
        out = subprocess.run(["mount", volume["path"], mount],
                             capture_output=True, text=True, timeout=60)
        if out.returncode:
            raise OSError(out.stderr.strip() or "mount refused")
    probe = os.path.join(mount, ".writable")
    try:
        with open(probe, "w"):
            pass
        os.remove(probe)
    except OSError:
        raise OSError("%s is read-only" % volume["path"])
    return mount


def copy_storage(source, target):
    """Copy the machines and images across, and say how much moved.

    copyfile, not copy2: FAT32 and NTFS refuse the ownership and mode a
    faithful copy would try to carry over.
    """
    moved = 0
    for name in ("pc98", "roms"):
        here = os.path.join(source, name)
        if not os.path.isdir(here):
            continue
        shutil.copytree(here, os.path.join(target, name),
                        dirs_exist_ok=True, copy_function=shutil.copyfile)
        for base, _, files in os.walk(here):
            moved += sum(os.path.getsize(os.path.join(base, f))
                         for f in files if os.path.exists(
                             os.path.join(base, f)))
    return moved


DATA_FOLDER = "Mirai98"          # what we make on somebody else's drive


def choose_data(volume_id):
    """Answer the storage question, and act on the answer now.

    The boot medium is used as it is, since it is ours.  Any other
    drive belongs to whoever formatted it, so the machines go in a
    folder of their own on it.

    The choice is written on the boot medium, never on the drive it
    names, so taking that drive away costs the machines and nothing
    else.
    """
    if not volume_id:
        if WINDOWS:
            path = default_storage()["path"]
            write_settings({"data": {"path": path}})
            use_storage(path)
            return path
        write_settings({"data": {}})
        use_storage(storage_root())
        return storage_root()
    if WINDOWS:
        drive = next((d for d in windows_drives() if d["id"] == volume_id),
                     None)
        if drive is None:
            raise OSError("no drive %s" % volume_id)
        path = os.path.join(drive["path"], DATA_FOLDER)
        os.makedirs(path, exist_ok=True)
        write_settings({"data": {"path": path, "fstype": drive["fstype"]}})
        use_storage(path)
        return path
    volume = next((v for v in volumes() if v["uuid"] == volume_id), None)
    if volume is None:
        raise OSError("no filesystem with that id")
    if volume["fstype"].startswith("ntfs") and ntfs_is_dirty(volume["path"]):
        raise OSError("Windows left %s hibernated: shut Windows down fully, "
                      "then try again" % volume["path"])
    mount = mount_data(volume)
    path = os.path.join(mount, DATA_FOLDER)
    os.makedirs(path, exist_ok=True)
    write_settings({"data": {"uuid": volume_id, "fstype": volume["fstype"],
                             "label": volume["label"],
                             "folder": DATA_FOLDER}})
    use_storage(path)
    return path


BRIDGE = "br98"


NETWORK_DEFAULTS = {"mode": "dhcp", "address": "", "gateway": "",
                    "dns": "", "bridge": "on"}


def read_network():
    conf = dict(NETWORK_DEFAULTS)
    saved = read_settings().get("network")
    if isinstance(saved, dict):
        conf.update({k: str(v) for k, v in saved.items() if k in conf})
    return conf


def write_network(conf):
    write_settings({"network": conf})


def uplink():
    """The wired interface guests should reach the LAN through.

    Wireless is left alone: a station cannot carry the extra MAC
    addresses a bridge would put on the air.
    """
    for iface in interfaces():
        name = iface["name"]
        if name.startswith(("en", "eth")) and name != BRIDGE:
            return name
    return ""


def bridge_state():
    """(exists, members) for the one bridge Mirai98 manages."""
    if not os.path.isdir("/sys/class/net/%s" % BRIDGE):
        return False, []
    try:
        members = sorted(os.listdir("/sys/class/net/%s/brif" % BRIDGE))
    except OSError:
        members = []
    return True, members


def ip(*args, log=lambda *a: None):
    try:
        out = subprocess.run(["ip"] + list(args), capture_output=True,
                             text=True, timeout=10)
        if out.returncode:
            log("ip %s: %s" % (" ".join(args), out.stderr.strip()))
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError) as err:
        log("ip %s: %s" % (" ".join(args), err))
        return False


def setup_bridge(conf, log=lambda *a: None):
    """Put the wired interface into br98 and move the addressing there.

    Guests attach to this bridge and appear on the LAN directly; the
    hypervisor keeps talking through the same bridge, so there is only
    ever one address to remember.
    """
    want = (conf.get("bridge", "on") != "off")
    exists, _members = bridge_state()
    port = uplink()
    if not want:
        if exists:
            ip("link", "set", BRIDGE, "down", log=log)
            ip("link", "del", BRIDGE, log=log)
        return "bridge off"
    if not port:
        return "no wired interface to bridge"
    if not exists:
        if not ip("link", "add", BRIDGE, "type", "bridge", log=log):
            return "could not create %s (needs root)" % BRIDGE
    ip("link", "set", BRIDGE, "up", log=log)
    _exists, members = bridge_state()
    if port not in members:
        # the address follows the port into the bridge
        addresses = [a for i in interfaces() if i["name"] == port
                     for a in i["addresses"]]
        ip("addr", "flush", "dev", port, log=log)
        ip("link", "set", port, "master", BRIDGE, log=log)
        for address in addresses:
            ip("addr", "add", address, "dev", BRIDGE, log=log)
    if conf.get("mode") == "static" and conf.get("address"):
        ip("addr", "replace", conf["address"], "dev", BRIDGE, log=log)
        if conf.get("gateway"):
            ip("route", "replace", "default", "via", conf["gateway"],
               log=log)
    # nothing asks for a lease here: the caller rewrites the networkd
    # unit to match the bridge and reloads, and networkd is the only
    # DHCP client this system has
    return "bridge on (%s)" % BRIDGE


def interfaces():
    """Wired interfaces with their addresses, straight from `ip`."""
    out = []
    try:
        raw = subprocess.run(["ip", "-j", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
        for link in json.loads(raw or "[]"):
            if link.get("link_type") == "loopback":
                continue
            out.append({
                "name": link.get("ifname"),
                "mac": link.get("address", ""),
                "state": link.get("operstate", ""),
                "addresses": [a["local"] + "/" + str(a["prefixlen"])
                              for a in link.get("addr_info", [])
                              if a.get("family") == "inet"],
            })
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return out


def apply_network(conf, log=lambda *a: None):
    """Rewrite the systemd-networkd drop-in and reload it.

    The running session may well be cut by this; the caller answers the
    browser first and lets it find its own way back.
    """
    note = setup_bridge(conf, log)
    unit = "/etc/systemd/network/mirai98.network"
    if not os.path.isdir(os.path.dirname(unit)):
        return "%s; addressing saved for the next boot" % note
    name = BRIDGE if conf.get("bridge", "on") != "off" else "en* eth*"
    body = ["[Match]", "Name=" + name, "", "[Network]"]
    if conf.get("mode") == "static" and conf.get("address"):
        body.append("Address=" + conf["address"])
        if conf.get("gateway"):
            body.append("Gateway=" + conf["gateway"])
    else:
        body.append("DHCP=yes")
    # name servers are set either way: given ones win over the lease
    servers = (conf.get("dns") or "").replace(",", " ").split()
    for server in servers:
        body.append("DNS=" + server)
    if servers:
        body.append("UseDNS=no")
    try:
        with open(unit, "w", encoding="utf-8") as f:
            f.write("\n".join(body) + "\n")
        subprocess.run(["networkctl", "reload"], capture_output=True,
                       timeout=10)
    except (OSError, subprocess.SubprocessError) as err:
        return "saved, but applying failed: %s" % err
    return "applied; " + note


# ------------------------------------------------------------- downloads

_jobs = {}                       # name -> {done, total, state, error}


def fetch_disk(kind, name, url):
    """Pull an image off the web into disks/, tracking progress."""
    import urllib.request
    dest = os.path.join(disks_root(kind), name)
    part = dest + ".part"
    job = {"name": name, "kind": kind, "done": 0, "total": 0,
           "state": "running", "error": ""}
    _jobs[name] = job

    def run():
        try:
            with urllib.request.urlopen(url, timeout=30) as src, \
                    open(part, "wb") as out:
                job["total"] = int(src.headers.get("Content-Length") or 0)
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
                    job["done"] += len(chunk)
            os.replace(part, dest)
            job["state"] = "done"
            say("downloaded %s (%d bytes) from %s" % (name, job["done"], url),
                "disk")
        except Exception as err:                  # any network trouble
            job["state"] = "failed"
            job["error"] = str(err)
            say("download of %s failed: %s" % (name, err), "disk")
            try:
                os.remove(part)
            except OSError:
                pass

    threading.Thread(target=run, daemon=True).start()
    return job


MEDIUM = "/run/live/medium"
# what a data drive may be formatted as, and what that costs
FS_NOTES = {
    "ext4": "",
    "exfat": "",
    "ntfs": "read-only if Windows left it hibernated",
    "ntfs3": "read-only if Windows left it hibernated",
    "vfat": "no file over 4 GB",
    "fat32": "no file over 4 GB",
}
FS_LIMITS = {"vfat": (4 << 30) - 1, "fat32": (4 << 30) - 1}


def volumes():
    """Every mountable filesystem the host can see, with what it is."""
    out = []
    try:
        raw = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "PATH,SIZE,FSTYPE,LABEL,UUID,TYPE,MOUNTPOINT,RM,MODEL"],
            capture_output=True, text=True, timeout=10).stdout
        blocks = json.loads(raw or "{}").get("blockdevices", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        blocks = []

    def walk(node, parent=""):
        fstype = (node.get("fstype") or "").lower()
        path = node.get("path") or ""
        if fstype in FS_NOTES:
            out.append({
                "path": path,
                "fstype": fstype,
                "label": node.get("label") or "",
                "uuid": node.get("uuid") or "",
                "size": node.get("size") or 0,
                "mountpoint": node.get("mountpoint") or "",
                "removable": node.get("rm") in (True, "1"),
                "model": (parent or "").strip(),
                "note": FS_NOTES.get(fstype, ""),
            })
        for child in node.get("children") or []:
            walk(child, (node.get("model") or parent))

    for node in blocks:
        walk(node)
    return out


def fs_of(path):
    """Which filesystem a path sits on, as lsblk names it."""
    try:
        out = subprocess.run(["findmnt", "-no", "FSTYPE", "-T", path],
                             capture_output=True, text=True,
                             timeout=10).stdout.strip().lower()
        return out
    except (OSError, subprocess.SubprocessError):
        return ""


def size_limit(path):
    """The largest file the filesystem under path can hold, or None."""
    return FS_LIMITS.get(fs_of(path))


def ntfs_is_dirty(device):
    """Windows fast startup leaves a flag that makes ntfs3 refuse to
    write; better to say so than to mount and disappoint later."""
    try:
        out = subprocess.run(["ntfsfix", "--no-action", device],
                             capture_output=True, text=True, timeout=30)
        text = (out.stdout + out.stderr).lower()
        return "hibernated" in text or "dirty" in text or "unclean" in text
    except (OSError, subprocess.SubprocessError):
        return False


SYSTEM_LABEL = "mirai98-sys"
# the two partitions the medium is made of; FAT labels stop at 11
BOOT_LABEL = "MIRAI98"
DATA_LABEL = "MIRAI98DATA"


def extension_path():
    """The image live-boot loads as the root overlay's upper layer.

    live-boot looks for a file named after the persistence label, so the
    name is not ours to choose.
    """
    return os.path.join(storage_root(), SYSTEM_LABEL)


def extension_state():
    """Whether the extension exists, and whether this boot is using it."""
    path = extension_path()
    present = os.path.isfile(path)
    active = False
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "/" and \
                        parts[2] == "overlay":
                    active = "persistence" in line
    except OSError:
        pass
    return {"present": present,
            "size": os.path.getsize(path) if present else 0,
            "active": active,
            "path": path,
            "writable_root": os.access("/etc", os.W_OK)}


def grub_paths():
    """Every grub.cfg that governs this machine's next boot."""
    out = []
    for base in (MEDIUM, "/boot-data", "/boot"):
        path = os.path.join(base, "boot", "grub", "grub.cfg")
        if os.path.isfile(path):
            out.append(path)
    # the medium copy is a RAM copy when booted with toram; the real one
    # lives on whichever partition is labelled MIRAI98
    device = subprocess.run(["blkid", "-L", BOOT_LABEL], capture_output=True,
                            text=True).stdout.strip()
    if device:
        mount = "/run/mirai98-boot"
        os.makedirs(mount, exist_ok=True)
        if not os.path.ismount(mount):
            subprocess.run(["mount", device, mount], capture_output=True,
                           timeout=60)
        path = os.path.join(mount, "boot", "grub", "grub.cfg")
        if os.path.isfile(path):
            out.append(path)
    return out


PERSIST_ARGS = ("persistence persistence-storage=file persistence-label=%s "
                "persistence-path=mirai98" % SYSTEM_LABEL)


def set_persistence(on):
    """Turn the persistent overlay on or off for the next boot.

    The first menu entry carries it; the second never does, so a broken
    upgrade is one menu choice away from being ignored.
    """
    touched = []
    for path in grub_paths():
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        lines = []
        first = True
        for line in text.splitlines():
            if "boot=live" in line and "console=ttyS0" not in line:
                line = line.replace(" " + PERSIST_ARGS, "")
                if on and first:
                    line = line.replace("boot=live toram",
                                        "boot=live toram " + PERSIST_ARGS)
                first = False
            lines.append(line)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            touched.append(path)
        except OSError:
            continue
    return touched


def make_extension(megabytes):
    """A fresh ext4 image for the overlay, with the marker live-boot
    wants inside it."""
    path = extension_path()
    if os.path.exists(path):
        return "the system extension is already there"
    cap = size_limit(storage_root())
    if cap and megabytes << 20 > cap:
        return "this storage is FAT32: no file over 4 GB"
    mount = "/run/mirai98-ext"
    subprocess.run(["truncate", "-s", "%dM" % megabytes, path], check=True,
                   timeout=120)
    try:
        subprocess.run(["mkfs.ext4", "-q", "-F", "-L", SYSTEM_LABEL, path],
                       check=True, timeout=600)
        os.makedirs(mount, exist_ok=True)
        subprocess.run(["mount", "-o", "loop", path, mount], check=True,
                       timeout=60)
        with open(os.path.join(mount, "persistence.conf"), "w") as f:
            f.write("/ union\n")
    finally:
        subprocess.run(["umount", mount], capture_output=True)
    touched = set_persistence(True)
    say("made the system extension (%d MB); grub updated in %s"
        % (megabytes, ", ".join(touched) or "nothing"), "system")
    return "made; the next boot keeps system changes"


def refresh_medium_kernel(out):
    """Put a freshly installed kernel where the boot loader looks.

    The kernel is read from the medium's /live, not from the root, so an
    upgrade only means anything once the pair is copied across.  The old
    pair stays behind as .old for the second menu entry to fall back on.
    """
    device = subprocess.run(["blkid", "-L", BOOT_LABEL], capture_output=True,
                            text=True).stdout.strip()
    if not device:
        out("no boot partition found; the kernel on the medium is unchanged")
        return
    mount = "/run/mirai98-boot"
    os.makedirs(mount, exist_ok=True)
    if not os.path.ismount(mount):
        subprocess.run(["mount", device, mount], capture_output=True,
                       timeout=60)
    live = os.path.join(mount, "live")
    if not os.path.isdir(live):
        out("the medium has no /live; leaving it alone")
        return
    kernels = sorted(f for f in os.listdir("/boot")
                     if f.startswith("vmlinuz-"))
    initrds = sorted(f for f in os.listdir("/boot")
                     if f.startswith("initrd.img-"))
    if not kernels or not initrds:
        out("no kernel in /boot; nothing to copy")
        return
    new_kernel = os.path.join("/boot", kernels[-1])
    new_initrd = os.path.join("/boot", initrds[-1])
    same = False
    try:
        same = (os.path.getsize(new_kernel) ==
                os.path.getsize(os.path.join(live, "vmlinuz")))
    except OSError:
        pass
    if same:
        out("the medium already has this kernel")
        return
    for source, name in ((new_kernel, "vmlinuz"),
                         (new_initrd, "initrd.img")):
        target = os.path.join(live, name)
        if os.path.exists(target):
            shutil.copy2(target, target + ".old")
        shutil.copy2(source, target)
    out("copied %s onto the medium; the old pair is kept as .old"
        % os.path.basename(new_kernel))


def start_update():
    """apt, with its output kept for the browser to read."""
    job = {"name": "system update", "kind": "update", "done": 0, "total": 4,
           "state": "running", "error": "", "lines": []}
    _jobs["system update"] = job

    def out(text):
        job["lines"].append(text.rstrip())
        del job["lines"][:-400]

    def run(*args):
        out("$ " + " ".join(args))
        proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                env=dict(os.environ,
                                         DEBIAN_FRONTEND="noninteractive"))
        for line in proc.stdout:
            out(line)
        return proc.wait()

    def work():
        try:
            for step, args in enumerate((
                    ("apt-get", "update"),
                    ("apt-get", "-y", "upgrade"),
                    ("apt-get", "-y", "dist-upgrade"))):
                job["done"] = step + 1
                if run(*args):
                    raise OSError("%s failed" % " ".join(args))
            job["done"] = 4
            refresh_medium_kernel(out)
            job["state"] = "done"
            out("--- finished; reboot to run what was installed")
            say("system update finished", "system")
        except Exception as err:
            job.update(state="failed", error=str(err))
            out("--- failed: %s" % err)
            say("system update failed: %s" % err, "system")

    threading.Thread(target=work, daemon=True).start()
    return job


def sectors_of(name):
    """How many 512-byte sectors a block device has, or 0."""
    try:
        with open("/sys/class/block/%s/size" % name) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def install_targets():
    """Disks that could take an installation.

    A whole disk with nothing of it mounted: the stick this booted from
    is excluded by that rule alone, since its data partition is in use.
    """
    out = []
    for drive in host_drives():
        if drive["type"] in ("cdrom", "fdd") or "/dev/loop" in drive["path"]:
            continue
        name = os.path.basename(drive["path"])
        # the system alone needs a good half gigabyte
        if sectors_of(name) < (2 << 30) // 512:
            continue
        if not os.path.isdir("/sys/class/block/%s" % name):
            continue
        if os.path.exists("/sys/class/block/%s/partition" % name):
            continue                       # a partition, not a disk
        parts = [p for p in host_drives()
                 if p["path"].startswith(drive["path"]) and
                 p["path"] != drive["path"]]
        busy = drive["busy"] or next((p["busy"] for p in parts if p["busy"]),
                                     "")
        out.append({"path": drive["path"], "size": drive["size"],
                    "model": drive["model"], "busy": busy,
                    "removable": drive["removable"]})
    return out


def install_to_disk(device, copy_data=False):
    """Put the whole appliance onto a disk, laid out like the stick.

    One system partition holding what booted this machine, and the rest
    of the disk as the data partition; a single OS on the disk, so the
    layout is the same every time and nothing has to be negotiated.
    """
    label = "install to %s" % device
    job = {"name": label, "kind": "install", "done": 0, "total": 7,
           "state": "running", "error": "", "message": "starting"}
    _jobs[label] = job

    target = next((d for d in install_targets() if d["path"] == device),
                  None)
    if target is None:
        job.update(state="failed", error="%s is not a disk here" % device)
    elif target["busy"]:
        job.update(state="failed",
                   error="%s is in use (%s)" % (device, target["busy"]))
    elif not os.path.isdir(os.path.join(MEDIUM, "live")):
        job.update(state="failed", error="the live medium is not readable")
    if job["state"] == "failed":
        say("%s refused: %s" % (label, job["error"]), "system")
        return job

    mount = "/run/mirai98-install"

    def step(text):
        job["done"] += 1
        job["message"] = text
        say("%s: %s" % (label, text), "system")

    def run(*args, **kw):
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=kw.get("timeout", 600))
        if out.returncode:
            raise OSError("%s: %s" % (args[0],
                                      (out.stderr or out.stdout).strip()))
        return out.stdout

    def work():
        try:
            megabytes = int(subprocess.run(
                ["du", "-sm", MEDIUM], capture_output=True, text=True,
                timeout=120).stdout.split()[0])
            system_end = megabytes + 96
            step("partitioning")
            run("wipefs", "-a", device)
            run("parted", "-s", device, "mklabel", "msdos",
                "mkpart", "primary", "fat32", "1MiB", "%dMiB" % system_end,
                "mkpart", "primary", "fat32", "%dMiB" % system_end, "100%",
                "set", "1", "boot", "on")
            run("partprobe", device)
            time.sleep(2)
            p1, p2 = device + "1", device + "2"
            if not os.path.exists(p1):        # nvme names them p1, p2
                p1, p2 = device + "p1", device + "p2"

            step("making file systems")
            run("mkfs.vfat", "-F", "32", "-n", BOOT_LABEL, p1)
            run("mkfs.vfat", "-F", "32", "-n", DATA_LABEL, p2)

            step("copying the system")
            os.makedirs(mount, exist_ok=True)
            run("mount", p1, mount)
            # -r, not -a: FAT holds no ownership or permissions to
            # preserve, and cp calls that a failure
            run("cp", "-r", MEDIUM + "/.", mount + "/",
                timeout=1800)

            step("installing the BIOS boot loader")
            run("grub-install", "--target=i386-pc",
                "--boot-directory=" + mount + "/boot", device)

            step("installing the UEFI boot loader")
            run("grub-install", "--target=x86_64-efi",
                "--efi-directory=" + mount,
                "--boot-directory=" + mount + "/boot",
                "--removable", "--no-nvram")
            run("umount", mount)

            if copy_data:
                step("copying the machines")
                run("mount", p2, mount)
                for source in (os.path.join(storage_root(), "pc98"),
                               os.path.join(storage_root(), "roms"),
                               settings_path()):
                    if os.path.exists(source):
                        run("cp", "-r", source, mount + "/",
                            timeout=3600)
                run("umount", mount)
            else:
                step("leaving the data partition empty")

            step("done")
            job["state"] = "done"
            job["message"] = "installed; the disk can boot on its own now"
        except Exception as err:
            subprocess.run(["umount", mount], capture_output=True)
            job.update(state="failed", error=str(err))
            say("%s failed: %s" % (label, err), "system")

    threading.Thread(target=work, daemon=True).start()
    return job


def drive_job(label, kind, name, device, to_drive):
    """Copy between an image and a real drive, block by block.

    Refuses anything the host has mounted, in either direction: writing
    would corrupt what the host is using, and reading it would produce a
    torn copy.
    """
    path = os.path.join(disks_root(kind), name)
    job = {"name": label, "kind": kind, "done": 0, "total": 0,
           "state": "running", "error": ""}
    drive = next((d for d in host_drives() if d["path"] == device), None)
    if drive is None:
        job.update(state="failed", error="%s is not there" % device)
    elif drive["busy"]:
        job.update(state="failed",
                   error="%s is mounted on %s" % (device, drive["busy"]))
    elif drive["system"]:
        job.update(state="failed", error="%s belongs to the host" % device)
    if job["state"] == "failed":
        _jobs[label] = job
        say("%s refused: %s" % (label, job["error"]), "disk")
        return job

    source, target = (path, device) if to_drive else (device, path)
    try:
        job["total"] = os.path.getsize(source) if to_drive \
            else int(subprocess.run(["blockdev", "--getsize64", device],
                                    capture_output=True, text=True,
                                    timeout=10).stdout or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        job["total"] = 0
    _jobs[label] = job

    def run():
        try:
            with open(source, "rb") as src, \
                    open(target, "r+b" if to_drive else "wb") as dst:
                while True:
                    block = src.read(4 << 20)
                    if not block:
                        break
                    dst.write(block)
                    job["done"] += len(block)
                dst.flush()
                os.fsync(dst.fileno())
            job["state"] = "done"
            say("%s finished (%d bytes)" % (label, job["done"]), "disk")
        except Exception as err:
            job.update(state="failed", error=str(err))
            say("%s failed: %s" % (label, err), "disk")

    threading.Thread(target=run, daemon=True).start()
    return job


_facts_cache = {}


def qemu_version():
    try:
        out = subprocess.run([CONFIG["qemu"], "--version"],
                             capture_output=True, text=True, timeout=10)
        return out.stdout.splitlines()[0].replace(
            "QEMU emulator version ", "")
    except (OSError, IndexError, subprocess.SubprocessError):
        return ""


def windows_facts():
    """What there is to say about a Windows host."""
    facts = {"cores": os.cpu_count(),
             "os": "%s %s" % (platform_name(), os_release()),
             "cpu_model": os.environ.get("PROCESSOR_IDENTIFIER", ""),
             "kernel": "", "boot": "", "kvm": False,
             "python": sys.version.split()[0],
             "root": CONFIG["root"],
             "qemu": qemu_version(),
             "roms": "real ROM set" if os.path.isdir(CONFIG["roms"]) and
                     os.listdir(CONFIG["roms"]) else "compatible ROMs"}
    try:
        facts["addresses"] = sorted(
            {a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None,
                                                 socket.AF_INET)})
    except OSError:
        facts["addresses"] = []
    return facts


def platform_name():
    import platform
    return platform.system()


def os_release():
    import platform
    return platform.release()


def host_facts():
    """The unchanging half: who this machine is and what it runs.

    Read once; none of it moves while the hypervisor is up.
    """
    if _facts_cache:
        return _facts_cache
    facts = {"hostname": socket.gethostname(), "platform": PLATFORM}
    if WINDOWS:
        facts.update(windows_facts())
        _facts_cache.update(facts)
        return _facts_cache
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    facts["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    facts["cores"] = os.cpu_count()
    try:
        facts["kernel"] = " ".join(os.uname()[:3][::2])
    except OSError:
        pass
    for path, key in (("/etc/os-release", "os"),):
        try:
            with open(path) as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        facts[key] = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    facts["kvm"] = os.path.exists("/dev/kvm")
    facts["boot"] = "UEFI" if os.path.isdir("/sys/firmware/efi") else "BIOS"
    for path in ("/opt/mirai98/version", os.path.join(HERE, "version")):
        try:
            with open(path) as f:
                facts["build"] = f.read().strip()
                break
        except OSError:
            continue
    try:
        out = subprocess.run([CONFIG["qemu"], "--version"],
                             capture_output=True, text=True, timeout=10)
        facts["qemu"] = out.stdout.splitlines()[0].replace(
            "QEMU emulator version ", "")
    except (OSError, IndexError, subprocess.SubprocessError):
        pass
    facts["roms"] = "real ROM set" if os.path.isdir(CONFIG["roms"]) and any(
        f.lower().endswith(".rom") or f.lower().startswith("bios")
        for f in os.listdir(CONFIG["roms"])) else "compatible ROMs"
    facts["python"] = sys.version.split()[0]
    facts["root"] = CONFIG["root"]
    try:
        addrs = subprocess.run(["hostname", "-I"], capture_output=True,
                               text=True, timeout=5).stdout.split()
        facts["addresses"] = addrs
    except (OSError, subprocess.SubprocessError):
        pass
    _facts_cache.update(facts)
    return _facts_cache


def qemu_argv(inst):
    vnc, ws, qmp_port = ports_of(inst)
    display = vnc - 5900
    accel = "kvm:tcg" if inst.get("accel", "kvm") == "kvm" else "tcg"
    argv = [CONFIG["qemu"],
            "-M", "%s,accel=%s" % (inst.get("machine") or "pc9821", accel),
            "-m", inst.get("memory") or "64M"]
    # QEMU searches -L paths in order, so putting the uploaded ROMs first
    # lets a partial real set fall through to the compatible ones
    if inst.get("bios") == "real":
        argv += ["-L", CONFIG["roms"]]
    # the boards play into a null backend; the VNC server captures that
    # mix and hands it to any client that asks, so the browser hears them
    fm, pcm = SOUNDS[sound_of(inst)]
    vnc = "0.0.0.0:%d,websocket=%d" % (display, ws)
    if fm or pcm:
        vnc += ",audiodev=snd"
    argv += ["-L", CONFIG["datadir"],
            "-display", "none",
            "-vnc", vnc,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    if fm or pcm:
        argv += ["-audiodev", "none,id=snd"]
        # the boards are ISA devices of their own, not part of the machine
        if fm:
            argv += ["-device", "pc98-opna,audiodev=snd"]
        if pcm:
            argv += ["-device", "pc98-wss,audiodev=snd"]
    if inst.get("snapshot"):
        argv.append("-snapshot")
    # a shared folder takes an IDE unit of its own, after the disks;
    # the BIOS only enumerates two, so a third would go unseen
    disks = [k for k in ("hdd1", "hdd2", "mount") if inst.get(k)][:2]
    for unit, key in enumerate(disks):
        if key == "mount":
            # exempt from snapshot mode: a shared folder exists to carry
            # files out again, and vvfat refuses rw on a snapshotted drive
            argv += ["-drive", "if=ide,bus=0,unit=%d,format=raw,"
                     "snapshot=off,file=fat98:rw:%s"
                     % (unit, os.path.expanduser(inst["mount"]))]
        else:
            argv += ["-drive", "if=ide,bus=0,unit=%d," % unit
                     + drive_backing(disk_path(inst, key))]
    if inst.get("cd"):
        argv += ["-drive", "if=ide,bus=1,unit=0,media=cdrom,readonly=on,"
                 + drive_backing(disk_path(inst, "cd"))]
    floppies = [k for k in ("fdd1", "fdd2") if inst.get(k)]
    for unit, key in enumerate(floppies):
        spec = "if=floppy,unit=%d," % unit if len(floppies) > 1 \
            else "if=floppy,"
        argv += ["-drive", spec + drive_backing(disk_path(inst, key))]
    for unit, key in enumerate(SCSI_KEYS):
        if inst.get(key):
            argv += ["-drive", "if=scsi,bus=0,unit=%d," % unit
                     + drive_backing(disk_path(inst, key))]
    if inst.get("net") == "nat":
        # an LGY-98 behind QEMU's own NAT; nothing asked of the host
        argv += ["-netdev", "user,id=lan",
                 "-device", "pc98-lgy98,netdev=lan"]
    elif inst.get("net") == "bridge":
        # straight onto the LAN through the host's bridge
        argv += ["-netdev", "bridge,id=lan,br=" + BRIDGE,
                 "-device", "pc98-lgy98,netdev=lan"]
    if inst.get("serial"):
        argv += ["-chardev", "serial,id=ser0,path=" + inst["serial"],
                 "-device", "serial98,chardev=ser0"]
    # the parallel port speaks through an FT245RL in bit-bang mode and
    # GP-IB through its own adapter; neither device exists in QEMU yet,
    # so the machine is described here and will start using them once
    # they land
    if inst.get("parallel"):
        argv += ["-chardev", "serial,id=par0,path=" + inst["parallel"],
                 "-device", "printer98,chardev=par0"]
    if inst.get("gpib"):
        argv += ["-chardev", "serial,id=gpib0,path=" + inst["gpib"],
                 "-device", "gpib98,chardev=gpib0"]
    if inst.get("extra"):
        argv += inst["extra"].split()
    return argv


def start_instance(inst):
    if is_running(inst):
        return "already running"
    # a drive the host is using cannot be handed to a guest as well:
    # both would write to it, and the guest would read a moving target
    trouble = busy_drives(inst)
    if trouble:
        result = "; ".join(trouble)
        say("vm %s not started: %s" % (inst["name"], result), "vm")
        return result
    argv = qemu_argv(inst)
    log_path = os.path.join(inst_dir(inst["index"]), "qemu.log")
    with open(log_path, "ab") as log:
        log.write(("\n--- %s\n%s\n" % (time.strftime("%F %T"),
                                       " ".join(argv))).encode())
        proc = subprocess.Popen(argv, stdout=log, stderr=log,
                                start_new_session=True)
    _procs[inst["name"]] = proc
    # give it a moment to either come up or die with a reason
    for _ in range(20):
        if proc.poll() is not None:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                tail = f.read().strip().splitlines()[-1:]
            result = "failed: %s" % (tail[0] if tail else "see log")
            say("vm %s: %s" % (inst["name"], result), "vm")
            return result
        if is_running(inst):
            say("vm %s started (%s, %s, pid %d)"
                % (inst["name"], inst.get("machine", "pc9821"),
                   inst.get("memory", "64M"), proc.pid), "vm")
            return "started"
        time.sleep(0.25)
    say("vm %s started but slow to answer QMP" % inst["name"], "vm")
    return "started (slow to answer QMP)"


def stop_instance(inst):
    if not is_running(inst):
        return "not running"
    qmp(inst, "quit")
    proc = _procs.pop(inst["name"], None)
    if proc:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    for _ in range(20):
        if not is_running(inst):
            say("vm %s stopped" % inst["name"], "vm")
            return "stopped"
        time.sleep(0.25)
    say("vm %s will not stop" % inst["name"], "vm")
    return "still answering; stop it by hand"


def media_devices(inst):
    """The drives a running machine will let you swap a disk in and out
    of, as QEMU names them."""
    reply = qmp(inst, "query-block")
    if not reply or "return" not in reply:
        return []
    out = []
    for block in reply["return"]:
        if not block.get("removable"):
            continue
        inserted = (block.get("inserted") or {}).get("file", "")
        # a snapshotted drive shows its temp overlay, the image is behind
        if '"filename": "' in inserted:
            inserted = inserted.split('"filename": "')[1].split('"')[0]
        name = block.get("device", "")
        out.append({
            "device": name,
            "kind": "fdd" if name.startswith("floppy") else "cdrom",
            "file": inserted,
        })
    return out


def change_media(inst, device, path):
    """Put a disk in a running machine's drive, or take one out."""
    known = {d["device"] for d in media_devices(inst)}
    if device not in known:
        return "no drive called %s" % device
    if not path:
        reply = qmp(inst, "eject", {"device": device, "force": True})
        text = "ejected"
    else:
        reply = qmp(inst, "blockdev-change-medium",
                    {"device": device, "filename": path, "format": "raw"})
        text = "loaded %s" % os.path.basename(path)
    if reply is None:
        return "the machine did not answer"
    if "error" in reply:
        return reply["error"].get("desc", "refused")
    say("vm %s: %s in %s" % (inst["name"], text, device), "vm")
    return text


SNAPSHOT_TAG = "mirai98"


def save_state(inst):
    """Freeze the machine into a snapshot inside its disk image.

    QEMU does this with savevm, which needs a qcow2 disk to write into;
    the PC-98 side of it is still being built, so whatever QEMU says
    comes straight back to the browser rather than being dressed up.
    """
    if not is_running(inst):
        return "not running"
    if inst.get("snapshot"):
        return ("snapshot mode throws the state away at shutdown; turn it "
                "off to save")
    if not any(disk_path(inst, k).lower().endswith(".qcow2")
               for k in DISK_KEYS if inst.get(k)):
        return "state needs somewhere to live: give the machine a qcow2 disk"
    reply = qmp(inst, "human-monitor-command",
                {"command-line": "savevm " + SNAPSHOT_TAG}, timeout=120)
    if reply is None:
        return "no answer from the machine"
    out = (reply.get("return") or "").strip()
    if out:
        say("vm %s: save failed: %s" % (inst["name"], out), "vm")
        return "save failed: " + out
    say("vm %s state saved" % inst["name"], "vm")
    return "state saved"


def resume_state(inst):
    """Start the machine and put the saved state back on top."""
    if not is_running(inst):
        result = start_instance(inst)
        if not result.startswith("started"):
            return result
    reply = qmp(inst, "human-monitor-command",
                {"command-line": "loadvm " + SNAPSHOT_TAG}, timeout=120)
    if reply is None:
        return "no answer from the machine"
    out = (reply.get("return") or "").strip()
    if out:
        say("vm %s: resume failed: %s" % (inst["name"], out), "vm")
        return "resume failed: " + out
    say("vm %s resumed from saved state" % inst["name"], "vm")
    return "resumed"


def thumbnail(inst):
    """Path of a reasonably fresh screen thumbnail, or None."""
    png = os.path.join(inst_dir(inst["index"]), "thumb.png")
    try:
        if time.time() - os.path.getmtime(png) < THUMB_AGE:
            return png
    except OSError:
        pass
    if not is_running(inst):
        return png if os.path.exists(png) else None
    ppm = png + ".ppm"
    reply = qmp(inst, "screendump", {"filename": ppm})
    if reply and "return" in reply:
        subprocess.run(["convert", ppm, "-resize", "320x", png],
                       capture_output=True)
        try:
            os.remove(ppm)
        except OSError:
            pass
    return png if os.path.exists(png) else None


# ------------------------------------------------------------- the page


# ------------------------------------------------------------- the page

LOGIN = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mirai98 Hypervisor Platform OS</title>
<style>
 body { font: 13px/1.5 system-ui, sans-serif; background: #1b1f24;
        color: #d6dde4; height: 100vh; margin: 0; display: flex;
        align-items: center; justify-content: center; }
 form { background: #232a31; border: 1px solid #38424c; border-radius: 4px;
        padding: 1.6em 1.8em; width: 22em; }
 .logo { font-size: 19px; font-weight: 700; }
 .logo span { color: #0095d9; }
 .sub { color: #8d99a5; margin: .2em 0 1.2em; }
 label { display: block; color: #8d99a5; margin-bottom: .3em; }
 input { width: 100%; padding: .45em .5em; border-radius: 3px;
         border: 1px solid #38424c; background: #1b1f24; color: #d6dde4;
         font: inherit; }
 button { margin-top: 1em; width: 100%; padding: .5em; border-radius: 3px;
          border: 1px solid #0095d9; background: #0095d9; color: #fff;
          font: inherit; cursor: pointer; }
 .msg { color: #e06c5f; min-height: 1.3em; margin-top: .6em; }
</style>
</head>
<body>
<form onsubmit="return signIn()">
 <div class="logo">Mirai<span>98</span></div>
 <div class="sub">Hypervisor Platform OS</div>
 <label for="pw" id="pw-label">Password</label>
 <input type="password" id="pw" autofocus>
 <button id="go">Sign in</button>
 <div class="msg" id="msg"></div>
</form>
<script>
try {
  if (localStorage.getItem('mirai98-lang') === 'ja') {
    document.documentElement.lang = 'ja';
    document.getElementById('pw-label').textContent = 'パスワード';
    document.getElementById('go').textContent = 'サインイン';
  }
} catch (err) {}
function signIn() {
  fetch('/api/login', {method: 'POST',
      body: JSON.stringify({password: document.getElementById('pw').value})})
    .then(async r => {
      if (r.ok) { location.reload(); return; }
      const d = await r.json().catch(() => ({}));
      document.getElementById('msg').textContent = d.error || 'refused';
    });
  return false;
}
</script>
</body>
</html>
"""

SETUP = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mirai98 Hypervisor Platform OS</title>
<style>
 body { font: 13px/1.55 system-ui, sans-serif; background: #1b1f24;
        color: #d6dde4; min-height: 100vh; margin: 0; display: flex;
        align-items: center; justify-content: center; }
 .box { background: #232a31; border: 1px solid #38424c; border-radius: 4px;
        padding: 1.6em 1.8em; width: 38em; max-width: 92vw; }
 .logo { font-size: 19px; font-weight: 700; }
 .logo span { color: #0095d9; }
 .sub { color: #8d99a5; margin: .2em 0 1.4em; }
 h2 { font-size: 15px; margin: 0 0 .6em; font-weight: 600; }
 .note { color: #8d99a5; }
 .pick { display: flex; gap: .6em; align-items: flex-start;
         border: 1px solid #38424c; border-radius: 3px; padding: .7em .8em;
         margin: .6em 0; cursor: pointer; }
 .pick.on { border-color: #0095d9; background: #1f2933; }
 .pick.big { justify-content: center; font-size: 15px; padding: 1em; }
 table { width: 100%; border-collapse: collapse; margin-top: .5em; }
 td, th { padding: .35em .5em; border-bottom: 1px solid #2f3841;
          text-align: left; }
 th { color: #8d99a5; font-weight: 400; }
 tr.sel { background: #1f2933; }
 input[type=password] { width: 100%; padding: .45em .5em; border-radius: 3px;
         border: 1px solid #38424c; background: #1b1f24; color: #d6dde4;
         font: inherit; }
 .bar { display: flex; gap: .6em; margin-top: 1.4em; align-items: center; }
 button { padding: .5em 1.1em; border-radius: 3px; border: 1px solid #38424c;
          background: #2b333b; color: #d6dde4; font: inherit;
          cursor: pointer; }
 button.primary { border-color: #0095d9; background: #0095d9; color: #fff; }
 .step { color: #8d99a5; margin-left: auto; }
 .msg { color: #e06c5f; min-height: 1.3em; margin-top: .6em; }
 .hide { display: none; }
</style>
</head>
<body>
<div class="box">
 <div class="logo">Mirai<span>98</span></div>
 <div class="sub">Hypervisor Platform OS</div>

 <div id="s0">
  <h2 id="t-language">Language</h2>
  <div class="pick big on" onclick="pickLang('en')">English</div>
  <div class="pick big" onclick="pickLang('ja')">&#26085;&#26412;&#35486;</div>
 </div>

 <div id="s1" class="hide">
  <h2 id="t-setup">Setup</h2>
  <div class="note" id="t-where">Where should the machines live?</div>
  <div class="pick on" id="pick-default" onclick="pickDefault()">
   <input type="radio" name="where" checked id="radio-default">
   <div id="t-default">Use free space on this USB media (default)</div>
  </div>
  <div class="pick" id="pick-other" onclick="pickOther()">
   <input type="radio" name="where" id="radio-other">
   <div><span id="t-other">Use existing partition and make a folder.</span>
    <div id="choices" class="hide"></div></div>
  </div>
  <div class="bar"><button class="primary" onclick="toStep(2)"
   id="t-continue">Continue</button>
   <span class="step" id="t-step1">Step 1 of 2</span></div>
 </div>

 <div id="s2" class="hide">
  <h2 id="t-password">Password</h2>
  <div class="note" id="pw-note"></div>
  <div style="margin:1em 0">
   <input type="password" id="pw" placeholder="password" autofocus>
  </div>
  <div class="note" id="t-blank">Leave it empty to start without one.</div>
  <div class="bar"><button onclick="toStep(1)" id="t-back">Back</button>
   <button class="primary" onclick="finish()" id="t-finish">Finish</button>
   <span class="step" id="t-step2">Step 2 of 2</span></div>
  <div class="msg" id="msg"></div>
 </div>
</div>
<script>
const JA = {
  't-language': '言語',
  't-setup': 'セットアップ',
  't-where': '仮想マシンをどこに置きますか。',
  't-default': 'このUSBメディアの空き領域を使う（推奨）',
  't-other': '既存のパーティションにフォルダを作る',
  't-continue': '次へ',
  't-step1': '1 / 2',
  't-password': 'パスワード',
  't-blank': '空欄のまま進めてもかまいません。',
  't-back': '戻る',
  't-finish': '完了',
  't-step2': '2 / 2',
};
const JA_TEXT = {
  pwWin: 'この管理画面を守ります。',
  pwUnix: 'root のパスワードになり、管理画面とシェルの両方で聞かれます。',
  winDefault: 'このプログラムを起動した場所を使う（推奨）',
  otherDrive: 'ドライブにフォルダを作る',
  grown: 'このメディアの空き領域 %s を使えるようにしました。',
  none: '他のドライブは見つかりません。',
  pickOne: 'ドライブを選んでください。',
  noPassword: 'パスワードを設定せずに進めますか。',
  working: '設定中...',
  drive: 'ドライブ', type: '種別', size: '容量',
};
const EN_TEXT = {
  pwWin: 'It guards this console.',
  pwUnix: 'It becomes the root password: this console and the shell both ' +
          'ask for it.',
  winDefault: 'Use where this program was started (default)',
  otherDrive: 'Use another drive and make a folder.',
  grown: 'This medium now has %s of free space to use.',
  none: 'No other drive was found.',
  pickOne: 'Pick a drive.',
  noPassword: 'Start without a password?',
  working: 'setting up...',
  drive: 'Drive', type: 'Type', size: 'Size',
};

let info = null, chosen = '', lang = 'en', T = EN_TEXT;
const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const fmt = n => !n ? '-' : n >= (1 << 30)
  ? (n / (1 << 30)).toFixed(1) + ' GB' : Math.round(n / (1 << 20)) + ' MB';

function pickLang(which) {
  lang = which;
  T = which === 'ja' ? JA_TEXT : EN_TEXT;
  try { localStorage.setItem('mirai98-lang', which); } catch (err) {}
  if (which === 'ja')
    for (const id in JA) document.getElementById(id).textContent = JA[id];
  document.documentElement.lang = which;
  draw();
  document.getElementById('s0').classList.add('hide');
  document.getElementById('s1').classList.remove('hide');
}

fetch('/api/setup').then(r => r.json()).then(d => { info = d; draw(); });

function draw() {
  if (!info) return;
  const d = info;
  if (d.platform === 'windows') {
    document.getElementById('t-default').textContent = T.winDefault;
    document.getElementById('t-other').textContent = T.otherDrive;
  }
  document.getElementById('pw-note').textContent =
    d.platform === 'windows' ? T.pwWin : T.pwUnix;
  const where = document.getElementById('t-default');
  where.title = d.default.path;
  if (d.grown) {
    let box = document.getElementById('grown-note');
    if (!box) {
      box = document.createElement('div');
      box.id = 'grown-note';
      box.className = 'note';
      box.style.margin = '.2em 0 0';
      where.parentNode.appendChild(box);
    }
    box.textContent = T.grown.replace('%s', d.grown);
  }
  document.getElementById('choices').innerHTML = d.choices.length
    ? '<table><tr><th>' + esc(T.drive) + '</th><th>' + esc(T.type) +
      '</th><th>' + esc(T.size) + '</th></tr>' +
      d.choices.map((c, n) => '<tr id="row-' + n + '" ' +
        'onclick="pickDrive(' + n + ')"><td>' + esc(c.path) +
        (c.label ? ' <span class="note">' + esc(c.label) + '</span>' : '') +
        '</td><td>' + esc(c.fstype || '?') + '</td><td>' + fmt(c.size) +
        '</td></tr>').join('') + '</table>'
    : '<div class="note">' + esc(T.none) + '</div>';
}
function pickDefault() {
  chosen = '';
  document.getElementById('radio-default').checked = true;
  document.getElementById('pick-default').classList.add('on');
  document.getElementById('pick-other').classList.remove('on');
  document.getElementById('choices').classList.add('hide');
}
function pickOther() {
  document.getElementById('radio-other').checked = true;
  document.getElementById('pick-other').classList.add('on');
  document.getElementById('pick-default').classList.remove('on');
  document.getElementById('choices').classList.remove('hide');
}
function pickDrive(n) {
  chosen = info.choices[n].id;
  pickOther();
  for (const row of document.querySelectorAll('#choices tr'))
    row.classList.remove('sel');
  const row = document.getElementById('row-' + n);
  if (row) row.classList.add('sel');
}
function toStep(n) {
  if (n === 2 && document.getElementById('radio-other').checked && !chosen) {
    alert(T.pickOne);
    return;
  }
  document.getElementById('s1').classList.toggle('hide', n !== 1);
  document.getElementById('s2').classList.toggle('hide', n !== 2);
  if (n === 2) document.getElementById('pw').focus();
}
function finish() {
  const password = document.getElementById('pw').value;
  if (!password && !confirm(T.noPassword)) return;
  document.getElementById('msg').textContent = T.working;
  fetch('/api/setup', {method: 'POST',
      body: JSON.stringify({storage: chosen, password, lang})})
    .then(async r => {
      const d = await r.json().catch(() => ({}));
      if (r.ok) { location.href = '/'; return; }
      document.getElementById('msg').textContent = d.error || 'refused';
    });
}
</script>
</body>
</html>
"""

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Mirai98 Hypervisor Platform OS</title>
<style>
 :root {
   --bg: #1b1f24; --panel: #232a31; --panel2: #2b333b; --line: #38424c;
   --ink: #d6dde4; --dim: #8d99a5; --accent: #0095d9; --link: #4db8f0;
   --ok: #6cc04a; --off: #7d858c; --cpu: #0095d9; --mem: #00b39f;
 }
 * { box-sizing: border-box; }
 html, body { height: 100%; }
 body { margin: 0; font: 13px/1.45 system-ui, sans-serif;
        background: var(--bg); color: var(--ink);
        display: grid; grid-template-rows: 42px 1fr 150px;
        grid-template-columns: 15em 1fr;
        grid-template-areas: "head head" "tree main" "tasks tasks"; }
 a { color: var(--link); text-decoration: none; }

 header { grid-area: head; background: #10151a; display: flex;
          align-items: center; gap: 1em; padding: 0 1em;
          border-bottom: 1px solid var(--line); }
 header .logo { font-weight: 700; letter-spacing: .5px; font-size: 15px; }
 header .logo span { color: var(--accent); }
 header .sub { color: var(--dim); }
 header .spacer { flex: 1; }
 header .msg { color: #f0c674; }

 nav { grid-area: tree; background: var(--panel); overflow: auto;
       border-right: 1px solid var(--line); padding: 0 0 .6em; }
 nav .group { color: var(--dim); font-size: 11px; text-transform: uppercase;
              letter-spacing: .06em; padding: .9em 1em .25em; }
 nav .navhead { padding: .5em 1em; font-weight: 600; font-size: 12px;
                letter-spacing: .04em; color: var(--dim);
                background: var(--panel2);
                border-bottom: 1px solid var(--line); }
 nav a { display: block; color: var(--ink); padding: .3em 1em .3em 1.4em;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
 nav a:hover { background: var(--panel2); }
 nav a.on { background: #14324a; color: #fff;
            box-shadow: inset 3px 0 0 var(--accent); }
 nav a .count { color: var(--dim); font-size: 11px; }

 /* the toolbar row VMware puts under every title */
 .actions { display: flex; gap: .4em; flex-wrap: wrap;
            padding: .5em 0 .8em; border-bottom: 1px solid var(--line);
            margin-bottom: 1em; }
 .crumb { color: var(--dim); font-size: 12px; margin-bottom: .3em; }
 .crumb a { color: var(--link); }
 .titlerow { display: flex; align-items: center; gap: .6em; }
 .titlerow h2 { margin: 0; font-size: 18px; font-weight: 500; }
 .titlerow .glyph { font-size: 20px; color: var(--accent); }
 /* resource consumption bars, the ESXi summary panel */
 .meterlab { display: flex; justify-content: space-between;
             font-size: 12px; color: var(--dim); }
 .track { height: 14px; background: #171c21; border: 1px solid var(--line);
          margin: .15em 0 .8em; }
 .track > div { height: 100%; background: linear-gradient(#3fb0e8,
                #0d7fb8); }
 nav a .dot { display: inline-block; width: .55em; height: .55em;
              border-radius: 50%; margin-right: .5em;
              background: var(--off); }
 nav a.run .dot { background: var(--ok); }
 nav .node { padding-left: 1em; font-weight: 600; }

 main { grid-area: main; overflow: auto; padding: 1em 1.2em; }
 footer { grid-area: tasks; background: var(--panel); overflow: auto;
          border-top: 1px solid var(--line); }
 footer .bar { padding: .4em 1em; color: var(--dim);
               border-bottom: 1px solid var(--line);
               position: sticky; top: 0; background: var(--panel); }

 .card { background: var(--panel); border: 1px solid var(--line);
         border-radius: 3px; margin-bottom: 1em; }
 .card > h3 { margin: 0; padding: .55em .9em; font-size: 13px;
              font-weight: 600; border-bottom: 1px solid var(--line);
              background: var(--panel2); }
 .card > .body { padding: .9em; }
 .card h4 { margin: 0; padding: .4em .9em; font-size: 12px;
            font-weight: 600; color: var(--dim);
            background: #22262a; border-bottom: 1px solid var(--line);
            border-top: 1px solid var(--line); }
 .card h4:first-child { border-top: none; }
 table { border-collapse: collapse; width: 100%; }
 th, td { padding: .45em .7em; text-align: left; vertical-align: middle;
          border-bottom: 1px solid var(--line); }
 th { color: var(--dim); font-weight: 600; font-size: 11px;
      text-transform: uppercase; letter-spacing: .04em; }
 tr:last-child td { border-bottom: none; }

 button { font: inherit; padding: .28em .8em; border-radius: 3px;
          border: 1px solid var(--line); background: var(--panel2);
          color: var(--ink); cursor: pointer; }
 button:hover:not(:disabled) { background: #3a4046; }
 button.primary { background: var(--accent); border-color: var(--accent);
                  color: #fff; }
 button.primary:hover:not(:disabled) { background: #f08000; }
 button:disabled { opacity: .4; cursor: default; }
 input[type=text], input[type=number], select {
   font: inherit; padding: .25em .4em; border-radius: 3px;
   border: 1px solid var(--line); background: #1b1e21; color: var(--ink); }
 input[type=text], select { min-width: 14em; }
 input[type=range] { vertical-align: middle; width: 14em; }
 label.check { user-select: none; }

 .state { display: inline-block; padding: 0 .55em; border-radius: 9px;
          font-size: 11px; border: 1px solid var(--line);
          color: var(--dim); }
 .state.on { border-color: #3f7a2c; background: #23331c; color: #9ede7a; }
 img.thumb, .thumb-empty { width: 116px; height: 73px; display: block;
   background: #000; border: 1px solid var(--line); }
 img.thumb { object-fit: contain; }
 .thumb-empty { display: flex; align-items: center; justify-content: center;
                color: #555; font-size: 11px; }
 .topbar { display: flex; align-items: center; gap: .8em;
           margin-bottom: .8em; }
 .topbar h2 { margin: 0; font-size: 17px; font-weight: 600; }
 .grid2 { display: grid; grid-template-columns: 25em 1fr; gap: 1em;
          align-items: start; }
 dl { display: grid; grid-template-columns: 8.5em 1fr; margin: 0;
      row-gap: .3em; }
 dt { color: var(--dim); } dd { margin: 0; }
 .row { display: flex; align-items: center; gap: .6em; margin: .35em 0; }
 .row > label:first-child { width: 9em; color: var(--dim); flex: none; }
 .bar-btns { display: flex; gap: .5em; flex-wrap: wrap; }

 /* graphs, cockpit style: labels outside, plot stretches */
 .graphs { display: grid; grid-template-columns: repeat(auto-fit,
           minmax(20em, 1fr)); gap: 1.2em; }
 .graph .cap { color: var(--dim); margin-bottom: .2em; }
 .graph .cap b { color: var(--ink); float: right; font-weight: 600; }
 .graph .plotrow { display: flex; gap: .4em; }
 .graph .ylab { display: flex; flex-direction: column;
                justify-content: space-between; font-size: 10px;
                color: var(--dim); height: 110px; text-align: right;
                width: 3.2em; flex: none; }
 .graph .plot { flex: 1; height: 110px; background: #15181a;
                border: 1px solid var(--line); }
 .graph svg { display: block; width: 100%; height: 100%; }
 .graph .xlab { display: flex; justify-content: space-between;
                font-size: 10px; color: var(--dim); margin: .15em 0 0 3.6em; }

 #console-box { background: #000; height: 480px; position: relative;
                resize: vertical; overflow: hidden; min-height: 240px; }
 .hint { color: #666; position: absolute; top: 45%; width: 100%;
         text-align: center; }

 /* the create dialog */
 .veil { position: fixed; inset: 0; background: #000a; display: none;
         align-items: center; justify-content: center; z-index: 20; }
 .veil.open { display: flex; }
 .dialog { width: 46em; max-width: 95vw; background: var(--panel);
           border: 1px solid var(--line); border-radius: 4px;
           box-shadow: 0 10px 40px #000a; }
 .dialog .title { padding: .6em .9em; border-bottom: 1px solid var(--line);
                  display: flex; align-items: center;
                  background: var(--panel2); }
 .dialog .title b { color: var(--link); font-weight: 600; }
 .dialog .title .x { margin-left: auto; cursor: pointer;
                     color: var(--dim); }
 .tabs { display: flex; gap: .3em; padding: .5em .9em 0;
         border-bottom: 1px solid var(--line); }
 .tabs span { padding: .35em .9em; border: 1px solid transparent;
              border-bottom: none; border-radius: 3px 3px 0 0;
              color: var(--dim); cursor: pointer; }
 .tabs span.on { background: var(--panel2); border-color: var(--line);
                 color: #fff; margin-bottom: -1px; }
 .pane { display: none; padding: 1em; min-height: 17em; }
 .pane.on { display: block; }
 .dialog .foot { padding: .6em .9em; border-top: 1px solid var(--line);
                 display: flex; gap: .5em; align-items: center; }
 .dialog .foot .spacer { flex: 1; }
 .note { color: var(--dim); font-size: 12px; }
</style>
</head>
<body>
<header>
 <div class="logo">Mirai<span>98</span></div>
 <div class="sub">Hypervisor Platform OS</div>
 <div class="msg" id="msg"></div>
 <div class="spacer"></div>
 <button class="primary" onclick="openCreate()">Create VM</button>
 <button onclick="location.hash='#/shell'" id="btn-shell">Shell</button>
 <select id="lang-pick" onchange="setLang(this.value)" title="Language">
  <option value="en">English</option>
  <option value="ja">日本語</option>
 </select>
 <div class="sub" id="whereami"></div>
</header>
<nav id="tree"></nav>
<main><div id="view"></div><div id="datalists"></div></main>
<footer>
 <div class="bar">Tasks</div>
 <table id="tasks"></table>
</footer>

<div class="veil" id="tree-veil"
     onclick="if(event.target===this)closeTree()">
 <div class="dialog" style="width:40em">
  <div class="title"><b id="tree-name"></b>
   <span class="x" onclick="closeTree()">&#10005;</span></div>
  <div style="padding:1em;max-height:60vh;overflow:auto;
              font-family:ui-monospace,monospace;font-size:12px"
       id="tree-body"></div>
  <div class="foot"><span class="spacer"></span>
   <button onclick="closeTree()">Close</button></div>
 </div>
</div>

<div class="veil" id="upload-veil">
 <div class="dialog" style="width:34em">
  <div class="title"><b>Upload</b></div>
  <div style="padding:1em">
   <div id="upload-name" style="overflow-wrap:anywhere"></div>
   <progress id="upload-bar" max="100" value="0"
             style="width:100%;height:1.2em;margin:.8em 0"></progress>
   <div class="note" id="upload-stat"></div>
  </div>
  <div class="foot"><span class="spacer"></span>
   <button id="upload-close" onclick="closeUpload()">Cancel</button></div>
 </div>
</div>

<div class="veil" id="veil" onclick="if(event.target===this)closeCreate()">
 <div class="dialog">
  <div class="title"><b>Create: Virtual Machine</b>
   <span class="x" onclick="closeCreate()">&#10005;</span></div>
  <div class="tabs" id="tabs"></div>
  <form id="wizard" onsubmit="return submitCreate()">
   <div class="pane on" data-pane="General">
    <!-- validated in submitCreate, not by the browser: a required field
         on a hidden tab blocks submit with nothing on screen to fix -->
    <div class="row"><label>Name</label>
     <input type="text" name="name" placeholder="dos620"></div>
    <div class="row"><label>Machine type</label>
     <select name="machine">
      <option value="pc9821" selected>PC-9821 (386 and later)</option>
      <option value="pc9801">PC-9801 (the older line)</option>
     </select></div>
    <div class="row"><label>BIOS</label>
     <select name="bios">
      <option value="compat" selected>Compatible (ships with Mirai98)
      </option>
      <option value="real">Real machine ROMs (uploaded in Storage)</option>
     </select></div>
    <div class="row"><label>Snapshot</label>
     <label class="check"><input type="checkbox" name="snapshot" checked>
      discard disk changes when the machine stops</label></div>
   </div>
   <div class="pane" data-pane="Disks" id="pane-disks"></div>
   <div class="pane" data-pane="Host">
    <div class="row"><label>Shared folder</label>
     <input type="text" name="mount" placeholder="/data/share"></div>
    <div class="note">Mounted as an IDE disk (fat98). Uses one of the
     two units the disk BIOS sees.</div>
    <div id="pane-ports" style="margin-top:1em"></div>
    <div class="note">Host drives: see the Disks tab. Mounted drives
     are refused. Parallel and GP-IB: not yet in QEMU.</div>
   </div>
   <div class="pane" data-pane="Memory">
    <div class="row"><label>Memory</label>
     <input type="range" id="mem-range" min="0" oninput="memSlide(this)">
     <input type="text" name="memory" id="mem-text" value="64M"
            style="min-width:6em" oninput="memType(this)"></div>
    <div class="note">640K: as shipped. DOS games: 4M to 16M.
     Windows 95: more.</div>
   </div>
   <div class="pane" data-pane="Sound">
    <div class="row"><label>Sound board</label>
     <select name="sound">
      <option value="86" selected>PC-9801-86</option>
      <option value="wss">WSS</option>
      <option value="none">None</option>
     </select></div>
    <div class="note">PC-9801-86: FM and PCM. WSS: the Mate-X built-in.
     The console plays whichever one is fitted.</div>
   </div>
   <div class="pane" data-pane="Network">
    <div class="row"><label>Network</label>
     <select name="net"><option value="">None</option>
      <option value="nat">NAT</option>
      <option value="bridge">Bridge</option></select></div>
    <div class="note">LGY-98 board. NAT: outbound only.
     Bridge: own address on the LAN.</div>
   </div>
   <div class="pane" data-pane="Options">
    <div class="row"><label>Acceleration</label>
     <label class="check"><input type="checkbox" name="kvm" checked>
      use KVM when the host has it, TCG otherwise</label></div>
    <div class="row"><label>Extra QEMU args</label>
     <input type="text" name="extra" style="min-width:24em"></div>
    <div class="note">Appended to the command line as typed.</div>
   </div>
   <div class="pane" data-pane="Confirm">
    <div id="confirm-table"></div>
   </div>
   <div class="foot">
    <span class="note" id="wizard-note"></span>
    <span class="spacer"></span>
    <button type="button" onclick="closeCreate()">Cancel</button>
    <button type="button" id="btn-back" onclick="tabStep(-1)">Back</button>
    <button type="button" id="btn-next" class="primary"
            onclick="tabStep(1)">Next</button>
    <button type="submit" id="btn-finish" class="primary"
            style="display:none">Finish</button>
   </div>
  </form>
 </div>
</div>

<script type="module">
// loaded on demand: without noVNC beside the program the consoles are
// dead, but everything else still works
let RFB = null;
const loadRFB = async () => RFB ||
  (RFB = (await import('/novnc/core/rfb.js')).default);

const MEMS = ["640K","2M","4M","8M","16M","32M","64M","128M","256M","512M",
              "1G","2G","4G","8G","16G","32G"];
// the boards are named after the chips, so they keep their capitals
// the boards are named after the machines they came in
const SOUND_LABEL = {"86": "PC-9801-86", "wss": "WSS", "none": "None"};
const SOUND_ALIAS = {"opna+wss": "86", "opna": "86"};
const soundKey = v => SOUND_ALIAS[v] || (SOUND_LABEL[v] ? v : '86');
const soundName = v => SOUND_LABEL[soundKey(v)];
const DISK_ROWS = [["hdd1","IDE HDD 1","hdd"], ["hdd2","IDE HDD 2","hdd"],
                   ["cd","IDE CD-ROM","cdrom"], ["fdd1","FDD 1","fdd"],
                   ["fdd2","FDD 2","fdd"], ["scsi1","SCSI 1","hdd"],
                   ["scsi2","SCSI 2","hdd"], ["scsi3","SCSI 3","hdd"],
                   ["scsi4","SCSI 4","hdd"]];
const TABS = ["General","Disks","Host","Memory","Sound","Network",
              "Options","Confirm"];
const view = document.getElementById('view');
let rfb = null, consoleWatch = null, catalog = {hdd:[], fdd:[], cdrom:[]};
let instances = [], hostFacts = {}, tab = 0;
let hardware = {drives: [], serial: []}, autoConnect = '', facts = {};

window.toast = t => {
  document.getElementById('msg').textContent = t || '';
  if (t) setTimeout(() => {
    if (document.getElementById('msg').textContent === t) toast('');
  }, 5000);
};
// ------------------------------------------------------- the language
// The page is written in English and translated on the way out: every
// screen is built by the same string concatenation it always was, and a
// MutationObserver swaps whole labels for their Japanese counterparts as
// they land in the document.  A phrase with no entry stays English.
const JA = {
  'Host Server': 'ホストサーバー', 'Storage': 'ストレージ',
  'Networking': 'ネットワーク', 'Logging': 'ログ',
  'System Settings': 'システム設定', 'Shell': 'シェル',
  'Navigator': 'ナビゲーター', 'Virtual machines': '仮想マシン',
  'All machines': 'すべての仮想マシン', 'Create VM': '仮想マシンの作成',
  'Host': 'ホスト', 'Tasks': 'タスク', 'connected': '接続中',
  'running': '実行中', 'stopped': '停止', 'Refresh': '更新',
  'Console': 'コンソール', 'Power on': '起動', 'Shut down': 'シャットダウン',
  'Suspend': 'サスペンド', 'Restart': 'リセット', 'Edit': '編集',
  'Delete': '削除', 'Save': '保存', 'Cancel': '中止', 'Close': '閉じる',
  'Back': '戻る', 'Next': '次へ', 'Finish': '完了', 'Set': '設定',
  'Create': '作成', 'Upload...': 'アップロード...', 'Download': 'ダウンロード',
  'Resume': '再開', 'Connect': '接続', 'Disconnect': '切断',
  'Configuration': '構成', 'Resource consumption': 'リソース使用状況',
  'Performance': 'パフォーマンス', 'General information': '基本情報',
  'Hardware configuration': 'ハードウェア構成', 'Media': 'メディア',
  'CPU(s)': 'CPU', 'Kernel version': 'カーネル',
  'Operating system': 'オペレーティングシステム', 'Boot mode': 'ブートモード',
  'Hardware virtualisation': 'ハードウェア仮想化', 'QEMU': 'QEMU',
  'PC-98 BIOS': 'PC-98 BIOS', 'Storage root': 'ストレージのルート',
  'Python': 'Python', 'Build': 'ビルド', 'Addresses': 'アドレス',
  'Memory': 'メモリ', 'Machine type': 'マシン型', 'BIOS': 'BIOS',
  'Acceleration': 'アクセラレーション', 'Disks': 'ディスク',
  'Sound': 'サウンド', 'Display': '画面', 'Network': 'ネットワーク',
  'Networking': 'ネットワーク', 'Shared folder': '共有フォルダ',
  'Serial port': 'シリアルポート', 'Parallel port': 'パラレルポート',
  'GP-IB': 'GP-IB', 'Home': '保存場所', 'none': 'なし', 'None': 'なし',
  'compatible': '互換ROM', 'of one guest CPU': 'ゲストCPU 1個あたり',
  'Images': 'イメージ', 'Add an image': 'イメージの追加',
  'Create a disk': 'ディスクの作成', 'Create a floppy': 'フロッピーの作成',
  'Upload from this computer...': 'このPCからアップロード...',
  'Download from URL...': 'URLからダウンロード...',
  'Import a server path...': 'サーバー上のパスから取り込み...',
  'Read a host drive...': '実機ドライブから読み取り...',
  'Write to a drive...': '実機ドライブへ書き込み...',
  'Contents': '中身', 'Copy': '複製', 'NAME': '名前', 'SIZE': 'サイズ',
  'MODIFIED': '更新日時', 'USED BY': '使用中の仮想マシン',
  'DEVICE': 'デバイス', 'TYPE': '種別', 'LABEL': 'ラベル', 'STATE': '状態',
  'NOTE': '備考', 'MODEL': 'モデル', 'DRIVE': 'ドライブ', 'FILE': 'ファイル',
  'Device': 'デバイス', 'Type': '種別', 'Label': 'ラベル', 'Size': 'サイズ',
  'State': '状態', 'Note': '備考', 'Model': 'モデル', 'Drive': 'ドライブ',
  'File': 'ファイル', 'Name': '名前', 'Filesystems': 'ファイルシステム',
  'Drives': 'ドライブ', 'Data storage': 'データの保存先',
  'In use': '使用中', 'Settings': '設定ファイル',
  'Use for data': 'データ用に使う', 'in use for data': 'データ用に使用中',
  'Password': 'パスワード', 'Current': '現在のパスワード',
  'New password': '新しいパスワード',
  'Remove the password': 'パスワードを解除',
  'Persistent System Image': '永続システムイメージ',
  'Image': 'イメージ', 'This boot': 'この起動',
  'Create the image': 'イメージを作成', 'Remove the image': 'イメージを削除',
  'Update the system': 'システムを更新', 'Update': '更新',
  'Install to a Disk': 'ディスクへインストール',
  'Install here': 'ここへインストール', 'free': '空き',
  'System log': 'システムログ', 'all': 'すべて', 'system': 'システム',
  'vm': '仮想マシン', 'disk': 'ディスク', 'network': 'ネットワーク',
  'web': 'ウェブ', 'lines': '行', 'Interfaces': 'インタフェース',
  'Address': 'アドレス', 'Mode': 'モード', 'Gateway': 'ゲートウェイ',
  'Name servers': 'ネームサーバー', 'LAN bridge': 'LANブリッジ',
  'Save and apply': '保存して適用', 'Open in a tab': '別タブで開く',
  'the hypervisor host, not a guest': 'ゲストではなくホスト側です',
  'the hypervisor’s own address': 'このホスト自身のアドレス',
  'General': '全般', 'Options': 'オプション', 'Confirm': '確認',
  'the machine is not running': '仮想マシンが動いていません',
};
// long notes, keyed by the sentence they replace
JA['Machines live at the root of the chosen filesystem. Only the ' +
   'choice is kept on the boot medium. ext4 and exFAT: no limits. ' +
   'NTFS: needs a full Windows shutdown. FAT32: no file over 4 GB.'] =
  '選んだ場所に仮想マシンを置きます。ブートメディアには選択だけを保存' +
  'します。ext4とexFATは制限なし。NTFSはWindowsを完全に終了してから。' +
  'FAT32は4GBを超えるファイルを置けません。';
JA['One disk, one system. The disk is wiped and laid out like the ' +
   'stick: a system partition and the rest as data. BIOS and UEFI are ' +
   'both installed.'] =
  'ディスク全体をこのシステムだけで使います。USBメディアと同じ構成' +
  '（システム用と残り全部のデータ用）に作り直し、BIOSとUEFIの両方を' +
  'インストールします。中身はすべて消えます。';
JA['Keeps everything apt installs. The boot menu always has a second ' +
   'entry that ignores it. A new kernel is copied onto the boot medium ' +
   'afterwards.'] =
  'aptで入れたものが再起動後も残ります。ブートメニューには常にこれを' +
  '無視する項目もあります。更新後は新しいカーネルをブートメディアへ' +
  'コピーします。';
JA['Cleared at boot. Trimmed to 1000 lines at 1 MB.'] =
  '起動時に消去し、1MBを超えたら末尾1000行に切り詰めます。';
JA['Not set. Anyone on this network can drive this host.'] =
  '未設定です。このネットワークの誰でもこのホストを操作できます。';
JA['Manage disks in Storage.'] = 'ディスクはストレージ画面で管理します。';
JA['Set. The web console, ssh and the shell all ask for it.'] =
  '設定済みです。管理画面・ssh・シェルのすべてで聞かれます。';
JA['Set. This console asks for it.'] = '設定済みです。この管理画面で聞かれます。';
JA['This is root\'s own password, so the shell asks for it too. A live ' +
   'system forgets it at every boot, so its hash is kept on the boot ' +
   'medium and put back at start-up. Changing it from a shell leaves ' +
   'this page out of step.'] =
  'root 自身のパスワードなので、シェルでも同じものを聞かれます。ライブ' +
  '起動では毎回忘れられるため、ハッシュをブートメディアに保存して起動時' +
  'に復元します。シェルから変更するとこの画面とずれます。';
JA['none: system changes stay in RAM'] =
  'なし: システムへの変更はRAMに残るだけです';
JA['RAM overlay'] = 'RAMオーバーレイ';
JA['using the image'] = 'イメージを使用中';
JA['KVM available'] = 'KVM 利用可能';
JA['not available (TCG only)'] = '利用不可（TCGのみ）';
JA['compatible ROMs'] = '互換ROM';
JA['real ROM set'] = '実機ROM';
JA['the boot medium'] = 'ブートメディア';
JA['where this program was started'] = 'このプログラムを起動した場所';

JA['Modified'] = '更新日時';
JA['Used by'] = '使用中';
JA['(empty)'] = '(なし)';
JA['to hdi'] = 'hdiへ';
JA['to qcow2'] = 'qcow2へ';
JA['to raw'] = 'rawへ';
JA['to fdi'] = 'fdiへ';
JA['.qcow2 grows on demand. .hdi: Anex86. .raw: flat.'] =
  '.qcow2 は必要に応じて大きくなります。.hdi は Anex86 形式、.raw はベタ。';
JA['.fdi or .raw. Formatted, empty.'] =
  '.fdi または .raw。フォーマット済みの空ディスクです。';
JA['not uploaded, the compatible ROM stands in'] =
  '未アップロード（互換ROMで代用）';
JA['A machine set to the real BIOS uses these and falls back to the ' +
   'compatible ROMs. N88 BASIC needs pc98basic.bin. No compatible ' +
   'version yet.'] =
  '実機BIOSに設定した仮想マシンがこれらを使い、足りない分は互換ROMで' +
  '補います。N88 BASIC には pc98basic.bin が必要です（互換版はまだ' +
  'ありません）。';

JA['powered off'] = '停止中';
JA['🔇 Sound off'] = '🔇 サウンドOFF';
JA['🔊 Sound on'] = '🔊 サウンドON';
JA['not running'] = '停止中';
JA['Machine'] = 'マシン';
JA['MEMORY'] = 'メモリ';
JA['PROCESS'] = 'プロセス';
JA['Create Virtual Machine'] = '仮想マシンの作成';
JA['Machine type and name'] = 'マシン型と名前';
JA['Discard disk changes when the machine stops'] =
  '停止時にディスクへの変更を捨てる';
JA['Sound board'] = 'サウンドボード';
JA['PC-9801-86: FM and PCM. WSS: the Mate-X built-in. The console ' +
   'plays whichever one is fitted.'] =
  'PC-9801-86 は FM と PCM。WSS は Mate-X 内蔵音源。装着した方の音を' +
  'コンソールで再生します。';
JA['Real machine BIOS'] = '実機BIOS';
JA['Compatible (ships with Mirai98)'] = '互換ROM（Mirai98 同梱）';
JA['Real ROM set (upload in Storage)'] = '実機ROM（ストレージでアップロード）';

// phrases built around a value: matched whole, rewritten with the value
const JA_RE = [
  [/^(.+) free of (.+)$/, '$2 中 $1 空き'],
  [/^(\S+) \(KVM, else TCG\)$/, '$1（KVM、無ければTCG）'],
  [/^Real machine ROMs — (.+)$/, '実機ROM — $1'],
  [/^uptime (.+)$/, '稼働時間 $1'],
  [/^Load average (.+)$/, '平均負荷 $1'],
  [/^Virtual machines: (\d+) running of (\d+)$/, '仮想マシン $2 台中 $1 台が実行中'],
  [/^(\d+)% of (\d+) CPUs$/, '$1% / $2 CPU'],
  [/^host RSS (.+)$/, 'ホストRSS $1'],
  [/^(\d+) lines$/, '$1 行'],
  [/^(\d+) disks?$/, 'ディスク $1 台'],
  [/^\((\d+), (\d+) on\)$/, '($1 台、$2 台実行中)'],
  [/^in use: (.+)$/, '使用中: $1'],
  [/^Move back to (.+)$/, '$1 に戻す'],
  [/^VNC :(\d+), websocket (\d+)$/, 'VNC :$1、WebSocket $2'],
  [/^KVM, else TCG$/, 'KVM（無ければTCG）'],
  [/^the boot medium, grown by (.+)$/, 'ブートメディア（$1 拡張済み）'],
];
let lang = '';
function translateNode(node) {
  if (lang !== 'ja') return;
  if (node.nodeType === 3) {
    const key = node.nodeValue.trim();
    if (!key) return;
    if (JA[key]) { node.nodeValue = node.nodeValue.replace(key, JA[key]);
                   return; }
    // a label behind a glyph: "› System Settings", "▣ Host Server"
    const tail = key.match(/^([^A-Za-z0-9]+)\s*(.+)$/);
    if (tail && JA[tail[2]]) {
      node.nodeValue = node.nodeValue.replace(tail[2], JA[tail[2]]);
      return;
    }
    for (const [pattern, into] of JA_RE)
      if (pattern.test(key)) {
        node.nodeValue = node.nodeValue.replace(key, key.replace(pattern,
                                                                into));
        return;
      }
    return;
  }
  if (node.nodeType !== 1) return;
  if (node.tagName === 'CANVAS' || node.tagName === 'IFRAME') return;
  for (const attr of ['placeholder', 'title']) {
    const value = node.getAttribute && node.getAttribute(attr);
    if (value && JA[value.trim()]) node.setAttribute(attr, JA[value.trim()]);
  }
  for (const child of node.childNodes) translateNode(child);
}
new MutationObserver(records => {
  if (lang !== 'ja') return;
  for (const record of records)
    for (const node of record.addedNodes) translateNode(node);
}).observe(document.body, {childList: true, subtree: true});

window.setLang = value => {
  try { localStorage.setItem('mirai98-lang', value); } catch (err) {}
  fetch('/api/lang', {method: 'POST', body: JSON.stringify({lang: value})})
    .finally(() => location.reload());
};

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// a Windows path is full of backslashes, which a JS string literal eats
const jsq = s => String(s ?? '').replace(/\\/g, '\\\\');
const fmtBytes = n => {
  if (n == null) return '-';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
};

// ------------------------------------------------------------ task log
const tasks = [];
function task(what, status) {
  const d = new Date();
  tasks.unshift({t: d.toLocaleTimeString(), what, status});
  tasks.splice(30);
  document.getElementById('tasks').innerHTML =
    '<tr><th style="width:8em">Time</th><th>Description</th>' +
    '<th style="width:14em">Status</th></tr>' +
    tasks.map(x => '<tr><td>' + x.t + '</td><td>' + esc(x.what) +
      '</td><td style="color:' +
      (/fail|error|refus|exist|use by/i.test(x.status) ? '#e06c5f'
                                                       : '#9ede7a') +
      '">' + esc(x.status) + '</td></tr>').join('');
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.error || r.statusText); return null; }
  return data;
}
window.act = async (name, verb) => {
  toast(verb + ' ' + name + '...');
  const data = await api('/api/instances/' + name + '/' + verb,
                         {method: 'POST'});
  if (data) { toast(name + ': ' + data.result);
              task('VM ' + name + ' - ' + verb, data.result); }
  // starting a machine and then hunting for its screen is two clicks
  // too many: go there and connect
  if (data && ['start', 'resume'].includes(verb) &&
      /^(started|resumed)/.test(data.result)) {
    autoConnect = name;
    if (location.hash !== '#/vm/' + name) { location.hash = '#/vm/' + name;
                                            return; }
  }
  render();
};
window.removeVm = async name => {
  if (!confirm('delete ' + name + '?')) return;
  await window.act(name, 'delete');
  location.hash = '#/';
};

// ---------------------------------------------------------------- tree
function drawTree() {
  const here = location.hash || '#/';
  const link = (href, text, cls) =>
    '<a href="' + href + '" class="' + (href === here ? 'on ' : '') +
    (cls || '') + '">' + text + '</a>';
  const running = instances.filter(i => i.running).length;
  document.getElementById('btn-shell').style.display =
    facts.platform === 'windows' ? 'none' : '';
  document.getElementById('tree').innerHTML =
    '<div class="navhead">Navigator</div>' +
    '<div class="group">Host Server</div>' +
    link('#/', '&#9635; Host Server') +
    link('#/storage', '&#9707; Storage') +
    (facts.platform === 'windows' ? '' :
     link('#/network', '&#8646; Networking')) +
    link('#/log', '&#9776; Logging') +
    link('#/settings', '&#9881; System Settings') +
    (facts.platform === 'windows' ? '' : link('#/shell', '&#9002;_ Shell')) +
    '<div class="group">Virtual machines <span class="count">(' +
    instances.length + ', ' + running + ' on)</span></div>' +
    link('#/vms', '&#9776; All machines') +
    instances.map(i => link('#/vm/' + i.name,
        '<span class="dot"></span>' + esc(i.name),
        i.running ? 'run' : '')).join('');
}

// VMware puts the same row of verbs above every object it shows
function actionBar(buttons) {
  return '<div class="actions">' + buttons.filter(Boolean).join('') +
         '</div>';
}
function meterBar(label, used, total, extra) {
  const pct = total ? Math.min(100, used / total * 100) : 0;
  return '<div class="meterlab"><span>' + label + '</span><span>' +
    pct.toFixed(0) + '% &nbsp; ' + (extra || (fmtBytes(used) + ' of ' +
    fmtBytes(total))) + '</span></div>' +
    '<div class="track"><div style="width:' + pct.toFixed(1) +
    '%"></div></div>';
}

// -------------------------------------------------------- host graphs
const HISTORY = 120, history = {cpu: [], mem: [], at: []};

function plot(points, colour, scale) {
  if (points.length < 2) return '<svg viewBox="0 0 100 100"></svg>';
  const step = 100 / (HISTORY - 1);
  const y = v => (100 - Math.max(0, Math.min(100, v / scale * 100)))
                 .toFixed(2);
  const line = points.map((v, i) => (i * step).toFixed(2) + ',' + y(v))
                     .join(' ');
  const last = ((points.length - 1) * step).toFixed(2);
  return '<svg viewBox="0 0 100 100" preserveAspectRatio="none">' +
    [25, 50, 75].map(g => '<line x1="0" y1="' + g + '" x2="100" y2="' + g +
      '" stroke="#2a2f34" stroke-width="1" vector-effect="non-scaling-stroke"/>'
      ).join('') +
    '<polygon points="0,100 ' + line + ' ' + last + ',100" fill="' +
    colour + '33"/>' +
    '<polyline points="' + line + '" fill="none" stroke="' + colour +
    '" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>';
}

function graph(title, now, points, colour, scale, ticks) {
  const times = history.at;
  const stamp = i => times[i] ? times[i] : '';
  const xs = times.length > 1
    ? [stamp(0), stamp(Math.floor(times.length / 2)), stamp(times.length - 1)]
    : ['', '', ''];
  return '<div class="graph"><div class="cap">' + title + '<b>' + now +
    '</b></div><div class="plotrow"><div class="ylab">' +
    ticks.map(t => '<span>' + t + '</span>').join('') +
    '</div><div class="plot">' + plot(points, colour, scale) +
    '</div></div><div class="xlab">' +
    xs.map(t => '<span>' + t + '</span>').join('') + '</div></div>';
}

function hostCard() {
  const f = hostFacts;
  const memGB = (f.mem_total || 1) / 1073741824;
  const up = f.uptime == null ? '' :
    Math.floor(f.uptime / 86400) + 'd ' +
    Math.floor(f.uptime % 86400 / 3600) + 'h ' +
    Math.floor(f.uptime % 3600 / 60) + 'm';
  return '<div class="card"><h3>Node</h3>' +
    '<div class="body"><div class="graphs">' +
    graph('% CPU', (history.cpu.at(-1) ?? 0).toFixed(0) + '% of ' +
          (f.cores || '?') + ' cores', history.cpu, 'var(--cpu)', 100,
          ['100','50','0']) +
    graph('GiB Memory', fmtBytes(f.mem_used || 0) + ' / ' +
          fmtBytes(f.mem_total || 0), history.mem, 'var(--mem)',
          f.mem_total || 1,
          [memGB.toFixed(1), (memGB / 2).toFixed(1), '0']) +
    '</div><div class="note" style="margin-top:.8em">' +
    (f.disk_total ? 'storage ' + fmtBytes(f.disk_free) + ' free of ' +
     fmtBytes(f.disk_total) + ' &nbsp; ' : '') +
    (up ? 'uptime ' + up : '') + '</div></div></div>';
}

async function pollHost() {
  const h = await api('/api/host');
  if (!h) return;
  hostFacts = h;
  if (h.cpu != null) {
    history.cpu.push(h.cpu);
    history.mem.push(h.mem_used || 0);
    history.at.push(new Date().toTimeString().slice(0, 5));
    for (const k of ['cpu', 'mem', 'at'])
      if (history[k].length > HISTORY) history[k].shift();
  }
  const slot = document.getElementById('host-card');
  if (slot) slot.innerHTML = hostCard();
}

// ------------------------------------------------------------ list view
function mediaSummary(i) {
  const parts = [];
  for (const [k] of DISK_ROWS)
    if (i[k]) parts.push(k + '=' + i[k]);
  if (i.net === 'nat') parts.push('lan(nat)');
  return parts.join(' ') || '(no media)';
}

function listRow(i) {
  const thumb = i.running
    ? '<img class="thumb" src="/thumb/' + i.name + '.png?t=' +
      Math.floor(Date.now() / 5000) + '" onerror="this.style.opacity=0">'
    : '<div class="thumb-empty">off</div>';
  return '<tr><td style="width:130px">' + thumb + '</td>' +
    '<td><a href="#/vm/' + i.name + '">' + esc(i.name) + '</a></td>' +
    '<td><span class="state' + (i.running ? ' on' : '') + '">' +
    (i.running ? 'running' : 'stopped') + '</span></td>' +
    '<td class="note">' + esc(i.machine || 'pc9821') + '</td>' +
    '<td>' + esc(i.memory) + (i.snapshot ? ' <span class="note">snap</span>'
                                         : '') + '</td>' +
    '<td class="note" style="max-width:24em;overflow-wrap:anywhere">' +
    esc(mediaSummary(i)) + '</td><td style="text-align:right">' +
    (i.running
     ? '<button onclick="act(\'' + i.name + '\',\'stop\')">Shutdown</button>'
     : '<button class="primary" onclick="act(\'' + i.name +
       '\',\'start\')">Start</button>') + '</td></tr>';
}

let lastList = '';
async function listView(force) {
  const data = await api('/api/instances');
  if (!data) return;
  instances = data;
  drawTree();
  const key = JSON.stringify(data);
  const active = document.activeElement;
  if (!force && key === lastList) {
    for (const img of view.querySelectorAll('img.thumb'))
      img.src = img.src.replace(/t=\d+/, 't=' +
                                Math.floor(Date.now() / 5000));
    return;
  }
  if (!force && active && view.contains(active) &&
      ['INPUT','SELECT','TEXTAREA'].includes(active.tagName)) return;
  lastList = key;
  const running = data.filter(i => i.running).length;
  view.innerHTML =
    '<div class="crumb"><a href="#/">' +
    esc(facts.hostname || 'host') + '</a> &rsaquo; Virtual machines' +
    '</div>' +
    '<div class="titlerow"><span class="glyph">&#9776;</span>' +
    '<h2>Virtual machines</h2><span style="flex:1"></span>' +
    '<span class="note">' + running + ' running, ' +
    (data.length - running) + ' stopped</span></div>' +
    actionBar([
      '<button class="primary" onclick="openCreate()">Create VM</button>',
      '<button onclick="render()">Refresh</button>']) +
    '<div class="card"><h3>Guests</h3><table>' +
    '<tr><th></th><th>Virtual machine</th><th>Status</th>' +
    '<th>Machine</th><th>Memory</th><th>Media</th><th></th></tr>' +
    (data.map(listRow).join('') ||
     '<tr><td colspan="7" class="note">no machines yet</td></tr>') +
    '</table></div>' +
    '<div id="host-card">' + hostCard() + '</div>';
}

// ---------------------------------------------------------- detail view
function diskSelect(key, kind, value, name) {
  const files = catalog[kind] || [];
  // real drives of the matching sort come after the images, so a guest
  // can be pointed at the host's own CD or floppy
  const drives = (hardware.drives || []).filter(
    d => d.type === kind || (kind === 'hdd' && d.type === 'hdd'));
  const known = files.some(f => f.name === value) ||
                drives.some(d => d.path === value);
  return '<select name="' + (name || key) + '">' +
    '<option value="">(none)</option>' +
    files.map(f => '<option' + (f.name === value ? ' selected' : '') + '>' +
      esc(f.name) + '</option>').join('') +
    (drives.length
     ? '<optgroup label="host drives">' + drives.map(d =>
         '<option value="' + esc(d.path) + '"' +
         (d.path === value ? ' selected' : '') +
         (d.busy ? ' disabled' : '') + '>' + esc(d.path) +
         (d.model ? ' — ' + esc(d.model) : '') +
         (d.size ? ' (' + esc(d.size) + ')' : '') +
         (d.busy ? ' [mounted on ' + esc(d.busy) + ']' : '') +
         '</option>').join('') + '</optgroup>'
     : '') +
    (value && !known ? '<option selected>' + esc(value) + '</option>' : '') +
    '</select>';
}

function serialSelect(value, name) {
  const ports = hardware.serial || [];
  return '<select name="' + (name || 'serial') + '">' +
    '<option value="">(none)</option>' +
    ports.map(p => '<option' + (p === value ? ' selected' : '') + '>' +
      esc(p) + '</option>').join('') +
    (value && !ports.includes(value)
     ? '<option selected>' + esc(value) + '</option>' : '') + '</select>';
}

// the three port rows, shared by the wizard and the hardware editor
function portRows(i) {
  return '<div class="row"><label>Serial port</label>' +
    serialSelect(i.serial || '', 'serial') +
    ' <span class="note">RS-232C</span></div>' +
    '<div class="row"><label>Parallel port</label>' +
    serialSelect(i.parallel || '', 'parallel') +
    ' <span class="note">UART BitBang Device (FT245RL)</span></div>' +
    '<div class="row"><label>GP-IB</label>' +
    serialSelect(i.gpib || '', 'gpib') +
    ' <span class="note">adapter on a serial device</span></div>';
}

// Proxmox lays hardware out as a table of what the machine actually has;
// the same rows become a form once Edit is pressed
function hardwareTable(i) {
  const rows = [['&#9636; Memory', esc(i.memory)],
                ['&#9881; Machine', esc(i.machine || 'pc9821') + ' (' +
                 (i.accel === 'tcg' ? 'TCG' : 'KVM, else TCG') + ')'],
                ['&#9750; BIOS', i.bios === 'real'
                 ? 'real machine ROMs, compatible for the rest'
                 : 'compatible'],
                ['&#9834; Sound', soundName(i.sound)],
                ['&#9635; Display', 'VNC :' + (i.ports[0] - 5900) +
                 ', websocket ' + i.ports[1]]];
  for (const [k, label] of DISK_ROWS)
    if (i[k]) rows.push(['&#9707; ' + label, esc(i[k]) +
      (i[k].startsWith('/dev/')
       ? ' <span class="note">(host drive)</span>'
       : k === 'cd' ? ' <span class="note">(read-only)</span>' : '')]);
  if (i.mount)
    rows.push(['&#9707; Shared folder', esc(i.mount) +
               ' <span class="note">(fat98)</span>']);
  if (i.serial) rows.push(['&#9704; Serial port', esc(i.serial)]);
  if (i.parallel)
    rows.push(['&#9704; Parallel port', esc(i.parallel) +
               ' <span class="note">(UART BitBang Device)</span>']);
  if (i.gpib) rows.push(['&#9704; GP-IB', esc(i.gpib)]);
  rows.push(['&#8646; Network', i.net === 'nat'
             ? 'pc98-lgy98, user NAT' : 'none']);
  if (i.snapshot)
    rows.push(['&#8635; Snapshot', 'changes discarded on shutdown']);
  if (i.extra) rows.push(['&#9656; Extra args', esc(i.extra)]);
  return '<table>' + rows.map(([k, v]) =>
    '<tr><td style="width:13em;color:#8b9298">' + k + '</td><td>' + v +
    '</td></tr>').join('') + '</table>';
}

function editForm(i) {
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    DISK_ROWS.map(([k, label, kind]) =>
      '<div class="row"><label>' + label + '</label>' +
      diskSelect(k, kind, i[k]) + '</div>').join('') +
    '<div class="row"><label>Machine type</label><select name="machine">' +
    ['pc9821','pc9801'].map(m => '<option' +
      ((i.machine || 'pc9821') === m ? ' selected' : '') + '>' + m +
      '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Shared folder</label>' +
    '<input type="text" name="mount" value="' + esc(i.mount || '') +
    '" placeholder="/data/share"> <span class="note">appears as an IDE ' +
    'disk (fat98)</span></div>' +
    portRows(i) +
    '<div class="row"><label>BIOS</label><select name="bios">' +
    [['compat','Compatible'], ['real','Real machine ROMs']].map(
      ([v, label]) => '<option value="' + v + '"' +
        ((i.bios || 'compat') === v ? ' selected' : '') + '>' + label +
        '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Memory</label><select name="memory">' +
    MEMS.map(m => '<option' + (i.memory === m ? ' selected' : '') + '>' +
             m + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Sound</label><select name="sound">' +
    ['86','wss','none'].map(s => '<option value="' + s + '"' +
      (soundKey(i.sound) === s ? ' selected' : '') + '>' +
      soundName(s) + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Acceleration</label>' +
    '<label class="check"><input type="checkbox" name="kvm"' +
    (i.accel === 'tcg' ? '' : ' checked') + '> use KVM when available' +
    '</label></div>' +
    '<div class="row"><label>Network</label><select name="net">' +
    [['', 'None'], ['nat', 'NAT'], ['bridge', 'Bridge']].map(
      ([v, label]) => '<option value="' + v + '"' +
        ((i.net || '') === v ? ' selected' : '') + '>' + label +
        '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Snapshot</label>' +
    '<label class="check"><input type="checkbox" name="snapshot"' +
    (i.snapshot ? ' checked' : '') + '> discard changes</label></div>' +
    '<div class="row"><label>Extra args</label>' +
    '<input type="text" name="extra" value="' + esc(i.extra) + '"></div>' +
    '<div class="row"><label></label><button class="primary"' +
    (i.running ? ' disabled title="stop it first"' : '') + '>Save</button>' +
    '<button type="button" onclick="removeVm(\'' + i.name + '\')"' +
    (i.running ? ' disabled' : '') + '>Delete</button></div></form>';
}
window.toggleEdit = () => {
  const edit = document.getElementById('hw-edit');
  const show = edit.style.display === 'none';
  edit.style.display = show ? '' : 'none';
  document.getElementById('hw-view').style.display = show ? 'none' : '';
};
window.saveVm = (form, name) => {
  const inst = {name};
  for (const el of form.elements)
    if (el.name) inst[el.name] = el.type === 'checkbox' ? el.checked
                                                        : el.value.trim();
  inst.accel = inst.kvm === false ? 'tcg' : 'kvm';
  delete inst.kvm;
  api('/api/instances/' + name, {method: 'PUT', body: JSON.stringify(inst)})
    .then(d => { if (d) { toast('saved'); task('VM ' + name +
                                               ' - config', 'saved'); }
                 render(); });
  return false;
};

// ------------------------------------------------------- console sound
// QEMU's VNC server streams the guest's PCM over the same WebSocket as
// the pixels (S16LE, stereo, 44.1 kHz).  Chunks are turned into
// AudioBuffers and scheduled back to back on a small jitter cushion.
const AUDIO_RATE = 44100;
let audioCtx = null, audioAt = 0, audioOn = false;
function audioChunk(bytes) {
  if (!audioCtx) return;
  const frames = bytes.byteLength >> 2;         // 2 channels x 16 bits
  if (!frames) return;
  const view = new DataView(bytes.buffer, bytes.byteOffset,
                            bytes.byteLength);
  const buf = audioCtx.createBuffer(2, frames, AUDIO_RATE);
  const left = buf.getChannelData(0), right = buf.getChannelData(1);
  for (let i = 0; i < frames; i++) {
    left[i] = view.getInt16(i * 4, true) / 32768;
    right[i] = view.getInt16(i * 4 + 2, true) / 32768;
  }
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);
  const now = audioCtx.currentTime;
  if (audioAt < now + 0.05) audioAt = now + 0.05;   // fell behind: rebase
  src.start(audioAt);
  audioAt += buf.duration;
}
window.toggleAudio = () => {
  if (!rfb || !rfb.enableAudio) { toast('no console'); return; }
  audioOn = !audioOn;
  const btn = document.getElementById('btn-audio');
  if (audioOn) {
    if (!audioCtx)
      audioCtx = new AudioContext({sampleRate: AUDIO_RATE});
    audioCtx.resume();
    rfb.enableAudio(3, 2, AUDIO_RATE);          // 3 = S16
    if (btn) { btn.textContent = '🔊 Sound on'; }
    toast('sound on');
  } else {
    rfb.disableAudio();
    if (btn) { btn.textContent = '🔇 Sound off'; }
    toast('sound off');
  }
};
function stopAudio() {
  audioOn = false;
  if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
  audioAt = 0;
}

window.connectConsole = async (name, ws) => {
  disconnectConsole();
  window.toggleConsolePane(true);      // a hidden box has no size to scale to
  const target = document.getElementById('console-box');
  target.innerHTML = '';
  try {
    await loadRFB();
  } catch (err) {
    target.textContent = 'noVNC is missing: put it in novnc/ beside the ' +
                         'program.';
    toast('no noVNC to draw the console with');
    return;
  }
  rfb = new RFB(target, 'ws://' + location.hostname + ':' + ws + '/');
  rfb.scaleViewport = true;
  rfb.background = '#000';
  consoleWatch = new ResizeObserver(() =>
    window.dispatchEvent(new Event('resize')));
  consoleWatch.observe(target);
  rfb.addEventListener('connect', () => toast(name + ': console connected'));
  rfb.addEventListener('disconnect',
                       () => toast(name + ': console disconnected'));
  rfb.addEventListener('audiodata', e => audioChunk(e.detail.data));
  document.getElementById('btn-connect').style.display = 'none';
  for (const id of ['btn-disconnect','btn-cad','btn-expand','btn-audio'])
    document.getElementById(id).style.display = '';
};
window.disconnectConsole = () => {
  if (consoleWatch) { consoleWatch.disconnect(); consoleWatch = null; }
  if (rfb) { try { rfb.disconnect(); } catch (e) {} rfb = null; }
  stopAudio();
};
window.toggleConsolePane = show => {
  const pane = document.getElementById('console-pane');
  if (!pane) return false;
  const open = show === true || pane.style.display === 'none';
  pane.style.display = open ? '' : 'none';
  document.getElementById('console-caret').innerHTML =
    open ? '&#9662;' : '&#9656;';
  return open;
};
window.openConsolePane = (name, ws) => {
  window.toggleConsolePane(true);
  if (!rfb) window.connectConsole(name, ws);
};
window.sendCad = () => rfb && rfb.sendCtrlAltDel();
window.expandConsole = () =>
  document.getElementById('console-box').requestFullscreen();

async function detailView(name) {
  const i = await api('/api/instances/' + encodeURIComponent(name));
  if (!i) { view.innerHTML = ''; return; }
  const wasConnected = rfb !== null;
  const disks = DISK_ROWS.filter(([k]) => i[k]);
  const stamp = Math.floor(Date.now() / 5000);
  view.innerHTML =
    '<div class="crumb"><a href="#/">' +
    esc(facts.hostname || 'host') + '</a> &rsaquo; ' +
    '<a href="#/vms">Virtual machines</a> &rsaquo; ' + esc(name) + '</div>' +
    '<div class="titlerow"><span class="glyph">&#9635;</span>' +
    '<h2>' + esc(name) + '</h2>' +
    '<span class="state' + (i.running ? ' on' : '') + '">' +
    (i.running ? 'running' : 'stopped') + '</span></div>' +
    actionBar([
      '<button id="btn-connect" class="primary" onclick="connectConsole(\'' +
      name + '\',' + i.ports[1] + ')"' + (i.running ? '' : ' disabled') +
      '>Console</button>',
      '<button id="btn-disconnect" style="display:none" ' +
      'onclick="disconnectConsole();render()">Disconnect</button>',
      '<button id="btn-cad" style="display:none" onclick="sendCad()">' +
      'Ctrl+Alt+Del</button>',
      '<button id="btn-expand" style="display:none" ' +
      'onclick="expandConsole()">Fullscreen</button>',
      '<button id="btn-audio" style="display:none" ' +
      'onclick="toggleAudio()">🔇 Sound off</button>',
      i.running
        ? '<button onclick="act(\'' + name + '\',\'stop\')">Shut down' +
          '</button>'
        : '<button class="primary" onclick="act(\'' + name +
          '\',\'start\')">Power on</button>',
      i.running
        ? '<button onclick="act(\'' + name + '\',\'save\')">Suspend</button>'
        : '<button onclick="act(\'' + name + '\',\'resume\')">Resume' +
          '</button>',
      i.running
        ? '<button onclick="act(\'' + name + '\',\'reset\')">Restart' +
          '</button>' : '',
      '<button onclick="toggleEdit()"' +
      (i.running ? ' disabled title="power it off first"' : '') +
      '>Edit</button>',
      '<button onclick="render()">Refresh</button>',
      i.running ? '' : '<button onclick="removeVm(\'' + name +
                       '\')">Delete</button>']) +
    // the screen comes first, but folded away until it is wanted
    '<div class="card"><h3 style="cursor:pointer" ' +
    'onclick="toggleConsolePane()"><span id="console-caret">&#9656;</span>' +
    ' Console</h3>' +
    '<div class="body" id="console-pane" style="display:none">' +
    '<div id="console-box"><div class="hint">' +
    (i.running ? 'press Console to connect' : 'the machine is powered off') +
    '</div></div></div></div>' +
    // then the summary strip: still shot, facts, gauges
    '<div style="display:flex;gap:1.2em;flex-wrap:wrap;margin-bottom:1em">' +
    '<div>' + (i.running
      ? '<img class="thumb" style="width:230px;height:144px;cursor:pointer"' +
        ' title="open the console" onclick="openConsolePane(\'' + name +
        '\',' + i.ports[1] + ')" src="/thumb/' +
        name + '.png?t=' + stamp + '">'
      : '<div class="thumb-empty" style="width:230px;height:144px">' +
        'powered off</div>') + '</div>' +
    '<div style="flex:1;min-width:18em"><dl>' +
    '<dt>Machine type</dt><dd>' + esc(i.machine || 'pc9821') + '</dd>' +
    '<dt>BIOS</dt><dd>' + (i.bios === 'real' ? 'real machine ROMs'
                                             : 'compatible') + '</dd>' +
    '<dt>Acceleration</dt><dd>' + (i.accel === 'tcg' ? 'TCG'
                                   : 'KVM, else TCG') + '</dd>' +
    '<dt>Memory</dt><dd>' + esc(i.memory) + '</dd>' +
    '<dt>Disks</dt><dd>' + (disks.length || 'none — N88 BASIC') + '</dd>' +
    '<dt>Console</dt><dd>VNC :' + (i.ports[0] - 5900) + '</dd></dl></div>' +
    '<div style="width:16em" id="gauges"></div></div>' +
    // the two panels, as VMware arranges them
    '<div class="grid2" style="grid-template-columns:1fr 1fr">' +
    '<div class="card"><h3>General information</h3><table>' +
    '<tr><td style="width:11em;color:#8d99a5">Networking</td><td>' +
    (i.net === 'nat' ? 'LGY-98, user NAT'
     : i.net === 'bridge' ? 'LGY-98, bridged to the LAN' : 'none') +
    '</td></tr>' +
    '<tr><td style="color:#8d99a5">Storage</td><td>' +
    (disks.length + ' disk' + (disks.length === 1 ? '' : 's')) +
    (i.snapshot ? ', changes discarded at shutdown' : '') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Shared folder</td><td>' +
    (i.mount ? esc(i.mount) + ' (fat98)' : 'none') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Serial port</td><td>' +
    (i.serial ? esc(i.serial) : 'none') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Parallel port</td><td>' +
    (i.parallel ? esc(i.parallel) +
     ' <span class="note">UART BitBang Device</span>' : 'none') +
    '</td></tr>' +
    '<tr><td style="color:#8d99a5">GP-IB</td><td>' +
    (i.gpib ? esc(i.gpib) : 'none') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Home</td><td>vm/vm-' + i.index +
    '/</td></tr>' +
    (i.running ? '<tr><td style="color:#8d99a5">Media</td>' +
     '<td id="media-row" class="note">reading...</td></tr>' : '') +
    '<tr><td style="color:#8d99a5">QMP</td><td>127.0.0.1:' + i.ports[2] +
    '</td></tr></table></div>' +
    '<div class="card"><h3>Hardware configuration</h3>' +
    '<div id="hw-view">' + hardwareTable(i) + '</div>' +
    '<div id="hw-edit" class="body" style="display:none">' +
    editForm(i) + '</div></div>' +
    '</div>';
  if ((wasConnected || autoConnect === name) && i.running)
    window.connectConsole(name, i.ports[1]);
  autoConnect = '';
  updateUsage(name);
  if (i.running) drawMedia(name);
}

// swapping a disk while the machine runs, the way a hand would
async function drawMedia(name) {
  const slot = document.getElementById('media-row');
  if (!slot) return;
  const d = await api('/api/instances/' + encodeURIComponent(name) +
                      '/media');
  if (!d || !document.getElementById('media-row')) return;
  if (!d.drives.length) {
    document.getElementById('media-row').textContent =
      'no floppy or CD-ROM drive';
    return;
  }
  document.getElementById('media-row').innerHTML = d.drives.map(drive => {
    const files = catalog[drive.kind] || [];
    const here = drive.file.split('/').pop();
    return '<div class="row" style="margin:.1em 0">' +
      '<span style="width:5.5em">' + esc(drive.device) + '</span>' +
      '<select onchange="swapMedia(\'' + name + '\',\'' + drive.device +
      '\',this.value)"><option value="">(empty)</option>' +
      files.map(f => '<option' + (f.name === here ? ' selected' : '') + '>' +
        esc(f.name) + '</option>').join('') +
      (here && !files.some(f => f.name === here)
       ? '<option selected>' + esc(here) + '</option>' : '') +
      '</select></div>';
  }).join('');
}
window.swapMedia = (name, device, file) => {
  api('/api/instances/' + encodeURIComponent(name) + '/media',
      {method: 'POST', body: JSON.stringify({device, name: file})})
    .then(r => { if (r) { toast(device + ': ' + r.result);
                          task('VM ' + name + ' - ' + device, r.result); }
                 drawMedia(name); });
};

// the three readings VMware stacks down the right of a VM summary
async function updateUsage(name) {
  const slot = document.getElementById('gauges');
  if (!slot) return;
  const s = await api('/api/instances/' + encodeURIComponent(name) +
                      '/stats');
  if (!s || !document.getElementById('gauges')) return;
  const inst = instances.find(v => v.name === name) || {};
  const gauge = (label, value, note) =>
    '<div style="display:flex;align-items:center;gap:.6em;' +
    'margin-bottom:.9em"><div style="flex:1;text-align:right">' +
    '<div class="note" style="letter-spacing:.08em">' + label + '</div>' +
    '<div style="font-size:17px">' + value + '</div>' +
    (note ? '<div class="note">' + note + '</div>' : '') + '</div>' +
    '<div style="width:22px;height:22px;border:1px solid var(--line);' +
    'background:#171c21"></div></div>';
  document.getElementById('gauges').innerHTML = s.running
    ? gauge('CPU', (s.cpu == null ? '...' : s.cpu + '%'),
            'of one guest CPU') +
      gauge('MEMORY', esc(inst.memory || ''),
            'host RSS ' + fmtBytes(s.rss)) +
      gauge('PROCESS', 'pid ' + s.pid, '')
    : gauge('CPU', '—', '') + gauge('MEMORY', esc(inst.memory || ''), '') +
      gauge('PROCESS', 'not running', '');
}

// --------------------------------------------------------- storage view
function fmtDate(t) { return new Date(t * 1000).toLocaleString(); }
const extOf = n => { const i = n.lastIndexOf('.');
                     return i < 0 ? '' : n.slice(i).toLowerCase(); };
const CONVERT_TARGETS = {
  'hdd:.hdi': ['raw'], 'hdd:.raw': ['hdi','qcow2'],
  'hdd:.img': ['hdi','qcow2'], 'hdd:.qcow2': ['raw'],
  'fdd:.fdi': ['raw'], 'fdd:.raw': ['fdi'], 'fdd:.img': ['fdi'],
};

function storageCard(kind, files) {
  const rows = files.map(f => {
    const used = f.used_by.join(', ');
    const targets = CONVERT_TARGETS[kind + ':' + extOf(f.name)] || [];
    return '<tr><td><a href="#/disk/' + kind + '/' +
      encodeURIComponent(f.name) + '">' + esc(f.name) + '</a></td><td>' +
      fmtBytes(f.size) +
      '</td><td class="note">' + fmtDate(f.mtime) + '</td><td>' +
      esc(used || '-') + '</td><td style="text-align:right">' +
      '<button type="button" onclick="showTree(\'' + kind + '\',\'' +
      f.name + '\')">Contents</button> ' +
      '<a href="/disks/' + kind + '/' + f.name + '" download>' +
      '<button type="button">Download</button></a> ' +
      targets.map(t => '<button type="button" onclick="convertDisk(\'' +
        kind + '\',\'' + f.name + '\',\'' + t + '\')">to ' + t +
        '</button>').join(' ') +
      ' <button type="button"' + (used ? ' disabled title="in use"' : '') +
      ' onclick="deleteDisk(\'' + kind + '\',\'' + f.name +
      '\')">Delete</button></td></tr>';
  }).join('');
  let create = '';
  if (kind === 'hdd')
    create = '<h4>Create a disk</h4><div class="body">' +
      '<form onsubmit="return createDisk(this,\'hdd\')" class="row">' +
      '<input type="text" name="name" placeholder="new-disk.qcow2" ' +
      'required style="min-width:11em"><select name="size">' +
      [40,80,160,320,640,1200,2100,4300].map(s => '<option' +
        (s === 40 ? ' selected' : '') + '>' + s + '</option>').join('') +
      '</select><span class="note">MB</span>' +
      '<label class="check"><input type="checkbox" name="fat32"> FAT32' +
      '</label><button class="primary">Create</button></form>' +
      '<div class="note">.qcow2 grows on demand. .hdi: Anex86. ' +
      '.raw: flat.</div></div>';
  else if (kind === 'fdd')
    create = '<h4>Create a floppy</h4><div class="body">' +
      '<form onsubmit="return createDisk(this,\'fdd\')" class="row">' +
      '<input type="text" name="name" placeholder="new-floppy.fdi" ' +
      'required style="min-width:11em"><select name="format">' +
      '<option>1.2</option><option>1.44</option></select>' +
      '<button class="primary">Create</button></form>' +
      '<div class="note">.fdi or .raw. Formatted, empty.</div></div>';
  return '<div class="card"><h3>disks/' + kind + '/</h3>' +
    '<h4>Images</h4><table>' +
    '<tr><th>Name</th><th>Size</th><th>Modified</th><th>Used by</th>' +
    '<th></th></tr>' +
    (rows || '<tr><td colspan="5" class="note">(empty)</td></tr>') +
    '</table>' +
    '<h4>Add an image</h4><div class="body"><div class="row">' +
    '<button type="button" onclick="pickUpload(\'' + kind +
    '\')">Upload from this computer...</button>' +
    '<button type="button" onclick="fetchDisk(\'' + kind +
    '\')">Download from URL...</button>' +
    '<button type="button" onclick="importDisk(\'' + kind +
    '\')">Import a server path...</button>' +
    '<button type="button" onclick="readFromDrive(\'' + kind +
    '\')">Read a host drive...</button></div>' +
    '<div id="jobs-' + kind + '" class="note"></div></div>' +
    create + '</div>';
}

function romCard(data) {
  const rows = data.roms.map(r =>
    '<tr><td>' + esc(r.name) + '</td><td>' +
    (r.present ? fmtBytes(r.size) : '<span class="note">not uploaded, ' +
     'the compatible ROM stands in</span>') + '</td>' +
    '<td style="text-align:right">' +
    '<button type="button" onclick="pickRom(\'' + r.name + '\')">' +
    (r.present ? 'Replace...' : 'Upload...') + '</button>' +
    (r.present ? ' <button type="button" onclick="deleteRom(\'' + r.name +
     '\')">Remove</button>' : '') + '</td></tr>').join('');
  return '<div class="card"><h3>Real machine ROMs &mdash; ' +
    esc(data.dir) + '</h3><table>' +
    '<tr><th>File</th><th>State</th><th></th></tr>' + rows + '</table>' +
    '<div class="body note">A machine set to the real BIOS uses these ' +
    'and falls back to the compatible ROMs. N88 BASIC needs ' +
    'pc98basic.bin. No compatible version yet.</div></div>';
}

window.pickRom = name => {
  const input = document.createElement('input');
  input.type = 'file';
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/roms?name=' + encodeURIComponent(name));
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.response); } catch (e) {}
      if (xhr.status === 200) {
        toast('uploaded ' + name);
        task('ROM ' + name + ' - upload', 'OK');
        render();
      } else {
        toast(r.error || 'upload failed');
        task('ROM ' + name + ' - upload', r.error || 'failed');
      }
    };
    xhr.send(file);
  };
  input.click();
};
window.deleteRom = name => {
  if (!confirm('remove ' + name + '?')) return;
  api('/api/roms/' + name, {method: 'DELETE'})
    .then(r => { if (r) { toast('removed ' + name);
                          task('ROM ' + name + ' - remove', 'OK'); }
                 render(); });
};

// ---------------------------------------------------- one disk's page
async function diskView(kind, name) {
  view.innerHTML = '<div class="note">reading ' + esc(name) + '...</div>';
  const d = await api('/api/disk/' + kind + '/' + encodeURIComponent(name));
  if (!d) { view.innerHTML = ''; return; }
  const listing = d.error
    ? '<div class="body note">Cannot read the file system inside: ' +
      esc(d.error) + '</div>'
    : '<table><tr><th>Name</th><th style="width:8em">Size</th></tr>' +
      (d.files.length
       ? d.files.map(f => '<tr><td>' + esc(f.name) + '</td><td>' +
           (f.size == null ? '' : fmtBytes(f.size)) + '</td></tr>').join('')
       : '<tr><td colspan="2" class="note">(empty)</td></tr>') + '</table>';
  view.innerHTML =
    '<div class="crumb"><a href="#/">' + esc(facts.hostname || 'host') +
    '</a> &rsaquo; <a href="#/storage">Storage</a> &rsaquo; ' +
    esc(kind) + ' &rsaquo; ' + esc(name) + '</div>' +
    '<div class="titlerow"><span class="glyph">&#9707;</span>' +
    '<h2>' + esc(name) + '</h2>' +
    (d.used_by.length
     ? '<span class="state on">in use</span>' : '') + '</div>' +
    actionBar([
      '<a href="/disks/' + kind + '/' + encodeURIComponent(name) +
      '" download><button>Download image</button></a>',
      '<a href="/zip/' + kind + '/' + encodeURIComponent(name) +
      '"><button>Download contents as ZIP</button></a>',
      '<button onclick="pickZip(\'' + kind + '\',\'' + name +
      '\')">Write a ZIP into it...</button>',
      '<button onclick="copyDisk(\'' + kind + '\',\'' + name +
      '\')">Make a copy</button>',
      '<button onclick="writeToDrive(\'' + kind + '\',\'' + name +
      '\')">Write to a drive...</button>',
      '<button onclick="render()">Refresh</button>',
      d.used_by.length ? '' : '<button onclick="deleteDisk(\'' + kind +
        '\',\'' + name + '\',\'#/storage\')">Delete</button>']) +
    '<div class="grid2" style="grid-template-columns:22em 1fr">' +
    '<div class="card"><h3>Details</h3><table>' +
    [['File', d.path], ['Format', d.format], ['Size', fmtBytes(d.size)],
     ['Contents', d.error ? 'unreadable'
       : d.files.length + ' entries, ' + fmtBytes(d.bytes)],
     ['Modified', new Date(d.mtime * 1000).toLocaleString()],
     ['Used by', d.used_by.join(', ') || 'no machine']]
      .map(([k, v]) => '<tr><td style="width:7em;color:#8d99a5">' + k +
        '</td><td style="overflow-wrap:anywhere">' + esc(v) +
        '</td></tr>').join('') + '</table></div>' +
    '<div class="card"><h3>File system</h3>' + listing + '</div></div>';
}

// real drives, both ways: a card written from an image, or an image
// taken off a card
function freeDrives(kind) {
  return (hardware.drives || []).filter(
    d => !d.busy && !d.system &&
         (kind === 'cdrom' ? d.type === 'cdrom' : d.type !== 'cdrom'));
}
function askDrive(kind, verb) {
  const list = freeDrives(kind);
  if (!list.length) {
    toast('no free drive: everything the host can see is mounted');
    return null;
  }
  const menu = list.map((d, n) => n + ': ' + d.path +
    (d.model ? ' — ' + d.model : '') + (d.size ? ' (' + d.size + ')' : ''))
    .join('\n');
  const pick = prompt(verb + '\n\n' + menu + '\n\nnumber:', '0');
  if (pick === null) return null;
  const chosen = list[parseInt(pick, 10)];
  if (!chosen) { toast('no such drive'); return null; }
  return chosen;
}
window.writeToDrive = (kind, name) => {
  const drive = askDrive(kind, 'Write ' + name + ' onto which drive?');
  if (!drive) return;
  if (!confirm('Everything on ' + drive.path + ' will be overwritten by ' +
               name + '.\n\nContinue?')) return;
  api('/api/disk/' + kind + '/' + encodeURIComponent(name) + '/to-drive',
      {method: 'POST', body: JSON.stringify({device: drive.path})})
    .then(r => { if (r) { toast('writing to ' + drive.path);
                          task('Disk ' + name + ' → ' + drive.path,
                               'started'); pollJobs(); } });
};
window.readFromDrive = kind => {
  const drive = askDrive(kind, 'Read which drive into a new image?');
  if (!drive) return;
  const guess = drive.path.split('/').pop() +
                (kind === 'fdd' ? '.raw' : '.raw');
  const name = prompt('name for the new image', guess);
  if (!name) return;
  api('/api/disks/' + kind + '/from-drive',
      {method: 'POST',
       body: JSON.stringify({device: drive.path, name})})
    .then(r => { if (r) { toast('reading ' + drive.path);
                          task('Disk ' + drive.path + ' → ' + name,
                               'started'); pollJobs(); } });
};

window.copyDisk = (kind, name) => {
  const dot = name.lastIndexOf('.');
  const guess = dot > 0 ? name.slice(0, dot) + '-copy' + name.slice(dot)
                        : name + '-copy';
  const target = prompt('name for the copy', guess);
  if (!target) return;
  toast('copying ' + name + '...');
  api('/api/disk/' + kind + '/' + encodeURIComponent(name) + '/copy',
      {method: 'POST', body: JSON.stringify({name: target})})
    .then(r => { if (r) { toast('made ' + r.name);
                          task('Disk ' + name + ' - copy', 'OK');
                          location.hash = '#/disk/' + kind + '/' + r.name; }
                 render(); });
};

window.pickZip = (kind, name) => {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.zip,application/zip';
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    toast('writing ' + file.name + ' into ' + name + '...');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/disk/' + kind + '/' + encodeURIComponent(name) +
             '/unzip');
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.response); } catch (e) {}
      if (xhr.status === 200) {
        toast('wrote ' + file.name + ' into ' + name);
        task('Disk ' + name + ' - write ZIP', 'OK');
      } else {
        toast(r.error || 'failed');
        task('Disk ' + name + ' - write ZIP', r.error || 'failed');
      }
      render();
    };
    xhr.onerror = () => toast('the connection dropped');
    xhr.send(file);
  };
  input.click();
};

async function storageView() {
  const roms = await api('/api/roms');
  view.innerHTML = '<div class="topbar"><h2>Storage</h2>' +
    '<span class="note">' + (hostFacts.disk_total
      ? fmtBytes(hostFacts.disk_free) + ' free of ' +
        fmtBytes(hostFacts.disk_total) : '') + '</span></div>' +
    ['hdd','fdd','cdrom'].map(k => storageCard(k, catalog[k])).join('') +
    (roms ? romCard(roms) : '');
}

window.createDisk = (form, kind) => {
  const d = {};
  for (const el of form.elements)
    if (el.name) d[el.name] = el.type === 'checkbox' ? el.checked
                                                     : el.value.trim();
  toast('creating ' + d.name + '...');
  api('/api/disks/' + kind + '/create',
      {method: 'POST', body: JSON.stringify(d)})
    .then(r => { if (r) { toast('created ' + r.name);
                          task('Disk ' + r.name + ' - create', 'OK'); }
                 render(); });
  return false;
};
window.convertDisk = (kind, name, target) => {
  toast('converting ' + name + '...');
  api('/api/disks/' + kind + '/convert',
      {method: 'POST', body: JSON.stringify({source: name, format: target})})
    .then(r => { if (r) { toast('made ' + r.name);
                          task('Disk ' + name + ' - convert to ' + target,
                               'OK'); }
                 render(); });
};
window.deleteDisk = (kind, name, then) => {
  if (!confirm('delete ' + name + '?')) return;
  api('/api/disks/' + kind + '/' + encodeURIComponent(name),
      {method: 'DELETE'})
    .then(r => { if (r) { toast('deleted ' + name);
                          task('Disk ' + name + ' - delete', 'OK');
                          if (then) { location.hash = then; return; } }
                 render(); });
};
// a FAT image can be read without starting anything, so show what is in
// it; anything else says why it cannot
window.showTree = async (kind, name) => {
  const veil = document.getElementById('tree-veil');
  document.getElementById('tree-name').textContent = name;
  document.getElementById('tree-body').textContent = 'reading...';
  veil.classList.add('open');
  const d = await api('/api/disk/' + kind + '/' + encodeURIComponent(name));
  if (!d) { veil.classList.remove('open'); return; }
  document.getElementById('tree-body').innerHTML = d.error
    ? '<div class="note">no readable file system: ' + esc(d.error) + '</div>'
    : '<div class="note">' + d.files.length + ' entries, ' +
      fmtBytes(d.bytes) + '</div><pre style="margin:.4em 0 0">' +
      esc(d.files.map(f => (f.name.endsWith('/') ? f.name
        : f.name.padEnd(28) + (f.size == null ? '' : fmtBytes(f.size))))
        .join('\n')) + '</pre>';
};
window.closeTree = () =>
  document.getElementById('tree-veil').classList.remove('open');

window.fetchDisk = kind => {
  const url = prompt('image URL to download into disks/' + kind + '/');
  if (!url) return;
  api('/api/disks/' + kind + '/fetch',
      {method: 'POST', body: JSON.stringify({url})})
    .then(r => { if (r) { toast('downloading ' + r.name);
                          task('Disk ' + r.name + ' - download', 'started');
                          pollJobs(); } });
};
async function pollJobs() {
  const jobs = await api('/api/jobs');
  if (!jobs) return;
  for (const kind of ['hdd', 'fdd', 'cdrom']) {
    const slot = document.getElementById('jobs-' + kind);
    if (!slot) continue;
    const mine = jobs.filter(j => j.kind === kind);
    slot.innerHTML = mine.map(j =>
      j.state === 'running'
        ? 'downloading ' + esc(j.name) + ': ' + fmtBytes(j.done) +
          (j.total ? ' of ' + fmtBytes(j.total) + ' (' +
           (j.done / j.total * 100).toFixed(0) + '%)' : '')
        : j.state === 'failed'
          ? '<span style="color:#e06c5f">' + esc(j.name) + ': ' +
            esc(j.error) + '</span>'
          : esc(j.name) + ': downloaded').join('<br>');
    if (mine.some(j => j.state === 'done' &&
                       !catalog[kind].some(f => f.name === j.name)))
      render();
  }
}
window.importDisk = kind => {
  const path = prompt('server path to adopt into disks/' + kind + '/');
  if (!path) return;
  api('/api/disks/' + kind + '/import',
      {method: 'POST', body: JSON.stringify({path})})
    .then(r => { if (r) { toast('imported ' + r.name);
                          task('Disk ' + r.name + ' - import', 'OK'); }
                 render(); });
};
// ------------------------------------------------------------- uploads
// A whole disk image goes up in slices: the browser only has to hold one
// at a time, a stall costs one slice rather than the lot, and what
// arrived stays on the server so the next try continues from there.
const CHUNK = 16 << 20;
let uploadStop = false;

function uploadBox(file, kind) {
  document.getElementById('upload-name').textContent =
    file.name + ' → disks/' + kind + '/';
  document.getElementById('upload-veil').classList.add('open');
  document.getElementById('upload-bar').value = 0;
  document.getElementById('upload-stat').textContent = '';
  document.getElementById('upload-close').textContent = 'Cancel';
  uploadStop = false;
}
window.closeUpload = () => {
  uploadStop = true;
  document.getElementById('upload-veil').classList.remove('open');
};

function sendSlice(url, blob) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.response); } catch (e) {}
      xhr.status === 200 ? resolve(r)
                         : reject(new Error(r.error || 'HTTP ' + xhr.status));
    };
    xhr.onerror = () => reject(new Error('the connection dropped'));
    xhr.send(blob);
  });
}

window.pickUpload = kind => {
  const input = document.createElement('input');
  input.type = 'file';
  input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    let overwrite = '';
    if (catalog[kind].some(f => f.name === file.name)) {
      if (!confirm(file.name + ' exists. Overwrite?')) return;
      overwrite = '&overwrite=1';
    }
    const base = '/api/disks/' + kind;
    const q = 'name=' + encodeURIComponent(file.name) + '&total=' +
              file.size;
    uploadBox(file, kind);
    const bar = document.getElementById('upload-bar');
    const stat = document.getElementById('upload-stat');
    const started = Date.now();
    let sent = 0, tries = 0;
    while (sent < file.size) {
      if (uploadStop) { toast('upload cancelled'); return; }
      const end = Math.min(sent + CHUNK, file.size);
      try {
        const r = await sendSlice(
          base + '/chunk?' + q + '&offset=' + sent + overwrite,
          file.slice(sent, end));
        sent = r.have ?? end;
        tries = 0;
      } catch (err) {
        if (++tries > 3) {
          stat.textContent = 'stopped: ' + err.message;
          document.getElementById('upload-close').textContent = 'Close';
          toast('upload failed: ' + err.message);
          task('Disk ' + file.name + ' - upload', err.message);
          return;
        }
        stat.textContent = 'retrying after ' + err.message + ' (' + tries +
                           '/3)';
        await new Promise(r => setTimeout(r, 1500));
        continue;
      }
      const secs = (Date.now() - started) / 1000;
      const rate = sent / Math.max(secs, 0.1);
      const left = (file.size - sent) / Math.max(rate, 1);
      bar.value = sent / file.size * 100;
      stat.textContent = fmtBytes(sent) + ' of ' + fmtBytes(file.size) +
        '  —  ' + fmtBytes(rate) + '/s, ' +
        (left > 90 ? Math.round(left / 60) + ' min left'
                   : Math.round(left) + ' s left');
    }
    stat.textContent = 'finishing...';
    try {
      const done = await sendSlice(base + '/finish?' + q, null);
      const named = done.name || file.name;
      stat.textContent = 'uploaded ' + named +
        (named !== file.name ? ' (converted on the way in)' : '');
      document.getElementById('upload-close').textContent = 'Close';
      toast('uploaded ' + named);
      task('Disk ' + named + ' - upload', 'OK');
      render();
    } catch (err) {
      stat.textContent = 'failed: ' + err.message;
      document.getElementById('upload-close').textContent = 'Close';
      task('Disk ' + file.name + ' - upload', err.message);
    }
  };
  input.click();
};

// ------------------------------------------------------ create wizard
window.memSlide = el => {
  document.getElementById('mem-text').value = MEMS[el.value];
};
window.memType = el => {
  const i = MEMS.indexOf(el.value.trim().toUpperCase());
  if (i >= 0) document.getElementById('mem-range').value = i;
};
function drawTabs() {
  document.getElementById('tabs').innerHTML = TABS.map((t, n) =>
    '<span class="' + (n === tab ? 'on' : '') + '" onclick="tabGo(' + n +
    ')">' + t + '</span>').join('');
  document.querySelectorAll('#wizard .pane').forEach((p, n) =>
    p.classList.toggle('on', n === tab));
  document.getElementById('btn-back').disabled = tab === 0;
  const last = tab === TABS.length - 1;
  document.getElementById('btn-next').style.display = last ? 'none' : '';
  document.getElementById('btn-finish').style.display = last ? '' : 'none';
  if (last) drawConfirm();
}
window.tabGo = n => { tab = n; drawTabs(); };
window.tabStep = d => { tab = Math.max(0, Math.min(TABS.length - 1,
                                                   tab + d));
                        drawTabs(); };
function wizardValues() {
  const form = document.getElementById('wizard');
  const out = {};
  for (const el of form.elements)
    if (el.name) out[el.name] = el.type === 'checkbox' ? el.checked
                                                       : el.value.trim();
  out.accel = out.kvm === false ? 'tcg' : 'kvm';
  delete out.kvm;
  return out;
}
function drawConfirm() {
  const v = wizardValues();
  const anyDisk = DISK_ROWS.some(([k]) => v[k]);
  const rows = [['Name', v.name || '(unnamed)'],
                ['Machine type', v.machine],
                ['BIOS', v.bios === 'real' ? 'real machine ROMs'
                                           : 'compatible'],
                ['Memory', v.memory],
                ['Sound', soundName(v.sound)],
                ['Acceleration', v.accel === 'kvm' ? 'KVM, else TCG'
                                                   : 'TCG only'],
                ['Snapshot', v.snapshot ? 'yes' : 'no'],
                ['Network', v.net === 'nat' ? 'NAT (LGY-98)'
                            : v.net === 'bridge' ? 'Bridge (LGY-98)'
                            : 'none']];
  for (const [k, label] of DISK_ROWS)
    if (v[k]) rows.push([label, v[k]]);
  if (v.mount) rows.push(['Shared folder', v.mount + ' (fat98)']);
  if (v.serial) rows.push(['Serial port', v.serial]);
  if (v.parallel) rows.push(['Parallel port',
                             v.parallel + ' (UART BitBang Device)']);
  if (v.gpib) rows.push(['GP-IB', v.gpib]);
  if (v.extra) rows.push(['Extra args', v.extra]);
  if (!anyDisk) rows.push(['Disks', 'none — the machine will land in ' +
                           'N88 BASIC, which lives in ROM']);
  document.getElementById('confirm-table').innerHTML = '<table>' +
    rows.map(([k, x]) => '<tr><td style="width:11em;color:#8b9298">' + k +
      '</td><td>' + esc(x) + '</td></tr>').join('') + '</table>';
}
window.openCreate = () => {
  document.getElementById('pane-disks').innerHTML = DISK_ROWS.map(
    ([k, label, kind]) => '<div class="row"><label>' + label + '</label>' +
    diskSelect(k, kind, '') + '</div>').join('') +
    '<div class="note">Images and host drives. Manage images in Storage. ' +
    'No disk: starts N88 BASIC.</div>';
  document.getElementById('pane-ports').innerHTML = portRows({});
  const range = document.getElementById('mem-range');
  range.max = MEMS.length - 1;
  range.value = MEMS.indexOf('64M');
  document.getElementById('mem-text').value = '64M';
  document.getElementById('wizard').reset();
  document.getElementById('mem-text').value = '64M';
  tab = 0;
  drawTabs();
  document.getElementById('veil').classList.add('open');
};
window.closeCreate = () =>
  document.getElementById('veil').classList.remove('open');
function wizardSays(text) {
  const note = document.getElementById('wizard-note');
  note.textContent = text || '';
  note.style.color = text ? '#e06c5f' : '';
}
window.submitCreate = () => {
  const v = wizardValues();
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(v.name)) {
    wizardSays('the name must be 1-32 letters, digits, - or _');
    tabGo(0);
    return false;
  }
  wizardSays('creating...');
  // the answer belongs in the dialog: a toast at the top of the page is
  // easy to miss while looking at the form that just refused to close
  fetch('/api/instances', {method: 'POST', body: JSON.stringify(v)})
    .then(async r => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        wizardSays(d.error || ('the server said ' + r.status));
        task('VM ' + v.name + ' - create', d.error || 'failed');
        return;
      }
      wizardSays('');
      toast('created ' + v.name);
      task('VM ' + v.name + ' - create', 'OK');
      closeCreate();
      location.hash = '#/vm/' + v.name;
    })
    .catch(err => wizardSays(String(err)));
  return false;
};

// --------------------------------------------------------------- router
// the navigator shows the whole fleet whatever page is open, so these
// come along on every render, not only on the list
async function refreshFleet() {
  const [fleet, gear] = await Promise.all([api('/api/instances'),
                                           api('/api/hardware')]);
  if (fleet) instances = fleet;
  if (gear) hardware = gear;
  if (!facts.hostname) {
    const f = await api('/api/facts');
    if (f) facts = f;
  }
}

async function refreshDisks() {
  const gear = await api('/api/hardware');
  if (gear) hardware = gear;
  const disks = await api('/api/disks');
  if (!disks) return;
  catalog = disks;
  document.getElementById('datalists').innerHTML =
    ['hdd','fdd','cdrom'].map(kind => '<datalist id="dl-' + kind + '">' +
      disks[kind].map(f => '<option value="' + esc(f.name) + '">').join('') +
      '</datalist>').join('');
}
// --------------------------------------------------------- system view
function meter(label, used, total, extra) {
  const pct = total ? used / total * 100 : 0;
  return '<div style="margin:.5em 0"><div style="display:flex">' +
    '<span style="color:#8b9298">' + label + '</span>' +
    '<span style="flex:1"></span><span>' + pct.toFixed(1) + '% (' +
    fmtBytes(used) + ' of ' + fmtBytes(total) + ')' +
    (extra ? ' <span class="note">' + extra + '</span>' : '') + '</span>' +
    '</div><div style="height:5px;background:#1b1e21;border:1px solid ' +
    'var(--line);margin-top:.2em"><div style="height:100%;width:' +
    Math.min(100, pct).toFixed(1) + '%;background:var(--cpu)"></div>' +
    '</div></div>';
}

async function systemView() {
  const f = facts.hostname ? facts : (await api('/api/facts') || {});
  const h = hostFacts;
  const cpuPct = history.cpu.at(-1) ?? 0;
  const running = instances.filter(i => i.running).length;
  const rows = [
    ['CPU(s)', (f.cores || '?') + ' x ' + (f.cpu_model || 'unknown')],
    ['Kernel version', f.kernel || ''],
    ['Operating system', f.os || '-'],
    ['Boot mode', f.boot || ''],
    ['Hardware virtualisation', f.kvm ? 'KVM available'
                                      : 'not available (TCG only)'],
    ['QEMU', f.qemu || 'not found'],
    ['PC-98 BIOS', f.roms || '-'],
    ['Storage root', f.root || '-'],
    ['Python', f.python || '-'],
  ].filter(([, v]) => v);              // what a platform has nothing to say
  if (f.build) rows.unshift(['Build', f.build]);
  if (f.addresses && f.addresses.length)
    rows.unshift(['Addresses', f.addresses.join(', ')]);
  const up = h.uptime == null ? '-' :
    Math.floor(h.uptime / 86400) + 'd ' +
    String(Math.floor(h.uptime % 86400 / 3600)).padStart(2, '0') + ':' +
    String(Math.floor(h.uptime % 3600 / 60)).padStart(2, '0');
  view.innerHTML =
    '<div class="crumb">Host</div>' +
    '<div class="titlerow"><span class="glyph">&#9635;</span>' +
    '<h2>' + esc(f.hostname || 'host') + '</h2>' +
    '<span class="state on">connected</span>' +
    '<span style="flex:1"></span>' +
    '<span class="note">uptime ' + up + '</span></div>' +
    actionBar([
      '<button class="primary" onclick="openCreate()">Create VM</button>',
      '<button onclick="location.hash=\'#/vms\'">Virtual machines</button>',
      '<button onclick="location.hash=\'#/storage\'">Storage</button>',
      f.platform === 'windows' ? '' :
        '<button onclick="location.hash=\'#/network\'">Networking</button>',
      f.platform === 'windows' ? '' :
        '<button onclick="location.hash=\'#/shell\'">Shell</button>',
      '<button onclick="render()">Refresh</button>']) +
    '<div class="grid2" style="grid-template-columns:1fr 22em">' +
    '<div class="card"><h3>Configuration</h3><table>' +
    rows.map(([k, v]) => '<tr><td style="width:15em;color:#8d99a5">' + k +
      '</td><td>' + esc(v) + '</td></tr>').join('') + '</table></div>' +
    '<div class="card"><h3>Resource consumption</h3><div class="body">' +
    meterBar('CPU', cpuPct, 100,
             cpuPct.toFixed(0) + '% of ' + (f.cores || '?') + ' CPUs') +
    meterBar('Memory', h.mem_used || 0, h.mem_total || 1) +
    meterBar('Storage', (h.disk_total || 0) - (h.disk_free || 0),
             h.disk_total || 1) +
    '<div class="note" style="margin-top:.4em">Load average ' +
    (h.load ? h.load.join(', ') : '-') + '<br>Virtual machines: ' +
    running + ' running of ' + instances.length + '</div>' +
    '</div></div></div>' +
    '<div class="card"><h3>Performance</h3><div class="body">' +
    '<div class="graphs">' +
    graph('% CPU', cpuPct.toFixed(0) + '%', history.cpu, 'var(--cpu)', 100,
          ['100','50','0']) +
    graph('Memory', fmtBytes(h.mem_used || 0), history.mem, 'var(--mem)',
          h.mem_total || 1,
          [fmtBytes(h.mem_total || 0), fmtBytes((h.mem_total || 0) / 2),
           '0']) +
    '</div></div></div>';
}

// ------------------------------------------------------ system settings
async function settingsView() {
  const s = await api('/api/storage-config');
  if (!s) return;
  const here = s.chosen.uuid || s.chosen.path || '';
  const rows = s.choices.map(v => {
    const chosen = v.id === here;
    return '<tr><td>' + esc(v.path) + '</td><td>' + esc(v.fstype) +
      '</td><td>' + esc(v.label || '') + '</td><td>' + fmtBytes(v.size) +
      '</td><td class="note">' + esc(v.note || '') +
      '</td><td style="text-align:right">' +
      (chosen ? '<span class="state on">in use for data</span>'
       : '<button onclick="chooseStorage(\'' + esc(jsq(v.id)) + '\',\'' +
         esc(jsq(v.path)) + '\')">Use for data</button>') + '</td></tr>';
  }).join('');
  view.innerHTML =
    '<div class="crumb"><a href="#/">' + esc(facts.hostname || 'host') +
    '</a> &rsaquo; System Settings</div>' +
    '<div class="titlerow"><span class="glyph">&#9881;</span>' +
    '<h2>System Settings</h2></div>' +
    actionBar(['<button onclick="render()">Refresh</button>']) +
    '<div class="card"><h3>Data storage</h3><table>' +
    '<tr><td style="width:12em;color:#8d99a5">In use</td><td>' +
    esc(s.root) + (s.fstype ? ' (' + esc(s.fstype) + ')' : '') +
    (s.on_stick ? ' <span class="note">' + esc(s.default.what) + '</span>'
                : '') +
    '</td></tr>' +
    '<tr><td style="color:#8d99a5">Settings</td><td>' + esc(s.settings) +
    '</td></tr></table>' +
    '<h4>' + (s.platform === 'windows' ? 'Drives' : 'Filesystems') +
    '</h4><table>' +
    '<tr><th>' + (s.platform === 'windows' ? 'Drive' : 'Device') +
    '</th><th>Type</th><th>Label</th><th>Size</th>' +
    '<th>Note</th><th></th></tr>' +
    (rows || '<tr><td colspan="6" class="note">none found</td></tr>') +
    '</table><div class="body">' +
    (s.on_stick ? '' : '<button onclick="chooseStorage(\'\',\'' +
     esc(jsq(s.default.path)) + '\')">Move back to ' +
     esc(s.default.what) + '</button>') +
    '<div class="note" style="margin-top:.5em">' +
    (s.platform === 'windows'
     ? 'A Mirai98 folder is made on the drive that is picked, and only ' +
       'the choice is kept beside the program. FAT32: no file over 4 GB.'
     : 'Machines live at the root of the chosen filesystem. Only the ' +
       'choice is kept on the boot medium. ext4 and exFAT: no limits. ' +
       'NTFS: needs a full Windows shutdown. FAT32: no file over 4 GB.') +
    '</div></div></div>' +
    passwordCard(s) + await persistCard() + await installCard();
}

// ------------------------------------------------------- the password
function passwordCard(s) {
  const win = s.platform === 'windows';
  return '<div class="card"><h3>Password</h3><div class="body">' +
    '<div class="note" style="margin-bottom:.6em">' +
    (s.password
     ? (win ? 'Set. This console asks for it.'
            : 'Set. The web console, ssh and the shell all ask for it.')
     : 'Not set. Anyone on this network can drive this host.') +
    '</div><form onsubmit="return savePassword(this)">' +
    (s.password
     ? '<div class="row"><label>Current</label>' +
       '<input type="password" name="current"></div>' : '') +
    '<div class="row"><label>New password</label>' +
    '<input type="password" name="password"></div>' +
    '<div class="row"><label></label><button class="primary">Set</button>' +
    (s.password ? '<button type="button" onclick="clearPassword()">' +
     'Remove the password</button>' : '') + '</div>' +
    '<div class="note">' +
    (win ? 'It guards this console. Windows accounts are left alone.'
         : 'This is root\'s own password, so the shell asks for it too. ' +
           'A live system forgets it at every boot, so its hash is kept ' +
           'on the boot medium and put back at start-up. Changing it ' +
           'from a shell leaves this page out of step.') +
    '</div></form></div></div>';
}
window.savePassword = form => {
  const body = {};
  for (const el of form.elements) if (el.name) body[el.name] = el.value;
  if (!body.password) { toast('type a password'); return false; }
  api('/api/password', {method: 'POST', body: JSON.stringify(body)})
    .then(r => { if (r) { toast('password ' + r.result);
                          task('Root password', r.result); }
                 render(); });
  return false;
};
window.clearPassword = () => {
  const current = prompt('current password');
  if (current === null) return;
  if (!confirm('Remove the password? Anyone on this network could then ' +
               'drive this host.')) return;
  api('/api/password', {method: 'POST',
                        body: JSON.stringify({current, password: ''})})
    .then(r => { if (r) { toast('password cleared'); location.reload(); } });
};

// ----------------------------------- the persistent system image and apt
async function persistCard() {
  if (facts.platform === 'windows') return '';
  const e = await api('/api/extension');
  if (!e) return '';
  const job = e.updates[e.updates.length - 1];
  const canUpdate = e.present && e.active;
  return '<div class="card"><h3>Persistent System Image</h3><table>' +
    '<tr><td style="width:12em;color:#8d99a5">Image</td><td>' +
    (e.present ? esc(e.path) + ' (' + fmtBytes(e.size) + ')'
               : 'none: system changes stay in RAM') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">This boot</td><td>' +
    (e.active ? 'using the image'
              : 'RAM overlay' + (e.present ? ' — reboot to use the image'
                                           : '')) + '</td></tr>' +
    '</table><div class="body"><div class="row">' +
    (e.present
     ? '<button onclick="dropExtension()">Remove the image</button>'
     : '<button class="primary" onclick="makeExtension()">' +
       'Create the image</button>') +
    '<button' + (canUpdate ? '' : ' disabled title="needs the image, ' +
     'and a boot that uses it"') + ' onclick="runUpdate()">' +
    'Update the system</button></div>' +
    '<div class="note">Keeps everything apt installs. ' +
    'The boot menu always has a second entry that ignores it. ' +
    'A new kernel is copied onto the boot medium afterwards.</div>' +
    '</div></div>' +
    (job ? '<div class="card"><h3>Update</h3><div class="body">' +
      '<div class="note">' + esc(job.state) + '</div>' +
      '<pre id="update-out" style="max-height:22em;overflow:auto;' +
      'background:#15181a;border:1px solid var(--line);padding:.6em;' +
      'font-size:12px;white-space:pre-wrap">' +
      esc((job.lines || []).join('\n')) + '</pre></div></div>' : '');
}
window.makeExtension = () => {
  const size = prompt('size of the persistent system image, in MB', '2048');
  if (!size) return;
  toast('making the image...');
  api('/api/extension', {method: 'POST',
                         body: JSON.stringify({action: 'create',
                                               size: parseInt(size, 10)})})
    .then(r => { if (r) { toast(r.result);
                          task('Persistent System Image - create', 'OK'); }
                 render(); });
};
window.dropExtension = () => {
  if (!confirm('Remove the image? Everything installed into it goes ' +
               'with it.')) return;
  api('/api/extension', {method: 'POST',
                         body: JSON.stringify({action: 'remove'})})
    .then(r => { if (r) { toast(r.result);
                          task('Persistent System Image - remove', 'OK'); }
                 render(); });
};
window.runUpdate = () => {
  if (!confirm('Run apt update, upgrade and dist-upgrade now?')) return;
  api('/api/update', {method: 'POST'})
    .then(r => { if (r) { toast('updating');
                          task('System update', 'started');
                          watchUpdate(); } });
};
async function watchUpdate() {
  const e = await api('/api/extension');
  const job = e && e.updates[e.updates.length - 1];
  const box = document.getElementById('update-out');
  if (!job) return;
  if (box) box.textContent = (job.lines || []).join('\n');
  else { await settingsView(); }
  if (box) box.scrollTop = box.scrollHeight;
  if (job.state === 'running') setTimeout(watchUpdate, 2000);
  else { toast('update ' + job.state); render(); }
}
window.chooseStorage = (id, where) => {
  if (!confirm('Keep the machines on ' + where + '?\n\n' +
               'Every machine must be stopped first.')) return;
  const copy = confirm('Copy what is here now to that drive?');
  api('/api/storage-config',
      {method: 'POST', body: JSON.stringify({storage: id, copy})})
    .then(r => { if (r) { toast(r.result);
                          task('Data storage → ' + where, 'saved'); }
                 render(); });
};

// ---------------------------------- install to a disk, inside settings
let installRunning = false;
async function installCard() {
  if (facts.platform === 'windows') return '';
  const d = await api('/api/install');
  if (!d) return '';
  const job = d.jobs[d.jobs.length - 1];
  installRunning = !!job && job.state === 'running';
  const rows = d.targets.map(t =>
    '<tr><td>' + esc(t.path) + '</td><td>' + esc(t.size) + '</td>' +
    '<td class="note">' + esc(t.model || '') +
    (t.removable ? ' (removable)' : '') + '</td>' +
    '<td>' + (t.busy ? '<span class="note">in use: ' + esc(t.busy) +
                       '</span>' : 'free') + '</td>' +
    '<td style="text-align:right">' +
    (t.busy ? '' : '<button onclick="installTo(\'' + t.path +
     '\')">Install here</button>') + '</td></tr>').join('');
  return '<div class="card"><h3>Install to a Disk</h3><table>' +
    '<tr><th>Device</th><th>Size</th><th>Model</th><th>State</th>' +
    '<th></th></tr>' +
    (rows || '<tr><td colspan="5" class="note">no disks</td></tr>') +
    '</table><div class="body">' +
    (job
     ? '<div>' + esc(job.message || job.state) + '</div>' +
       '<progress max="' + job.total + '" value="' + job.done +
       '" style="width:100%;height:1.1em;margin:.6em 0"></progress>' +
       (job.error ? '<div style="color:#e06c5f">' + esc(job.error) +
                    '</div>' : '')
     : '') +
    '<div class="note">One disk, one system. The disk is wiped and laid ' +
    'out like the stick: a system partition and the rest as data. BIOS ' +
    'and UEFI are both installed. ' +
    (d.medium ? '' : 'The live medium is missing: install is not ' +
                     'possible from this boot.') +
    '</div></div></div>';
}
window.installTo = device => {
  if (!confirm('Everything on ' + device + ' will be destroyed.\n\n' +
               'Install Mirai98 there?')) return;
  const copy = confirm('Copy the machines and images from this system to ' +
                       'the new disk as well?');
  api('/api/install', {method: 'POST',
                       body: JSON.stringify({device, copy_data: copy})})
    .then(r => { if (r) { toast('installing to ' + device);
                          task('Install to ' + device, 'started'); }
                 settingsView(); });
};

// ------------------------------------------------------------ log view
let logKind = '';
async function logView() {
  const data = await api('/api/log' + (logKind ? '?kind=' + logKind : ''));
  if (!data) return;
  const html = '<pre style="margin:0;white-space:pre-wrap;' +
    'overflow-wrap:anywhere">' + esc(data.lines.join('')) + '</pre>';
  const box = document.getElementById('log-box');
  if (box && document.getElementById('log-count')) {
    box.innerHTML = html;
    document.getElementById('log-count').textContent =
      data.lines.length + ' lines';
    return;
  }
  view.innerHTML = '<div class="topbar"><h2>System log</h2>' +
    '<span class="note" id="log-count">' + data.lines.length +
    ' lines</span><span style="flex:1"></span>' +
    ['', ...data.kinds].map(k =>
      '<button onclick="logFilter(\'' + k + '\')"' +
      (k === logKind ? ' class="primary"' : '') + '>' +
      (k || 'all') + '</button>').join('') +
    '</div><div class="card"><div class="body" id="log-box" ' +
    'style="max-height:68vh;overflow:auto;font-family:ui-monospace,' +
    'monospace;font-size:12px">' + html + '</div></div>' +
    '<div class="note">Cleared at boot. Trimmed to 1000 lines at 1 MB.' +
    '</div>';
  const b = document.getElementById('log-box');
  b.scrollTop = b.scrollHeight;
}
window.logFilter = kind => { logKind = kind;
                             view.innerHTML = ''; logView(); };

// -------------------------------------------------------- network view
async function networkView() {
  const n = await api('/api/network');
  if (!n) return;
  const c = n.config;
  view.innerHTML =
    '<div class="topbar"><h2>Network</h2>' +
    '<span class="note">the hypervisor\'s own address</span></div>' +
    '<div class="card"><h3>Interfaces</h3><table>' +
    '<tr><th>Name</th><th>State</th><th>Addresses</th><th>MAC</th></tr>' +
    (n.interfaces.map(i => '<tr><td>' + esc(i.name) + '</td>' +
      '<td><span class="state' + (i.state === 'UP' ? ' on' : '') + '">' +
      esc(i.state.toLowerCase()) + '</span></td><td>' +
      esc(i.addresses.join(', ') || '-') + '</td><td class="note">' +
      esc(i.mac) + '</td></tr>').join('') ||
     '<tr><td colspan="4" class="note">no interfaces found</td></tr>') +
    '</table></div>' +
    '<div class="card"><h3>Address</h3><div class="body">' +
    '<form onsubmit="return saveNetwork(this)">' +
    '<div class="row"><label>LAN bridge</label>' +
    '<label class="check"><input type="checkbox" name="bridge"' +
    (c.bridge === 'off' ? '' : ' checked') + '> put the wired interface ' +
    'into ' + esc(n.bridge.name) + ' so guests can sit on the LAN' +
    '</label></div>' +
    '<div class="row"><label></label><span class="note">' +
    (n.bridge.exists
     ? esc(n.bridge.name) + ' is up with ' +
       (n.bridge.members.join(', ') || 'no members yet')
     : esc(n.bridge.name) + ' does not exist yet') + '</span></div>' +
    '<div class="row"><label>Mode</label><select name="mode" ' +
    'onchange="netMode(this.value)">' +
    '<option value="dhcp"' + (c.mode === 'static' ? '' : ' selected') +
    '>DHCP</option><option value="static"' +
    (c.mode === 'static' ? ' selected' : '') + '>Static</option>' +
    '</select></div>' +
    '<div id="static-rows" style="display:' +
    (c.mode === 'static' ? '' : 'none') + '">' +
    '<div class="row"><label>Address</label>' +
    '<input type="text" name="address" value="' + esc(c.address) +
    '" placeholder="10.0.10.50/24"></div>' +
    '<div class="row"><label>Gateway</label>' +
    '<input type="text" name="gateway" value="' + esc(c.gateway) +
    '" placeholder="10.0.10.1"></div>' +
    '<div class="row"><label>Name servers</label>' +
    '<input type="text" name="dns" value="' + esc(c.dns) +
    '" placeholder="10.0.10.1 1.1.1.1"></div></div>' +
    '<div class="row"><label></label><button class="primary">Save and ' +
    'apply</button></div>' +
    '<div class="note">Saved on the data partition. Changing the ' +
    'address moves this page.</div></form></div></div>';
}
window.netMode = mode => {
  document.getElementById('static-rows').style.display =
    mode === 'static' ? '' : 'none';
};
window.saveNetwork = form => {
  const conf = {};
  for (const el of form.elements)
    if (el.name) conf[el.name] = el.type === 'checkbox' ? el.checked
                                                        : el.value.trim();
  if (conf.mode === 'static' &&
      !confirm('Move this host to ' + conf.address + '?\n\n' +
               'The page will stop answering here.')) return false;
  api('/api/network', {method: 'PATCH', body: JSON.stringify(conf)})
    .then(r => { if (r) { toast(r.result);
                          task('Host network - ' + conf.mode, 'saved'); } });
  return false;
};

// the shell is ttyd's own page, framed so it stays inside the console
async function shellView() {
  view.innerHTML = '<div class="topbar"><h2>Shell</h2>' +
    '<span class="note">the hypervisor host, not a guest</span>' +
    '<span style="flex:1"></span>' +
    '<button onclick="window.open(shellUrl())">Open in a tab</button>' +
    '</div><div class="card"><iframe id="shell-frame" src="' + shellUrl() +
    '" style="width:100%;height:72vh;border:0;background:#000"></iframe>' +
    '</div>';
}
window.shellUrl = () => 'http://' + location.hostname + ':7681/';

function route() {
  if (location.hash === '#/storage') return {view: 'storage'};
  if (location.hash === '#/shell') return {view: 'shell'};
  if (location.hash === '#/network') return {view: 'network'};
  if (location.hash === '#/log') return {view: 'log'};
  // install used to be its own page; it is a card in settings now
  if (location.hash === '#/install' ||
      location.hash === '#/settings') return {view: 'settings'};
  const disk = location.hash.match(
    /^#\/disk\/(hdd|fdd|cdrom)\/(.+)$/);
  if (disk) return {view: 'disk', kind: disk[1],
                    name: decodeURIComponent(disk[2])};
  if (location.hash === '#/vms') return {view: 'list'};
  const m = location.hash.match(/^#\/vm\/([A-Za-z0-9_-]+)$/);
  return m ? {view: 'vm', name: m[1]} : {view: 'system'};
}
async function render() {
  await refreshFleet();
  await refreshDisks();
  await pollHost();
  const r = route();
  drawTree();
  if (r.view === 'vm') { await detailView(r.name); return; }
  disconnectConsole();
  if (r.view === 'disk') await diskView(r.kind, r.name);
  else if (r.view === 'storage') await storageView();
  else if (r.view === 'shell') await shellView();
  else if (r.view === 'network') await networkView();
  else if (r.view === 'log') await logView();
  else if (r.view === 'settings') await settingsView();
  else if (r.view === 'list') await listView(true);
  else await systemView();
}
window.addEventListener('hashchange', () => { disconnectConsole();
                                              render(); });
document.getElementById('whereami').textContent = location.host;
task('Mirai98 console opened', 'OK');

// the browser remembers its own choice; a browser that has never been
// here follows the one made during setup
async function startLang() {
  try { lang = localStorage.getItem('mirai98-lang') || ''; } catch (err) {}
  if (!lang) {
    const f = await api('/api/facts');
    lang = (f && f.lang) || 'en';
    try { localStorage.setItem('mirai98-lang', lang); } catch (err) {}
  }
  document.documentElement.lang = lang;
  document.getElementById('lang-pick').value = lang;
  if (lang === 'ja') translateNode(document.body);
}
startLang().then(render);
setInterval(() => {
  const r = route();
  pollHost();
  if (r.view === 'list') listView();
  else if (r.view === 'vm') updateUsage(r.name);
  else if (r.view === 'system') systemView();
  else if (r.view === 'storage') pollJobs();
  else if (r.view === 'log') logView();
  else if (r.view === 'settings' && installRunning) settingsView();
}, 3000);
</script>
</body>
</html>
"""


# ------------------------------------------------------------ the server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def reply(self, code, body, ctype="application/json"):
        blob = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def fail(self, code, message):
        self.reply(code, {"error": message})

    def refuse(self, code, message):
        """Turn a request away without reading what it was sending.

        The body still has to go somewhere: left in the socket it would
        be read as the next request line, and every later request on
        that connection comes back nonsense.
        """
        try:
            left = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            left = 0
        if left > (8 << 20):
            self.close_connection = True     # too much to swallow politely
        else:
            while left > 0:
                chunk = self.rfile.read(min(1 << 16, left))
                if not chunk:
                    break
                left -= len(chunk)
        self.fail(code, message)

    # --------------------------------------------------------- the lock
    def signed_in(self):
        if not password_set():
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "mirai98" and value in _sessions:
                return True
        return False

    def sign_in(self, token):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Set-Cookie",
                         "mirai98=%s; Path=/; HttpOnly; SameSite=Strict"
                         % token)
        body = json.dumps({"result": "welcome"}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def shown(self, inst):
        out = dict(inst)
        out["running"] = is_running(inst)
        out["ports"] = ports_of(inst)
        return out

    # ------------------------------------------------------------- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        if not setup_done():
            if path == "/":
                self.reply(200, SETUP.encode(), "text/html; charset=utf-8")
            elif path == "/api/setup":
                default = default_storage()
                grown = grown_by()
                self.reply(200, {
                    "platform": PLATFORM,
                    "default": default,
                    "grown": human_mb(grown) if grown else "",
                    "choices": data_choices(),
                })
            else:
                self.fail(409, "finish setting up first")
            return
        if not self.signed_in():
            if path == "/":
                self.reply(200, LOGIN.encode(), "text/html; charset=utf-8")
            elif path.startswith("/novnc/"):
                self.static(path[len("/novnc/"):])
            else:
                self.fail(401, "sign in first")
            return
        if path == "/":
            self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path == "/api/instances":
            with _lock:
                instances = load_instances()
            self.reply(200, [self.shown(i) for i in instances])
        elif path == "/api/disks":
            with _lock:
                self.reply(200, disk_catalog())
        elif path == "/api/roms":
            self.reply(200, {"roms": rom_catalog(), "dir": CONFIG["roms"]})
        elif path == "/api/hardware":
            self.reply(200, {"drives": host_drives(),
                             "serial": serial_ports()})
        elif path == "/api/extension":
            state = extension_state()
            state["updates"] = [j for j in _jobs.values()
                                if j.get("kind") == "update"]
            self.reply(200, state)
        elif path == "/api/storage-config":
            chosen = read_settings().get("data") or {}
            self.reply(200, {
                "platform": PLATFORM,
                "choices": data_choices(),
                "chosen": chosen,
                "root": storage_root(),
                "fstype": chosen.get("fstype") or
                          ("" if WINDOWS else fs_of(storage_root())),
                "default": default_storage(),
                # on the appliance nothing chosen means the medium; on
                # Windows the default is a path like any other
                "on_stick": (os.path.abspath(chosen.get("path") or BASE) ==
                             os.path.abspath(BASE)) if WINDOWS
                            else not chosen.get("uuid"),
                "settings": settings_path(),
                "password": password_set(),
            })
        elif path == "/api/install":
            self.reply(200, {"targets": install_targets(),
                             "medium": os.path.isdir(
                                 os.path.join(MEDIUM, "live")),
                             "jobs": [j for j in _jobs.values()
                                      if j.get("kind") == "install"]})
        elif path == "/api/host":
            self.reply(200, host_usage())
        elif path == "/api/facts":
            facts = dict(host_facts())
            # the language chosen during setup, for a browser that has
            # never been here before
            facts["lang"] = read_settings().get("lang") or "en"
            self.reply(200, facts)
        elif path == "/api/network":
            exists, members = bridge_state()
            self.reply(200, {"config": read_network(),
                             "interfaces": interfaces(),
                             "bridge": {"name": BRIDGE, "exists": exists,
                                        "members": members,
                                        "uplink": uplink()}})
        elif path == "/api/jobs":
            self.reply(200, list(_jobs.values()))
        elif path == "/api/log":
            from urllib.parse import parse_qs, urlparse
            kind = (parse_qs(urlparse(self.path).query).get("kind")
                    or [""])[0]
            self.reply(200, {"lines": read_log(kind=kind),
                             "kinds": LOG_KINDS})
        elif path.startswith("/api/instances/"):
            self.api_get(path[len("/api/instances/"):])
        elif path.startswith("/disks/"):
            self.download(path[len("/disks/"):])
        elif path.startswith("/api/disk/"):
            self.disk_page(path[len("/api/disk/"):])
        elif path.startswith("/zip/"):
            self.send_zip(path[len("/zip/"):])
        elif path.startswith("/thumb/"):
            self.thumb(path[len("/thumb/"):])
        elif path.startswith("/novnc/"):
            self.static(path[len("/novnc/"):])
        else:
            self.fail(404, "no such page")

    def disk_ref(self, rest):
        """(kind, name, full path) out of a disks/<kind>/<name> URL."""
        kind, _, name = rest.partition("/")
        if kind not in DISK_KINDS or not SAFE_NAME.match(name):
            return None
        return kind, name, os.path.join(disks_root(kind), name)

    def disk_page(self, rest):
        """One image: its facts, and what is inside it if that can be
        worked out at all."""
        ref = self.disk_ref(rest)
        if ref is None or not os.path.isfile(ref[2]):
            self.fail(404, "no such disk")
            return
        kind, name, full = ref
        st = os.stat(full)
        used = sorted(i["name"] for i in load_instances()
                      if any(disk_path(i, k) == full for k in DISK_KEYS))
        out = {"kind": kind, "name": name, "size": st.st_size,
               "mtime": int(st.st_mtime), "used_by": used,
               "format": os.path.splitext(name)[1].lstrip(".").lower()
                         or "raw",
               "path": full}
        try:
            out.update(disk_contents(kind, name))
        except Exception as err:
            out["error"] = str(err)
        self.reply(200, out)

    def send_zip(self, rest):
        ref = self.disk_ref(rest)
        if ref is None or not os.path.isfile(ref[2]):
            self.fail(404, "no such disk")
            return
        kind, name, _full = ref
        try:
            archive = disk_zip(kind, name)
        except Exception as err:
            self.fail(400, "could not read the image: %s" % err)
            return
        try:
            size = os.path.getsize(archive)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s.zip"'
                             % os.path.splitext(name)[0])
            self.end_headers()
            with open(archive, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        finally:
            os.remove(archive)

    def download(self, rest):
        ref = self.disk_ref(rest)
        if ref is None or not os.path.isfile(ref[2]):
            self.fail(404, "no such disk")
            return
        _kind, name, full = ref
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % name)
        self.end_headers()
        with open(full, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    def api_get(self, rest):
        name, _, extra = rest.partition("/")
        with _lock:
            inst = find_instance(load_instances(), name)
        if inst is None:
            self.fail(404, "no such instance")
        elif extra == "":
            self.reply(200, self.shown(inst))
        elif extra == "media":
            self.reply(200, {"drives": media_devices(inst)
                             if is_running(inst) else []})
        elif extra == "stats":
            usage = usage_of(inst) if is_running(inst) else None
            out = {"running": usage is not None}
            out.update(usage or {})
            self.reply(200, out)
        else:
            self.fail(404, "no such page")

    def thumb(self, rest):
        name = rest[:-4] if rest.endswith(".png") else rest
        with _lock:
            inst = find_instance(load_instances(), name)
        if inst is None:
            self.fail(404, "no such instance")
            return
        path = thumbnail(inst)
        if not path:
            self.fail(404, "no thumbnail yet")
            return
        with open(path, "rb") as f:
            self.reply(200, f.read(), "image/png")

    def static(self, rel):
        base = os.path.realpath(CONFIG["novnc"])
        full = os.path.realpath(os.path.join(base, rel))
        if not full.startswith(base + os.sep):
            self.fail(403, "outside novnc/")
            return
        try:
            with open(full, "rb") as f:
                blob = f.read()
        except OSError:
            self.fail(404, "not found")
            return
        types = {".html": "text/html", ".js": "text/javascript",
                 ".css": "text/css", ".svg": "image/svg+xml",
                 ".png": "image/png", ".json": "application/json"}
        ctype = types.get(os.path.splitext(full)[1],
                          "application/octet-stream")
        self.reply(200, blob, ctype)

    # ------------------------------------------------------- POST / PUT
    def body_json(self):
        try:
            size = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(size))
        except ValueError:
            return None

    def do_POST(self):
        path = self.path.split("?")[0]
        if not setup_done():
            if path == "/api/setup":
                self.run_setup()
            else:
                self.refuse(409, "finish setting up first")
            return
        if path == "/api/login":
            data = self.body_json() or {}
            if not password_ok(str(data.get("password") or "")):
                time.sleep(1)             # a wrong guess costs a second
                self.fail(403, "wrong password")
                return
            token = secrets.token_urlsafe(32)
            _sessions.add(token)
            self.sign_in(token)
            return
        if not self.signed_in():
            self.refuse(401, "sign in first")
            return
        if path == "/api/lang":
            data = self.body_json() or {}
            lang = "ja" if str(data.get("lang") or "") == "ja" else "en"
            write_settings({"lang": lang})
            self.reply(200, {"result": lang})
            return
        if path == "/api/password":
            data = self.body_json() or {}
            if password_set() and not password_ok(
                    str(data.get("current") or "")):
                self.fail(403, "the current password does not match")
                return
            new = str(data.get("password") or "")
            if new and len(new) < 4:
                self.fail(400, "give it at least four characters")
                return
            try:
                set_password(new)
            except Exception as err:
                self.fail(500, str(err))
                return
            self.reply(200, {"result": "set" if new else "cleared"})
            return
        if path == "/api/instances":
            self.create()
            return
        if path == "/api/roms":
            self.upload_rom()
            return
        m = re.match(r"^/api/disk/(hdd|fdd|cdrom)/([^/]+)/"
                     r"(unzip|copy|to-drive)$", path)
        if m:
            self.disk_action(*m.groups())
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/from-drive$", path)
        if m:
            self.from_drive(m.group(1))
            return
        if path == "/api/storage-config":
            self.choose_storage()
            return
        if path == "/api/extension":
            data = self.body_json() or {}
            verb = str(data.get("action") or "")
            if verb == "create":
                size = max(256, min(16384, int(data.get("size") or 2048)))
                try:
                    result = make_extension(size)
                except Exception as err:
                    self.fail(500, "could not make it: %s" % err)
                    return
            elif verb == "remove":
                try:
                    os.remove(extension_path())
                except OSError as err:
                    self.fail(404, str(err))
                    return
                set_persistence(False)
                say("removed the system extension", "system")
                result = "removed; the next boot goes back to RAM"
            else:
                self.fail(400, "action must be create or remove")
                return
            self.reply(200, {"result": result})
            return
        if path == "/api/update":
            state = extension_state()
            if not state["present"]:
                self.fail(409, "make the system extension first")
                return
            if not state["active"]:
                self.fail(409, "reboot first: this boot is still on RAM")
                return
            self.reply(200, {"result": "updating",
                             "job": start_update()["name"]})
            return
        if path == "/api/install":
            data = self.body_json() or {}
            job = install_to_disk(str(data.get("device") or ""),
                                  bool(data.get("copy_data")))
            if job["state"] == "failed":
                self.fail(409, job["error"])
            else:
                self.reply(200, {"result": "installing"})
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/chunk$", path)
        if m:
            self.upload_chunk(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/finish$", path)
        if m:
            self.finish_upload(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)$", path)
        if m:
            self.upload(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/import$", path)
        if m:
            self.import_disk(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/fetch$", path)
        if m:
            self.fetch(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd)/create$", path)
        if m:
            self.create_disk(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd)/convert$", path)
        if m:
            self.convert_disk(m.group(1))
            return
        m = re.match(r"^/api/instances/([^/]+)/media$", path)
        if m:
            data = self.body_json() or {}
            with _lock:
                inst = find_instance(load_instances(), m.group(1))
            if inst is None:
                self.fail(404, "no such instance")
                return
            if not is_running(inst):
                self.fail(409, "the machine is not running")
                return
            name = str(data.get("name") or "").strip()
            device = str(data.get("device") or "")
            # the drive says what sort of image belongs in it
            drive = next((d for d in media_devices(inst)
                          if d["device"] == device), None)
            if drive is None:
                self.fail(404, "no drive called %s" % device)
                return
            path_ = os.path.join(disks_root(drive["kind"]), name) \
                if name else ""
            if name and not os.path.isfile(path_):
                self.fail(404, "no %s image called %s"
                          % (drive["kind"], name))
                return
            self.reply(200, {"result": change_media(inst, device, path_)})
            return
        m = re.match(r"^/api/instances/([^/]+)/"
                     r"(start|stop|reset|delete|save|resume)$", path)
        if not m:
            self.fail(404, "no such action")
            return
        name, verb = m.groups()
        with _lock:
            instances = load_instances()
            inst = find_instance(instances, name)
            if inst is None:
                self.fail(404, "no such instance")
                return
            if verb == "start":
                result = start_instance(inst)
            elif verb == "stop":
                result = stop_instance(inst)
            elif verb == "reset":
                result = ("reset" if qmp(inst, "system_reset")
                          else "not running")
            elif verb == "save":
                result = save_state(inst)
            elif verb == "resume":
                result = resume_state(inst)
            else:                                       # delete
                if is_running(inst):
                    self.fail(409, "stop it first")
                    return
                shutil.rmtree(inst_dir(inst["index"]), ignore_errors=True)
                result = "deleted"
        self.reply(200, {"result": result})

    def refuse_upload(self, size, code, message):
        """Failing an upload means draining what the client is already
        sending, or the pipe breaks before it ever sees the answer."""
        if size <= (256 << 20):
            left = size
            while left > 0:
                chunk = self.rfile.read(min(1 << 20, left))
                if not chunk:
                    break
                left -= len(chunk)
        else:
            self.close_connection = True
        self.fail(code, message)

    def upload(self, kind):
        """A disk image, streamed straight from the browser to disks/."""
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        overwrite = (query.get("overwrite") or ["0"])[0] == "1"
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = 0
        if not SAFE_NAME.match(name):
            self.refuse_upload(size, 400, "name must be letters, digits, . _ -")
            return
        if size <= 0:
            self.fail(400, "empty upload")
            return
        dest = os.path.join(disks_root(kind), name)
        if os.path.exists(dest) and not overwrite:
            self.refuse_upload(size, 409, "%s already exists" % name)
            return
        if size + (512 << 20) > free_bytes(disks_root(kind)):
            self.refuse_upload(size, 507,
                               "not enough free space for %d bytes" % size)
            return
        part = dest + ".part"
        done = 0
        try:
            with open(part, "wb") as f:
                while done < size:
                    chunk = self.rfile.read(min(1 << 20, size - done))
                    if not chunk:
                        raise OSError("connection dropped")
                    f.write(chunk)
                    done += len(chunk)
            os.replace(part, dest)
        except OSError as err:
            try:
                os.remove(part)
            except OSError:
                pass
            self.fail(500, "upload failed: %s" % err)
            return
        final = self.auto_convert(kind, dest)
        self.reply(200, {"result": "uploaded", "size": done, "name": final})

    def disk_action(self, kind, name, verb):
        """Write a ZIP into an image, or duplicate the image itself."""
        if not SAFE_NAME.match(name):
            self.fail(400, "bad name")
            return
        source = os.path.join(disks_root(kind), name)
        if not os.path.isfile(source):
            self.fail(404, "no such disk")
            return
        if verb == "to-drive":
            data = self.body_json() or {}
            device = str(data.get("device") or "")
            job = drive_job("%s → %s" % (name, device), kind, name, device,
                            to_drive=True)
            if job["state"] == "failed":
                self.fail(409, job["error"])
                return
            self.reply(200, {"result": "writing", "job": job["name"]})
            return
        if verb == "copy":
            data = self.body_json() or {}
            target = str(data.get("name") or "").strip()
            if not SAFE_NAME.match(target):
                self.fail(400, "the copy needs a name")
                return
            dest = os.path.join(disks_root(kind), target)
            if os.path.exists(dest):
                self.fail(409, "%s already exists" % target)
                return
            if os.path.getsize(source) > free_bytes(disks_root(kind)):
                self.fail(507, "not enough room for a copy")
                return
            shutil.copy2(source, dest)
            say("copied %s to %s" % (name, target), "disk")
            self.reply(200, {"result": "copied", "name": target})
            return

        # unzip: the body is the archive itself
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = 0
        if size <= 0:
            self.fail(400, "send a ZIP file as the body")
            return
        used = [i["name"] for i in load_instances()
                if any(disk_path(i, k) == source for k in DISK_KEYS)
                and is_running(i)]
        if used:
            self.refuse_upload(size, 409,
                               "in use by a running machine: %s"
                               % ", ".join(used))
            return
        handle, archive = tempfile.mkstemp(prefix="pc98-in-", suffix=".zip")
        try:
            with os.fdopen(handle, "wb") as f:
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise OSError("connection dropped")
                    f.write(chunk)
                    left -= len(chunk)
            written = []
            zip_into_disk(kind, name, archive,
                          log=lambda *a: written.append(" ".join(
                              str(x) for x in a)))
        except Exception as err:
            self.fail(400, "could not write into the image: %s" % err)
            return
        finally:
            try:
                os.remove(archive)
            except OSError:
                pass
        say("wrote a ZIP into %s" % name, "disk")
        self.reply(200, {"result": "written", "log": written[-40:]})

    def run_setup(self):
        """The two answers from the first run, applied at once.

        The storage answer takes effect immediately, so the machines
        made in the next minute already land in the right place.
        """
        data = self.body_json() or {}
        password = str(data.get("password") or "")
        if password and len(password) < 4:
            self.fail(400, "give it at least four characters")
            return
        try:
            where = choose_data(str(data.get("storage") or "").strip())
        except Exception as err:
            self.fail(409, str(err))
            return
        if password:
            try:
                set_password(password)
            except Exception as err:
                self.fail(500, "storage is set, but the password is not: %s"
                          % err)
                return
        lang = "ja" if str(data.get("lang") or "") == "ja" else "en"
        write_settings({"setup": True, "lang": lang})
        start_log()
        say("set up: machines in %s, password %s"
            % (where, "set" if password else "not set"))
        seed_fleet()
        self.reply(200, {"result": "ready", "root": where})

    def choose_storage(self):
        """Move the machines to another filesystem, from this moment on.

        Only the choice is written, and it is written on the boot
        medium: the drive itself is never told it has been chosen, so
        nothing is lost if it is taken away.
        """
        data = self.body_json() or {}
        target = str(data.get("storage") or data.get("uuid") or "").strip()
        if any(is_running(i) for i in load_instances()):
            self.fail(409, "stop the machines first")
            return
        source = storage_root()
        try:
            where = choose_data(target)
        except Exception as err:
            self.fail(409, str(err))
            return
        result = "the machines now live in %s" % where
        if data.get("copy") and os.path.abspath(source) != \
                os.path.abspath(where):
            try:
                copied = copy_storage(source, where)
            except Exception as err:
                self.fail(500, "the drive is set, but copying failed: %s"
                          % err)
                return
            result += ", with %s copied from %s" % (human_mb(copied >> 20),
                                                    source)
        ensure_tree()
        say(result, "system")
        self.reply(200, {"result": result})

    def from_drive(self, kind):
        """Read a real drive into a new image."""
        data = self.body_json() or {}
        device = str(data.get("device") or "")
        name = str(data.get("name") or "").strip()
        if not SAFE_NAME.match(name):
            self.fail(400, "the image needs a name")
            return
        if os.path.exists(os.path.join(disks_root(kind), name)):
            self.fail(409, "%s already exists" % name)
            return
        job = drive_job("%s → %s" % (device, name), kind, name, device,
                        to_drive=False)
        if job["state"] == "failed":
            self.fail(409, job["error"])
            return
        self.reply(200, {"result": "reading", "job": job["name"]})

    def upload_target(self, kind):
        """(name, part path, query) for a chunked upload, or None."""
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)
        name = (query.get("name") or [""])[0]
        if not SAFE_NAME.match(name):
            return None, None, query
        return name, os.path.join(disks_root(kind), name + ".part"), query

    def upload_chunk(self, kind):
        """One slice of a large image.

        A whole 2 GB disk in one request asks the browser, the socket and
        every buffer along the way to behave for minutes at a time; a
        slice at a time survives, and what arrived stays on disk so an
        interrupted upload can pick up where it stopped.
        """
        name, part, query = self.upload_target(kind)
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = 0
        if name is None:
            self.refuse_upload(size, 400, "bad name")
            return
        try:
            offset = int((query.get("offset") or ["0"])[0])
            total = int((query.get("total") or ["0"])[0])
        except ValueError:
            self.refuse_upload(size, 400, "bad offset or total")
            return
        dest = os.path.join(disks_root(kind), name)
        overwrite = (query.get("overwrite") or ["0"])[0] == "1"
        if offset == 0:
            if os.path.exists(dest) and not overwrite:
                self.refuse_upload(size, 409, "%s already exists" % name)
                return
            if total + (256 << 20) > free_bytes(disks_root(kind)):
                self.refuse_upload(size, 507, "not enough free space for "
                                              "%d bytes" % total)
                return
            cap = size_limit(disks_root(kind))
            if cap and total > cap:
                self.refuse_upload(size, 507,
                                   "this storage is FAT32: no file over 4 GB")
                return
            try:
                os.remove(part)
            except OSError:
                pass
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset > have:
            self.refuse_upload(size, 409,
                               "expected the slice at %d, not %d"
                               % (have, offset))
            return
        try:
            with open(part, "r+b" if os.path.exists(part) else "wb") as f:
                f.seek(offset)
                left = size
                while left > 0:
                    chunk = self.rfile.read(min(1 << 20, left))
                    if not chunk:
                        raise OSError("connection dropped")
                    f.write(chunk)
                    left -= len(chunk)
        except OSError as err:
            self.fail(500, "write failed: %s" % err)
            return
        self.reply(200, {"result": "ok",
                         "have": os.path.getsize(part)})

    def finish_upload(self, kind):
        name, part, query = self.upload_target(kind)
        if name is None:
            self.fail(400, "bad name")
            return
        if not os.path.exists(part):
            self.fail(404, "nothing was uploaded")
            return
        try:
            total = int((query.get("total") or ["0"])[0])
        except ValueError:
            total = 0
        have = os.path.getsize(part)
        if total and have != total:
            self.fail(409, "got %d bytes of %d; upload again" % (have, total))
            return
        dest = os.path.join(disks_root(kind), name)
        os.replace(part, dest)
        final = self.auto_convert(kind, dest)
        say("uploaded %s (%d bytes)" % (final, have), "disk")
        self.reply(200, {"result": "uploaded", "name": final, "size": have})

    def auto_convert(self, kind, dest):
        """Uploaded containers become what the tree prefers: an HDI turns
        into qcow2, an FDI into a bare raw floppy.  Returns the name the
        upload ended up under; trouble just keeps the original."""
        stem, ext = os.path.splitext(dest)
        ext = ext.lower()
        if (kind, ext) == ("hdd", ".hdi"):
            target = stem + ".qcow2"
        elif (kind, ext) == ("fdd", ".fdi"):
            target = stem + ".raw"
        else:
            return os.path.basename(dest)
        if os.path.exists(target):
            return os.path.basename(dest)      # never clobber sideways
        try:
            import virtpc98
            payload = virtpc98.read_image(dest)[0]
            if target.endswith(".qcow2"):
                virtpc98.qcow2_write(target, payload, lambda *a: None)
            else:
                with open(target, "wb") as f:
                    f.write(payload)
            os.remove(dest)
        except Exception:
            try:
                os.remove(target)
            except OSError:
                pass
            return os.path.basename(dest)
        return os.path.basename(target)

    def upload_rom(self):
        """One file of a real machine's ROM set, kept beside the guests."""
        from urllib.parse import parse_qs, urlparse
        name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = 0
        if name not in ROM_FILES:
            self.refuse_upload(size, 400, "the ROM must be one of: %s"
                               % ", ".join(ROM_FILES))
            return
        if not 0 < size <= (4 << 20):
            self.refuse_upload(size, 400, "a ROM image is a few hundred "
                                          "kilobytes, not %d bytes" % size)
            return
        os.makedirs(CONFIG["roms"], exist_ok=True)
        dest = os.path.join(CONFIG["roms"], name)
        blob = b""
        while len(blob) < size:
            chunk = self.rfile.read(min(1 << 20, size - len(blob)))
            if not chunk:
                self.fail(500, "upload cut short")
                return
            blob += chunk
        try:
            with open(dest + ".part", "wb") as f:
                f.write(blob)
            os.replace(dest + ".part", dest)
        except OSError as err:
            self.fail(500, "could not save: %s" % err)
            return
        say("rom %s uploaded (%d bytes)" % (name, size), "system")
        self.reply(200, {"result": "uploaded", "name": name, "size": size})

    def import_disk(self, kind):
        """Adopt a file already on the server, without moving it."""
        data = self.body_json()
        path = os.path.expanduser(str((data or {}).get("path", "")))
        if not os.path.isfile(path):
            self.fail(400, "%s is not a file" % path)
            return
        name = os.path.basename(path)
        if not SAFE_NAME.match(name):
            self.fail(400, "file name %s is too strange to adopt" % name)
            return
        dest = os.path.join(disks_root(kind), name)
        if os.path.exists(dest):
            self.fail(409, "%s already exists" % name)
            return
        try:
            os.link(path, dest)            # instant on the same filesystem
        except OSError:
            shutil.copy2(path, dest)
        self.reply(200, {"result": "imported", "name": name})

    def fetch(self, kind):
        data = self.body_json() or {}
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self.fail(400, "the URL must start with http:// or https://")
            return
        name = str(data.get("name") or "").strip()
        if not name:
            from urllib.parse import unquote, urlparse
            name = os.path.basename(unquote(urlparse(url).path))
        if not SAFE_NAME.match(name):
            self.fail(400, "cannot tell what to call it; give a file name")
            return
        if os.path.exists(os.path.join(disks_root(kind), name)):
            self.fail(409, "%s already exists" % name)
            return
        fetch_disk(kind, name, url)
        self.reply(200, {"result": "downloading", "name": name})

    def do_PATCH(self):
        if self.path.split("?")[0] != "/api/network":
            self.fail(404, "no such page")
            return
        data = self.body_json() or {}
        conf = {"mode": "static" if data.get("mode") == "static" else "dhcp",
                "address": str(data.get("address") or "").strip(),
                "gateway": str(data.get("gateway") or "").strip(),
                "dns": str(data.get("dns") or "").strip(),
                "bridge": "off" if data.get("bridge") in (False, "off")
                          else "on"}
        if conf["mode"] == "static" and "/" not in conf["address"]:
            self.fail(400, "the address needs a prefix, like 10.0.0.5/24")
            return
        try:
            write_network(conf)
        except OSError as err:
            self.fail(500, "could not save: %s" % err)
            return
        # answer before the address moves, or the browser never hears back
        self.reply(200, {"result": "saved; applying"})
        threading.Timer(0.5, apply_network, args=(conf,)).start()

    def create_disk(self, kind):
        """A fresh, formatted image, made by virtpc98's builders."""
        data = self.body_json() or {}
        name = str(data.get("name") or "")
        if not SAFE_NAME.match(name):
            self.fail(400, "name must be letters, digits, . _ -")
            return
        dest = os.path.join(disks_root(kind), name)
        if os.path.exists(dest):
            self.fail(409, "%s already exists" % name)
            return
        cap = size_limit(disks_root(kind))
        wanted = int(data.get("size") or 40) << 20
        if kind == "hdd" and cap and wanted > cap:
            self.fail(507, "this storage is FAT32: no file over 4 GB")
            return
        quiet = lambda *a: None
        try:
            import virtpc98
            if kind == "hdd":
                image_kind = (virtpc98.KIND_QCOW2
                              if name.endswith(".qcow2") else None)
                virtpc98.new_image(
                    dest, megabytes=int(data.get("size") or 40),
                    label=str(data.get("label") or "NO NAME"),
                    kind=image_kind, fat32=bool(data.get("fat32")),
                    log=quiet)
            else:
                fmt = "1.44" if str(data.get("format")) == "1.44" else "1.2"
                virtpc98.new_image(dest, fmt=fmt,
                                   label=str(data.get("label") or "NO NAME"),
                                   log=quiet)
        except Exception as err:
            try:
                os.remove(dest)
            except OSError:
                pass
            self.fail(400, "create failed: %s" % err)
            return
        self.reply(200, {"result": "created", "name": name})

    # source extension and requested format pick the virtpc98 command
    CONVERSIONS = {
        ("hdd", ".hdi", "raw"): ("hdi-to-raw", ".raw"),
        ("hdd", ".raw", "hdi"): ("raw-to-hdi", ".hdi"),
        ("hdd", ".img", "hdi"): ("raw-to-hdi", ".hdi"),
        ("hdd", ".raw", "qcow2"): ("raw-to-qcow2", ".qcow2"),
        ("hdd", ".img", "qcow2"): ("raw-to-qcow2", ".qcow2"),
        ("hdd", ".qcow2", "raw"): ("qcow2-to-raw", ".raw"),
        ("fdd", ".fdi", "raw"): ("fdi-to-raw", ".raw"),
        ("fdd", ".raw", "fdi"): ("raw-to-fdi", ".fdi"),
        ("fdd", ".img", "fdi"): ("raw-to-fdi", ".fdi"),
    }

    def convert_disk(self, kind):
        data = self.body_json() or {}
        source = str(data.get("source") or "")
        target = str(data.get("format") or "")
        if not SAFE_NAME.match(source):
            self.fail(400, "bad source name")
            return
        stem, ext = os.path.splitext(source)
        plan = self.CONVERSIONS.get((kind, ext.lower(), target))
        if plan is None:
            self.fail(400, "cannot make %s out of %s" % (target, source))
            return
        command, out_ext = plan
        src = os.path.join(disks_root(kind), source)
        dest = os.path.join(disks_root(kind), stem + out_ext)
        if not os.path.isfile(src):
            self.fail(404, "no such disk")
            return
        if os.path.exists(dest):
            self.fail(409, "%s already exists" % os.path.basename(dest))
            return
        try:
            import virtpc98
            virtpc98.run_command(command, src, dest, {},
                                 log=lambda *a: None)
        except Exception as err:
            self.fail(400, "convert failed: %s" % err)
            return
        self.reply(200, {"result": "converted",
                         "name": os.path.basename(dest)})

    def do_DELETE(self):
        if not self.signed_in():
            self.refuse(401, "sign in first")
            return
        path = self.path.split("?")[0]
        if path.startswith("/api/roms/"):
            name = path[len("/api/roms/"):]
            if name not in ROM_FILES:
                self.fail(404, "no such ROM")
                return
            try:
                os.remove(os.path.join(CONFIG["roms"], name))
            except OSError as err:
                self.fail(404, str(err))
                return
            say("rom %s removed" % name, "system")
            self.reply(200, {"result": "deleted"})
            return
        ref = self.disk_ref(path.removeprefix("/api/disks/")) \
            if path.startswith("/api/disks/") else None
        if ref is None:
            self.fail(404, "no such page")
            return
        kind, name, full = ref
        with _lock:
            if not os.path.isfile(full):
                self.fail(404, "no such disk")
                return
            used = sorted(i["name"] for i in load_instances()
                          if any(disk_path(i, k) == full
                                 for k in DISK_KEYS))
            if used:
                self.fail(409, "in use by: %s" % ", ".join(used))
                return
            os.remove(full)
        self.reply(200, {"result": "deleted"})

    def do_PUT(self):
        if not self.signed_in():
            self.refuse(401, "sign in first")
            return
        m = re.match(r"^/api/instances/([^/]+)$", self.path.split("?")[0])
        if not m:
            self.fail(404, "no such page")
            return
        name = m.group(1)
        data = self.body_json()
        if data is None:
            self.fail(400, "bad JSON")
            return
        data["name"] = name
        with _lock:
            instances = load_instances()
            inst = find_instance(instances, name)
            if inst is None:
                self.fail(404, "no such instance")
                return
            if is_running(inst):
                self.fail(409, "shut it down first")
                return
            record, complaint = sanitize(data)
            if record is None:
                self.fail(400, complaint)
                return
            record["index"] = inst["index"]
            save_instance(record)
        self.reply(200, {"result": "saved"})

    def create(self):
        data = self.body_json()
        if data is None:
            self.fail(400, "bad JSON")
            return
        with _lock:
            instances = load_instances()
            record, complaint = sanitize(
                data, {i["name"] for i in instances})
            if record is None:
                self.fail(400, complaint)
                return
            record["index"] = next_index(instances)
            save_instance(record)
        self.reply(200, {"result": "created", "index": record["index"]})


def start_up():
    """What has to be true before the first request, per platform.

    Until the first run has been answered, nothing is laid out and no
    machine is seeded: the answer says where all of that goes.
    """
    if not setup_done():
        # a settings file from an older layout counts as answered, so an
        # existing stick does not ask again
        if read_settings().get("data") is not None:
            write_settings({"setup": True})
        else:
            say("waiting for the first run to say where the machines live")
            return False
    saved = read_settings().get("data") or {}
    if WINDOWS and saved.get("path"):
        use_storage(saved["path"])
    restore_password()
    ensure_tree()
    start_log()
    migrate_legacy()
    seed_fleet()
    if not WINDOWS:
        # whatever the stick was configured to be, it becomes again.
        # Nothing here may stop the server: a host that cannot be
        # reached over the network is exactly when the console is
        # needed
        conf = read_network()
        if os.geteuid() == 0:
            try:
                say("network: %s" % apply_network(conf, say), "network")
            except Exception as err:
                say("network: could not be applied: %s" % err, "network")
        elif conf != NETWORK_DEFAULTS:
            say("network: saved settings need root to apply; leaving as is",
                "network")
    return True


def main(argv):
    host, port = "0.0.0.0", 8098
    for arg in argv[1:]:
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1]
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg.startswith("--config="):
            with open(arg.split("=", 1)[1], encoding="utf-8") as f:
                CONFIG.update(json.load(f))
        else:
            print(__doc__)
            return 2
    if not os.path.isdir(CONFIG["novnc"]):
        # everything but the consoles works without it
        print("no noVNC in %s: consoles will not draw" % CONFIG["novnc"])
    ready = start_up()
    server = ThreadingHTTPServer((host, port), Handler)
    print("Mirai98 Hypervisor Platform OS (%s) on http://%s:%d"
          % (PLATFORM, host, port))
    print("  machines in %s" % CONFIG["root"] if ready else
          "  not set up yet: open the address above to choose where the "
          "machines live")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
