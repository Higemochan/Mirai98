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
    todo "the embeddable CPython sidecar" "phase 3"
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
