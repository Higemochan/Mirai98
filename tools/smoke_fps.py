#!/usr/bin/env python3
"""What pcatgl_fps_input actually puts on the wire.

The fiddly part is turning a button MASK from the browser into the press
and release transitions QEMU wants: get it wrong and a button is left held
down in the guest, which in a game means the trigger stays pulled.  QMP is
stubbed, so this asserts on the events, not on a machine.

Usage: smoke_fps.py <plugins/pcatgl.py>
"""
import importlib.util, os, sys

PLUGIN = os.path.abspath(sys.argv[1])
spec = importlib.util.spec_from_file_location("pcatgl_fps_test", PLUGIN)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

sent = []
m._qmp_command = lambda port, cmd, args=None, timeout=1: (
    sent.append((cmd, args)) or '{"return":{}}')

class Api:
    os = os
    def ports_of(self, inst):
        return (5996, 5906, 5896, 4796)

api, inst = Api(), {"index": 96, "name": "T", "machine": "pcat-gl"}
bad = []

def check(label, ok, detail=""):
    print("  %-52s %s%s" % (label, "PASS" if ok else "FAIL",
                            "" if ok else "  <- " + detail))
    if not ok:
        bad.append(label)

def call(dx, dy, buttons):
    del sent[:]
    out = m.pcatgl_fps_input(api, inst, {"dx": dx, "dy": dy, "buttons": buttons})
    evs = sent[0][1]["events"] if sent else []
    return out, evs

def rels(evs):
    return [(e["data"]["axis"], e["data"]["value"])
            for e in evs if e["type"] == "rel"]

def btns(evs):
    return [(e["data"]["button"], e["data"]["down"])
            for e in evs if e["type"] == "btn"]

print("movement")
out, evs = call(7, -3, 0)
check("dx/dy become rel x and rel y", rels(evs) == [("x", 7), ("y", -3)], str(rels(evs)))
check("the command is input-send-event", sent and sent[0][0] == "input-send-event")
out, evs = call(5, 0, 0)
check("a zero axis is not sent at all", rels(evs) == [("x", 5)], str(rels(evs)))
out, evs = call(0, 0, 0)
check("an empty flush sends nothing", not sent and out.get("result") == "nothing to send",
      repr(out))

print("buttons")
out, evs = call(0, 0, 1)
check("mask 1 -> left down", btns(evs) == [("left", True)], str(btns(evs)))
out, evs = call(3, 3, 1)
check("mask unchanged -> no button event repeated", btns(evs) == [], str(btns(evs)))
out, evs = call(0, 0, 0)
check("back to 0 -> left up", btns(evs) == [("left", False)], str(btns(evs)))
out, evs = call(0, 0, 4)
check("mask 4 -> right down", btns(evs) == [("right", True)], str(btns(evs)))
out, evs = call(0, 0, 5)
check("4 -> 5 adds left, keeps right", btns(evs) == [("left", True)], str(btns(evs)))
out, evs = call(0, 0, 0)
check("5 -> 0 releases both", sorted(btns(evs)) == [("left", False), ("right", False)],
      str(btns(evs)))
check("nothing is left recorded as held", 96 not in m._fps_buttons,
      repr(m._fps_buttons))

print("a flush carries movement and the transition together")
call(0, 0, 0)
out, evs = call(9, 9, 1)
check("one command, not two", len(sent) == 1, "%d commands" % len(sent))
check("rel and btn in the same event list",
      rels(evs) == [("x", 9), ("y", 9)] and btns(evs) == [("left", True)],
      "%s %s" % (rels(evs), btns(evs)))
call(0, 0, 0)

print("refusals")
out, _ = call(999999, 0, 0)
check("an impossible delta is refused, not sent",
      isinstance(out, tuple) and out[0] == 400, repr(out))
del sent[:]
out = m.pcatgl_fps_input(api, inst, {"dx": "left a bit", "dy": 0, "buttons": 0})
check("a non-number is refused", isinstance(out, tuple) and out[0] == 400, repr(out))
check("nothing reached the machine on a refusal", not sent, str(sent))

print("a machine that is not answering")
def boom(*a, **k):
    raise OSError("connection refused")
m._qmp_command = boom
out = m.pcatgl_fps_input(api, inst, {"dx": 1, "dy": 1, "buttons": 0})
check("becomes 503, not an exception",
      isinstance(out, tuple) and out[0] == 503, repr(out))

print()
print("FAILED: %s" % "; ".join(bad) if bad else "ALL PASS")
sys.exit(1 if bad else 0)
