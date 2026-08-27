# qemu-pc98 解剖 — FM TOWNS 実装 (qemu-towns) の構造テンプレート

qemu-pc98 (`781172abba`, QEMU 11.0.93 ベース) が PC-98 を QEMU にどう実装したかの調査記録 (2026-08-10)。
qemu-towns はこの構造をそのまま踏襲する。

## 要点サマリ

- **追加コード ≈ 42,000 行に対し、upstream 既存ファイルへの侵襲は ≈ 30 行** (+meson/Kconfig 宣言 30 行弱)。CPU コア・MemoryRegion・メインループ・UI/audio バックエンドへの変更は皆無 → 本家 QEMU に追従リベース可能な構造
- マシンは `TYPE_X86_MACHINE` 直派生で **`hw/i386/pc.c` (AT互換機) を一切使わない**。共有は `x86-common.c` のみ (microvm と同じパターン)
- デバイスは `TYPE_ISA_DEVICE` + `isa_register_portio_list` + `MemoryRegionPortio` テーブルで統一
- 互換 BIOS はアセンブリソースごと `roms/pc98bios/` に同梱 (gcc -m16)、blob は `pc-bios/*.bin`

## 1. マシンタイプ定義

- 中核: `hw/i386/pc98.c` (621行)。`Pc98MachineState` (:95) が `X86MachineState` を継承、`Pc98MachineClass` (:107) は has_pci / has_wab / has_coregraph / pegc_post_compat の4フラグで機種バリアント (`pc9801` / `pc9821`) を分岐。TypeInfo 登録 :600-621 (`TYPE_PC98_MACHINE` は abstract)
- デバイス生成本体 `pc98_devices_init` :292-479、machine init :481-508、reset :510-519
- `mc->max_cpus = 1`、default CPU = 486、`block_default_type = IF_IDE`、irqchip は強制ユーザ空間 (:531-534)
- ボード直結 I/O (A20、ソフトリセット、ストラップ、0x8900 shutdown) は machine object 自身が `PortioList` で保持 (:267-280, 476-478)
- IRQ/DMA マップは :68-93 のコメントブロックに一覧 (towns.c でも同形式で残す)
- ビルド: `hw/i386/meson.build:19` — `CONFIG_PC98 → x86-common.c, pc98.c, pc98-mem.c, pc98-pcihost.c`

→ **towns 版は `hw/i386/towns.c` + `towns-mem.c` の2ファイル + TYPE_X86_MACHINE 直派生が素直**

## 2. デバイスモデル一覧 (実装規模つき)

| 機能 | ファイル | 行数 | 形態 |
|------|---------|-----:|------|
| 表示 (GDC/GRCG/EGC/PEGC) | hw/display/pc98-vga.c | 5118 | 非qdev (portio_list + vmstate直登録) |
| FDC (uPD765A) | hw/block/pc98-fdc.c | 3078 | ISA_DEVICE + 独自 FLOPPY98_BUS |
| WAB (Cirrus GD5428) | hw/display/pc98-wab.c | 2933 | ISA_DEVICE |
| メモリコントローラ/ROMバンク | hw/i386/pc98-mem.c | 1300 | 非qdev (機能関数) |
| SCSI (WD33C93) | hw/scsi/pc98-scsi.c | 1299 | ISA_DEVICE |
| Core-Graph (PCI Cirrus) | hw/display/pc98-coregraph.c | 970 | PCI_DEVICE |
| FM音源 (YM2608 OPNA) | hw/audio/pc98-opna.c | 805 | ISA_DEVICE |
| システムポート/カレンダ | hw/misc/pc98-sys.c | 667 | ISA_DEVICE |
| DMA (uPD71037) | hw/dma/pc98-dma.c | 651 | ISA_DEVICE + TYPE_ISADMA 実装 |
| キーボード (8251) | hw/input/pc98-kbd.c | 585 | ISA_DEVICE |
| IDE | hw/ide/pc98-ide.c | 573 | ISA_DEVICE |
| PIT (8254 PC-98配置) | hw/timer/i8254-pc98.c | 539 | TYPE_PIT_COMMON 派生 |
| RS-232C (uPD8251) | hw/char/pc98-serial.c | 533 | ISA_DEVICE |
| バスマウス (uPD8255) | hw/input/pc98-mouse.c | 438 | ISA_DEVICE |
| PCIホストブリッジ | hw/i386/pc98-pcihost.c | 353 | PCI_HOST_BRIDGE |
| WSS (CS4231A) | hw/audio/pc98-wss.c | 264 | ISA_DEVICE (cs4231a を子に) |
| LGY-98 (NE2000系) | hw/net/pc98-lgy98.c | 217 | ISA_DEVICE |
| PIC 配線 (8259A×2) | hw/intc/i8259-pc98.c | 98 | 型追加なし。共有 I8259 を cascade-irq=7 で再利用 |

- 音源コアは MAME OPN (`fmopn.c` 4658行) + `ymdeltat.c` + `emu2149.c` を新規取り込み (計6087行)
- 重心は表示系 (計9021行 = C コードの43%) と FDC。PIC/PIT/DMA/KBD 等「配置替えだけ」のデバイスは 100〜650 行と軽い

### 実装パターン (towns デバイスで踏襲するもの)

