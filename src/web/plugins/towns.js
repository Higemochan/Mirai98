// FM TOWNS front-end plugin for the Mirai98 web manager.
//
// Registers the "towns" machine: Towns create defaults, a list badge, a
// Towns-specific hardware form (only what the emulation actually wires),
// and relative-pointer capture on a TOWNS console.  The PC-98 side is
// untouched: the console hook only engages for machine === "towns".

// ---- Towns hardware form --------------------------------------------------
// The stock form is PC-98 shaped (IDE HDD, PC-98 SCSI, board sound, compat
// BIOS).  For FM TOWNS the machine wires the built-in CD drive, the two
// internal floppy drives, the SCSI hard disks, the built-in sound (FM, PCM
// and CD-DA playback) and the real ROM set, so we show just those.
function townsEditForm(i, h) {
  const note = (t) => ' <span class="note">' + t + '</span>';
  const mems = TOWNS_MEMS.includes(i.memory) ? TOWNS_MEMS
                                              : TOWNS_MEMS.concat([i.memory]);
  const memOpts = mems.map(m =>
    '<option' + (i.memory === m ? ' selected' : '') + '>' + m + '</option>'
  ).join('');
  const machineOpts = h.machineList().map(m =>
    '<option value="' + m + '"' +
    ((i.machine || 'towns') === m ? ' selected' : '') + '>' +
    h.esc(h.machineLabel ? h.machineLabel(m) : m) + '</option>').join('');
  const bootOpts = TOWNS_BOOTS.map(([v, label]) =>
    '<option value="' + v + '"' + ((i.boot || '') === v ? ' selected' : '') +
    '>' + label + '</option>').join('');
  const midiOpts = TOWNS_MIDI.map(([v, label]) =>
    '<option value="' + v + '"' + ((i.midi || '') === v ? ' selected' : '') +
    '>' + label + '</option>').join('');
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', i.cd) +
      note('built-in FM TOWNS CD drive; use a .cue/.bin set (or a .img with ' +
           'a sibling .cue) so the CD-DA audio tracks are kept') + '</div>' +
    '<div class="row"><label>Floppy A</label>' +
      h.diskSelect('fdd1', 'fdd', i.fdd1) +
      note('internal 3-mode drive; raw dump or D77/D88 image') + '</div>' +
    '<div class="row"><label>Floppy B</label>' +
      h.diskSelect('fdd2', 'fdd', i.fdd2) + '</div>' +
    ['scsi1', 'scsi2', 'scsi3', 'scsi4'].map((k, n) =>
      '<div class="row"><label>SCSI HDD ' + n + '</label>' +
      h.diskSelect(k, 'hdd', i[k]) +
      (n === 0 ? note('SCSI ID ' + n + '; a raw image (e.g. a Tsugaru .h0)')
               : note('SCSI ID ' + n)) + '</div>').join('') +
    '<div class="row"><label>MIDI</label>' +
      '<select name="midi">' + midiOpts + '</select>' +
      note('the MT-402/403 card; its music is mixed into the machine sound ' +
           'and reaches you over the console. A few titles will not run ' +
           'with a card fitted') + '</div>' +
    '<div class="row"><label>Boot from</label>' +
      '<select name="boot">' + bootOpts + '</select>' +
      note('the key held at power-on for the system ROM') + '</div>' +
    '<div class="row"><label>CMOS seed</label>' +
      '<select name="cmos">' + townsCmosOpts(i.cmos) + '</select>' +
      '<button type="button" onclick="townsResetCmos(\'' + i.name + '\')"' +
      (i.running ? ' disabled title="stop it first"' : '') +
      '>Reset CMOS</button>' +
      note('the machine keeps its own CMOS (SETUP settings); Reset drops it ' +
           'so the next start begins from the chosen seed') + '</div>' +
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
  if (i.fdd1) rows.push(['&#9707; Floppy A', h.esc(i.fdd1)]);
  if (i.fdd2) rows.push(['&#9707; Floppy B', h.esc(i.fdd2)]);
  ['scsi1', 'scsi2', 'scsi3', 'scsi4'].forEach((k, n) => {
    if (i[k]) rows.push(['&#9707; SCSI HDD ' + n, h.esc(i[k]) +
      ' <span class="note">(SCSI ID ' + n + ')</span>']);
  });
  rows.push(['&#9654; Boot from', townsBootLabel(i.boot)]);
  rows.push(['&#9881; CMOS', 'per-machine towns.cmos, seeded from ' +
    townsCmosLabel(i.cmos)]);
  if (i.snapshot)
    rows.push(['&#8635; Snapshot', 'changes discarded on shutdown']);
  if (i.extra) rows.push(['&#9656; Extra args', h.esc(i.extra)]);
  return { rows, bios: TOWNS_BIOS, sound: TOWNS_SOUND };
}

