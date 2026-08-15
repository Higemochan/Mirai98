// loaded on demand: without noVNC beside the program the consoles are
// dead, but everything else still works
let RFB = null;
const loadRFB = async () => RFB ||
  (RFB = (await import('/novnc/core/rfb.js')).default);

// --- machine plugins -------------------------------------------------------
// Extra machine types (FM TOWNS, ...) register here; the core reads the
// registry at render time, so a plugin loaded after the first paint still
// shows up on the next screen the app draws.  A plugin adds machine ids for
// the create form, per-machine create defaults, a list badge, and an optional
// console hook (returning a cleanup function).
window.MiraiPlugins = window.MiraiPlugins ||
  { machines: [], defaults: {}, badge: {}, console: [], editForm: {} };
window.registerMachinePlugin = (p) => {
  const P = window.MiraiPlugins;
  P.consolePrep = P.consolePrep || [];
  (p.machines || []).forEach(m => { if (!P.machines.includes(m)) P.machines.push(m); });
  Object.assign(P.defaults, p.defaults || {});
  Object.assign(P.badge, p.badge || {});
  Object.assign(P.editForm, p.editForm || {});   // per-machine hardware form
  if (p.console) P.console.push(p.console);
  if (p.consolePrep) P.consolePrep.push(p.consolePrep);
  lastList = '';   // a new machine/badge must invalidate the cached VM list
};
// the machine ids offered in the create form: core PC-98 plus any plugin
const machineList = () => ['pc9821', 'pc9801'].concat(window.MiraiPlugins.machines);
// a machine's cell in the list, with a plugin badge if it registered one
const machineCell = (m) => {
  m = m || 'pc9821';
  const b = window.MiraiPlugins.badge[m];
  return esc(m) + (b ? ' <span style="display:inline-block;padding:0 .4em;' +
    'border-radius:.3em;background:#7a3cff;color:#fff;font-size:.8em;' +
    'vertical-align:middle">' + esc(b) + '</span>' : '');
};
// apply a plugin machine's create defaults when the type changes (a core
// PC-98 machine just re-enables the board and BIOS choices)
window.applyMachineDefaults = (sel) => {
  const form = sel.form;
  const d = window.MiraiPlugins.defaults[sel.value];
  if (form.sound) form.sound.disabled = !!(d && d.lockSound);
  if (form.bios) form.bios.disabled = !!(d && d.lockBios);
  if (!d) return;
  if (d.memory && form.memory) form.memory.value = d.memory;
  if (d.sound && form.sound) form.sound.value = d.sound;
  if (d.bios && form.bios) form.bios.value = d.bios;
};
// pull in the front-end plugins the server advertises, then redraw
(async () => {
  let list = [];
  try { list = await (await fetch('/api/plugins')).json(); } catch (e) { return; }
  for (const f of list) {
    // cache-bust: plugin scripts are tiny and must never be served stale
    try { await import('/plugins/' + f + '?v=' + Date.now()); }
    catch (e) { console.error('plugin', f, e); }
  }
  // repaint once plugins are registered; lastList is cleared so the cached
  // (badge-less) list is invalidated, and the async render's rejection is
  // handled rather than escaping the old synchronous try/catch
  if (list.length) { lastList = ''; render().catch(() => {}); }
})();

const MEMS = ["640K","2M","4M","8M","16M","32M","64M","128M","256M","512M",
              "1G","2G","4G","8G","16G","32G"];
// the boards are named after the chips, so they keep their capitals
// the boards are named after the machines they came in
const SOUND_LABEL = {"86": "PC-9801-86", "wss": "WSS", "none": "None"};
const SOUND_ALIAS = {"opna+wss": "86", "opna": "86"};
const soundKey = v => SOUND_ALIAS[v] || (SOUND_LABEL[v] ? v : '86');
const soundName = v => SOUND_LABEL[soundKey(v)];
const DISK_ROWS = [["hdd1","IDE HDD 1","hdd"], ["hdd2","IDE HDD 2","hdd"],
                   ["cd","IDE CD-ROM","cdrom"], ["fdd1","FDD 1","fdd"],
                   ["fdd2","FDD 2","fdd"], ["scsi1","SCSI 1","hdd"],
                   ["scsi2","SCSI 2","hdd"], ["scsi3","SCSI 3","hdd"],
                   ["scsi4","SCSI 4","hdd"]];
const TABS = ["General","Disks","Host","Memory","Sound","Network",
              "Options","Confirm"];
const view = document.getElementById('view');
let rfb = null, consoleWatch = null, catalog = {hdd:[], fdd:[], cdrom:[]};
let instances = [], hostFacts = {}, tab = 0;
let hardware = {drives: [], serial: []}, autoConnect = '', facts = {};

window.toast = t => {
  document.getElementById('msg').textContent = t || '';
  if (t) setTimeout(() => {
    if (document.getElementById('msg').textContent === t) toast('');
  }, 5000);
};
// ------------------------------------------------------- the language
// The page is written in English and translated on the way out: every
// screen is built by the same string concatenation it always was, and a
// MutationObserver swaps whole labels for their Japanese counterparts as
// they land in the document.  A phrase with no entry stays English.
const JA = {
  'Host Server': 'ホストサーバー', 'Storage': 'ストレージ',
  'This Computer': 'このコンピュータ',
  'Networking': 'ネットワーク', 'Logging': 'ログ',
  'System Settings': 'システム設定', 'Shell': 'シェル',
  'Navigator': 'ナビゲーター', 'Virtual machines': '仮想マシン',
  'All machines': 'すべての仮想マシン', 'Create VM': '仮想マシンの作成',
  'Host': 'ホスト', 'Tasks': 'タスク', 'connected': '接続中',
  'running': '実行中', 'stopped': '停止', 'Refresh': '更新',
  'Console': 'コンソール', 'Power on': '起動', 'Shut down': 'シャットダウン',
  'Suspend': 'サスペンド', 'Restart': 'リセット', 'Edit': '編集',
  'Delete': '削除', 'Save': '保存', 'Cancel': '中止', 'Close': '閉じる',
  'Back': '戻る', 'Next': '次へ', 'Finish': '完了', 'Set': '設定',
  'Create': '作成', 'Upload...': 'アップロード...', 'Download': 'ダウンロード',
  'Resume': '再開', 'Connect': '接続', 'Disconnect': '切断',
  'Configuration': '構成', 'Resource consumption': 'リソース使用状況',
  'Performance': 'パフォーマンス', 'General information': '基本情報',
  'Hardware configuration': 'ハードウェア構成', 'Media': 'メディア',
  'CPU(s)': 'CPU', 'Kernel version': 'カーネル',
  'Operating system': 'オペレーティングシステム', 'Boot mode': 'ブートモード',
  'Hardware virtualisation': 'ハードウェア仮想化', 'QEMU': 'QEMU',
  'PC-98 BIOS': 'PC-98 BIOS', 'Storage root': 'ストレージのルート',
  'Python': 'Python', 'Build': 'ビルド', 'Addresses': 'アドレス',
  'Memory': 'メモリ', 'Machine type': 'マシン型', 'BIOS': 'BIOS',
  'Acceleration': 'アクセラレーション', 'Disks': 'ディスク',
  'Sound': 'サウンド', 'Display': '画面', 'Network': 'ネットワーク',
  'Networking': 'ネットワーク', 'Shared folder': '共有フォルダ',
  'Serial port': 'シリアルポート', 'Parallel port': 'パラレルポート',
  'GP-IB': 'GP-IB', 'Home': '保存場所', 'none': 'なし', 'None': 'なし',
  'compatible': '互換ROM', 'of one guest CPU': 'ゲストCPU 1個あたり',
  'Images': 'イメージ', 'Add an image': 'イメージの追加',
  'Create a disk': 'ディスクの作成', 'Create a floppy': 'フロッピーの作成',
  'Upload from this computer...': 'このPCからアップロード...',
  'Download from URL...': 'URLからダウンロード...',
  'Import a server path...': 'サーバー上のパスから取り込み...',
  'Read a host drive...': '実機ドライブから読み取り...',
  'Write to a drive...': '実機ドライブへ書き込み...',
  'Contents': '中身', 'Copy': '複製', 'NAME': '名前', 'SIZE': 'サイズ',
  'MODIFIED': '更新日時', 'USED BY': '使用中の仮想マシン',
  'DEVICE': 'デバイス', 'TYPE': '種別', 'LABEL': 'ラベル', 'STATE': '状態',
  'NOTE': '備考', 'MODEL': 'モデル', 'DRIVE': 'ドライブ', 'FILE': 'ファイル',
  'Device': 'デバイス', 'Type': '種別', 'Label': 'ラベル', 'Size': 'サイズ',
  'State': '状態', 'Note': '備考', 'Model': 'モデル', 'Drive': 'ドライブ',
  'File': 'ファイル', 'Name': '名前', 'Filesystems': 'ファイルシステム',
  'Drives': 'ドライブ', 'Data storage': 'データの保存先',
  'In use': '使用中', 'Settings': '設定ファイル',
  'Use for data': 'データ用に使う', 'in use for data': 'データ用に使用中',
  'Password': 'パスワード', 'Current': '現在のパスワード',
  'New password': '新しいパスワード',
  'Remove the password': 'パスワードを解除',
  'Persistent System Image': '永続システムイメージ',
  'Image': 'イメージ', 'This boot': 'この起動',
  'Create the image': 'イメージを作成', 'Remove the image': 'イメージを削除',
  'Update the system': 'システムを更新', 'Update': '更新',
  'Install to a Disk': 'ディスクへインストール',
  'Install here': 'ここへインストール', 'free': '空き',
  'System log': 'システムログ', 'all': 'すべて', 'system': 'システム',
  'vm': '仮想マシン', 'disk': 'ディスク', 'network': 'ネットワーク',
  'web': 'ウェブ', 'lines': '行', 'Interfaces': 'インタフェース',
  'Address': 'アドレス', 'Mode': 'モード', 'Gateway': 'ゲートウェイ',
  'Name servers': 'ネームサーバー', 'LAN bridge': 'LANブリッジ',
  'Save and apply': '保存して適用', 'Open in a tab': '別タブで開く',
  'the hypervisor host, not a guest': 'ゲストではなくホスト側です',
  'the hypervisor’s own address': 'このホスト自身のアドレス',
  'General': '全般', 'Options': 'オプション', 'Confirm': '確認',
  'the machine is not running': '仮想マシンが動いていません',
};
// long notes, keyed by the sentence they replace
JA['Machines live at the root of the chosen filesystem. Only the ' +
   'choice is kept on the boot medium. ext4 and exFAT: no limits. ' +
   'NTFS: needs a full Windows shutdown. FAT32: no file over 4 GB.'] =
  '選んだ場所に仮想マシンを置きます。ブートメディアには選択だけを保存' +
  'します。ext4とexFATは制限なし。NTFSはWindowsを完全に終了してから。' +
  'FAT32は4GBを超えるファイルを置けません。';
JA['One disk, one system. The disk is wiped and laid out like the ' +
   'stick: a system partition and the rest as data. BIOS and UEFI are ' +
   'both installed.'] =
  'ディスク全体をこのシステムだけで使います。USBメディアと同じ構成' +
  '（システム用と残り全部のデータ用）に作り直し、BIOSとUEFIの両方を' +
  'インストールします。中身はすべて消えます。';
JA['Keeps everything apt installs. The boot menu always has a second ' +
   'entry that ignores it. A new kernel is copied onto the boot medium ' +
   'afterwards.'] =
  'aptで入れたものが再起動後も残ります。ブートメニューには常にこれを' +
  '無視する項目もあります。更新後は新しいカーネルをブートメディアへ' +
  'コピーします。';
JA['Cleared at boot. Trimmed to 1000 lines at 1 MB.'] =
  '起動時に消去し、1MBを超えたら末尾1000行に切り詰めます。';
JA['Not set. Anyone on this network can drive this host.'] =
  '未設定です。このネットワークの誰でもこのホストを操作できます。';
JA['Manage disks in Storage.'] = 'ディスクはストレージ画面で管理します。';
JA['Set. The web console, ssh and the shell all ask for it.'] =
  '設定済みです。管理画面・ssh・シェルのすべてで聞かれます。';
JA['Set. This console asks for it.'] = '設定済みです。この管理画面で聞かれます。';
JA['This is root\'s own password, so the shell asks for it too. A live ' +
   'system forgets it at every boot, so its hash is kept on the boot ' +
   'medium and put back at start-up. Changing it from a shell leaves ' +
   'this page out of step.'] =
  'root 自身のパスワードなので、シェルでも同じものを聞かれます。ライブ' +
  '起動では毎回忘れられるため、ハッシュをブートメディアに保存して起動時' +
  'に復元します。シェルから変更するとこの画面とずれます。';
JA['none: system changes stay in RAM'] =
  'なし: システムへの変更はRAMに残るだけです';
JA['RAM overlay'] = 'RAMオーバーレイ';
JA['using the image'] = 'イメージを使用中';
JA['KVM available'] = 'KVM 利用可能';
JA['not available (TCG only)'] = '利用不可（TCGのみ）';
JA['compatible ROMs'] = '互換ROM';
JA['real ROM set'] = '実機ROM';
JA['the boot medium'] = 'ブートメディア';
JA['where this program was started'] = 'このプログラムを起動した場所';

