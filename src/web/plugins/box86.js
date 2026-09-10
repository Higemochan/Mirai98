// 86Box front-end plugin for the Mirai98 web manager.
//
// Registers the "box86" machine: a single fixed Slot 1/Voodoo2 preset
// (nothing about it is a per-instance field yet -- see box86.py), a list
// badge, a minimal create wizard (only the panes that mean anything for
// this machine), Storage formats for its own dosv shelf, and the one
// thing no other console here needs: a second, audio-only websocket the
// video console has nothing to do with. The relative-pointer mouse
// capture every other console here gets from the core (app.js) is opted
// out of below (relativePointer: false) -- see that flag's own comment
// for why a real X11 console needs the plain, unmodified VNC pointer
// the core would otherwise not send it.

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

// ---- audio: raw s16le stereo over its own websocket, played through
// app.js's own AudioWorklet ring (window.consoleAudioSink). Nothing here
// is box86-specific past the port number, and nothing here decodes --
// the bytes are already PCM at the worklet's own rate.

function startBox86Audio(target, port, onPlayFailed) {
  // Raw s16le stereo at the worklet's own rate, straight into the same
  // AudioWorklet ring a PC-98 console plays through.
  //
  // This used to be Opus in WebM through MediaSource, and that was the
  // wrong shape for what it is for. MediaSource is a buffered-playback
  // API: it is designed to sit behind the live edge, and the encoder
  // feeding it only emitted at WebM cluster boundaries. Measured
  // 2026-09-09 the player alone ran 0.38-0.60s behind, with more delay
  // upstream of it -- fine for listening to something, useless for
  // playing a game, which is what this is actually for.
  //
  // Nothing here decodes: the bytes are PCM already. The worklet owns
  // the jitter buffer (its own prefill, and a lag cap that discards
  // rather than drifts), and app.js's own audioChunk owns the framing,
  // including carrying a stereo frame split across two websocket
  // messages -- dropping those odd bytes would cross the channels for
  // the rest of the connection.
  const sink = window.consoleAudioSink;
  if (!sink) {
    console.error('box86 audio: app.js exposes no worklet sink');
    return () => {};
  }
  let ws = null, streamer = null, stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (streamer) { try { streamer.stop(); } catch (e) {} }
    streamer = null;
    if (ws) { try { ws.close(); } catch (e) {} }
    ws = null;
    try { sink.stop(); } catch (e) {}
  };
  const url = 'ws://' + location.hostname + ':' + port + '/';

  sink.start().then(async () => {
    if (stopped) return;
    // a click got us here, so the context may be resumed straight away
    sink.resume();
    // What this is for: a worker owns the socket and hands PCM to the
    // worklet directly, so noVNC decoding a burst of framebuffer updates
    // on the main thread cannot starve the sound. It returns null where
    // that cannot be built (no Worker, or a policy that forbids one), and
    // then the socket is read here exactly as it always was.
    if (sink.stream) {
      try {
        streamer = await sink.stream(url, (err) => {
          if (stopped) return;
          if (typeof onPlayFailed === 'function') onPlayFailed(err);
        });
      } catch (err) {
        console.warn('box86 audio: no worker path', err);
        streamer = null;
      }
      // stopped while that was being set up
      if (stopped) { if (streamer) { try { streamer.stop(); } catch (e) {} } return; }
      if (streamer) return;
    }
    ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    ws.onmessage = (e) => {
      if (stopped) return;
      sink.feed(new Uint8Array(e.data));
    };
    ws.onerror = () => {
      console.warn('box86 audio: websocket error');
      if (typeof onPlayFailed === 'function') onPlayFailed(new Error('websocket'));
    };
    ws.onclose = () => {
      if (stopped) return;
      if (typeof onPlayFailed === 'function') onPlayFailed(new Error('closed'));
    };
  }).catch(err => {
    console.error('box86 audio: worklet would not start', err);
    if (typeof onPlayFailed === 'function') onPlayFailed(err);
  });

  return stop;
}

