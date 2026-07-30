#!/bin/bash

# Mirai98 for Windows: QEMU, the web app and Electron in one zip
#
#   ./build-electron.sh           everything, into dist/mirai98-win64.zip
#   ./build-electron.sh qemu      cross-build Windows QEMU from the submodule
#   ./build-electron.sh sidecar   embeddable CPython + src/ + qemu
#   ./build-electron.sh app       Electron, over the staged sidecar
#   ./build-electron.sh zip       wrap the stage into dist/
#
# QEMU comes from the same qemu-pc98 submodule the Live USB uses, so the
# two products are always the same commit.  There is no installer: the
# zip is unpacked wherever the user likes and keeps its data in
# %LOCALAPPDATA%\Mirai98 regardless.

set -euo pipefail

BASE=$(cd "$(dirname "$0")" && pwd)
CACHE=$BASE/cache
OUT=$BASE/out
DISTDIR=$BASE/dist
STAGE=$OUT/win64
QEMU_SRC=${QEMU_SRC:-$BASE/qemu-pc98}
VERSION=$(date +%Y%m%d)

# The PC-98 machines are in both system emulators; x86_64 is the one that
# gets used, and the one virtpc98 prefers as well.
QEMU_ARCH=${QEMU_ARCH:-x86_64}

# CPython for Windows as a zip that only needs unpacking.  pc98web uses
# nothing outside the standard library, and every extension module it
# does reach for is in here: _ctypes, _socket, _hashlib, _ssl and
# _elementtree.  Pinned, and checked, like every other download.
PYTHON_VERSION=3.13.7
PYTHON_TAG=313
PYTHON_URL=https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-embed-amd64.zip
PYTHON_SHA256=f6cca216a359be84797cabb54149ce5e062afb16cc7567eb7fc51cacb2d86b65

NOVNC_URL=https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz

# Electron, as the prebuilt Windows zip.  Taking it this way rather than
# through npm keeps the whole build to file copying, so it runs here and
# needs no Windows machine and no wine.
ELECTRON_VERSION=v43.2.0
ELECTRON_ZIP=electron-$ELECTRON_VERSION-win32-x64.zip
ELECTRON_URL=https://github.com/electron/electron/releases/download/$ELECTRON_VERSION/$ELECTRON_ZIP
ELECTRON_SHA256=eba5f5088af40ecb364fe258809c79a5234c6ece5a75c64722772eba01b02786

mkdir -p "$CACHE" "$OUT" "$DISTDIR"

[ -f "$QEMU_SRC/configure" ] || {
    echo "no QEMU source in $QEMU_SRC"
    echo "if that is the submodule, check it out first:"
    echo "    git submodule update --init"
    exit 1; }

todo() {
    echo "not built yet: $1"
    echo "  (planned for $2; see ELECTRON-PLAN.md)"
    exit 1
}

# ------------------------------------------------------- Windows QEMU

build_qemu() {
    [ -x "$BASE/win64/build.sh" ] || todo "the mingw cross-build" "phase 2"
    # deps are stamped and skipped once built; qemu checks for itself that
    # the fat98 block driver and qcow1 are in, and dies if they are not
    "$BASE/win64/build.sh" deps
    "$BASE/win64/build.sh" qemu

    local exe=$BASE/win64/build/qemu-system-$QEMU_ARCH.exe
    [ -f "$exe" ] || { echo "no $exe after the qemu stage"; exit 1; }
    # record what it was built from, so it can be held against the
    # version the Live USB stamps into out/payload/opt/mirai98/version
    git -C "$QEMU_SRC" rev-parse --short HEAD > "$OUT/win64-qemu.rev"
    echo "=== windows qemu: $(cat "$OUT/win64-qemu.rev"), $(du -h "$exe" | cut -f1)"
}

# ---------------------------------------------------------- the sidecar

