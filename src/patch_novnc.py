#!/usr/bin/env python3

"""Teach noVNC the QEMU Audio pseudo-encoding (-259).

QEMU's VNC server already streams the guest's sound to any client that
asks; noVNC 1.5.0 simply never asks.  This adds the encoding, the two
client messages (set format, enable/disable), the server message parser
(begin / end / data), and two public methods on RFB.  Sound then rides
the same WebSocket as the pixels.

  python3 patch_novnc.py <path-to-novnc>

Idempotent: a tree that is already patched is left alone.  The
replacements are exact strings from noVNC 1.5.0, so a version bump that
changes them fails loudly here instead of quietly shipping no audio.
"""

import io
import os
import sys

MARK = "QEMUAudioSetFormat"

# (anchor, replacement) pairs, applied in order to core/rfb.js
PATCHES = [
    # 1. advertise the encoding
    ("        encs.push(encodings.pseudoEncodingQEMULedEvent);",
     "        encs.push(encodings.pseudoEncodingQEMULedEvent);\n"
     "        encs.push(-259);  // QEMU Audio"),

    # 2. the server acknowledges the encoding with a rect of its own;
    #    without this the rect falls through to the pixel decoder, which
    #    fails the connection the moment sound is available
    ("""            case encodings.pseudoEncodingQEMULedEvent:
                return this._handleLedEvent();""",
     """            case encodings.pseudoEncodingQEMULedEvent:
                return this._handleLedEvent();

            case -259:  // QEMU Audio: an acknowledgement, nothing to draw
                this._qemuAudioSupported = true;
                return true;"""),

    # 3. route server message 255 to the handler
    ("""            case 250:  // XVP
                return this._handleXvpMsg();

            default:""",
     """            case 250:  // XVP
                return this._handleXvpMsg();

            case 255: // QEMU server message
                return this._handleQEMUMsg();

            default:"""),

    # 4. the handler itself, kept next to the XVP one
    ("    _handleXvpMsg() {",
     """    _handleQEMUMsg() {
        if (this._sock.rQwait("QEMU message", 3, 1)) { return false; }
        const subType = this._sock.rQshift8();
        if (subType !== 1) {
            this._fail("Unexpected QEMU server message (sub-type " +
                       subType + ")");
            return true;
        }
        const operation = this._sock.rQshift16();
        switch (operation) {
            case 0: // audio end
                this.dispatchEvent(new CustomEvent("audioend",
                                                   { detail: {} }));
                return true;
            case 1: // audio begin
                this.dispatchEvent(new CustomEvent("audiostart",
                                                   { detail: {} }));
                return true;
            case 2: { // audio data
                if (this._sock.rQwait("audio length", 4, 4)) { return false; }
                const length = this._sock.rQshift32();
                if (this._sock.rQwait("audio data", length, 8)) {
                    return false;
                }
                const data = this._sock.rQshiftBytes(length);
                this.dispatchEvent(new CustomEvent("audiodata",
                                                   { detail: { data } }));
                return true;
            }
            default:
                this._fail("Unexpected QEMU audio operation (" +
                           operation + ")");
                return true;
        }
    }

    _handleXvpMsg() {"""),

    # 5. the public switch, next to the other senders
    ("    _sendEncodings() {",
     """    enableAudio(format, nchannels, frequency) {
        if (this._rfbConnectionState !== 'connected') { return; }
        RFB.messages.QEMUAudioSetFormat(this._sock, format, nchannels,
                                        frequency);
        RFB.messages.QEMUAudioEnable(this._sock, true);
    }

    disableAudio() {
        if (this._rfbConnectionState !== 'connected') { return; }
        RFB.messages.QEMUAudioEnable(this._sock, false);
    }

    _sendEncodings() {"""),

    # 6. the client messages
    ("    pointerEvent(sock, x, y, mask) {",
     """    QEMUAudioSetFormat(sock, format, nchannels, frequency) {
        sock.sQpush8(255); // msg-type
        sock.sQpush8(1);   // sub msg-type: audio
        sock.sQpush16(2);  // operation: set format

        sock.sQpush8(format);
        sock.sQpush8(nchannels);
        sock.sQpush32(frequency);

        sock.flush();
    },

    QEMUAudioEnable(sock, on) {
        sock.sQpush8(255); // msg-type
        sock.sQpush8(1);   // sub msg-type: audio
        sock.sQpush16(on ? 0 : 1);

        sock.flush();
    },

    pointerEvent(sock, x, y, mask) {"""),
]


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    path = os.path.join(argv[1], "core", "rfb.js")
    with io.open(path, encoding="utf-8") as f:
        src = f.read()
    if MARK in src:
        print("novnc: audio patch already in %s" % path)
        return 0
    for anchor, replacement in PATCHES:
        if src.count(anchor) != 1:
            print("novnc: anchor not found once in %s: %r"
                  % (path, anchor.splitlines()[0]))
            return 1
        src = src.replace(anchor, replacement)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(src)
    print("novnc: audio patch applied to %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