// ---- hardware (read-only) --------------------------------------------
// Every instance is still made from the same board/CPU/video preset, but
// the table no longer says so from memory: it reads this instance's own
// cfg, which is where an instance and the preset part company. The disks
// are its own too, box86.py syncs them into that cfg from hdd1/fdd1/
// fdd2/cd (the dosv shelf) the same as any PC-98 or Towns machine's.
// No board name here any more: the Machine row below reads the real one
// out of the instance's own cfg, and two places naming the same board is
// one place to go stale.
const BOX86_BIOS = '86Box’s own BIOS';
const BOX86_SOUND = 'OpenAL/PulseAudio, carried over its own audio websocket';
if (typeof JA === 'object') {
  JA[BOX86_BIOS] = '86Box 自前 BIOS';
  JA[BOX86_SOUND] = 'OpenAL/PulseAudio、専用の音声 websocket 経由';
}
// what box86.py's own _sync_midi wires "synth" to: 86Box's own standalone
// MPU-401 (independent of any sound card, snd_mpu401.c) feeding its own
// built-in FluidSynth MIDI-out device (midi_fluidsynth.c) -- both
// confirmed against 86Box's own source. Not MPU-PC98II: that board does
// not exist here, so this never reuses pc98/towns' own MIDI_LABEL/
// midiName, which only ever say that.
const BOX86_MIDI = [['', 'None'],
                    ['synth', 'MPU-401 + SoundFont']];
const box86MidiLabel = v =>
  (BOX86_MIDI.find(([k]) => k === (v || '')) || BOX86_MIDI[0])[1];
// The detail view's own Media row (General information card, not this
// file's own Hardware configuration one -- window.MiraiPlugins.mediaRow,
// app.js's own drawMedia): 86Box has no QMP of its own for the stock
// QMP-backed picture there to ever get real drives back from, so this
// draws box86's own three (fdd1/fdd2/cd), live while running, the same
// visual shape (a .row/span/picker per drive) that QMP-backed one
// already has for a QEMU machine's own drives. 86Box's own patched
// build (mirai98-box86-patches) polls this instance's own media.ctl for
// a swap box86.py's own swap-media action wrote there -- its own
// equivalent of a QEMU machine's live media change through QMP. Not
// drawn at all stopped (h is only ever built for a running instance by
// drawMedia's own caller): nothing to change live in a machine that is
// not running, and the real edit path (stop it, Edit, Save) is what the
// next start actually reads.
function box86MediaRow(i, h) {
  return [['fdd1', 'fdd'], ['fdd2', 'fdd'], ['cd', 'cdrom']].map(
    ([key, kind]) =>
      '<div class="row" style="margin:.1em 0">' +
        '<span style="width:5.5em">' + h.esc(key) + '</span>' +
        // No ext filter: box86.py's own box86_swap_media puts a
        // floppy's path through the exact same _fdd_compatible_path
        // _sync_disks already does offline, so every extension fdd.c's
        // own loaders[] table knows plays live, not just .img, and
        // .raw itself still works too, via the same symlink the
        // offline path already relies on.
        h.diskPicker(kind, i[key],
          {orphans: true, empty: '(empty)', platform: h.platform,
           filter: h.filters[key] || '',
           box: ' data-device="' + key + '"',
           attrs: 'onchange="box86SwapMedia(\'' + i.name + '\',\'' + key +
                  '\',this.value)"'}) +
      '</div>').join('');
}
window.box86SwapMedia = (name, device, file) => {
  api('/api/instances/' + encodeURIComponent(name) + '/x/swap-media',
      {method: 'POST', body: JSON.stringify({device, name: file})})
    .then(r => {
      if (r) toast(device + ': ' + (r.result || r.error));
      window.redrawMediaRow(name);
    });
};