JA['Modified'] = '更新日時';
JA['Used by'] = '使用中';
JA['(empty)'] = '(なし)';
JA['to hdi'] = 'hdiへ';
JA['to qcow2'] = 'qcow2へ';
JA['to raw'] = 'rawへ';
JA['to fdi'] = 'fdiへ';
JA['.qcow2 grows on demand. .hdi: Anex86. .raw: flat.'] =
  '.qcow2 は必要に応じて大きくなります。.hdi は Anex86 形式、.raw はベタ。';
JA['.fdi or .raw. Formatted, empty.'] =
  '.fdi または .raw。フォーマット済みの空ディスクです。';
JA['not uploaded, the compatible ROM stands in'] =
  '未アップロード（互換ROMで代用）';
JA['A machine set to the real BIOS uses these and falls back to the ' +
   'compatible ROMs. N88 BASIC needs pc98basic.bin. No compatible ' +
   'version yet.'] =
  '実機BIOSに設定した仮想マシンがこれらを使い、足りない分は互換ROMで' +
  '補います。N88 BASIC には pc98basic.bin が必要です（互換版はまだ' +
  'ありません）。';

JA['powered off'] = '停止中';
JA['🔇 Sound off'] = '🔇 サウンドOFF';
JA['🔊 Sound on'] = '🔊 サウンドON';
JA['not running'] = '停止中';
JA['Machine'] = 'マシン';
JA['MEMORY'] = 'メモリ';
JA['PROCESS'] = 'プロセス';
JA['Create Virtual Machine'] = '仮想マシンの作成';
JA['Machine type and name'] = 'マシン型と名前';
JA['Discard disk changes when the machine stops'] =
  '停止時にディスクへの変更を捨てる';
JA['Sound board'] = 'サウンドボード';
JA['PC-9801-86: FM and PCM. WSS: the Mate-X built-in. The console ' +
   'plays whichever one is fitted.'] =
  'PC-9801-86 は FM と PCM。WSS は Mate-X 内蔵音源。装着した方の音を' +
  'コンソールで再生します。';
JA['Real machine BIOS'] = '実機BIOS';
JA['Compatible (ships with Mirai98)'] = '互換ROM（Mirai98 同梱）';
JA['Real ROM set (upload in Storage)'] = '実機ROM（ストレージでアップロード）';

// phrases built around a value: matched whole, rewritten with the value
const JA_RE = [
  [/^(.+) free of (.+)$/, '$2 中 $1 空き'],
  [/^(\S+) \(KVM \(Experimental\)\)$/, '$1（KVM・実験的）'],
  [/^Real machine ROMs — (.+)$/, '実機ROM — $1'],
  [/^uptime (.+)$/, '稼働時間 $1'],
  [/^Load average (.+)$/, '平均負荷 $1'],
  [/^Virtual machines: (\d+) running of (\d+)$/, '仮想マシン $2 台中 $1 台が実行中'],
  [/^(\d+)% of (\d+) CPUs$/, '$1% / $2 CPU'],
  [/^host RSS (.+)$/, 'ホストRSS $1'],
  [/^(\d+) lines$/, '$1 行'],
  [/^(\d+) disks?$/, 'ディスク $1 台'],
  [/^\((\d+), (\d+) on\)$/, '($1 台、$2 台実行中)'],
  [/^in use: (.+)$/, '使用中: $1'],
  [/^Move back to (.+)$/, '$1 に戻す'],
  [/^VNC :(\d+), websocket (\d+)$/, 'VNC :$1、WebSocket $2'],
  [/^KVM \(Experimental\)$/, 'KVM（実験的）'],
  [/^the boot medium, grown by (.+)$/, 'ブートメディア（$1 拡張済み）'],
  [/^checking (.+)$/, '照合中 $1'],
  [/^(.+): done, read back and checked$/, '$1: 完了（読み戻し照合済み）'],
  [/^(.+): done$/, '$1: 完了'],
  [/^(.+): downloaded$/, '$1: ダウンロード完了'],
];
let lang = '';
function translateNode(node) {
  if (lang !== 'ja') return;
  if (node.nodeType === 3) {
    const key = node.nodeValue.trim();
    if (!key) return;
    if (JA[key]) { node.nodeValue = node.nodeValue.replace(key, JA[key]);
                   return; }
    // a label behind a glyph: "› System Settings", "▣ Host Server"
    const tail = key.match(/^([^A-Za-z0-9]+)\s*(.+)$/);
    if (tail && JA[tail[2]]) {
      node.nodeValue = node.nodeValue.replace(tail[2], JA[tail[2]]);
      return;
    }
    for (const [pattern, into] of JA_RE)
      if (pattern.test(key)) {
        node.nodeValue = node.nodeValue.replace(key, key.replace(pattern,
                                                                into));
        return;
      }
    return;
  }
  if (node.nodeType !== 1) return;
  if (node.tagName === 'CANVAS' || node.tagName === 'IFRAME') return;
  for (const attr of ['placeholder', 'title']) {
    const value = node.getAttribute && node.getAttribute(attr);
    if (value && JA[value.trim()]) node.setAttribute(attr, JA[value.trim()]);
  }
  for (const child of node.childNodes) translateNode(child);
}
new MutationObserver(records => {
  if (lang !== 'ja') return;
  for (const record of records)
    for (const node of record.addedNodes) translateNode(node);
}).observe(document.body, {childList: true, subtree: true});

window.setLang = value => {
  try { localStorage.setItem('mirai98-lang', value); } catch (err) {}
  fetch('/api/lang', {method: 'POST', body: JSON.stringify({lang: value})})
    .finally(() => location.reload());
};

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// a Windows path is full of backslashes, which a JS string literal eats
const jsq = s => String(s ?? '').replace(/\\/g, '\\\\');
const fmtBytes = n => {
  if (n == null) return '-';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + u[i];
};

// ------------------------------------------------------------ task log
const tasks = [];
function task(what, status) {
  const d = new Date();
  tasks.unshift({t: d.toLocaleTimeString(), what, status});
  tasks.splice(30);
  document.getElementById('tasks').innerHTML =
    '<tr><th style="width:8em">Time</th><th>Description</th>' +
    '<th style="width:14em">Status</th></tr>' +
    tasks.map(x => '<tr><td>' + x.t + '</td><td>' + esc(x.what) +
      '</td><td style="color:' +
      (/fail|error|refus|exist|use by/i.test(x.status) ? '#e06c5f'
                                                       : '#9ede7a') +
      '">' + esc(x.status) + '</td></tr>').join('');
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { toast(data.error || r.statusText); return null; }
  return data;
}
window.act = async (name, verb) => {
  toast(verb + ' ' + name + '...');
  const data = await api('/api/instances/' + name + '/' + verb,
                         {method: 'POST'});
  if (data) { toast(name + ': ' + data.result);
              task('VM ' + name + ' - ' + verb, data.result); }
  // starting a machine and then hunting for its screen is two clicks
  // too many: go there and connect
  if (data && ['start', 'resume'].includes(verb) &&
      /^(started|resumed)/.test(data.result)) {
    autoConnect = name;
    if (location.hash !== '#/vm/' + name) { location.hash = '#/vm/' + name;
                                            return; }
  }
  render();
};
window.removeVm = async name => {
  if (!confirm('delete ' + name + '?')) return;
  await window.act(name, 'delete');
  location.hash = '#/';
};

// ---------------------------------------------------------------- tree
function drawTree() {
  const here = location.hash || '#/';
  const link = (href, text, cls) =>
    '<a href="' + href + '" class="' + (href === here ? 'on ' : '') +
    (cls || '') + '">' + text + '</a>';
  const running = instances.filter(i => i.running).length;
  document.getElementById('btn-shell').style.display =
    facts.platform === 'windows' ? 'none' : '';
  const win = facts.platform === 'windows';
  document.getElementById('tree').innerHTML =
    '<div class="navhead">Navigator</div>' +
    '<div class="group">Host Server</div>' +
    // on Windows the disks are what people came for, so they go first;
    // on the appliance the host itself is the thing being administered
    (win ? link('#/storage', '&#9707; Storage') +
           link('#/', '&#9635; This Computer')
         : link('#/', '&#9635; Host Server') +
           link('#/storage', '&#9707; Storage') +
           link('#/network', '&#8646; Networking')) +
    link('#/log', '&#9776; Logging') +
    link('#/settings', '&#9881; System Settings') +
    (win ? '' : link('#/shell', '&#9002;_ Shell')) +
    '<div class="group">Virtual machines <span class="count">(' +
    instances.length + ', ' + running + ' on)</span></div>' +
    link('#/vms', '&#9776; All machines') +
    instances.map(i => link('#/vm/' + i.name,
        '<span class="dot"></span>' + esc(i.name),
        i.running ? 'run' : '')).join('');
}

// VMware puts the same row of verbs above every object it shows
function actionBar(buttons) {
  return '<div class="actions">' + buttons.filter(Boolean).join('') +
         '</div>';
}
function meterBar(label, used, total, extra) {
  const pct = total ? Math.min(100, used / total * 100) : 0;
  return '<div class="meterlab"><span>' + label + '</span><span>' +
    pct.toFixed(0) + '% &nbsp; ' + (extra || (fmtBytes(used) + ' of ' +
    fmtBytes(total))) + '</span></div>' +
    '<div class="track"><div style="width:' + pct.toFixed(1) +
    '%"></div></div>';
}

// -------------------------------------------------------- host graphs
const HISTORY = 120, history = {cpu: [], mem: [], at: []};

function plot(points, colour, scale) {
  if (points.length < 2) return '<svg viewBox="0 0 100 100"></svg>';
  const step = 100 / (HISTORY - 1);
  const y = v => (100 - Math.max(0, Math.min(100, v / scale * 100)))
                 .toFixed(2);
  const line = points.map((v, i) => (i * step).toFixed(2) + ',' + y(v))
                     .join(' ');
  const last = ((points.length - 1) * step).toFixed(2);
  return '<svg viewBox="0 0 100 100" preserveAspectRatio="none">' +
    [25, 50, 75].map(g => '<line x1="0" y1="' + g + '" x2="100" y2="' + g +
      '" stroke="#2a2f34" stroke-width="1" vector-effect="non-scaling-stroke"/>'
      ).join('') +
    '<polygon points="0,100 ' + line + ' ' + last + ',100" fill="' +
    colour + '33"/>' +
    '<polyline points="' + line + '" fill="none" stroke="' + colour +
    '" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>';
}

function graph(title, now, points, colour, scale, ticks) {
  const times = history.at;
  const stamp = i => times[i] ? times[i] : '';
  const xs = times.length > 1
    ? [stamp(0), stamp(Math.floor(times.length / 2)), stamp(times.length - 1)]
    : ['', '', ''];
  return '<div class="graph"><div class="cap">' + title + '<b>' + now +
    '</b></div><div class="plotrow"><div class="ylab">' +
    ticks.map(t => '<span>' + t + '</span>').join('') +
    '</div><div class="plot">' + plot(points, colour, scale) +
    '</div></div><div class="xlab">' +
    xs.map(t => '<span>' + t + '</span>').join('') + '</div></div>';
}

function hostCard() {
  const f = hostFacts;
  const memGB = (f.mem_total || 1) / 1073741824;
  const up = f.uptime == null ? '' :
    Math.floor(f.uptime / 86400) + 'd ' +
    Math.floor(f.uptime % 86400 / 3600) + 'h ' +
    Math.floor(f.uptime % 3600 / 60) + 'm';
  return '<div class="card"><h3>Node</h3>' +
    '<div class="body"><div class="graphs">' +
    graph('% CPU', (history.cpu.at(-1) ?? 0).toFixed(0) + '% of ' +
          (f.cores || '?') + ' cores', history.cpu, 'var(--cpu)', 100,
          ['100','50','0']) +
    graph('GiB Memory', fmtBytes(f.mem_used || 0) + ' / ' +
          fmtBytes(f.mem_total || 0), history.mem, 'var(--mem)',
          f.mem_total || 1,
          [memGB.toFixed(1), (memGB / 2).toFixed(1), '0']) +
    '</div><div class="note" style="margin-top:.8em">' +
    (f.disk_total ? 'storage ' + fmtBytes(f.disk_free) + ' free of ' +
     fmtBytes(f.disk_total) + ' &nbsp; ' : '') +
    (up ? 'uptime ' + up : '') + '</div></div></div>';
}

async function pollHost() {
  const h = await api('/api/host');
  if (!h) return;
  hostFacts = h;
  if (h.cpu != null) {
    history.cpu.push(h.cpu);
    history.mem.push(h.mem_used || 0);
    history.at.push(new Date().toTimeString().slice(0, 5));
    for (const k of ['cpu', 'mem', 'at'])
      if (history[k].length > HISTORY) history[k].shift();
  }
  const slot = document.getElementById('host-card');
  if (slot) slot.innerHTML = hostCard();
}

