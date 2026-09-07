// 86Box front-end plugin for the Mirai98 web manager.
//
// Registers the "box86" machine: a single fixed Socket 7/Voodoo2 preset
// (nothing about it is a per-instance field yet -- see box86.py), a list
// badge, a minimal create wizard (only the panes that mean anything for
// this machine), and the one thing no other console here needs: a second,
// audio-only websocket the video console has nothing to do with. The
// relative-pointer mouse capture every console already gets from the core
// (app.js) needs nothing added here at all.

// ---- which console belongs to a box86 machine, and its audio port --------
// consolePrep runs once, before the RFB video connection is even made
// (mirrors towns.js's own prepTownsConsole); by the time the console hook
// itself runs, this already knows whether there is an audio websocket to
// open at all, and which port.
const box86AudioPort = new Map();

async function prepBox86Console(name) {
  try {
    const r = await fetch('/api/instances/' + encodeURIComponent(name));
    const inst = await r.json();
    if (inst && inst.machine === 'box86' && Array.isArray(inst.ports) &&
        inst.ports.length > 3) {
      box86AudioPort.set(name, inst.ports[3]);
    } else {
      box86AudioPort.delete(name);
    }
  } catch (e) { box86AudioPort.delete(name); }
}

// ---- audio: a raw Opus/WebM stream over its own websocket, played back
// with MediaSource the same way a live stream from any other source would
// be. Nothing here is box86-specific past the port number -- an <audio>
// element, one MediaSource, one SourceBuffer, appended to as bytes arrive.
const BOX86_AUDIO_MIME = 'audio/webm; codecs="opus"';

function startBox86Audio(target, port) {
  const audio = document.createElement('audio');
  audio.autoplay = true;
  audio.style.display = 'none';
  target.appendChild(audio);
  let ws = null, ms = null, stopped = false;
  if (!window.MediaSource || !MediaSource.isTypeSupported(BOX86_AUDIO_MIME)) {
    console.error('box86 audio: this browser cannot decode', BOX86_AUDIO_MIME);
    return () => audio.remove();
  }
  const queue = [];
  let sb = null, jumped = false;
  // ffmpeg's own timestamps are wall-clock ones, not zero-based, so the
  // first bytes this SourceBuffer ever gets already sit far past time 0
  // -- an <audio> element left sitting at currentTime 0 then has nothing
  // buffered there at all (HAVE_METADATA, and it never once advances).
  // The live edge is wherever the buffer's own last range ends; jumping
  // there the first time anything arrives is what every live player does
  // with a stream that carries real timestamps instead of relative ones.
  //
  // The very first range to show up is not that edge, though: the ffmpeg
  // supervisor loop's own restarts (see box86.py) leave tiny fragments
  // behind from whichever WebM init segment arrived most recently, and
  // jumping into one of those the moment it appears (confirmed live,
  // 2026-09-07: end - 0.1 on a [0, 0.061] range clamps to 0 and latches
  // there for good) never advances at all. So this waits for a range
  // actually worth playing from -- half a second of it, at least -- and
  // lands 1.5s short of its end rather than right at it, which is
  // however far ahead a moment of decode/append jitter can eat into
  // before playback would otherwise catch up to nothing yet buffered.
  const jumpToLiveEdge = () => {
    if (jumped || !sb.buffered.length) return;
    const last = sb.buffered.length - 1;
    const start = sb.buffered.start(last), end = sb.buffered.end(last);
    if (end - start < 0.5) return;
    jumped = true;
    audio.currentTime = Math.max(start + 0.05, end - 1.5);
    audio.play().catch(() => {});
  };
  const pump = () => {
    if (stopped || !sb || sb.updating || !queue.length) return;
    try { sb.appendBuffer(queue.shift()); }
    catch (e) { console.error('box86 audio: append failed', e); }
  };
  ms = new MediaSource();
  audio.src = URL.createObjectURL(ms);
  ms.addEventListener('sourceopen', () => {
    if (stopped) return;
    try {
      sb = ms.addSourceBuffer(BOX86_AUDIO_MIME);
    } catch (e) {
      console.error('box86 audio: addSourceBuffer failed', e);
      return;
    }
    sb.addEventListener('updateend', () => { jumpToLiveEdge(); pump(); });
    ws = new WebSocket('ws://' + location.hostname + ':' + port + '/');
    ws.binaryType = 'arraybuffer';
    ws.onmessage = (ev) => { queue.push(new Uint8Array(ev.data)); pump(); };
    // a dropped connection just leaves the machine silent; the console
    // itself (the video side) says plainly enough that something is wrong
    ws.onerror = () => console.error('box86 audio: websocket error');
  }, { once: true });
  return () => {
    stopped = true;
    try { if (ws) ws.close(); } catch (e) {}
    try { if (ms.readyState === 'open') ms.endOfStream(); } catch (e) {}
    try { URL.revokeObjectURL(audio.src); } catch (e) {}
    audio.remove();
  };
}

