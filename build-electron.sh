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

# CPython for Windows as a zip that only needs unpacking.  pc98web uses
# nothing outside the standard library, and every extension module it
# does reach for is in here: _ctypes, _socket, _hashlib, _ssl and
# _elementtree.  Pinned, and checked, like every other download.
PYTHON_VERSION=3.13.7
PYTHON_TAG=313
PYTHON_URL=https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-embed-amd64.zip
PYTHON_SHA256=f6cca216a359be84797cabb54149ce5e062afb16cc7567eb7fc51cacb2d86b65

NOVNC_URL=https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz

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

    local exe=$BASE/win64/build/qemu-system-i386.exe
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

    mkdir -p "$side/qemu/share/keymaps"
    cp "$BASE"/win64/build/qemu-system-i386.exe \
       "$BASE"/win64/build/qemu-system-i386w.exe "$side/qemu/"
    # the DLL closure, worked out from the PE import tables
    python3 "$BASE/win64/bundle-deps.py" \
        --objdump x86_64-w64-mingw32-objdump \
        --search "$BASE/win64/root/bin" --search "$BASE/win64/root/lib" \
        --dest "$side/qemu" --report "$side/qemu/DLL-DEPENDENCIES.txt" \
        "$side/qemu/qemu-system-i386.exe" >/dev/null
    # what -L has to find: the compatible ROMs, and the keymaps the VNC
    # server reads.  Straight from the submodule, as the appliance does.
    cp "$QEMU_SRC"/pc-bios/pc98*.bin "$side/qemu/share/"
    cp "$QEMU_SRC"/pc-bios/keymaps/* "$side/qemu/share/keymaps/"
    # unstripped these are 78 MB each
    x86_64-w64-mingw32-strip --strip-unneeded "$side"/qemu/*.exe \
        "$side"/qemu/*.dll

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
    cat > "$side/pc98web.json" <<'EOF'
{
  "qemu": "qemu/qemu-system-i386.exe",
  "datadir": "qemu/share",
  "novnc": "novnc",
  "web": "src/web"
}
EOF

    git -C "$QEMU_SRC" rev-parse --short HEAD > "$STAGE/qemu.rev"
    echo "$VERSION" > "$STAGE/version"
    echo "=== sidecar staged in out/win64 ($(du -sh "$STAGE" | cut -f1))"
}

# --------------------------------------------------------- the Electron

build_app() {
    todo "the Electron shell" "phase 4"
}

# --------------------------------------------------------------- the zip

build_zip() {
    todo "the portable zip" "phase 7"
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
