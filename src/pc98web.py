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
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
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
# instance fields a plugin adds (name -> validator(value) -> complaint|None);
# they ride through sanitize()/save_instance()/load_instance() untouched
# otherwise, so a machine type may keep settings of its own in vm.xml
PLUGIN_FIELDS = {}
# machine name -> fn(record) -> complaint | None; runs last in sanitize() and
# may trim or refuse what makes no sense for that machine
MACHINE_SANITIZE = {}
# (kind, format) -> fn(dest, data): a plugin's own image builder for the
# Storage "Create" form (kind is hdd or fdd; format the value it registered)
# Containers a floppy on this shelf may already be named for.  A name
# carrying anything else is given the shelf's own .raw, appended rather
# than replaced, so a deliberate .fdi or .nfd survives.
FDD_CONTAINERS = (".raw", ".img", ".fdi", ".nfd", ".d88")

DISK_BUILDERS = {}
# (machine, action) -> fn(inst, data) -> reply dict | (status, message):
# a plugin's own POST /api/instances/<name>/x/<action>
PLUGIN_ACTIONS = {}


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

    def qemu_file(self, path):
        """A path for one of QEMU's option strings: commas doubled."""
        return qemu_file(path)

    def drive_backing(self, path):
        """format= and file= for a drive, with the format read off the
        name.  Telling QEMU raw about a qcow2 shows the guest the
        container's own header as its first sector, and the first write
        eats the image."""
        return drive_backing(path)

    def disk_path(self, inst, kind):
        return disk_path(inst, kind)

    def add_machine(self, name):
        global MACHINES
        if name not in MACHINES:
            MACHINES = MACHINES + (name,)

    def machine_argv(self, name, builder):
        MACHINE_ARGV[name] = builder

    def instance_action(self, machine, action, fn):
        """A POST /api/instances/<name>/x/<action> of the plugin's own."""
        PLUGIN_ACTIONS[(machine, action)] = fn

    def is_running(self, inst):
        return is_running(inst)

    def qmp(self, inst, command, arguments=None):
        """One QMP command against a running instance; None if unreachable."""
        return qmp(inst, command, arguments)

    def save_instance(self, inst):
        """Write a machine's record back, for a setting changed outside the
        edit form -- one changed while it runs still has to be the setting
        it starts with next time.

        Without the lock, and it must stay that way: a plugin action is
        dispatched with _lock already held, and _lock is not reentrant, so
        taking it here wedged the whole server on the first click -- every
        endpoint that wants the lock waits for a thread that is waiting
        for itself.  What made it hard to see is that the access log is
        written when a request finishes, so the request that hung left no
        line at all and looked like one that never arrived.
        """
        save_instance(inst)

    def disk_builder(self, kind, fmt, fn):
        """Offer a disk image format of the plugin's own in Storage."""
        DISK_BUILDERS[(kind, fmt)] = fn

    def machine_sanitize(self, name, fn):
        """A final check/trim of an instance record for this machine."""
        MACHINE_SANITIZE[name] = fn

    def add_field(self, name, validator=None):
        """Declare an instance field of the plugin's own (kept in vm.xml)."""
        PLUGIN_FIELDS[name] = validator

    def inst_dir(self, inst):
        return inst_dir(inst["index"])


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
# a MIDI board is a second card, fitted or not whatever the sound board
# is.  "synth" renders what the board is sent with a SoundFont and mixes
# it into the machine's sound, which is what reaches the browser over the
# console connection.  A machine plugin may read this field too.
MIDI_MODES = {"": False, "synth": True}


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


# ---------------------------------------------------------- disk groups
# A kind's images all sit in the one folder, and past a dozen of them the
# list is hard to read and harder to pick from: eight EVE floppies and
# eight of MS-DOS bury the one disc someone is after.  A group is a
# folder a level down -- disks/fdd/EVE/EVE_A.raw -- so the grouping is
# something the file tree shows as plainly as the list does, and it
# survives the tree being copied to another machine.
#
# A machine still names an image by its file name alone.  The group is
# how the shelf is arranged and not part of what a disc is called, so an
# image can be moved into a group, or out of one, without a single vm.xml
# changing -- and a machine that is running keeps the file it opened,
# which a move does not disturb.  That holds only while a name means one
# file, so a name a kind already holds anywhere is refused for a new one.

def group_dir(kind, group):
    """The folder a group's images sit in; the kind's own for no group."""
    root = disks_root(kind)
    return os.path.join(root, group) if group else root


def disk_groups(kind):
    """The group folders of a kind, in the order they are listed.

    Lenient about what is found, as everything that reads the tree is: a
    folder put here by hand under a name this would not have given it is
    still a folder with images in it, and passing it over hid them from
    the listing altogether.
    """
    root = disks_root(kind)
    try:
        return sorted(n for n in os.listdir(root)
                      if listed_name(n)
                      and os.path.isdir(os.path.join(root, n)))
    except OSError:
        return []


def disk_find(kind, name):
    """Where the image called `name` is: on the shelf itself, or in one
    of the groups.  A name that is nowhere answers with where it would go
    if it arrived without a group, which is what both the callers asking
    "is this taken?" and the callers about to write want."""
    root = disks_root(kind)
    here = os.path.join(root, name)
    if os.path.lexists(here):
        return here
    for group in disk_groups(kind):
        there = os.path.join(root, group, name)
        if os.path.lexists(there):
            return there
    # An image uploaded from a Mac before names were composed on the way
    # in sits on the shelf decomposed, while the name it is now asked for
    # by -- out of a .cue sheet, or typed -- is composed.  One name, two
    # spellings: look again with the difference taken out.  Composition
    # only; case still tells two names apart here, as it always has.
    found = disk_match(kind, name)
    if found:
        return found
    return here


def disk_match(kind, name):
    """Where an image whose name differs from `name` only in composition
    is, or None.  The exact lookup comes first and this answers after it
    has failed, so nothing that used to be found is found differently."""
    # Not "is this name already composed": the stored name is the one
    # that may be decomposed, and it is the one being looked for.  An
    # ASCII name is the only one with nothing to compose either side of.
    if name.isascii():
        return None
    want = nfc(name)
    root = disks_root(kind)
    for folder in [root] + [os.path.join(root, g)
                            for g in disk_groups(kind)]:
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            if nfc(entry) == want:
                return os.path.join(folder, entry)
    return None


def disk_taken(kind, name):
    """Whether a kind holds that name already, group or no group.  Two
    files of one name would leave which of them a machine meant to the
    order the folders happen to be read in."""
    return os.path.lexists(disk_find(kind, name))


def disk_group_of(kind, name):
    """The group an image is in, or "" for one in none."""
    return os.path.dirname(os.path.relpath(disk_find(kind, name),
                                           disks_root(kind)))


def disk_dest(kind, name, group="", make=False):
    """Where a file that is about to arrive should be written."""
    if group and not given_name(group):
        raise ValueError("a group is named like an image: " + NAME_RULE)
    if group and make:
        os.makedirs(group_dir(kind, group), exist_ok=True)
    return os.path.join(group_dir(kind, group), name)


def disk_drop_group(kind, group):
    """Take a group folder away once nothing is left in it: a group is
    the images in it, and an empty one is only a name in the way."""
    if not group:
        return
    try:
        if not os.listdir(group_dir(kind, group)):
            os.rmdir(group_dir(kind, group))
    except OSError:
        pass


def disk_path(inst, key):
    """A device's image file: a bare name lives in the rule tree, and
    anything with a separator in it is taken as a path of its own,
    which is how a real drive like /dev/sr0 gets through."""
    value = inst.get(key) or ""
    if not value:
        return ""
    if "/" in value or value.startswith("~"):
        return os.path.expanduser(value)
    return disk_find(disk_dir_of(key), value)


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

# Two questions are asked about a name, and they are not the same one.
#
# A name that is already there is asked only to name one file in one
# folder.  A file may have been put on the shelf by hand -- copied in
# over ssh, carried in on a stick -- under anything its filesystem
# allowed, and refusing to rename or delete such a file would leave it
# listed and untouchable, which is worse to be than strict.
#
# A name this is about to *give* a file is asked for more, but only for
# what something downstream would break on.  A disc is called what it is
# called -- 天晴, "Appare CD Vol. 2 - Houou no Maki (Japan).chd" -- and
# putting every such name through an ASCII sieve threw away the name the
# dump came with.  So what is refused is:
#
#   /  \               a name is one path component, not two
#   :  *  ?  "  <  >  |   Windows will not have them, and this tree is
#                      meant to be carried to another machine on a stick
#   control bytes      they have no business in a file name
#   CON, LPT1 and so on   are devices on Windows rather than files
#   a leading dot      the listing passes those over, so a file named
#                      that way would not appear in it at all
#   a trailing space or dot   Windows drops them quietly, and the name
#                      the shelf shows would stop being the one on disk
#
# and a length in bytes rather than characters, because a filesystem
# counts bytes, one Japanese character is three of them, and ".part"
# while it uploads and a ".cue" beside it have to fit as well.
NAME_BAD = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
NAME_BYTES = 240
WIN_DEVICES = frozenset(["CON", "PRN", "AUX", "NUL"] +
                        ["COM%d" % n for n in range(1, 10)] +
                        ["LPT%d" % n for n in range(1, 10)])
NAME_RULE = ('a name cannot hold / \\ : * ? " < > |, start with a dot, '
             'end with a space or a dot, or run past %d bytes' % NAME_BYTES)


def attachment(name):
    """The Content-Disposition for a download.  A header is latin-1 on
    the wire, so a name with a kanji in it cannot go in one as it stands
    -- sending it raw did not mangle the name, it threw and the download
    never began.  RFC 6266's filename* carries the real name UTF-8 and
    percent-encoded; the plain filename beside it is an ASCII stand-in
    for anything too old to read the other, and browsers that read both
    prefer the encoded one."""
    from urllib.parse import quote

    # control bytes out first: a newline in a file name would end the
    # header block, and everything after it would be read as another
    # header and then as a body of the page's choosing
    flat = re.sub(r"[\x00-\x1f\x7f]", "_", name)
    plain = flat.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return ('attachment; filename="%s"; filename*=UTF-8\'\'%s'
            % (plain, quote(flat, safe="")))