- (a) 標準形: `isa_register_portio_list` + `MemoryRegionPortio` テーブル — pc98-sys.c:506-535 がテンプレ (奇数ポート・2バイトストライドもテーブルで平坦化)
- (b) ストライド展開: 1本の MMIO に `isa_register_ioport` でエイリアスを貼る — i8254-pc98.c:462-473
- (c) VRAM: `memory_region_init_io` の**純 MMIO** + サイズ別ディスパッチをマクロ生成 — pc98-vga.c:5019-5049。ダーティ管理は自前ビットフラグ (memory_region_set_dirty 不使用)
- (d) DMA: `IsaDmaClass` (hold_DREQ/register_channel/read_memory/write_memory/schedule) を実装すれば FDC/SCSI は upstream 汎用 API のままで済む — pc98-dma.c:338-521
- (e) 割込み配線: `qdev_connect_gpio_out` — pc98.c:331 (KBD→IRQ1) 等

## 3. BIOS / ROM ロード

- `mem_load_firmware()` (pc98-mem.c:860-945) が `qemu_find_file(QEMU_FILE_TYPE_BIOS,...)` + `load_image_size()` で pc98bank0..7 / itf / bios / pci / basic / ide をロード → `memory_region_init_rom` (:1092)
- 実機 ROM ダンプ対応のバイナリパッチ機構あり: ITF 自己診断無効化 (:780-833)、チェックサム再計算 (:835-846)
- フォント (pc98font.bin 288KB) は pc98-vga.c:4637-4659 で別ロード
- **互換 BIOS のフルソースが `roms/pc98bios/` に直接コミットされている**: bios.S (3862行) / itf.S / ide.S / scsi.S / pci.S / basic.S、`gcc -m16 -march=i486` + objcopy でビルド、blob を `pc-bios/` へコピー (Makefile の qemu-blobs ターゲット)。フォントは GNU Unifont から `makefont.py` で生成 (BDF 自体は未収録)

→ **towns 版**: 当面は実機 ROM (FMT_SYS.ROM 等) 前提で「ROM ロード + 必要ならパッチ」の枠だけ真似る。互換 BIOS 自作は将来課題 (roms/townsbios/ の枠を用意)

## 4. VRAM / 描画

- 接続点は `qemu_graphic_console_create(NULL, 0, &ops, s)` の1箇所 (pc98-vga.c:5117)。`GraphicHwOps` は invalidate と gfx_update の2フックのみ (:4555-4558)
- update_display はテキスト/グラフィックを中間バッファに個別レンダしてから合成し、`qemu_console_surface()` に 32bpp 直書き → `qemu_console_update`
- VSYNC は `timer_new_ns(QEMU_CLOCK_VIRTUAL,...)` で周期発火し IRQ2 (:5111)
- 入力は `qemu_input_handler_register()` (kbd:360, mouse:168)

→ towns 版もこの2フック構成。TOWNS はテキストVRAM がなくスプライト+2レイヤ合成なので update_display の中身が主戦場

## 5. upstream への侵襲 (全量)

| 場所 | 内容 |
|------|------|
| target/i386/cpu.{h,c}, helper.c | `pc98-a20-mask` bool プロパティ追加 (A20 を 1MiB ラップにする) — 計~15行 |
| include/hw/isa/i8259_internal.h + hw/intc/i8259.c | `cascade_irq` 一般化 (AT=2/PC-98=7) — 計~5行 |
| hw/ide/core.c:1665-1675 | INITIALIZE DEVICE PARAMETERS 後の IDENTIFY word 54-56 更新 |
| 各 meson.build / Kconfig | 宣言行のみ |

既知の不整合 (触らない/騙されない): `hw/pci-host/pc98-pcihost.c` は**ビルドされない重複コピー** (ビルド実体は hw/i386/ 版、ただし機能は pci-host 版が新しい=移動途中の取り残し)。MAINTAINERS が挙げる fmopn_mamedefs.h / docs/pc98-*.md は存在しない。

## 6. ビルド統合

- 親スイッチ `hw/i386/Kconfig:121-150`: `config PC98` が `default y / depends on I386` で子 (`PC98_VGA` 等 15個) を select → i386-softmmu で自動有効、configs/ への追記不要
- 各サブディレクトリの meson.build に `when: 'CONFIG_PC98_xxx'` 1行ずつ

→ towns 版: `config TOWNS` + `TOWNS_xxx` 子スイッチで同型に

## 7. 音声

- audiodev は QEMU 11 標準 `AudioBackend` API のみ (audio_be_open_out/audio_be_write)。独自バックエンドなし
- **VNC への音声はフォーク独自実装ではなく upstream QEMU 標準の VNC audio 拡張** (ui/vnc.c の audio_capture 系)。Mirai98 の patch_novnc.py はクライアント (noVNC) 側をこの拡張に対応させるパッチ
- ビープは upstream `TYPE_PC_SPEAKER` を流用 (pc98.c:377-390)

→ towns 版: YM2612 (OPN2) + RF5C68 PCM。fmopn.c (OPN 系) が流用/参考にできる可能性が高い。VNC 音声経路はそのまま乗る

## FM TOWNS 実装への設計指針 (このレポートの結論)

1. `hw/i386/towns.c` + `towns-mem.c`、`TYPE_X86_MACHINE` 直派生、`x86-common.c` 共有
2. デバイスは `hw/<subsys>/towns-*.c`、ISA_DEVICE + portio テーブル統一。IRQ/IO マップはコメントブロックで管理
3. upstream への侵襲は最小に (pc98 の A20/cascade_irq に相当する TOWNS 固有 CPU 差分が出た場合のみ)
4. Kconfig: `config TOWNS` 親 + `TOWNS_xxx` 子、i386-softmmu 自動有効
5. 描画は GraphicHwOps 2フック + 純 MMIO VRAM + 自前ダーティフラグ
6. DMA は TYPE_ISADMA 実装で周辺を upstream API に乗せる
7. ROM は qemu_find_file + load_image_size、実機 ROM パッチ機構の枠を用意
8. 音源は fmopn.c の OPN コア資産を検討 (YM2612 は OPN2)、PCM (RF5C68) は新規