build_sidecar() {
    local exe=$BASE/win64/build/qemu-system-i386.exe
    [ -f "$exe" ] || { echo "no Windows qemu yet; run: $0 qemu"; exit 1; }
    command -v node >/dev/null \
        || { echo "node is needed to check the page: apt install nodejs";
             exit 1; }
    python3 "$BASE/src/check_page.py" "$BASE/src/web"

    # CPython for Windows, as a zip that only needs unpacking.  pc98web
    # uses nothing outside the standard library, so this is enough, and it
    # means the sidecar can be put together here rather than on Windows.
    local zip=$CACHE/python-$PYTHON_VERSION-embed-amd64.zip
    if [ ! -f "$zip" ]; then
        echo "fetching CPython $PYTHON_VERSION for Windows..."
        wget -qO "$zip.part" "$PYTHON_URL"
        echo "$PYTHON_SHA256  $zip.part" | sha256sum -c --quiet \
            || { rm -f "$zip.part"; echo "checksum mismatch"; exit 1; }
        mv "$zip.part" "$zip"
    fi

    rm -rf "$STAGE"
    mkdir -p "$STAGE/resources/sidecar"
    local side=$STAGE/resources/sidecar

    mkdir -p "$side/python"
    unzip -q "$zip" -d "$side/python"
    # the embeddable build reads this instead of guessing, and it is where
    # our own directory has to be named for "import virtpc98" to work
    cat >> "$side/python/python$PYTHON_TAG._pth" <<'EOF'
../src
EOF

    mkdir -p "$side/src"
    cp "$BASE/src/pc98web.py" "$BASE/src/virtpc98.py" "$side/src/"
    cp -r "$BASE/src/web" "$side/src/web"
    cp -r "$BASE/src/drives" "$side/src/drives"
    rm -rf "$side/src/drives/__pycache__"

    # the elevated half of drive work: a small launcher, so the UAC dialog
    # names Mirai98 rather than Python
    x86_64-w64-mingw32-gcc -municode -mwindows -O2         "$BASE/electron/helper/helper.c" -o "$side/mirai98-helper.exe"

    mkdir -p "$side/qemu/share/keymaps"
    # the windowless build too: nothing here wants a console window, and
    # it is the one a person double-clicking would rather have
    cp "$BASE"/win64/build/qemu-system-$QEMU_ARCH.exe \
       "$BASE"/win64/build/qemu-system-${QEMU_ARCH}w.exe "$side/qemu/"
    # the DLL closure, worked out from the PE import tables
    python3 "$BASE/win64/bundle-deps.py" \
        --objdump x86_64-w64-mingw32-objdump \
        --search "$BASE/win64/root/bin" --search "$BASE/win64/root/lib" \
        --dest "$side/qemu" --report "$side/qemu/DLL-DEPENDENCIES.txt" \
        "$side/qemu/qemu-system-$QEMU_ARCH.exe" >/dev/null
    # what -L has to find: the compatible ROMs, and the keymaps the VNC
    # server reads.  Straight from the submodule, as the appliance does.
    cp "$QEMU_SRC"/pc-bios/pc98*.bin "$side/qemu/share/"
    cp "$QEMU_SRC"/pc-bios/keymaps/* "$side/qemu/share/keymaps/"
    # unstripped these are 78 MB each
    x86_64-w64-mingw32-strip --strip-unneeded "$side"/qemu/*.exe \
        "$side"/qemu/*.dll
    # stripping cannot lose a feature, but shipping one that was never
    # built can: without VNC there is no console at all, and the page
    # would come up with a dead screen and nothing to say about it
    # grep -x with its output thrown away, not grep -q: -q closes the pipe
    # on the first match, strings takes SIGPIPE, and pipefail then calls
    # the whole thing a failure
    x86_64-w64-mingw32-strings "$side/qemu/qemu-system-$QEMU_ARCH.exe" \
        | grep -x vnc-ws-listen > /dev/null \
        || { echo "the staged qemu has no VNC: check --enable-vnc in"
             echo "win64/build.sh, since --without-default-features means"
             echo "anything not asked for is simply absent"; exit 1; }

    # patched noVNC, which unlike the stock one asks for the guest's sound
    if [ ! -d "$CACHE/novnc" ]; then
        echo "fetching noVNC..."
        wget -qO "$CACHE/novnc.tar.gz" "$NOVNC_URL"
        tar -C "$CACHE" -xzf "$CACHE/novnc.tar.gz"
        mv "$CACHE/noVNC-1.5.0" "$CACHE/novnc"
        rm "$CACHE/novnc.tar.gz"
    fi
    cp -r "$CACHE/novnc" "$side/novnc"
    python3 "$BASE/src/patch_novnc.py" "$side/novnc"

    # paths here are relative to this file, so the directory can be
    # unpacked anywhere.  What the user creates is not in it: --base says
    # where that goes, because this side may well be unwritable.
    cat > "$side/pc98web.json" <<EOF
{
  "qemu": "qemu/qemu-system-$QEMU_ARCH.exe",
  "datadir": "qemu/share",
  "novnc": "novnc",
  "web": "src/web"
}
EOF

    # inside the sidecar: Electron lays a "version" file of its own at the
    # top of the stage, and would overwrite one put there
    { echo "Mirai98 $VERSION"
      echo "QEMU $(git -C "$QEMU_SRC" rev-parse --short HEAD)"
      echo "CPython $PYTHON_VERSION"
      echo "Electron ${ELECTRON_VERSION#v}"; } > "$side/build.txt"
    echo "=== sidecar staged in out/win64 ($(du -sh "$STAGE" | cut -f1))"
}

# --------------------------------------------------------- the Electron

fetch_pinned() {
    local file=$1 url=$2 sum=$3
    [ -f "$file" ] && return
    echo "fetching $(basename "$file")..."
    wget -qO "$file.part" "$url"
    echo "$sum  $file.part" | sha256sum -c --quiet \
        || { rm -f "$file.part"; echo "checksum mismatch: $url"; exit 1; }
    mv "$file.part" "$file"
}

build_app() {
    [ -d "$STAGE/resources/sidecar" ] \
        || { echo "no sidecar yet; run: $0 sidecar"; exit 1; }
    for f in "$BASE"/electron/main/*.js "$BASE/electron/preload.js"; do
        node --check "$f" || exit 1
    done

    fetch_pinned "$CACHE/$ELECTRON_ZIP" "$ELECTRON_URL" "$ELECTRON_SHA256"
    rm -rf "$OUT/electron"
    mkdir -p "$OUT/electron"
    unzip -q "$CACHE/$ELECTRON_ZIP" -d "$OUT/electron"

    # Electron looks for its application in resources/app, and the sidecar
    # is already staged beside where that goes
    cp -r "$OUT"/electron/* "$STAGE/"
    rm -rf "$STAGE/resources/default_app.asar"
    mkdir -p "$STAGE/resources/app"
    cp -r "$BASE"/electron/main "$BASE"/electron/preload.js \
          "$BASE"/electron/package.json "$STAGE/resources/app/"
    # electron.exe is the name of the thing people will double-click
    mv "$STAGE/electron.exe" "$STAGE/Mirai98.exe"

    echo "=== app staged in out/win64 ($(du -sh "$STAGE" | cut -f1))"
    echo "    Mirai98.exe keeps Electron's own icon: setting ours needs"
    echo "    rcedit, which wants wine here, and is not worth a build"
    echo "    dependency.  See BUILD.md."
}

# --------------------------------------------------------------- the zip

build_zip() {
    [ -f "$STAGE/Mirai98.exe" ] || { echo "no app yet; run: $0 app"; exit 1; }

    # the licences of everything that rides along.  Electron's own are at
    # the stage top already; CPython's is inside python/.
    mkdir -p "$STAGE/licenses/QEMU" "$STAGE/licenses/noVNC"
    cp "$QEMU_SRC/COPYING" "$STAGE/licenses/QEMU/"
    cp "$CACHE"/novnc/LICENSE.txt "$STAGE/licenses/noVNC/" 2>/dev/null || true

    cat > "$STAGE/README.txt" <<EOF
Mirai98 for Windows $VERSION
============================

There is nothing to install.  Unpack this folder anywhere and run
Mirai98.exe.  Machines and disk images are kept in %APPDATA%\Mirai98,
so deleting this folder removes the program and nothing else.

If Windows warns about the file when first run, that is the mark web
browsers put on downloads: right-click the zip before unpacking,
Properties, tick "Unblock".

Reading and writing real drives needs administrator rights: right-click
Mirai98.exe and choose "Run as administrator" for that.  Everything else
works without.

If disk-image work feels slow, Windows Defender may be scanning every
write.  An administrator PowerShell can exempt the data folder:

  Add-MpPreference -ExclusionPath "\$env:APPDATA\Mirai98"

QEMU is GPLv2: sources at https://github.com/awemorris/qemu-pc98
(commit $(git -C "$QEMU_SRC" rev-parse --short HEAD)).  Licences for
everything bundled are in the licenses folders.
EOF

    local name=mirai98-win64-$VERSION
    rm -f "$DISTDIR/$name.zip" "$OUT/Mirai98"
    # a link, so the folder people unpack is called Mirai98, not win64
    ln -sfn win64 "$OUT/Mirai98"
    (cd "$OUT" && zip -X -q -r "$DISTDIR/$name.zip" Mirai98)
    rm -f "$OUT/Mirai98"
    (cd "$DISTDIR" && sha256sum "$name.zip" > "$name.zip.sha256")
    echo "=== dist/$name.zip ($(du -h "$DISTDIR/$name.zip" | cut -f1))"
}

# ------------------------------------------------------------------ main

case "${1:-}" in
    qemu)    build_qemu ;;
    sidecar) build_sidecar ;;
    app)     build_app ;;
    zip)     build_zip ;;
    "")      build_qemu; build_sidecar; build_app; build_zip ;;
    *)       sed -n '3,9p' "$0"; exit 2 ;;
esac
