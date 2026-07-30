"""Real drives: what is attached, and reading and writing them whole.

Two implementations behind one door, because what a drive is differs
between the appliance and Windows but what is done with one does not.
Everything above this point works in the same terms either way.

The refusals live here on purpose.  A caller that forgets to ask, or a
page that loses its confirmation dialog, must not be able to write over
somebody's system disk; so the checks are made where the writing happens,
and there is no way past them from above.
"""

import hashlib
import os
import time

WINDOWS = os.name == "nt"

if WINDOWS:
    from . import windows as impl
else:
    from . import linux as impl

# How much is moved at a time.  A multiple of 4096 as well as of 512, so
# it satisfies raw device I/O on Windows whatever the sector size is.
CHUNK = 4 << 20


class Refused(Exception):
    """This layer will not do it.  The message says why, for the user."""


# Asking costs a subprocess — lsblk here, PowerShell on Windows, where it
# is most of a second — and the page asks on every render.  So a listing is
# kept for a moment.  Nothing that decides whether to write uses it: see
# check_read and check_write, which always look again.
LISTING_AGE = 4.0
_listing = {"at": 0.0, "drives": []}


def enumerate_drives(fresh=False):
    """Every drive the host can see, as dictionaries all alike:

    path        what to open, and what a machine's config records
    size        for people, like "465.8G"
    size_bytes  0 if it could not be worked out
    sector      logical sector size, 512 unless told otherwise
    type        hdd, fdd or cdrom
    removable   True for something the user can unplug
    readonly    True if it cannot be written at all
    busy        non-empty if the host is using it, and what for
    system      True if the host lives on it: never writable from here
    model       whatever the drive calls itself

    A listing from the last few seconds is reused unless `fresh` says not
    to.  Anything about to write asks fresh.
    """
    if not fresh and _listing["drives"] \
            and time.time() - _listing["at"] < LISTING_AGE:
        return _listing["drives"]
    found = impl.enumerate_drives()
    _listing.update(at=time.time(), drives=found)
    return found


def find(path, fresh=False):
    """The drive at that path, or None."""
    return next((d for d in enumerate_drives(fresh)
                 if d["path"] == path), None)


def is_device(path):
    """Whether a path names a drive rather than a file."""
    return path.startswith("/dev/") or path.startswith("\\\\.\\")


# ------------------------------------------------------------ the refusals

def _wanted(path):
    # always a fresh look: a listing from a moment ago could describe a
    # drive that has since been unplugged, and something else put in its
    # place would then answer to the confirmation given for the first one
    drive = find(path, fresh=True)
    if drive is None:
        raise Refused("%s is not there" % path)
    return drive


def check_read(path):
    """A drive can be read unless the host is in the middle of using it."""
    drive = _wanted(path)
    if drive["busy"]:
        # reading a mounted filesystem gives a torn copy, which looks
        # like a working image until something is missing from it
        raise Refused("%s is mounted on %s" % (path, drive["busy"]))
    return drive


def check_write(path, confirm="", allow_internal=False):
    """A drive can be written only deliberately.

    The caller has to repeat something the drive itself says, and that is
    checked here against the drive rather than against anything the caller
    passed in earlier.  Writing to an internal disk takes saying so as
    well, and writing to the host's own is never allowed.
    """
    drive = _wanted(path)
    if drive["system"]:
        raise Refused("%s is the host's own disk" % path)
    if drive["readonly"]:
        raise Refused("%s is read-only" % path)
    if drive["busy"]:
        raise Refused("%s is mounted on %s" % (path, drive["busy"]))
    if not drive["removable"] and not allow_internal:
        raise Refused("%s is an internal disk; say so explicitly to write "
                      "to it" % path)
    given = str(confirm or "").strip()
    if not given:
        raise Refused("confirm the drive by its model or its size in bytes")
    if given.casefold() != (drive["model"] or "").strip().casefold() \
            and given != str(drive["size_bytes"]):
        raise Refused("that does not match %s: it says %r, %d bytes"
                      % (path, drive["model"], drive["size_bytes"]))
    return drive


# --------------------------------------------------------------- the moving

def reading(path):
    """Open a drive to read it whole.  Returns (file, length, sector)."""
    drive = check_read(path)
    return impl.open_read(path), drive["size_bytes"], drive["sector"]


def writing(path, confirm="", allow_internal=False):
    """Open a drive to write it whole.  Returns (file, room, sector).

    Whatever the implementation has to do first — on Windows, taking the
    drive away from the host so its cache cannot write over us — is done
    here and undone by closing.
    """
    drive = check_write(path, confirm, allow_internal)
    return impl.open_write(path), drive["size_bytes"], drive["sector"]


def copy(src, dst, sector, progress=None, limit=None):
    """Copy src into dst, and say what went by.

    The digest is of what was read, not of what was written: the last
    write is padded out to a whole sector, because a raw device will not
    take a partial one, and those extra bytes are nobody's data.
    """
    digest = hashlib.sha256()
    done = 0
    while limit is None or done < limit:
        want = CHUNK if limit is None else min(CHUNK, limit - done)
        block = src.read(want)
        if not block:
            break
        digest.update(block)
        done += len(block)
        short = len(block) % sector
        if short:
            block += b"\0" * (sector - short)
        dst.write(block)
        if progress:
            progress(done)
    dst.flush()
    try:
        os.fsync(dst.fileno())
    except (OSError, AttributeError):
        pass                             # not everything can be fsynced
    return digest.hexdigest(), done


def verify(path, length, want, progress=None):
    """Read length bytes back off a drive and check them against a digest.

    Reads whole sectors, because that is all a raw device gives, and hashes
    only as far as the data went.
    """
    digest = hashlib.sha256()
    done = 0
    with impl.open_read(path) as f:
        while done < length:
            block = f.read(CHUNK)
            if not block:
                break
            block = block[:length - done]
            digest.update(block)
            done += len(block)
            if progress:
                progress(done)
    if done != length:
        return False, "only %d of %d bytes could be read back" % (done, length)
    if digest.hexdigest() != want:
        return False, "what came back off the drive is not what went on"
    return True, digest.hexdigest()