// ---- hardware (read-only) --------------------------------------------
// The board/CPU/video preset is still fixed for every instance -- that
// part of the table says exactly that -- but the disks are this
// instance's own now, box86.py syncs them into its cfg from hdd1/fdd1/
// fdd2/cd (the dosv shelf) the same as any PC-98 or Towns machine's.
const BOX86_BIOS = 'tx97 (430TX), 86Box’s own BIOS';
const BOX86_SOUND = 'OpenAL/PulseAudio, carried over its own audio websocket';
if (typeof JA === 'object') {
  JA[BOX86_BIOS] = 'tx97 (430TX)、86Box 自前 BIOS';
  JA[BOX86_SOUND] = 'OpenAL/PulseAudio、専用の音声 websocket 経由';
}
function box86Hardware(i, h) {
  const note = (t) => ' <span class="note">' + t + '</span>' ;
  return {
    bios: BOX86_BIOS,
    sound: BOX86_SOUND,
    rows: [
      ['&#9881; Machine', 'tx97 (430TX, Socket 7)'],
      ['&#9636; CPU', 'Pentium MMX (pentium_p55c), 200MHz'],
      ['&#9635; Video', 'S3 ViRGE/DX + 3Dfx Voodoo2 (passthrough)'],
      ['&#9834; Sound', BOX86_SOUND],
      ['&#9707; Hard disk', i.hdd1 ? h.esc(i.hdd1)
       : 'a private copy of a seed image' + note('(nothing attached ' +
         'from Storage; made the first time this instance starts)')],
      ['&#9707; Floppy A', i.fdd1 ? h.esc(i.fdd1) : '(empty)'],
      ['&#9707; Floppy B', i.fdd2 ? h.esc(i.fdd2) : '(empty)'],
      ['&#9707; CD-ROM', i.cd ? h.esc(i.cd) : '(empty)'],
      ['&#9635; Display', i.ports && i.ports.length
       ? 'VNC :' + (i.ports[0] - 5900) + ', websocket ' + i.ports[1] +
         ', audio websocket ' + i.ports[3]
       : '(not started yet)'],
    ],
  };
}