def url_path(raw):
    """The path of a request, as the names in it are actually spelt.  A
    browser percent-encodes what it puts in a URL, so a disc with a space
    or a kanji in its name is asked for as %20 and %E5%A4%A9 -- and a
    lookup for the encoded spelling finds nothing, which is how the
    download and the page for such a disc used to answer 404 while the
    listing showed it plainly.  The query is cut off first: the ? that
    starts it is the one character here that is not part of a name."""
    from urllib.parse import unquote

    return unquote(raw.split("?")[0])


def listed_name(name):
    """Whether a name is one this may act on: see above."""
    return (bool(name) and len(name) < 256 and "/" not in name
            and "\\" not in name and not name.startswith("."))


def nfc(name):
    """A name in composed form.

    macOS hands the browser the decomposed spelling -- the "de" of
    "A-Ressha de Ikou" arrives as a bare "te" followed by a combining
    voiced mark -- while a .cue sheet written anywhere else spells the
    same name composed.  Linux stores whichever bytes it is handed and
    compares them as bytes, so the two never meet: the sheet's FILE line
    names a file that is sitting right there under a name made of
    different bytes.  Compose on the way in and again before comparing,
    and there is one name.

    A name off the filesystem may hold lone surrogates and is not text
    this can normalize; such a name is its own answer.
    """
    try:
        return unicodedata.normalize("NFC", name)
    except (TypeError, ValueError):
        return name


def name_key(name):
    """The form two file names are compared in: composed, and lowered
    because the shelf has always matched names without regard to case.
    Use this wherever a name from outside meets a name off the disk."""
    return nfc(name).lower()


def given_name(name):
    """Whether this may give a file that name: see above.

    A name that came off the filesystem may not be text at all: bytes
    that are not UTF-8 arrive as lone surrogates, and asking such a name
    how many bytes it is raises.  It is not a name this would give a file
    -- that is the answer -- but it has to be *an* answer, because this
    is asked about every entry in a folder while a listing is built, and
    one Shift-JIS name dropped in by hand took the whole Storage page
    down with it.
    """
    if not name or name != name.strip() or name.endswith("."):
        return False
    if name.startswith(".") or NAME_BAD.search(name):
        return False
    if os.path.splitext(name)[0].upper() in WIN_DEVICES:
        return False
    try:
        return len(name.encode("utf-8")) <= NAME_BYTES
    except UnicodeEncodeError:
        return False


def safe_disk_name(name):
    """The name a file is stored under: the name it came with, with what
    given_name refuses turned into '_' (the browser does the same)."""
    out = NAME_BAD.sub("_", name).strip().lstrip(".").rstrip(" .")
    if os.path.splitext(out)[0].upper() in WIN_DEVICES:
        out = "_" + out
    # cut to bytes, and drop the character the cut fell inside
    out = out.encode("utf-8")[:NAME_BYTES].decode("utf-8", "ignore")
    return out or "disk"



# ------------------------------------------------------- inside an image

FS_MAX_PUT = 256 << 20              # one file dropped in at a time

# The server answers in threads and a volume is edited in place, so two
# requests against one image would each scan the FAT for free clusters,
# hand out the same ones and write directory blocks over each other --
# both answering that they had succeeded.  One at a time per image.
_fs_locks = {}
_fs_locks_guard = threading.Lock()


def fs_lock(path):
    with _fs_locks_guard:
        lock = _fs_locks.get(path)
        if lock is None:
            lock = _fs_locks[path] = threading.Lock()
        return lock


def fat_stamp(date, clock):
    """A FAT date/time pair as an ISO string, or '' when it is unset."""
    if not date:
        return ""
    year, month, day = 1980 + (date >> 9), (date >> 5) & 15, date & 31
    hour, minute = (clock >> 11) & 31, (clock >> 5) & 63
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return ""
    return "%04d-%02d-%02d %02d:%02d" % (year, month, day, hour, minute)


def fs_path_ok(where):
    """A path inside an image: no climbing out of it."""
    return ".." not in str(where).replace("\\", "/").split("/")


def disk_contents(kind, name):
    """What is inside an image, read through virtpc98's FAT reader."""
    import virtpc98
    path = disk_find(kind, name)
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
    path = disk_find(kind, name)
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
    path = disk_find(kind, name)
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


# ------------------------------------------------------- CD cue sheets
# A CD dump often comes as a data file (.bin/.img) plus a .cue sheet that
# names it and lays out the tracks (CD-DA audio ones included).  The
# emulator is given the data file and finds the sheet beside it, so the
# pair is one disc in the tree: the data file is the entry, the .cue its
# sidecar, and a .cue whose data file is missing is an orphan nothing can
# use.  The same goes for Alcohol's .mds and CloneCD's .ccd, which name
# their data by stem instead of by line.

CUE_FILE_RE = re.compile(r'^\s*FILE\s+(?:"([^"]*)"|(\S+))\s+(\S+)',
                         re.IGNORECASE)
CUE_TRACK_RE = re.compile(r'^\s*TRACK\s+(\d+)\s+(\S+)', re.IGNORECASE)


def read_cue(path):
    """The sheet's lines.  A cue is text of no stated encoding: mostly
    ASCII, sometimes Shift-JIS in a TITLE, and -- since a disc may be
    called 天晴 -- sometimes UTF-8 in the name it points at.  Decoded
    UTF-8 with surrogateescape it reads the ASCII and the UTF-8 as they
    are and keeps whatever it cannot make sense of as it found it, so the
    lines written back out again are the same bytes."""
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "surrogateescape").splitlines(
            keepends=True)


