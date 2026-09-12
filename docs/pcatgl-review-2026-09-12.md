# pcat-gl 敵対レビュー (874c1dd / 67fdb23) + 980a4a7 — 2026-09-12

対象: /storage/work/pcat-wt (branch pcat-plugin)。静的読解+読み取り専用の確認のみ(VM/スタック起動なし、配備なし)。
構文: node --check pcatgl.js/pcat.js OK、ast.parse pcatgl.py OK。既定バイナリ build-n は実在(md5 fab62154…、本番バイナリではない)。

## 判定
- **pcat-gl (874c1dd+67fdb23): 修正後配備。** 高1〜3 と中4〜8 を直してから。設計の骨格(別エンジン、index 由来の DISPLAY/ポート、cwd 固定+cfg 毎回生成、pcat⇔pcat-gl 限定の切替、稼働中編集は core が 409 で拒否)は妥当。
- **980a4a7 (pc98 -nic none): 配備可。** 本番 pc98web.py(md5 e822dc1f) との差分はこの 7 行のみ(branch md5 9f7f4079)=単一ファイルコピーで配備できる。

## 高
1. **フォールバック(-vnc)にコンソールが繋がらない** — pcatgl.py `_qemu_argv` gl=False: `-vnc host:display` に `websocket=<ws>` が無く、フォールバック時は `_kill_pids(pids)` で websockify も落とす。core のコンソールは ports_of の ws(5830+i) に接続するので誰も居ない。`on_start` の待ちも `_port_open(ws)` を待つため必ず 10 秒待って "started (slow to come up)"。音も無い(audiodev=snd 無し)。→ pcat と同じ `-vnc %s:%d,websocket=%d,audiodev=snd`。
2. **QMP が 0.0.0.0 に開く** — `_qemu_argv`: `"-qmp","tcp:%s:%d,server,nowait" % (host, qmp_port)`、host は `api.LOOPBACK` が偽なら "0.0.0.0"。本番 ExecStart(/etc/systemd/system/mirai98.service)は `--loopback` 無し=LOOPBACK False。pcat は常に `tcp:127.0.0.1:%d`。認証無しの QMP が LAN に出る。→ 127.0.0.1 固定(`_qmp_command` も 127.0.0.1 に繋ぐので整合)。
3. **cfg 適用ヘルスチェックが構造的に機能しない** — (a) `_cfg_confirmed` は追記専用の pcatgl.log 全体を読むので、前世代の "FpsLimit [" が残っていれば今回未適用でも True。(b) マーカーは QEMU 起動時ではなく **ゲストがラッパ DLL を読み GL コンテキストを作った時**(ImplMesaGLReset)に出る(実ログ: "mesapt: DLL loaded" 直後に "FpsLimit [ 60 FPS ]"/"ReadbackPresent enabled")。起動直後 5 秒のポーリングは Win98 の起動中に終わるので、正常でもほぼ毎回 "not confirmed" を記録し、本物の取り違えと区別できない。→ 起動時に `log.tell()` のオフセットを保存し、確認は遅延評価(machine_shown の hardware カードで「cfg 適用: 確認済/未確認(まだ 3D 未使用)」を、オフセット以降のログから判定)。

## 中
4. **ドライブが常設でない** — `_drive_args`: CD は inst.cd がある時だけ、FD は fdd1 のみ、fdd2 は無し。pcat は CD(`readonly=on`)と FD 2 台を常に作り、Media 行(POST /media → QMP query-block の removable)で空トレイに挿せる。pcat-gl を空で起動すると挿入不可。pcatgl.js の CD 注記「can be swapped while running」が偽になる。→ pcat の drive ブロックをそのまま流用(readonly=on も)。
5. **snapshot が無視される** — core フィールド(sanitize: `"snapshot": bool(...)`)で、pcatgl.js の編集/ウィザード/カードは「discard changes」を出すが、`_qemu_argv` は `-snapshot` を付けない。原本に書く。→ `if inst.get("snapshot"): argv.append("-snapshot")`。
6. **net=nat が無視される** — UI(PCATGL_NETS)は NAT を選ばせ、カードは "NAT (outbound)" と出すが、backend は常に `-nic none`。→ pcat の nat 分岐(`-netdev user,id=lan -device rtl8139,netdev=lan`)を複製するか、sanitize で net="" に固定して選択肢を消す。
7. **インスタンス dir が配備木の中** — `_inst_dir` = CONFIG["datadir"]/pcatgl/vm-N = /opt/mirai98/pcatgl/vm-N。box86 は `api.inst_dir(inst)`(=/storage/pc98/vm/vm-N)配下(box86.py:380,462)。ログ・pids.json・mesagl.cfg・gl_fallback・weston.log が /opt に書かれ、再配備で消える/持ち出される。→ `os.path.join(api.inst_dir(inst), "pcatgl")`。
8. **孤児掃除が QEMU を捕まえられず、待たない** — `_sweep_orphans` の QMP トークン `"127.0.0.1:%d " % qmp_port` は末尾空白を要求するが argv は `tcp:127.0.0.1:4820,server,nowait`(カンマ続き)、フォールバックなら 0.0.0.0。前世代の QEMU(ディスクを掴んだまま)は掃除されず、新 QEMU はポート衝突で "exited during startup"(音は出るが片付かない)。また SIGTERM を送るだけで終了を待たないため、旧 weston/Xwayland のソケットが残っていて `_wait_for` が即 True → 新 x11vnc/QEMU が死にかけの旧サーバに繋ぐ。→ トークンを `"tcp:127.0.0.1:%d," % qmp_port` に、掃除後は `_kill_pids` と同じ待ち(5 秒→KILL)を入れてから spawn。

