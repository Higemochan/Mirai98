"""Drives as Windows sees them.

Enumeration is one PowerShell call returning JSON, which is the same shape
of problem as lsblk -J and is parsed the same way.

Writing needs the drive taken away from Windows first.  An online disk is
one Windows has a filesystem cache for, and that cache will happily write
its own idea of the disk over ours minutes later; the image then looks
written and is quietly wrong.  Set-Disk -IsOffline is the documented way
to stop that, and it also releases the drive letters, which is what
locking and dismounting each volume by hand would otherwise be for.
"""

import json
import os
import subprocess

# Get-Disk gives the disk, Get-Partition the drive letters that make it
# busy.  IsSystem or IsBoot means Windows itself is on it.
LIST = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-Disk | ForEach-Object {
  $d = $_
  $letters = @(Get-Partition -DiskNumber $d.Number |
               Where-Object DriveLetter |
               ForEach-Object { "$($_.DriveLetter):" })
  [pscustomobject]@{
    number    = $d.Number
    path      = "\\.\PHYSICALDRIVE$($d.Number)"
    size      = [int64]$d.Size
    sector    = [int]$d.LogicalSectorSize
    model     = "$($d.FriendlyName)".Trim()
    bus       = "$($d.BusType)"
    removable = ($d.BusType -in 'USB','SD','MMC')
    readonly  = [bool]$d.IsReadOnly
    system    = ([bool]$d.IsSystem -or [bool]$d.IsBoot)
    letters   = $letters
  }
} | ConvertTo-Json -Depth 3 -AsArray
"""


def _powershell(script, timeout=60):
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=timeout)
    return out


def _json(script, timeout=60):
    try:
        out = _powershell(script, timeout)
        return json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _human(size):
    size = float(size or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return ("%.1f%s" % (size, unit)).replace(".0", "")
        size /= 1024
    return ""


def enumerate_drives():
    out = []
    for disk in _json(LIST):
        letters = disk.get("letters") or []
        if isinstance(letters, str):
            letters = [letters]
        size = int(disk.get("size") or 0)
        out.append({
            "path": disk.get("path") or "",
            "size": _human(size),
            "size_bytes": size,
            "sector": int(disk.get("sector") or 512) or 512,
            # Windows does not say whether a disk is a CD or a floppy in
            # the same breath as its size, and neither is written whole
            "type": "hdd",
            "removable": bool(disk.get("removable")),
            "readonly": bool(disk.get("readonly")),
            "model": disk.get("model") or "",
            # a drive letter means Windows has it, and a cache for it
            "busy": ", ".join(letters),
            "system": bool(disk.get("system")),
        })
    return out


def _offline(number, yes):
    _powershell("Set-Disk -Number %d -IsOffline $%s"
                % (number, "true" if yes else "false"))


def _number(path):
    tail = path.rstrip("\\").upper().rsplit("PHYSICALDRIVE", 1)
    try:
        return int(tail[1])
    except (IndexError, ValueError):
        raise ValueError("not a physical drive path: %s" % path)


class _Raw:
    """A drive, open, with Windows told to keep its hands off it.

    Only the writing side takes it offline; reading does not need to and
    should not disturb anything.
    """

    def __init__(self, path, writable):
        self.path = path
        # not "write": that is the name of a method below
        self.writable = writable
        self.number = _number(path)
        if writable:
            _offline(self.number, True)
            _powershell("Set-Disk -Number %d -IsReadOnly $false"
                        % self.number)
        try:
            self.f = open(path, "r+b" if writable else "rb", buffering=0)
        except OSError:
            if writable:
                _offline(self.number, False)
            raise

    def read(self, n):
        return self.f.read(n)

    def write(self, data):
        return self.f.write(data)

    def flush(self):
        return self.f.flush()

    def fileno(self):
        return self.f.fileno()

    def close(self):
        try:
            self.f.close()
        finally:
            if self.writable:
                _offline(self.number, False)
                # so Explorer notices the drive changed under it
                _powershell("Update-HostStorageCache")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_read(path):
    return _Raw(path, writable=False)


def open_write(path):
    return _Raw(path, writable=True)
