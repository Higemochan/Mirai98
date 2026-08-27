# FM TOWNS ハードウェア目録と qemu-towns 実装計画

Tsugaru (`~/towns-env/tsugaru`) ソースの調査結果 (2026-08-10) を、[qemu-pc98 構造テンプレート](qemu-pc98-anatomy.md) に対応付けたもの。
Tsugaru のパスはすべてそのリポジトリルートからの相対。

## 1. デバイス目録 → QEMU ファイル対応表

| FM TOWNS ハード | チップ | Tsugaru 参照 (行数) | qemu-towns 実装先 (案) | QEMU 既存資産 |
|----------------|--------|---------------------|------------------------|---------------|
| PIC ×2 | i8259A | pic/pic.cpp (482) | hw/intc/i8259-towns.c | **hw/intc/i8259.c 流用** (pc98 が cascade_irq 一般化済み。ポートを 0x00/0x02/0x10/0x12 へ) |
| タイマ ×2 | i8253 | timer/timer.cpp (557) | hw/timer/i8254-towns.c | **hw/timer/i8254 流用** (i8254-pc98.c と同型の TYPE_PIT_COMMON 派生。0x40-/0x50- + 割込制御 0x60) |
| DMAC | μPD71071 | dmac/dmac.cpp (558) | hw/dma/towns-dma.c | **新規** (i8237 非互換)。TYPE_ISADMA 実装で周辺を upstream API に乗せる |
| CRTC + パレット + VSYNC | TOWNS専用 (32reg) | crtc/crtc.cpp (2261), crtc.h:344-400 (タイミング) | hw/display/towns-crtc.c | 新規。GraphicHwOps 2フック構成 |
| スプライト | TOWNS専用 (1024枚/16x16) | sprite/sprite.cpp (640) | hw/display/towns-sprite.c | 新規 |
| VRAM/メモリコントローラ | — | memory/physmem.cpp (1512), memaccess.cpp (753) | hw/i386/towns-mem.c | pc98-mem.c と同型 (純MMIO + エイリアス) |
| CD-ROM (CDC) | TOWNS内蔵 | **cdrom/cdrom.cpp (2045)** | hw/misc/towns-cdc.c (or hw/scsi/ 配下) | ディスク層は QEMU blockdev に置換 (Tsugaru discimg 1581行は不要) |
| キーボード | シリアルI/F | keyboard/keyboard.cpp (705) + keytrans (305) | hw/input/towns-kbd.c | qemu_input_handler_register |
| ゲームポート | パッド/マウス | gameport/gameport.cpp (880) | hw/input/towns-pad.c | 同上 |
| FDC | MB8877A (WD1793互換) | fdc/fdc.cpp (755) | hw/block/towns-fdc.c | WD1793 系の知見流用可 |
| SCSI | MB89352系 | scsi/scsi.cpp (2003) | hw/scsi/towns-scsi.c | Phase 3 はスタブで可 |
| FM音源 | **YM2612 (OPN2)** | src/ym2612/ (2778) | hw/audio/towns-opn2.c | **qemu-pc98 の fmopn.c (MAME OPNコア) が OPN2 対応か要調査** — 対応なら大幅短縮 |
| PCM音源 | RF5C68 | src/rf5c68/ (552) | hw/audio/towns-rf5c68.c | 新規 (小さい) |
| RTC | BCDレジスタ式 | rtc/rtc.cpp (202) | hw/rtc/towns-rtc.c | 新規 (小さい) |
| CMOS 8KB | I/O 0x3000-0x3FFF | townscmos.cpp (1037, 既定イメージ defCMOS[] 含む) | towns-mem.c 内 or hw/misc/towns-cmos.c | **既定 CMOS イメージをそのまま移植するのが最短** |
| システム系 (リセット要因/マシンID/フリーランタイマ/FASTモード) | — | townsio.cpp (towns.cpp 内 I/O) | hw/misc/towns-sys.c | pc98-sys.c と同型 |
| RS-232C | i8251 | serialport/ (727) | hw/char/towns-serial.c | 後回し |
| MIDI / 高品位PCM / LAN | FMT-40x / REX-3586 | midi/ highrespcm/ lan_rex3586/ | — | ゲーム互換にほぼ不要、当面対象外 |

**移植不要 (Tsugaru のホスト連携機構)**: vndrv, tgdrv, eventlog, outside_world, render, townsapp_*.cpp

**バス設計ノート**: TOWNS に ISA バスは無いが、qemu-pc98 は portio 登録の担体として TYPE_ISA_DEVICE を使っている (PC-98 も AT 互換ではない)。qemu-towns も同じ便法を踏襲する。

## 2. 割込み / DMA / 主要 I/O

```
IRQ0 TIMER   IRQ1 KEYBOARD  IRQ2 RS232C  IRQ6 FDC  IRQ7 PIC_BRIDGE
IRQ8 SCSI    IRQ9 CDROM     IRQ11 VSYNC★ IRQ13 SOUND
DMA: CH0=FDD CH1=SCSI CH2=PRINTER CH3=CDROM★
```

I/O マップの原典: `src/towns/townsdef/townsdef.h:260-721` (登録は towns.cpp:719-1023)。
主要どころ: PIC 0x00/0x02/0x10/0x12、タイマ 0x40-/0x50-/0x60、フリーランタイマ 0x26、マシンID 0x30、
DMAC 0xA0-0xAF、FDC 0x200-0x20E、CRTC 0x440-0x44C、高解像CRTC 0x470- (MX以降)、スプライト 0x450/0x452、
SysROM/DicROM切替 **0x480★**、CD-ROM 0x4C0-0x4CD、パッド 0x4D0-0x4D6、YM2612 0x4D5-0x4DE、
RF5C68 0x4E7-0x4F8、VSYNC クリア **0x5CA★**、FASTモード 0x5E0-0x5EC、キーボード 0x600-0x604、
SCSI 0xC30-0xC34、CMOS 0x3000-0x3FFF、パレット 0xFD90-、VSYNC状態 0xFDA0/0xFF86、漢字ROM 0xFF94-。

