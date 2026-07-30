#!/bin/bash

# Mirai98 Hypervisor Platform OS: the Live USB image
#
#   ./build-liveusb.sh          build what is missing (base, qemu, payload)
#   ./build-liveusb.sh qemu     force the appliance QEMU rebuild
#   ./build-liveusb.sh base     force the base rootfs rebuild
#   ./build-liveusb.sh usb      wrap everything into dist/mirai98-usb.img
#   ./build-liveusb.sh iso      wrap everything into a BIOS+UEFI ISO
#
# QEMU comes from the qemu-pc98 submodule, so the appliance and the
# Windows build (build-electron.sh) are always the same commit.
#
# The output splits along how often things change:
#   cache/base-<hash>.squashfs   Debian userland; rebuilt only when
#                                packages.list or the distro changes
#   cache/vmlinuz, initrd.img    kernel pair, extracted from the base
#   out/mirai98.squashfs         qemu + web app + config; seconds to rebuild
#   dist/mirai98-usb.img         what you write to a stick
# live-boot unions every /live/*.squashfs, so the payload rides on top of
# the base without ever touching it.  ./run-dev.sh boots the pair in QEMU
# without making an image at all.

set -euo pipefail

BASE=$(cd "$(dirname "$0")" && pwd)
CACHE=$BASE/cache
OUT=$BASE/out
# where the things worth keeping land; DIST below is the Debian suite
DISTDIR=$BASE/dist
DIST=trixie
MIRROR=http://deb.debian.org/debian
STAGE1_VERSION=3                 # bump to force a base rebuild
VERSION=$(date +%Y%m%d)

# the qemu-pc98 submodule: the machine, its ROMs and its keymaps
QEMU_SRC=${QEMU_SRC:-$BASE/qemu-pc98}
QEMU_BUILD=$BASE/qemu-build
NOVNC_URL=https://github.com/novnc/noVNC/archive/refs/tags/v1.5.0.tar.gz
# FreeDOS(98): the GPL kernel and shell, built for PC-98 by lpproj.  It
# ships as the machine a fresh stick already has.
FREEDOS_URL=https://github.com/lpproj/fdkernel/releases/download/test-20220120-cherrypick/fd98_hd_250m_20220123.zip
# a static binary, because trixie dropped the ttyd package
TTYD_URL=https://github.com/tsl0922/ttyd/releases/download/1.7.7/ttyd.x86_64

mkdir -p "$CACHE" "$OUT" "$DISTDIR"

[ -f "$QEMU_SRC/configure" ] || {
    echo "no QEMU source in $QEMU_SRC"
    echo "if that is the submodule, check it out first:"
    echo "    git submodule update --init"
    exit 1; }

key=$( (echo "$DIST $STAGE1_VERSION"; \
        grep -v '^#' "$BASE/appliance/packages.list") \
      | sha256sum | cut -c1-16)
BASE_SQ=$CACHE/base-$key.squashfs

# ------------------------------------------------------------ stage 1: base

