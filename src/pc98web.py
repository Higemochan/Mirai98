#!/usr/bin/env python3

"""
Mirai98 Hypervisor Platform OS
Copyright (C) 2026 Awe Morris

Manages a fleet of QEMU PC-98 instances from a browser: create, start,
stop, reconfigure, and watch each machine's screen through noVNC.
QEMU's own VNC server speaks WebSocket, so the browser connects to it
directly and this process never touches the pixel path.

  pc98web.py [--host=0.0.0.0] [--port=8098] [--config=pc98web.json]
             [--base=DIR] [--loopback] [--app-token] [--dev]
             [--parent-pid=N]

The page it serves is a set of plain files in web/, read once at startup;
--dev re-reads them per request, so an edit shows up on a reload.

The last three are for being started by a program rather than a person,
which is how the Windows application runs it:

  --base=DIR    where the settings and the machines go, instead of the
                directory it was started in
  --loopback    keep every listener on 127.0.0.1, each machine's VNC
                server included
  --app-token   invent a secret and report it, to be used in place of a
                password; the password is then no way in at all
  --port=0      let the operating system pick the port
  --parent-pid  stop the machines and quit once that process is gone, so
                that an application crashing does not leave them running

Either of the last two prints one machine-readable line once the server
is up, and nothing else is written to that line:

  MIRAI98-READY {"port": 54321, "token": "..."}

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

import drives

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
        # the page itself: index.html and what it pulls in
        "web": os.path.join(PROG, "web"),
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
        # in a checkout this sits beside pc98web.py; the appliance's own
        # config points at where the build put it
        "web": os.path.join(HERE, "web"),
        # a pre-rule instances.json is folded into the tree once, at startup
        "legacy": os.path.join(HERE, "instances.json"),
    }

CONFIG = dict(DEFAULTS)

# --dev: re-read the page from web/ on every request, so an edit shows up
# on a reload.  Off, the files are read once and kept.
DEV = False

# --loopback: keep every listener on 127.0.0.1, each machine's VNC server
# included.  An application running on the machine its user is sitting at
# has no reason to be on the network, and on Windows a loopback-only
# listener does not raise the firewall dialog.
LOOPBACK = False

# --app-token: a secret the program that started this one is told and
# nobody else is, in place of a password.  Without it any page a browser
# happens to have open could drive this API over 127.0.0.1, and this one
# writes to real disks.
APP_TOKEN = ""

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
DISK_KEYS = ("hdd1", "hdd2", "cd", "fdd1", "fdd2",
             "scsi1", "scsi2", "scsi3", "scsi4")
SCSI_KEYS = ("scsi1", "scsi2", "scsi3", "scsi4")
DISK_DIR = {"hdd1": "hdd", "hdd2": "hdd", "fdd1": "fdd", "fdd2": "fdd",
            "cd": "cdrom"}
NETWORKS = ("", "nat", "bridge")
MACHINES = ("pc9821", "pc9801")

# --- machine plugins -------------------------------------------------------
# Extra machine types (e.g. FM TOWNS) live as self-contained plugins under
# plugins/*.py, so the core stays a pure PC-98 manager.  A plugin's
# register(api) may add a machine name and a builder returning its QEMU argv;
# everything else (create/start/console/snapshot) is the unchanged PC-98 flow.
MACHINE_ARGV = {}


class PluginAPI:
    """The surface a machine plugin is given; names resolve lazily."""
    os = os

    @property
    def CONFIG(self):
        return CONFIG

    @property
    def LOOPBACK(self):
        return LOOPBACK

    def ports_of(self, inst):
        return ports_of(inst)

    def win_short(self, path):
        return win_short(path)

    def disk_path(self, inst, kind):
        return disk_path(inst, kind)

    def add_machine(self, name):
        global MACHINES
        if name not in MACHINES:
            MACHINES = MACHINES + (name,)

    def machine_argv(self, name, builder):
        MACHINE_ARGV[name] = builder


def load_plugins():
    import glob
    import importlib.util
    pdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugins")
    if not os.path.isdir(pdir):
        return
    api = PluginAPI()
    for path in sorted(glob.glob(os.path.join(pdir, "*.py"))):
        name = os.path.basename(path)
        if name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "mirai_plugin_" + name[:-3], path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "register"):
                mod.register(api)
                say("machine plugin loaded: %s" % name, "web")
        except Exception as exc:
            say("machine plugin failed (%s): %s" % (name, exc), "web")


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
    return drives.is_device(path)


def free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def host_drives():
    """Real drives the host could hand to a guest.

    The drives layer knows what a drive is on this platform; this is the
    one shape the rest of the program and the page work in.
    """
    return drives.enumerate_drives()


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
            "accel": root.findtext("accel") or "tcg",
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
    ET.SubElement(root, "accel").text = inst.get("accel") or "tcg"
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
    """The three ports a machine keeps for life, from its index.

    The bases can be moved in the config, which is what lets a second copy
    run beside an appliance that is already using the usual ones.
    """
    index = inst["index"]
    return (5900 + CONFIG.get("vnc_display", VNC_DISPLAY_BASE) + index,
            CONFIG.get("websocket", WEBSOCKET_BASE) + index,
            CONFIG.get("qmp", QMP_BASE) + index)


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
              "accel": str(data.get("accel") or "tcg"),
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
                  "machine": "pc9821", "bios": "compat", "accel": "tcg",
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
    return "format=%s,file=%s" % (fmt, win_short(path))


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
    try:
        sys.stderr.write(line + "\n")
    except OSError:
        # stderr is a pipe to whoever started us, and they may be the
        # very thing whose death is being reported
        pass
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
        # normally the directory it was started in, but --base moves it,
        # and then saying "where it was started" would be a lie
        here = os.path.abspath(CONFIG["boot"])
        return {"path": here,
                "what": "where this program was started"
                        if here == os.path.abspath(BASE) else "this computer"}
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


def drive_job(label, kind, name, device, to_drive, confirm="",
              allow_internal=False, check=True):
    """Copy between an image and a real drive.

    Every refusal is the drives layer's, so that nothing can be written by
    a caller that simply forgot to ask.  Writing also needs `confirm` to
    repeat what the drive says about itself, and is followed by reading it
    back and comparing, because a write that went nowhere and a write that
    worked look exactly alike until someone tries to boot from it.
    """
    path = os.path.join(disks_root(kind), name)
    job = {"name": label, "kind": kind, "done": 0, "total": 0,
           "state": "running", "error": "", "verified": "",
           "checking": False}
    try:
        if to_drive:
            drive = drives.check_write(device, confirm, allow_internal)
            job["total"] = os.path.getsize(path)
            if drive["size_bytes"] and job["total"] > drive["size_bytes"]:
                raise drives.Refused(
                    "%s holds %d bytes and the image is %d"
                    % (device, drive["size_bytes"], job["total"]))
        else:
            drive = drives.check_read(device)
            job["total"] = drive["size_bytes"]
    except drives.Refused as err:
        job.update(state="failed", error=str(err))
        _jobs[label] = job
        say("%s refused: %s" % (label, err), "disk")
        return job
    sector = drive["sector"]
    _jobs[label] = job

    def progress(done):
        job["done"] = done

    def elevated():
        """The same job, done by a process that is allowed to.

        Windows only hands out raw drives to administrators.  The helper
        is started through UAC, told what to do in a file, and watched
        through another; the refusals run again on its side, so nothing
        is riskier for being elevated.
        """
        say("%s needs administrator rights; asking" % label, "disk")
        work = tempfile.mkdtemp(prefix="mirai98-drive-")
        opf = os.path.join(work, "op.json")
        prf = os.path.join(work, "progress.json")
        with open(opf, "w", encoding="utf-8") as f:
            json.dump({"to_drive": to_drive, "image": path, "device": device,
                       "confirm": confirm, "allow_internal": allow_internal,
                       "check": check, "progress": prf}, f)
        try:
            handle = drives.impl.elevate(opf)
        except OSError as err:
            job.update(state="failed", error=str(err))
            say("%s failed: %s" % (label, err), "disk")
            shutil.rmtree(work, ignore_errors=True)
            return
        while True:
            time.sleep(0.3)
            try:
                with open(prf, encoding="utf-8") as f:
                    got = json.load(f)
            except (OSError, ValueError):
                got = {}
            job.update(done=got.get("done", job["done"]),
                       total=got.get("total", job["total"]),
                       checking=got.get("checking", False))
            if got.get("state") in ("done", "failed"):
                job.update(state=got["state"], error=got.get("error", ""),
                           verified=got.get("verified", ""))
                break
            if drives.impl.process_gone(handle) and not got:
                job.update(state="failed", checking=False,
                           error="the elevated helper stopped without "
                                 "a word")
                break
        shutil.rmtree(work, ignore_errors=True)
        if job["state"] == "done":
            say("%s finished (%d bytes)%s"
                % (label, job["done"],
                   ", read back and checked" if job["verified"] else ""),
                "disk")
        else:
            say("%s failed: %s" % (label, job["error"]), "disk")

    def run():
        try:
            if to_drive:
                with open(path, "rb") as src,                         drives.impl.open_write(device) as dst:
                    written, done = drives.copy(src, dst, sector, progress)
                if check:
                    job.update(checking=True, done=0)
                    ok, said = drives.verify(device, done, written, progress)
                    job["checking"] = False
                    if not ok:
                        raise OSError(said)
                    job["verified"] = said[:16]
            else:
                with drives.impl.open_read(device) as src,                         open(path, "wb") as dst:
                    _read, done = drives.copy(src, dst, 1, progress,
                                              limit=job["total"] or None)
            job.update(state="done", done=done)
            say("%s finished (%d bytes)%s"
                % (label, done,
                   ", read back and checked" if job["verified"] else ""),
                "disk")
        except PermissionError as err:
            # not allowed is not the same as failed: on Windows it means
            # ask again as an administrator, through the UAC dialog
            if WINDOWS:
                job.update(done=0, checking=False)
                elevated()
            else:
                job.update(state="failed", error=str(err), checking=False)
                say("%s failed: %s" % (label, err), "disk")
        except Exception as err:
            job.update(state="failed", error=str(err), checking=False)
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


def win_short(path):
    """The 8.3 name of a path, on Windows, when it has one.

    QEMU is handed paths inside option strings (-L, -drive file=) where
    "OneDrive - Tabata Computer" and other space-laden, non-ASCII homes
    have proven unreliable.  The short name is plain ASCII with no spaces
    and names the same file, so everything QEMU gets goes through here.
    Volumes with 8.3 names turned off just get the path back unchanged.
    """
    if not WINDOWS or not os.path.exists(path):
        return path
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, 260)
    return buf.value if 0 < n < 260 else path


def missing_roms():
    """A complaint about the compatible ROMs, or "" if they are all there.

    QEMU wants either pc98bank*.bin or pc98itf.bin and pc98bios.bin, in one
    of the directories given with -L.  When it cannot find them it says so
    without saying where it looked, which is the one thing worth knowing.
    """
    wanted = ("pc98itf.bin", "pc98bios.bin")
    for where in (CONFIG["roms"], CONFIG["datadir"]):
        if not where:
            continue
        try:
            here = set(os.listdir(where))
        except OSError:
            continue
        if any(n.startswith("pc98bank") for n in here):
            return ""
        if all(n in here for n in wanted):
            return ""
    return ("no PC-98 ROMs in %s: wanted pc98bank*.bin, or both %s"
            % (CONFIG["datadir"], " and ".join(wanted)))


def qemu_argv(inst):
    # a plugin-provided machine builds its own command line
    builder = MACHINE_ARGV.get(inst.get("machine") or "pc9821")
    if builder:
        return builder(inst)
    vnc, ws, qmp_port = ports_of(inst)
    display = vnc - 5900
    accel = "kvm:tcg" if inst.get("accel", "tcg") == "kvm" else "tcg"
    argv = [CONFIG["qemu"],
            "-M", "%s,accel=%s%s" % (
                inst.get("machine") or "pc9821", accel,
                ",pcspk-audiodev=snd" if any(SOUNDS[sound_of(inst)]) else ""),
            "-m", inst.get("memory") or "64M",
            "-k", "ja"]   # PC-98 is a JIS keyboard; use the Japanese VNC keymap
    # QEMU searches -L paths in order, so putting the uploaded ROMs first
    # lets a partial real set fall through to the compatible ones
    if inst.get("bios") == "real":
        argv += ["-L", win_short(CONFIG["roms"])]
    # the boards play into a null backend; the VNC server captures that
    # mix and hands it to any client that asks, so the browser hears them
    fm, pcm = SOUNDS[sound_of(inst)]
    vnc = "%s:%d,websocket=%d" % ("127.0.0.1" if LOOPBACK else "0.0.0.0",
                                  display, ws)
    if fm or pcm:
        vnc += ",audiodev=snd"
    argv += ["-L", win_short(CONFIG["datadir"]),
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
                     % (unit, win_short(os.path.expanduser(inst["mount"])))]
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
    # QEMU's own complaint about a missing ROM says which files it wanted
    # but not where it looked, which leaves a real answer out of reach.
    # This says it before starting, and names the directory.
    missing = missing_roms()
    if missing:
        say("vm %s not started: %s" % (inst["name"], missing), "vm")
        return missing
    argv = qemu_argv(inst)
    log_path = os.path.join(inst_dir(inst["index"]), "qemu.log")
    with open(log_path, "ab") as log:
        log.write(("\n--- %s\n%s\n" % (time.strftime("%F %T"),
                                       " ".join(argv))).encode())
        proc = subprocess.Popen(argv, stdout=log, stderr=log,
                                start_new_session=True)
    _procs[inst["name"]] = proc
    # A machine is deliberately in a session of its own, so that restarting
    # this server does not take the guests down with it.  That also means
    # no signal to this process reaches them, so the pid is written down:
    # whatever started this server can then clear up after it even if it
    # has stopped answering.
    try:
        with open(os.path.join(inst_dir(inst["index"]), "qemu.pid"), "w") \
                as f:
            f.write("%d\n" % proc.pid)
    except OSError:
        pass
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


def forget_pid(inst):
    try:
        os.remove(os.path.join(inst_dir(inst["index"]), "qemu.pid"))
    except OSError:
        pass


def stop_instance(inst):
    if not is_running(inst):
        forget_pid(inst)
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
            forget_pid(inst)
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
#
# The page is a handful of real files in web/: index.html and the script
# and style it pulls in, plus the two standalone pages the server shows
# before the app is reachable, the setup wizard and the sign-in form.
# Nothing there is templated; every value the page shows it asks /api for.

CTYPES = {".html": "text/html; charset=utf-8",
          ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
          ".png": "image/png", ".json": "application/json"}

_pages = {}


def page_bytes(name):
    """A file from web/, read once and kept unless --dev is on.

    Two threads racing here just read the file twice, which is why there
    is no lock.
    """
    if not DEV and name in _pages:
        return _pages[name]
    base = os.path.realpath(CONFIG["web"])
    full = os.path.realpath(os.path.join(base, name))
    if not full.startswith(base + os.sep):
        raise ValueError("outside web/: %s" % name)
    with open(full, "rb") as f:
        blob = f.read()
    _pages[name] = blob
    return blob


# ------------------------------------------------------------ the server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # set by mint_session(), sent with whatever reply follows
    cookie = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def handle_one_request(self):
        # one connection serves many requests; a cookie minted for one of
        # them must not ride along on the rest
        self.cookie = ""
        self.began = time.time()
        BaseHTTPRequestHandler.handle_one_request(self)

    def log_request(self, code="-", size="-"):
        # with the time it took: when the page feels slow, this says which
        # request to blame, which guessing does not
        took = (time.time() - getattr(self, "began", time.time())) * 1000
        self.log_message('"%s" %s %s %dms', self.requestline, str(code),
                         str(size), took)

    def reply(self, code, body, ctype="application/json"):
        blob = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        if self.cookie:
            self.send_header("Set-Cookie", self.cookie)
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
    def has_session(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "mirai98" and value in _sessions:
                return True
        return False

    def signed_in(self):
        if APP_TOKEN:
            # the token takes the place of a password: it is handed over
            # once, in the address the application opens, and a session
            # cookie carries it from then on
            if self.has_session():
                return True
            from urllib.parse import parse_qs, urlparse
            given = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if given and hmac.compare_digest(given, APP_TOKEN):
                self.mint_session()
                return True
            return False
        if not password_set():
            return True
        return self.has_session()

    def mint_session(self):
        """Start a session, on the reply this handler is about to send."""
        token = secrets.token_urlsafe(32)
        _sessions.add(token)
        self.cookie = ("mirai98=%s; Path=/; HttpOnly; SameSite=Strict"
                       % token)

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
        # A password does not exist until the first run sets one, so the
        # wizard below has to be reachable without it.  A token does exist
        # from the start, and so covers the wizard too.
        if APP_TOKEN and not self.signed_in():
            self.fail(401, "no token")
            return
        if not setup_done():
            if path == "/":
                self.page("setup.html")
            elif path == "/api/setup":
                default = default_storage()
                grown = grown_by()
                self.reply(200, {
                    "platform": PLATFORM,
                    "default": default,
                    "grown": human_mb(grown) if grown else "",
                    "choices": data_choices(),
                    # with a token in force there is nothing for a password
                    # to guard, so the wizard has one question fewer
                    "token": bool(APP_TOKEN),
                })
            else:
                self.fail(409, "finish setting up first")
            return
        if not self.signed_in():
            if path == "/":
                self.page("login.html")
            elif path.startswith("/novnc/"):
                self.static(path[len("/novnc/"):])
            else:
                self.fail(401, "sign in first")
            return
        if path == "/":
            self.page("index.html")
        elif path in ("/app.js", "/style.css"):
            self.page(path[1:])
        elif (path.startswith("/plugins/") and path.endswith(".js")
              and ".." not in path):
            # front-end plugin scripts, served from web/plugins/
            self.page(path[1:])
        elif path == "/api/plugins":
            pdir = os.path.join(CONFIG["web"], "plugins")
            names = sorted(f for f in os.listdir(pdir)
                           if f.endswith(".js")) \
                if os.path.isdir(pdir) else []
            self.reply(200, names)
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
                # Windows the default is a path like any other, and where
                # that is depends on --base
                "on_stick": (os.path.abspath(chosen.get("path")
                                             or CONFIG["boot"]) ==
                             os.path.abspath(CONFIG["boot"])) if WINDOWS
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
            # a token in force means a password guards nothing and cannot
            # be set, so the page leaves it out rather than offering it
            facts["token"] = bool(APP_TOKEN)
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

    def page(self, name):
        """One of the files in web/, by name and nothing else."""
        try:
            blob = page_bytes(name)
        except (OSError, ValueError) as err:
            self.fail(404, "no %s: %s" % (name, err))
            return
        self.reply(200, blob,
                   CTYPES.get(os.path.splitext(name)[1],
                              "application/octet-stream"))

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
        ctype = CTYPES.get(os.path.splitext(full)[1],
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
        if APP_TOKEN and not self.signed_in():
            self.refuse(401, "no token")
            return
        if not setup_done():
            if path == "/api/setup":
                self.run_setup()
            else:
                self.refuse(409, "finish setting up first")
            return
        if path == "/api/login":
            if APP_TOKEN:
                # the token is the only way in; a password would be a
                # second, weaker one
                self.refuse(404, "no such action")
                return
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
        if path == "/api/shutdown":
            # The application that started us is closing.  Only it may ask:
            # on the appliance this server is the system's, not a window's.
            if not APP_TOKEN:
                self.refuse(404, "no such action")
                return
            with _lock:
                instances = load_instances()
            running = [i for i in instances if is_running(i)]
            left = [i["name"] for i in running
                    if stop_instance(i) != "stopped"]
            self.reply(200, {"result": "stopping",
                             "stopped": len(running) - len(left),
                             "left": left})
            # once the reply is out, and from another thread, or this one
            # would be waiting on itself
            threading.Thread(target=self.server.shutdown,
                             daemon=True).start()
            return
        if path == "/api/password":
            if APP_TOKEN:
                self.refuse(404, "no such action")
                return
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
            # what the user typed to say they mean this drive, and not
            # some other one that happens to be plugged in
            job = drive_job("%s → %s" % (name, device), kind, name, device,
                            to_drive=True,
                            confirm=str(data.get("confirm") or ""),
                            allow_internal=bool(data.get("internal")),
                            check=data.get("check", True) is not False)
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
        if not self.signed_in():
            self.refuse(401, "sign in first")
            return
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


def read_config(path):
    """A config file, with its relative paths made absolute.

    A path in it is taken as relative to the file itself, so a directory
    that ships as one piece can be unpacked anywhere and still find the
    things beside it.  The appliance's own config gives absolute paths and
    is untouched by this.
    """
    with open(path, encoding="utf-8") as f:
        conf = json.load(f)
    beside = os.path.dirname(os.path.abspath(path))
    # normpath as well as join: the separators written in the file are
    # forward slashes, and joining leaves them, so a Windows path comes out
    # as C:\...\qemu/share.  Most things take that; not everything does.
    return {key: os.path.normpath(os.path.join(beside, value))
            if isinstance(value, str) and value and not os.path.isabs(value)
            else value
            for key, value in conf.items()}


def watch_parent(pid):
    """Stop the machines and quit once the given process is gone.

    Asked for with --parent-pid, by an application that wants this server
    to be its own and not outlive it.  Without it, an application that
    crashes leaves this server and every guest running, holding a port and
    invisible to the next attempt to start.

    Watching an inherited pipe for end-of-file would be the tidier trick,
    and does not work: Electron's helper processes inherit the same handle
    and carry on running after the process that spawned them is killed, so
    the pipe never closes.  Watching the pid is what is left.
    """
    def gone():
        if WINDOWS:
            import ctypes
            # SYNCHRONIZE is enough to wait on it, and the wait is exact
            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False,
                                                        pid)
            if not handle:
                return True              # already gone
            ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            time.sleep(2)

    def wait():
        gone()
        # from here the exit must happen whatever else does, and one
        # machine failing to stop must not stop the next from being
        # tried: this net exists for the messy cases, and the messy
        # cases are exactly where any of this can throw
        try:
            say("the program that started this has gone; stopping the "
                "machines", "system")
            with _lock:
                instances = load_instances()
            for inst in instances:
                try:
                    if is_running(inst):
                        stop_instance(inst)
                except Exception:
                    pass
        finally:
            os._exit(0)

    threading.Thread(target=wait, daemon=True).start()


def use_base(path):
    """Put the settings and the machines under a directory given to us.

    Normally the directory the program was started in is the answer, but a
    program that starts this one cannot promise anything about that: on
    Windows it is wherever the launcher sat, which may well be somewhere
    that cannot be written to.
    """
    root = os.path.abspath(os.path.expanduser(path))
    os.makedirs(root, exist_ok=True)
    CONFIG.update({"root": os.path.join(root, "pc98"),
                   "boot": root,
                   # uploaded real ROMs are the user's, not the program's
                   "roms": os.path.join(root, "roms"),
                   "legacy": os.path.join(root, "instances.json")})


def main(argv):
    global DEV, LOOPBACK, APP_TOKEN
    host, port = "0.0.0.0", 8098
    base, want_token, parent = "", False, 0
    for arg in argv[1:]:
        if arg.startswith("--host="):
            host = arg.split("=", 1)[1]
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
        elif arg.startswith("--config="):
            CONFIG.update(read_config(arg.split("=", 1)[1]))
        elif arg.startswith("--base="):
            base = arg.split("=", 1)[1]
        elif arg == "--dev":
            DEV = True
        elif arg == "--loopback":
            LOOPBACK = True
        elif arg == "--app-token":
            want_token = True
        elif arg.startswith("--parent-pid="):
            parent = int(arg.split("=", 1)[1])
        else:
            print(__doc__)
            return 2
    if LOOPBACK:
        host = "127.0.0.1"
    # last, so it wins over a config file: it says where this run lives
    if base:
        use_base(base)
    if want_token:
        APP_TOKEN = secrets.token_urlsafe(32)
    if parent:
        watch_parent(parent)
    # machine plugins register after the config is settled, before serving
    load_plugins()
    # --port=0 means "any free one", which only a program would ask for
    started_by_program = bool(APP_TOKEN) or port == 0
    if not os.path.isfile(os.path.join(CONFIG["web"], "index.html")):
        # unlike noVNC, there is no working without this one
        print("no index.html in %s: nothing to serve" % CONFIG["web"])
        return 1
    if not os.path.isdir(CONFIG["novnc"]):
        # everything but the consoles works without it
        print("no noVNC in %s: consoles will not draw" % CONFIG["novnc"])
    ready = start_up()
    server = ThreadingHTTPServer((host, port), Handler)
    # --port=0 asks the operating system to pick one, so the answer is
    # only known now; the caller is told below
    port = server.server_address[1]
    print("Mirai98 Hypervisor Platform OS (%s) on http://%s:%d"
          % (PLATFORM, host, port))
    print("  machines in %s" % CONFIG["root"] if ready else
          "  not set up yet: open the address above to choose where the "
          "machines live")
    if started_by_program:
        # it needs the address to open, and the port may have been the
        # operating system's choice: one line, so it can stop reading
        print("MIRAI98-READY " + json.dumps({"port": port,
                                             "token": APP_TOKEN}),
              flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
