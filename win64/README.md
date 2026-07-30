# QEMU PC-98 Win64 cross build

Debian Linux 上で QEMU PC-98 を Windows x86-64 向けにクロスビルドする
環境です。MSYS2 は不要です。ターゲット用ライブラリは固定したソース
アーカイブから `root/` へ構築し、QEMU は `build/`、配布物は
`$DIST_NAME/`（既定は `qemu-pc98-bin/`）に生成します。

## ディレクトリ

- `../qemu-pc98/`: ビルド対象の QEMU ソース。Mirai98 の submodule なので、
  Live USB と Windows 版が必ず同じ commit から作られます。`QEMU_SOURCE_DIR`
  で別のツリーも指定できます
- `root/`: MinGW-w64 用ヘッダ、import library、DLL
- `deps-src/`, `deps-build/`: 依存ライブラリの展開・ビルド領域
- `build/`: QEMU の out-of-tree build
- `package-assets/`: 配布物に入れる README と `virtpc98.exe`
- `$DIST_NAME/`: 展開済み Windows 配布物
- `$DIST_NAME.zip`: それを格納した配布用 ZIP

`build/`、`root/`、配布物などの生成物は Git の管理対象外です。

## 最短手順

submodule が取得済みなら、ソースを置く作業はありません。

```sh
git submodule update --init      # ../qemu-pc98 がまだ空なら
./build.sh bootstrap
./build.sh all
```

`bootstrap` は Debian の MinGW-w64 POSIX-thread toolchain とホスト側
ビルドツールを `sudo apt-get` で導入します。`all` は依存ライブラリ、
QEMU、配布物、静的検証を順番に実行します。

個別のコマンドは次のとおりです。

```sh
./build.sh deps       # 固定ソースを検証し、root/へ依存ライブラリを構築
./build.sh qemu       # i386 / x86_64 の console版とGUI版を構築
./build.sh dist       # $DIST_NAME/ と同名のZIPを生成
./build.sh verify     # PE形式、SHA-256、DLL依存の閉包を検証
```

## リリースの発行

qemu-pc98 のリリースとして配る ZIP は、この木から作って上げます。

```sh
./build.sh release rev11
```

`all` を公開名で通し、`qemu-11.0-pc98-rev11/` と同名の ZIP を作ります。
バージョン部分は submodule の `VERSION` から取るので手で書きません。
**アップロードはしません**。最後に実行すべき `gh release` コマンドを
表示して終わるので、公開は必ず人が判断する一手になります。

ZIP に入る `virtpc98.py` は `../src/virtpc98.py` です。qemu-pc98 側は
上流 QEMU へのマージ用リポジトリなので Python は置かず、Mirai98 の木が
唯一の管理場所になります。

`JOBS=8` のように並列数を指定できます。既定値はメモリ枯渇を避けるため
CPU 数にかかわらず最大 16 です。

```sh
JOBS=8 ./build.sh all
DOWNLOAD_ONLY=1 ./build.sh deps
```

後者は固定アーカイブのダウンロードと SHA-256 検証だけを行います。
一度ダウンロードした後の構築では、QEMU/Meson による暗黙のネットワーク
取得を禁止しています。

## ランチャー資材

`package-assets/` には次の2つを置きます。

```text
package-assets/
  README.txt      配布物に同梱する説明（Git 管理対象）
  virtpc98.exe    Windows側で生成（Git 管理対象外）
```

`virtpc98.exe` は `virtpc98.py` をPyInstallerの `--onefile --windowed`
で生成したものです。Linux では作れないので、`dist` を回す前に Windows
機から持ってきて置きます。`PACKAGE_ASSET_DIR=/path/to/assets
./build.sh dist` のように別ディレクトリも指定できます。いずれかが無ければ、
不完全な配布物を生成せず `dist` が失敗します。

`virtpc98.py` はここには置きません。`../src/virtpc98.py` から直接コピー
するので、木の中に写しが1つだけある状態を保てます。

## ビルド内容

`versions.conf` に以下を URL、バージョン、SHA-256 付きで固定しています。

- zlib、libffi、PCRE2、GNU libiconv
- GLib と Windows 用 proxy-libintl
- Pixman、SDL2、libslirp、libusb
- QEMU が指定する keycodemapdb revision

QEMU は `i386-softmmu` と `x86_64-softmmu` の両方を対象にし、各ターゲット
について console 版と console window を開かない GUI 版を生成します。
TCG、SDL2、Pixman、DirectSound、libslirp user networking、libusb を有効に
しています。GTK、GIO、Rust、tools、guest agent、documentation など配布に
不要な機能は無効です。WHPX は両ターゲットで共通の構成とするため、現状は
明示的に無効化しています。

配布物には両ターゲットの4本のQEMU実行ファイルを含めます。ランチャーは
`qemu-system-x86_64.exe` を優先し、見つからない場合だけi386版へ
フォールバックします。互換ROMは `$DIST_NAME/share/pc98bios/` に
まとめます。

`$DIST_NAME/DLL-DEPENDENCIES.txt` には PE import table を再帰的に
解析した結果が
記録されます。Windows 標準 DLL の許可リスト外に未解決 DLL があれば
`dist` または `verify` は失敗します。`BUILD-INFO.txt` には QEMU commit、
compiler、依存バージョンを記録し、`SHA256SUMS` は配布物内の全ファイルを
検証します。

## ソース更新

submodule を別の commit に移した場合は、古い Meson configuration を
混在させないため `build/` を削除してから `./build.sh qemu` を実行して
ください。`build/` には configure 時の絶対パスが記録されるので、木ごと
別の場所へ複写したときも同様に消す必要があります。依存バージョンを変更する場合は
`versions.conf` の URL と SHA-256 を更新し、該当する
`root/.stamps/<name>-<version>` が残っていない状態で `deps` を実行します。

## Windows での使用

配布物には NEC の proprietary PC-98 BIOS ROM を含めません。Xa7/C9W BIOS
などを別ディレクトリに用意し、`-L` で指定します。

```bat
qemu-system-x86_64w.exe -M pc9821 -m 64M -L bios-xa7c9w ^
  -drive if=ide,bus=0,unit=0,format=raw,file=win95-aclmm-test.raw
```

問題調査では console 版の `qemu-system-x86_64.exe` を使うとエラー出力を
確認できます。`qemu-system-x86_64w.exe` は console window を開かない
GUI 用実行ファイルです。

## Linux 上での任意スモークテスト

Wine がある場合は、配布ディレクトリを current directory にして次を
実行できます。このテストは DLL のロード、QEMU 初期化、PC-98 machine
登録を確認するもので、ゲスト OS の完全な動作試験ではありません。

```sh
cd qemu-pc98-bin        # release で作った場合はその名前のディレクトリ
wine ./qemu-system-i386.exe --version
wine ./qemu-system-i386.exe -machine help
wine ./qemu-system-x86_64.exe --version
wine ./qemu-system-x86_64.exe -machine help
```