function box86Hardware(i, h) {
  const note = (t) => ' <span class="note">' + t + '</span>' ;
  // The board, the CPU, the video and the memory are whatever this
  // instance's own 86box.cfg says (box86.py's own _hardware), because
  // that is the file 86Box is started with. Naming the preset here was
  // right until an instance's cfg said something else, which is what
  // happens every time CFG_TEMPLATE changes under an instance that
  // already exists, or its record asks for different memory.
  const spec = i.hardware || {};
  const hw = (key) => spec[key] ? h.esc(spec[key])
    : (spec.problem ? '(unreadable)' : '(unknown)');
  const pending = spec.live === false
    ? note('(not started yet: what its first start will write)') : '';
  // A cfg that will not parse is a thing to say once, next to the board
  // it failed to describe, not four times down the card.
  const trouble = spec.problem
    ? note('(its own 86box.cfg could not be read: ' + h.esc(spec.problem) +
           ')') : '';
  return {
    bios: BOX86_BIOS,
    sound: BOX86_SOUND,
    rows: [
      ['&#9881; Machine', hw('machine') + pending + trouble],
      ['&#9636; CPU', hw('cpu')],
      ['&#9635; Video', hw('video')],
      ['&#9737; Memory', hw('memory')],
      ['&#9834; Sound', BOX86_SOUND],
      ['&#9834; MIDI', box86MidiLabel(i.midi)],
      ['&#9707; Hard disk', i.hdd1 ? h.esc(i.hdd1)
       : 'a private copy of a seed image' + note('(nothing attached ' +
         'from Storage; made the first time this instance starts. No ' +
         'live swap for a hard disk either way: stop it, Edit, Save)')],
      // Floppy A/B and CD-ROM: plain fact here, same as Hard disk just
      // above -- the live picker for these three lives in the General
      // information card's own Media row instead (box86MediaRow,
      // window.MiraiPlugins.mediaRow), not duplicated in this card too.
      ['&#9707; Floppy A', i.fdd1 ? h.esc(i.fdd1) : '(empty)'],
      ['&#9707; Floppy B', i.fdd2 ? h.esc(i.fdd2) : '(empty)'],
      ['&#9707; CD-ROM', i.cd ? h.esc(i.cd) : '(empty)'],
      ['&#9635; Display', i.ports && i.ports.length
       ? 'VNC :' + (i.ports[0] - 5900) + ', websocket ' + i.ports[1] +
         (i.ports.length > 3 ? ', audio websocket ' + i.ports[3] : '')
       : '(not started yet)'],
    ],
  };
}

// ---- edit form ---------------------------------------------------------
// Machine, name and the four disks are all box86.py's engine actually
// reads; everything else about the preset is still fixed, so the form
// does not pretend to let it be changed. This form itself is only ever
// reachable stopped anyway (Edit stays disabled while running, below):
// hdd1 really does still need that, with no live swap of its own for a
// hard disk either way, but Floppy A/B and the CD-ROM no longer do --
// see box86Hardware's own box86MediaRow for the live path those three
// actually take now, while running.
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
    (i.running ? '<div class="note">a running instance keeps the hard ' +
     'disk it started with; Floppy A/B and the CD-ROM can still be ' +
     'swapped live, from its own detail page</div>' : '') +
    '<div class="row"><label>MIDI</label><select name="midi">' +
      BOX86_MIDI.map(([v, label]) => '<option value="' + v + '"' +
        ((i.midi || '') === v ? ' selected' : '') + '>' + label +
        '</option>').join('') + '</select>' +
      note('a standalone MPU-401 at 0x330/IRQ2, synthesised through the ' +
           'same SoundFont as PC-98/towns’ own and mixed into ' +
           '86Box’s own audio') + '</div>' +
    '<div class="row"><label>Machine type</label>' +
    '<select name="machine">' + machineOpts + '</select></div>' +
    '<div class="row"><label></label><span class="note">p2bls, Pentium ' +
    'II 266MHz, Voodoo2 -- the one preset this MVP offers; CPU/video ' +
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
    'Only what starts this instance the first time: once it exists, ' +
    'Floppy A/B and the CD-ROM can be swapped live from its own detail ' +
    'page while it runs -- the hard disk cannot, that one still needs ' +
    'it stopped, in Edit.</div>';
}

// ---- create wizard: MIDI ------------------------------------------------
// Replaces the "Sound" tab's own stock PC-98 content (Sound board *and*
// MIDI board, neither box86's) rather than merely hiding it: the one
// thing under "Sound" that is real for box86 is this, so the tab keeps
// its stock label and gets box86's own single control in its place --
// not a second, independent name="midi" living alongside a full pane of
// its own elsewhere, which is exactly the trap towns.js's own wizard is
// still in (its Options pane's MIDI select and this stock one both
// answer to the same form field; see the box86WizardConfirm commit).
function box86WizardMidi(h) {
  return '<div class="row"><label>MIDI</label><select name="midi">' +
    BOX86_MIDI.map(([v, label]) => '<option value="' + v + '"' +
      (v === '' ? ' selected' : '') + '>' + label + '</option>').join('') +
    '</select>' +
    h.note('a standalone MPU-401 at 0x330/IRQ2, synthesised through the ' +
           'same SoundFont as PC-98/towns\' own and mixed into 86Box\'s ' +
           'own audio -- no card fitted at all otherwise') + '</div>';
}