def write_cue(path, lines):
    """Put a sheet back where it was, without ever leaving less of one
    than there was.  Opening the sheet itself for writing empties it
    first, so a rewrite that then fails leaves nothing at all -- which is
    what happened the first time a name in kanji met an encoder that
    could not hold it, and a disc lost the sheet that made it one.  This
    writes beside the sheet and moves the finished file into place, and
    the name it writes under begins with a dot so a listing taken in the
    middle of it does not show a half-written one."""
    # Through a link rather than over it: a sheet may be a link to a
    # master kept somewhere else, and replacing the link with a file of
    # our own takes the master quietly out of the arrangement -- the disc
    # still works and the next person to look at the master finds it
    # describing a name that no longer exists.
    real = os.path.realpath(path)
    folder = os.path.dirname(real) or "."
    # a name of its own, so a file that happens to be called
    # ".<sheet>.new" is not eaten, and two writers cannot collide
    fd, beside = tempfile.mkstemp(prefix="." + os.path.basename(real) + ".",
                                  suffix=".new", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write("".join(lines).encode("utf-8", "surrogateescape"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(beside, real)
    except BaseException:
        # half a sheet under a hidden name is something the listing cannot
        # show and nothing in the program can delete: take it away here
        try:
            os.remove(beside)
        except OSError:
            pass
        raise
    sync_file(folder)                   # the rename has to reach the medium


def cue_summary(path):
    """(referenced file names, track count, audio track count)."""
    files, tracks, audio = [], 0, 0
    try:
        for line in read_cue(path):
            m = CUE_FILE_RE.match(line)
            if m:
                files.append(os.path.basename((m.group(1) or m.group(2))
                                              .replace("\\", "/")))
                continue
            m = CUE_TRACK_RE.match(line)
            if m:
                tracks += 1
                if m.group(2).upper() == "AUDIO":
                    audio += 1
    except OSError:
        pass
    return files, tracks, audio


# The other shape the same pair comes in: Alcohol writes a binary .mds
# beside its .mdf and names the data file inside it with a wildcard --
# "*.mdf" is the one sharing the descriptor's stem, which is also how the
# emulator finds it.  Only the fields this listing needs are read here.
MDS_SIGNATURE = b"MEDIA DESCRIPTOR"
MDS_MODE_AUDIO = 0xa9


def mds_summary(path):
    """(referenced file names, track count, audio track count)."""
    tracks = audio = 0
    try:
        with open(path, "rb") as f:
            head = f.read(0x58)
            if len(head) < 0x58 or not head.startswith(MDS_SIGNATURE):
                return [], 0, 0
            if struct.unpack_from("<H", head, 0x14)[0] != 1:
                return [], 0, 0             # one session, as the drive reads
            f.seek(struct.unpack_from("<I", head, 0x50)[0])
            session = f.read(0x18)
            if len(session) < 0x18:
                return [], 0, 0
            blocks = session[0x0a]
            f.seek(struct.unpack_from("<I", session, 0x14)[0])
            for _ in range(blocks):
                block = f.read(0x50)
                if len(block) < 0x50:
                    return [], 0, 0
                if not 1 <= block[0x04] <= 99:
                    continue                # A0/A1/A2 describe the disc
                tracks += 1
                if block[0x00] == MDS_MODE_AUDIO:
                    audio += 1
    except (OSError, struct.error):
        return [], 0, 0
    return [os.path.splitext(os.path.basename(path))[0] + ".mdf"], tracks, audio


# CloneCD's sheet is the third shape: INI-style text beside a raw .img,
# naming the data by stem as an .mds does, with the TOC in [Entry N]
# sections -- one per lead-in point, the A0-A2 disc entries included.  A
# .sub of subchannel data often completes the set; the emulator reads it
# when it is there, so it rides with the disc but is not required of it.


def ccd_summary(path):
    """(referenced file names, track count, audio track count)."""
    tracks = audio = 0
    sessions = 1
    in_disc = in_entry = False
    entries = []
    point = control = None
    try:
        for line in read_cue(path):
            line = line.strip()
            if line.startswith("["):
                if point is not None:
                    entries.append((point, control))
                point = control = None
                low = line.lower()
                in_disc = low.startswith("[disc]")
                in_entry = low.startswith("[entry")
                continue
            key, eq, value = line.partition("=")
            if not eq:
                continue
            try:
                value = int(value.strip(), 0)
            except ValueError:
                continue
            key = key.strip().lower()
            if in_disc and key == "sessions":
                sessions = value
            elif in_entry and key == "point":
                point = value
            elif in_entry and key == "control":
                control = value
        if point is not None:
            entries.append((point, control))
    except OSError:
        return [], 0, 0
    if sessions != 1:
        return [], 0, 0             # one session, as the drive reads
    for point, control in entries:
        if 1 <= point <= 99:
            tracks += 1
            if control is not None and not control & 0x04:
                audio += 1
    if not tracks:
        return [], 0, 0
    return ([os.path.splitext(os.path.basename(path))[0] + ".img"],
            tracks, audio)


SIDECAR_EXTS = (".cue", ".mds", ".ccd")


def is_sidecar(name):
    return name.lower().endswith(SIDECAR_EXTS)


def sidecar_summary(path):
    """(referenced file names, track count, audio count), either kind."""
    if path.lower().endswith(".mds"):
        return mds_summary(path)
    if path.lower().endswith(".ccd"):
        return ccd_summary(path)
    return cue_summary(path)


def sidecar_partner(root, name):
    """The .cue or .mds beside a data file (same stem), or None."""
    stem = os.path.splitext(name)[0]
    for ext in SIDECAR_EXTS:
        for cand in (stem + ext, stem + ext.upper()):
            if os.path.isfile(os.path.join(root, cand)):
                return cand
    return None


def sub_partner(root, name):
    """The CloneCD .sub riding with a data file (same stem), or None."""
    stem = os.path.splitext(name)[0]
    for cand in (stem + ".sub", stem + ".SUB"):
        if os.path.isfile(os.path.join(root, cand)):
            return cand
    return None


def disc_set(root, name):
    """The files an image travels with.  A CD dump is its data file and
    the .cue, .mds or .ccd beside it, which the emulator finds by the
    stem the two share -- a CloneCD set's .sub of subchannel data as
    well; anything else is the one file.  Moving or copying half of
    a set leaves a disc that reads as a single data track with its audio
    gone, so whatever happens to the data file happens to the sheet."""
    if is_sidecar(name):
        return [name]
    partner = sidecar_partner(root, name)
    if not partner:
        return [name]
    if partner.lower().endswith(".ccd"):
        sub = sub_partner(root, name)
        if sub:
            return [name, partner, sub]
    return [name, partner]


def sheet_left_behind(root, name):
    """The sheet a rename or a move cannot take along: one that lives
    beside the file a link points at rather than beside the link itself.
    The drive resolves the link and finds it there whatever the link is
    called -- which is why this is said rather than refused -- but it is
    worth saying, because the shelf is then showing a disc whose sheet is
    named after something else, and the next person to look wonders what
    broke."""
    if is_sidecar(name) or sidecar_partner(root, name):
        return None
    here = os.path.join(root, name)
    real = os.path.realpath(here)
    if real == here:
        return None
    cue = sidecar_partner(os.path.dirname(real), os.path.basename(real))
    return os.path.join(os.path.dirname(real), cue) if cue else None


def point_cue_at(path, was, now):
    """Rewrite the FILE line naming `was` to name `now`, and leave every
    other line of the sheet as it stands -- what is in it beyond the name
    belongs to whoever wrote it.  A sheet naming several data files keeps
    the names of the ones this did not touch."""
    try:
        lines = read_cue(path)
    except OSError:
        return
    out = []
    for line in lines:
        m = CUE_FILE_RE.match(line)
        if m and name_key(os.path.basename((m.group(1) or m.group(2))
                                           .replace("\\", "/"))) \
                == name_key(was):
            nl = "\r\n" if line.endswith("\r\n") else "\n"
            line = 'FILE "%s" %s%s' % (now, m.group(3), nl)
        out.append(line)
    write_cue(path, out)


def cdrom_pairs(root, names):
    """{data name: sidecar name} for the sheets whose data file is here,
    and {sidecar name: [missing files]} for the orphans."""
    pairs, orphans = {}, {}
    lower = {name_key(n): n for n in names}
    for name in names:
        if not is_sidecar(name):
            continue
        files, _, _ = sidecar_summary(os.path.join(root, name))
        here = [lower.get(name_key(f)) for f in files]
        missing = [f for f, h in zip(files, here) if h is None]
        if files and not missing:
            for h in here:
                pairs.setdefault(h, name)
        else:
            orphans[name] = missing or ["(no FILE line)"]
    # a data file that is a link into another folder may keep its sheet
    # there (the emulator resolves the link and looks beside the target)
    for name in names:
        if name in pairs or is_sidecar(name):
            continue
        real = os.path.realpath(os.path.join(root, name))
        if real != os.path.join(root, name):
            cue = sidecar_partner(os.path.dirname(real),
                                  os.path.basename(real))
            if cue:
                pairs[name] = os.path.join(os.path.dirname(real), cue)
    return pairs, orphans


# --------------------------------------------------------- CHD images
# MAME's CHD packs the whole disc into the one file, tracks and all, so
# there is no sidecar to pair it with and no way for it to be an orphan:
# what a .cue names beside the data, a CHD carries inside it.  The
# descriptors sit uncompressed in a chain at the head of the file, and
# the track ones are read here for the same count the sheets give.
CHD_MAGIC = b"MComprHD"
CHD_META_AT = {3: 36, 4: 36, 5: 48}     # where the chain starts, by version
CHD_TRACK_TAGS = (b"CHT2", b"CHTR")
CHD_MAX_META = 200          # entries followed before the chain is given up on


def chd_summary(path):
    """(track count, audio track count) of a CHD, and (0, 0) for anything
    else -- a file that is not one, or one this cannot follow."""
    tracks = audio = 0
    try:
        with open(path, "rb") as f:
            head = f.read(56)
            if len(head) < 56 or not head.startswith(CHD_MAGIC):
                return 0, 0
            version = struct.unpack_from(">I", head, 12)[0]
            if version not in CHD_META_AT:
                return 0, 0
            off = struct.unpack_from(">Q", head, CHD_META_AT[version])[0]
            for _ in range(CHD_MAX_META):
                if not off:
                    break
                f.seek(off)
                entry = f.read(16)
                if len(entry) < 16:
                    break
                if entry[:4] in CHD_TRACK_TAGS:
                    tracks += 1
                    length = int.from_bytes(entry[5:8], "big")
                    if b"TYPE:AUDIO" in f.read(min(length, 512)):
                        audio += 1
                off = struct.unpack_from(">Q", entry, 8)[0]
    except (OSError, struct.error):
        return 0, 0
    return tracks, audio


def fdd_media_label(size):
    return {1261568: "1.23M", 1474560: "1.44M", 737280: "720K",
            655360: "640K", 327680: "320K"}.get(size, "")


def disk_type(kind, name, size, cue=None):
    """The short 'what is this' shown in the Storage list."""
    ext = os.path.splitext(name)[1].lower().lstrip(".") or "-"
    if kind == "cdrom" and cue:
        return ext + "/" + os.path.splitext(cue)[1].lower().lstrip(".")
    if kind == "fdd" and ext == "raw":
        label = fdd_media_label(size)
        return "raw " + label if label else "raw"
    return ext


def disk_catalog():
    """Every image in the tree, with who is using it, its type, the group
    it is shelved in and (for CD dumps) the cue sheet that belongs to it.
    A group is read as its own shelf: a disc and its sheet are in the one
    folder, so they pair there and nowhere else."""
    instances = load_instances()
    out = {}
    for kind in DISK_KINDS:
        entries = []
        for group in [""] + disk_groups(kind):
            root = group_dir(kind, group)
            try:
                names = sorted(os.listdir(root))
            except OSError:
                continue
            names = [n for n in names
                     if not (n.startswith(".") or n.endswith(".part")
                             or not os.path.isfile(os.path.join(root, n)))]
            pairs, orphans = ({}, {})
            if kind == "cdrom":
                pairs, orphans = cdrom_pairs(root, names)
            for name in names:
                full = os.path.join(root, name)
                if kind == "cdrom" and is_sidecar(name) \
                        and name not in orphans:
                    continue                # a sidecar rides with its data
                if kind == "cdrom" and name.lower().endswith(".sub") \
                        and any(os.path.splitext(d)[0]
                                == os.path.splitext(name)[0]
                                and pairs[d].lower().endswith(".ccd")
                                for d in pairs):
                    continue        # a CloneCD .sub rides with its disc too
                st = os.stat(full)
                used = sorted(i["name"] for i in instances
                              if any(disk_path(i, k) == full
                                     for k in DISK_KEYS))
                entry = {"name": name, "size": st.st_size, "group": group,
                         "mtime": int(st.st_mtime), "used_by": used,
                         "type": disk_type(kind, name, st.st_size,
                                           pairs.get(name))}
                if name in pairs:
                    cue_path = os.path.join(root, pairs[name])
                    refs, tracks, audio = sidecar_summary(cue_path)
                    entry.update({"cue": os.path.basename(pairs[name]),
                                  "tracks": tracks, "audio": audio})
                    # the emulator looks for <data stem>.cue beside the
                    # file itself: for a link that is the file it points
                    # at, whose sheet matches its stem however the link is
                    # named -- so a difference there is worth showing and
                    # is not a fault.  A sheet in this folder under a
                    # different stem is one, and the disc will not pair.
                    if os.path.isabs(pairs[name]):
                        entry["cue_where"] = os.path.dirname(pairs[name])
                    elif os.path.splitext(os.path.basename(pairs[name]))[0] \
                            != os.path.splitext(name)[0]:
                        entry["cue_mismatch"] = True
                    if len(refs) > 1:
                        entry["multi"] = len(refs)
                elif name in orphans:
                    entry.update({"orphan": True, "missing": orphans[name]})
                elif kind == "cdrom":
                    tracks, audio = chd_summary(full)
                    if tracks:
                        entry.update({"tracks": tracks, "audio": audio})
                entries.append(entry)
        out[kind] = entries
    return out


# ------------------------------------------------------------ the fleet

_lock = threading.Lock()
_procs = {}                      # name -> Popen, for children we started
_cpu_cache = {}                  # name -> (pid, ticks, when)
# How often to look for machines that ended without being asked to.  Short
# enough that the log line lands while whoever caused it is still watching,
# long enough that an idle server is not measurably busy.
REAP_INTERVAL = 3.0


def real_font_present():
    """Whether the ROM shelf holds a font dumped from a real machine."""
    try:
        return os.path.isfile(os.path.join(CONFIG["roms"], "pc98font.bin"))
    except (KeyError, OSError):
        return False


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
            "font": "real" if real_font_present() else "compat",
            "accel": root.findtext("accel") or "tcg",
            "sound": root.findtext("sound") or DEFAULT_SOUND,
            "midi": root.findtext("midi") or "",
            "serial": root.findtext("serial") or "",
            "parallel": root.findtext("parallel") or "",
            "gpib": root.findtext("gpib") or "",
            "mount": root.findtext("mount") or "",
            "extra": root.findtext("extra") or ""}
    for key in PLUGIN_FIELDS:
        inst[key] = root.findtext(key) or ""
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
    if inst.get("midi"):
        ET.SubElement(root, "midi").text = inst["midi"]
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
    for key in PLUGIN_FIELDS:
        if inst.get(key):
            ET.SubElement(root, key).text = str(inst[key])
    for key in DISK_KEYS:
        if inst.get(key):
            ET.SubElement(root, "disk", dev=key, ref=inst[key])
    tree = ET.ElementTree(root)
    ET.indent(tree)
    os.makedirs(inst_dir(inst["index"]), exist_ok=True)
    path = os.path.join(inst_dir(inst["index"]), "vm.xml")
    # beside itself and then moved into place, for the reason write_cue
    # gives: opening the file that is there empties it before a byte is
    # written, and a write that fails after that leaves a machine with no
    # description of itself at all
    folder = inst_dir(inst["index"])
    fd, beside = tempfile.mkstemp(prefix=".vm.xml.", suffix=".new", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            tree.write(f, encoding="unicode")
            f.flush()
            os.fsync(f.fileno())  # the power may go before the cache does
        os.replace(beside, path)
    except BaseException:
        try:
            os.remove(beside)
        except OSError:
            pass
        raise
    sync_file(folder)


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
              "midi": str(data.get("midi") or "").strip(),
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
    if record["midi"] not in MIDI_MODES:
        return None, "midi must be empty or synth"
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
    for key, validator in PLUGIN_FIELDS.items():
        record[key] = str(data.get(key) or "").strip()
        if validator:
            complaint = validator(record[key])
            if complaint:
                return None, complaint
    if record["machine"] in MACHINE_SANITIZE:
        complaint = MACHINE_SANITIZE[record["machine"]](record)
        if complaint:
            return None, complaint
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
        if not name.lower().endswith((".raw", ".img", ".qcow2")):
            continue    # .hdi is an interchange format, not one to run
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
    told format=raw would happily show a guest the qcow2 container.

    This is the one place a disk reaches a machine, so it is also where
    an Anex86 image is stopped.  QEMU cannot read that container: told
    format=raw it hands the guest the 4096 byte header as the first
    sectors of the disk, and every partition then sits 4096 bytes from
    where the guest looks for it -- a disk that mounts nothing, with no
    error anywhere to say why.  Refusing here catches one whatever path
    put it on the shelf, including the ones that predate this check.
    """
    fmt = "qcow2" if path.lower().endswith(".qcow2") else "raw"
    if fmt == "raw":
        try:
            import virtpc98
            headered = virtpc98.anex86_header(path) is not None
        except Exception:
            headered = False        # unreadable is the caller's problem
        if headered:
            raise ValueError(
                "%s is an Anex86 (.hdi) image, which cannot be run here. "
                "Convert it to raw first." % os.path.basename(path))
    return "format=%s,file=%s" % (fmt, qemu_file(path))


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
    """One line in the system log, and on stderr for whoever is watching.

    One line, and only one: most of what is said here quotes something
    the program did not choose -- a file name, a machine name -- and a
    newline in one of those would start a line of its own, wearing any
    timestamp and tag it liked.  A log is worth reading only while every
    line in it came from where it says it did.
    """
    flat = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    line = "%s [%s] %s" % (time.strftime("%F %T"), kind, flat)
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


def fetch_disk(kind, name, url, group=""):
    """Pull an image off the web into disks/, tracking progress."""
    import urllib.request
    dest = disk_dest(kind, name, group, make=True)
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
              allow_internal=False, check=True, group=""):
    """Copy between an image and a real drive.

    Every refusal is the drives layer's, so that nothing can be written by
    a caller that simply forgot to ask.  Writing also needs `confirm` to
    repeat what the drive says about itself, and is followed by reading it
    back and comparing, because a write that went nowhere and a write that
    worked look exactly alike until someone tries to boot from it.
    """
    path = (disk_find(kind, name) if to_drive
            else disk_dest(kind, name, group, make=True))
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


def qemu_file(path):
    """A path as it goes into one of QEMU's option strings.  Those are
    comma-separated lists, and a comma inside a value has to be doubled
    or the rest of the name is read as another option -- so a disc called
    "Ys I, II.chd" would otherwise be a syntax error rather than a disc.
    The Windows short name is taken first, for the reasons below."""
    return win_short(path).replace(",", ",,")


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
    for where in (CONFIG.get("pc98_roms")
                  or os.path.join(CONFIG["roms"], "pc98"),
                  CONFIG["roms"], CONFIG["datadir"]):
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
    # the boards this machine has anything to play through
    fm, pcm = SOUNDS[sound_of(inst)]
    midi = MIDI_MODES.get(inst.get("midi") or "")
    sound = fm or pcm or midi
    machine = inst.get("machine") or "pc9821"
    # The PC-9821 this emulates is one machine rather than a menu: PEGC
    # for its 256 colours and a GA-98NB beside it, and neither the window
    # accelerator nor the built-in Cirrus.  Name all four either way round,
    # so a VM saved back when they were settings still comes up as the same
    # machine.  A PC-9801 has none of them -- its machine type does not
    # carry the coregraph property at all -- so it gets the bare line.
    # PEGC lives in the bundled compatibility BIOS only: QEMU refuses the
    # pair outright, so a PC-9821 set to boot a real ROM dump gets the
    # card and the two negatives, and takes its 256 colours from the
    # dump's own PEGC support instead.
    machine_opts = (",pegc=on,ga98nb=on,coregraph=off,wab=off"
                    if machine == "pc9821" else "")
    argv = [CONFIG["qemu"],
            "-M", "%s,accel=%s%s%s%s" % (
                machine, accel,
                ",pcspk-audiodev=snd" if sound else "",
                # the CD-ROM drive plays a disc's audio tracks itself, and
                # needs to be told where they go.  On the real machines that
                # went through the sound board, and here it goes through the
                # same mix, so a machine with no board to hear it through
                # has nowhere to put it either.
                ",audiodev=snd" if sound else "",
                machine_opts),
            "-m", inst.get("memory") or "64M",
            # PC-98 is a JIS keyboard; use the Japanese VNC keymap
            "-k", "ja",
            # The calendar clock reads the host's local time rather than
            # UTC, so a guest that has no notion of a timezone -- which is
            # every guest this runs -- shows the same wall clock as the
            # machine it is running on.
            "-rtc", "base=localtime"]
    # The compatibility BIOS is what this boots: PEGC lives in it, and a
    # real dump refuses the pair outright.  The one piece of a real machine
    # worth keeping is its font, so when the shelf holds one the shelf goes
    # first -- QEMU takes the first -L that answers, and the shelf carries
    # no BIOS of its own, so only the font comes from there.
    if real_font_present():
        argv += ["-L", win_short(CONFIG["roms"])]
    # the boards play into a null backend; the VNC server captures that
    # mix and hands it to any client that asks, so the browser hears them
    vnc = "%s:%d,websocket=%d" % ("127.0.0.1" if LOOPBACK else "0.0.0.0",
                                  display, ws)
    if sound:
        vnc += ",audiodev=snd"
    argv += ["-L", win_short(CONFIG["datadir"]),
            "-display", "none",
            "-vnc", vnc,
            "-qmp", "tcp:127.0.0.1:%d,server=on,wait=off" % qmp_port]
    if sound:
        argv += ["-audiodev", "none,id=snd"]
        # the boards are ISA devices of their own, not part of the machine
        if fm:
            argv += ["-device", "pc98-opna,audiodev=snd"]
        if pcm:
            argv += ["-device", "pc98-wss,audiodev=snd"]
        if midi:
            # the board's synthesiser opens on the same audiodev as the
            # rest, so its music arrives in step with the FM and PCM
            board = "pc98-midi,audiodev=snd"
            if CONFIG.get("soundfont"):
                board += ",soundfont=%s" % qemu_file(CONFIG["soundfont"])
            argv += ["-device", board]
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
                     % (unit, qemu_file(os.path.expanduser(inst["mount"])))]
        else:
            argv += ["-drive", "if=ide,bus=0,unit=%d," % unit
                     + drive_backing(disk_path(inst, key))]
    # The CD-ROM drive belongs to the machine whether or not a disc is in
    # it.  QEMU can only load a disc into a drive that already exists, so
    # leaving the drive out would mean a machine started with an empty
    # tray could never be handed a disc without being restarted.
    disc = (("," + drive_backing(disk_path(inst, "cd")))
            if inst.get("cd") else "")
    argv += ["-drive",
             "if=ide,bus=1,unit=0,media=cdrom,readonly=on" + disc]
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
    else:
        # A machine from an earlier server run has no subprocess handle,
        # and a wedged QEMU may never answer the QMP quit; escalate
        # through its recorded pid so the ports actually come free.
        pid = pid_of(inst)
        for _ in range(20):
            if not pid or not is_running(inst):
                break
            time.sleep(0.25)
        if pid and is_running(inst):
            try:
                os.kill(pid, 15)                    # SIGTERM
            except OSError:
                pid = None
            for _ in range(12):
                if not pid or not is_running(inst):
                    break
                time.sleep(0.25)
            if pid and is_running(inst):
                try:
                    os.kill(pid, 9)                 # SIGKILL
                except OSError:
                    pass
    for _ in range(20):
        if not is_running(inst):
            forget_pid(inst)
            say("vm %s stopped" % inst["name"], "vm")
            return "stopped"
        time.sleep(0.25)
    say("vm %s will not stop" % inst["name"], "vm")
    return "still answering; stop it by hand"


def reap_children():
    """Wait for machines that ended without being asked to.

    stop_instance() waits for the ones it stops.  A guest that powers
    itself off -- through APM, or the switch on the emulated front panel --
    ends its QEMU with nobody waiting for it, and the system keeps the
    process entry until someone does, for as long as this server runs.
    poll() is what collects it; the loop is only here to call poll()
    eventually.

    Nothing is removed from _procs.  A handle whose process has ended is
    already treated as gone -- pid_of and stop_instance both ask poll()
    first -- and the next start of that machine replaces it, while removing
    it here would race with a start happening at the same moment.  What is
    said is marked on the handle instead, so a machine is named once.
    """
    while True:
        time.sleep(REAP_INTERVAL)
        for name, proc in list(_procs.items()):
            try:
                code = proc.poll()
            except Exception:
                # a handle in a state we did not expect is not worth
                # taking the whole loop down for
                continue
            if code is None or getattr(proc, "reaped_said", False):
                continue
            proc.reaped_said = True
            say("vm %s exited on its own (code %s, pid %d)"
                % (name, code, proc.pid), "vm")


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
    """A file from web/, kept in memory until the file itself changes.

    A copy that outlives the file it came from is a trap.  This used to
    hold the first read for the life of the process unless --dev was on,
    so a page replaced while the server was up went on being served in
    its old form, with nothing on either side to say so -- and the fault
    was then looked for in the browser, which was faithfully running what
    it had been sent.  The modification time and the size together say
    whether the file is still the one that was read: the time alone
    misses an edit that lands inside one tick, and the size alone misses
    an edit that does not change the length.

    Two threads racing here just read the file twice, which is why there
    is no lock.
    """
    base = os.path.realpath(CONFIG["web"])
    full = os.path.realpath(os.path.join(base, name))
    if not full.startswith(base + os.sep):
        raise ValueError("outside web/: %s" % name)
    # before the cache is consulted, so a file that has gone away is an
    # error the caller turns into 404 rather than a stale copy
    stat = os.stat(full)
    stamp = (stat.st_mtime_ns, stat.st_size)
    if not DEV:
        held = _pages.get(name)
        if held is not None and held[0] == stamp:
            return held[1]
    with open(full, "rb") as f:
        blob = f.read()
    _pages[name] = (stamp, blob)
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

    def parse_request(self):
        # The request line is read before this, and on a connection that is
        # being kept alive that read waits for the browser's next poll.
        # Timing from before the wait made every request look as slow as the
        # polling interval -- three seconds with the tab in front, a minute
        # once the browser throttled it in the background -- which reads as a
        # server that has slowed down when nothing has.  Start the clock once
        # the request is actually in hand.  The stamp above stays as the one
        # for requests that never get this far.
        parsed = BaseHTTPRequestHandler.parse_request(self)
        self.began = time.time()
        return parsed

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
        path = url_path(self.path)
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
        elif path in ("/app.js", "/style.css", "/gamepad.html",
                      "/keytest.html"):
            # gamepad.html answers one question on its own: whether this
            # browser will show a controller to a page at all.  It talks to
            # no machine, so it can be opened with none running.  keytest.html
            # answers the same kind of question for the keyboard: what this
            # browser calls the key you just pressed.
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
        elif path.startswith("/api/fs/"):
            self.fs_page(path[len("/api/fs/"):])
        elif path.startswith("/fsfile/"):
            self.fs_file(path[len("/fsfile/"):])
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
        if kind not in DISK_KINDS or not listed_name(name):
            return None
        return kind, name, disk_find(kind, name)

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
        # what is inside is the file view's business now: listing it here
        # meant unpacking the whole image into a temporary folder first
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
                             attachment(os.path.splitext(name)[0] + ".zip"))
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
        self.send_header("Content-Disposition", attachment(name))
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
        path = url_path(self.path)
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
        if path == "/api/note":
            # A page saying what it sees, so that what happens in a browser
            # can be read from the machine it happened against -- the
            # gamepad check is unreadable from anywhere else.
            #
            # One line, and only ever one: a newline in here would let a
            # page write a line of its own into the log, wearing whatever
            # timestamp and tag it liked, and a log worth reading is one
            # where every line came from where it says it did.
            data = self.body_json() or {}
            say(str(data.get("text") or "")[:800], "web")
            self.reply(200, {"result": "noted"})
            return
        if path.startswith("/api/fs/"):
            rest, _slash, verb = path[len("/api/fs/"):].rpartition("/")
            if verb in ("put", "mkdir", "delete", "rename"):
                self.fs_write(rest, verb)
                return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/rename$", path)
        if m:
            self.rename_disk(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/move$", path)
        if m:
            self.move_disks(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/suggest-groups$", path)
        if m:
            self.suggest_groups(m.group(1))
            return
        m = re.match(r"^/api/disks/(hdd|fdd|cdrom)/import$", path)
        if m:
            self.import_disk(m.group(1))
            return
        if path == "/api/disks/cdrom/set":
            self.settle_cue_set()
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
        m = re.match(r"^/api/instances/([^/]+)/x/([a-z0-9_-]+)$", path)
        if m:
            data = self.body_json()
            if data is None or not isinstance(data, dict):
                self.fail(400, "expected a JSON object")
                return
            with _lock:
                inst = find_instance(load_instances(), m.group(1))
                if inst is None:
                    self.fail(404, "no such instance")
                    return
                fn = PLUGIN_ACTIONS.get((inst.get("machine"), m.group(2)))
                if fn is None:
                    self.fail(404, "no such action for this machine")
                    return
                try:
                    result = fn(inst, data)
                except Exception as err:
                    say("plugin action %s failed: %s" % (m.group(2), err),
                        "web")
                    self.fail(500, "the action failed: %s" % err)
                    return
            if isinstance(result, tuple):
                self.fail(result[0], result[1])
            else:
                self.reply(200, result)
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
            path_ = disk_find(drive["kind"], name) if name else ""
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
        group = (query.get("group") or [""])[0]
        overwrite = (query.get("overwrite") or ["0"])[0] == "1"
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = 0
        if not given_name(name):
            self.refuse_upload(size, 400, NAME_RULE)
            return
        if size <= 0:
            self.fail(400, "empty upload")
            return
        if group and not given_name(group):
            self.refuse_upload(size, 400, "a group is named like an image: " + NAME_RULE)
            return
        # an image is replaced where it stands, whatever group it is in
        if disk_taken(kind, name):
            if not overwrite:
                self.refuse_upload(size, 409, "%s already exists" % name)
                return
            dest = disk_find(kind, name)
        else:
            dest = disk_dest(kind, name, group, make=True)
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

    def move_disks(self, kind):
        """Shelve images in a group, or take them out of one.  A CD dump
        moves with its sheet, and nothing that names an image is touched:
        the name is the same wherever it sits, which is the whole reason
        a group is a folder and not part of the name.  A machine running
        off one of them keeps the file it opened, and finds it again
        under the same name next time it starts."""
        data = self.body_json() or {}
        names = [str(n) for n in (data.get("names") or [])]
        group = str(data.get("group") or "").strip()
        if group and not given_name(group):
            self.fail(400, "a group is named like an image: " + NAME_RULE)
            return
        if not names:
            self.fail(400, "nothing to move")
            return
        done, left = [], []
        with _lock:
            plan = []
            for name in names:
                if not listed_name(name):
                    self.fail(404, "no such disk: %s" % name)
                    return
                src = disk_find(kind, name)
                if not os.path.isfile(src):
                    self.fail(404, "no such disk: %s" % name)
                    return
                was = disk_group_of(kind, name)
                if was == group:
                    continue            # it is already shelved there
                here = os.path.dirname(src)
                if kind == "cdrom":
                    stays = sheet_left_behind(here, name)
                    if stays and os.path.basename(stays) not in left:
                        left.append(os.path.basename(stays))
                for f in (disc_set(here, name) if kind == "cdrom"
                          else [name]):
                    if (was, f) not in plan:
                        plan.append((was, f))
            try:
                for was, f in plan:
                    dest = disk_dest(kind, f, group, make=True)
                    if os.path.lexists(dest):
                        raise FileExistsError("%s is there already" % f)
                    os.replace(os.path.join(group_dir(kind, was), f), dest)
                    done.append((was, f))
            except OSError as err:
                # half a moved set is a disc with no sheet: put it back
                for was, f in reversed(done):
                    os.replace(disk_dest(kind, f, group),
                               os.path.join(group_dir(kind, was), f))
                done = []
                self.fail(409, "could not move: %s" % err)
                return
            for was in {w for w, _ in done}:
                disk_drop_group(kind, was)
        if done:
            say("moved %s %s" % (", ".join(f for _, f in done),
                                 "into %s" % group if group
                                 else "out of its group"), "disk")
        self.reply(200, {"result": "moved", "group": group,
                         "files": [f for _, f in done], "left": left})

    def suggest_groups(self, kind):
        """The groups the names themselves suggest: what stands in front
        of a separator, wherever two or more images share it.  Only ever
        a proposal -- a name is a hint about what a disc belongs to and
        not a statement of it, so nothing moves until this comes back and
        is asked for.  The longest prefix takes a tie, so four
        Towns_A4_* floppies are offered as Towns_A4 rather than as
        Towns, and only images not already shelved are counted."""
        loose = [e for e in disk_catalog()[kind] if not e.get("group")]
        seen = {}
        for entry in loose:
            stem = os.path.splitext(entry["name"])[0]
            for i, ch in enumerate(stem):
                if i > 0 and ch in "_-. ":
                    seen.setdefault(stem[:i], []).append(entry["name"])
        out, spoken = [], set()
        for prefix in sorted(seen, key=lambda p: (-len(seen[p]), -len(p))):
            if not given_name(prefix):
                continue
            names = [n for n in seen[prefix] if n not in spoken]
            if len(names) < 2:
                continue
            spoken.update(names)
            out.append({"group": prefix, "names": sorted(names)})
        self.reply(200, {"result": "suggested", "groups": out})

    def rename_disk(self, kind):
        """Give an image another name.  A CD dump is not one file: the
        sheet beside it is found by the stem the two share, so the set is
        renamed together and a .cue is pointed at the name its data file
        now has.  A machine that names the image follows it -- which is
        why a running one is refused, its command line having been
        written with the name as it was.

        What is asked for is the stem alone: the ending says what the
        image is, to the drives that open it and to this list both, and
        renaming is not the occasion to change what a file is.  It used
        to be given the whole name and refuse one whose ending had
        moved, which asked the typing of something that was then only
        checked -- so now the ending is not asked for at all, and the
        file keeps its own."""
        data = self.body_json() or {}
        # not trimmed: a file whose name really does begin or end with a
        # space is a file, and trimming it here made it unreachable
        name = str(data.get("name") or "")
        stem = str(data.get("stem") or "").strip()
        if not listed_name(name):
            self.fail(404, "no such disk")
            return
        root = os.path.dirname(disk_find(kind, name))
        to = stem + os.path.splitext(name)[1]
        if not stem or not given_name(to):
            self.fail(400, "%s cannot be the name of an image: %s"
                      % (to or "that", NAME_RULE))
            return
        with _lock:
            if not os.path.isfile(os.path.join(root, name)):
                self.fail(404, "no such disk")
                return
            if to == name:
                self.reply(200, {"result": "renamed", "name": name,
                                 "files": [name], "vms": []})
                return
            files = disc_set(root, name) if kind == "cdrom" else [name]
            left = sheet_left_behind(root, name) if kind == "cdrom" else None
            stem = os.path.splitext(to)[0]
            moves = [(name, to)] + [(f, stem + os.path.splitext(f)[1])
                                    for f in files if f != name]
            for _, into in moves:
                if disk_taken(kind, into):
                    self.fail(409, "%s is there already" % into)
                    return
            # a machine names the image, and the name is about to change
            was = {os.path.join(root, f) for f, _ in moves}
            users = [i for i in load_instances()
                     if any(disk_path(i, k) in was for k in DISK_KEYS)]
            running = [i["name"] for i in users if is_running(i)]
            if running:
                self.fail(409, "started with the name as it is, and would "
                          "not find it under another until stopped: %s"
                          % ", ".join(running))
                return
            became = {os.path.join(root, f): into for f, into in moves}
            done, was_named, touched = [], [], []
            try:
                for f, into in moves:
                    os.replace(os.path.join(root, f),
                               os.path.join(root, into))
                    done.append((f, into))
                for _, into in moves[1:]:
                    if into.lower().endswith(".cue"):
                        point_cue_at(os.path.join(root, into), name, to)
                for inst in users:
                    before = {k: inst.get(k) for k in DISK_KEYS}
                    for key in DISK_KEYS:
                        old = disk_path(inst, key)
                        if old not in became:
                            continue
                        ref = inst[key]
                        inst[key] = (os.path.join(os.path.dirname(ref),
                                                  became[old])
                                     if "/" in ref or ref.startswith("~")
                                     else became[old])
                        if inst["name"] not in touched:
                            touched.append(inst["name"])
                    if inst["name"] in touched:
                        was_named.append((inst, before))
                        save_instance(inst)
            except (OSError, ValueError) as err:
                # Everything, not only the files.  A sheet rewritten
                # under the new name and machines pointed at it are as
                # much a half-done rename as a moved file is, and the
                # earlier version of this put back only the moves -- so a
                # full disk left a disc whose sheet named a file that was
                # no longer there and a machine that named it too.
                for inst, before in reversed(was_named):
                    for key, value in before.items():
                        if value is None:
                            inst.pop(key, None)
                        else:
                            inst[key] = value
                    try:
                        save_instance(inst)
                    except OSError:
                        pass
                for f, into in reversed(done):
                    try:
                        os.replace(os.path.join(root, into),
                                   os.path.join(root, f))
                    except OSError:
                        pass
                for f, into in done[1:]:
                    if f.lower().endswith(".cue"):
                        point_cue_at(os.path.join(root, f), to, name)
                self.fail(500, "could not rename: %s" % err)
                return
        say("renamed %s to %s" % (", ".join(f for f, _ in moves),
                                  ", ".join(t for _, t in moves)), "disk")
        if left:
            say("%s stayed in %s, beside the file %s points at"
                % (os.path.basename(left), os.path.dirname(left), to), "disk")
        for vm in touched:
            say("vm %s: follows %s to %s" % (vm, name, to), "vm")
        self.reply(200, {"result": "renamed", "name": to,
                         "files": [t for _, t in moves], "vms": touched,
                         "left": os.path.basename(left) if left else ""})

    # --------------------------------------------------- inside an image
    def fs_target(self, rest, writable=False):
        """(kind, name, path) of an image the file view may open.

        A machine with the image open is writing to it, so its files are
        not ours to move around underneath it -- the same rule renaming an
        image already follows.
        """
        ref = self.disk_ref(rest)
        if ref is None or not os.path.isfile(ref[2]):
            self.fail(404, "no such disk")
            return None
        kind, name, full = ref
        if kind == "cdrom":
            self.fail(400, "a disc image is read-only")
            return None
        if writable:
            with _lock:
                busy = sorted(i["name"] for i in load_instances()
                              if is_running(i)
                              and any(disk_path(i, k) == full
                                      for k in DISK_KEYS))
            if busy:
                self.fail(409, "%s has this image open: stop it first"
                          % ", ".join(busy))
                return None
        return kind, name, full

    def fs_drain(self):
        """Swallow a body whose request has already been turned down: left
        in the socket it would be read back as the next request."""
        left = int(self.headers.get("Content-Length", 0) or 0)
        while left > 0:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                break
            left -= len(chunk)

    def fs_query(self):
        """(partition, path inside the image, a name) out of the URL."""
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        try:
            part = max(1, int((q.get("partition") or ["1"])[0]))
        except ValueError:
            part = 1
        return part, (q.get("path") or ["/"])[0], (q.get("name") or [""])[0]

    def fs_page(self, rest):
        """One directory inside an image, and what the volume looks like."""
        target = self.fs_target(rest)
        if target is None:
            return
        _kind, _name, full = target
        partition, where, _n = self.fs_query()
        if not fs_path_ok(where):
            self.fail(400, "bad path")
            return
        import virtpc98
        try:
            with fs_lock(full), virtpc98.Volume(full, partition) as vol:
                free, total = vol.usage()
                out = {"partition": partition, "path": where,
                       "fat": vol.fat.bits, "cluster": vol.fat.clustersize,
                       "free": free, "total": total,
                       "partitions": [{"n": i + 1, "name": nm} for i, (nm, _o)
                                      in enumerate(vol.table)]
                                     or [{"n": 1, "name": ""}],
                       "entries": [{"name": e["name"], "long": e["long"],
                                    "dir": e["dir"], "size": e["size"],
                                    "attr": e["attr"],
                                    "modified": fat_stamp(e["date"],
                                                          e["time"])}
                                   for e in vol.listdir(where)]}
        except Exception as err:
            self.fail(400, "%s" % err)
            return
        self.reply(200, out)

    def fs_file(self, rest):
        """One file out of an image."""
        target = self.fs_target(rest)
        if target is None:
            return
        _kind, _name, full = target
        partition, where, _n = self.fs_query()
        if not fs_path_ok(where):
            self.fail(400, "bad path")
            return
        import virtpc98
        try:
            with fs_lock(full), virtpc98.Volume(full, partition) as vol:
                blob = vol.read_file(where)
        except Exception as err:
            self.fail(404, "%s" % err)
            return
        leaf = where.replace("\\", "/").rstrip("/").rpartition("/")[2] \
            or "file"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Content-Disposition", attachment(leaf))
        self.end_headers()
        self.wfile.write(blob)

    def fs_write(self, rest, verb):
        """Put a file in, make a directory, remove one, or rename one."""
        writable = self.fs_target(rest, writable=True)
        if writable is None:
            if verb == "put":
                self.fs_drain()      # the body is already on its way in
            return
        _kind, name, full = writable
        partition, where, filename = self.fs_query()
        import virtpc98
        if verb == "put":
            try:
                size = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                size = -1
            if size < 0:
                self.refuse(400, "the request does not say how long it is")
                return
            if not filename or not fs_path_ok(where):
                self.refuse(400, "the file needs a name and a place")
                return
            if size > FS_MAX_PUT:
                self.refuse_upload(size, 413,
                                   "that file is too big to place here")
                return
            blob = self.rfile.read(size) if size else b""
            try:
                with fs_lock(full), \
                        virtpc98.Volume(full, partition, writable=True) as vol:
                    stored = vol.put(where, filename, blob)
            except ValueError as err:
                self.fail(409, "%s" % err)
                return
            except Exception as err:
                self.fail(400, "%s" % err)
                return
            say("%s: %s written into %s" % (name, stored, where or "/"),
                "disk")
            self.reply(200, {"result": "written", "name": stored,
                             "renamed": stored.upper() != filename.upper()})
            return
        data = self.body_json() or {}
        where = str(data.get("path") or where)
        if not fs_path_ok(where):
            self.fail(400, "bad path")
            return
        try:
            with fs_lock(full), \
                    virtpc98.Volume(full, partition, writable=True) as vol:
                if verb == "mkdir":
                    leaf = str(data.get("name") or "").strip()
                    if not leaf:
                        self.fail(400, "the folder needs a name")
                        return
                    made = vol.mkdir(where, leaf)
                    say("%s: %s created in %s" % (name, made, where or "/"),
                        "disk")
                    self.reply(200, {"result": "created", "name": made})
                elif verb == "delete":
                    paths = data.get("paths") or ([where] if where != "/"
                                                  else [])
                    if not paths:
                        self.fail(400, "nothing to remove")
                        return
                    gone = 0
                    for one in paths:
                        if not fs_path_ok(one):
                            continue
                        gone += vol.delete(one)
                    say("%s: %d removed" % (name, gone), "disk")
                    self.reply(200, {"result": "removed", "count": gone})
                else:
                    leaf = str(data.get("name") or "").strip()
                    if not leaf:
                        self.fail(400, "the new name is missing")
                        return
                    got = vol.rename(where, leaf)
                    say("%s: %s renamed to %s" % (name, where, got), "disk")
                    self.reply(200, {"result": "renamed", "name": got,
                                     "renamed": got.upper() != leaf.upper()})
        except FileExistsError as err:
            self.fail(409, "%s is already there" % err)
        except FileNotFoundError as err:
            self.fail(404, "%s" % err)
        except ValueError as err:
            self.fail(409, "%s" % err)
        except Exception as err:
            self.fail(400, "%s" % err)

    def disk_action(self, kind, name, verb):
        """Write a ZIP into an image, or duplicate the image itself."""
        if not listed_name(name):
            self.fail(400, "bad name")
            return
        source = disk_find(kind, name)
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
            if not given_name(target):
                self.fail(400, "the copy needs a name")
                return
            root = os.path.dirname(source)      # the copy sits beside it
            files = disc_set(root, name) if kind == "cdrom" else [name]
            stem = os.path.splitext(target)[0]
            copies = [(name, target)] + [(f, stem + os.path.splitext(f)[1])
                                         for f in files if f != name]
            for _, made in copies:
                if disk_taken(kind, made):
                    self.fail(409, "%s already exists" % made)
                    return
            if sum(os.path.getsize(os.path.join(root, f))
                   for f, _ in copies) > free_bytes(root):
                self.fail(507, "not enough room for a copy")
                return
            for f, made in copies:
                shutil.copy2(os.path.join(root, f), os.path.join(root, made))
            # the copied sheet names the file it was copied beside
            for _, made in copies[1:]:
                if made.lower().endswith(".cue"):
                    point_cue_at(os.path.join(root, made), name, target)
            say("copied %s to %s" % (", ".join(f for f, _ in copies),
                                     ", ".join(t for _, t in copies)), "disk")
            self.reply(200, {"result": "copied", "name": target,
                             "files": [t for _, t in copies]})
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
        group = str(data.get("group") or "").strip()
        if not given_name(name):
            self.fail(400, "the image needs a name")
            return
        if group and not given_name(group):
            self.fail(400, "a group is named like an image: " + NAME_RULE)
            return
        if disk_taken(kind, name):
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
        # compose before the check, not after: the byte length the
        # check enforces is the length of what will be stored
        name = nfc((query.get("name") or [""])[0])
        group = nfc((query.get("group") or [""])[0])
        if not given_name(name) or (group and not given_name(group)):
            return None, None, query
        # the part file waits where the image itself will end up, so the
        # last slice is a rename inside one folder and never a copy
        if disk_taken(kind, name):
            return name, disk_find(kind, name) + ".part", query
        return name, disk_dest(kind, name, group, make=True) + ".part", query

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
        dest = part[:-len(".part")]
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
        dest = part[:-len(".part")]
        os.replace(part, dest)
        final = self.auto_convert(kind, dest)
        say("uploaded %s (%d bytes)" % (final, have), "disk")
        self.reply(200, {"result": "uploaded", "name": final, "size": have})

    def auto_convert(self, kind, dest):
        """Uploaded containers become what the tree prefers: an HDI turns
        into qcow2, an FDI into a bare raw floppy.  Returns the name the
        upload ended up under; trouble just keeps the original."""
        import virtpc98
        stem, ext = os.path.splitext(dest)
        ext = ext.lower()
        # What the file is, not what it is called.  An Anex86 image keeps
        # a 4096 byte header in front of the disk, and every image but a
        # qcow2 is handed to QEMU flat -- so one left on the shelf has its
        # whole disk 4096 bytes from where its guest looks for it, whatever
        # the name says.  Uploads arrive named anything.
        headered = (kind == "hdd"
                    and virtpc98.anex86_header(dest) is not None)
        if headered or (kind, ext) == ("hdd", ".hdi"):
            target = os.path.splitext(dest)[0] + ".qcow2"
        elif (kind, ext) in (("fdd", ".fdi"), ("fdd", ".nfd")):
            target = stem + ".raw"
        else:
            return os.path.basename(dest)
        # not just "is it here": a name this kind already holds in a group
        # would make two files of one name, and everything that looks one
        # up by name -- including Delete -- would then find whichever comes
        # first and leave the other
        if disk_taken(kind, os.path.basename(target)) or os.path.lexists(target):
            # Never clobber sideways -- but an Anex86 image cannot simply
            # be left where it is either, so the upload has to fail rather
            # than sit on the shelf looking usable.
            if headered:
                os.remove(dest)
                raise ValueError(
                    "%s is an Anex86 image and has to be converted, but "
                    "%s is already taken" % (os.path.basename(dest),
                                             os.path.basename(target)))
            return os.path.basename(dest)
        try:
            import virtpc98
            payload = (virtpc98.nfd_to_raw(dest) if ext == ".nfd"
                       else virtpc98.read_image(dest)[0])
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

    def settle_stem_set(self, root, sheet, files):
        """A descriptor names its data by stem -- "*.mdf" is the one
        sharing an .mds's, and a .ccd's is the .img beside it, with the
        .sub of subchannel data riding along -- so there are no names in
        it to point at what was stored.  The stem is the whole of the
        pairing, and the lone data file that came up with it takes it,
        whatever rides with it following."""
        present = {name_key(n): n for n in os.listdir(root)
                   if os.path.isfile(os.path.join(root, n))}
        chosen = [present[name_key(f)] for f in files
                  if name_key(f) in present]
        data = [f for f in chosen if not f.lower().endswith(".sub")]
        if not data:
            self.fail(400, "%s came without its data file" % sheet)
            return
        if len(data) == 1:
            stem = os.path.splitext(sheet)[0]
            renamed = []
            for f in chosen:
                want = stem + os.path.splitext(f)[1]
                if f != want and not disk_taken("cdrom", want):
                    os.replace(os.path.join(root, f),
                               os.path.join(root, want))
                    f = want
                renamed.append(f)
            chosen = renamed
        _, tracks, audio = sidecar_summary(os.path.join(root, sheet))
        say("disc set %s: %d track(s), %d audio" % (sheet, tracks, audio),
            "disk")
        self.reply(200, {"result": "set", "cue": sheet, "files": chosen,
                         "tracks": tracks, "audio": audio,
                         "multi": len(data) > 1})

    def settle_cue_set(self):
        """A cue sheet and its data files have all arrived: point the
        sheet's FILE lines at the names they were stored under (uploads
        get safe names, and sheets often name the file with a path or a
        different case) and check the set is complete.  A binary .mds
        or a CloneCD .ccd carries no such names and is settled by its
        stem instead."""
        data = self.body_json() or {}
        cue = str(data.get("cue") or "")
        files = [str(f) for f in (data.get("files") or [])]
        if not given_name(cue) or not is_sidecar(cue):
            self.fail(400, "bad sheet name")
            return
        cue_path = disk_find("cdrom", cue)
        root = os.path.dirname(cue_path)
        if not os.path.isfile(cue_path):
            self.fail(404, "no such cue sheet")
            return
        if cue.lower().endswith((".mds", ".ccd")):
            self.settle_stem_set(root, cue, files)
            return
        present = {name_key(n): n for n in os.listdir(root)
                   if os.path.isfile(os.path.join(root, n))}
        given = [f for f in files if name_key(f) in present]
        try:
            lines = read_cue(cue_path)
        except OSError as err:
            self.fail(400, "cannot read the cue: %s" % err)
            return
        refs = [m for m in (CUE_FILE_RE.match(l) for l in lines) if m]
        if not refs:
            self.fail(400, "%s has no FILE line" % cue)
            return
        rewritten, missing, chosen = [], [], []
        pending = list(given)
        for line in lines:
            m = CUE_FILE_RE.match(line)
            if not m:
                rewritten.append(line)
                continue
            ref = os.path.basename((m.group(1) or m.group(2))
                                   .replace("\\", "/"))
            key = name_key(ref)
            safe = name_key(safe_disk_name(ref))
            pick = None
            for cand in (key, safe):
                if cand in present:
                    pick = present[cand]
                    break
            # No guessing by position: pointing a sheet at whichever
            # upload came next rewrites the disc into something nobody
            # asked for, and answers 200 while doing it.
            if pick is None:
                missing.append(ref)
                rewritten.append(line)
                continue
            if pick in pending:
                pending.remove(pick)
            chosen.append(pick)
            nl = "\r\n" if line.endswith("\r\n") else "\n"
            rewritten.append('FILE "%s" %s%s' % (pick, m.group(3), nl))
        if missing:
            self.fail(400, "the cue names %s, which was not uploaded"
                      % ", ".join(missing))
            return
        write_cue(cue_path, rewritten)
        _, tracks, audio = cue_summary(cue_path)
        # the disc takes the sheet's stem: data.bin + data.cue is one set
        # the emulator finds without help; rename a lone data file to match
        stem = os.path.splitext(cue)[0]
        if len(chosen) == 1:
            data_name = chosen[0]
            want = stem + os.path.splitext(data_name)[1]
            if data_name != want and not disk_taken("cdrom", want):
                os.replace(os.path.join(root, data_name),
                           os.path.join(root, want))
                write_cue(cue_path, [
                    'FILE "%s" %s%s' % (want, CUE_FILE_RE.match(l).group(3),
                                        "\r\n" if l.endswith("\r\n") else "\n")
                    if CUE_FILE_RE.match(l) else l
                    for l in read_cue(cue_path)])
                chosen = [want]
        say("cue set %s: %d track(s), %d audio" % (cue, tracks, audio),
            "disk")
        self.reply(200, {"result": "set", "cue": cue, "files": chosen,
                         "tracks": tracks, "audio": audio,
                         "multi": len(chosen) > 1})

    def import_disk(self, kind):
        """Adopt a file already on the server, without moving it.  A CD
        cue sheet brings the data files it names along, and a data file
        its sheet, so the disc arrives whole."""
        data = self.body_json()
        path = os.path.expanduser(str((data or {}).get("path", "")))
        if not os.path.isfile(path):
            self.fail(400, "%s is not a file" % path)
            return
        name = os.path.basename(path)
        into = str((data or {}).get("group") or "").strip()
        if not given_name(name):
            self.fail(400, "file name %s is too strange to adopt" % name)
            return
        if into and not given_name(into):
            self.fail(400, "a group is named like an image: " + NAME_RULE)
            return
        group = [path]
        if kind == "cdrom":
            folder = os.path.dirname(path)
            if is_sidecar(name):
                refs, _, _ = sidecar_summary(path)
                for ref in refs:
                    cand = os.path.join(folder, ref)
                    if not os.path.isfile(cand):
                        self.fail(400, "the cue names %s, which is not "
                                  "beside it" % ref)
                        return
                    group.append(cand)
                if name.lower().endswith(".ccd"):
                    sub = sub_partner(folder, name)
                    if sub:
                        group.append(os.path.join(folder, sub))
            else:
                group += [os.path.join(folder, f)
                          for f in disc_set(folder, name)[1:]]
        for src in group:
            base = os.path.basename(src)
            if not given_name(base):
                self.fail(400, "file name %s is too strange to adopt" % base)
                return
            if disk_taken(kind, base):
                self.fail(409, "%s already exists" % base)
                return
        for src in group:
            dest = disk_dest(kind, os.path.basename(src), into, make=True)
            try:
                os.link(src, dest)         # instant on the same filesystem
            except OSError:
                shutil.copy2(src, dest)
        self.reply(200, {"result": "imported", "name": name,
                         "files": [os.path.basename(g) for g in group]})

    def fetch(self, kind):
        data = self.body_json() or {}
        url = str(data.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self.fail(400, "the URL must start with http:// or https://")
            return
        name = str(data.get("name") or "").strip()
        group = str(data.get("group") or "").strip()
        if group and not given_name(group):
            self.fail(400, "a group is named like an image: " + NAME_RULE)
            return
        if not name:
            from urllib.parse import unquote, urlparse
            name = os.path.basename(unquote(urlparse(url).path))
        if not given_name(name):
            self.fail(400, "cannot tell what to call it; give a file name")
            return
        if disk_taken(kind, name):
            self.fail(409, "%s already exists" % name)
            return
        fetch_disk(kind, name, url, group)
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
        if not given_name(name):
            self.fail(400, NAME_RULE)
            return
        group = str(data.get("group") or "").strip()
        if group and not given_name(group):
            self.fail(400, "a group is named like an image: " + NAME_RULE)
            return
        fmt = str(data.get("format") or "")
        if kind == "hdd" and (kind, fmt) not in DISK_BUILDERS:
            # The container is decided here rather than typed.  A name
            # that ended in .hdi made an Anex86 image -- a 4096 byte
            # header in front of the disk -- and every image but a qcow2
            # is opened flat, so that header was read as the first
            # sectors and every partition sat 4096 bytes from where the
            # guest looks for it.  Such a disk is not readable by the
            # guest it was made for.
            name = re.sub(r"\.(raw|img|hdi|qcow2)$", "", name, flags=re.I)
            name += ".qcow2" if fmt == "qcow2" else ".raw"
        elif kind == "fdd" and (kind, fmt) not in DISK_BUILDERS:
            # The courtesy the hard disk already had.  A floppy left
            # without an extension was not merely unlabelled: virtpc98
            # puts an Anex86 header in front of anything that is not
            # .raw or .img, so the image came out with 4096 bytes ahead
            # of its boot sector and no PC-98 could read it.
            if os.path.splitext(name)[1].lower() not in FDD_CONTAINERS:
                name += ".raw"
        if disk_taken(kind, name):
            self.fail(409, "%s already exists" % name)
            return
        dest = disk_dest(kind, name, group, make=True)
        cap = size_limit(disks_root(kind))
        wanted = int(data.get("size") or 40) << 20
        if kind == "hdd" and cap and wanted > cap:
            self.fail(507, "this storage is FAT32: no file over 4 GB")
            return
        quiet = lambda *a: None
        try:
            import virtpc98
            builder = DISK_BUILDERS.get((kind, str(data.get("format") or "")))
            if builder:
                builder(dest, data)
            elif kind == "hdd":
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
        # No raw-to-hdi: an Anex86 image on this shelf is handed to QEMU
        # flat, which reads its header as the first sectors of the disk.
        # The way out to another emulator is to take the .raw away.
        ("hdd", ".raw", "qcow2"): ("raw-to-qcow2", ".qcow2"),
        ("hdd", ".img", "qcow2"): ("raw-to-qcow2", ".qcow2"),
        ("hdd", ".qcow2", "raw"): ("qcow2-to-raw", ".raw"),
        ("fdd", ".fdi", "raw"): ("fdi-to-raw", ".raw"),
        ("fdd", ".nfd", "raw"): ("nfd-to-raw", ".raw"),
        ("fdd", ".raw", "fdi"): ("raw-to-fdi", ".fdi"),
        ("fdd", ".img", "fdi"): ("raw-to-fdi", ".fdi"),
        ("fdd", ".raw", "nfd"): ("raw-to-nfd", ".nfd"),
        ("fdd", ".img", "nfd"): ("raw-to-nfd", ".nfd"),
    }

    def convert_disk(self, kind):
        data = self.body_json() or {}
        source = str(data.get("source") or "")
        target = str(data.get("format") or "")
        if not listed_name(source):
            self.fail(400, "bad source name")
            return
        stem, ext = os.path.splitext(source)
        plan = self.CONVERSIONS.get((kind, ext.lower(), target))
        if plan is None:
            self.fail(400, "cannot make %s out of %s" % (target, source))
            return
        command, out_ext = plan
        src = disk_find(kind, source)
        dest = os.path.join(os.path.dirname(src), stem + out_ext)
        if not os.path.isfile(src):
            self.fail(404, "no such disk")
            return
        if disk_taken(kind, os.path.basename(dest)):
            self.fail(409, "%s already exists" % os.path.basename(dest))
            return
        if os.path.getsize(src) > free_bytes(os.path.dirname(dest)):
            self.fail(507, "not enough room to convert it")
            return
        try:
            import virtpc98
            virtpc98.run_command(command, src, dest, {},
                                 log=lambda *a: None)
        except Exception as err:
            # what it got as far as writing is not an image; a stub left
            # on the shelf is listed as though it were one
            try:
                os.remove(dest)
            except OSError:
                pass
            self.fail(400, "convert failed: %s" % err)
            return
        self.reply(200, {"result": "converted",
                         "name": os.path.basename(dest)})

    def do_DELETE(self):
        if not self.signed_in():
            self.refuse(401, "sign in first")
            return
        path = url_path(self.path)
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
            was = disk_group_of(kind, name)
            os.remove(full)
            if kind == "cdrom" and not is_sidecar(name):
                cue = sidecar_partner(os.path.dirname(full), name)
                if cue:
                    try:
                        os.remove(os.path.join(os.path.dirname(full), cue))
                    except OSError:
                        pass
            disk_drop_group(kind, was)
        self.reply(200, {"result": "deleted"})

    def do_PUT(self):
        if not self.signed_in():
            self.refuse(401, "sign in first")
            return
        m = re.match(r"^/api/instances/([^/]+)$", url_path(self.path))
        if not m:
            self.fail(404, "no such page")
            return
        name = m.group(1)
        data = self.body_json()
        if data is None:
            self.fail(400, "bad JSON")
            return
        # the URL says which machine; the body may carry a new name
        data["name"] = str(data.get("name") or name)
        with _lock:
            instances = load_instances()
            inst = find_instance(instances, name)
            if inst is None:
                self.fail(404, "no such instance")
                return
            if is_running(inst):
                self.fail(409, "shut it down first")
                return
            record, complaint = sanitize(
                data, {i["name"] for i in instances} - {name})
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
    # a machine the guest shut down from inside leaves its QEMU waiting to
    # be collected; nothing else in this program ever waits for one
    threading.Thread(target=reap_children, daemon=True).start()
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
    # a burst -- a browser putting a handful of files into an image one
    # after another, or two tabs at once -- should wait in the queue
    # rather than be turned away at the door by the default backlog of 5
    ThreadingHTTPServer.request_queue_size = 64
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