## 低 / 注記
9. `_stop_now`: quit → 0.5 秒 → TERM → 5 秒で KILL。raw ディスクへの書き戻し中に KILL される余地(box86 と同形だが QEMU はゲストディスクを持つ)。qemu キーだけ猶予を伸ばす(例 20 秒)か、QMP ポートが閉じるのを待ってから他を落とす。
10. pcatgl.log は全ヘルパ+QEMU が永久追記、ローテ無し(readback の 5 秒ログで約 17KB/時)。起動時に .1 へ退避すれば 3(a) も同時に解決。
11. `api.add_field("vga", …)` は pcat と同名再登録(PLUGIN_FIELDS は名前で上書き、ロード順 pcat.py→pcatgl.py)。中身が同一なので無害だが、片方だけ変えると他方が黙って負ける。
12. pid 再利用: pids.json は再起動を跨いで残り、別プロセスが同じ pid を取ると is_up が真→"already running"。box86 と同形。/proc/<pid>/cmdline にバイナリ名が含まれるかで確認すると堅い。
13. GL モードのサムネイル `import -window root`(rootful Xwayland)は静的には未検証(x11vnc が同じ root を読めているので恐らく可)。
14. 検証済(OK): argv の parity(-M acpi=on,accel=kvm:tcg / -cpu / -m / -rtc / -vga retrace=precise / -bios nopnp / -L ×2 / -nic none / -boot / extra、-display sdl と -vnc の排他)、環境(DISPLAY, SDL_VIDEODRIVER=x11, WAYLAND_DISPLAY 除去、XDG_RUNTIME_DIR=/run/user/0)、cwd=インスタンス dir、weston 13.0.0 の `--backend=headless-backend.so --renderer=gl`(glx-stack.sh 実証・--help 一致)、Xwayland :20+i/-geometry、x11vnc -listen 127.0.0.1 -rfbport 5920+i、websockify ws→127.0.0.1:vnc(box86.py:1843 と同形、--loopback 無し運用では全 IF 待受が必要)、Wayland ソケット名 wl-pcatgl-N、mesagl.cfg の書式/キー(mesagl_impl.c:1660-1666、FpsLimit 0 は非表示=DPRINTF_COND、ReadbackPresent,1 固定なので 0 でも "ReadbackPresent enabled" で拾える)、マーカー文字列(mesapt_mm.c:2356 / mglcntx_linux.c:826)、既定バイナリ不在は Popen OSError→"failed"(黙って別物に落ちない)、機種切替(do_PUT は稼働中 409、sanitize が対象機の sanitizer を再実行、両 picker は pcat/pcat-gl 限定)、fpslimit "0" は文字列で送られ `or ""` を生き残る、hidden kvm→accel=kvm、ウィザードは accel tcg で来るが sanitize が kvm に固定、hardware カードの fallback 理由表示、鍵/秘密のログ出力なし、ROM 追加なし、共有ファイルは pcat.js の picker 絞り込みのみ(pc98/towns/box86 に影響なし)。
15. 設計文書(docs/pcat-gpu-design.md:42)は「silent fallback しない」と書いており、実装のフォールバックは c1 の指示による変更。文書側を更新すること。

## 980a4a7 (pc98: -nic none)
- else 分岐は `if net == "nat"` / `elif net == "bridge"` の後(src/pc98web.py:3415-3429)なので nat/bridge では発火しない。
- net キー無しの vm.xml は `root.findtext("net") or ""`(1467)で ""、sanitize も ""(1600)→ else に入る(None は来ない)。
- towns/pcat は `qemu_argv` 冒頭(3310)で MACHINE_ARGV に委譲されて到達しない、box86 はエンジンで qemu_argv を通らない。
- extra に -netdev がある機体でも `-nic none` は既定 NIC の抑止だけで共存可。
- 本番との diff は当該 7 行のみ → 単一ファイルコピーで配備可。
