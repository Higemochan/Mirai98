// DOS/V (standard PC, KVM) front-end plugin for the Mirai98 web manager.
//
// Registers the "pcat" machine: a plain PC/AT that QEMU runs under KVM for a
// DOS/V Windows 9x guest.  It shares the "dosv" Storage shelf with box86
// (the same floppies and CDs; a plain raw hard disk of its own).  Everything
// the console needs -- picture, sound over the VNC stream, media swap over
// QMP -- is the core's QEMU path, so there is no console hook here: only the
// create/edit form, the read-only hardware card, and one hard-disk format.

const PCAT_MEMS = ['64M', '128M', '256M', '512M'];
const PCAT_VGAS = [['std', 'std (Bochs VBE) -- SoftGPU'],
                   ['cirrus', 'Cirrus -- stock Win98 driver']];
const PCAT_BOOTS = [['hdd', 'Hard disk'], ['fdd', 'Floppy A'],
                    ['cd', 'CD-ROM']];
const PCAT_NETS = [['off', 'Isolated (no network)'],
                   ['nat', 'NAT (QEMU user, RTL8139)']];

// The fixed parts, named so the create wizard shows what the machine really
// has where the stock "Sound/BIOS" selects would otherwise sit.
const PCAT_BIOS = 'QEMU SeaBIOS (pc-i440fx, ACPI off)';
const PCAT_SOUND = 'Sound Blaster 16 (ISA); Win98 has the driver';
if (typeof JA === 'object') {
  JA[PCAT_BIOS] = 'QEMU SeaBIOS (pc-i440fx, ACPI 無効)';
  JA[PCAT_SOUND] = 'Sound Blaster 16 (ISA); Win98 標準ドライバ';
}

function pcatEditForm(i, h) {
  const note = (t) => ' <span class="note">' + t + '</span>';
  const opts = (list, cur) => list.map(([v, l]) =>
    '<option value="' + v + '"' + ((cur || '') === v ? ' selected' : '') +
    '>' + h.esc(l) + '</option>').join('');
  const mems = PCAT_MEMS.includes(i.memory) ? PCAT_MEMS
    : PCAT_MEMS.concat([i.memory]);
  const memOpts = mems.map(m =>
    '<option' + (i.memory === m ? ' selected' : '') + '>' + m +
    '</option>').join('');
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
    '<div class="row"><label>Floppy B</label>' +
      h.diskSelect('fdd2', 'fdd', i.fdd2, null, 'dosv') + '</div>' +
    '<div class="row"><label>Video</label>' +
      '<select name="vga">' + opts(PCAT_VGAS, i.vga || 'std') + '</select>' +
      note('std (Bochs VBE) for the SoftGPU driver; Cirrus for the stock ' +
           'Win98 display driver') + '</div>' +
    '<div class="row"><label>Boot from</label>' +
      '<select name="boot">' + opts(PCAT_BOOTS, i.boot || 'hdd') +
      '</select></div>' +
    '<div class="row"><label>Network</label>' +
      '<select name="net">' + opts(PCAT_NETS, i.net || 'off') + '</select>' +
      note('isolated by default; NAT gives the guest outbound through QEMU ' +
           'with no host bridge') + '</div>' +
    '<div class="row"><label>Machine type</label>' +
      '<select name="machine">' + h.machineList().map(m =>
        '<option value="' + m + '"' +
        ((i.machine || 'pcat') === m ? ' selected' : '') + '>' +
        h.esc(h.machineLabel ? h.machineLabel(m) : m) +
        '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Memory</label>' +
      '<select name="memory">' + memOpts + '</select></div>' +
    '<div class="row"><label>BIOS</label>' + note(PCAT_BIOS) +
      '<input type="hidden" name="bios" value="real"></div>' +
    '<div class="row"><label>Sound</label>' + note(PCAT_SOUND) +
      '<input type="hidden" name="sound" value="none"></div>' +
    '<div class="row"><label>Execution</label>' +
      '<label class="check"><input type="checkbox" name="kvm"' +
      (i.accel === 'kvm' ? ' checked' : '') +
      '> run on the host CPU (KVM), fall back to translation</label>' +
      note('KVM runs the guest near native; if the host cannot, it falls ' +
           'back to translation on its own') + '</div>' +
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

// The read-only "Hardware configuration" table.  The machine's own board,
// CPU, video and memory come from pcat.py's machine_shown (i.hardware), so
// the accelerator row can show what is really running (KVM or the TCG it
// fell back to) rather than only what was asked for.
function pcatHardware(i, h) {
  const spec = i.hardware || {};
  const boot = (PCAT_BOOTS.find(([v]) => v === (i.boot || 'hdd')) ||
                ['', i.boot || 'hdd'])[1];
  const net = (PCAT_NETS.find(([v]) => v === (i.net || 'off')) ||
               ['', i.net || 'off'])[1];
  const rows = [
    ['&#9881; Machine', h.esc(spec.machine || 'PC/AT (i440FX)')],
    ['&#9636; CPU', h.esc(spec.cpu || 'Pentium III')],
    ['&#9889; Acceleration',
     h.esc(spec.accel || (i.accel === 'kvm' ? 'KVM' : 'TCG'))],
    ['&#9635; Video', h.esc(spec.video || i.vga || 'std')],
    ['&#9737; Memory', h.esc(spec.memory || i.memory || '')],
    ['&#9834; Sound', h.esc(spec.sound || 'Sound Blaster 16')],
    ['&#9707; Hard disk', i.hdd1 ? h.esc(i.hdd1) : '(empty)']];
  if (i.hdd2) rows.push(['&#9707; Hard disk 2', h.esc(i.hdd2)]);
  rows.push(['&#9707; CD-ROM', i.cd ? h.esc(i.cd) +
    ' <span class="note">(read-only)</span>' : '(empty)']);
  rows.push(['&#9707; Floppy A', i.fdd1 ? h.esc(i.fdd1) : '(empty)']);
  if (i.fdd2) rows.push(['&#9707; Floppy B', h.esc(i.fdd2)]);
  rows.push(['&#9654; Boot from', h.esc(boot)]);
  rows.push(['&#8646; Network', h.esc(net)]);
  if (i.ports) rows.push(['&#9635; Display', 'VNC :' + (i.ports[0] - 5900) +
    ', websocket ' + i.ports[1]]);
  if (i.snapshot)
    rows.push(['&#8635; Snapshot', 'changes discarded on shutdown']);
  if (i.extra) rows.push(['&#9656; Extra args', h.esc(i.extra)]);
  return { rows, bios: PCAT_BIOS, sound: PCAT_SOUND };
}

window.registerMachinePlugin({
  machines: ['pcat'],
  // its disks live on the shared dosv shelf, beside box86's
  platform: 'dosv',
  defaults: {
    pcat: { memory: '256M', vga: 'std', boot: 'hdd', net: 'off',
            accel: 'kvm', sound: 'none', bios: 'real',
            lockSound: true, lockBios: true }
  },
  badge: { pcat: 'KVM' },
  labels: { pcat: 'DOS/V PC (KVM, std VGA)' },
  editForm: { pcat: pcatEditForm },
  hardware: { pcat: pcatHardware },
  // Storage "Create": a blank raw hard disk for this machine.  The dosv
  // floppy layouts and the CD come from box86's own registration on the
  // same shelf, so they are not repeated here.
  diskFormats: {
    hdd: [{ value: 'pcat-raw', label: 'PC/AT hard disk (blank .img, raw)',
            note: 'a sparse raw image; attach as the hard disk and let ' +
                  'Win98 FDISK/FORMAT partition it, as on a real machine' }]
  }
});
