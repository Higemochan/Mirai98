// Front-end for the pcat-gl machine (DOS/V PC + 3dfx, GL output).  It is
// the plain "pcat" machine's twin: the same DOS/V body and the same dosv
// disk shelf, run through the GL engine instead of QEMU's own -vnc.  An
// existing pcat instance is turned into one (and back) by switching just
// its "Machine type" field -- the disk and everything else stay -- so the
// machine picker here is restricted to that pair, never a cross-platform
// machine whose shelf and disks would not match.

const PCATGL_MEMS = ['64M', '128M', '256M', '512M'];
const PCATGL_VGAS = [['std', 'std (Bochs VBE) -- SoftGPU / 3dfx wrapper'],
                     ['cirrus', 'Cirrus Logic']];
const PCATGL_BOOTS = [['hd', 'Hard disk'], ['fd', 'Floppy A'],
                      ['cd', 'CD-ROM']];
const PCATGL_NETS = [['', 'Isolated (no network)'], ['nat', 'NAT (outbound)']];
// pcat <-> pcat-gl only: same DOS/V body and disk, one carrying 3dfx GL.
// Other machines (pc98, towns, box86) draw their disks from a different
// shelf, so switching to one would leave this record's disk behind.
const PCATGL_SWITCHABLE = ['pcat', 'pcat-gl'];
const PCATGL_BIOS = 'SeaBIOS (no PnP when the shelf has bios-nopnp.bin)';
const PCATGL_SOUND = 'Sound Blaster 16';

function pcatglMachineSelect(i, h) {
  const cur = i.machine || 'pcat-gl';
  const list = h.machineList().filter(m => PCATGL_SWITCHABLE.includes(m));
  // keep the current machine present even if its plugin has not loaded
  if (!list.includes(cur)) list.unshift(cur);
  return '<select name="machine">' + list.map(m =>
    '<option value="' + m + '"' + (cur === m ? ' selected' : '') + '>' +
    h.esc(h.machineLabel ? h.machineLabel(m) : m) +
    '</option>').join('') + '</select>';
}

function pcatglEditForm(i, h) {
  const note = (t) => ' <span class="note">' + t + '</span>';
  const opts = (list, cur) => list.map(([v, l]) =>
    '<option value="' + v + '"' + ((cur || '') === v ? ' selected' : '') +
    '>' + h.esc(l) + '</option>').join('');
  const mems = PCATGL_MEMS.includes(i.memory) ? PCATGL_MEMS
    : PCATGL_MEMS.concat([i.memory]);
  const memOpts = mems.map(m =>
    '<option' + (i.memory === m ? ' selected' : '') + '>' + m +
    '</option>').join('');
  const fps = (i.fpslimit === '' || i.fpslimit == null) ? '60' : i.fpslimit;
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    '<div class="row"><label>Hard disk</label>' +
      h.diskSelect('hdd1', 'hdd', i.hdd1, null, 'dosv') +
      note('IDE primary master; a plain .img (raw) or .qcow2') + '</div>' +
    '<div class="row"><label>Hard disk 2</label>' +
      h.diskSelect('hdd2', 'hdd', i.hdd2, null, 'dosv') + '</div>' +
    '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', i.cd, null, 'dosv') +
      note('IDE secondary master; can be swapped while running') + '</div>' +
    '<div class="row"><label>Floppy A</label>' +
      h.diskSelect('fdd1', 'fdd', i.fdd1, null, 'dosv') + '</div>' +
    '<div class="row"><label>Video</label>' +
      '<select name="vga">' + opts(PCATGL_VGAS, i.vga || 'std') + '</select>' +
      note('std for the SoftGPU / 3dfx wrapper') + '</div>' +
    '<div class="row"><label>Frame rate (FPS)</label>' +
      '<input type="number" name="fpslimit" min="20" max="75" step="1" value="' +
      h.esc(String(fps)) + '">' +
      note('console frame rate (20-75, default 60): caps the guest (mesagl ' +
           'FpsLimit) and the x11vnc capture/send') + '</div>' +
    '<div class="row"><label>Boot from</label>' +
      '<select name="boot">' + opts(PCATGL_BOOTS, i.boot || 'hd') +
      '</select></div>' +
    '<div class="row"><label>Network</label>' +
      '<select name="net">' + opts(PCATGL_NETS, i.net || '') + '</select>' +
      note('isolated by default; NAT gives outbound through QEMU') + '</div>' +
    '<div class="row"><label>Machine type</label>' +
      pcatglMachineSelect(i, h) +
      note('switch to plain pcat to run the same disk without 3dfx GL; ' +
           'stop the machine first') + '</div>' +
    '<div class="row"><label>Memory</label>' +
      '<select name="memory">' + memOpts + '</select></div>' +
    '<div class="row"><label>ACPI</label>' + note('always on (the 3dfx ' +
      'guest wrapper needs the PIIX4 PM timer)') +
      '<input type="hidden" name="acpi" value="on"></div>' +
    '<div class="row"><label>Execution</label>' + note('always KVM ' +
      '(3dfx on translation is pointless)') +
      '<input type="hidden" name="kvm" value="on"></div>' +
    '<div class="row"><label>BIOS</label>' + note(PCATGL_BIOS) +
      '<input type="hidden" name="bios" value="real"></div>' +
    '<div class="row"><label>Sound</label>' + note(PCATGL_SOUND) +
      '<input type="hidden" name="sound" value="none"></div>' +
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