// ------------------------------------------------------------ list view
function mediaSummary(i) {
  const parts = [];
  for (const [k] of DISK_ROWS)
    if (i[k]) parts.push(k + '=' + i[k]);
  if (i.net === 'nat') parts.push('lan(nat)');
  return parts.join(' ') || '(no media)';
}

function listRow(i) {
  const thumb = i.running
    ? '<img class="thumb" src="/thumb/' + i.name + '.png?t=' +
      Math.floor(Date.now() / 5000) + '" onerror="this.style.opacity=0">'
    : '<div class="thumb-empty">off</div>';
  return '<tr><td style="width:130px">' + thumb + '</td>' +
    '<td><a href="#/vm/' + i.name + '">' + esc(i.name) + '</a></td>' +
    '<td><span class="state' + (i.running ? ' on' : '') + '">' +
    (i.running ? 'running' : 'stopped') + '</span></td>' +
    '<td class="note">' + machineCell(i.machine) + '</td>' +
    '<td>' + esc(i.memory) + (i.snapshot ? ' <span class="note">snap</span>'
                                         : '') + '</td>' +
    '<td class="note" style="max-width:24em;overflow-wrap:anywhere">' +
    esc(mediaSummary(i)) + '</td><td style="text-align:right">' +
    (i.running
     ? '<button onclick="act(\'' + i.name + '\',\'stop\')">Shutdown</button>'
     : '<button class="primary" onclick="act(\'' + i.name +
       '\',\'start\')">Start</button>') + '</td></tr>';
}

let lastList = '';
async function listView(force) {
  const data = await api('/api/instances');
  if (!data) return;
  instances = data;
  drawTree();
  const key = JSON.stringify(data);
  const active = document.activeElement;
  if (!force && key === lastList) {
    for (const img of view.querySelectorAll('img.thumb'))
      img.src = img.src.replace(/t=\d+/, 't=' +
                                Math.floor(Date.now() / 5000));
    return;
  }
  if (!force && active && view.contains(active) &&
      ['INPUT','SELECT','TEXTAREA'].includes(active.tagName)) return;
  lastList = key;
  const running = data.filter(i => i.running).length;
  view.innerHTML =
    '<div class="crumb"><a href="#/">' +
    esc(facts.hostname || 'host') + '</a> &rsaquo; Virtual machines' +
    '</div>' +
    '<div class="titlerow"><span class="glyph">&#9776;</span>' +
    '<h2>Virtual machines</h2><span style="flex:1"></span>' +
    '<span class="note">' + running + ' running, ' +
    (data.length - running) + ' stopped</span></div>' +
    actionBar([
      '<button class="primary" onclick="openCreate()">Create VM</button>',
      '<button onclick="render()">Refresh</button>']) +
    '<div class="card"><h3>Guests</h3><table>' +
    '<tr><th></th><th>Virtual machine</th><th>Status</th>' +
    '<th>Machine</th><th>Memory</th><th>Media</th><th></th></tr>' +
    (data.map(listRow).join('') ||
     '<tr><td colspan="7" class="note">no machines yet</td></tr>') +
    '</table></div>' +
    '<div id="host-card">' + hostCard() + '</div>';
}

// ---------------------------------------------------------- detail view
function diskSelect(key, kind, value, name) {
  const files = catalog[kind] || [];
  // real drives of the matching sort come after the images, so a guest
  // can be pointed at the host's own CD or floppy
  const drives = (hardware.drives || []).filter(
    d => d.type === kind || (kind === 'hdd' && d.type === 'hdd'));
  const known = files.some(f => f.name === value) ||
                drives.some(d => d.path === value);
  return '<select name="' + (name || key) + '">' +
    '<option value="">(none)</option>' +
    files.map(f => '<option' + (f.name === value ? ' selected' : '') + '>' +
      esc(f.name) + '</option>').join('') +
    (drives.length
     ? '<optgroup label="host drives">' + drives.map(d =>
         '<option value="' + esc(d.path) + '"' +
         (d.path === value ? ' selected' : '') +
         (d.busy ? ' disabled' : '') + '>' + esc(d.path) +
         (d.model ? ' — ' + esc(d.model) : '') +
         (d.size ? ' (' + esc(d.size) + ')' : '') +
         (d.busy ? ' [mounted on ' + esc(d.busy) + ']' : '') +
         '</option>').join('') + '</optgroup>'
     : '') +
    (value && !known ? '<option selected>' + esc(value) + '</option>' : '') +
    '</select>';
}

function serialSelect(value, name) {
  const ports = hardware.serial || [];
  return '<select name="' + (name || 'serial') + '">' +
    '<option value="">(none)</option>' +
    ports.map(p => '<option' + (p === value ? ' selected' : '') + '>' +
      esc(p) + '</option>').join('') +
    (value && !ports.includes(value)
     ? '<option selected>' + esc(value) + '</option>' : '') + '</select>';
}

// the three port rows, shared by the wizard and the hardware editor
function portRows(i) {
  return '<div class="row"><label>Serial port</label>' +
    serialSelect(i.serial || '', 'serial') +
    ' <span class="note">RS-232C</span></div>' +
    '<div class="row"><label>Parallel port</label>' +
    serialSelect(i.parallel || '', 'parallel') +
    ' <span class="note">UART BitBang Device (FT245RL)</span></div>' +
    '<div class="row"><label>GP-IB</label>' +
    serialSelect(i.gpib || '', 'gpib') +
    ' <span class="note">adapter on a serial device</span></div>';
}

// Proxmox lays hardware out as a table of what the machine actually has;
// the same rows become a form once Edit is pressed
function hardwareTable(i) {
  const rows = [['&#9636; Memory', esc(i.memory)],
                ['&#9881; Machine', esc(i.machine || 'pc9821') + ' (' +
                 (i.accel === 'tcg' ? 'TCG'
                  : 'KVM (Experimental)') + ')'],
                ['&#9750; BIOS', i.bios === 'real'
                 ? 'real machine ROMs, compatible for the rest'
                 : 'compatible'],
                ['&#9834; Sound', soundName(i.sound)],
                ['&#9635; Display', 'VNC :' + (i.ports[0] - 5900) +
                 ', websocket ' + i.ports[1]]];
  for (const [k, label] of DISK_ROWS)
    if (i[k]) rows.push(['&#9707; ' + label, esc(i[k]) +
      (i[k].startsWith('/dev/')
       ? ' <span class="note">(host drive)</span>'
       : k === 'cd' ? ' <span class="note">(read-only)</span>' : '')]);
  if (i.mount)
    rows.push(['&#9707; Shared folder', esc(i.mount) +
               ' <span class="note">(fat98)</span>']);
  if (i.serial) rows.push(['&#9704; Serial port', esc(i.serial)]);
  if (i.parallel)
    rows.push(['&#9704; Parallel port', esc(i.parallel) +
               ' <span class="note">(UART BitBang Device)</span>']);
  if (i.gpib) rows.push(['&#9704; GP-IB', esc(i.gpib)]);
  rows.push(['&#8646; Network', i.net === 'nat'
             ? 'pc98-lgy98, user NAT' : 'none']);
  if (i.snapshot)
    rows.push(['&#8635; Snapshot', 'changes discarded on shutdown']);
  if (i.extra) rows.push(['&#9656; Extra args', esc(i.extra)]);
  return '<table>' + rows.map(([k, v]) =>
    '<tr><td style="width:13em;color:#8b9298">' + k + '</td><td>' + v +
    '</td></tr>').join('') + '</table>';
}

