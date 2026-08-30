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
  { machines: [], defaults: {}, badge: {}, console: [], editForm: {},
    hardware: {}, wizard: {}, diskFormats: {}, labels: {}, actions: {} };
window.registerMachinePlugin = (p) => {
  const P = window.MiraiPlugins;
  P.consolePrep = P.consolePrep || [];
  (p.machines || []).forEach(m => { if (!P.machines.includes(m)) P.machines.push(m); });
  Object.assign(P.defaults, p.defaults || {});
  Object.assign(P.badge, p.badge || {});
  Object.assign(P.editForm, p.editForm || {});   // per-machine hardware form
  P.hardware = P.hardware || {};
  Object.assign(P.hardware, p.hardware || {});   // per-machine hardware view
  P.wizard = P.wizard || {};
  Object.assign(P.wizard, p.wizard || {});       // per-machine create wizard
  P.actions = P.actions || {};
  Object.assign(P.actions, p.actions || {});     // extra verbs on the detail
  P.labels = P.labels || {};
  Object.assign(P.labels, p.labels || {});       // machine id -> shown name
  P.diskFormats = P.diskFormats || {};           // Storage "Create" formats
  for (const kind of Object.keys(p.diskFormats || {})) {
    P.diskFormats[kind] = (P.diskFormats[kind] || []).concat(p.diskFormats[kind]);
  }
  if (p.console) P.console.push(p.console);
  if (p.consolePrep) P.consolePrep.push(p.consolePrep);
  lastList = '';   // a new machine/badge must invalidate the cached VM list
};
// the machine ids offered in the create form: core PC-98 plus any plugin
const machineList = () => ['pc9821', 'pc9801'].concat(window.MiraiPlugins.machines);
// how a machine id reads in a select, at the PC-98 entries' grain
const MACHINE_LABELS = { pc9821: 'PC-9821 (386 and later)',
                         pc9801: 'PC-9801 (the older line)' };
