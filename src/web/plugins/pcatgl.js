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
    '<div class="row"><label>Frame limit</label>' +
      '<input type="number" name="fpslimit" min="0" step="1" value="' +
      h.esc(String(fps)) + '">' +
      note('mesagl.cfg FpsLimit, in FPS; 0 = unlimited') + '</div>' +
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
    ['&#127909; Frame limit', h.esc(spec.fpslimit ||
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
    '<div class="row"><label>Frame limit</label>' +
      '<input type="number" name="fpslimit" min="0" step="1" value="60">' +
      h.note('mesagl.cfg FpsLimit, in FPS; 0 = unlimited') + '</div>' +
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
    ['Frame limit', String(fps) === '0' ? 'unlimited' : fps + ' FPS'],
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

window.registerMachinePlugin({
  machines: ['pcat-gl'],
  platform: 'dosv',
  defaults: {
    'pcat-gl': { memory: '256M', vga: 'std', boot: 'hd', net: '',
                 fpslimit: '60', accel: 'kvm', sound: 'none', bios: 'real',
                 acpi: 'on', lockSound: true, lockBios: true }
  },
  badge: { 'pcat-gl': '3dfx' },
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