function editForm(i) {
  // a machine plugin may supply its own hardware form (given the helpers it
  // needs); otherwise the stock PC-98 form below is used
  const custom = window.MiraiPlugins.editForm[i.machine];
  if (custom) {
    return custom(i, { esc, diskSelect, machineList, MEMS });
  }
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    DISK_ROWS.map(([k, label, kind]) =>
      '<div class="row"><label>' + label + '</label>' +
      diskSelect(k, kind, i[k]) + '</div>').join('') +
    '<div class="row"><label>Machine type</label>' +
    '<select name="machine" onchange="applyMachineDefaults(this)">' +
    machineList().map(m => '<option' +
      ((i.machine || 'pc9821') === m ? ' selected' : '') + '>' + m +
      '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Shared folder</label>' +
    '<input type="text" name="mount" value="' + esc(i.mount || '') +
    '" placeholder="/data/share"> <span class="note">appears as an IDE ' +
    'disk (fat98)</span></div>' +
    portRows(i) +
    '<div class="row"><label>BIOS</label><select name="bios">' +
    [['compat','Compatible'], ['real','Real machine ROMs']].map(
      ([v, label]) => '<option value="' + v + '"' +
        ((i.bios || 'compat') === v ? ' selected' : '') + '>' + label +
        '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Memory</label><select name="memory">' +
    MEMS.map(m => '<option' + (i.memory === m ? ' selected' : '') + '>' +
             m + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Sound</label><select name="sound">' +
    ['86','wss','none'].map(s => '<option value="' + s + '"' +
      (soundKey(i.sound) === s ? ' selected' : '') + '>' +
      soundName(s) + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Acceleration</label>' +
    '<label class="check"><input type="checkbox" name="kvm"' +
    (i.accel === 'tcg' ? '' : ' checked') +
    '> use KVM when available <b>(Experimental)</b>' +
    '</label></div>' +
    '<div class="row"><label>Network</label><select name="net">' +
    [['', 'None'], ['nat', 'NAT'], ['bridge', 'Bridge']].map(
      ([v, label]) => '<option value="' + v + '"' +
        ((i.net || '') === v ? ' selected' : '') + '>' + label +
        '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Snapshot</label>' +
    '<label class="check"><input type="checkbox" name="snapshot"' +
    (i.snapshot ? ' checked' : '') + '> discard changes</label></div>' +
    '<div class="row"><label>Extra args</label>' +
    '<input type="text" name="extra" value="' + esc(i.extra) + '"></div>' +
    '<div class="row"><label></label><button class="primary"' +
    (i.running ? ' disabled title="stop it first"' : '') + '>Save</button>' +
    '<button type="button" onclick="removeVm(\'' + i.name + '\')"' +
    (i.running ? ' disabled' : '') + '>Delete</button></div></form>';
}
window.toggleEdit = () => {
  const edit = document.getElementById('hw-edit');
  const show = edit.style.display === 'none';
  edit.style.display = show ? '' : 'none';
  document.getElementById('hw-view').style.display = show ? 'none' : '';
};
window.saveVm = (form, name) => {
  const inst = {name};
  for (const el of form.elements)
    if (el.name) inst[el.name] = el.type === 'checkbox' ? el.checked
                                                        : el.value.trim();
  inst.accel = inst.kvm === false ? 'tcg' : 'kvm';
  delete inst.kvm;
  api('/api/instances/' + name, {method: 'PUT', body: JSON.stringify(inst)})
    .then(d => { if (d) { toast('saved'); task('VM ' + name +
                                               ' - config', 'saved'); }
                 render(); });
  return false;
};

// ------------------------------------------------------- console sound
// QEMU's VNC server streams the guest's PCM over the same WebSocket as
// the pixels (S16LE, stereo, 44.1 kHz).  Chunks are turned into
// AudioBuffers and scheduled back to back on a small jitter cushion.
const AUDIO_RATE = 44100;
const RING_FRAMES = AUDIO_RATE;               // 1 s ring
const AUDIO_PREFILL = Math.floor(AUDIO_RATE * 0.12);
let audioCtx = null, audioNode = null, audioOn = false;
let ringL = null, ringR = null, wPos = 0, rPos = 0, primed = false;
// One continuous ScriptProcessor drains a ring buffer.  Scheduling a fresh
// AudioBuffer per chunk left clicks between them that a pure tone (the
// power-on beep) exposed; a single stream removes the seams.
function audioStart() {
  if (audioCtx) return;
  audioCtx = new AudioContext({sampleRate: AUDIO_RATE});
  ringL = new Float32Array(RING_FRAMES);
  ringR = new Float32Array(RING_FRAMES);
  wPos = 0; rPos = 0; primed = false;
  audioNode = audioCtx.createScriptProcessor(1024, 0, 2);
  audioNode.onaudioprocess = (e) => {
    const oL = e.outputBuffer.getChannelData(0);
    const oR = e.outputBuffer.getChannelData(1);
    const avail = (wPos - rPos + RING_FRAMES) % RING_FRAMES;
    if (!primed) {
      if (avail < AUDIO_PREFILL) { oL.fill(0); oR.fill(0); return; }
      primed = true;
    }
    for (let i = 0; i < oL.length; i++) {
      if (rPos === wPos) { oL[i] = 0; oR[i] = 0; primed = false; }
      else {
        oL[i] = ringL[rPos]; oR[i] = ringR[rPos];
        rPos = (rPos + 1) % RING_FRAMES;
      }
    }
  };
  audioNode.connect(audioCtx.destination);
}
function audioChunk(bytes) {
  if (!audioCtx || !ringL) return;
  const frames = bytes.byteLength >> 2;         // 2 channels x 16 bits
  if (!frames) return;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  for (let i = 0; i < frames; i++) {
    ringL[wPos] = view.getInt16(i * 4, true) / 32768;
    ringR[wPos] = view.getInt16(i * 4 + 2, true) / 32768;
    wPos = (wPos + 1) % RING_FRAMES;
    if (wPos === rPos) rPos = (rPos + 1) % RING_FRAMES;   // full: drop oldest
  }
}
function enableAudioNow() {
  if (!rfb || !rfb.enableAudio) return;
  audioStart();
  audioCtx.resume();
  rfb.enableAudio(3, 2, AUDIO_RATE);          // 3 = S16
  audioOn = true;
  const btn = document.getElementById('btn-audio');
  if (btn) { btn.textContent = '\u{1F50A} Sound on'; }
}
window.toggleAudio = () => {
  if (!rfb || !rfb.enableAudio) { toast('no console'); return; }
  audioOn = !audioOn;
  const btn = document.getElementById('btn-audio');
  if (audioOn) {
    audioStart();
    audioCtx.resume();
    rfb.enableAudio(3, 2, AUDIO_RATE);          // 3 = S16
    if (btn) { btn.textContent = '🔊 Sound on'; }
    toast('sound on');
  } else {
    rfb.disableAudio();
    if (btn) { btn.textContent = '🔇 Sound off'; }
    toast('sound off');
  }
};
function stopAudio() {
  audioOn = false;
  if (audioNode) { try { audioNode.disconnect(); } catch (e) {} audioNode = null; }
  if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
  ringL = ringR = null; wPos = 0; rPos = 0; primed = false;
}

window.connectConsole = async (name, ws) => {
  disconnectConsole();
  window.toggleConsolePane(true);      // a hidden box has no size to scale to
  const target = document.getElementById('console-box');
  target.innerHTML = '';
  try {
    await loadRFB();
  } catch (err) {
    target.textContent = 'noVNC is missing: put it in novnc/ beside the ' +
                         'program.';
    toast('no noVNC to draw the console with');
    return;
  }
  // plugins prepare the connection first (e.g. the relative-pointer
  // negotiation must be in place before the VNC handshake advertises the
  // client encodings, so this happens before the RFB object exists)
  for (const fn of (window.MiraiPlugins.consolePrep || [])) {
    try { await fn(name); } catch (e) { console.error('console prep', e); }
  }
  rfb = new RFB(target, 'ws://' + location.hostname + ':' + ws + '/');
  rfb.scaleViewport = true;
  rfb.background = '#000';
  // let plugins augment the console (e.g. relative-pointer capture)
  window._pluginConsoleCleanups = [];
  (window.MiraiPlugins.console || []).forEach(fn => {
    try {
      const c = fn(rfb, target, name);
      if (c) window._pluginConsoleCleanups.push(c);
    } catch (e) { console.error('console plugin', e); }
  });
  consoleWatch = new ResizeObserver(() =>
    window.dispatchEvent(new Event('resize')));
  consoleWatch.observe(target);
  rfb.addEventListener('connect', () => { toast(name + ': console connected'); enableAudioNow(); });
  rfb.addEventListener('disconnect',
                       () => toast(name + ': console disconnected'));
  rfb.addEventListener('audiodata', e => audioChunk(e.detail.data));
  document.getElementById('btn-connect').style.display = 'none';
  for (const id of ['btn-disconnect','btn-cad','btn-expand','btn-audio'])
    document.getElementById(id).style.display = '';
};
window.disconnectConsole = () => {
  (window._pluginConsoleCleanups || []).forEach(c => { try { c(); } catch (e) {} });
  window._pluginConsoleCleanups = [];
  if (consoleWatch) { consoleWatch.disconnect(); consoleWatch = null; }
  if (rfb) { try { rfb.disconnect(); } catch (e) {} rfb = null; }
  stopAudio();
};
window.toggleConsolePane = show => {
  const pane = document.getElementById('console-pane');
  if (!pane) return false;
  const open = show === true || pane.style.display === 'none';
  pane.style.display = open ? '' : 'none';
  document.getElementById('console-caret').innerHTML =
    open ? '&#9662;' : '&#9656;';
  return open;
};
window.openConsolePane = (name, ws) => {
  window.toggleConsolePane(true);
  if (!rfb) window.connectConsole(name, ws);
};
window.sendCad = () => rfb && rfb.sendCtrlAltDel();
window.expandConsole = () =>
  document.getElementById('console-box').requestFullscreen();

async function detailView(name) {
  const i = await api('/api/instances/' + encodeURIComponent(name));
  if (!i) { view.innerHTML = ''; return; }
  const wasConnected = rfb !== null;
  const disks = DISK_ROWS.filter(([k]) => i[k]);
  const stamp = Math.floor(Date.now() / 5000);
  view.innerHTML =
    '<div class="crumb"><a href="#/">' +
    esc(facts.hostname || 'host') + '</a> &rsaquo; ' +
    '<a href="#/vms">Virtual machines</a> &rsaquo; ' + esc(name) + '</div>' +
    '<div class="titlerow"><span class="glyph">&#9635;</span>' +
    '<h2>' + esc(name) + '</h2>' +
    '<span class="state' + (i.running ? ' on' : '') + '">' +
    (i.running ? 'running' : 'stopped') + '</span></div>' +
    actionBar([
      '<button id="btn-connect" class="primary" onclick="connectConsole(\'' +
      name + '\',' + i.ports[1] + ')"' + (i.running ? '' : ' disabled') +
      '>Console</button>',
      '<button id="btn-disconnect" style="display:none" ' +
      'onclick="disconnectConsole();render()">Disconnect</button>',
      '<button id="btn-cad" style="display:none" onclick="sendCad()">' +
      'Ctrl+Alt+Del</button>',
      '<button id="btn-expand" style="display:none" ' +
      'onclick="expandConsole()">Fullscreen</button>',
      '<button id="btn-audio" style="display:none" ' +
      'onclick="toggleAudio()">🔇 Sound off</button>',
      i.running
        ? '<button onclick="act(\'' + name + '\',\'stop\')">Shut down' +
          '</button>'
        : '<button class="primary" onclick="act(\'' + name +
          '\',\'start\')">Power on</button>',
      i.running
        ? '<button onclick="act(\'' + name + '\',\'save\')">Suspend</button>'
        : '<button onclick="act(\'' + name + '\',\'resume\')">Resume' +
          '</button>',
      i.running
        ? '<button onclick="act(\'' + name + '\',\'reset\')">Restart' +
          '</button>' : '',
      '<button onclick="toggleEdit()"' +
      (i.running ? ' disabled title="power it off first"' : '') +
      '>Edit</button>',
      '<button onclick="render()">Refresh</button>',
      i.running ? '' : '<button onclick="removeVm(\'' + name +
                       '\')">Delete</button>']) +
    // the screen comes first, but folded away until it is wanted
    '<div class="card"><h3 style="cursor:pointer" ' +
    'onclick="toggleConsolePane()"><span id="console-caret">&#9656;</span>' +
    ' Console</h3>' +
    '<div class="body" id="console-pane" style="display:none">' +
    '<div id="console-box"><div class="hint">' +
    (i.running ? 'press Console to connect' : 'the machine is powered off') +
    '</div></div></div></div>' +
    // then the summary strip: still shot, facts, gauges
    '<div style="display:flex;gap:1.2em;flex-wrap:wrap;margin-bottom:1em">' +
    '<div>' + (i.running
      ? '<img class="thumb" style="width:230px;height:144px;cursor:pointer"' +
        ' title="open the console" onclick="openConsolePane(\'' + name +
        '\',' + i.ports[1] + ')" src="/thumb/' +
        name + '.png?t=' + stamp + '">'
      : '<div class="thumb-empty" style="width:230px;height:144px">' +
        'powered off</div>') + '</div>' +
    '<div style="flex:1;min-width:18em"><dl>' +
    '<dt>Machine type</dt><dd>' + esc(i.machine || 'pc9821') + '</dd>' +
    '<dt>BIOS</dt><dd>' + (i.bios === 'real' ? 'real machine ROMs'
                                             : 'compatible') + '</dd>' +
    '<dt>Acceleration</dt><dd>' + (i.accel === 'tcg' ? 'TCG'
                     : 'KVM (Experimental)') + '</dd>' +
    '<dt>Memory</dt><dd>' + esc(i.memory) + '</dd>' +
    '<dt>Disks</dt><dd>' + (disks.length || 'none — N88 BASIC') + '</dd>' +
    '<dt>Console</dt><dd>VNC :' + (i.ports[0] - 5900) + '</dd></dl></div>' +
    '<div style="width:16em" id="gauges"></div></div>' +
    // the two panels, as VMware arranges them
    '<div class="grid2" style="grid-template-columns:1fr 1fr">' +
    '<div class="card"><h3>General information</h3><table>' +
    '<tr><td style="width:11em;color:#8d99a5">Networking</td><td>' +
    (i.net === 'nat' ? 'LGY-98, user NAT'
     : i.net === 'bridge' ? 'LGY-98, bridged to the LAN' : 'none') +
    '</td></tr>' +
    '<tr><td style="color:#8d99a5">Storage</td><td>' +
    (disks.length + ' disk' + (disks.length === 1 ? '' : 's')) +
    (i.snapshot ? ', changes discarded at shutdown' : '') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Shared folder</td><td>' +
    (i.mount ? esc(i.mount) + ' (fat98)' : 'none') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Serial port</td><td>' +
    (i.serial ? esc(i.serial) : 'none') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Parallel port</td><td>' +
    (i.parallel ? esc(i.parallel) +
     ' <span class="note">UART BitBang Device</span>' : 'none') +
    '</td></tr>' +
    '<tr><td style="color:#8d99a5">GP-IB</td><td>' +
    (i.gpib ? esc(i.gpib) : 'none') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">Home</td><td>vm/vm-' + i.index +
    '/</td></tr>' +
    (i.running ? '<tr><td style="color:#8d99a5">Media</td>' +
     '<td id="media-row" class="note">reading...</td></tr>' : '') +
    '<tr><td style="color:#8d99a5">QMP</td><td>127.0.0.1:' + i.ports[2] +
    '</td></tr></table></div>' +
    '<div class="card"><h3>Hardware configuration</h3>' +
    '<div id="hw-view">' + hardwareTable(i) + '</div>' +
    '<div id="hw-edit" class="body" style="display:none">' +
    editForm(i) + '</div></div>' +
    '</div>';
  if ((wasConnected || autoConnect === name) && i.running)
    window.connectConsole(name, i.ports[1]);
  autoConnect = '';
  updateUsage(name);
  if (i.running) drawMedia(name);
}

// swapping a disk while the machine runs, the way a hand would
async function drawMedia(name) {
  const slot = document.getElementById('media-row');
  if (!slot) return;
  const d = await api('/api/instances/' + encodeURIComponent(name) +
                      '/media');
  if (!d || !document.getElementById('media-row')) return;
  if (!d.drives.length) {
    document.getElementById('media-row').textContent =
      'no floppy or CD-ROM drive';
    return;
  }
  document.getElementById('media-row').innerHTML = d.drives.map(drive => {
    const files = catalog[drive.kind] || [];
    const here = drive.file.split('/').pop();
    return '<div class="row" style="margin:.1em 0">' +
      '<span style="width:5.5em">' + esc(drive.device) + '</span>' +
      '<select onchange="swapMedia(\'' + name + '\',\'' + drive.device +
      '\',this.value)"><option value="">(empty)</option>' +
      files.map(f => '<option' + (f.name === here ? ' selected' : '') + '>' +
        esc(f.name) + '</option>').join('') +
      (here && !files.some(f => f.name === here)
       ? '<option selected>' + esc(here) + '</option>' : '') +
      '</select></div>';
  }).join('');
}
window.swapMedia = (name, device, file) => {
  api('/api/instances/' + encodeURIComponent(name) + '/media',
      {method: 'POST', body: JSON.stringify({device, name: file})})
    .then(r => { if (r) { toast(device + ': ' + r.result);
                          task('VM ' + name + ' - ' + device, r.result); }
                 drawMedia(name); });
};

// the three readings VMware stacks down the right of a VM summary
async function updateUsage(name) {
  const slot = document.getElementById('gauges');
  if (!slot) return;
  const s = await api('/api/instances/' + encodeURIComponent(name) +
                      '/stats');
  if (!s || !document.getElementById('gauges')) return;
  const inst = instances.find(v => v.name === name) || {};
  const gauge = (label, value, note) =>
    '<div style="display:flex;align-items:center;gap:.6em;' +
    'margin-bottom:.9em"><div style="flex:1;text-align:right">' +
    '<div class="note" style="letter-spacing:.08em">' + label + '</div>' +
    '<div style="font-size:17px">' + value + '</div>' +
    (note ? '<div class="note">' + note + '</div>' : '') + '</div>' +
    '<div style="width:22px;height:22px;border:1px solid var(--line);' +
    'background:#171c21"></div></div>';
  document.getElementById('gauges').innerHTML = s.running
    ? gauge('CPU', (s.cpu == null ? '...' : s.cpu + '%'),
            'of one guest CPU') +
      gauge('MEMORY', esc(inst.memory || ''),
            'host RSS ' + fmtBytes(s.rss)) +
      gauge('PROCESS', 'pid ' + s.pid, '')
    : gauge('CPU', '—', '') + gauge('MEMORY', esc(inst.memory || ''), '') +
      gauge('PROCESS', 'not running', '');
}

// --------------------------------------------------------- storage view
function fmtDate(t) { return new Date(t * 1000).toLocaleString(); }
const extOf = n => { const i = n.lastIndexOf('.');
                     return i < 0 ? '' : n.slice(i).toLowerCase(); };
const CONVERT_TARGETS = {
  'hdd:.hdi': ['raw'], 'hdd:.raw': ['hdi','qcow2'],
  'hdd:.img': ['hdi','qcow2'], 'hdd:.qcow2': ['raw'],
  'fdd:.fdi': ['raw'], 'fdd:.raw': ['fdi'], 'fdd:.img': ['fdi'],
};

function storageCard(kind, files) {
  const rows = files.map(f => {
    const used = f.used_by.join(', ');
    const targets = CONVERT_TARGETS[kind + ':' + extOf(f.name)] || [];
    return '<tr><td><a href="#/disk/' + kind + '/' +
      encodeURIComponent(f.name) + '">' + esc(f.name) + '</a></td><td>' +
      fmtBytes(f.size) +
      '</td><td class="note">' + fmtDate(f.mtime) + '</td><td>' +
      esc(used || '-') + '</td><td style="text-align:right">' +
      '<button type="button" onclick="showTree(\'' + kind + '\',\'' +
      f.name + '\')">Contents</button> ' +
      '<a href="/disks/' + kind + '/' + f.name + '" download>' +
      '<button type="button">Download</button></a> ' +
      targets.map(t => '<button type="button" onclick="convertDisk(\'' +
        kind + '\',\'' + f.name + '\',\'' + t + '\')">to ' + t +
        '</button>').join(' ') +
      ' <button type="button"' + (used ? ' disabled title="in use"' : '') +
      ' onclick="deleteDisk(\'' + kind + '\',\'' + f.name +
      '\')">Delete</button></td></tr>';
  }).join('');
  let create = '';
  if (kind === 'hdd')
    create = '<h4>Create a disk</h4><div class="body">' +
      '<form onsubmit="return createDisk(this,\'hdd\')" class="row">' +
      '<input type="text" name="name" placeholder="new-disk.qcow2" ' +
      'required style="min-width:11em"><select name="size">' +
      [40,80,160,320,640,1200,2100,4300].map(s => '<option' +
        (s === 40 ? ' selected' : '') + '>' + s + '</option>').join('') +
      '</select><span class="note">MB</span>' +
      '<label class="check"><input type="checkbox" name="fat32"> FAT32' +
      '</label><button class="primary">Create</button></form>' +
      '<div class="note">.qcow2 grows on demand. .hdi: Anex86. ' +
      '.raw: flat.</div></div>';
  else if (kind === 'fdd')
    create = '<h4>Create a floppy</h4><div class="body">' +
      '<form onsubmit="return createDisk(this,\'fdd\')" class="row">' +
      '<input type="text" name="name" placeholder="new-floppy.fdi" ' +
      'required style="min-width:11em"><select name="format">' +
      '<option>1.2</option><option>1.44</option></select>' +
      '<button class="primary">Create</button></form>' +
      '<div class="note">.fdi or .raw. Formatted, empty.</div></div>';
  return '<div class="card"><h3>disks/' + kind + '/</h3>' +
    '<h4>Images</h4><table>' +
    '<tr><th>Name</th><th>Size</th><th>Modified</th><th>Used by</th>' +
    '<th></th></tr>' +
    (rows || '<tr><td colspan="5" class="note">(empty)</td></tr>') +
    '</table>' +
    '<h4>Add an image</h4><div class="body"><div class="row">' +
    '<button type="button" onclick="pickUpload(\'' + kind +
    '\')">Upload from this computer...</button>' +
    '<button type="button" onclick="fetchDisk(\'' + kind +
    '\')">Download from URL...</button>' +
    '<button type="button" onclick="importDisk(\'' + kind +
    '\')">Import a server path...</button>' +
    '<button type="button" onclick="readFromDrive(\'' + kind +
    '\')">Read a host drive...</button></div>' +
    '<div id="jobs-' + kind + '" class="note"></div></div>' +
    create + '</div>';
}

function romCard(data) {
  const rows = data.roms.map(r =>
    '<tr><td>' + esc(r.name) + '</td><td>' +
    (r.present ? fmtBytes(r.size) : '<span class="note">not uploaded, ' +
     'the compatible ROM stands in</span>') + '</td>' +
    '<td style="text-align:right">' +
    '<button type="button" onclick="pickRom(\'' + r.name + '\')">' +
    (r.present ? 'Replace...' : 'Upload...') + '</button>' +
    (r.present ? ' <button type="button" onclick="deleteRom(\'' + r.name +
     '\')">Remove</button>' : '') + '</td></tr>').join('');
  return '<div class="card"><h3>Real machine ROMs &mdash; ' +
    esc(data.dir) + '</h3><table>' +
    '<tr><th>File</th><th>State</th><th></th></tr>' + rows + '</table>' +
    '<div class="body note">A machine set to the real BIOS uses these ' +
    'and falls back to the compatible ROMs. N88 BASIC needs ' +
    'pc98basic.bin. No compatible version yet.</div></div>';
}

window.pickRom = name => {
  const input = document.createElement('input');
  input.type = 'file';
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/roms?name=' + encodeURIComponent(name));
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.response); } catch (e) {}
      if (xhr.status === 200) {
        toast('uploaded ' + name);
        task('ROM ' + name + ' - upload', 'OK');
        render();
      } else {
        toast(r.error || 'upload failed');
        task('ROM ' + name + ' - upload', r.error || 'failed');
      }
    };
    xhr.send(file);
  };
  input.click();
};
window.deleteRom = name => {
  if (!confirm('remove ' + name + '?')) return;
  api('/api/roms/' + name, {method: 'DELETE'})
    .then(r => { if (r) { toast('removed ' + name);
                          task('ROM ' + name + ' - remove', 'OK'); }
                 render(); });
};

// ---------------------------------------------------- one disk's page
async function diskView(kind, name) {
  view.innerHTML = '<div class="note">reading ' + esc(name) + '...</div>';
  const d = await api('/api/disk/' + kind + '/' + encodeURIComponent(name));
  if (!d) { view.innerHTML = ''; return; }
  const listing = d.error
    ? '<div class="body note">Cannot read the file system inside: ' +
      esc(d.error) + '</div>'
    : '<table><tr><th>Name</th><th style="width:8em">Size</th></tr>' +
      (d.files.length
       ? d.files.map(f => '<tr><td>' + esc(f.name) + '</td><td>' +
           (f.size == null ? '' : fmtBytes(f.size)) + '</td></tr>').join('')
       : '<tr><td colspan="2" class="note">(empty)</td></tr>') + '</table>';
  view.innerHTML =
    '<div class="crumb"><a href="#/">' + esc(facts.hostname || 'host') +
    '</a> &rsaquo; <a href="#/storage">Storage</a> &rsaquo; ' +
    esc(kind) + ' &rsaquo; ' + esc(name) + '</div>' +
    '<div class="titlerow"><span class="glyph">&#9707;</span>' +
    '<h2>' + esc(name) + '</h2>' +
    (d.used_by.length
     ? '<span class="state on">in use</span>' : '') + '</div>' +
    actionBar([
      '<a href="/disks/' + kind + '/' + encodeURIComponent(name) +
      '" download><button>Download image</button></a>',
      '<a href="/zip/' + kind + '/' + encodeURIComponent(name) +
      '"><button>Download contents as ZIP</button></a>',
      '<button onclick="pickZip(\'' + kind + '\',\'' + name +
      '\')">Write a ZIP into it...</button>',
      '<button onclick="copyDisk(\'' + kind + '\',\'' + name +
      '\')">Make a copy</button>',
      '<button onclick="writeToDrive(\'' + kind + '\',\'' + name +
      '\')">Write to a drive...</button>',
      '<button onclick="render()">Refresh</button>',
      d.used_by.length ? '' : '<button onclick="deleteDisk(\'' + kind +
        '\',\'' + name + '\',\'#/storage\')">Delete</button>']) +
    '<div class="grid2" style="grid-template-columns:22em 1fr">' +
    '<div class="card"><h3>Details</h3><table>' +
    [['File', d.path], ['Format', d.format], ['Size', fmtBytes(d.size)],
     ['Contents', d.error ? 'unreadable'
       : d.files.length + ' entries, ' + fmtBytes(d.bytes)],
     ['Modified', new Date(d.mtime * 1000).toLocaleString()],
     ['Used by', d.used_by.join(', ') || 'no machine']]
      .map(([k, v]) => '<tr><td style="width:7em;color:#8d99a5">' + k +
        '</td><td style="overflow-wrap:anywhere">' + esc(v) +
        '</td></tr>').join('') + '</table></div>' +
    '<div class="card"><h3>File system</h3>' + listing + '</div></div>';
}

// real drives, both ways: a card written from an image, or an image
// taken off a card
function freeDrives(kind) {
  return (hardware.drives || []).filter(
    d => !d.busy && !d.system &&
         (kind === 'cdrom' ? d.type === 'cdrom' : d.type !== 'cdrom'));
}
async function askDrive(kind, verb) {
  // a stick plugged in a moment ago has to be in this list
  await refreshGear(true);
  const list = freeDrives(kind);
  if (!list.length) {
    toast('no free drive: everything the host can see is mounted');
    return null;
  }
  const menu = list.map((d, n) => n + ': ' + d.path +
    (d.model ? ' — ' + d.model : '') + (d.size ? ' (' + d.size + ')' : ''))
    .join('\n');
  const pick = prompt(verb + '\n\n' + menu + '\n\nnumber:', '0');
  if (pick === null) return null;
  const chosen = list[parseInt(pick, 10)];
  if (!chosen) { toast('no such drive'); return null; }
  return chosen;
}
window.writeToDrive = async (kind, name) => {
  const drive = await askDrive(kind, 'Write ' + name + ' onto which drive?');
  if (!drive) return;
  // The server will not write unless it is told back what the drive says
  // about itself, and it checks that against the drive rather than against
  // anything sent from here.  So if the wrong drive was picked, this is
  // where it shows, and nothing on this side can wave it through.
  const asked = drive.model || String(drive.size_bytes);
  const said = prompt('Everything on ' + drive.path + ' will be overwritten' +
    ' by ' + name + '.\n\nType this to confirm:\n\n    ' + asked, '');
  if (said === null) return;
  api('/api/disk/' + kind + '/' + encodeURIComponent(name) + '/to-drive',
      {method: 'POST', body: JSON.stringify(
        {device: drive.path, confirm: said, internal: !drive.removable})})
    .then(r => { if (r) { toast('writing to ' + drive.path);
                          task('Disk ' + name + ' → ' + drive.path,
                               'started'); pollJobs(); } });
};
window.readFromDrive = async kind => {
  const drive = await askDrive(kind, 'Read which drive into a new image?');
  if (!drive) return;
  const guess = drive.path.split('/').pop() +
                (kind === 'fdd' ? '.raw' : '.raw');
  const name = prompt('name for the new image', guess);
  if (!name) return;
  api('/api/disks/' + kind + '/from-drive',
      {method: 'POST',
       body: JSON.stringify({device: drive.path, name})})
    .then(r => { if (r) { toast('reading ' + drive.path);
                          task('Disk ' + drive.path + ' → ' + name,
                               'started'); pollJobs(); } });
};

window.copyDisk = (kind, name) => {
  const dot = name.lastIndexOf('.');
  const guess = dot > 0 ? name.slice(0, dot) + '-copy' + name.slice(dot)
                        : name + '-copy';
  const target = prompt('name for the copy', guess);
  if (!target) return;
  toast('copying ' + name + '...');
  api('/api/disk/' + kind + '/' + encodeURIComponent(name) + '/copy',
      {method: 'POST', body: JSON.stringify({name: target})})
    .then(r => { if (r) { toast('made ' + r.name);
                          task('Disk ' + name + ' - copy', 'OK');
                          location.hash = '#/disk/' + kind + '/' + r.name; }
                 render(); });
};

window.pickZip = (kind, name) => {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.zip,application/zip';
  input.onchange = () => {
    const file = input.files[0];
    if (!file) return;
    toast('writing ' + file.name + ' into ' + name + '...');
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/disk/' + kind + '/' + encodeURIComponent(name) +
             '/unzip');
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.response); } catch (e) {}
      if (xhr.status === 200) {
        toast('wrote ' + file.name + ' into ' + name);
        task('Disk ' + name + ' - write ZIP', 'OK');
      } else {
        toast(r.error || 'failed');
        task('Disk ' + name + ' - write ZIP', r.error || 'failed');
      }
      render();
    };
    xhr.onerror = () => toast('the connection dropped');
    xhr.send(file);
  };
  input.click();
};

