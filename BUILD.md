# Mirai98 Hypervisor Platform OS — building the image

A Debian live system that boots straight into the PC-98 hypervisor.
Everything here is source; the image is assembled by one script.

## What you need

A Debian/Ubuntu machine (the build chroots, so it has to be Linux) with:

    sudo apt install debootstrap squashfs-tools parted dosfstools \
        grub-pc-bin grub-efi-amd64-bin grub2-common xorriso \
        imagemagick wget unzip python3 qemu-utils

and a QEMU source tree with the PC-98 machine:

    git clone https://github.com/awemorris/qemu-pc98.git ~/qemu-pc98

## Laying out the tree

Put the two Python programs the appliance runs into `src/`:

    cp /path/to/pc98web.py /path/to/virtpc98.py src/

`src/patch_novnc.py` is already here.  It teaches the bundled noVNC to
ask for the guest's sound, which stock noVNC never does.

## Building

    ./build-mirai98.sh          # base rootfs, appliance QEMU, payload
    ./build-mirai98.sh usb      # ... and out/mirai98-usb.img
    ./build-mirai98.sh iso      # ... or a BIOS+UEFI ISO

The first run takes a while: it debootstraps a Debian userland and
builds QEMU.  Both land in `cache/` and are reused, so later runs that
only change the Python side finish in seconds.

`QEMU_SRC=~/qemu-pc98` is the default; point it elsewhere to build a
different tree.  Bump `STAGE1_VERSION` in the script to force the base
rootfs to be rebuilt (a comment-only edit to `packages.list` will not,
since the cache key ignores comments).

Write the result to a stick with `dd`, or boot it without one:

    ./run-dev.sh                # boots the current build in QEMU

## What the build downloads

Not redistributed here, fetched when building, each under its own
licence:

- noVNC 1.5.0 (MPL-2.0) — the browser console
- ttyd 1.7.7 (MIT) — the browser terminal
- FreeDOS(98) by lpproj (GPL) — the machine a fresh stick starts with
- Debian packages listed in `packages.list`

## What is not in this tree

`cache/`, `out/` and `qemu-build/` are build products and can be deleted
at any time; nothing in them needs to be published.