// The read-only hardware table.  Board/CPU/video/memory/acpi/fpslimit and
// the live display state (GL, or the plain-VGA fallback with its reason)
// come from pcatgl.py's machine_shown (i.hardware).
function pcatglHardware(i, h) {
  const spec = i.hardware || {};
  const boot = (PCATGL_BOOTS.find(([v]) => v === (i.boot || 'hd')) ||
                ['', i.boot || 'hd'])[1];
  const net = (PCATGL_NETS.find(([v]) => v === (i.net || '')) ||
               ['', i.net || 'Isolated'])[1];
  const rows = [
    ['&#9881; Machine', h.esc(spec.machine || 'DOS/V PC + 3dfx (GL)')],
    ['&#9636; CPU', h.esc(spec.cpu || 'Pentium III')],
    ['&#9889; Acceleration', h.esc(spec.accel || 'KVM')],
    ['&#9203; ACPI', h.esc(spec.acpi || 'ACPI enabled')],
    ['&#9635; Video', h.esc(spec.video || i.vga || 'std')],
    ['&#127909; Frame rate', h.esc(spec.fpslimit ||
      ((i.fpslimit && String(i.fpslimit) !== '0') ? i.fpslimit + ' FPS'
        : (String(i.fpslimit) === '0' ? 'unlimited' : '60 FPS')))],
    ['&#128421; Display', h.esc(spec.display ||
      '3dfx GL via weston + Xwayland + x11vnc')],
    // shown only while running in GL mode: the wrapper writes the marker
    // when the guest first makes a GL context, so "not yet" before any 3D
    ...(spec.cfg ? [['&#9881; mesagl.cfg', h.esc(spec.cfg)]] : []),
    ['&#9737; Memory', h.esc(spec.memory || i.memory || '')],
    ['&#9834; Sound', h.esc(spec.sound || 'Sound Blaster 16')],
    ['&#9750; BIOS', h.esc(spec.bios || PCATGL_BIOS)],
    ['&#9707; Hard disk', i.hdd1 ? h.esc(i.hdd1) : '(empty)']];
  if (i.hdd2) rows.push(['&#9707; Hard disk 2', h.esc(i.hdd2)]);
  rows.push(['&#9707; CD-ROM', i.cd ? h.esc(i.cd) +
    ' <span class="note">(read-only)</span>' : '(empty)']);
  rows.push(['&#9707; Floppy A', i.fdd1 ? h.esc(i.fdd1) : '(empty)']);
  rows.push(['&#9654; Boot from', h.esc(boot)]);
  rows.push(['&#8646; Network', h.esc(net)]);
  if (i.ports) rows.push(['&#9635; Console', 'VNC :' + (i.ports[0] - 5900) +
    ', websocket ' + i.ports[1]]);
  if (i.snapshot)
    rows.push(['&#8635; Snapshot', 'changes discarded on shutdown']);
  if (i.extra) rows.push(['&#9656; Extra args', h.esc(i.extra)]);
  return { rows, bios: spec.bios || PCATGL_BIOS, sound: PCATGL_SOUND };
}