const machineLabel = m => window.MiraiPlugins.labels[m] || MACHINE_LABELS[m] || m;
// a machine's cell in the list, with a plugin badge if it registered one
const machineBadge = (m) => {
  const b = window.MiraiPlugins.badge[m || 'pc9821'];
  return b ? ' <span style="display:inline-block;padding:0 .4em;' +
    'border-radius:.3em;background:#7a3cff;color:#fff;font-size:.8em;' +
    'vertical-align:middle">' + esc(b) + '</span>' : '';
};
const machineCell = (m) => esc(m || 'pc9821') + machineBadge(m);
// apply a plugin machine's create defaults when the type changes (a core
// PC-98 machine just re-enables the board and BIOS choices)
window.applyMachineDefaults = (sel) => {
  const form = sel.form;
  const d = window.MiraiPlugins.defaults[sel.value];
  // in the create wizard a plugin machine may replace or hide whole tabs
  if (form.id === 'wizard') applyWizardMachine(sel.value);
  if (form.sound) form.sound.disabled = !!(d && d.lockSound);
  if (form.bios) form.bios.disabled = !!(d && d.lockBios);
  // a locked choice says what the machine really has instead (the plugin's
  // hardware description), so the greyed "None" is not read as no sound
  const hw = window.MiraiPlugins.hardware[sel.value];
  const desc = hw ? hw({machine: sel.value}, { esc }) : {};
  for (const [field, text] of [['sound', desc.sound], ['bios', desc.bios]]) {
    const el = form[field];
    if (!el) continue;
    let note = el.parentNode.querySelector('.plugin-fixed');
    if (text) {
      if (!note) {
        note = document.createElement('span');
        note.className = 'note plugin-fixed';
        el.insertAdjacentElement('afterend', note);
      }
      note.textContent = ' ' + text;
      el.style.display = 'none';
    } else {
      if (note) note.remove();
      el.style.display = '';
    }
  }
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
// the MIDI board, and what plays what it is sent (see pc98web.py)
const MIDI_LABEL = {"": "None", "synth": "MPU-PC98II + SoundFont"};
const midiName = v => MIDI_LABEL[v] || MIDI_LABEL[""];
const DISK_ROWS = [["hdd1","IDE HDD 1","hdd"], ["hdd2","IDE HDD 2","hdd"],
                   ["cd","IDE CD-ROM","cdrom"], ["fdd1","FDD 1","fdd"],
                   ["fdd2","FDD 2","fdd"], ["scsi1","SCSI 1","hdd"],
                   ["scsi2","SCSI 2","hdd"], ["scsi3","SCSI 3","hdd"],
                   ["scsi4","SCSI 4","hdd"]];
const TABS = ["General","Disks","Host","Memory","Sound","Network",
              "Options","Confirm"];
const view = document.getElementById('view');
let rfb = null, consoleWatch = null, consoleFbWatch = null;
let consolePointerStop = null;
let imeKeyStop = null;
let catalog = {hdd:[], fdd:[], cdrom:[]};
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
JA['filter'] = '絞り込み';
JA['narrow the list of images'] = 'イメージ一覧を絞り込む';
JA['to hdi'] = 'hdiへ';
JA['to qcow2'] = 'qcow2へ';
JA['to raw'] = 'rawへ';
JA['to nfd'] = 'nfdへ';
JA['to fdi'] = 'fdiへ';
JA['The extension is added for you. Anex86 .hdi cannot be made here: upload one and it is converted.'] =
  '拡張子はこちらで付けます。Anex86 の .hdi はここでは作れません。' +
  '取り込めば変換されます。';
JA['PC-98 (FAT, flat)'] = 'PC-98 (FAT・ベタ)';
JA['PC-98 (FAT, grows on demand)'] = 'PC-98 (FAT・必要に応じて拡大)';
JA['The extension is added for you. Formatted, empty. A name already '
   + 'ending in .raw, .img, .fdi, .nfd or .d88 keeps it.'] =
  '拡張子はこちらで付けます。フォーマット済みの空ディスクです。'
  + '.raw / .img / .fdi / .nfd / .d88 で終わる名前はそのまま使います。';
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
  [/^(\d+) of (\d+) shown$/, '$2 件中 $1 件'],
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
// What goes inside a single-quoted JS string in an onclick: a Windows
// path is full of backslashes, which the string literal would eat, and a
// name put on the shelf by hand may hold an apostrophe, which would end
// the literal early.  Wrap this in esc() as well -- the attribute has to
// survive the HTML parser before the string reaches JS.
const jsq = s => String(s ?? '').replace(/\\/g, '\\\\')
                                .replace(/'/g, "\\'");
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

async function apiResult(path, opts) {
  // every caller is written for "data, or null"; a fetch that rejects --
  // the server restarting, the tunnel dropping -- used to reach none of
  // them and leave the screen sitting on whatever it last said
  let r;
  try {
    r = await fetch(path, opts);
  } catch (err) {
    return {ok: false, data: null, error: 'no answer from the server'};
  }
  const data = await r.json().catch(() => null);
  if (!r.ok) {
    return {ok: false, data: null,
            error: (data && data.error) || r.statusText || 'failed'};
  }
  return {ok: true, data: data === null ? {} : data, error: ''};
}
async function api(path, opts) {
  const got = await apiResult(path, opts);
  if (!got.ok) { toast(got.error); return null; }
  return got.data;
}
/*
 * A machine plugin is loaded as its own module and its buttons call back
 * through onclick, i.e. from the global scope.  These two are the verbs
 * they need; everything else they are handed.
 */
window.api = (path, opts) => api(path, opts);
window.task = (what, status) => task(what, status);
// plugins add their own phrases to this; it is theirs to reach as well
window.JA = JA;

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
    // a plugin machine carries its badge here too, as in the list
    instances.map(i => link('#/vm/' + i.name,
        '<span class="dot"></span>' + esc(i.name) + machineBadge(i.machine),
        i.running ? 'run' : '')).join('');
}

// extra verbs from a machine plugin, and never an exception from one
function pluginActions(i) {
  const fn = window.MiraiPlugins.actions[i.machine];
  if (!fn) {
    return [];
  }
  try {
    const out = fn(i, { esc });
    return Array.isArray(out) ? out.filter(b => typeof b === 'string') : [];
  } catch (err) {
    console.error('machine plugin actions failed', err);
    return [];
  }
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
// The images of a kind as <option>s: the groups first, each in an
// <optgroup> of its own, then whatever is in none.  Picking one disc out
// of two dozen is what groups are for, so every place that offers them
// offers them arranged the same way.
function diskOptions(files, value) {
  const groups = [...new Set(files.map(f => f.group || ''))]
    .filter(Boolean).sort();
  const opt = f => '<option' + (f.name === value ? ' selected' : '') + '>' +
                   esc(f.name) + '</option>';
  return groups.map(g => '<optgroup label="' + esc(g) + '">' +
      files.filter(f => (f.group || '') === g).map(opt).join('') +
      '</optgroup>').join('') +
    files.filter(f => !f.group).map(opt).join('');
}

// The inside of a disk <select>, drawn from scratch again every time the
// filter box in front of it is typed into.  o.drives offers the host's own
// drives after the images, o.orphans keeps the ones whose file has gone
// missing, o.empty names the first entry, o.filter narrows the rest.
function diskBody(kind, value, o) {
  o = o || {};
  const q = (o.filter || '').trim().toLowerCase();
  // whatever is already picked survives the filter: a narrowed list that
  // dropped the pick would quietly change what the form submits
  const hit = (name, text) =>
    name === value || !q || text.toLowerCase().includes(q);
  const all = (catalog[kind] || []).filter(f => o.orphans || !f.orphan);
  const files = all.filter(f => hit(f.name, f.name + ' ' + (f.group || '')));
  // real drives of the matching sort come after the images, so a guest
  // can be pointed at the host's own CD or floppy
  const allDrives = o.drives ? (hardware.drives || []).filter(
    d => d.type === kind) : [];
  const drives = allDrives.filter(
    d => hit(d.path, d.path + ' ' + (d.model || '')));
  const known = all.some(f => f.name === value) ||
                allDrives.some(d => d.path === value);
  return '<option value="">' + esc(o.empty || '(none)') + '</option>' +
    diskOptions(files, value) +
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
    (value && !known ? '<option selected>' + esc(value) + '</option>' : '');
}

// how many images the filter left, read the same way the Storage filter
// reads a shelf: the name and the group it sits in
function diskCount(kind, o) {
  const q = (o.filter || '').trim().toLowerCase();
  if (!q) return '';
  const all = (catalog[kind] || []).filter(f => o.orphans || !f.orphan);
  const shown = all.filter(f => (f.name + ' ' + (f.group || ''))
                                .toLowerCase().includes(q)).length;
  return shown + ' of ' + all.length + ' shown';
}

// A shelf outgrows a list long before it outgrows the disk: groups thin a
// couple of dozen images down, a few typed letters thin any number of them.
// The box carries what its list is made of, so a keystroke can rebuild it.
// o.attrs goes on the <select>, o.box on the filter itself.
function diskPicker(kind, value, o) {
  o = o || {};
  return '<input type="text" class="disk-filter" placeholder="filter" ' +
    'title="narrow the list of images" oninput="filterDiskSelect(this)" ' +
    'value="' + esc(o.filter || '') + '" data-kind="' + esc(kind) + '"' +
    (o.drives ? ' data-drives="1"' : '') +
    (o.orphans ? ' data-orphans="1"' : '') +
    ' data-empty="' + esc(o.empty || '(none)') + '"' + (o.box || '') + '>' +
    '<select ' + (o.attrs || '') + '>' + diskBody(kind, value, o) +
    '</select><span class="note disk-count">' + esc(diskCount(kind, o)) +
    '</span>';
}

window.filterDiskSelect = (box) => {
  const sel = box.nextElementSibling;
  if (!sel || sel.tagName !== 'SELECT') return;
  const d = box.dataset;
  const o = {filter: box.value, drives: !!d.drives, orphans: !!d.orphans,
             empty: d.empty};
  const value = sel.value;
  sel.innerHTML = diskBody(d.kind, value, o);
  sel.value = value;          // redrawing the list must not move the pick
  const note = sel.nextElementSibling;
  if (note && note.classList.contains('disk-count'))
    note.textContent = diskCount(d.kind, o);
};

function diskSelect(key, kind, value, name) {
  return diskPicker(kind, value,
                    {drives: true, attrs: 'name="' + (name || key) + '"'});
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
  // a machine plugin may describe its own hardware (what its emulation
  // actually wires); otherwise the stock PC-98 rows below are shown
  const custom = window.MiraiPlugins.hardware[i.machine];
  if (custom) return hardwareRows(custom(i, { esc }).rows);
  const rows = [['&#9636; Memory', esc(i.memory)],
                ['&#9881; Machine', esc(i.machine || 'pc9821') + ' (' +
                 (i.accel === 'tcg' ? 'TCG'
                  : 'KVM (Experimental)') + ')'],
                ['&#9750; BIOS', 'compatible'],
                ['&#9636; Font', i.font === 'real' ? 'real machine ROM'
                                                   : 'compatible'],
                ['&#9834; Sound', soundName(i.sound)],
                ['&#9834; MIDI', midiName(i.midi)],
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
  return hardwareRows(rows);
}
function hardwareRows(rows) {
  return '<table>' + rows.map(([k, v]) =>
    '<tr><td style="width:13em;color:#8b9298">' + k + '</td><td>' + v +
    '</td></tr>').join('') + '</table>';
}
// the one-line BIOS summary; a machine plugin may name its own ROM set
function biosLabel(i) {
  const custom = window.MiraiPlugins.hardware[i.machine];
  const b = custom && custom(i, { esc }).bios;
  return b || 'compatible';
}

function editForm(i) {
  // a machine plugin may supply its own hardware form (given the helpers it
  // needs); otherwise the stock PC-98 form below is used
  const custom = window.MiraiPlugins.editForm[i.machine];
  if (custom) {
    return custom(i, { esc, diskSelect, machineList, machineLabel, MEMS });
  }
  return '<form onsubmit="return saveVm(this,\'' + i.name + '\')">' +
    DISK_ROWS.map(([k, label, kind]) =>
      '<div class="row"><label>' + label + '</label>' +
      diskSelect(k, kind, i[k]) + '</div>').join('') +
    '<div class="row"><label>Machine type</label>' +
    '<select name="machine" onchange="applyMachineDefaults(this)">' +
    machineList().map(m => '<option value="' + m + '"' +
      ((i.machine || 'pc9821') === m ? ' selected' : '') + '>' +
      esc(machineLabel(m)) + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Shared folder</label>' +
    '<input type="text" name="mount" value="' + esc(i.mount || '') +
    '" placeholder="/data/share"> <span class="note">appears as an IDE ' +
    'disk (fat98)</span></div>' +
    portRows(i) +
    '<div class="row"><label>Memory</label><select name="memory">' +
    MEMS.map(m => '<option' + (i.memory === m ? ' selected' : '') + '>' +
             m + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>Sound</label><select name="sound">' +
    ['86','wss','none'].map(s => '<option value="' + s + '"' +
      (soundKey(i.sound) === s ? ' selected' : '') + '>' +
      soundName(s) + '</option>').join('') + '</select></div>' +
    '<div class="row"><label>MIDI</label><select name="midi">' +
    ['', 'synth'].map(v => '<option value="' + v + '"' +
      ((i.midi || '') === v ? ' selected' : '') + '>' + midiName(v) +
      '</option>').join('') + '</select> <span class="note">a board at ' +
    'I/O E0D0h; what it is sent is played through a SoundFont and mixed ' +
    'into the machine sound</span></div>' +
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
window.renameVm = async (name) => {
  const to = prompt('New name (1-32 letters, digits, - or _)', name);
  if (!to || to === name) return;
  const i = await api('/api/instances/' + encodeURIComponent(name));
  if (!i) return;
  i.name = to;
  const d = await api('/api/instances/' + encodeURIComponent(name),
                      {method: 'PUT', body: JSON.stringify(i)});
  if (d) { toast('renamed'); location.hash = '#/vm/' + to; }
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
// QEMU's VNC server streams the guest's PCM over the same WebSocket as the
// pixels (S16LE, stereo, 44.1 kHz).  Playback lives in an AudioWorklet: its
// process() runs on the audio thread, so a busy main thread -- noVNC
// decoding a burst of framebuffer updates while a game loads, say -- cannot
// starve it.  The main thread only hands the arriving chunks over.
const AUDIO_RATE = 44100;
const AUDIO_PREFILL = 0.12;        // seconds of cushion before playback starts
const AUDIO_MAX_LAG = 0.40;        // seconds; give back anything beyond this

// The worklet keeps its own ring: the main thread posts PCM in, the audio
// thread takes it out a render quantum at a time.
const AUDIO_WORKLET_SRC = `
class Pc98Sink extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opt = options.processorOptions;
    this.size = Math.round(sampleRate);          // one second of room
    this.l = new Float32Array(this.size);
    this.r = new Float32Array(this.size);
    this.w = 0;
    this.rd = 0;
    this.prefill = Math.round(sampleRate * opt.prefill);
    this.maxLag = Math.round(sampleRate * opt.maxLag);
    this.primed = false;
    this.starved = 0;
    this.dry = 0;
    this.told = 0;
    this.port.onmessage = (e) => this.push(new Int16Array(e.data));
  }
  avail() {
    return (this.w - this.rd + this.size) % this.size;
  }
  push(pcm) {
    const frames = pcm.length >> 1;
    for (let i = 0; i < frames; i++) {
      this.l[this.w] = pcm[i * 2] / 32768;
      this.r[this.w] = pcm[i * 2 + 1] / 32768;
      this.w = (this.w + 1) % this.size;
      if (this.w === this.rd) { this.rd = (this.rd + 1) % this.size; }
    }
    // Server and sound card never tick at exactly the same rate, so the
    // backlog creeps.  Come back to the cushion -- stopping at maxLag
    // would make the worst case the resting state, and one stall would
    // leave the sound a full 0.4 s behind for good.
    if (this.avail() > this.maxLag) {
      this.rd = (this.rd + this.avail() - this.prefill) % this.size;
    }
  }
  process(inputs, outputs) {
    const out = outputs[0];
    const oL = out[0];
    const oR = out[1];
    if (!this.primed) {
      if (this.avail() < this.prefill) { return true; }
      this.primed = true;
    }
    for (let i = 0; i < oL.length; i++) {
      if (this.rd === this.w) {
        // Ran dry: fill in silence and carry on.  Waiting for a fresh
        // cushion here would turn every hiccup into a long gap.
        oL[i] = 0;
        oR[i] = 0;
        this.starved++;
        this.dry++;
      } else {
        oL[i] = this.l[this.rd];
        oR[i] = this.r[this.rd];
        this.rd = (this.rd + 1) % this.size;
        this.dry = 0;
      }
    }
    if (this.dry > sampleRate / 2) {
      // nothing has arrived for half a second: the stream has stopped
      // rather than stumbled, so stop calling it starvation and take the
      // cushion again when it comes back
      this.primed = false;
      this.starved = 0;
      this.dry = 0;
    }
    if (this.starved && currentTime - this.told >= 1) {
      this.told = currentTime;
      this.port.postMessage({starved: this.starved});
      this.starved = 0;
    }
    return true;
  }
}
registerProcessor('pc98-sink', Pc98Sink);
`;

let audioCtx = null, audioNode = null, audioOn = false;
// One attempt at a time: two quick clicks used to load the worklet module
// twice on one context and leave a second node connected but never fed.
let audioPending = null;
// 1-3 bytes of a frame that a chunk boundary cut in half
let audioCarry = null;

async function audioStart() {
  if (audioNode) return;
  if (audioPending) { await audioPending; return; }
  if (!window.AudioWorkletNode) {
    toast('this browser has no AudioWorklet: no console sound');
    return;
  }
  audioPending = (async () => {
    if (!audioCtx) audioCtx = new AudioContext({sampleRate: AUDIO_RATE});
    const url = URL.createObjectURL(
      new Blob([AUDIO_WORKLET_SRC], {type: 'application/javascript'}));
    try {
      await audioCtx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }
    if (audioNode) return;            // something finished this while we waited
    const node = new AudioWorkletNode(audioCtx, 'pc98-sink', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [2],
      processorOptions: {prefill: AUDIO_PREFILL, maxLag: AUDIO_MAX_LAG}
    });
    node.port.onmessage = (e) => {
      if (e.data && e.data.starved) {
        console.warn('console sound: filled in ' + e.data.starved +
                     ' frames of silence');
      }
    };
    node.connect(audioCtx.destination);
    audioNode = node;
  })();
  try {
    await audioPending;
  } catch (err) {
    // the context, the module, the node and the connection can each fail;
    // saying nothing left the button looking dead
    console.error('console sound', err);
    toast('console sound would not start: ' + (err && err.name || err));
    audioOn = false;
  } finally {
    audioPending = null;
  }
}
function audioChunk(bytes) {
  // this runs inside noVNC's own dispatcher, which does not guard its
  // listeners: a throw here would abort the message drain mid-message and
  // take the console down with the sound
  try {
    if (!audioNode) return;
    let data = bytes;
    if (audioCarry && audioCarry.length) {
      data = new Uint8Array(audioCarry.length + bytes.byteLength);
      data.set(audioCarry, 0);
      data.set(bytes, audioCarry.length);
    }
    const whole = data.byteLength & ~3;         // whole stereo frames only
    // a boundary that cut a frame in half is carried, not dropped: losing
    // the odd bytes would cross the channels from there on
    audioCarry = whole < data.byteLength ? data.slice(whole) : null;
    if (!whole) return;
    const copy = data.slice(0, whole);          // own it, then hand it over
    audioNode.port.postMessage(copy.buffer, [copy.buffer]);
  } catch (err) {
    console.error('console sound', err);
  }
}
async function enableAudioNow() {
  if (!rfb || !rfb.enableAudio) return;
  audioOn = true;
  await audioStart();
  if (!audioNode || !audioOn) return;   // turned off while it was loading
  audioCtx.resume();
  rfb.enableAudio(3, 2, AUDIO_RATE);          // 3 = S16
  audioOn = true;
  const btn = document.getElementById('btn-audio');
  if (btn) { btn.textContent = '\u{1F50A} Sound on'; }
}
window.toggleAudio = async () => {
  if (!rfb || !rfb.enableAudio) { toast('no console'); return; }
  audioOn = !audioOn;
  const btn = document.getElementById('btn-audio');
  if (audioOn) {
    await audioStart();
    if (!audioNode || !audioOn) { return; }   // clicked off while loading
    audioCtx.resume();
    rfb.enableAudio(3, 2, AUDIO_RATE);          // 3 = S16
    if (btn) { btn.textContent = '🔊 Sound on'; }
    toast('sound on');
  } else {
    rfb.disableAudio();
    // and actually stop: leaving the worklet connected kept it reporting
    // the silence it was filling in, once a second, for good
    stopAudio();
    audioOn = false;
    if (btn) { btn.textContent = '🔇 Sound off'; }
    toast('sound off');
  }
};
function stopAudio() {
  audioOn = false;
  audioCarry = null;
  if (audioNode) {
    try { audioNode.port.onmessage = null; audioNode.disconnect(); } catch (e) {}
    audioNode = null;
  }
  if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
}

/*
 * How tall the console box comes up.  It used to be a flat 480px, which
 * is a 640x400 screen at 1.2 pixels a line -- fine for a PC-98, but an
 * FM TOWNS switching to 1024x768 was then squeezed into two thirds of
 * its own size.  So keep the 1.2 and apply it to whatever the machine is
 * showing now, and never ask for more height than the pane is wide
 * enough to use: past that point a taller box only adds black bands
 * above and below the picture.
 */
const CONSOLE_LINE_ZOOM = 480 / 400;

function fitConsoleBox() {
  const box = document.getElementById('console-box');
  const canvas = box && box.querySelector('canvas');
  if (!canvas || !canvas.width || !canvas.height) return;
  const wide = box.clientWidth;
  if (!wide) return;                 // a hidden pane has no width to fit to
  const want = Math.round(Math.min(canvas.height * CONSOLE_LINE_ZOOM,
                                   canvas.height * wide / canvas.width));
  if (want > 0 && Math.abs(want - box.clientHeight) > 1) {
    box.style.height = want + 'px';
  }
}

// --- relative-pointer capture ---------------------------------------------
// QEMU's VNC turns every absolute pointer event into a delta from the last
// one, which stalls at the canvas edge and drifts as soon as the guest
// applies its own pointer acceleration.  Every machine this manager runs
// has a relative mouse (the PC-98 bus mouse, the FM TOWNS mouse), so there
// is no absolute path worth keeping: we ask for QEMU's relative branch
// (pseudo-encoding -257) and, while the pointer is locked, feed each host
// movement as a delta around 0x7FFF.
// The -257 request has to ride in the handshake's initial client-encodings
// message, so the patch goes on before an RFB object ever exists.
function patchRFBForRelativePointer() {
  if (!RFB || RFB.messages._miraiRelPatched) return;
  const orig = RFB.messages.clientEncodings;
  RFB.messages.clientEncodings = function (sock, encodings) {
    if (!encodings.includes(-257)) encodings = encodings.concat([-257]);
    return orig.call(this, sock, encodings);
  };
  const origHandleRect = RFB.prototype._handleRect;
  RFB.prototype._handleRect = function () {
    if (this._FBU.encoding === -257) return true;   // pointer-type-change: no data
    return origHandleRect.call(this);
  };
  RFB.messages._miraiRelPatched = true;
}

// A Mac's JIS keyboard has 英数 and かな where a PC/AT one has the Alt keys,
// and the browser reports them as code "Lang2" and "Lang1".  noVNC's scan-code
// table maps those two to the Korean Hanja/Hangeul codes (0x71/0x72), which
// the PC-98 keyboard has no key for, so they arrive at the guest as nothing.
// The PC-98 keys a Japanese typist wants there are 無変換 (NFER) and 変換
// (XFER); QEMU already translates AT set-1 0x7b and 0x79 into those, so the
// whole fix is to point the two entries at the codes that do land.
let xtScancodes = null;
async function patchXtScancodesForJIS() {
  if (xtScancodes) return;
  xtScancodes = (await import('/novnc/core/input/xtscancodes.js')).default;
  xtScancodes['Lang2'] = 0x7b;   // 英数 -> 無変換 (NFER)
  xtScancodes['Lang1'] = 0x79;   // かな -> 変換   (XFER)
}

// The two keys above land on the guest, but on a PC-98 they are not what a
// Mac typist means by them.  Measured on Windows 98 with MS-IME: 変換 on its
// own does nothing at all, 無変換 walks the input mode round (hiragana ->
// full-width katakana -> ...), Shift+無変換 goes to direct input, and
// Ctrl+変換 turns the IME on or off.  Only Shift+無変換 names a state
// outright; the rest move relative to wherever the IME already was.
//
// So the かな key drops the IME into direct input first and only then turns
// it on: both keys then land on the same state however often they are
// pressed.  A combination is sent as the DOM code noVNC wants, because
// sendKey() uses the code for nothing but the XtScancode[] lookup, and the
// two entries patched above already carry 変換 and 無変換.
//
// AltRight is deliberately absent from Grph: the guest's scan-code table
// sends it as かな (a locking key), not GRPH, so it must never count as a
// modifier that is already held.
const IME_KEYS = {
  Shift: {sym: 0xffe1, codes: ['ShiftLeft', 'ShiftRight']},
  Ctrl:  {sym: 0xffe3, codes: ['ControlLeft', 'ControlRight']},
  Grph:  {sym: 0xffe9, codes: ['AltLeft']},
  Xfer:  {sym: 0xff23, codes: ['Lang1']},   // -> AT 0x79 -> PC-98 0x35 変換
  Nfer:  {sym: 0xff22, codes: ['Lang2']}    // -> AT 0x7b -> PC-98 0x51 無変換
};

// Shift+無変換 takes two presses to reach direct input: a kana mode goes to
// full-width alphanumeric first, and only that goes on to direct input.  So
// the 英数 key sends it twice and lands there from anywhere.
//
// The かな key drops to direct input the same way, turns the IME back on, and
// then walks whatever mode it came back in to hiragana: GRPH+無変換 pushes
// every mode into the kana group, Shift+無変換 collapses that group onto
// full-width alphanumeric, and 無変換 takes that to hiragana.
const S = {mods: ['Shift'], key: 'Nfer'};   // Shift+無変換
const C = {mods: ['Ctrl'],  key: 'Xfer'};   // Ctrl+変換
const G = {mods: ['Grph'],  key: 'Nfer'};   // GRPH+無変換
const N = {mods: [],        key: 'Nfer'};   // 無変換

// GRPH+無変換 only does anything when SYSTEM.INI carries
// [keyboard] MakeIMEVKey=yes; with the stock file it is inert, and without it
// nothing measured brings the IME back out of direct input.  So 'mac' needs
// that line in the guest, while 'eisu' works on a stock machine.
const IME_MACRO_SETS = {
  // 英数 -> direct input; かな -> hiragana.  Needs MakeIMEVKey=yes in the guest.
  mac:    {Lang2: [S, S],
           Lang1: [S, S, C, G, S, N]},
  // only the 英数 key is redirected; かな keeps whatever it does today.
  // Works on a stock guest: Shift+無変換 is the same with or without the line,
  // and direct input is where it stops, so two presses land there from
  // anywhere.
  eisu:   {Lang2: [S, S]},
  // both keys toggle the IME, which is not what a Mac keyboard promises
  toggle: {Lang2: [C], Lang1: [C]},
  // the one-to-one assignment patched above, untouched
  passthrough: null
};

// 'mac' | 'eisu' | 'toggle' | 'passthrough'
const IME_KEY_MODE = 'passthrough';

// Wrapping _sendKeyEvent rather than onkeyevent is what makes the repeat
// guard work: noVNC updates its own list of depressed keys before it calls
// onkeyevent, so by then a first press and an auto-repeat look alike.  From
// here the list is still untouched, and sharing it means the release noVNC
// synthesises on blur (and the one Windows fakes for these keys) clears the
// guard for free.
function installImeKeyMacros(rfb) {
  const macros = IME_MACRO_SETS[IME_KEY_MODE];
  if (!macros) return null;
  const kbd = rfb._keyboard;
  const hadOwn = Object.prototype.hasOwnProperty.call(kbd, '_sendKeyEvent');
  const prev = kbd._sendKeyEvent;
  const orig = prev.bind(kbd);
  const send = (name, down) => {
    const k = IME_KEYS[name];
    rfb.sendKey(k.sym, k.codes[0], down);
  };
  kbd._sendKeyEvent = function (keysym, code, down, numlock = null,
                                capslock = null) {
    // Without the QEMU extended key event only a keysym goes out, and a
    // combination cannot be spelled that way; leave those connections alone.
    const steps = rfb._qemuExtKeyEventSupported ? macros[code] : null;
    if (!steps) return orig(keysym, code, down, numlock, capslock);
    if (!down) {                            // the press already sent both
      if (code in kbd._keyDownList) delete kbd._keyDownList[code];
      return;
    }
    if (code in kbd._keyDownList) return;   // an auto-repeat, not a new press
    kbd._keyDownList[code] = keysym;
    for (const step of steps) {
      // Modifiers the user is really holding are left alone.  Pressing one
      // again makes QEMU insert a break first, so releasing it afterwards
      // would let go of a key the user still has down.
      const add = step.mods.filter(
        m => !IME_KEYS[m].codes.some(c => c in kbd._keyDownList));
      try {
        for (const m of add) send(m, true);
        send(step.key, true);
        send(step.key, false);
      } finally {
        for (const m of add.slice().reverse()) send(m, false);
      }
    }
  };
  // Put the keyboard back the way it was found.  The method comes from the
  // prototype, so undoing the wrap means taking away what was written over
  // it rather than writing something else in its place.
  return () => {
    if (hadOwn) kbd._sendKeyEvent = prev;
    else delete kbd._sendKeyEvent;
  };
}

function captureRelativePointer(rfb, target) {
  const RFB = rfb.constructor;
  const CENTER = 0x7FFF;
  rfb._sendMouse = function () {};        // silence noVNC's absolute sends
  // These guests draw their own (software) cursor, so noVNC sees no server
  // cursor and hides the local one (canvas cursor:none) -- which left the
  // pointer invisible after Esc.  Ask noVNC for a dot cursor instead.
  rfb.showDotCursor = true;
  let locked = false, mask = 0, accX = 0, accY = 0, flush = false;
  const canvas = () => target.querySelector('canvas');
  const send = (dx, dy, m) => {
    if (!rfb || rfb._rfbConnectionState !== 'connected') return;
    RFB.messages.pointerEvent(rfb._sock,
      (CENTER + dx) & 0xffff, (CENTER + dy) & 0xffff, m);
  };
  // The PC-98 bus mouse and the FM TOWNS mouse both have two buttons, so
  // the middle one is free: while the pointer is captured it leaves the
  // capture, for people who would rather not reach for Esc.  It is
  // swallowed either way (it would
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
  // the relative-pointer negotiation must be in place before the VNC
  // handshake advertises the client encodings, i.e. before the RFB object
  // exists; plugins get the same chance to prepare the connection
  patchRFBForRelativePointer();
  await patchXtScancodesForJIS();
  for (const fn of (window.MiraiPlugins.consolePrep || [])) {
    try { await fn(name); } catch (e) { console.error('console prep', e); }
  }
  rfb = new RFB(target, 'ws://' + location.hostname + ':' + ws + '/');
  rfb.scaleViewport = true;
  rfb.background = '#000';
  try {
    consolePointerStop = captureRelativePointer(rfb, target);
  } catch (e) {
    console.error('pointer capture', e);
  }
  try {
    imeKeyStop = installImeKeyMacros(rfb);
  } catch (e) {
    console.error('ime key macros', e);
  }
  // let plugins augment the console (e.g. the FM TOWNS gamepad)
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
  /*
   * A mode change swaps the canvas's own width and height, and that is
   * the only announcement noVNC makes of it -- the scaled size it puts in
   * the style does not say what the guest switched to.  Watching the
   * element covers the canvas being put there in the first place too.
   */
  consoleFbWatch = new MutationObserver(fitConsoleBox);
  consoleFbWatch.observe(target, {childList: true, subtree: true,
                                  attributes: true,
                                  attributeFilter: ['width', 'height']});
  rfb.addEventListener('connect', () => {
    toast(name + ': console connected');
    fitConsoleBox();
    enableAudioNow().catch(err => console.error('console sound', err));
  });
  // Which connection this listener belongs to: a stale disconnect from a
  // replaced one must not clear the connection that took its place.
  const thisRfb = rfb;
  rfb.addEventListener('disconnect', () => {
    toast(name + ': console disconnected');
    if (rfb !== thisRfb) return;
    // The guest powering itself off takes QEMU with it, and the socket
    // closing is the only way the page hears about it.  Let go of the
    // pointer and the keyboard before anything else, so the window is
    // usable even if the reload below fails, and then ask the server what
    // is actually running: that is what turns "Shut down" back into
    // "Power on".  (The sound goes too: left running, the worklet plays
    // silence and reports it once a second for as long as the page is open.)
    rfb = null;
    releaseConsoleHold();
    render();
  });
  rfb.addEventListener('audiodata', e => audioChunk(e.detail.data));
  document.getElementById('btn-connect').style.display = 'none';
  for (const id of ['btn-disconnect','btn-cad','btn-expand','btn-audio'])
    document.getElementById(id).style.display = '';
};
// Everything the console took hold of while it was open: the pointer, the
// keyboard wrapper, the two observers, the sound.  A guest that powers itself
// off closes the socket from its end, and then there is no connection left to
// disconnect -- only these to let go of -- so this is separate from the
// button's disconnect.  Calling it twice is harmless.
function releaseConsoleHold() {
  (window._pluginConsoleCleanups || []).forEach(c => { try { c(); } catch (e) {} });
  window._pluginConsoleCleanups = [];
  if (consolePointerStop) {
    try { consolePointerStop(); } catch (e) {}
    consolePointerStop = null;
  }
  if (imeKeyStop) {
    try { imeKeyStop(); } catch (e) {}
    imeKeyStop = null;
  }
  if (consoleWatch) { consoleWatch.disconnect(); consoleWatch = null; }
  if (consoleFbWatch) { consoleFbWatch.disconnect(); consoleFbWatch = null; }
  stopAudio();
}
window.disconnectConsole = () => {
  releaseConsoleHold();
  if (rfb) { try { rfb.disconnect(); } catch (e) {} rfb = null; }
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
    (i.running ? '' : '<button type="button" title="rename" ' +
      'onclick="renameVm(\'' + name + '\')">&#9998;</button>') +
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
      // a machine plugin may put its own verbs beside the stock ones;
      // a plugin that throws or hands back something else must not take
      // the page down with it, as the console hooks below also guard
      ...pluginActions(i),
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
    '<dt>BIOS</dt><dd>' + biosLabel(i) + '</dd>' +
    (i.machine === 'towns' ? '' :
     '<dt>Font</dt><dd>' + (i.font === 'real' ? 'real machine ROM'
                                              : 'compatible') + '</dd>') +
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
    (i.machine === 'towns' ? '' :
     '<tr><td style="color:#8d99a5">Display</td><td>' +
     (i.machine === 'pc9801' ? 'PC-9801 standard'
      : 'PEGC + GA-98NB') + '</td></tr>') +
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
  // a swap redraws these rows; a filter someone typed to find the disc is
  // still the one they are working through, so it comes back with them
  const typed = {};
  for (const box of document.querySelectorAll('#media-row .disk-filter'))
    typed[box.dataset.device] = box.value;
  document.getElementById('media-row').innerHTML = d.drives.map(drive =>
    '<div class="row" style="margin:.1em 0">' +
      '<span style="width:5.5em">' + esc(drive.device) + '</span>' +
      diskPicker(drive.kind, drive.file.split('/').pop(),
                 {orphans: true, empty: '(empty)',
                  filter: typed[drive.device] || '',
                  box: ' data-device="' + esc(drive.device) + '"',
                  attrs: 'onchange="swapMedia(\'' + name + '\',\'' +
                         drive.device + '\',this.value)"'}) +
    '</div>').join('');
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
  // The buttons and the state chip were drawn from the fleet list, which
  // nothing refreshes while this view sits open.  A guest that stops on its
  // own moves out from under them, and this reading is where the page finds
  // out; redraw once so the machine can be started again from here.
  if (inst.name !== undefined && inst.running !== undefined &&
      inst.running !== s.running) {
    render();
    return;
  }
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
// what is left of a name once the ending is taken off it: the ending is
// in the type column already, and the name column is for reading
const stemOf = n => { const i = n.lastIndexOf('.');
                      return i > 0 ? n.slice(0, i) : n; };
const CONVERT_TARGETS = {
  // no raw->hdi: an Anex86 image on this shelf is handed to QEMU flat,
  // which reads its header as the first sectors of the disk
  'hdd:.hdi': ['raw'], 'hdd:.raw': ['qcow2'],
  'hdd:.img': ['qcow2'], 'hdd:.qcow2': ['raw'],
  'fdd:.fdi': ['raw'], 'fdd:.nfd': ['raw'],
  'fdd:.raw': ['fdi', 'nfd'], 'fdd:.img': ['fdi', 'nfd'],
};

// what a catalog entry is: its type, and for a CD dump the state of its
// cue sheet (tracks, audio, a sheet that cannot be followed, an orphan)
function diskTypeCell(f) {
  let t = esc(f.type || extOf(f.name) || '-');
  if (f.orphan) {
    return '<span style="color:#e06c5f">' + t + ' — no data file: ' +
      esc((f.missing || []).join(', ')) + '</span>';
  }
  // a sheet gives the track count, and a .chd gives it from inside
  if (f.tracks) {
    t += ' <span class="note">' + f.tracks + ' trk' +
         (f.audio ? ', ' + f.audio + ' audio' : '') + '</span>';
  }
  if (f.cue_where) {
    t += ' <span class="note" title="the drive follows the link and finds ' +
      'the sheet beside the file it points at, whatever this is called">' +
      'sheet in ' + esc(f.cue_where) + '</span>';
  }
  if (f.cue) {
    if (f.multi) t += ' <span style="color:#e06c5f" title="the cue names ' +
      f.multi + ' data files; the emulator follows a single one">' +
      'multi-file cue</span>';
    else if (f.cue_mismatch) t += ' <span style="color:#e06c5f" title="' +
      esc(f.cue) + ' does not share the data file\'s name; the emulator ' +
      'looks for the same stem">sheet name differs</span>';
  }
  return t;
}
// Which groups are open, kept per browser: a shelf someone arranged is
// one they come back to.
const openGroups = JSON.parse(localStorage.getItem('mirai98.groups') || '{}');
const diskFilter = {};
function groupOpen(kind, group) { return !!openGroups[kind + '/' + group]; }

// One row per image, and one per group with its images folded behind it.
function storageCard(kind, files) {
  const groups = [...new Set(files.map(f => f.group || ''))]
    .filter(Boolean).sort();
  // the ending is shown in the type column, so the name column drops it
  // -- unless two discs here would then read the same, in which case the
  // ending is the only thing telling them apart and it stays
  const stems = {};
  files.forEach(f => { stems[stemOf(f.name)] = (stems[stemOf(f.name)] || 0) + 1; });
  const fileRow = f => {
    const used = f.used_by.join(', ');
    const targets = CONVERT_TARGETS[kind + ':' + extOf(f.name)] || [];
    const g = f.group || '';
    return '<tr data-name="' + esc(f.name) + '" data-groupname="' + esc(g) +
      '"' + (g && !groupOpen(kind, g) ? ' style="display:none"' : '') +
      '><td' + (g ? ' style="padding-left:1.6em"' : '') + '>' +
      '<input type="checkbox" class="pick" value="' + esc(f.name) + '"> ' +
      '<a href="#/disk/' + kind + '/' + encodeURIComponent(f.name) +
      '" title="' + esc(f.name) + '">' +
      esc(stems[stemOf(f.name)] > 1 ? f.name : stemOf(f.name)) +
      '</a></td><td>' +
      diskTypeCell(f) + '</td><td>' + fmtBytes(f.size) +
      '</td><td class="note">' + fmtDate(f.mtime) + '</td><td>' +
      esc(used || '-') + '</td><td style="text-align:right">' +
      '<button type="button" onclick="showTree(\'' + kind + '\',\'' +
      esc(jsq(f.name)) + '\')">Contents</button> ' +
      '<a href="/disks/' + kind + '/' + encodeURIComponent(f.name) +
      '" download><button type="button">Download</button></a> ' +
      targets.map(t => '<button type="button" onclick="convertDisk(\'' +
        kind + '\',\'' + esc(jsq(f.name)) + '\',\'' + t + '\')">to ' + t +
        '</button>').join(' ') +
      ' <button type="button" onclick="moveOne(\'' + kind + '\',\'' +
      esc(jsq(f.name)) + '\')">Move...</button>' +
      ' <button type="button" onclick="renameDisk(\'' + kind +
      '\',\'' + esc(jsq(f.name)) + '\')">Rename</button>' +
      ' <button type="button"' + (used ? ' disabled title="in use"' : '') +
      ' onclick="deleteDisk(\'' + kind + '\',\'' + esc(jsq(f.name)) +
      '\')">Delete</button></td></tr>';
  };
  const groupRow = g => {
    const mine = files.filter(f => (f.group || '') === g);
    const used = [...new Set(mine.flatMap(f => f.used_by))].join(', ');
    return '<tr data-grouprow="' + esc(g) + '" style="cursor:pointer" ' +
      'onclick="toggleGroup(\'' + kind + '\',\'' + g + '\')">' +
      '<td><span class="caret">' + (groupOpen(kind, g) ? '▼' : '▶') +
      '</span> <b>' + esc(g) + '</b> <span class="note">' + mine.length +
      ' image' + (mine.length === 1 ? '' : 's') + '</span></td>' +
      '<td class="note">group</td><td>' +
      fmtBytes(mine.reduce((n, f) => n + f.size, 0)) +
      '</td><td class="note">' +
      fmtDate(Math.max(...mine.map(f => f.mtime))) + '</td><td>' +
      esc(used || '-') + '</td><td style="text-align:right">' +
      '<button type="button" onclick="event.stopPropagation();moveGroup(\'' +
      kind + '\',\'' + g + '\')">Move all...</button></td></tr>';
  };
  const rows = groups.map(g => groupRow(g) +
      files.filter(f => (f.group || '') === g).map(fileRow).join('')).join('') +
    files.filter(f => !f.group).map(fileRow).join('');
  const shelf = '<div class="row" style="margin:.2em 0">' +
    '<input type="text" placeholder="filter" style="min-width:9em" ' +
    'value="' + esc(diskFilter[kind] || '') + '" ' +
    'oninput="filterDisks(\'' + kind + '\',this.value)">' +
    '<span class="note" id="storage-count-' + kind + '">' + files.length +
    ' image' + (files.length === 1 ? '' : 's') + '</span>' +
    '<button type="button" onclick="moveChecked(\'' + kind +
    '\')">Move checked...</button>' +
    '<button type="button" onclick="suggestGroups(\'' + kind +
    '\')">Group by name...</button></div>';
  let create = '';
  // a machine plugin may add image formats of its own (label, note)
  const extra = window.MiraiPlugins.diskFormats[kind] || [];
  const extraOpts = extra.map(f => '<option value="' + esc(f.value) + '">' +
                                   esc(f.label) + '</option>').join('');
  const extraNotes = extra.filter(f => f.note).map(f =>
    '<div class="note">' + esc(f.label) + ': ' + esc(f.note) + '</div>').join('');
  if (kind === 'hdd')
    create = '<h4>Create a disk</h4><div class="body">' +
      '<form onsubmit="return createDisk(this,\'hdd\')" class="row">' +
      '<input type="text" name="name" placeholder="new-disk" ' +
      'required style="min-width:11em"><select name="size">' +
      [40,80,160,320,640,1200,1600,2100,4300].map(s => '<option' +
        (s === 40 ? ' selected' : '') + '>' + s + '</option>').join('') +
      '</select><span class="note">MB</span>' +
      '<select name="format"><option value="">PC-98 (FAT, flat)</option>' +
      '<option value="qcow2">PC-98 (FAT, grows on demand)</option>' +
      extraOpts + '</select>' +
      '<label class="check"><input type="checkbox" name="fat32"> FAT32' +
      '</label><button class="primary">Create</button></form>' +
      '<div class="note">The extension is added for you. Anex86 .hdi ' +
      'cannot be made here: upload one and it is converted.</div>' +
      extraNotes + '</div>';
  else if (kind === 'fdd')
    create = '<h4>Create a floppy</h4><div class="body">' +
      '<form onsubmit="return createDisk(this,\'fdd\')" class="row">' +
      '<input type="text" name="name" placeholder="new-floppy" ' +
      'required style="min-width:11em"><select name="format">' +
      '<option value="1.2">1.25MB 2HD (raw)</option>' +
      '<option value="1.44">1.44MB 2HD (raw)</option>' + extraOpts + '</select>' +
      '<button class="primary">Create</button></form>' +
      '<div class="note">The extension is added for you. Formatted, empty. ' +
      'A name already ending in .raw, .img, .fdi, .nfd or .d88 keeps it.' +
      '</div>' +
      extraNotes + '</div>';
  return '<div class="card" id="storage-' + kind + '"><h3>disks/' + kind +
    '/</h3>' +
    '<h4>Images</h4>' + shelf + '<table>' +
    '<tr><th>Name</th><th>Type</th><th>Size</th><th>Modified</th>' +
    '<th>Used by</th><th></th></tr>' +
    (rows || '<tr><td colspan="6" class="note">(empty)</td></tr>') +
    '</table>' +
    '<h4>Add an image</h4><div class="body" ondragover="event.preventDefault()"' +
    ' ondrop="dropUpload(event,\'' + kind + '\')"><div class="row">' +
    '<button type="button" onclick="pickUpload(\'' + kind +
    '\')">Upload from this computer...</button>' +
    '<button type="button" onclick="fetchDisk(\'' + kind +
    '\')">Download from URL...</button>' +
    '<button type="button" onclick="importDisk(\'' + kind +
    '\')">Import a server path...</button>' +
    '<button type="button" onclick="readFromDrive(\'' + kind +
    '\')">Read a host drive...</button>' +
    '<label class="check">into <select id="upload-group-' + kind + '">' +
    '<option value="">no group</option>' +
    [...new Set(files.map(f => f.group || ''))].filter(Boolean).sort()
      .map(g => '<option>' + esc(g) + '</option>').join('') +
    '<option value="__new">new group...</option></select></label></div>' +
    '<div id="jobs-' + kind + '" class="note"></div>' +
    (kind === 'cdrom'
     ? '<div class="note">Several files may be chosen (or dropped here) at ' +
       'once: a .cue and its .bin/.img, a .ccd with its .img and .sub, or ' +
       'a .mds and its .mdf travel as one disc, the sheet is pointed at ' +
       'the stored names, and the set is listed as one entry.  A .chd ' +
       'holds its tracks inside it and comes on its own.</div>' : '') +
    '</div>' +
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

// ------------------------------------------- the files inside an image
// A thin cover over /api/fs.  The image is edited in place, so the server
// turns writes away while a machine has it open, and names become 8.3 on
// the way in -- the answer says what a file actually landed as.
let fsAt = null;
// Which listing is the current one.  Two overlapping loads used to split
// the state: the screen came from the answer that arrived last, fsAt from
// the request that was issued last, and the row handlers then acted on a
// directory that was not the one on screen.
let fsSeq = 0;
let fsBusy = false;

function fsJoin(dir, leaf) {
  return (dir === '/' ? '' : dir) + '/' + leaf;
}

function fsUrl(verb, extra, partition, at) {
  const on = at || fsAt;
  return '/api/fs/' + on.kind + '/' + encodeURIComponent(on.name) +
    (verb ? '/' + verb : '') +
    '?partition=' + (partition == null ? on.partition : partition) +
    (extra || '');
}

function fsCrumbs(path) {
  const parts = path.split('/').filter(Boolean);
  let at = '';
  const out = ['<a href="#" data-go="/">(root)</a>'];
  parts.forEach(p => {
    at += '/' + p;
    out.push('<a href="#" data-go="' + esc(at) + '">' + esc(p) + '</a>');
  });
  return out.join(' &rsaquo; ');
}

function fsRow(e) {
  return '<tr><td><a href="#" data-open="' + esc(e.name) + '" data-dir="' +
    (e.dir ? 1 : 0) + '">' + (e.dir ? '&#128193;' : '&#128196;') + ' ' +
    esc(e.name) + '</a>' +
    (e.long && e.long.toUpperCase() !== e.name.toUpperCase()
      ? '<div class="note">' + esc(e.long) + '</div>' : '') +
    '</td><td>' + (e.dir ? '' : fmtBytes(e.size)) +
    '</td><td class="note">' + esc(e.modified || '') +
    '</td><td style="white-space:nowrap">' +
    '<button data-rename="' + esc(e.name) + '" data-shown="' +
    esc(e.long || e.name) + '">Rename</button> ' +
    '<button data-remove="' + esc(e.name) + '" data-shown="' +
    esc(e.long || e.name) + '" data-dir="' +
    (e.dir ? 1 : 0) + '">Delete</button></td></tr>';
}

async function fsLoad(path) {
  const box = document.getElementById('fsbox');
  if (!box || !fsAt) return;
  const seq = ++fsSeq;
  const where = path == null ? fsAt.path : path;
  const got = await apiResult(fsUrl('', '&path=' + encodeURIComponent(where),
                                    fsAt.partition));
  if (seq !== fsSeq) return;            // a newer listing is already on its way
  if (!got.ok || !got.data || !Array.isArray(got.data.entries)) {
    box.innerHTML = '<div class="note">' +
      esc(got.error || 'that folder could not be read') +
      (got.error && /qcow2/i.test(got.error) ? '' :
       ' &mdash; only raw PC-98 disks and floppies can be browsed.') +
      '</div><div class="row" style="margin-top:.6em">' +
      '<button data-go="' + esc(fsAt.path) + '">Back to ' +
      esc(fsAt.path) + '</button></div>';
    fsWire(box, fsAt.path, fsAt.partition);
    return;
  }
  const d = got.data;
  // only now is this the folder we are in
  fsAt.path = d.path;
  fsAt.partition = d.partition;
  const dirs = d.entries.filter(e => e.dir);
  const files = d.entries.filter(e => !e.dir);
  const by = (a, b) => a.name.localeCompare(b.name);
  box.innerHTML =
    '<div class="row" style="align-items:center;gap:.6em;flex-wrap:wrap">' +
    (d.partitions.length > 1
      ? '<label>Partition <select data-part>' + d.partitions.map(p =>
          '<option value="' + esc(p.n) + '"' + (p.n === d.partition ? ' selected' : '')
          + '>' + p.n + (p.name ? ' – ' + esc(p.name) : '') + '</option>')
          .join('') + '</select></label>' : '') +
    '<span class="note">FAT' + esc(d.fat) + ' · ' + fmtBytes(d.free) +
    ' free of ' + fmtBytes(d.total) + '</span>' +
    '<button data-mkdir>New folder...</button></div>' +
    '<div class="crumb">' + fsCrumbs(d.path) + '</div>' +
    '<div data-drop style="border:1px dashed #4a5a6a;border-radius:4px;' +
    'padding:.4em">' +
    '<table><tr><th>Name</th><th style="width:7em">Size</th>' +
    '<th style="width:10em">Modified</th><th style="width:12em"></th></tr>' +
    (d.path !== '/' ? '<tr><td colspan="4"><a href="#" data-go="' +
      esc(d.path.replace(/\/[^/]*$/, '') || '/') + '">&#8593; up</a></td></tr>'
      : '') +
    (d.entries.length
      ? dirs.sort(by).concat(files.sort(by)).map(fsRow).join('')
      : '<tr><td colspan="4" class="note">(empty)</td></tr>') +
    '</table>' +
    '<div class="note">Drop files here from the desktop to put them in this ' +
    'folder. Long names are kept the way Windows keeps them, with an 8.3 ' +
    'name beside them for DOS.</div></div>';
  fsWire(box, d.path, d.partition);
}

function fsWire(box, path, partition) {
  // every handler works on the folder that is on the screen, taken as it
  // was rendered: reading fsAt at click time meant acting on whatever had
  // been navigated to since
  const here = {kind: fsAt.kind, name: fsAt.name, partition: partition,
                path: path};
  box.querySelectorAll('[data-go]').forEach(a => {
    a.onclick = ev => { ev.preventDefault(); fsLoad(a.dataset.go); };
  });
  box.querySelectorAll('[data-open]').forEach(a => {
    a.onclick = ev => {
      ev.preventDefault();
      const where = fsJoin(here.path, a.dataset.open);
      if (a.dataset.dir === '1') { fsLoad(where); return; }
      location.href = '/fsfile/' + here.kind + '/' +
        encodeURIComponent(here.name) + '?partition=' + here.partition +
        '&path=' + encodeURIComponent(where);
    };
  });
  box.querySelectorAll('[data-rename]').forEach(b => {
    b.onclick = () => fsRename(here, b.dataset.rename, b.dataset.shown);
  });
  box.querySelectorAll('[data-remove]').forEach(b => {
    b.onclick = () => fsRemove(here, b.dataset.remove, b.dataset.shown,
                               b.dataset.dir === '1');
  });
  const part = box.querySelector('[data-part]');
  if (part) part.onchange = () => {
    fsAt.partition = Number(part.value);
    fsLoad('/');
  };
  const mk = box.querySelector('[data-mkdir]');
  if (mk) mk.onclick = () => fsMkdir(here);
  const zone = box.querySelector('[data-drop]');
  if (!zone) return;
  let inside = 0;
  const lit = on => { zone.style.background = on ? 'rgba(122,60,255,.12)' : ''; };
  zone.addEventListener('dragenter', ev => { ev.preventDefault(); inside++; lit(true); });
  zone.addEventListener('dragover', ev => { ev.preventDefault(); lit(true); });
  // crossing onto a child fires dragleave, so count the crossings
  zone.addEventListener('dragleave', () => { inside = Math.max(0, inside - 1);
                                             if (!inside) lit(false); });
  zone.addEventListener('drop', async ev => {
    ev.preventDefault();
    inside = 0; lit(false);
    await fsDrop(ev.dataTransfer, here);
  });
}

async function fsDrop(transfer, here) {
  if (fsBusy) { toast('still putting the last lot in'); return; }
  const items = transfer.items ? [...transfer.items] : [];
  const folders = items.filter(
    it => it.webkitGetAsEntry && it.webkitGetAsEntry() &&
          it.webkitGetAsEntry().isDirectory);
  const files = [...(transfer.files || [])].filter(f => !folders.length ||
                                                        f.size || f.type);
  if (!files.length) {
    toast(folders.length ? 'drop the files themselves, not the folder'
                         : 'nothing to put in');
    return;
  }
  // where they were dropped, kept for the whole run: navigating away mid
  // upload used to send the rest into another folder, or another image
  const at = here || fsAt;
  fsBusy = true;
  let done = 0;
  const became = [], failed = [];
  try {
    for (const file of files) {
      toast('putting ' + file.name + ' in...');
      const got = await apiResult(
        fsUrl('put', '&path=' + encodeURIComponent(at.path) +
              '&name=' + encodeURIComponent(file.name), at.partition, at),
        {method: 'POST', body: file});
      if (!got.ok) { failed.push(file.name + ': ' + got.error); continue; }
      done++;
      if (got.data.renamed) became.push(file.name + ' \u2192 ' + got.data.name);
    }
  } finally {
    fsBusy = false;
  }
  const said = [];
  if (done) said.push(done + ' file' + (done === 1 ? '' : 's') + ' written' +
                      (became.length ? ' (' + became.join(', ') + ')' : ''));
  if (failed.length) said.push(failed.length + ' failed \u2014 ' + failed[0]);
  if (folders.length) said.push('folders were skipped');
  if (said.length) toast(said.join('; '));
  if (done) task('Disk ' + at.name + ' - ' + done + ' file(s) in',
                 failed.length ? 'partly' : 'OK');
  if (failed.length) task('Disk ' + at.name + ' - ' + failed.length +
                          ' file(s) refused', failed[0]);
  if (fsAt && at.name === fsAt.name && at.path === fsAt.path) fsLoad();
}

async function fsMkdir(here) {
  const at = here || fsAt;
  const typed = prompt('name for the new folder in ' + at.path + '?');
  if (typed === null) return;
  const name = typed.trim();
  if (!name) return;
  const r = await api(fsUrl('mkdir', '', at.partition, at), {method: 'POST',
    body: JSON.stringify({path: at.path, name: name})});
  if (r) {
    toast('created ' + r.name);
    task('Disk ' + at.name + ' - new folder ' + r.name, 'OK');
    fsLoad();
  }
}

async function fsRename(here, name, shown) {
  const at = here || fsAt;
  const was = shown || name;
  const typed = prompt('rename ' + was + '\n\nin ' + at.path, was);
  if (typed === null) return;
  const to = typed.trim();
  if (!to) return;
  if (to === was) { toast('that is the name it already has'); return; }
  const r = await api(fsUrl('rename', '', at.partition, at), {method: 'POST',
    body: JSON.stringify({path: fsJoin(at.path, name), name: to})});
  if (r) {
    toast('renamed to ' + r.name);
    task('Disk ' + at.name + ' - rename ' + was, 'OK');
    fsLoad();
  }
}

async function fsRemove(here, name, shown, isDir) {
  const at = here || fsAt;
  const was = shown || name;
  if (!confirm('remove ' + was + ' from ' + at.path +
               (isDir ? ' and everything in it?' : '?')))
    return;
  const r = await api(fsUrl('delete', '', at.partition, at), {method: 'POST',
    body: JSON.stringify({paths: [fsJoin(at.path, name)]})});
  if (r) {
    toast('removed ' + r.count + ' entr' + (r.count === 1 ? 'y' : 'ies'));
    task('Disk ' + at.name + ' - remove ' + was, 'OK');
    fsLoad();
  }
}

// ---------------------------------------------------- one disk's page
async function diskView(kind, name) {
  view.innerHTML = '<div class="note">reading ' + esc(name) + '...</div>';
  const d = await api('/api/disk/' + kind + '/' + encodeURIComponent(name));
  if (!d) { view.innerHTML = ''; return; }
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
      '<button onclick="pickZip(\'' + kind + '\',\'' + esc(jsq(name)) +
      '\')">Write a ZIP into it...</button>',
      '<button onclick="copyDisk(\'' + kind + '\',\'' + esc(jsq(name)) +
      '\')">Make a copy</button>',
      '<button onclick="renameDisk(\'' + kind + '\',\'' + esc(jsq(name)) +
      '\',\'#/disk/' + kind + '/\')">Rename...</button>',
      '<button onclick="writeToDrive(\'' + kind + '\',\'' + esc(jsq(name)) +
      '\')">Write to a drive...</button>',
      '<button onclick="render()">Refresh</button>',
      d.used_by.length ? '' : '<button onclick="deleteDisk(\'' + kind +
        '\',\'' + esc(jsq(name)) + '\',\'#/storage\')">Delete</button>']) +
    '<div class="grid2" style="grid-template-columns:22em 1fr">' +
    '<div class="card"><h3>Details</h3><table>' +
    [['File', d.path], ['Format', d.format], ['Size', fmtBytes(d.size)],
     ['Modified', new Date(d.mtime * 1000).toLocaleString()],
     ['Used by', d.used_by.join(', ') || 'no machine']]
      .map(([k, v]) => '<tr><td style="width:7em;color:#8d99a5">' + k +
        '</td><td style="overflow-wrap:anywhere">' + esc(v) +
        '</td></tr>').join('') + '</table></div>' +
    '<div class="card"><h3>Files</h3><div id="fsbox">' +
    '<div class="note">reading...</div></div></div></div>';
  fsAt = {kind: kind, name: name, partition: 1, path: '/'};
  if (kind !== 'cdrom') fsLoad('/');
  else document.getElementById('fsbox').innerHTML =
    '<div class="note">A disc image is read-only here.</div>';
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
       body: JSON.stringify({device: drive.path, name,
                             group: uploadGroup(kind)})})
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
    .then(r => { if (r) { toast('made ' + (r.files || [r.name]).join(' + '));
                          task('Disk ' + name + ' - copy', 'OK');
                          location.hash = '#/disk/' + kind + '/' +
                            encodeURIComponent(r.name); }
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
  // the rows were written folded; this is what a filter left on from
  // before does to them, once they are in the document
  ['hdd', 'fdd', 'cdrom'].forEach(applyView);
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
// Rows come out of storageCard already folded the way the browser last
// left them; this is what a click on a caret, or a letter in the filter,
// does to them afterwards.
function applyView(kind) {
  const card = document.getElementById('storage-' + kind);
  if (!card) return;
  const q = (diskFilter[kind] || '').trim().toLowerCase();
  let shown = 0, total = 0;
  card.querySelectorAll('tr[data-name]').forEach(tr => {
    const g = tr.dataset.groupname;
    const hit = !q || (tr.dataset.name + ' ' + g).toLowerCase().includes(q);
    total++;
    if (hit) shown++;
    // while a filter is on, every group is open: a hit folded away behind
    // a shut group reads as no hit at all
    tr.style.display = (hit && (!g || q || groupOpen(kind, g))) ? '' : 'none';
  });
  card.querySelectorAll('tr[data-grouprow]').forEach(tr => {
    const g = tr.dataset.grouprow;
    const kids = [...card.querySelectorAll('tr[data-groupname="' + g + '"]')];
    tr.style.display = (!q || kids.some(k => k.style.display !== 'none'))
      ? '' : 'none';
    const caret = tr.querySelector('.caret');
    if (caret) caret.textContent = (q || groupOpen(kind, g)) ? '▼' : '▶';
  });
  const note = document.getElementById('storage-count-' + kind);
  if (note) note.textContent = q
    ? shown + ' of ' + total + ' shown'
    : total + ' image' + (total === 1 ? '' : 's');
}
function rememberGroups() {
  localStorage.setItem('mirai98.groups', JSON.stringify(openGroups));
}
window.toggleGroup = (kind, group) => {
  openGroups[kind + '/' + group] = !openGroups[kind + '/' + group];
  rememberGroups();
  applyView(kind);
};
window.filterDisks = (kind, text) => {
  diskFilter[kind] = text;
  applyView(kind);
};
// Moving is not renaming: the name is the same wherever the image sits,
// so nothing that named it has to be told, and a CD dump takes its sheet
// with it.
function askGroup(kind, what) {
  const groups = [...new Set((catalog[kind] || []).map(f => f.group)
                                                  .filter(Boolean))];
  const to = prompt('move ' + what + ' into which group?' +
    (groups.length ? '\n\nthere is already: ' + groups.join(', ') : '') +
    '\n\nleave it empty to take it out of its group', '');
  return to === null ? null : to.trim();
}
function doMove(kind, names, group) {
  api('/api/disks/' + kind + '/move',
      {method: 'POST', body: JSON.stringify({names: names, group: group})})
    .then(r => { if (r) {
                   toast(r.files.length + ' moved ' +
                         (r.group ? 'into ' + r.group : 'out of its group') +
                         ((r.left || []).length
                          ? '; ' + r.left.join(', ') + ' stayed behind' : ''));
                   task('Storage ' + kind + ' - move', 'OK');
                   if (r.group) {
                     openGroups[kind + '/' + r.group] = true;
                     rememberGroups();
                   } }
                 render(); });
}
window.moveOne = (kind, name) => {
  const to = askGroup(kind, name);
  if (to !== null) doMove(kind, [name], to);
};
window.moveGroup = (kind, group) => {
  const names = (catalog[kind] || []).filter(f => f.group === group)
                                     .map(f => f.name);
  const to = askGroup(kind, group + ' (' + names.length + ')');
  if (to !== null) doMove(kind, names, to);
};
window.moveChecked = kind => {
  const card = document.getElementById('storage-' + kind);
  const names = [...card.querySelectorAll('input.pick:checked')]
    .map(i => i.value);
  if (!names.length) { toast('nothing is checked'); return; }
  const to = askGroup(kind, names.length + ' images');
  if (to !== null) doMove(kind, names, to);
};
// What the names suggest, put one group at a time so each can be turned
// down: a name is a hint about what a disc belongs to, not a statement.
window.suggestGroups = async kind => {
  const r = await api('/api/disks/' + kind + '/suggest-groups',
                      {method: 'POST'});
  if (!r) return;
  if (!r.groups.length) { toast('the names suggest nothing to group'); return; }
  let moved = 0;
  for (const g of r.groups) {
    if (!confirm('Shelve these ' + g.names.length + ' as "' + g.group +
                 '"?\n\n' + g.names.join('\n'))) continue;
    const ok = await api('/api/disks/' + kind + '/move',
      {method: 'POST', body: JSON.stringify({names: g.names, group: g.group})});
    if (!ok) break;
    moved += ok.files.length;
    openGroups[kind + '/' + g.group] = true;
  }
  rememberGroups();
  if (moved) {
    toast(moved + ' moved');
    task('Storage ' + kind + ' - group by name', 'OK');
  }
  render();
};
// The group whatever arrives next goes into, as the panel has it set.
function uploadGroup(kind) {
  const sel = document.getElementById('upload-group-' + kind);
  if (!sel) return '';
  if (sel.value !== '__new') return sel.value;
  return (prompt('name for the new group?', '') || '').trim();
}
// A CD dump is a set: the sheet beside the data file is renamed with
// it and pointed at the new name, and a machine that named the image is
// pointed at it too -- so the server answers with everything that moved.
window.renameDisk = (kind, name, then) => {
  // the ending is the file's own and is not up for typing: what is asked
  // for is the stem, and the server puts the ending back
  const dot = name.lastIndexOf('.');
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : '';
  const typed = prompt('rename ' + name + ' to?' +
                       (ext ? '\n\n' + ext + ' stays as it is' : ''), stem);
  if (typed === null) return;
  const to = typed.trim();
  if (!to || to === stem) return;
  api('/api/disks/' + kind + '/rename',
      {method: 'POST', body: JSON.stringify({name: name, stem: to})})
    .then(r => { if (r) { toast('renamed to ' + r.files.join(' + ') +
                            (r.vms.length ? ', and ' + r.vms.join(', ') +
                             ' follows' : '') +
                            (r.left ? '; ' + r.left + ' stayed with the file ' +
                             'this one points at' : ''));
                          task('Disk ' + name + ' - rename to ' + r.name,
                               'OK');
                          if (then) {
                            location.hash = then + encodeURIComponent(r.name);
                            return;
                          } }
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
      {method: 'POST', body: JSON.stringify({url, group: uploadGroup(kind)})})
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
      {method: 'POST', body: JSON.stringify({path, group: uploadGroup(kind)})})
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

function uploadBox(file, kind, label) {
  document.getElementById('upload-name').textContent =
    (label || file.name) + ' → disks/' + kind + '/';
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

// the name a file is stored under: what the server's SAFE_NAME allows,
// anything else becoming '_' (the server sanitises the same way)
// The same rule the server keeps (given_name / safe_disk_name there): a
// disc keeps the name it came with, kanji and spaces and all, and only
// what would break something downstream is turned into '_'.  The length
// is counted in bytes, as a filesystem counts it.
function safeDiskName(name) {
  let out = name.replace(/[\u0000-\u001f\u007f/\\:*?"<>|]/g, '_')
                .trim().replace(/^\.+/, '').replace(/[ .]+$/, '');
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)/i.test(out)) out = '_' + out;
  const bytes = new TextEncoder().encode(out);
  if (bytes.length > 240) {
    // the cut may land inside a character; the decoder marks it and it goes
    out = new TextDecoder().decode(bytes.slice(0, 240)).replace(/\uFFFD$/, '');
  }
  return out || 'disk';
}
window.dropUpload = (ev, kind) => {
  ev.preventDefault();
  if (ev.dataTransfer && ev.dataTransfer.files.length)
    uploadFiles(kind, [...ev.dataTransfer.files]);
};
window.pickUpload = kind => {
  const input = document.createElement('input');
  input.type = 'file';
  input.multiple = true;
  input.onchange = () => { if (input.files.length) uploadFiles(kind, [...input.files]); };
  input.click();
};
// Several files at once.  For CD dumps a sheet and the data files it
// names form a set: the sheet is checked against the selection first, all
// files go up under safe names, then the server points the sheet at them.
// A .mds or a .ccd names its data by stem instead -- the .mdf or .img
// sharing it -- and a CloneCD .sub rides along unnamed.
async function uploadFiles(kind, files) {
  const group = uploadGroup(kind);
  const plan = files.map(f => ({ file: f, name: safeDiskName(f.name) }));
  const cues = plan.filter(p => /\.(cue|mds|ccd)$/i.test(p.name));
  if (kind === 'cdrom' && cues.length) {
    const have = new Set(plan.map(p => p.file.name.toLowerCase()));
    const haveSafe = new Set(plan.map(p => p.name.toLowerCase()));
    for (const c of cues) {
      let refs;
      if (/\.(mds|ccd)$/i.test(c.name)) {
        refs = [c.file.name.replace(/\.[^.]*$/, '') +
                (/\.mds$/i.test(c.name) ? '.mdf' : '.img')];
      } else {
        const text = await c.file.text().catch(() => '');
        refs = [...text.matchAll(/^\s*FILE\s+(?:"([^"]*)"|(\S+))/gim)]
          .map(m => (m[1] || m[2]).replace(/\\/g, '/').split('/').pop());
      }
      const missing = refs.filter(r => !have.has(r.toLowerCase()) &&
        !haveSafe.has(safeDiskName(r).toLowerCase()) &&
        !(catalog[kind] || []).some(f => f.name.toLowerCase() === r.toLowerCase()));
      if (!refs.length) {
        if (!confirm(c.file.name + ' has no FILE line. Upload it anyway?')) return;
      } else if (missing.length) {
        if (!confirm(c.file.name + ' names ' + missing.join(', ') +
                     ', which is not among the chosen files (nor already ' +
                     'in disks/cdrom/). Upload anyway?')) return;
      }
    }
  }
  const dupes = plan.filter(p => (catalog[kind] || []).some(f => f.name === p.name));
  let overwrite = '';
  if (dupes.length) {
    if (!confirm(dupes.map(p => p.name).join(', ') + ' exist(s). Overwrite?')) return;
    overwrite = '&overwrite=1';
  }
  const stored = [];
  for (let n = 0; n < plan.length; n++) {
    const p = plan[n];
    const done = await uploadOne(kind, p.file, p.name, overwrite,
                                 plan.length > 1 ? (n + 1) + '/' + plan.length + ' ' : '',
                                 group);
    if (!done) return;
    stored.push(done);
  }
  if (kind === 'cdrom' && cues.length) {
    const stat = document.getElementById('upload-stat');
    for (const c of cues) {
      stat.textContent = 'settling ' + c.name + '...';
      const r = await api('/api/disks/cdrom/set',
        {method: 'POST', body: JSON.stringify(
          {cue: c.name, files: stored.filter(s => !/\.(cue|mds|ccd)$/i.test(s))})});
      if (r) {
        stat.textContent = 'disc set ' + r.cue + ': ' + r.files.join(' + ') +
          ' — ' + r.tracks + ' track(s)' + (r.audio ? ', ' + r.audio + ' audio' : '') +
          (r.multi ? ' (multi-file cue: not playable by the emulator yet)' : '');
        toast('disc set ' + r.cue + ' ready');
        task('Disc ' + r.cue + ' - set', 'OK');
      } else {
        stat.textContent = 'the disc set could not be settled: see the toast';
      }
    }
  }
  render();
}
// one file, in slices; resolves to the stored name or null when it failed
async function uploadOne(kind, file, name, overwrite, prefix, group) {
    const base = '/api/disks/' + kind;
    const q = 'name=' + encodeURIComponent(name) + '&total=' + file.size +
              (group ? '&group=' + encodeURIComponent(group) : '');
    uploadBox(file, kind, prefix + name);
    const bar = document.getElementById('upload-bar');
    const stat = document.getElementById('upload-stat');
    const started = Date.now();
    let sent = 0, tries = 0;
    while (sent < file.size) {
      if (uploadStop) { toast('upload cancelled'); return null; }
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
          return null;
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
      const named = done.name || name;
      stat.textContent = 'uploaded ' + named +
        (named !== file.name ? ' (stored as ' + named + ')' : '');
      document.getElementById('upload-close').textContent = 'Close';
      toast('uploaded ' + named);
      task('Disk ' + named + ' - upload', 'OK');
      return named;
    } catch (err) {
      stat.textContent = 'failed: ' + err.message;
      document.getElementById('upload-close').textContent = 'Close';
      task('Disk ' + file.name + ' - upload', err.message);
      return null;
    }
}

// ------------------------------------------------------ create wizard
window.memSlide = el => {
  document.getElementById('mem-text').value = MEMS[el.value];
};
window.memType = el => {
  const i = MEMS.indexOf(el.value.trim().toUpperCase());
  if (i >= 0) document.getElementById('mem-range').value = i;
};
// A plugin machine may hand the wizard its own panes (wizard.panes[name]:
// a function returning the pane's HTML, or null to hide the tab) and its
// own confirmation rows; the stock PC-98 panes are kept to swap back.
let hiddenTabs = new Set();
const stockPanes = {};
const visibleTabs = () => TABS.filter(t => !hiddenTabs.has(t));
function wizardHelpers() {
  return { esc, diskSelect, MEMS,
           note: t => ' <span class="note">' + t + '</span>' };
}
// which panes currently show a plugin's HTML (only those are put back:
// redrawing a stock pane would reset its inputs - the machine select in
// General among them)
const customPanes = new Set();
function applyWizardMachine(machine) {
  const w = window.MiraiPlugins.wizard[machine];
  hiddenTabs = new Set();
  document.querySelectorAll('#wizard .pane').forEach(p => {
    const name = p.dataset.pane;
    const custom = w && w.panes ? w.panes[name] : undefined;
    if (custom === null) {
      hiddenTabs.add(name);
      if (customPanes.has(name)) {
        p.innerHTML = stockPanes[name] || '';
        customPanes.delete(name);
      }
    } else if (typeof custom === 'function') {
      p.innerHTML = custom(wizardHelpers());
      customPanes.add(name);
    } else if (customPanes.has(name)) {
      p.innerHTML = stockPanes[name] || '';
      customPanes.delete(name);
    }
  });
  drawTabs();
}
function drawTabs() {
  const tabs = visibleTabs();
  if (tab >= tabs.length) tab = tabs.length - 1;
  document.getElementById('tabs').innerHTML = tabs.map((t, n) =>
    '<span class="' + (n === tab ? 'on' : '') + '" onclick="tabGo(' + n +
    ')">' + t + '</span>').join('');
  document.querySelectorAll('#wizard .pane').forEach(p =>
    p.classList.toggle('on', p.dataset.pane === tabs[tab]));
  document.getElementById('btn-back').disabled = tab === 0;
  const last = tab === tabs.length - 1;
  document.getElementById('btn-next').style.display = last ? 'none' : '';
  document.getElementById('btn-finish').style.display = last ? '' : 'none';
  if (last) drawConfirm();
}
window.tabGo = n => { tab = n; drawTabs(); };
window.tabStep = d => { tab = Math.max(0, Math.min(visibleTabs().length - 1,
                                                   tab + d));
                        drawTabs(); };
function wizardValues() {
  const form = document.getElementById('wizard');
  const out = {};
  for (const el of form.elements)
    if (el.name) out[el.name] = el.type === 'checkbox' ? el.checked
                                                       : el.value.trim();
  out.accel = out.kvm ? 'kvm' : 'tcg';   // no checkbox (plugin pane): TCG
  delete out.kvm;
  return out;
}
function drawConfirm() {
  const v = wizardValues();
  const anyDisk = DISK_ROWS.some(([k]) => v[k]);
  const w = window.MiraiPlugins.wizard[v.machine];
  if (w && w.confirm) {
    document.getElementById('confirm-table').innerHTML = '<table>' +
      w.confirm(v, wizardHelpers()).map(([k, val]) =>
        '<tr><td style="width:11em;color:#8b9298">' + k + '</td><td>' +
        val + '</td></tr>').join('') + '</table>';
    return;
  }
  const hw = window.MiraiPlugins.hardware[v.machine];
  const desc = hw ? hw(v, { esc }) : {};
  const rows = [['Name', v.name || '(unnamed)'],
                ['Machine type', v.machine],
                ['BIOS', desc.bios || 'compatible'],
                ['Font', v.machine === 'towns' ? '-'
                         : v.font === 'real' ? 'real machine ROM'
                                             : 'compatible'],
                ['Memory', v.memory],
                ['Sound', desc.sound || soundName(v.sound)],
                ['MIDI', midiName(v.midi)],
                ['Acceleration', v.accel === 'kvm'
                                ? 'KVM (Experimental)'
                                                   : 'TCG only'],
                ['Snapshot', v.snapshot ? 'yes' : 'no'],
                ['BIOS', 'compatible'],
                ['Font', v.machine === 'towns' ? '-'
                         : v.font === 'real' ? 'real machine ROM'
                                             : 'compatible'],
                ['Display', v.machine === 'pc9801' ? 'PC-9801 standard'
                            : v.machine === 'towns' ? 'TOWNS'
                            : 'PEGC + GA-98NB'],
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
  // back to the stock panes first: the ids below must exist even if the
  // wizard was last shown for a plugin machine
  document.querySelectorAll('#wizard .pane').forEach(p => {
    if (customPanes.has(p.dataset.pane))
      p.innerHTML = stockPanes[p.dataset.pane];
  });
  customPanes.clear();
  hiddenTabs = new Set();
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
  // remember the stock panes as freshly drawn (the disk lists are live)
  document.querySelectorAll('#wizard .pane').forEach(p => {
    stockPanes[p.dataset.pane] = p.innerHTML;
  });
  // let plugins add their machine types to the (static) create form select
  const msel = document.querySelector('#wizard select[name="machine"]');
  if (msel) {
    window.MiraiPlugins.machines.forEach(m => {
      if (![...msel.options].some(o => o.value === m)) {
        const o = document.createElement('option');
        o.value = m; o.textContent = machineLabel(m);
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