async function storageView() {
  // asked for alongside the rest rather than after it: three round trips
  // one behind the other is what made opening this page feel slow
  const roms = await romsSoon;
  view.innerHTML = '<div class="topbar"><h2>Storage</h2>' +
    '<span class="note">' + (hostFacts.disk_total
      ? fmtBytes(hostFacts.disk_free) + ' free of ' +
        fmtBytes(hostFacts.disk_total) : '') + '</span></div>' +
    ['hdd','fdd','cdrom'].map(k => storageCard(k, catalog[k])).join('') +
    (roms ? romCard(roms) : '');
}

window.createDisk = (form, kind) => {
  const d = {};
  for (const el of form.elements)
    if (el.name) d[el.name] = el.type === 'checkbox' ? el.checked
                                                     : el.value.trim();
  toast('creating ' + d.name + '...');
  api('/api/disks/' + kind + '/create',
      {method: 'POST', body: JSON.stringify(d)})
    .then(r => { if (r) { toast('created ' + r.name);
                          task('Disk ' + r.name + ' - create', 'OK'); }
                 render(); });
  return false;
};
window.convertDisk = (kind, name, target) => {
  toast('converting ' + name + '...');
  api('/api/disks/' + kind + '/convert',
      {method: 'POST', body: JSON.stringify({source: name, format: target})})
    .then(r => { if (r) { toast('made ' + r.name);
                          task('Disk ' + name + ' - convert to ' + target,
                               'OK'); }
                 render(); });
};
window.deleteDisk = (kind, name, then) => {
  if (!confirm('delete ' + name + '?')) return;
  api('/api/disks/' + kind + '/' + encodeURIComponent(name),
      {method: 'DELETE'})
    .then(r => { if (r) { toast('deleted ' + name);
                          task('Disk ' + name + ' - delete', 'OK');
                          if (then) { location.hash = then; return; } }
                 render(); });
};
// a FAT image can be read without starting anything, so show what is in
// it; anything else says why it cannot
window.showTree = async (kind, name) => {
  const veil = document.getElementById('tree-veil');
  document.getElementById('tree-name').textContent = name;
  document.getElementById('tree-body').textContent = 'reading...';
  veil.classList.add('open');
  const d = await api('/api/disk/' + kind + '/' + encodeURIComponent(name));
  if (!d) { veil.classList.remove('open'); return; }
  document.getElementById('tree-body').innerHTML = d.error
    ? '<div class="note">no readable file system: ' + esc(d.error) + '</div>'
    : '<div class="note">' + d.files.length + ' entries, ' +
      fmtBytes(d.bytes) + '</div><pre style="margin:.4em 0 0">' +
      esc(d.files.map(f => (f.name.endsWith('/') ? f.name
        : f.name.padEnd(28) + (f.size == null ? '' : fmtBytes(f.size))))
        .join('\n')) + '</pre>';
};
window.closeTree = () =>
  document.getElementById('tree-veil').classList.remove('open');

