#!/usr/bin/env bash
# Deploy pcat-gl (+ the pcat.js machine-switch restriction) from the
# pcat-plugin worktree to the running mirai98 install on CT209.
#
# Run this INSIDE the container as root (pct exec 209 -- bash, or a root
# shell on the container).  It does NOT deploy pc98web.py (#24) -- that is
# a separate single-file copy.  It does NOT touch pc98web.json.
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
DST="/opt/mirai98/web"
SERVICE="mirai98.service"
VMROOT="/storage/pc98/vm"
TS="$(date +%Y%m%d-%H%M%S)"

# src (in the worktree)            -> dst (in the running install)
FILES=(
  "src/plugins/pcatgl.py|plugins/pcatgl.py"
  "src/web/plugins/pcatgl.js|ui/plugins/pcatgl.js"
  "src/web/plugins/pcat.js|ui/plugins/pcat.js"
)

# --- optional qemu-3dfx binary sanity check --------------------------------
# The binary that will actually run is pcatgl.py's QEMU_3DFX_DEFAULT (unless
# pc98web.json overrides it); its path is read from the source below so this
# stays in step with the code.  Fill in the expected md5 when a build is
# blessed (build-p) to have the script refuse to deploy against the wrong
# binary; leave it empty to skip the check.
QEMU3DFX_EXPECTED_MD5="c56ad3b1fcf72e16ee6d70b519e06bf6"    # build-s の md5(最終候補)。別ビルドにしたらここも更新(空=検査しない)

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

# --- snapshot running guests BEFORE the restart ----------------------------
# vm-N with a qemu.pid whose pid is alive: these must survive the restart.
declare -A GUEST_PID=()
echo "running guests before restart:"
before_any=0
for pidf in "$VMROOT"/vm-*/qemu.pid; do
  [ -f "$pidf" ] || continue
  pid="$(cat "$pidf" 2>/dev/null || true)"
  [ -n "$pid" ] || continue
  vm="$(basename "$(dirname "$pidf")")"
  if kill -0 "$pid" 2>/dev/null; then
    GUEST_PID["$vm"]="$pid"
    before_any=1
    printf '  %-8s pid %s  %s\n' "$vm" "$pid" \
      "$(ps -o cmd= -p "$pid" 2>/dev/null | cut -c1-60)"
  fi
done
[ "$before_any" = 1 ] || echo "  (none)"
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
      printf '  %-8s pid %s  ALIVE  %s\n' "$vm" "$pid" \
        "$(ps -o cmd= -p "$pid" 2>/dev/null | cut -c1-50)"
    else
      printf '  %-8s pid %s  *** GONE ***\n' "$vm" "$pid"
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
