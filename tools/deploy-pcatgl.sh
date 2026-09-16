#!/usr/bin/env bash
# Deploy pcat-gl (+ the pcat.js machine-switch restriction) from the
# pcat-plugin worktree to the running mirai98 install on CT209.
#
# Run this INSIDE the container as root (pct exec 209 -- bash, or a root
# shell on the container).  It does NOT touch pc98web.json.
#
# pc98web.py and app.js used to be left out of FILES on the grounds that
# they were "a separate single-file copy" (#24).  In practice that meant a
# fix to either had to be carried across by hand, with none of the md5
# verification, backup or guest-survival check the rest of this script
# does -- and on 2026-09-14 that is exactly what happened twice.  They are
# deployed here now, by the same rules as everything else.
#
# Safety: it verifies every file against the worktree's md5 before AND
# after copying, backs the old file up first, and aborts before the
# service restart if any copy does not match -- so a failed copy never
# reaches a restart.  Running guests survive the restart (the unit is
# KillMode=process); this script proves that rather than assuming it.
#
# NOTE on shell style: `set -e` is deliberately NOT used together with
# `grep -c` anywhere here -- `grep -c` exits non-zero on zero matches and
# would abort the script mid-run under `set -e`.  Critical steps check
# their own exit status explicitly instead.

set -uo pipefail

WT="/storage/work/pcat-wt"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
DST="/opt/mirai98/web"
SERVICE="mirai98.service"
VMROOT="/storage/pc98/vm"
TS="$(date +%Y%m%d-%H%M%S)"

# src (in the worktree)            -> dst (in the running install)
FILES=(
  "src/pc98web.py|pc98web.py"
  "src/web/app.js|ui/app.js"
  "src/plugins/pcatgl.py|plugins/pcatgl.py"
  "src/web/plugins/pcatgl.js|ui/plugins/pcatgl.js"
  "src/web/plugins/pcat.js|ui/plugins/pcat.js"
  "src/web/style.css|ui/style.css"
)

# --- optional qemu-3dfx binary sanity check --------------------------------
# The binary that will actually run is pcatgl.py's QEMU_3DFX_DEFAULT (unless
# pc98web.json overrides it); its path is read from the source below so this
# stays in step with the code.  Fill in the expected md5 when a build is
# blessed (build-p) to have the script refuse to deploy against the wrong
# binary; leave it empty to skip the check.
QEMU3DFX_EXPECTED_MD5="481a15d947c4285dcc7d458a2b56acaf"    # build-cd の md5(CD+MIDI、cue単一FILE修正+ACPI SCI IRQ9→10移設+ATAPI DMA が cd_img を迂回していた件の修正後+DiscJuggler .cdi(session1/track1 Mode1)対応)。別ビルドにしたらここも更新(空=検査しない)

die() { echo "DEPLOY ABORTED: $*" >&2; exit 1; }

echo "== pcat-gl deploy =="
echo "worktree: $WT"
echo "target:   $DST"
echo "stamp:    $TS"
echo

# --- preconditions ---------------------------------------------------------
[ -d "$WT" ]  || die "worktree not found: $WT"
[ -d "$DST" ] || die "install dir not found: $DST"
command -v md5sum   >/dev/null 2>&1 || die "md5sum not found"
command -v systemctl >/dev/null 2>&1 || die "systemctl not found"

branch="$(git -C "$WT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
head="$(git -C "$WT" rev-parse --short HEAD 2>/dev/null || echo '?')"
echo "worktree branch: $branch @ $head"
if [ "$branch" != "pcat-plugin" ]; then
  echo "  (warning: expected branch pcat-plugin)"
fi
echo

md5_of() { md5sum "$1" | awk '{print $1}'; }

# --- optional binary check -------------------------------------------------
if [ -n "$QEMU3DFX_EXPECTED_MD5" ]; then
  qbin="$(python3 - "$WT/src/plugins/pcatgl.py" <<'PY'
import ast, sys
src = open(sys.argv[1]).read()
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "QEMU_3DFX_DEFAULT" for t in node.targets):
        print(ast.literal_eval(node.value))
        break
PY
)"
  [ -n "$qbin" ] || die "could not read QEMU_3DFX_DEFAULT from pcatgl.py"
  [ -f "$qbin" ] || die "qemu-3dfx binary not found: $qbin"
  got="$(md5_of "$qbin")"
  if [ "$got" != "$QEMU3DFX_EXPECTED_MD5" ]; then
    die "qemu-3dfx md5 mismatch: $qbin has $got, expected $QEMU3DFX_EXPECTED_MD5"
  fi
  echo "qemu-3dfx binary OK: $qbin ($got)"
  echo