// ---- Towns create wizard --------------------------------------------------
// What the emulation actually wires, and nothing PC-98 shaped: the disks
// tab lists the built-in CD drive, the two floppy drives and the SCSI
// disks; Host (fat98 share, serial, parallel, GP-IB) and Network (LGY-98)
// have no TOWNS counterpart and are hidden; memory is the MX range; the
// options tab carries the boot device instead of KVM (TCG only).
const TOWNS_MEMS = ['2M', '4M', '6M', '8M', '16M', '32M', '64M'];
const TOWNS_BOOTS = [['', 'ROM order (floppy, CD-ROM, hard disk)'],
                     ['cd', 'CD-ROM'], ['fd', 'floppy drive A'],
                     ['hd', 'SCSI hard disk 0']];
const townsBootLabel = v =>
  (TOWNS_BOOTS.find(([k]) => k === (v || '')) || TOWNS_BOOTS[0])[1];
// the MIDI card, and what plays what it is sent (see plugins/towns.py)
const TOWNS_MIDI = [['', 'No MIDI card'],
                    ['synth', 'MIDI card + SoundFont synthesiser']];
const townsMidiLabel = v =>
  (TOWNS_MIDI.find(([k]) => k === (v || '')) || TOWNS_MIDI[0])[1];
// where a machine's CMOS starts from (see plugins/towns.py CMOS_SEEDS)
const TOWNS_CMOS = [['', 'standard (nothing registered; SETUP registers disks)'],
                    ['real', 'real machine copy (towns.cmos.hdd)']];
const townsCmosLabel = v =>
  (TOWNS_CMOS.find(([k]) => k === (v || '')) || TOWNS_CMOS[0])[1];
const townsCmosOpts = v => TOWNS_CMOS.map(([k, label]) =>
  '<option value="' + k + '"' + ((v || '') === k ? ' selected' : '') + '>' +
  label + '</option>').join('');
window.townsResetCmos = async (name) => {
  if (!confirm('Drop ' + name + '\'s CMOS? The next start seeds it afresh ' +
               '(the Towns OS SETUP settings in it are lost).')) return;
  const r = await api('/api/instances/' + encodeURIComponent(name) + '/x/reset-cmos',
                      {method: 'POST', body: '{}'});
  if (r) toast('CMOS reset; next start seeds from ' + r.seed);
};