// ---- create wizard --------------------------------------------------------
function pcatglWizardDisks(h) {
  return '<div class="row"><label>Hard disk</label>' +
      h.diskSelect('hdd1', 'hdd', '', null, 'dosv') +
      h.note('a Win98 disk with the qemu-3dfx guest wrapper installed') +
      '</div>' +
    '<div class="row"><label>CD-ROM</label>' +
      h.diskSelect('cd', 'cdrom', '', null, 'dosv') + '</div>' +
    '<div class="row"><label>Floppy A</label>' +
      h.diskSelect('fdd1', 'fdd', '', null, 'dosv') + '</div>';
}

function pcatglWizardMemory(h) {
  return '<div class="row"><label>Memory</label><select name="memory">' +
    PCATGL_MEMS.map(m => '<option' + (m === '256M' ? ' selected' : '') + '>' +
                  m + '</option>').join('') + '</select></div>' +
    '<div class="note">256M is comfortable for Win98.</div>';
}

function pcatglWizardOptions(h) {
  const opts = (list, cur) => list.map(([v, l]) =>
    '<option value="' + v + '"' + ((cur || '') === v ? ' selected' : '') +
    '>' + h.esc(l) + '</option>').join('');
  return '<div class="row"><label>Video</label>' +
      '<select name="vga">' + opts(PCATGL_VGAS, 'std') + '</select>' +
      h.note('std for the SoftGPU / 3dfx wrapper') + '</div>' +
    '<div class="row"><label>Frame rate (FPS)</label>' +
      '<input type="number" name="fpslimit" min="20" max="75" step="1" value="60">' +
      h.note('console frame rate (20-75, default 60): guest cap + x11vnc capture') + '</div>' +
    '<div class="row"><label>Boot from</label>' +
      '<select name="boot">' + opts(PCATGL_BOOTS, 'hd') + '</select></div>' +
    '<div class="row"><label>Network</label>' +
      '<select name="net">' + opts(PCATGL_NETS, '') + '</select>' +
      h.note('isolated by default; NAT gives outbound through QEMU') +
      '</div>' +
    '<div class="row"><label>Snapshot</label>' +
      '<label class="check"><input type="checkbox" name="snapshot"> ' +
      'discard changes</label></div>';
}

function pcatglWizardConfirm(v, h) {
  const pick = (list, cur, dflt) =>
    (list.find(([x]) => x === (cur || dflt)) || ['', cur || dflt])[1];
  const fps = (v.fpslimit === '' || v.fpslimit == null) ? '60' : v.fpslimit;
  const rows = [
    ['Name', h.esc(v.name || '(unnamed)')],
    ['Machine type', 'DOS/V PC + 3dfx (GL)'],
    ['Video', pick(PCATGL_VGAS, v.vga, 'std')],
    ['Frame rate', (fps || '60') + ' FPS'],
    ['Acceleration', 'KVM'],
    ['ACPI', 'Enabled'],
    ['Boot from', pick(PCATGL_BOOTS, v.boot, 'hd')],
    ['Network', pick(PCATGL_NETS, v.net, '') || 'Isolated']];
  for (const [k, label] of [['hdd1', 'Hard disk'], ['fdd1', 'Floppy A'],
                            ['cd', 'CD-ROM']])
    if (v[k]) rows.push([label, h.esc(v[k])]);
  rows.push(['Snapshot', v.snapshot ? 'yes' : 'no']);
  return rows;
}

// --- console audio (box86's PCM-over-websocket path) -----------------------
// The GL console is x11vnc (no audio) and QEMU's VNC audio is off, so sound
// rides its own websocket -- ports_of[3], the same shape box86 uses -- into
// the shared AudioWorklet sink.  consolePrep learns the port before the RFB
// video connects; the console hook drives the one btn-audio toggle.
const pcatglAudioPort = new Map();