// ---- create wizard: confirm --------------------------------------------
// The stock confirm rows (drawConfirm, app.js) are PC-98/towns wording
// throughout -- BIOS "compatible", Font "real machine ROM", Display
// "PEGC + GA-98NB", Network "LGY-98" -- none of which box86 has any of;
// falling through to them (no confirm here at all) would just repeat
// the same kind of leak MIDI's own row was giving before this existed.
// Only Name/Machine type/MIDI/the four dosv disks/Snapshot are box86's
// own actual choices.
function box86WizardConfirm(v, h) {
  const rows = [['Name', h.esc(v.name || '(unnamed)')],
                ['Machine type', 'DOS/V PC (86Box, Voodoo2)'],
                ['MIDI', box86MidiLabel(v.midi)]];
  for (const [k, label] of [['hdd1', 'Hard disk'], ['fdd1', 'Floppy A'],
                             ['fdd2', 'Floppy B'], ['cd', 'CD-ROM']])
    if (v[k]) rows.push([label, h.esc(v[k])]);
  rows.push(['Snapshot', v.snapshot ? 'yes' : 'no']);
  return rows;
}

window.registerMachinePlugin({
  machines: ['box86'],
  // its own Storage shelf (disks/dosv/); a pc98 or towns image can never
  // be picked for it, nor its own image for them
  platform: 'dosv',
  // 86Box has no QMP and thus no QEMU VNC server of its own to honour
  // the core's relative-pointer scheme (pseudo-encoding -257) -- see
  // registerMachinePlugin. What travels to x11vnc is a plain absolute
  // VNC pointer, and it lands exactly right: confirmed live, 2026-09-08
  // (a precise hover and click on Windows 95's own Start button).
  //
  // This says nothing about capturing the pointer, which every console
  // now does -- app.js's capturePointer takes the same Pointer Lock for
  // this machine and integrates the locked movementX/Y into an absolute
  // position instead of sending deltas. Reading this flag as "no
  // capture either" is what left a box86 console losing focus at the
  // canvas edge, so its guest cursor could never reach the edges of its
  // own screen (reported by the person using it, 2026-09-09).
  relativePointer: false,
  // Same reasoning as relativePointer just above, a different QEMU-only
  // extension: rfb.enableAudio (app.js) is QEMU's own VNC message type
  // 255, which a plain RFB server (86Box's own x11vnc) has no defined
  // way to safely ignore -- it just closes the connection, which is
  // exactly what tore the console back down again ~1.5s after every
  // connect until this existed (confirmed live, 2026-09-08, Opus/fc).
  // box86 loses nothing opting out: its own audio already rides its own
  // separate websocket (the console hook below), never the VNC channel.
  vncAudio: false,
  // The detail view's own Media row is box86MediaRow's to draw (above):
  // the stock one reads /api/instances/<name>/media, a QMP-backed
  // endpoint that always answers an engine with no QMP at all (86Box)
  // with an empty drive list -- rendered as "no floppy or CD-ROM
  // drive", which box86 very much has (hdd1/fdd1/fdd2/cd) -- a false
  // negative, not a true one.
  mediaRow: { box86: box86MediaRow },
  defaults: {
    box86: { memory: '64M', sound: 'none', bios: 'real',
             lockSound: true, lockBios: true }
  },
  badge: { box86: '86Box' },
  labels: { box86: 'DOS/V PC (86Box, Voodoo2)' },
  editForm: { box86: box86EditForm },
  hardware: { box86: box86Hardware },
  // Storage "Create": standard IBM-compatible FAT12 floppy layouts (box86
  // is a stock PC/AT FDC regardless of which board box86.py's own
  // CFG_TEMPLATE names, not PC-98's or FM TOWNS' own non-standard
  // media) and a blank IDE hard disk to partition and format from the
  // guest OS. .img throughout, not .raw: fdd.c's own loaders[] table
  // (box86.py's FDD_EXTS) never matches .raw at all and silently ejects
  // it instead of erroring -- confirmed live, 2026-09-08 -- so a name
  // this shelf hands back from here has to already be one it knows.
  diskFormats: {
    fdd: [{ value: 'box86-144', label: '1.44M (.img)' },
          { value: 'box86-120', label: '1.2M (.img)' },
          { value: 'box86-720', label: '720K (.img)' },
          { value: 'box86-360', label: '360K (.img)' }],
    hdd: [{ value: 'box86-hdd', label: 'Blank (.img)',
            note: 'all zeros: partition and format it from the guest ' +
                  'OS, as on real hardware' }]
  },
  // Disks is its own pane now; Host/Memory/Network/Options have no
  // field box86.py reads. Sound did not either until _sync_midi: its
  // stock PC-98 pane (Sound board *and* MIDI board, both PC-98-only
  // hardware) was never gated here before, only sound itself was
  // (locked, via defaults.lockSound above), leaving MIDI board's own
  // "MPU-PC98II + SoundFont" fully live and selectable for a machine
  // with no such thing and, at the time, no code anywhere that read
  // what it was set to. box86WizardMidi replaces that content outright
  // now that box86.py's own _sync_midi gives it something real to mean.
  wizard: { box86: { panes: { Disks: box86WizardDisks, Host: null,
                              Memory: null, Sound: box86WizardMidi,
                              Network: null, Options: null },
                     confirm: box86WizardConfirm } },
  consolePrep: prepBox86Console,
  // Sound starts stopped and on a click, not on connect. Two reasons,
  // both real: a browser will not play audio no user gesture asked for
  // (this used to autoplay, have play() rejected, and swallow it), and
  // sound the person did not ask for is not obviously wanted anyway.
  // The button is app.js's own btn-audio -- see window.toggleAudio --
  // so a box86 console has the same one control every other console
  // has, driving a completely different pipeline underneath.
  console: (rfb, target, name) => {
    // The one control 86Box's own windowed menubar needs from here. It
    // is hidden by default (box86.py's own _sync_menubar) so the console
    // is chrome-free like PC-98/towns; this button toggles it live over
    // SIGUSR2 (box86.py's own menubar action) for the rare time 86Box's
    // own menus (Settings/media/reset) are wanted. Injected next to
    // app.js's own btn-audio, removed again when the console is let go.
    const addMenubarButton = () => {
      const audioBtn = document.getElementById('btn-audio');
      if (!audioBtn || !audioBtn.parentNode ||
          document.getElementById('btn-menubar')) return;
      const b = document.createElement('button');
      b.id = 'btn-menubar';
      b.type = 'button';
      b.textContent = '☰ メニュー';
      b.title = '86Box のメニューバーを表示/非表示';
      b.onclick = () => {
        api('/api/instances/' + encodeURIComponent(name) + '/x/menubar',
            {method: 'POST', body: JSON.stringify({})})
          .then(r => { if (r) toast(r.result || r.error || 'menu toggled'); });
      };
      audioBtn.parentNode.insertBefore(b, audioBtn);
    };
    const removeMenubarButton = () => {
      const b = document.getElementById('btn-menubar');
      if (b && b.parentNode) b.parentNode.removeChild(b);
    };
    addMenubarButton();

    const port = box86AudioPort.get(name);
    if (port == null) return () => { removeMenubarButton(); };
    let stop = null;
    const label = on => {
      const btn = document.getElementById('btn-audio');
      if (btn) btn.textContent = on ? '\u{1F50A} Sound on'
                                    : '\u{1F507} Sound off';
    };
    const off = () => {
      if (stop) { try { stop(); } catch (e) {} }
      stop = null;
      label(false);
    };
    window._pluginConsoleAudio = {
      isOn: () => !!stop,
      toggle: async () => {
        if (stop) { off(); toast('sound off'); return; }
        try {
          stop = startBox86Audio(target, port, () => {
            // the browser refused after all: do not leave the button
            // claiming sound is on
            off();
            toast('sound blocked by the browser');
          });
          label(true);
          toast('sound on');
          // A stream that arrives as one tiny fragment and then stops
          // never reaches a range worth playing from, so nothing ever
          // starts and nothing ever complains -- the button just sits
          // there saying sound is on. Seen live, 2026-09-09: a connect
          // that landed on an ffmpeg supervisor restart left a 40ms
          // range at [371.426, 371.466] that never grew, currentTime
          // stuck at 0 for the whole run. If it has not actually begun
          // within a few seconds, say so and go back to off, so the
          // person can simply press it again.
          setTimeout(() => {
            if (!stop) return;                        // already turned off
            if (sink.played && sink.played()) return;  // it began, fine
            off();
            toast('sound did not start -- press it again');
          }, 8000);
        } catch (err) {
          console.error('box86 audio', err);
          off();
          toast('sound failed: ' + err.message);
        }
      }
    };
    label(false);
    return () => { off(); removeMenubarButton(); window._pluginConsoleAudio = null; };
  }
});