window.fetchDisk = kind => {
  const url = prompt('image URL to download into disks/' + kind + '/');
  if (!url) return;
  api('/api/disks/' + kind + '/fetch',
      {method: 'POST', body: JSON.stringify({url})})
    .then(r => { if (r) { toast('downloading ' + r.name);
                          task('Disk ' + r.name + ' - download', 'started');
                          pollJobs(); } });
};
async function pollJobs() {
  const jobs = await api('/api/jobs');
  if (!jobs) return;
  for (const kind of ['hdd', 'fdd', 'cdrom']) {
    const slot = document.getElementById('jobs-' + kind);
    if (!slot) continue;
    const mine = jobs.filter(j => j.kind === kind);
    slot.innerHTML = mine.map(j =>
      j.state === 'running'
        // a write to a drive is read back afterwards and compared, which
        // takes as long again and is worth saying rather than looking stuck
        ? (j.checking ? 'checking ' : '') + esc(j.name) + ': ' +
          fmtBytes(j.done) +
          (j.total ? ' of ' + fmtBytes(j.total) + ' (' +
           (j.done / j.total * 100).toFixed(0) + '%)' : '')
        : j.state === 'failed'
          ? '<span style="color:#e06c5f">' + esc(j.name) + ': ' +
            esc(j.error) + '</span>'
          : esc(j.name) + ': done' +
            (j.verified ? ', read back and checked' : '')).join('<br>');
    if (mine.some(j => j.state === 'done' &&
                       !catalog[kind].some(f => f.name === j.name)))
      render();
  }
}
window.importDisk = kind => {
  const path = prompt('server path to adopt into disks/' + kind + '/');
  if (!path) return;
  api('/api/disks/' + kind + '/import',
      {method: 'POST', body: JSON.stringify({path})})
    .then(r => { if (r) { toast('imported ' + r.name);
                          task('Disk ' + r.name + ' - import', 'OK'); }
                 render(); });
};
// ------------------------------------------------------------- uploads
// A whole disk image goes up in slices: the browser only has to hold one
// at a time, a stall costs one slice rather than the lot, and what
// arrived stays on the server so the next try continues from there.
const CHUNK = 16 << 20;
let uploadStop = false;

function uploadBox(file, kind) {
  document.getElementById('upload-name').textContent =
    file.name + ' → disks/' + kind + '/';
  document.getElementById('upload-veil').classList.add('open');
  document.getElementById('upload-bar').value = 0;
  document.getElementById('upload-stat').textContent = '';
  document.getElementById('upload-close').textContent = 'Cancel';
  uploadStop = false;
}
window.closeUpload = () => {
  uploadStop = true;
  document.getElementById('upload-veil').classList.remove('open');
};

function sendSlice(url, blob) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    xhr.onload = () => {
      let r = {};
      try { r = JSON.parse(xhr.response); } catch (e) {}
      xhr.status === 200 ? resolve(r)
                         : reject(new Error(r.error || 'HTTP ' + xhr.status));
    };
    xhr.onerror = () => reject(new Error('the connection dropped'));
    xhr.send(blob);
  });
}

window.pickUpload = kind => {
  const input = document.createElement('input');
  input.type = 'file';
  input.onchange = async () => {
    const file = input.files[0];
    if (!file) return;
    let overwrite = '';
    if (catalog[kind].some(f => f.name === file.name)) {
      if (!confirm(file.name + ' exists. Overwrite?')) return;
      overwrite = '&overwrite=1';
    }
    const base = '/api/disks/' + kind;
    const q = 'name=' + encodeURIComponent(file.name) + '&total=' +
              file.size;
    uploadBox(file, kind);
    const bar = document.getElementById('upload-bar');
    const stat = document.getElementById('upload-stat');
    const started = Date.now();
    let sent = 0, tries = 0;
    while (sent < file.size) {
      if (uploadStop) { toast('upload cancelled'); return; }
      const end = Math.min(sent + CHUNK, file.size);
      try {
        const r = await sendSlice(
          base + '/chunk?' + q + '&offset=' + sent + overwrite,
          file.slice(sent, end));
        sent = r.have ?? end;
        tries = 0;
      } catch (err) {
        if (++tries > 3) {
          stat.textContent = 'stopped: ' + err.message;
          document.getElementById('upload-close').textContent = 'Close';
          toast('upload failed: ' + err.message);
          task('Disk ' + file.name + ' - upload', err.message);
          return;
        }
        stat.textContent = 'retrying after ' + err.message + ' (' + tries +
                           '/3)';
        await new Promise(r => setTimeout(r, 1500));
        continue;
      }
      const secs = (Date.now() - started) / 1000;
      const rate = sent / Math.max(secs, 0.1);
      const left = (file.size - sent) / Math.max(rate, 1);
      bar.value = sent / file.size * 100;
      stat.textContent = fmtBytes(sent) + ' of ' + fmtBytes(file.size) +
        '  —  ' + fmtBytes(rate) + '/s, ' +
        (left > 90 ? Math.round(left / 60) + ' min left'
                   : Math.round(left) + ' s left');
    }
    stat.textContent = 'finishing...';
    try {
      const done = await sendSlice(base + '/finish?' + q, null);
      const named = done.name || file.name;
      stat.textContent = 'uploaded ' + named +
        (named !== file.name ? ' (converted on the way in)' : '');
      document.getElementById('upload-close').textContent = 'Close';
      toast('uploaded ' + named);
      task('Disk ' + named + ' - upload', 'OK');
      render();
    } catch (err) {
      stat.textContent = 'failed: ' + err.message;
      document.getElementById('upload-close').textContent = 'Close';
      task('Disk ' + file.name + ' - upload', err.message);
    }
  };
  input.click();
};

// ------------------------------------------------------ create wizard
window.memSlide = el => {
  document.getElementById('mem-text').value = MEMS[el.value];
};
window.memType = el => {
  const i = MEMS.indexOf(el.value.trim().toUpperCase());
  if (i >= 0) document.getElementById('mem-range').value = i;
};
function drawTabs() {
  document.getElementById('tabs').innerHTML = TABS.map((t, n) =>
    '<span class="' + (n === tab ? 'on' : '') + '" onclick="tabGo(' + n +
    ')">' + t + '</span>').join('');
  document.querySelectorAll('#wizard .pane').forEach((p, n) =>
    p.classList.toggle('on', n === tab));
  document.getElementById('btn-back').disabled = tab === 0;
  const last = tab === TABS.length - 1;
  document.getElementById('btn-next').style.display = last ? 'none' : '';
  document.getElementById('btn-finish').style.display = last ? '' : 'none';
  if (last) drawConfirm();
}
window.tabGo = n => { tab = n; drawTabs(); };
window.tabStep = d => { tab = Math.max(0, Math.min(TABS.length - 1,
                                                   tab + d));
                        drawTabs(); };
function wizardValues() {
  const form = document.getElementById('wizard');
  const out = {};
  for (const el of form.elements)
    if (el.name) out[el.name] = el.type === 'checkbox' ? el.checked
                                                       : el.value.trim();
  out.accel = out.kvm === false ? 'tcg' : 'kvm';
  delete out.kvm;
  return out;
}
function drawConfirm() {
  const v = wizardValues();
  const anyDisk = DISK_ROWS.some(([k]) => v[k]);
  const rows = [['Name', v.name || '(unnamed)'],
                ['Machine type', v.machine],
                ['BIOS', v.bios === 'real' ? 'real machine ROMs'
                                           : 'compatible'],
                ['Memory', v.memory],
                ['Sound', soundName(v.sound)],
                ['Acceleration', v.accel === 'kvm'
                                ? 'KVM (Experimental)'
                                                   : 'TCG only'],
                ['Snapshot', v.snapshot ? 'yes' : 'no'],
                ['Network', v.net === 'nat' ? 'NAT (LGY-98)'
                            : v.net === 'bridge' ? 'Bridge (LGY-98)'
                            : 'none']];
  for (const [k, label] of DISK_ROWS)
    if (v[k]) rows.push([label, v[k]]);
  if (v.mount) rows.push(['Shared folder', v.mount + ' (fat98)']);
  if (v.serial) rows.push(['Serial port', v.serial]);
  if (v.parallel) rows.push(['Parallel port',
                             v.parallel + ' (UART BitBang Device)']);
  if (v.gpib) rows.push(['GP-IB', v.gpib]);
  if (v.extra) rows.push(['Extra args', v.extra]);
  if (!anyDisk) rows.push(['Disks', 'none — the machine will land in ' +
                           'N88 BASIC, which lives in ROM']);
  document.getElementById('confirm-table').innerHTML = '<table>' +
    rows.map(([k, x]) => '<tr><td style="width:11em;color:#8b9298">' + k +
      '</td><td>' + esc(x) + '</td></tr>').join('') + '</table>';
}
window.openCreate = () => {
  document.getElementById('pane-disks').innerHTML = DISK_ROWS.map(
    ([k, label, kind]) => '<div class="row"><label>' + label + '</label>' +
    diskSelect(k, kind, '') + '</div>').join('') +
    '<div class="note">Images and host drives. Manage images in Storage. ' +
    'No disk: starts N88 BASIC.</div>';
  document.getElementById('pane-ports').innerHTML = portRows({});
  const range = document.getElementById('mem-range');
  range.max = MEMS.length - 1;
  range.value = MEMS.indexOf('64M');
  document.getElementById('mem-text').value = '64M';
  document.getElementById('wizard').reset();
  document.getElementById('mem-text').value = '64M';
  // let plugins add their machine types to the (static) create form select
  const msel = document.querySelector('#wizard select[name="machine"]');
  if (msel) {
    window.MiraiPlugins.machines.forEach(m => {
      if (![...msel.options].some(o => o.value === m)) {
        const o = document.createElement('option');
        o.value = m; o.textContent = m;
        msel.appendChild(o);
      }
    });
    msel.onchange = () => window.applyMachineDefaults(msel);
  }
  tab = 0;
  drawTabs();
  document.getElementById('veil').classList.add('open');
};
window.closeCreate = () =>
  document.getElementById('veil').classList.remove('open');