async function prepPcatglConsole(name) {
  try {
    const r = await fetch('/api/instances/' + encodeURIComponent(name));
    const inst = await r.json();
    if (inst && inst.machine === 'pcat-gl' && Array.isArray(inst.ports) &&
        inst.ports.length > 3) {
      pcatglAudioPort.set(name, inst.ports[3]);
    } else {
      pcatglAudioPort.delete(name);
    }
  } catch (e) { pcatglAudioPort.delete(name); }
}

function startPcatglAudio(target, port, onPlayFailed) {
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
    console.error('pcatgl audio: app.js exposes no worklet sink');
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
        console.warn('pcatgl audio: no worker path', err);
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
      console.warn('pcatgl audio: websocket error');
      if (typeof onPlayFailed === 'function') onPlayFailed(new Error('websocket'));
    };
    ws.onclose = () => {
      if (stopped) return;
      if (typeof onPlayFailed === 'function') onPlayFailed(new Error('closed'));
    };
  }).catch(err => {
    console.error('pcatgl audio: worklet would not start', err);
    if (typeof onPlayFailed === 'function') onPlayFailed(err);
  });

  return stop;
}


// The detail console's sound control (app.js's own btn-audio, via
// window._pluginConsoleAudio -- the same button every console has, driving
// this pipeline).  No 86Box menubar here: this is QEMU.
function pcatglConsole(rfb, target, name) {
  // --- FPS mouselook ---------------------------------------------------
  // A game that looks around by reading the cursor, taking its distance
  // from the centre of the screen and warping it back there -- every
  // Quake-era engine, SiN among them -- gets nothing back from that warp
  // through this console.  The captured pointer's deltas are integrated
  // into an absolute position (app.js capturePointer), because RFB can
  // carry nothing else, x11vnc warps the X pointer there, and it reaches
  // the guest as a usb-tablet coordinate.  The guest's own warp moves no
  // tablet, so the next read is the same large distance again and the
  // view pins to an edge.
  //
  // While this is on the deltas go to pcatgl.py's fps-input action
  // instead, which hands them to the guest's PS/2 mouse over QMP as
  // relative motion -- x11vnc, X and SDL all out of the input path.  Off
  // by default: the absolute pointer is what the desktop wants, and it is
  // what makes a click land where it was aimed.
  let fpsOn = false, inFlight = false, qx = 0, qy = 0, qMask = 0, qDirty = false;
  const fpsPost = (dx, dy, m) =>
    window.apiQuiet('/api/instances/' + encodeURIComponent(name) +
                    '/x/fps-input',
                    {method: 'POST',
                     body: JSON.stringify({dx: dx, dy: dy, buttons: m})});
  // One request in flight at a time, with whatever arrives meanwhile
  // riding on the next.  A frame's movement is a sum, so coalescing loses
  // nothing; queuing a request per frame against a machine that has
  // stopped answering would lose the pointer instead.
  const fpsDrain = () => {
    if (inFlight || (!qx && !qy && !qDirty)) return;
    inFlight = true;
    const dx = qx, dy = qy, m = qMask;
    qx = 0; qy = 0; qDirty = false;
    fpsPost(dx, dy, m).then((got) => {
      inFlight = false;
      if (got && !got.ok) {
        // say it once and hand the pointer back, rather than every frame
        if (fpsOn) { fpsSet(false); toast('FPSマウス: ' + got.error); }
        return;
      }
      fpsDrain();
    }).catch(() => { inFlight = false; });
  };
  const fpsRelay = (dx, dy, m) => {
    qx += dx; qy += dy;
    if (m !== qMask) { qMask = m; qDirty = true; }
    fpsDrain();
  };
  const fpsLabel = () => {
    const b = document.getElementById('btn-fps');
    if (!b) return;
    b.textContent = fpsOn ? '\u{1F3AF} FPSマウス ON' : '\u{1F3AF} FPSマウス';
    b.style.fontWeight = fpsOn ? 'bold' : '';
  };
  const fpsSet = (on) => {
    fpsOn = on;
    window.MiraiConsole.pointerRelay = on ? fpsRelay : null;
    if (!on) {
      // let go of whatever the guest is still holding down
      qx = 0; qy = 0; qMask = 0; qDirty = true;
      fpsDrain();
    }
    fpsLabel();
  };
  const addFpsButton = () => {
    const anchor = document.getElementById('btn-audio');
    if (!anchor || !anchor.parentNode || document.getElementById('btn-fps'))
      return;
    const b = document.createElement('button');
    b.id = 'btn-fps';
    b.type = 'button';
    b.title = 'FPS 用の相対マウス。視点が端に張り付くときに入れる' +
              '（デスクトップ操作では切っておく）';
    b.onclick = () => {
      fpsSet(!fpsOn);
      toast(fpsOn ? 'FPSマウス ON' : 'FPSマウス OFF');
    };
    anchor.parentNode.insertBefore(b, anchor);
    fpsLabel();
  };
  addFpsButton();
  const fpsCleanup = () => {
    fpsSet(false);
    const b = document.getElementById('btn-fps');
    if (b && b.parentNode) b.parentNode.removeChild(b);
  };

  const port = pcatglAudioPort.get(name);
  if (port == null) return fpsCleanup;
  let stop = null;
  const label = (on) => {
    const btn = document.getElementById('btn-audio');
    if (btn) btn.textContent = on ? '\u{1F50A} Sound on' : '\u{1F507} Sound off';
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
        stop = startPcatglAudio(target, port, () => {
          off();
          toast('sound blocked by the browser');
        });
        label(true);
        toast('sound on');
        setTimeout(() => {
          if (!stop) return;
          const sink = window.consoleAudioSink;
          if (sink && sink.played && sink.played()) return;
          off();
          toast('sound did not start -- press it again');
        }, 8000);
      } catch (err) {
        console.error('pcatgl audio', err);
        off();
        toast('sound failed: ' + err.message);
      }
    }
  };
  label(false);
  return () => { fpsCleanup(); off(); window._pluginConsoleAudio = null; };
}