// ---- edit form ---------------------------------------------------------
// Machine, name and the four disks are all box86.py's engine actually
// reads; everything else about the preset is still fixed, so the form
// does not pretend to let it be changed. A running machine keeps the
// disks it started with -- box86 has no QMP media-change of its own,
// so like a rename, a swap needs it stopped first (its cfg is only
// re-synced from these fields at the next start).
function box86EditForm(i, h) {
  const machineOpts = h.machineList().map(m =>
    '<option value="' + m + '"' +
    ((i.machine || 'box86') === m ? ' selected' : '') + '>' +
    h.esc(h.machineLabel ? h.machineLabel(m) : m) + '</option>').join('');
  const note = (t) => ' <span class="note">' + t + '</span>';
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    '<div class="row"><label>Hard disk</label>' +
      h.diskSelect('hdd1', 'hdd', i.hdd1, null, 'dosv') +
      note('empty: a private copy of the seed image is made instead') +
      '</div>' +
    '<div class="row"><label>Floppy A</label>' +
      h.diskSelect('fdd1', 'fdd', i.fdd1, null, 'dosv') + '</div>' +
    '<div class="row"><label>Floppy B</label>' +
      h.diskSelect('fdd2', 'fdd', i.fdd2, null, 'dosv') + '</div>' +
    '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', i.cd, null, 'dosv') + '</div>' +
    (i.running ? '<div class="note">stop it first to change a disk; ' +
     'a running instance keeps the ones it started with</div>' : '') +
    '<div class="row"><label>Machine type</label>' +
    '<select name="machine">' + machineOpts + '</select></div>' +
    '<div class="row"><label></label><span class="note">tx97, Pentium ' +
    'MMX 200MHz, Voodoo2 -- the one preset this MVP offers; CPU/video ' +
    'options come later</span></div>' +
    '<input type="hidden" name="memory" value="' + h.esc(i.memory || '64M') +
    '"><input type="hidden" name="sound" value="none">' +
    '<input type="hidden" name="bios" value="real">' +
    '<div class="row"><label></label><button class="primary"' +
    (i.running ? ' disabled title="stop it first"' : '') + '>Save</button>' +
    '<button type="button" onclick="removeVm(\'' + i.name + '\')"' +
    (i.running ? ' disabled' : '') + '>Delete</button></div></form>';
}

// ---- create wizard: disks ----------------------------------------------
function box86WizardDisks(h) {
  return '<div class="row"><label>Hard disk</label>' +
      h.diskSelect('hdd1', 'hdd', '', null, 'dosv') +
      h.note('empty: a private copy of the seed image is made instead') +
      '</div>' +
    '<div class="row"><label>Floppy A</label>' +
      h.diskSelect('fdd1', 'fdd', '', null, 'dosv') + '</div>' +
    '<div class="row"><label>Floppy B</label>' +
      h.diskSelect('fdd2', 'fdd', '', null, 'dosv') + '</div>' +
    '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', '', null, 'dosv') + '</div>' +
    '<div class="note">Images live in Storage, on their own dosv shelf. ' +
    'box86 has no live media change yet: stop the machine to swap a ' +
    'disk, the next start picks up whatever is attached then.</div>';
}

window.registerMachinePlugin({
  machines: ['box86'],
  // its own Storage shelf (disks/dosv/); a pc98 or towns image can never
  // be picked for it, nor its own image for them
  platform: 'dosv',
  // 86Box has no QMP and thus no QEMU VNC server of its own to honour
  // the core's relative-pointer scheme (pseudo-encoding -257) -- see
  // registerMachinePlugin. A plain absolute VNC pointer already lands
  // exactly right once 86Box's own click-to-capture engages: confirmed
  // live, 2026-09-08 (a precise hover and click on Windows 95's own
  // Start button, captured, using nothing but noVNC's stock behaviour).
  relativePointer: false,
  defaults: {
    box86: { memory: '64M', sound: 'none', bios: 'real',
             lockSound: true, lockBios: true }
  },
  badge: { box86: '86Box' },
  labels: { box86: 'DOS/V PC (86Box, Voodoo2)' },
  editForm: { box86: box86EditForm },
  hardware: { box86: box86Hardware },
  // Disks is its own pane now; Host/Memory/Network/Options still have
  // no field box86.py reads, so only General/Disks/Confirm stay
  wizard: { box86: { panes: { Disks: box86WizardDisks, Host: null,
                              Memory: null, Network: null,
                              Options: null } } },
  consolePrep: prepBox86Console,
  console: (rfb, target, name) => {
    const port = box86AudioPort.get(name);
    if (port == null) return null;
    try {
      return startBox86Audio(target, port);
    } catch (err) {
      console.error('box86 console', err);
      return null;
    }
  }
});
