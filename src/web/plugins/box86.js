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
// Every field box86.py's CFG_TEMPLATE fixes is fixed for every instance
// today, so the table says exactly that rather than offering a form for
// settings that would not actually go anywhere yet.
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
      ['&#9707; Disk', 'a private copy of a seed image, made the first ' +
       'time this instance starts' + note(
         '(not the shared Storage shelf yet: see disks/dosv/)')],
      ['&#9635; Display', i.ports && i.ports.length
       ? 'VNC :' + (i.ports[0] - 5900) + ', websocket ' + i.ports[1] +
         ', audio websocket ' + i.ports[3]
       : '(not started yet)'],
    ],
  };
}

// ---- edit form ---------------------------------------------------------
// Nothing here is a field box86.py's engine reads except machine and
// name: the preset is fixed for now, so the form does not pretend to let
// any of it be changed.
function box86EditForm(i, h) {
  const machineOpts = h.machineList().map(m =>
    '<option value="' + m + '"' +
    ((i.machine || 'box86') === m ? ' selected' : '') + '>' +
    h.esc(h.machineLabel ? h.machineLabel(m) : m) + '</option>').join('');
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    '<div class="row"><label>Machine type</label>' +
    '<select name="machine">' + machineOpts + '</select></div>' +
    '<div class="row"><label></label><span class="note">tx97, Pentium ' +
    'MMX 200MHz, Voodoo2 -- the one preset this MVP offers; hardware ' +
    'options come later</span></div>' +
    '<input type="hidden" name="memory" value="' + h.esc(i.memory || '64M') +
    '"><input type="hidden" name="sound" value="none">' +
    '<input type="hidden" name="bios" value="real">' +
    '<div class="row"><label></label><button class="primary"' +
    (i.running ? ' disabled title="stop it first"' : '') + '>Save</button>' +
    '<button type="button" onclick="removeVm(\'' + i.name + '\')"' +
    (i.running ? ' disabled' : '') + '>Delete</button></div></form>';
}

window.registerMachinePlugin({
  machines: ['box86'],
  // its own Storage shelf (disks/dosv/); a pc98 or towns image can never
  // be picked for it, nor its own image for them
  platform: 'dosv',
  defaults: {
    box86: { memory: '64M', sound: 'none', bios: 'real',
             lockSound: true, lockBios: true }
  },
  badge: { box86: '86Box' },
  labels: { box86: 'DOS/V PC (86Box, Voodoo2)' },
  editForm: { box86: box86EditForm },
  hardware: { box86: box86Hardware },
  // nothing here has a field to fill in yet: only General (name, machine
  // type) and Confirm stay, the panes every machine keeps
  wizard: { box86: { panes: { Disks: null, Host: null, Memory: null,
                              Network: null, Options: null } } },
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