window.registerMachinePlugin({
  machines: ['pcat-gl'],
  platform: 'dosv',
  // absolute RFB pointer, not QEMU's relative pseudo-encoding (-257):
  // the GL console is x11vnc, which does not speak -257, and the guest's
  // absolute HID (usb-tablet) wants absolute coordinates.  Same opt-out
  // box86 makes for the same x11vnc reason.  (Default without this is
  // relative -- relativePointer[machine] !== false in app.js.)
  relativePointer: false,
  // no QEMU VNC audio extension (rfb.enableAudio, message type 255):
  // the GL console is x11vnc, a plain RFB server with no defined way to
  // ignore type 255 -- it closes the connection instead, tearing the
  // console down ~1.5s after every connect (app.js sends it on the
  // 'connect' event when wantsVncAudio; box86 hit exactly this,
  // 2026-09-08).  Opt out as box86 does.  The GL path carries no audio
  // yet regardless (x11vnc does not) -- a separate task.
  vncAudio: false,
  consolePrep: prepPcatglConsole,
  console: pcatglConsole,
  defaults: {
    'pcat-gl': { memory: '256M', vga: 'std', boot: 'hd', net: '',
                 fpslimit: '60', accel: 'kvm', sound: 'none', bios: 'real',
                 acpi: 'on', lockSound: true, lockBios: true }
  },
  badge: { 'pcat-gl': 'DOS/V' },
  labels: { 'pcat-gl': 'DOS/V PC + 3dfx (GL)' },
  editForm: { 'pcat-gl': pcatglEditForm },
  hardware: { 'pcat-gl': pcatglHardware },
  // no disk formats of its own: the blank raw disk and the dosv floppy
  // layouts come from plain pcat's and box86's registrations on the same
  // shelf.
  wizard: { 'pcat-gl': { panes: { Disks: pcatglWizardDisks, Host: null,
                                  Memory: pcatglWizardMemory, Sound: null,
                                  Network: null, Options: pcatglWizardOptions },
                         confirm: pcatglWizardConfirm } }
});
