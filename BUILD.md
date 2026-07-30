# Mirai98 — building it

Two things come out of this tree, both from the same QEMU commit:

- **`dist/mirai98-usb.img`** — the Live USB appliance, built by
  `./build-liveusb.sh`
- **`dist/mirai98-win64.zip`** — the Windows application, built by
  `./build-electron.sh`

Both are built on Linux.

## The tree

    src/            the web UI and its server, and virtpc98; shared
    appliance/      overlay and package list for the Live USB only
    electron/       the Windows application shell
    win64/          the mingw cross-build for Windows QEMU
    qemu-pc98/      submodule: github.com/awemorris/qemu-pc98

Everything written in Python lives here, including `src/virtpc98.py`.
The qemu-pc98 repository is kept for merging towards upstream QEMU and
carries no tooling of its own, so this tree is the only place any of it
is edited.

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

    ./build-electron.sh          # everything, into dist/mirai98-win64.zip
    ./build-electron.sh qemu     # just the mingw cross-build
    ./build-electron.sh sidecar  # CPython + src/ + qemu, into out/win64/
    ./build-electron.sh app      # ... and Electron over the top of it

QEMU for Windows is cross-built by `win64/build.sh`, from the same
submodule the appliance uses.  The first run builds the dependencies from
the pinned archives in `win64/versions.conf`, which takes a while; after
that they are stamped in `win64/root/` and skipped.  The `qemu` stage
checks for itself that the `fat98` block driver, qcow1 support and the VNC
websocket listener are all in the executables, and fails if they are not.
`configure` is given `--without-default-features`, so anything not asked
for is simply absent and nothing says so: a build with no VNC produces an
application whose every console is a dead grey rectangle.  The marker
looked for is `vnc-ws-listen` rather than `vnc`, because no build has a
bare `vnc` string in it, having VNC or not.

The `sidecar` stage puts the server side together: an embeddable
CPython from python.org, `src/`, the Windows QEMU and its DLLs, and the
patched noVNC.  Nothing is compiled for Windows here, so it all assembles
on Linux; `pc98web.py` reaches for nothing outside the standard library,
which is what makes that possible.

Paths in the `pc98web.json` it writes are relative to that file, so the
directory can be unpacked anywhere.  What the user creates does not live
in it: `--base` says where that goes, because the program's own directory
may well be unwritable.

Wine is enough for a real smoke test of the result, and it exercises the
Windows code paths rather than the Linux ones, because `os.name` is
`nt`:

    cd out/win64/resources/sidecar
    WINEPREFIX=~/.wine-mirai98 wine python/python.exe src/pc98web.py         --port=0 --loopback --app-token --base=Z:/tmp/mirai98-test         --config=pc98web.json

It prints one `MIRAI98-READY` line with the port and the token; opening
`http://127.0.0.1:<port>/?token=<token>` then works, and creating a disk
image proves the bundled QEMU and `virtpc98` both load.

The `app` stage lays Electron's own prebuilt Windows zip over that and
puts `electron/` in as `resources/app`, so the whole build is still only
file copying and needs no Windows machine and no wine.  `electron.exe`
becomes `Mirai98.exe`; it keeps Electron's icon, because changing that
needs rcedit, which would want wine here.  If the icon matters for a
release, run rcedit on a Windows machine afterwards.

Two things about the shell are worth knowing before changing it.

`electron/main/sidecar.js` deliberately uses nothing from Electron, so it
can be exercised with plain node — and it is where the awkward part lives.
A machine is started with `start_new_session`, in a session of its own, so
that restarting the server does not take the guests down with it; the cost
is that no signal to the server reaches them.  So the server writes each
machine's pid into `vm/vm-N/qemu.pid`, and the shell kills by pid when the
server stops answering.  Killing a process group would do nothing, on
either platform.

The renderer gets no Node: the page is the same one the appliance serves to
an ordinary browser, and must not come to depend on Electron.  That is why
`preload.js` is almost empty.

## Real drives

Everything that touches a real drive goes through `src/drives/`: one
door, two implementations (lsblk on the appliance, PowerShell and raw
physical-drive handles on Windows), and all of the refusals.  The
refusals live in the layer rather than in the page so that no caller can
write to a drive by simply forgetting to ask: writing takes repeating
something the drive itself says (its model, or its size in bytes), the
host's own disk is never writable, and internal disks take saying so on
top.  A write is read back afterwards and compared, because a write that
went nowhere looks exactly like one that worked.

Asking what drives exist costs a subprocess — lsblk here, PowerShell on
Windows, where starting it at all is most of a second.  The page used to ask
on every render, which made every click feel slow; now the layer keeps a
listing for a few seconds and the page fetches lazily, except when a drive
is about to be picked, where it always asks again.  Nothing that decides
whether to write uses a kept listing: a drive swapped for another in the
same socket would otherwise answer to the confirmation given for the first.

On Windows the writing side takes the disk offline first
(`Set-Disk -IsOffline`).  Without that, Windows' own filesystem cache
writes its stale idea of the disk back over the image minutes later, and
the result looks written but is quietly wrong.

The server also takes `--parent-pid`: it watches that process and, once
it is gone, stops every machine and exits.  That is the net under an
application crash — without it the machines and the server survive,
holding ports, invisible to the next start.  Watching an inherited pipe
would be tidier and does not work: Electron's helpers inherit the handle
and outlive the crash, so the pipe never closes.

`win64/build.sh dist` assembles the separate virtpc98 distribution.  It
needs `win64/package-assets/virtpc98.exe`, which PyInstaller produces on
Windows and so cannot be built here; drop it in by hand before running
that stage.  `virtpc98.py` itself comes from `src/`, so there is only one
copy of it in the tree.

There is no installer: the zip is unpacked wherever the user likes, and
the application keeps its data in `%LOCALAPPDATA%\Mirai98` regardless of
where it was unpacked.

## Publishing a QEMU release

The Windows QEMU zip that qemu-pc98 offers as a release is built and
uploaded from here:

    ./win64/build.sh release rev11

That assembles `win64/qemu-11.0-pc98-rev11/` and the matching zip, with
the version taken from the submodule's VERSION file and the folder inside
the zip named the way people unpack it.  It uploads nothing: it finishes
by printing the `gh release` command to run, so publishing stays a
deliberate step.

`virtpc98.py` in the zip comes from `src/`, so whatever is committed
here is what ships.  `virtpc98.exe` has to be dropped into
`win64/package-assets/` from a Windows machine first.

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
