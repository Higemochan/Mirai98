# pcat GPU 3D モード — 設計 (実装前)

対象: qemu-3dfx が **-vnc に出ず** ホスト X 窓へ GLX 直描き。表示は
weston(headless,GL)+Xwayland:N+x11vnc(loopback)+websockify で吸い出す
(box86 エンジンと同型)。凍結問題は別班解析中=解決前提。

## (a) エンジン化する — machine_argv では無理
core の `start_instance` は非エンジン機に対し `qemu_argv(inst)`(→`-vnc`,
QMP前提)を Popen し、QMP応答をポーリングする。DISPLAY 差替も X スタック起動
フックも無い。`is_running`/`find_pid`/`thumbnail` は全て `MACHINE_ENGINE`
分岐。3dfx は `-vnc`/QMP を持たず GLX+SDL+unix monitor。
→ **`api.add_engine("pcat-gl", {...})`**。契約は box86 と同一:
`on_start(inst)->"started"|失敗文字列` / `on_stop` / `is_up` / `pid` /
`thumbnail` / `on_reset`。media/reset は QEMU の `-monitor unix:...` へ。

## (b) index からの導出 / ライフサイクル
`vnc,ws,qmp,audio = api.ports_of(inst)`、`N = vnc-5900`(box86 と同式で衝突なし)。
- weston: `--socket=wl-pcat-N`、`XDG_RUNTIME_DIR=/run/user/0`
- Xwayland: `:N`、x11vnc: `-rfbport vnc -listen 127.0.0.1`(**`env -u
  WAYLAND_DISPLAY`** 必須=罠)、websockify: `ws`→ブラウザ
- qemu-3dfx: `DISPLAY=:N SDL_VIDEODRIVER=x11`、md5 照合(run-3dfx と同じ)、
  `-monitor unix:<instdir>/mon.sock,server,nowait`、pid を pids.json へ
起動順 weston→(wl待ち)→Xwayland→(X待ち)→x11vnc→websockify→qemu。停止は逆順
(monitor quit→SIGTERM/KILL エスカレーション)。**box86 から流用**: `_sweep_orphans`
(per-instance、`pkill -x` 厳禁)/`spawn`/`_load|_save_pids`/websockify 配線/
`on_stop` の threading+`_stopping` ガード/`thumbnail`(SDL窓を `-id` 追跡し
`import -window`)。Xvfb を weston+Xwayland に差替えるのが唯一の実質差分。

## (c) mesagl.cfg / UI
QEMU の cwd=インスタンス dir にし、on_start が `mesagl.cfg` を生成
(`FpsLimit,<n>` 等)。UI 項目 `fpslimit`(既定60)を add_field で足し、
値変更で cfg 再生成。他 GL 設定も同経路で拡張可。

## (d) 3D と通常(-vnc)の切替 = 別マシンにする
`MACHINE_ENGINE` は **machine 名でregister時に確定**(inst 単位で core QEMU
経路 ↔ エンジン経路を切替える口が無い)。バイナリ/表示/経路が丸ごと違うので
inst のフラグでは破綻。→ **別マシン `pcat-gl`**(通常は既存 `pcat`)。ディスク
棚は dosv 共有、UI/ウィザードは pcat から流用しエンジン項目のみ差替。

## (e) 表示スタック起動失敗時 (2026-09-12 更新: フォールバックあり)
weston/Xwayland/x11vnc/websockify のいずれか未起動なら、c1 指示により **通常
VGA(-vnc)へフォールバック**して起動を続ける: GL ヘルパを畳み、gl_fallback マーカ
に理由を書き、同じ QEMU バイナリを `-vnc host:display,websocket=ws,audiodev=snd`
で起動(＝core コンソールが繋がり音も出る)。ハードウェアカードは「plain VGA over
VNC -- GL unavailable: <理由>」と表示。既定バイナリ不在(Popen OSError)は "failed"
を返し黙って別物には落ちない。起動前に `_sweep_orphans`+`_reap` で前世代の
プロセス/ソケット/ポートを掃除(TERM→5秒→KILL)し、新世代が死にかけの旧サーバに
繋ぐのを防ぐ。ゾンビ(Z)は報告のみ。

## 未確認・実装時に要検証 (2026-09-12 敵対レビュー後)
- GL モードのサムネイル `import -window root`(rootful Xwayland の root)は実機
  未検証(x11vnc が同じ root を読めているので可の見込み)。
- weston/Xwayland/x11vnc のインスタンス並列は index 由来の DISPLAY/ソケット名
  +起動前 sweep/reap で分離(レビューで OK 判定)。実機同時起動は配備後に確認。
- core の usage stats は engine["pid"]=qemu-3dfx の pid で流用。


## 低・注記のみ (2026-09-12 再レビュー、非修正)
- `_reap`/`_kill_pids` の `os.killpg` は各ヘルパが自セッション(start_new_session)で
  pid==pgid であることが前提。孤児が何らかの理由で別 pgid に居ると killpg が取り逃す
  (try/except で握り潰し)。現構成では未観測。
- `pcatgl_hardware` の cfg 判定は毎描画で現世代 pcatgl.log 全体を読む(起動時 .1 退避で
  上限あり=readback 5 秒ログで約 17KB/時)。負荷は軽微につき非修正。