fi

# --- verify every source exists and matches, before touching anything ------
for pair in "${FILES[@]}"; do
  src="$WT/${pair%%|*}"
  [ -f "$src" ] || die "source missing: $src"
done

# --- preflight: does the code actually run? --------------------------------
# A NameError does not exist until its line runs, so neither py_compile nor a
# reviewer reading a diff can see one.  On 2026-09-14 a renamed variable left
# one use behind, reached production, and stopped every vm-0 start: on_start
# threw before QEMU was spawned, the display stack came up around nothing, and
# the console answered "no answer".  Both checks below run before a single
# byte is copied, and both are validated against that very file.
PYFLAKES_WHL="$SELF_DIR/pyflakes-3.4.0-py2.py3-none-any.whl"
SMOKE="$SELF_DIR/smoke_onstart.py"
[ -f "$PYFLAKES_WHL" ] || die "deploy gate needs $PYFLAKES_WHL"
[ -f "$SMOKE" ]        || die "deploy gate needs $SMOKE"

for pair in "${FILES[@]}"; do
  src="$WT/${pair%%|*}"
  case "$src" in *.py) ;; *) continue ;; esac
  python3 -m py_compile "$src" || die "py_compile failed: $src"
  # count the output rather than test $?: pyflakes exits non-zero for its own
  # reasons and a pipe would report the last command's status, not its own
  lint="$(PYTHONPATH="$PYFLAKES_WHL" python3 -m pyflakes "$src" 2>&1)"
  n="$(printf '%s' "$lint" | grep -c . || true)"
  if [ "$n" != "0" ]; then
    printf '%s\n' "$lint" >&2
    die "pyflakes: $n undefined-name/unused problem(s) in $src"
  fi
  printf '  lint ok:  %s\n' "$src"
done

# Executes on_start for real with the outside world replaced, then judges it
# by what it did -- the audio websockify must have been spawned with its port
# -- rather than by which lines it touched, because a line number rots on the
# next edit.  Hermetic: nothing spawned, no port bound, temp files only.
python3 "$SMOKE" "$WT/src/plugins/pcatgl.py" || die "on_start smoke failed"

# ... and the same discipline for the crash path (#74).  The teardown that
# runs when QEMU dies on its own has no user action behind it, so nothing
# would exercise it before a guest crashed in earnest.  This starts real
# stand-in processes on scratch ports far above any instance, kills the one
# standing in for QEMU, and asserts the rest went -- and that a recorded pid
# which is no longer ours was left alone.
CRASH_SMOKE="$SELF_DIR/smoke_crashreap.py"
FAKE_HELPER="$SELF_DIR/fakehelper.py"
[ -f "$CRASH_SMOKE" ] || die "deploy gate needs $CRASH_SMOKE"
[ -f "$FAKE_HELPER" ] || die "deploy gate needs $FAKE_HELPER"
python3 "$CRASH_SMOKE" "$WT/src/plugins/pcatgl.py" "$FAKE_HELPER" \
  || die "crash-teardown smoke failed"

# ... and the FPS pointer relay (#77).  Its job is turning a browser button
# mask into the press and release transitions QEMU wants, and getting that
# wrong leaves a button held down in the guest -- which in a game is a
# trigger held down, and shows up as the game misbehaving rather than as
# anything that looks like a console bug.  Stubs QMP: no machine, no ports.
FPS_SMOKE="$SELF_DIR/smoke_fps.py"
[ -f "$FPS_SMOKE" ] || die "deploy gate needs $FPS_SMOKE"
python3 "$FPS_SMOKE" "$WT/src/plugins/pcatgl.py" || die "fps-input smoke failed"
echo

# --- snapshot running guests BEFORE the restart ----------------------------
# Every running guest must survive the restart (KillMode=process).  Core
# QEMU machines (pc98/towns/pcat) write vm-N/qemu.pid; engine machines write
# their main pid into a pids.json -- pcat-gl at vm-N/pcatgl/pids.json under
# "qemu", box86 at vm-N/box86/pids.json under "86box".  All three are
# counted, or a running pcat-gl reads as "none" (as it did on 2026-09-12,
# hiding a live idx1).
declare -A GUEST_PID=()

epid() {   # epid <pids.json> <key> -> pid, or empty
  python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2]) or "")
except Exception:
    pass' "$1" "$2" 2>/dev/null
}