function wizardSays(text) {
  const note = document.getElementById('wizard-note');
  note.textContent = text || '';
  note.style.color = text ? '#e06c5f' : '';
}
window.submitCreate = () => {
  const v = wizardValues();
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(v.name)) {
    wizardSays('the name must be 1-32 letters, digits, - or _');
    tabGo(0);
    return false;
  }
  wizardSays('creating...');
  // the answer belongs in the dialog: a toast at the top of the page is
  // easy to miss while looking at the form that just refused to close
  fetch('/api/instances', {method: 'POST', body: JSON.stringify(v)})
    .then(async r => {
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        wizardSays(d.error || ('the server said ' + r.status));
        task('VM ' + v.name + ' - create', d.error || 'failed');
        return;
      }
      wizardSays('');
      toast('created ' + v.name);
      task('VM ' + v.name + ' - create', 'OK');
      closeCreate();
      location.hash = '#/vm/' + v.name;
    })
    .catch(err => wizardSays(String(err)));
  return false;
};

// --------------------------------------------------------------- router
// the navigator shows the whole fleet whatever page is open, so these
// come along on every render, not only on the list
// What drives the host has costs the server a real look at the hardware —
// on Windows that is a PowerShell run, most of a second — and it hardly
// ever changes.  So it is fetched lazily here, and always fresh at the one
// moment it matters, which is when a drive is about to be picked.
let gearAt = 0;
const GEAR_AGE = 15000;
async function refreshGear(force) {
  if (!force && Date.now() - gearAt < GEAR_AGE) return;
  const gear = await api('/api/hardware');
  if (gear) { hardware = gear; gearAt = Date.now(); }
}

async function refreshFleet() {
  const [fleet] = await Promise.all([api('/api/instances'), refreshGear()]);
  if (fleet) instances = fleet;
  if (!facts.hostname) {
    const f = await api('/api/facts');
    if (f) facts = f;
  }
}

// the ROM card on the storage page needs this; started here so it travels
// with the disks rather than after them
let romsSoon = Promise.resolve(null);

async function refreshDisks() {
  romsSoon = api('/api/roms');
  await refreshGear();
  const disks = await api('/api/disks');
  if (!disks) return;
  catalog = disks;
  document.getElementById('datalists').innerHTML =
    ['hdd','fdd','cdrom'].map(kind => '<datalist id="dl-' + kind + '">' +
      disks[kind].map(f => '<option value="' + esc(f.name) + '">').join('') +
      '</datalist>').join('');
}
// --------------------------------------------------------- system view
function meter(label, used, total, extra) {
  const pct = total ? used / total * 100 : 0;
  return '<div style="margin:.5em 0"><div style="display:flex">' +
    '<span style="color:#8b9298">' + label + '</span>' +
    '<span style="flex:1"></span><span>' + pct.toFixed(1) + '% (' +
    fmtBytes(used) + ' of ' + fmtBytes(total) + ')' +
    (extra ? ' <span class="note">' + extra + '</span>' : '') + '</span>' +
    '</div><div style="height:5px;background:#1b1e21;border:1px solid ' +
    'var(--line);margin-top:.2em"><div style="height:100%;width:' +
    Math.min(100, pct).toFixed(1) + '%;background:var(--cpu)"></div>' +
    '</div></div>';
}

async function systemView() {
  const f = facts.hostname ? facts : (await api('/api/facts') || {});
  const h = hostFacts;
  const cpuPct = history.cpu.at(-1) ?? 0;
  const running = instances.filter(i => i.running).length;
  const rows = [
    ['CPU(s)', (f.cores || '?') + ' x ' + (f.cpu_model || 'unknown')],
    ['Kernel version', f.kernel || ''],
    ['Operating system', f.os || '-'],
    ['Boot mode', f.boot || ''],
    ['Hardware virtualisation', f.kvm ? 'KVM available'
                                      : 'not available (TCG only)'],
    ['QEMU', f.qemu || 'not found'],
    ['PC-98 BIOS', f.roms || '-'],
    ['Storage root', f.root || '-'],
    ['Python', f.python || '-'],
  ].filter(([, v]) => v);              // what a platform has nothing to say
  if (f.build) rows.unshift(['Build', f.build]);
  if (f.addresses && f.addresses.length)
    rows.unshift(['Addresses', f.addresses.join(', ')]);
  const up = h.uptime == null ? '-' :
    Math.floor(h.uptime / 86400) + 'd ' +
    String(Math.floor(h.uptime % 86400 / 3600)).padStart(2, '0') + ':' +
    String(Math.floor(h.uptime % 3600 / 60)).padStart(2, '0');
  view.innerHTML =
    '<div class="crumb">Host</div>' +
    '<div class="titlerow"><span class="glyph">&#9635;</span>' +
    '<h2>' + esc(f.hostname || 'host') + '</h2>' +
    '<span class="state on">connected</span>' +
    '<span style="flex:1"></span>' +
    '<span class="note">uptime ' + up + '</span></div>' +
    actionBar([
      '<button class="primary" onclick="openCreate()">Create VM</button>',
      '<button onclick="location.hash=\'#/vms\'">Virtual machines</button>',
      '<button onclick="location.hash=\'#/storage\'">Storage</button>',
      f.platform === 'windows' ? '' :
        '<button onclick="location.hash=\'#/network\'">Networking</button>',
      f.platform === 'windows' ? '' :
        '<button onclick="location.hash=\'#/shell\'">Shell</button>',
      '<button onclick="render()">Refresh</button>']) +
    '<div class="grid2" style="grid-template-columns:1fr 22em">' +
    '<div class="card"><h3>Configuration</h3><table>' +
    rows.map(([k, v]) => '<tr><td style="width:15em;color:#8d99a5">' + k +
      '</td><td>' + esc(v) + '</td></tr>').join('') + '</table></div>' +
    '<div class="card"><h3>Resource consumption</h3><div class="body">' +
    meterBar('CPU', cpuPct, 100,
             cpuPct.toFixed(0) + '% of ' + (f.cores || '?') + ' CPUs') +
    meterBar('Memory', h.mem_used || 0, h.mem_total || 1) +
    meterBar('Storage', (h.disk_total || 0) - (h.disk_free || 0),
             h.disk_total || 1) +
    '<div class="note" style="margin-top:.4em">Load average ' +
    (h.load ? h.load.join(', ') : '-') + '<br>Virtual machines: ' +
    running + ' running of ' + instances.length + '</div>' +
    '</div></div></div>' +
    '<div class="card"><h3>Performance</h3><div class="body">' +
    '<div class="graphs">' +
    graph('% CPU', cpuPct.toFixed(0) + '%', history.cpu, 'var(--cpu)', 100,
          ['100','50','0']) +
    graph('Memory', fmtBytes(h.mem_used || 0), history.mem, 'var(--mem)',
          h.mem_total || 1,
          [fmtBytes(h.mem_total || 0), fmtBytes((h.mem_total || 0) / 2),
           '0']) +
    '</div></div></div>';
}

// ------------------------------------------------------ system settings
async function settingsView() {
  const s = await api('/api/storage-config');
  if (!s) return;
  const here = s.chosen.uuid || s.chosen.path || '';
  const rows = s.choices.map(v => {
    const chosen = v.id === here;
    return '<tr><td>' + esc(v.path) + '</td><td>' + esc(v.fstype) +
      '</td><td>' + esc(v.label || '') + '</td><td>' + fmtBytes(v.size) +
      '</td><td class="note">' + esc(v.note || '') +
      '</td><td style="text-align:right">' +
      (chosen ? '<span class="state on">in use for data</span>'
       : '<button onclick="chooseStorage(\'' + esc(jsq(v.id)) + '\',\'' +
         esc(jsq(v.path)) + '\')">Use for data</button>') + '</td></tr>';
  }).join('');
  view.innerHTML =
    '<div class="crumb"><a href="#/">' + esc(facts.hostname || 'host') +
    '</a> &rsaquo; System Settings</div>' +
    '<div class="titlerow"><span class="glyph">&#9881;</span>' +
    '<h2>System Settings</h2></div>' +
    actionBar(['<button onclick="render()">Refresh</button>']) +
    '<div class="card"><h3>Data storage</h3><table>' +
    '<tr><td style="width:12em;color:#8d99a5">In use</td><td>' +
    esc(s.root) + (s.fstype ? ' (' + esc(s.fstype) + ')' : '') +
    (s.on_stick ? ' <span class="note">' + esc(s.default.what) + '</span>'
                : '') +
    '</td></tr>' +
    '<tr><td style="color:#8d99a5">Settings</td><td>' + esc(s.settings) +
    '</td></tr></table>' +
    '<h4>' + (s.platform === 'windows' ? 'Drives' : 'Filesystems') +
    '</h4><table>' +
    '<tr><th>' + (s.platform === 'windows' ? 'Drive' : 'Device') +
    '</th><th>Type</th><th>Label</th><th>Size</th>' +
    '<th>Note</th><th></th></tr>' +
    (rows || '<tr><td colspan="6" class="note">none found</td></tr>') +
    '</table><div class="body">' +
    (s.on_stick ? '' : '<button onclick="chooseStorage(\'\',\'' +
     esc(jsq(s.default.path)) + '\')">Move back to ' +
     esc(s.default.what) + '</button>') +
    '<div class="note" style="margin-top:.5em">' +
    (s.platform === 'windows'
     ? 'A Mirai98 folder is made on the drive that is picked, and only ' +
       'the choice is kept beside the program. FAT32: no file over 4 GB.'
     : 'Machines live at the root of the chosen filesystem. Only the ' +
       'choice is kept on the boot medium. ext4 and exFAT: no limits. ' +
       'NTFS: needs a full Windows shutdown. FAT32: no file over 4 GB.') +
    '</div></div></div>' +
    passwordCard(s) + await persistCard() + await installCard();
}

// ------------------------------------------------------- the password
function passwordCard(s) {
  const win = s.platform === 'windows';
  // An application started this and holds a secret nothing else has, and
  // the server listens on this machine only.  A password would guard
  // nothing, and the server refuses to set one, so there is no card.
  if (facts.token) return '';
  return '<div class="card"><h3>Password</h3><div class="body">' +
    '<div class="note" style="margin-bottom:.6em">' +
    (s.password
     ? (win ? 'Set. This console asks for it.'
            : 'Set. The web console, ssh and the shell all ask for it.')
     : 'Not set. Anyone on this network can drive this host.') +
    '</div><form onsubmit="return savePassword(this)">' +
    (s.password
     ? '<div class="row"><label>Current</label>' +
       '<input type="password" name="current"></div>' : '') +
    '<div class="row"><label>New password</label>' +
    '<input type="password" name="password"></div>' +
    '<div class="row"><label></label><button class="primary">Set</button>' +
    (s.password ? '<button type="button" onclick="clearPassword()">' +
     'Remove the password</button>' : '') + '</div>' +
    '<div class="note">' +
    (win ? 'It guards this console. Windows accounts are left alone.'
         : 'This is root\'s own password, so the shell asks for it too. ' +
           'A live system forgets it at every boot, so its hash is kept ' +
           'on the boot medium and put back at start-up. Changing it ' +
           'from a shell leaves this page out of step.') +
    '</div></form></div></div>';
}
window.savePassword = form => {
  const body = {};
  for (const el of form.elements) if (el.name) body[el.name] = el.value;
  if (!body.password) { toast('type a password'); return false; }
  api('/api/password', {method: 'POST', body: JSON.stringify(body)})
    .then(r => { if (r) { toast('password ' + r.result);
                          task('Root password', r.result); }
                 render(); });
  return false;
};
window.clearPassword = () => {
  const current = prompt('current password');
  if (current === null) return;
  if (!confirm('Remove the password? Anyone on this network could then ' +
               'drive this host.')) return;
  api('/api/password', {method: 'POST',
                        body: JSON.stringify({current, password: ''})})
    .then(r => { if (r) { toast('password cleared'); location.reload(); } });
};