build_base() {
    echo "=== stage 1: debootstrap $DIST -> $BASE_SQ"
    local root=$CACHE/rootfs.$$
    sudo rm -rf "$root"
    sudo /usr/sbin/debootstrap --variant=minbase "$DIST" "$root" "$MIRROR"

    sudo mount -t proc proc "$root/proc"
    sudo mount -t sysfs sys "$root/sys"
    sudo mount --bind /dev "$root/dev"
    sudo mount --bind /dev/pts "$root/dev/pts"
    trap 'sudo umount -l "$root"/{proc,sys,dev/pts,dev} 2>/dev/null || true' \
        EXIT

    grep -v '^#' "$BASE/appliance/packages.list" | grep . \
        | sudo tee "$root/tmp/packages" >/dev/null
    sudo chroot "$root" sh -ec '
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        xargs apt-get install -y --no-install-recommends < /tmp/packages
        apt-get clean
        echo mirai98 > /etc/hostname
        passwd -d root
        rm -rf /var/lib/apt/lists/* /tmp/packages'

    # the splash must ride inside the initrd, or the longest stretch of
    # the boot (live-boot copying itself to RAM) stays a black screen
    make_splash
    sudo mkdir -p "$root/usr/share/plymouth/themes/mirai98" \
                  "$root/etc/plymouth"
    sudo cp \
        "$BASE"/appliance/overlay/usr/share/plymouth/themes/mirai98/mirai98.* \
        "$CACHE/splash.png" "$root/usr/share/plymouth/themes/mirai98/"
    sudo cp "$BASE/appliance/overlay/etc/plymouth/plymouthd.conf" \
            "$root/etc/plymouth/"
    sudo chroot "$root" sh -ec '
        plymouth-set-default-theme mirai98
        update-initramfs -u'

    sudo umount -l "$root"/{proc,sys,dev/pts,dev}
    trap - EXIT

    sudo cp "$root"/boot/vmlinuz-*   "$CACHE/vmlinuz"
    sudo cp "$root"/boot/initrd.img-* "$CACHE/initrd.img"
    sudo chown "$USER" "$CACHE/vmlinuz" "$CACHE/initrd.img"

    sudo mksquashfs "$root" "$BASE_SQ.tmp" -comp zstd -noappend -quiet
    sudo chown "$USER" "$BASE_SQ.tmp"
    mv "$BASE_SQ.tmp" "$BASE_SQ"
    ln -sf "$(basename "$BASE_SQ")" "$CACHE/base.squashfs"
    sudo rm -rf "$root"
    echo "=== stage 1 done: $(du -h "$BASE_SQ" | cut -f1)"
}

# --------------------------------------------- stage 1.5: appliance QEMU

build_qemu() {
    echo "=== appliance qemu: headless i386-softmmu from $QEMU_SRC"
    mkdir -p "$QEMU_BUILD"
    (cd "$QEMU_BUILD" && "$QEMU_SRC/configure" \
        --target-list=i386-softmmu \
        --disable-gtk --disable-sdl --disable-opengl --disable-curses \
        --disable-gnutls --disable-nettle --disable-gcrypt \
        --disable-curl --disable-libusb --disable-usb-redir \
        --disable-spice --disable-smartcard --disable-guest-agent \
        --disable-docs --disable-tpm --disable-plugins \
        --disable-pa --disable-alsa --disable-oss --disable-jack \
        --disable-pipewire --disable-sndio --disable-dbus-display \
        --disable-libdw --disable-libudev \
        && ninja -C . qemu-system-i386)
    strip -o "$CACHE/qemu-system-i386" "$QEMU_BUILD/qemu-system-i386"
    echo "=== appliance qemu done: $(du -h "$CACHE/qemu-system-i386" | cut -f1)"
    ldd "$CACHE/qemu-system-i386" | awk '$3 ~ /^\// {print $1}' | sort \
        > "$CACHE/qemu-libs.txt"
    echo "    (runtime libs listed in cache/qemu-libs.txt; keep"
    echo "     packages.list in step with it)"
}

# --------------------------------------------------- stage 2: the payload

build_payload() {
    echo "=== stage 2: out/mirai98.squashfs"
    [ -f "$CACHE/qemu-system-i386" ] \
        || { echo "no appliance qemu yet; run: $0 qemu"; exit 1; }
    [ -f "$BASE/src/pc98web.py" ] \
        || { echo "put pc98web.py in $BASE/src/ first"; exit 1; }
    # a syntax error in the page is a blank screen with nothing in the
    # log, so it is worth a second here rather than a bug report later
    command -v node >/dev/null \
        || { echo "node is needed to check the page: apt install nodejs";
             exit 1; }
    python3 "$BASE/src/check_page.py" "$BASE/src/web"

    if [ ! -d "$CACHE/novnc" ]; then
        echo "fetching noVNC..."
        wget -qO "$CACHE/novnc.tar.gz" "$NOVNC_URL"
        tar -C "$CACHE" -xzf "$CACHE/novnc.tar.gz"
        mv "$CACHE/noVNC-1.5.0" "$CACHE/novnc"
        rm "$CACHE/novnc.tar.gz"
    fi
    if [ ! -f "$CACHE/ttyd" ]; then
        echo "fetching ttyd..."
        wget -qO "$CACHE/ttyd" "$TTYD_URL"
        chmod +x "$CACHE/ttyd"
    fi
    if [ ! -f "$CACHE/freedos98.raw" ]; then
        echo "fetching FreeDOS(98)..."
        wget -qO "$CACHE/fd98.zip" "$FREEDOS_URL"
        rm -rf "$CACHE/fd98"
        unzip -q -o "$CACHE/fd98.zip" -d "$CACHE/fd98"
        hdi=$(find "$CACHE/fd98" -iname '*.hdi' | head -1)
        python3 "$BASE/src/virtpc98.py" hdi-to-raw "$hdi" \
            "$CACHE/freedos98.raw" --quiet
        cp "$CACHE"/fd98/READ*.htm "$CACHE/freedos98-readme.htm" 2>/dev/null
        rm -rf "$CACHE/fd98" "$CACHE/fd98.zip"
    fi

    local stage=$OUT/payload
    rm -rf "$stage"
    mkdir -p "$stage/opt/mirai98/web" "$stage/opt/mirai98/keymaps" \
             "$stage/opt/mirai98/bin"

    cp "$CACHE/qemu-system-i386" "$stage/opt/mirai98/"
    cp "$CACHE/ttyd" "$stage/opt/mirai98/bin/"
    # compatible ROMs sit right in the datadir; /storage/roms, searched
    # first, lets a real ROM set override them file by file
    cp "$QEMU_SRC"/pc-bios/pc98*.bin "$stage/opt/mirai98/"
    cp "$QEMU_SRC"/pc-bios/keymaps/* "$stage/opt/mirai98/keymaps/"
    cp "$BASE/src/pc98web.py" "$stage/opt/mirai98/web/"
    # pc98web leans on virtpc98 for image creation and conversion, and on
    # drives/ for everything that touches a real one
    cp "$BASE/src/virtpc98.py" "$stage/opt/mirai98/web/"
    cp -r "$BASE/src/drives" "$stage/opt/mirai98/web/drives"
    rm -rf "$stage/opt/mirai98/web/drives/__pycache__"
    # the page: index.html and the script and style it pulls in, in a
    # directory of their own so nothing else can be reached from there
    cp -r "$BASE/src/web" "$stage/opt/mirai98/web/ui"
    cp -r "$CACHE/novnc" "$stage/opt/mirai98/web/novnc"
    # stock noVNC never asks for the guest's sound; patched, it rides
    # the same WebSocket as the pixels
    python3 "$BASE/src/patch_novnc.py" "$stage/opt/mirai98/web/novnc"
    # the machine a fresh stick starts life with
    mkdir -p "$stage/opt/mirai98/seed"
    cp "$CACHE/freedos98.raw" "$stage/opt/mirai98/seed/"
    cp "$CACHE/freedos98-readme.htm" "$stage/opt/mirai98/seed/" 2>/dev/null
    cat > "$stage/opt/mirai98/seed/README" <<'EOF'
freedos98.raw is FreeDOS(98): the FreeDOS kernel and FreeCOM, built for
the PC-98 by lpproj and distributed under the GNU GPL.  Sources and the
original HDI image:

  https://github.com/lpproj/fdkernel
  https://bauxite.sakura.ne.jp/software/dos/freedos.htm

Mirai98 copies this image into disks/hdd/ the first time it boots and
defines a machine around it; delete the machine and the image to be rid
of it.
EOF

    cat > "$stage/opt/mirai98/web/pc98web.json" <<EOF
{
  "qemu": "/opt/mirai98/qemu-system-i386",
  "roms": "/storage/roms",
  "datadir": "/opt/mirai98",
  "root": "/storage/pc98",
  "boot": "/boot-data",
  "novnc": "/opt/mirai98/web/novnc",
  "web": "/opt/mirai98/web/ui"
}
EOF

    cp -r "$BASE/appliance/overlay/." "$stage/"
    chmod +x "$stage"/opt/mirai98/bin/*
    # the plymouth theme wants the same splash grub uses
    make_splash
    cp "$CACHE/splash.png" \
       "$stage/usr/share/plymouth/themes/mirai98/splash.png"
    # enabling a unit is just this symlink; overlayfs merges it into /etc
    local wants=$stage/etc/systemd/system/multi-user.target.wants
    mkdir -p "$wants"
    ln -sf /etc/systemd/system/mirai98.service "$wants/"
    ln -sf /etc/systemd/system/mirai98-console.service "$wants/"
    ln -sf /etc/systemd/system/mirai98-terminal.service "$wants/"
    ln -sf /usr/lib/systemd/system/systemd-networkd.service "$wants/"

    # which QEMU this image carries, so a running appliance can say so.
    # the same stamp goes into the Windows build, and the two are only
    # comparable if it is really there: a missing revision is a failure
    qemu_rev=$(git -C "$QEMU_SRC" rev-parse --short HEAD 2>/dev/null || true)
    [ -n "$qemu_rev" ] \
        || { echo "no commit for $QEMU_SRC: cannot stamp the image"; exit 1; }
    echo "$VERSION (qemu $qemu_rev)" > "$stage/opt/mirai98/version"
    mksquashfs "$stage" "$OUT/mirai98.squashfs" -comp zstd -noappend -quiet
    echo "=== stage 2 done: $(du -h "$OUT/mirai98.squashfs" | cut -f1)"
}

# -------------------------------------------------------------- the splash

make_splash() {
    [ -f "$CACHE/splash.png" ] && return
    echo "=== splash: cache/splash.png"
    # TrueColor on purpose: GRUB's PNG reader takes RGB and little else,
    # and ImageMagick would otherwise store black-and-white as greyscale
    convert -size 1024x768 xc:black -fill white -gravity center \
        -pointsize 140 -annotate +0-40 'Mirai98' \
        -pointsize 32 -annotate +0+70 'Hypervisor Platform OS' \
        -type TrueColor -define png:color-type=2 "$CACHE/splash.png"
}

grub_cfg() {
    cat <<'EOF'
insmod all_video
insmod png
if loadfont /boot/grub/fonts/unicode.pf2; then
    set gfxmode=1024x768,800x600,auto
    set gfxpayload=keep
    insmod gfxterm
    terminal_output gfxterm
fi
background_image /boot/grub/splash.png
set default=0
set timeout=2
set timeout_style=hidden
set color_normal=white/black
set menu_color_normal=white/black
set menu_color_highlight=black/white
menuentry "Mirai98 Hypervisor Platform OS" {
    linux /live/vmlinuz boot=live toram quiet splash loglevel=0 \
          systemd.show_status=false vt.global_cursor_default=0
    initrd /live/initrd.img
}
menuentry "Mirai98 (serial console)" {
    linux /live/vmlinuz boot=live toram console=ttyS0
    initrd /live/initrd.img
}
EOF
}

# ------------------------------------------------- stage 3b: the USB image

build_usb() {
    echo "=== usb: dist/mirai98-usb.img"
    local img=$DISTDIR/mirai98-usb.img
    usb_mnt=$OUT/usb-mnt
    usb_loop=""

    # The front partition carries the system, sized to fit.  The back one
    # starts as the smallest standards-compliant FAT32 volume (64 MiB) and
    # grows to the end of the stick on first boot.
    # -L, because base.squashfs is a symlink into the cache.
    local data_mb=${MIRAI98_DATA_MB:-64}
    [[ "$data_mb" =~ ^[0-9]+$ ]] && (( data_mb >= 64 )) ||
        { echo "MIRAI98_DATA_MB must be an integer of at least 64" >&2;
          return 1; }
    local sys_mb=$(( ( $(du -Lm "$CACHE/base.squashfs" | cut -f1) \
                     + $(du -Lm "$OUT/mirai98.squashfs" | cut -f1) \
                     + $(du -Lm "$CACHE/vmlinuz" | cut -f1) \
                     + $(du -Lm "$CACHE/initrd.img" | cut -f1) + 96 ) ))
    local sys_end=$(( 1 + sys_mb ))
    local total=$(( sys_end + data_mb + 1 ))

    rm -f "$img"
    truncate -s "${total}M" "$img"
    /sbin/parted -s "$img" mklabel msdos \
        mkpart primary fat32 1MiB "${sys_end}MiB" \
        mkpart primary fat32 "${sys_end}MiB" "$(( sys_end + data_mb ))MiB" \
        set 1 boot on

    usb_loop=$(sudo losetup -P -f --show "$img")
    trap 'sudo umount -l "${usb_mnt:-/nonexistent}" 2>/dev/null;
          [ -n "${usb_loop:-}" ] && sudo losetup -d "$usb_loop"' EXIT

    sudo mkfs.vfat -F 32 -n MIRAI98 "${usb_loop}p1" >/dev/null
    sudo mkfs.vfat -F 32 -n MIRAI98DATA "${usb_loop}p2" >/dev/null

    local mnt=$usb_mnt
    mkdir -p "$mnt"
    sudo mount "${usb_loop}p1" "$mnt"
    sudo mkdir -p "$mnt/live" "$mnt/boot/grub"
    sudo cp "$CACHE/vmlinuz" "$CACHE/initrd.img" "$mnt/live/"
    sudo cp -L "$CACHE/base.squashfs" "$mnt/live/filesystem.squashfs"
    sudo cp "$OUT/mirai98.squashfs" "$mnt/live/"
    # toram frees the stick completely: the data partition can then be
    # grown with every other partition unmounted, and the stick could
    # even be pulled once the system is up
    make_splash
    sudo cp "$CACHE/splash.png" "$mnt/boot/grub/"
    grub_cfg | sudo tee "$mnt/boot/grub/grub.cfg" >/dev/null
    sudo grub-install --target=i386-pc --boot-directory="$mnt/boot" \
        "$usb_loop" 2>/dev/null
    sudo grub-install --target=x86_64-efi --efi-directory="$mnt" \
        --boot-directory="$mnt/boot" --removable --no-nvram 2>/dev/null

    sudo umount "$mnt"
    sudo losetup -d "$usb_loop"
    usb_loop=""
    trap - EXIT
    echo "=== usb done: $(du -h --apparent-size "$img" | cut -f1) image," \
         "$(du -h "$img" | cut -f1) allocated (dd it to a stick)"
}

# ------------------------------------------------------- stage 3: the ISO

build_iso() {
    echo "=== stage 3: dist/mirai98-$VERSION-amd64.iso"
    local tree=$OUT/iso
    rm -rf "$tree"
    mkdir -p "$tree/live" "$tree/boot/grub"
    cp "$CACHE/vmlinuz" "$CACHE/initrd.img" "$tree/live/"
    cp "$CACHE/base.squashfs" "$tree/live/filesystem.squashfs"
    cp "$OUT/mirai98.squashfs" "$tree/live/"
    make_splash
    cp "$CACHE/splash.png" "$tree/boot/grub/"
    grub_cfg > "$tree/boot/grub/grub.cfg"
    grub-mkrescue -o "$DISTDIR/mirai98-$VERSION-amd64.iso" "$tree" \
        --product-name=Mirai98 2>/dev/null
    echo "=== stage 3 done: $(du -h "$DISTDIR/mirai98-$VERSION-amd64.iso" | cut -f1)"
}

# ------------------------------------------------------------------ main

case "${1:-}" in
    base) rm -f "$BASE_SQ"; build_base ;;
    qemu) build_qemu ;;
    iso)  [ -f "$BASE_SQ" ] || build_base
          [ -f "$CACHE/qemu-system-i386" ] || build_qemu
          build_payload; build_iso ;;
    usb)  [ -f "$BASE_SQ" ] || build_base
          [ -f "$CACHE/qemu-system-i386" ] || build_qemu
          build_payload; build_usb ;;
    "")   [ -f "$BASE_SQ" ] || build_base
          [ -f "$CACHE/qemu-system-i386" ] || build_qemu
          build_payload
          echo "ready: ./run-dev.sh boots it; '$0 usb' or '$0 iso' wraps it" ;;
    *)    sed -n '3,9p' "$0"; exit 2 ;;
esac
