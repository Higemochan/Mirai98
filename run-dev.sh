#!/bin/bash

# Boot the current Mirai98 build in QEMU, no ISO involved.
#
# The live medium is a plain ext2 image holding /live/*.squashfs, which
# live-boot finds by scanning block devices.  A second disk labelled
# MIRAI98DATA plays the persistent data partition.  The web UI lands on
# host port 18099, ssh on 18022 (root, no password).

set -euo pipefail
BASE=$(cd "$(dirname "$0")" && pwd)
CACHE=$BASE/cache
OUT=$BASE/out

[ -f "$CACHE/base.squashfs" ] || { echo "run ./build-mirai98.sh first"; exit 1; }
[ -f "$OUT/mirai98.squashfs" ] || { echo "run ./build-mirai98.sh first"; exit 1; }

root=$OUT/liveroot
rm -rf "$root"
mkdir -p "$root/live"
cp "$CACHE/base.squashfs" "$root/live/filesystem.squashfs"
cp "$OUT/mirai98.squashfs" "$root/live/"

size=$(( $(du -sm "$root" | cut -f1) + 64 ))
rm -f "$OUT/live.img"
/sbin/mke2fs -q -t ext2 -d "$root" -F "$OUT/live.img" "${size}M"

if [ ! -f "$OUT/data.img" ]; then
    truncate -s 8G "$OUT/data.img"
    /sbin/mkfs.ext4 -q -F -L MIRAI98DATA "$OUT/data.img"
fi

accel=tcg
[ -w /dev/kvm ] && accel=kvm

exec qemu-system-x86_64 -accel "$accel" -m 2048 -smp 2 \
    -kernel "$CACHE/vmlinuz" -initrd "$CACHE/initrd.img" \
    -append "boot=live console=ttyS0" \
    -drive format=raw,file="$OUT/live.img" \
    -drive format=raw,file="$OUT/data.img" \
    -nic user,hostfwd=tcp:0.0.0.0:18099-:8098,hostfwd=tcp:127.0.0.1:18022-:22 \
    -display none -serial mon:stdio "$@"