// ----------------------------------- the persistent system image and apt
async function persistCard() {
  if (facts.platform === 'windows') return '';
  const e = await api('/api/extension');
  if (!e) return '';
  const job = e.updates[e.updates.length - 1];
  const canUpdate = e.present && e.active;
  return '<div class="card"><h3>Persistent System Image</h3><table>' +
    '<tr><td style="width:12em;color:#8d99a5">Image</td><td>' +
    (e.present ? esc(e.path) + ' (' + fmtBytes(e.size) + ')'
               : 'none: system changes stay in RAM') + '</td></tr>' +
    '<tr><td style="color:#8d99a5">This boot</td><td>' +
    (e.active ? 'using the image'
              : 'RAM overlay' + (e.present ? ' — reboot to use the image'
                                           : '')) + '</td></tr>' +
    '</table><div class="body"><div class="row">' +
    (e.present
     ? '<button onclick="dropExtension()">Remove the image</button>'
     : '<button class="primary" onclick="makeExtension()">' +
       'Create the image</button>') +
    '<button' + (canUpdate ? '' : ' disabled title="needs the image, ' +
     'and a boot that uses it"') + ' onclick="runUpdate()">' +
    'Update the system</button></div>' +
    '<div class="note">Keeps everything apt installs. ' +
    'The boot menu always has a second entry that ignores it. ' +
    'A new kernel is copied onto the boot medium afterwards.</div>' +
    '</div></div>' +
    (job ? '<div class="card"><h3>Update</h3><div class="body">' +
      '<div class="note">' + esc(job.state) + '</div>' +
      '<pre id="update-out" style="max-height:22em;overflow:auto;' +
      'background:#15181a;border:1px solid var(--line);padding:.6em;' +
      'font-size:12px;white-space:pre-wrap">' +
      esc((job.lines || []).join('\n')) + '</pre></div></div>' : '');
}
window.makeExtension = () => {
  const size = prompt('size of the persistent system image, in MB', '2048');
  if (!size) return;
  toast('making the image...');
  api('/api/extension', {method: 'POST',
                         body: JSON.stringify({action: 'create',
                                               size: parseInt(size, 10)})})
    .then(r => { if (r) { toast(r.result);
                          task('Persistent System Image - create', 'OK'); }
                 render(); });
};
window.dropExtension = () => {
  if (!confirm('Remove the image? Everything installed into it goes ' +
               'with it.')) return;
  api('/api/extension', {method: 'POST',
                         body: JSON.stringify({action: 'remove'})})
    .then(r => { if (r) { toast(r.result);
                          task('Persistent System Image - remove', 'OK'); }
                 render(); });
};
window.runUpdate = () => {
  if (!confirm('Run apt update, upgrade and dist-upgrade now?')) return;
  api('/api/update', {method: 'POST'})
    .then(r => { if (r) { toast('updating');
                          task('System update', 'started');
                          watchUpdate(); } });
};
async function watchUpdate() {
  const e = await api('/api/extension');
  const job = e && e.updates[e.updates.length - 1];
  const box = document.getElementById('update-out');
  if (!job) return;
  if (box) box.textContent = (job.lines || []).join('\n');
  else { await settingsView(); }
  if (box) box.scrollTop = box.scrollHeight;
  if (job.state === 'running') setTimeout(watchUpdate, 2000);
  else { toast('update ' + job.state); render(); }
}
window.chooseStorage = (id, where) => {
  if (!confirm('Keep the machines on ' + where + '?\n\n' +
               'Every machine must be stopped first.')) return;
  const copy = confirm('Copy what is here now to that drive?');
  api('/api/storage-config',
      {method: 'POST', body: JSON.stringify({storage: id, copy})})
    .then(r => { if (r) { toast(r.result);
                          task('Data storage → ' + where, 'saved'); }
                 render(); });
};

// ---------------------------------- install to a disk, inside settings
let installRunning = false;
async function installCard() {
  if (facts.platform === 'windows') return '';
  const d = await api('/api/install');
  if (!d) return '';
  const job = d.jobs[d.jobs.length - 1];
  installRunning = !!job && job.state === 'running';
  const rows = d.targets.map(t =>
    '<tr><td>' + esc(t.path) + '</td><td>' + esc(t.size) + '</td>' +
    '<td class="note">' + esc(t.model || '') +
    (t.removable ? ' (removable)' : '') + '</td>' +
    '<td>' + (t.busy ? '<span class="note">in use: ' + esc(t.busy) +
                       '</span>' : 'free') + '</td>' +
    '<td style="text-align:right">' +
    (t.busy ? '' : '<button onclick="installTo(\'' + t.path +
     '\')">Install here</button>') + '</td></tr>').join('');
  return '<div class="card"><h3>Install to a Disk</h3><table>' +
    '<tr><th>Device</th><th>Size</th><th>Model</th><th>State</th>' +
    '<th></th></tr>' +
    (rows || '<tr><td colspan="5" class="note">no disks</td></tr>') +
    '</table><div class="body">' +
    (job
     ? '<div>' + esc(job.message || job.state) + '</div>' +
       '<progress max="' + job.total + '" value="' + job.done +
       '" style="width:100%;height:1.1em;margin:.6em 0"></progress>' +
       (job.error ? '<div style="color:#e06c5f">' + esc(job.error) +
                    '</div>' : '')
     : '') +
    '<div class="note">One disk, one system. The disk is wiped and laid ' +
    'out like the stick: a system partition and the rest as data. BIOS ' +
    'and UEFI are both installed. ' +
    (d.medium ? '' : 'The live medium is missing: install is not ' +
                     'possible from this boot.') +
    '</div></div></div>';
}
window.installTo = device => {
  if (!confirm('Everything on ' + device + ' will be destroyed.\n\n' +
               'Install Mirai98 there?')) return;
  const copy = confirm('Copy the machines and images from this system to ' +
                       'the new disk as well?');
  api('/api/install', {method: 'POST',
                       body: JSON.stringify({device, copy_data: copy})})
    .then(r => { if (r) { toast('installing to ' + device);
                          task('Install to ' + device, 'started'); }
                 settingsView(); });
};

// ------------------------------------------------------------ log view
let logKind = '';
async function logView() {
  const data = await api('/api/log' + (logKind ? '?kind=' + logKind : ''));
  if (!data) return;
  const html = '<pre style="margin:0;white-space:pre-wrap;' +
    'overflow-wrap:anywhere">' + esc(data.lines.join('')) + '</pre>';
  const box = document.getElementById('log-box');
  if (box && document.getElementById('log-count')) {
    box.innerHTML = html;
    document.getElementById('log-count').textContent =
      data.lines.length + ' lines';
    return;
  }
  view.innerHTML = '<div class="topbar"><h2>System log</h2>' +
    '<span class="note" id="log-count">' + data.lines.length +
    ' lines</span><span style="flex:1"></span>' +
    ['', ...data.kinds].map(k =>
      '<button onclick="logFilter(\'' + k + '\')"' +
      (k === logKind ? ' class="primary"' : '') + '>' +
      (k || 'all') + '</button>').join('') +
    '</div><div class="card"><div class="body" id="log-box" ' +
    'style="max-height:68vh;overflow:auto;font-family:ui-monospace,' +
    'monospace;font-size:12px">' + html + '</div></div>' +
    '<div class="note">Cleared at boot. Trimmed to 1000 lines at 1 MB.' +
    '</div>';
  const b = document.getElementById('log-box');
  b.scrollTop = b.scrollHeight;
}
window.logFilter = kind => { logKind = kind;
                             view.innerHTML = ''; logView(); };

// -------------------------------------------------------- network view
async function networkView() {
  const n = await api('/api/network');
  if (!n) return;
  const c = n.config;
  view.innerHTML =
    '<div class="topbar"><h2>Network</h2>' +
    '<span class="note">the hypervisor\'s own address</span></div>' +
    '<div class="card"><h3>Interfaces</h3><table>' +
    '<tr><th>Name</th><th>State</th><th>Addresses</th><th>MAC</th></tr>' +
    (n.interfaces.map(i => '<tr><td>' + esc(i.name) + '</td>' +
      '<td><span class="state' + (i.state === 'UP' ? ' on' : '') + '">' +
      esc(i.state.toLowerCase()) + '</span></td><td>' +
      esc(i.addresses.join(', ') || '-') + '</td><td class="note">' +
      esc(i.mac) + '</td></tr>').join('') ||
     '<tr><td colspan="4" class="note">no interfaces found</td></tr>') +
    '</table></div>' +
    '<div class="card"><h3>Address</h3><div class="body">' +
    '<form onsubmit="return saveNetwork(this)">' +
    '<div class="row"><label>LAN bridge</label>' +
    '<label class="check"><input type="checkbox" name="bridge"' +
    (c.bridge === 'off' ? '' : ' checked') + '> put the wired interface ' +
    'into ' + esc(n.bridge.name) + ' so guests can sit on the LAN' +
    '</label></div>' +
    '<div class="row"><label></label><span class="note">' +
    (n.bridge.exists
     ? esc(n.bridge.name) + ' is up with ' +
       (n.bridge.members.join(', ') || 'no members yet')
     : esc(n.bridge.name) + ' does not exist yet') + '</span></div>' +
    '<div class="row"><label>Mode</label><select name="mode" ' +
    'onchange="netMode(this.value)">' +
    '<option value="dhcp"' + (c.mode === 'static' ? '' : ' selected') +
    '>DHCP</option><option value="static"' +
    (c.mode === 'static' ? ' selected' : '') + '>Static</option>' +
    '</select></div>' +
    '<div id="static-rows" style="display:' +
    (c.mode === 'static' ? '' : 'none') + '">' +
    '<div class="row"><label>Address</label>' +
    '<input type="text" name="address" value="' + esc(c.address) +
    '" placeholder="10.0.10.50/24"></div>' +
    '<div class="row"><label>Gateway</label>' +
    '<input type="text" name="gateway" value="' + esc(c.gateway) +
    '" placeholder="10.0.10.1"></div>' +
    '<div class="row"><label>Name servers</label>' +
    '<input type="text" name="dns" value="' + esc(c.dns) +
    '" placeholder="10.0.10.1 1.1.1.1"></div></div>' +
    '<div class="row"><label></label><button class="primary">Save and ' +
    'apply</button></div>' +
    '<div class="note">Saved on the data partition. Changing the ' +
    'address moves this page.</div></form></div></div>';
}
window.netMode = mode => {
  document.getElementById('static-rows').style.display =
    mode === 'static' ? '' : 'none';
};
window.saveNetwork = form => {
  const conf = {};
  for (const el of form.elements)
    if (el.name) conf[el.name] = el.type === 'checkbox' ? el.checked
                                                        : el.value.trim();
  if (conf.mode === 'static' &&
      !confirm('Move this host to ' + conf.address + '?\n\n' +
               'The page will stop answering here.')) return false;
  api('/api/network', {method: 'PATCH', body: JSON.stringify(conf)})
    .then(r => { if (r) { toast(r.result);
                          task('Host network - ' + conf.mode, 'saved'); } });
  return false;
};

// the shell is ttyd's own page, framed so it stays inside the console
async function shellView() {
  view.innerHTML = '<div class="topbar"><h2>Shell</h2>' +
    '<span class="note">the hypervisor host, not a guest</span>' +
    '<span style="flex:1"></span>' +
    '<button onclick="window.open(shellUrl())">Open in a tab</button>' +
    '</div><div class="card"><iframe id="shell-frame" src="' + shellUrl() +
    '" style="width:100%;height:72vh;border:0;background:#000"></iframe>' +
    '</div>';
}
window.shellUrl = () => 'http://' + location.hostname + ':7681/';

function route() {
  if (location.hash === '#/storage') return {view: 'storage'};
  if (location.hash === '#/shell') return {view: 'shell'};
  if (location.hash === '#/network') return {view: 'network'};
  if (location.hash === '#/log') return {view: 'log'};
  // install used to be its own page; it is a card in settings now
  if (location.hash === '#/install' ||
      location.hash === '#/settings') return {view: 'settings'};
  const disk = location.hash.match(
    /^#\/disk\/(hdd|fdd|cdrom)\/(.+)$/);
  if (disk) return {view: 'disk', kind: disk[1],
                    name: decodeURIComponent(disk[2])};
  if (location.hash === '#/vms') return {view: 'list'};
  const m = location.hash.match(/^#\/vm\/([A-Za-z0-9_-]+)$/);
  return m ? {view: 'vm', name: m[1]} : {view: 'system'};
}
async function render() {
  await refreshFleet();
  await refreshDisks();
  await pollHost();
  const r = route();
  drawTree();
  if (r.view === 'vm') { await detailView(r.name); return; }
  disconnectConsole();
  if (r.view === 'disk') await diskView(r.kind, r.name);
  else if (r.view === 'storage') await storageView();
  else if (r.view === 'shell') await shellView();
  else if (r.view === 'network') await networkView();
  else if (r.view === 'log') await logView();
  else if (r.view === 'settings') await settingsView();
  else if (r.view === 'list') await listView(true);
  else await systemView();
}
window.addEventListener('hashchange', () => { disconnectConsole();
                                              render(); });
document.getElementById('whereami').textContent = location.host;
task('Mirai98 console opened', 'OK');

// the browser remembers its own choice; a browser that has never been
// here follows the one made during setup
async function startLang() {
  try { lang = localStorage.getItem('mirai98-lang') || ''; } catch (err) {}
  if (!lang) {
    const f = await api('/api/facts');
    lang = (f && f.lang) || 'en';
    try { localStorage.setItem('mirai98-lang', lang); } catch (err) {}
  }
  document.documentElement.lang = lang;
  document.getElementById('lang-pick').value = lang;
  if (lang === 'ja') translateNode(document.body);
}
// Where a fresh window lands.  On Windows the disks are the point, so it
// opens on Storage; on the appliance the host overview is.  A window
// opened with an address of its own keeps it.
async function start() {
  await startLang();
  if (!location.hash || location.hash === '#/') {
    const f = facts.platform ? facts : await api('/api/facts');
    if (f && f.platform === 'windows') {
      location.hash = '#/storage';
      return;                          // the hashchange draws it
    }
  }
  render();
}
start();
setInterval(() => {
  const r = route();
  pollHost();
  if (r.view === 'list') listView();
  else if (r.view === 'vm') updateUsage(r.name);
  else if (r.view === 'system') systemView();
  else if (r.view === 'storage') pollJobs();
  else if (r.view === 'log') logView();
  else if (r.view === 'settings' && installRunning) settingsView();
}, 3000);
