# pcat-gl 実機検証手順書 (2026-09-12)

対象ブランチ: pcat-plugin (HEAD 585d754 時点)。**配備は 54259(vm-0 再起動窓)、実施は HDD/fc**。
本書は手順のみ。コマンドは特記なき限り CT209 コンテナ内 root で実行(`pct exec 209 -- bash` か
`ssh higem-proxmox` 経由)。既存 VM(vm-0/2/3/4)には触れない。

前提の事実:
- 本番サービス `mirai98.service`: `ExecStart=python3 /opt/mirai98/web/pc98web.py
  --config=/opt/mirai98/web/pc98web.json`、**`--loopback` 無し ⇒ LOOPBACK=False**、`KillMode=process`。
- `--loopback` 無し運用では、ブラウザ用の待受(websockify の ws ポート、フォールバック時の
  QEMU 自身の -vnc)は**全インターフェース待受が正常**(他機と同じ)。隔離対象は
  **x11vnc(-listen 127.0.0.1) と QMP(tcp:127.0.0.1) のみ**。
- qemu-3dfx バイナリは固定せず設定値。既定は build-n
  `/storage/work/kvm98/src/qemu-3dfx-0b399bd-fix/build-n/qemu-system-i386`(md5 fab62154…)。

---

## (1) 配備

配備ファイルと配備先(src → 本番):

