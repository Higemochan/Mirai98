# Mirai98 — building it

Two things come out of this tree, both from the same QEMU commit:

- **`dist/mirai98-usb.img`** — the Live USB appliance, built by
  `./build-liveusb.sh`
- **`dist/mirai98-win64.zip`** — the Windows application, built by
  `./build-electron.sh`

Both are built on Linux.

## The tree

    src/            the web UI and its server; shared by both products
    appliance/      overlay and package list for the Live USB only
    electron/       the Windows application shell
    win64/          the mingw cross-build for Windows QEMU
    qemu-pc98/      submodule: github.com/awemorris/qemu-pc98

QEMU is a submodule so that the two products cannot drift onto
different commits.  After cloning:

    git submodule update --init

To move to a newer QEMU, check the submodule out and commit the new
pointer in this repository:

    git -C qemu-pc98 fetch origin
    git -C qemu-pc98 checkout origin/master
    git add qemu-pc98 && git commit -m "qemu: move to <rev>"

## What you need

A Debian/Ubuntu machine (the Live USB build chroots, so it has to be
Linux) with:

    sudo apt install debootstrap squashfs-tools parted dosfstools \
        grub-pc-bin grub-efi-amd64-bin grub2-common xorriso \
        imagemagick wget unzip python3 qemu-utils nodejs

and, for the Windows side, the mingw-w64 toolchain:

    sudo apt install mingw-w64 mingw-w64-tools meson ninja-build

nodejs is there for one job: the build syntax-checks the page in
`src/web/` before it packs it, because a stray quote in the
JavaScript is a blank screen with nothing in the log.

## The Live USB

    ./build-liveusb.sh          # base rootfs, appliance QEMU, payload
    ./build-liveusb.sh usb      # ... and dist/mirai98-usb.img
    ./build-liveusb.sh iso      # ... or a BIOS+UEFI ISO

The first run takes a while: it debootstraps a Debian userland and
builds QEMU.  Both land in `cache/` and are reused, so later runs that
only change the Python side finish in seconds.

Check what came out:

    cat out/payload/opt/mirai98/version     # 20260730 (qemu 57fa021)

The revision there must be the submodule's.  `QEMU_SRC` overrides where
QEMU is built from, if you want to try a tree that is not the submodule.

Bump `STAGE1_VERSION` in the script to force the base rootfs to be
rebuilt.  A comment-only edit to `appliance/packages.list` will not do
it, because the cache key ignores comments.

Write the result to a stick with `dd`, or boot it without one:

    ./run-dev.sh                # boots the current build in QEMU

## The Windows application

    ./build-electron.sh         # everything, into dist/mirai98-win64.zip

There is no installer: the zip is unpacked wherever the user likes, and
the application keeps its data in `%LOCALAPPDATA%\Mirai98` regardless of
where it was unpacked.

## What the build downloads

Not redistributed here, fetched when building, each under its own
licence:

- noVNC 1.5.0 (MPL-2.0) — the browser console
- ttyd 1.7.7 (MIT) — the browser terminal
- FreeDOS(98) by lpproj (GPL) — the machine a fresh stick starts with
- Debian packages listed in `appliance/packages.list`
- Electron and an embeddable CPython, for the Windows build
- the pinned source archives in `win64/versions.conf`

## What is not in this tree

`cache/`, `out/`, `dist/` and `qemu-build/` are build products and can be
deleted at any time.  `dist/` is where the two finished images land.
