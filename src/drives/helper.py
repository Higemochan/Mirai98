"""The elevated half of moving a whole drive.

Started - with administrator rights, through mirai98-helper.exe - when the
server found it was not allowed to open the drive itself.  One job per
process; nothing stays elevated afterwards.

  helper.py <op.json>

The op file says what to do, and where to report:

  {"to_drive": bool, "image": path, "device": path, "confirm": str,
   "allow_internal": bool, "check": bool, "progress": path}

Progress is one JSON object, rewritten atomically as the work moves, so
the unelevated side can simply keep reading it.  Every refusal is the
drives layer's own: running elevated changes what this process may open,
not what it will agree to do.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # src/, for "import drives"
import drives


def report(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def main(argv):
    with open(argv[1], encoding="utf-8") as f:
        op = json.load(f)
    prog = op["progress"]
    state = {"state": "running", "done": 0, "total": 0, "error": "",
             "verified": "", "checking": False}
    last = [0.0]

    def tick(force=False):
        if force or time.time() - last[0] > 0.3:
            last[0] = time.time()
            report(prog, state)

    def progress(done):
        state["done"] = done
        tick()

    try:
        if op["to_drive"]:
            drive = drives.check_write(op["device"], op.get("confirm", ""),
                                       op.get("allow_internal", False))
            state["total"] = os.path.getsize(op["image"])
            if drive["size_bytes"] and state["total"] > drive["size_bytes"]:
                raise drives.Refused(
                    "%s holds %d bytes and the image is %d"
                    % (op["device"], drive["size_bytes"], state["total"]))
            tick(True)
            with open(op["image"], "rb") as src, \
                    drives.impl.open_write(op["device"]) as dst:
                written, done = drives.copy(src, dst, drive["sector"],
                                            progress)
            if op.get("check", True):
                state.update(checking=True, done=0)
                tick(True)
                ok, said = drives.verify(op["device"], done, written,
                                         progress)
                state["checking"] = False
                if not ok:
                    raise OSError(said)
                state["verified"] = said[:16]
        else:
            drive = drives.check_read(op["device"])
            state["total"] = drive["size_bytes"]
            tick(True)
            with drives.impl.open_read(op["device"]) as src, \
                    open(op["image"], "wb") as dst:
                _read, done = drives.copy(src, dst, 1, progress,
                                          limit=state["total"] or None)
        state.update(state="done", done=done)
    except Exception as err:
        state.update(state="failed", error=str(err), checking=False)
    report(prog, state)
    return 0 if state["state"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