function townsWizardDisks(h) {
  return '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', '') +
      h.note('built-in drive; a .cue/.bin set (or a .img with a sibling ' +
             '.cue) keeps the CD-DA tracks') + '</div>' +
    '<div class="row"><label>Floppy A</label>' +
      h.diskSelect('fdd1', 'fdd', '') +
      h.note('internal 3-mode drive; raw dump or D77/D88 image') + '</div>' +
    '<div class="row"><label>Floppy B</label>' +
      h.diskSelect('fdd2', 'fdd', '') + '</div>' +
    ['scsi1', 'scsi2', 'scsi3', 'scsi4'].map((k, n) =>
      '<div class="row"><label>SCSI HDD ' + n + '</label>' +
      h.diskSelect(k, 'hdd', '') +
      (n === 0 ? h.note('SCSI ID 0; a raw image (e.g. a Tsugaru .h0)')
               : h.note('SCSI ID ' + n)) + '</div>').join('') +
    '<div class="note">Images live in Storage. Disks can be changed while ' +
    'the machine runs from its Media row. With no disk the system ROM ' +
    'waits for one (システムをセットしてください).</div>';
}
function townsWizardMemory(h) {
  return '<div class="row"><label>Memory</label><select name="memory">' +
    TOWNS_MEMS.map(m => '<option' + (m === '16M' ? ' selected' : '') + '>' +
                   m + '</option>').join('') + '</select></div>' +
    '<div class="note">FM TOWNS II MX: 4M as shipped, up to 64M. Towns OS ' +
    'and most titles: 4M to 8M; Windows 3.1: 16M or more.</div>';
}
function townsWizardOptions(h) {
  return '<div class="row"><label>MIDI</label><select name="midi">' +
    TOWNS_MIDI.map(([v, label]) => '<option value="' + v + '"' +
      (v === '' ? ' selected' : '') + '>' + label + '</option>').join('') +
    '</select>' +
    h.note('the MT-402/403 card; its music is mixed into the machine sound. ' +
           'A few titles will not run with a card fitted, so it is left out ' +
           'unless asked for') + '</div>' +
    '<div class="row"><label>Boot from</label><select name="boot">' +
    TOWNS_BOOTS.map(([v, label]) => '<option value="' + v + '"' +
      (v === '' ? ' selected' : '') + '>' + label + '</option>').join('') +
    '</select>' + h.note('the key held at power-on for the system ROM') +
    '</div>' +
    '<div class="row"><label>CMOS seed</label><select name="cmos">' +
    townsCmosOpts('') + '</select>' +
    h.note('standard is right for a new machine; the real machine copy ' +
           'only for hard disk images taken from that machine') + '</div>' +
    '<div class="row"><label>Extra QEMU args</label>' +
    '<input type="text" name="extra" style="min-width:24em"></div>' +
    '<div class="note">Appended to the command line as typed. The machine ' +
    'runs under TCG; its CMOS (SETUP settings) is kept per machine.</div>';
}
function townsWizardConfirm(v, h) {
  const rows = [['Name', h.esc(v.name || '(unnamed)')],
                ['Machine type', 'towns (FM TOWNS II MX)'],
                ['BIOS', TOWNS_BIOS],
                ['Memory', h.esc(v.memory)],
                ['Sound', TOWNS_SOUND],
                ['MIDI', townsMidiLabel(v.midi)],
                ['Boot from', townsBootLabel(v.boot)],
                ['CMOS seed', townsCmosLabel(v.cmos)],
                ['Snapshot', v.snapshot ? 'yes' : 'no']];
  if (v.cd) rows.push(['CD-ROM', h.esc(v.cd)]);
  if (v.fdd1) rows.push(['Floppy A', h.esc(v.fdd1)]);
  if (v.fdd2) rows.push(['Floppy B', h.esc(v.fdd2)]);
  ['scsi1', 'scsi2', 'scsi3', 'scsi4'].forEach((k, n) => {
    if (v[k]) rows.push(['SCSI HDD ' + n, h.esc(v[k])]);
  });
  if (v.extra) rows.push(['Extra args', h.esc(v.extra)]);
  if (!v.cd && !v.fdd1 && !v.fdd2 && !v.scsi1 && !v.scsi2 && !v.scsi3 &&
      !v.scsi4) {
    rows.push(['Disks', 'none - the system ROM will wait for a disk']);
  }
  return rows;
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
  // The FM TOWNS mouse has two buttons, so the middle one is free: while
  // the pointer is captured it leaves the capture, for people who would
  // rather not reach for Esc.  It is swallowed either way (it would
  // otherwise start the browser's autoscroll) and never reaches the guest.
  const MIDDLE = 1;
  const onDown = (ev) => {
    if (!locked) {
      if (target.contains(ev.target)) { const c = canvas(); if (c) c.requestPointerLock(); }
      return;
    }
    ev.preventDefault(); ev.stopPropagation();
    if (ev.button === MIDDLE) {
      document.exitPointerLock();
      return;
    }
    mask |= (1 << ev.button); send(0, 0, mask);
  };
  const onUp = (ev) => {
    if (!locked) return;
    ev.preventDefault(); ev.stopPropagation();
    if (ev.button === MIDDLE) {
      return;
    }
    mask &= ~(1 << ev.button); send(0, 0, mask);
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
  hint.textContent = 'クリックでマウス操作を開始（Escまたは中ボタンで解除）';
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
  // the model the machine reports (ID 0x0c/0x02 = TOWNS II MX, i486)
  labels: { towns: 'FM TOWNS II MX (i486)' },
  editForm: { towns: townsEditForm },
  hardware: { towns: townsHardware },
  // Storage "Create": FM TOWNS floppy layouts (empty FAT12, as the Towns
  // FORMAT command lays them out) and a blank SCSI hard disk to be
  // initialised by the Towns OS SETUP / hard-disk utility
  diskFormats: {
    fdd: [{ value: 'towns-1.23', label: '1.23M FM TOWNS 2HD',
            note: 'raw image (.raw), FAT12, empty; the usual Towns floppy' },
          { value: 'towns-1.44', label: '1.44M FM TOWNS 2HD' },
          { value: 'towns-720', label: '720K FM TOWNS 2DD' },
          { value: 'towns-640', label: '640K FM TOWNS 2DD' }],
    hdd: [{ value: 'towns', label: 'FM TOWNS SCSI (blank .h0)',
            note: 'all zeros: attach as SCSI HDD and initialise it ' +
                  '(partitions, format) from the Towns OS system CD\'s ' +
                  'SETUP / hard-disk utility, as on the real machine' }]
  },
  wizard: { towns: { panes: { Disks: townsWizardDisks, Host: null,
                              Memory: townsWizardMemory, Network: null,
                              Options: townsWizardOptions },
                     confirm: townsWizardConfirm } },
  // decided synchronously from the consolePrep result: by the time the
  // console hook runs the machine kind is already known, so the capture
  // is in place before the first pointer event (the old fetch-here-async
  // gating raced the VNC handshake and lost the -257 negotiation)
  consolePrep: prepTownsConsole,
  console: (rfb, target, name) =>
    townsConsoles.has(name) ? captureRelativePointer(rfb, target) : null
});