## 3. メモリマップ (32bit 機。386SX 系/Marty は別マップ)

| アドレス | 内容 |
|---------|------|
| 0x00000000- | メインRAM (既定4MB、最大64MB)。C0000-CFFFF に FMR互換VRAM/CVRAM、F8000-FFFFF は SYSROM末尾/RAM 切替 (I/O 0x480) |
| 0x80000000 / 0x80100000 | VRAM Layer0 / Layer1 (各512KB) |
| 0x81000000 | スプライトRAM 128KB |
| 0xC2000000 | FMT_DOS.ROM 512KB |
| 0xC2080000 | FMT_DIC.ROM 512KB |
| 0xC2100000 | FMT_FNT.ROM 256KB |
| 0xC2140000 | ネイティブ CMOS 8KB |
| 0xFFFC0000 | **FMT_SYS.ROM 256KB (リセットベクタ)** |

必須 ROM (サイズ厳密): FMT_SYS.ROM (256KB) / FMT_DOS.ROM (512KB) / FMT_FNT.ROM (256KB) / FMT_DIC.ROM (512KB)。任意: FMT_F20.ROM, MYTOWNS.ROM。原典: physmem.cpp:389-540。

## 4. 実装で踏み抜きやすい罠 (Tsugaru/Errata の知見)

- **I/O 0x480**: SYSROM は RAM テスト前に自分のマッピング (F8000) を外す。これが無いと POST 即死。bit0 の挙動と REIPL 時の CMOS 破壊問題は tsugaru readme.md:565-575 と physmem.cpp:819-827 参照
- **CMOS チェックサム** (0x3148/0x314A/0x33CE): 不整合だと SYSROM が CMOS を初期化。ブートデバイス設定は 0x3182/0x3C28 (CD=0x80)
- **Databook の誤記** (FMTOWNS_Technical_Databook_Errata.md 必読): FDC の IMSK/HDISEL/DSKCHG は**極性が逆**、パッドのビット0/1逆、スプライトBUSYはVSYNC**開始**と同時、YM2612 内部クロック667KHz 等
- **VSYNC**: 周期 0x1000000 ns (≒59.6Hz、2の冪で剰余高速化)、VSYNC期間 60μs、IRQ11、**I/O 0x5CA 書込でクリア**。Tsugaru は10μs刻みポーリングで生成 (crtc.h:344-400) — QEMU では timer_new_ns 2本 (立上げ/立下げ) で置換
- **CPU**: unreal mode (リアルモード復帰でセグメント limit 維持) が必須 — QEMU TCG の既存挙動で満たされる見込み (要検証)。無印用の「386DX に見せるマシンID偽装」は機種プロパティで
- **速度感受性**: FASTモード (I/O 0x5EC + メモリウェイト設定で 25MHz↔5MHz 切替) と Tsugaru のアプリ別速度設定 (townsdef.h:1144-1180 に約20タイトル) が示す通り、**CPU 速度制御は互換性の核心**。TCG では icount/クロック調整で対応、KVM は速度非依存ソフト向けオプション扱い

## 5. マイルストーン (Phase 0-4)

- **Phase 0 骨格**: メモリマップ + ROM ローダ + I/O 0x480 + CMOS(既定イメージ) → CPU が SYSROM を実行開始
- **Phase 1 POST 通過**: PIC/タイマ (QEMU 既存流用) + フリーランタイマ + マシンID + RTC + FASTモードレジスタ + **DMAC (新規)**
- **Phase 2 画面**: CRTC (まず VSYNC IRQ + 状態レジスタのみ) + VRAM + パレット + 漢字ROM + FMR互換VRAM
- **Phase 3 = マイルストーン1: CD ブート成立**: CDC (まず SEEK/MODE1READ/TOCREAD/SETSTATE の4コマンド + DMA CH3 + IRQ9) + キーボード (ブートキー) + スプライト (BUSYビット) + FDC/SCSI スタブ
- **Phase 4 実用**: YM2612 + RF5C68 (音源初期化失敗で止まるゲーム多数 → 実質優先度高)、パッド、SCSI HDD、CDDA、速度調整

規模見積り: CD ブート最小セットは Tsugaru 該当部 ≈ 9,000-10,000 行、QEMU 既存資産 (PIC/PIT/blockdev) で置換できる分を引くと **正味新規 ≈ 7,000 行**。qemu-pc98 全体 (≈42,000行) と比べても妥当なスコープ。

## 6. 機種バリアント戦略

Tsugaru 既定は MX (486DX, マシンID 0x0C)。qemu-towns も **最初は `towns` = MX 相当 1 機種**で始め、
qemu-pc98 の `Pc98MachineClass` フラグ方式 (has_pci 等) に倣って後から分岐を足す:
CPU種別 / メモリマップ系統 (32bit / 386SX / Marty) / マシンID / 高解像CRTC有無 (MX以降) / CD2倍速 (MX以降) / SCSI有無 (Marty無し)。
Tsugaru では機種差分が各デバイスにポイント分岐で散在 (physmem.cpp:627-768, crtc.cpp:1509-, cdrom.cpp:186- 等) — MachineClass プロパティに集約し直す。