note_guest() {   # note_guest <label> <pid>: record it if alive
  local label="$1" pid="$2"
  [ -n "$pid" ] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    GUEST_PID["$label"]="$pid"
    printf '  %-14s pid %s  %s\n' "$label" "$pid" \
      "$(ps -o cmd= -p "$pid" 2>/dev/null | cut -c1-55)"
  fi
}

echo "running guests before restart:"
for pidf in "$VMROOT"/vm-*/qemu.pid; do
  [ -f "$pidf" ] || continue
  note_guest "$(basename "$(dirname "$pidf")")" "$(cat "$pidf" 2>/dev/null)"
done
for pidf in "$VMROOT"/vm-*/pcatgl/pids.json; do
  [ -f "$pidf" ] || continue
  note_guest "$(basename "$(dirname "$(dirname "$pidf")")")(gl)" \
             "$(epid "$pidf" qemu)"
done
for pidf in "$VMROOT"/vm-*/box86/pids.json; do
  [ -f "$pidf" ] || continue
  note_guest "$(basename "$(dirname "$(dirname "$pidf")")")(86box)" \
             "$(epid "$pidf" 86box)"
done
[ "${#GUEST_PID[@]}" -eq 0 ] && echo "  (none)"
echo

# --- backup + copy + re-verify each file -----------------------------------
echo "deploying files:"
for pair in "${FILES[@]}"; do
  src="$WT/${pair%%|*}"
  dst="$DST/${pair##*|}"
  smd5="$(md5_of "$src")"

  if [ -f "$dst" ]; then
    bak="$dst.pre-pcatgl.$TS"
    cp -a "$dst" "$bak" || die "backup failed: $dst -> $bak"
    mode="$(stat -c %a "$dst")"
    echo "  backup: $bak"
  else
    mode="644"
    echo "  (new file, no backup): $dst"
  fi

  cp -f "$src" "$dst"           || die "copy failed: $src -> $dst"
  chown root:root "$dst"        || die "chown failed: $dst"
  chmod "$mode" "$dst"          || die "chmod failed: $dst"

  dmd5="$(md5_of "$dst")"
  if [ "$dmd5" != "$smd5" ]; then
    die "md5 mismatch after copy: $dst has $dmd5, source $smd5 (restore from $bak)"
  fi
  printf '  ok:     %s  (%s)\n' "$dst" "$dmd5"
done
echo

# --- restart the service ---------------------------------------------------
echo "restarting $SERVICE ..."
systemctl restart "$SERVICE" || die "systemctl restart failed"
sleep 2

# --- report ----------------------------------------------------------------
echo
echo "== post-deploy report =="
active="$(systemctl show -p ActiveState --value "$SERVICE" 2>/dev/null || echo '?')"
mainpid="$(systemctl show -p MainPID --value "$SERVICE" 2>/dev/null || echo '?')"
echo "service ActiveState: $active   MainPID: $mainpid"
[ "$active" = "active" ] || echo "  (warning: service is not active -- check: journalctl -u $SERVICE -n50)"
echo

echo "deployed file md5 (live):"
for pair in "${FILES[@]}"; do
  dst="$DST/${pair##*|}"
  printf '  %-40s %s\n' "${pair##*|}" "$(md5_of "$dst")"
done
echo

echo "running guests after restart (must match the before list):"
survived=1
if [ "${#GUEST_PID[@]}" -eq 0 ]; then
  echo "  (none were running before)"
else
  for vm in "${!GUEST_PID[@]}"; do
    pid="${GUEST_PID[$vm]}"
    if kill -0 "$pid" 2>/dev/null; then
      printf '  %-14s pid %s  ALIVE  %s\n' "$vm" "$pid" \
        "$(ps -o cmd= -p "$pid" 2>/dev/null | cut -c1-50)"
    else
      printf '  %-14s pid %s  *** GONE ***\n' "$vm" "$pid"
      survived=0
    fi
  done
fi
echo
if [ "$survived" = 1 ]; then
  echo "RESULT: deploy complete; running guests survived the restart."
else
  echo "RESULT: deploy complete BUT a running guest did not survive -- investigate."
  exit 2
fi

# --- notes -----------------------------------------------------------------
# qemu-3dfx binary: this script does NOT edit pc98web.json.  The binary is
# pcatgl.py's QEMU_3DFX_DEFAULT by default; to override it at the install,
# add   "qemu_3dfx": "/path/to/qemu-system-i386"   to
# /opt/mirai98/web/pc98web.json (the key mirai98 reads via CONFIG).
