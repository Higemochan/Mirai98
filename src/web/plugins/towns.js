// FM TOWNS front-end plugin for the Mirai98 web manager.
//
// Registers the "towns" machine: Towns create defaults, a list badge, a
// Towns-specific hardware form (only what the emulation actually wires),
// and relative-pointer capture on a TOWNS console.  The PC-98 side is
// untouched: the console hook only engages for machine === "towns".

// ---- Towns hardware form --------------------------------------------------
// The stock form is PC-98 shaped (IDE HDD, PC-98 SCSI, board sound, compat
// BIOS).  For FM TOWNS the machine wires only the built-in CD drive, the
// built-in sound (FM, PCM and CD-DA playback) and the real ROM set, so we
// show just those.  SCSI HDD is not emulated yet, so it is absent.
function townsEditForm(i, h) {
  const note = (t) => ' <span class="note">' + t + '</span>';
  const memOpts = h.MEMS.map(m =>
    '<option' + (i.memory === m ? ' selected' : '') + '>' + m + '</option>'
  ).join('');
  const machineOpts = h.machineList().map(m =>
    '<option' + ((i.machine || 'towns') === m ? ' selected' : '') + '>' + m +
    '</option>').join('');
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', i.cd) +
      note('built-in FM TOWNS CD drive; use a .cue/.bin set (or a .img with ' +
           'a sibling .cue) so the CD-DA audio tracks are kept') + '</div>' +
    '<div class="row"><label>Machine type</label>' +
      '<select name="machine">' + machineOpts + '</select></div>' +
    '<div class="row"><label>Memory</label>' +
      '<select name="memory">' + memOpts + '</select></div>' +
    '<div class="row"><label>BIOS</label>' + note(TOWNS_BIOS) +
      '<input type="hidden" name="bios" value="real"></div>' +
    '<div class="row"><label>Sound</label>' + note(TOWNS_SOUND) +
      '<input type="hidden" name="sound" value="none"></div>' +
    // unchecked, hidden: the Towns machine runs under TCG (icount needs it)
    '<input type="checkbox" name="kvm" hidden>' +
    '<div class="row"><label>Snapshot</label>' +
      '<label class="check"><input type="checkbox" name="snapshot"' +
      (i.snapshot ? ' checked' : '') + '> discard changes</label></div>' +
    '<div class="row"><label>Extra args</label>' +
      '<input type="text" name="extra" value="' + h.esc(i.extra || '') +
      '"></div>' +
    '<div class="row"><label></label><button class="primary"' +
      (i.running ? ' disabled title="stop it first"' : '') + '>Save</button>' +
      '<button type="button" onclick="removeVm(\'' + i.name + '\')"' +
      (i.running ? ' disabled' : '') + '>Delete</button></div></form>';
}

// ---- Towns hardware view --------------------------------------------------
// The read-only "Hardware configuration" table.  The stock rows describe a
// PC-98 (compat/real BIOS, a sound board, an IDE CD-ROM); the Towns machine
// ignores the bios/sound fields altogether -- it always boots the real ROM
// set with the on-board sound -- so say what really runs.
const TOWNS_BIOS = 'FM TOWNS real ROM set';
const TOWNS_SOUND = 'built-in YM2612 FM + RF5C68 PCM, CD-DA';
if (typeof JA === 'object') {          // the core's phrase table, if present
  JA[TOWNS_BIOS] = 'FM TOWNS 実機ROMセット';
  JA[TOWNS_SOUND] = '内蔵 YM2612 FM + RF5C68 PCM、CD-DA';
}
// (i may be a create-wizard draft without ports; the table then omits Display)
function townsHardware(i, h) {
  const rows = [
    ['&#9636; Memory', h.esc(i.memory || '')],
    ['&#9881; Machine', h.esc(i.machine || 'towns') + ' (TCG)'],
    ['&#9750; BIOS', TOWNS_BIOS],
    ['&#9834; Sound', TOWNS_SOUND]];
  if (i.ports) rows.push(['&#9635; Display', 'VNC :' + (i.ports[0] - 5900) +
     ', websocket ' + i.ports[1]]);
  if (i.cd) rows.push(['&#9707; CD-ROM', h.esc(i.cd) +
    ' <span class="note">(built-in FM TOWNS drive, read-only; a .cue ' +
    'beside the image carries the audio tracks)</span>']);
  if (i.snapshot)
    rows.push(['&#8635; Snapshot', 'changes discarded on shutdown']);
  if (i.extra) rows.push(['&#9656; Extra args', h.esc(i.extra)]);
  return { rows, bios: TOWNS_BIOS, sound: TOWNS_SOUND };
}

// ---- relative-pointer capture (only for a Towns console) ------------------
// QEMU's VNC turns each absolute pointer event into a delta from the last
// one, which stalls at the canvas edge; instead we ask for QEMU's relative
// branch (pseudo-encoding -257) and, while the pointer is locked, feed each
// host movement as a delta around 0x7FFF.  Returns a cleanup function.
// Whether the NEXT console connection belongs to a TOWNS machine, decided
// in the consolePrep hook BEFORE the RFB object exists: the -257 request
// must ride in the initial client-encodings message of the handshake, and
// only for TOWNS consoles (a PC-98 console keeps its stock pointer path).
const townsConsoles = new Set();
let relativeNext = false;