| ソース(worktree /storage/work/pcat-wt) | 配備先 |
|---|---|
| src/plugins/pcatgl.py | /opt/mirai98/web/plugins/pcatgl.py |
| src/web/plugins/pcatgl.js | /opt/mirai98/web/ui/plugins/pcatgl.js |
| src/web/plugins/pcat.js | /opt/mirai98/web/ui/plugins/pcat.js (機種切替制限) |
| src/pc98web.py | /opt/mirai98/web/pc98web.py (#24 -nic none 同梱時のみ) |

md5(HEAD 585d754 時点。**再レビュー修正が入れば変わるので配備直前に採取**):
- pcatgl.py = 1f38271a0da37b2f38f5c1747ea9645b
- pcatgl.js = 4bf6c91a6dc3d14c962ed94251fcb54b
- pcat.js  = bc253c24fc4e26b7333eee92a4099627
- pc98web.py = 9f7f40795d01fb6d5d2eb03351a665da

手順:
1. 配備直前に真の md5 を採る: `git -C /storage/work/pcat-wt show 585d754:src/plugins/pcatgl.py | md5sum` 等。
2. 既存ファイルは日付付きで退避(box86.py.pre-… と同流儀): 例 `cp -a <dst> <dst>.pre-pcatgl.$(date +%Y%m%d-%H%M%S)`。
3. コピー後、**配備先の md5 が 1 と一致**することを確認。
4. qemu-3dfx パスを設定に追加: `/opt/mirai98/web/pc98web.json` に
   `"qemu_3dfx": "/storage/work/kvm98/src/qemu-3dfx-0b399bd-fix/build-n/qemu-system-i386"` を入れる
   (JSON 構文維持)。未設定なら既定パスにフォールバックするが、明示が安全。
5. 再起動: `systemctl restart mirai98.service`。**KillMode=process なので稼働中 VM(vm-0 等)は生存**。
   確認: 再起動前後で稼働 VM の QEMU pid が不変(`pgrep -af qemu-system-i386`)。
6. サービス起動確認: `systemctl is-active mirai98.service` = active、`journalctl -u mirai98 -n30` にエラー無し。
7. プラグイン読み込み確認: ブラウザで機種一覧に「DOS/V PC + 3dfx (GL)」(badge 3dfx)が出る、
   または `curl -s localhost:58099/api/plugins`(要ログイン)に pcatgl.js。

合否記入: (1) 配備 md5 一致 [ ] / 稼働VM生存 [ ] / サービス active [ ] / 機種一覧に出現 [ ]

---

## (2) 使い捨て pcat-gl 機の作成

1. テスト用ディスクを dosv hdd 棚へコピー(原本を汚さない):
   `cp --reflink=auto /storage/work/kvm98/DOSV4300-stage2 /storage/pc98/disks/dosv/hdd/pcatgl-livetest.img`
   (raw イメージ。drive_backing が format=raw と判定)。
2. Web UI の作成ウィザードで機種「DOS/V PC + 3dfx (GL)」を選び:
   - Hard disk = pcatgl-livetest.img
   - Frame limit = 60(既定)
   - Network = Isolated(net="")
   - Snapshot = **on**(原本破棄、検証を繰り返せる)
   - 名前 = pcatgl-test 等
3. index は manager が空き番号を自動採番(既存 0/2/3/4 を避けた最小空き)。作成後
   `cat /storage/pc98/vm/vm-<N>/vm.xml` で machine=pcat-gl / net 空 / snapshot=true を確認。

合否記入: (2) 作成成功 [ ] / vm.xml 期待通り [ ]

---

## (3) 起動時の確認点(GL 経路)

起動: UI の「起動」。数秒後、以下を確認(N = 5900 引く前の DISPLAY 番号 = vnc-5900、
vnc=5920+index、ws=5830+index、qmp=4820+index)。

1. ヘルパの pid: `cat /storage/pc98/vm/vm-<N>/pcatgl/pids.json`
   → weston / xwayland / x11vnc / websockify / qemu の 5 つが揃う。
2. 待受アドレス: `ss -ltnp`
   - x11vnc(rfbport 5920+index) が **127.0.0.1 のみ** [必須]
   - QMP(4820+index) が **127.0.0.1 のみ** [必須]
   - websockify(5830+index) は 0.0.0.0(全 IF)= 正常(ブラウザ用)
3. QEMU cwd と cfg: `ls -l /proc/<qemu-pid>/cwd` → /storage/pc98/vm/vm-<N>/pcatgl。
   `cat /storage/pc98/vm/vm-<N>/pcatgl/mesagl.cfg` →
   `FpsLimit,60` / `AlwaysBlit,1` / `SwapWatchdogSec,5` / `ReadbackPresent,1`。
4. ログ退避: 一度停止→再起動して `ls /storage/pc98/vm/vm-<N>/pcatgl/` に
   `pcatgl.log` と `pcatgl.log.1` の両方があること(起動時 .1 退避)。
5. QEMU argv に `-display sdl`・`-nic none`・`-M pc-i440fx-9.2,acpi=on,accel=kvm:tcg`・
   `-qmp tcp:127.0.0.1:<qmp>`(0.0.0.0 でない)を確認: `tr '\0' ' ' < /proc/<qemu-pid>/cmdline`。

合否記入: (3) 5ヘルパ [ ] / x11vnc・QMP 127.0.0.1 [ ] / cwd+cfg 内容 [ ] / log .1 退避 [ ] / argv [ ]

---

## (4) ブラウザコンソールで 3D 表示

1. UI でコンソールを開く → **Win98 デスクトップ**が表示(マウス捕捉は Ctrl+End で解放)。
2. glchecker(または SoftGPU 添付の 3D テスタ)を起動。
3. ハードウェアカードを再表示 → 行「mesagl.cfg」が **「mesagl.cfg applied」** に変わる
   (3D コンテキスト生成後。それ以前は「not yet confirmed (3D not used yet)」が正常)。
   併せて `grep -a "FpsLimit \[\|ReadbackPresent enabled" /storage/pc98/vm/vm-<N>/pcatgl/pcatgl.log`。
4. Blendermark(等の 3D デモ)を実行し、**動いて見える**ことを fc の xwd 実像判定で確認:
   `DISPLAY=:<N> import -window root /tmp/f1.xwd.png; sleep 1;
    DISPLAY=:<N> import -window root /tmp/f2.xwd.png` → 2 枚が**異なる**(静止画でない=描画更新)。
   FPS はカード/ログの値と体感が整合すること。

合否記入: (4) デスクトップ [ ] / applied 表示 [ ] / Blendermark 動作(xwd 差分) [ ]

---

## (5) 2 インスタンス同時起動(分離)

1. 手順(2)をもう 1 台(別ディスクコピー、別名)作成。index は別値になる。
2. 両方起動し、次が**別々**であること:
   - DISPLAY(:20+i)、wl ソケット(`ls $XDG_RUNTIME_DIR` に wl-pcatgl-<i1>/<i2>)、
     rfbport/ws/qmp ポート、pcatgl/vm-<N> ディレクトリ。
   - `ss -ltnp` に両者の x11vnc/websockify/QMP が別ポートで並ぶ。
3. 両コンソールを別タブで開き、片方の操作がもう片方に漏れないこと。

合否記入: (5) DISPLAY/ソケット/ポート分離 [ ] / コンソール独立 [ ]

---

## (6) フォールバック試験

**重要**: 実装のフォールバック(通常 VGA+理由表示)は **表示スタック(weston 等)の起動失敗**で発火する。
**qemu-3dfx バイナリ不在は fallback ではなく "failed" を返す**(黙って別物に落ちない設計)。
両方を別々に試す。

(6a) 表示スタック失敗 → VGA フォールバック[本命]:
1. 一時的に weston を起動不能にする(例: `mv /usr/bin/weston /usr/bin/weston.hidden`。**検証後必ず戻す**)。
2. pcat-gl 機を起動 → on_start が weston ソケット待ちで失敗し、通常 VGA(-vnc)経路で起動継続。
3. 確認: ハードウェアカードの Display 行が
   **「plain VGA over VNC -- GL unavailable: weston compositor socket never appeared」**。
   `cat /storage/pc98/vm/vm-<N>/pcatgl/gl_fallback` に理由。
   **ブラウザコンソールが繋がり**、Win98 が VGA で表示(GL 無し・音あり)。
   `ss -ltnp` に weston/Xwayland/x11vnc は無く、QEMU 自身の -vnc(5920+index、全 IF)+websockify。
4. `mv /usr/bin/weston.hidden /usr/bin/weston` で復旧。

(6b) バイナリ不在 → クリーンな失敗[副検証]:
1. pc98web.json の qemu_3dfx を存在しないパスに一時変更し restart。
2. 起動 → 結果は **"failed: …"**(fallback しない)。ログに Popen の OSError。
   ゴミプロセスが残らない(pids.json 空 or 掃除済)。
3. 設定を戻して restart。

合否記入: (6a) 理由表示 [ ] / コンソール接続 [ ] / GLヘルパ不在 [ ]  (6b) failed 返却 [ ] / 残置なし [ ]

---

## (7) 停止と孤児掃除

1. 稼働中の pcat-gl 機を UI「停止」。
2. 数秒後(QEMU は quit→最大15秒猶予→他ヘルパ TERM→5秒→KILL):
   - `pgrep -af "wl-pcatgl-<i>\|Xwayland :<N> \|rfbport 592<..>\|tcp:127.0.0.1:482<..>"` → **全消滅**。
   - `cat /storage/pc98/vm/vm-<N>/pcatgl/pids.json` → **{}**(空)。
   - SHM/ソケット残置なし: `ls $XDG_RUNTIME_DIR/wl-pcatgl-<i>` 無し、`ls /tmp/.X11-unix/X<N>` 無し、
     `ls /dev/shm | grep -i pcatgl` 無し。
3. 孤児掃除の確認(前世代残置の回収): 停止直後にもう一度起動 → on_start の sweep+reap が
   前世代の残骸を掃除してから新規起動する(ポート衝突で "exited during startup" が出ないこと)。
   ログに `[pcatgl] swept orphan pid …` が出れば掃除が働いた証跡。

合否記入: (7) 全プロセス消滅 [ ] / pids.json 空 [ ] / SHM・ソケット残置なし [ ] / 再起動でポート衝突なし [ ]

---

## (8) 総合合否

| 項目 | 合 | 否 | 備考 |
|---|---|---|---|
| (1) 配備・稼働VM生存 |  |  |  |
| (2) 機作成 |  |  |  |
| (3) 起動時の待受/cwd/cfg/log |  |  |  |
| (4) 3D 表示・applied |  |  |  |
| (5) 2 台分離 |  |  |  |
| (6a) VGA フォールバック |  |  |  |
| (6b) バイナリ不在=failed |  |  |  |
| (7) 停止・孤児掃除 |  |  |  |

実施者: ______  日付: ______  総合: 合 / 否(要修正: ____________)