async function ensureRelPatch() {
  const RFB = (await import('/novnc/core/rfb.js')).default;
  if (RFB.messages._townsRelPatched) return;
  const orig = RFB.messages.clientEncodings;
  RFB.messages.clientEncodings = function (sock, encodings) {
    if (relativeNext && !encodings.includes(-257)) {
      encodings = encodings.concat([-257]);
    }
    return orig.call(this, sock, encodings);
  };
  const origHandleRect = RFB.prototype._handleRect;
  RFB.prototype._handleRect = function () {
    if (this._FBU.encoding === -257) return true;   // pointer-type-change: no data
    return origHandleRect.call(this);
  };
  RFB.messages._townsRelPatched = true;
}

async function prepTownsConsole(name) {
  relativeNext = false;
  try {
    const r = await fetch('/api/instances/' + encodeURIComponent(name));
    const inst = await r.json();
    if (inst && inst.machine === 'towns') {
      townsConsoles.add(name);
      relativeNext = true;
    } else {
      townsConsoles.delete(name);
    }
  } catch (e) { townsConsoles.delete(name); }
  await ensureRelPatch();
}

function captureRelativePointer(rfb, target) {
  const RFB = rfb.constructor;
  const CENTER = 0x7FFF;
  rfb._sendMouse = function () {};        // silence noVNC's absolute sends
  // FM TOWNS draws its own (software) cursor, so noVNC sees no server cursor
  // and hides the local one (canvas cursor:none) -- which left the pointer
  // invisible after Esc.  Ask noVNC to always show a dot cursor instead.
  rfb.showDotCursor = true;
  let locked = false, mask = 0, accX = 0, accY = 0, flush = false;
  const canvas = () => target.querySelector('canvas');
  const send = (dx, dy, m) => {
    if (!rfb || rfb._rfbConnectionState !== 'connected') return;
    RFB.messages.pointerEvent(rfb._sock,
      (CENTER + dx) & 0xffff, (CENTER + dy) & 0xffff, m);
  };
  const onDown = (ev) => {
    if (!locked) {
      if (target.contains(ev.target)) { const c = canvas(); if (c) c.requestPointerLock(); }
      return;
    }
    mask |= (1 << ev.button); send(0, 0, mask); ev.preventDefault(); ev.stopPropagation();
  };
  const onUp = (ev) => {
    if (!locked) return;
    mask &= ~(1 << ev.button); send(0, 0, mask); ev.preventDefault(); ev.stopPropagation();
  };
  const onMove = (ev) => {
    if (!locked) return;
    accX += ev.movementX; accY += ev.movementY; ev.preventDefault(); ev.stopPropagation();
    if (!flush) {
      flush = true;
      requestAnimationFrame(() => {
        flush = false;
        if (accX || accY) { send(accX, accY, mask); accX = 0; accY = 0; }
      });
    }
  };
  const hint = document.createElement('div');
  hint.textContent = 'クリックでマウス操作を開始（Escで解除）';
  hint.style.cssText = 'position:absolute;left:50%;bottom:8px;' +
    'transform:translateX(-50%);background:rgba(0,0,0,.7);color:#fff;' +
    'font:12px sans-serif;padding:4px 10px;border-radius:4px;' +
    'pointer-events:none;z-index:5';
  if (getComputedStyle(target).position === 'static') target.style.position = 'relative';
  target.appendChild(hint);
  const onLock = () => {
    locked = document.pointerLockElement === canvas();
    hint.style.display = locked ? 'none' : '';
    if (!locked) {
      mask = 0;
      // Pointer Lock hid the cursor and noVNC leaves the canvas at
      // cursor:none; bring a visible host cursor back after Esc.
      const c = canvas();
      if (c) c.style.cursor = 'default';
    }
  };
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('mouseup', onUp, true);
  document.addEventListener('mousemove', onMove, true);
  document.addEventListener('pointerlockchange', onLock);
  return () => {
    document.removeEventListener('mousedown', onDown, true);
    document.removeEventListener('mouseup', onUp, true);
    document.removeEventListener('mousemove', onMove, true);
    document.removeEventListener('pointerlockchange', onLock);
    if (document.pointerLockElement) document.exitPointerLock();
    hint.remove();
  };
}

window.registerMachinePlugin({
  machines: ['towns'],
  defaults: {
    towns: { memory: '16M', sound: 'none', bios: 'real',
             lockSound: true, lockBios: true }
  },
  badge: { towns: 'TOWNS' },
  editForm: { towns: townsEditForm },
  hardware: { towns: townsHardware },
  // decided synchronously from the consolePrep result: by the time the
  // console hook runs the machine kind is already known, so the capture
  // is in place before the first pointer event (the old fetch-here-async
  // gating raced the VNC handshake and lost the -257 negotiation)
  consolePrep: prepTownsConsole,
  console: (rfb, target, name) =>
    townsConsoles.has(name) ? captureRelativePointer(rfb, target) : null
});
