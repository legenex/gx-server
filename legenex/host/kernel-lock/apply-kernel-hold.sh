#!/bin/bash
# GX10 kernel-migration guard.
# Freezes the NVIDIA HWE kernel meta-packages and protects the locked 6.17 runtime set.
# Derives package names from what is actually installed - nothing is hardcoded.
#
#   ./apply-kernel-hold.sh           # dry run, shows the plan only
#   sudo ./apply-kernel-hold.sh --apply
#
# Reversal: see REVERSAL.md (or ./apply-kernel-hold.sh --unhold)

set -Eeuo pipefail

LOCKED_KVER="6.17.0-1032-nvidia"
LOCKED_SHORT="6.17.0-1032"
MODE="${1:-}"

die() { echo "REFUSING: $*" >&2; exit 1; }

# ---- safety gates ------------------------------------------------------
[ "$(uname -r)" = "$LOCKED_KVER" ] \
  || die "running kernel is $(uname -r), expected $LOCKED_KVER"

grep -qE "^[[:space:]]*GRUB_DEFAULT=.*${LOCKED_KVER}" /etc/default/grub \
  || die "GRUB_DEFAULT in /etc/default/grub does not reference $LOCKED_KVER"

# ---- derive package sets from the live dpkg database -------------------
# Group 1: every installed NVIDIA HWE meta-package (name ends -nvidia-hwe-24.04).
mapfile -t METAS < <(
  dpkg-query -W -f='${db:Status-Abbrev}\t${Package}\n' 2>/dev/null \
    | awk -F'\t' '$1 ~ /^ii/ {print $2}' \
    | grep -E -- '-nvidia-hwe-24\.04$' | sort
)

# Group 2: every installed package belonging to the locked 6.17 kernel build.
mapfile -t LOCKED < <(
  dpkg-query -W -f='${db:Status-Abbrev}\t${Package}\n' 2>/dev/null \
    | awk -F'\t' '$1 ~ /^ii/ {print $2}' \
    | grep -E -- "(^linux-.*${LOCKED_SHORT}(-nvidia)?$)" | sort
)

[ "${#METAS[@]}" -gt 0 ]  || die "no *-nvidia-hwe-24.04 meta-packages found"
[ "${#LOCKED[@]}" -gt 0 ] || die "no installed packages found for kernel $LOCKED_SHORT"

ALL=( "${METAS[@]}" "${LOCKED[@]}" )

echo "=== node: $(hostname)  running kernel: $(uname -r) ==="
echo
echo "Group 1 - NVIDIA HWE meta-packages to HOLD (freezes the meta so apt"
echo "          cannot pull a newer kernel ABI in behind it):"
for p in "${METAS[@]}"; do
  printf "   %-50s %s\n" "$p" "$(dpkg-query -W -f='${Version}' "$p")"
done
echo
echo "Group 2 - locked $LOCKED_SHORT runtime packages to HOLD (stops apt"
echo "          remove/autoremove from deleting the kernel GRUB is pinned to):"
for p in "${LOCKED[@]}"; do
  printf "   %-50s %s\n" "$p" "$(dpkg-query -W -f='${Version}' "$p")"
done
echo

case "$MODE" in
  --apply)
    [ "$(id -u)" -eq 0 ] || die "--apply needs root (use sudo)"
    apt-mark hold "${ALL[@]}"
    echo
    echo "=== holds now in place on $(hostname) ==="
    apt-mark showhold | sort | sed 's/^/  /'
    ;;
  --unhold)
    [ "$(id -u)" -eq 0 ] || die "--unhold needs root (use sudo)"
    apt-mark unhold "${ALL[@]}"
    echo
    echo "=== remaining holds on $(hostname) ==="
    apt-mark showhold | sort | sed 's/^/  /' || true
    ;;
  *)
    echo "DRY RUN - nothing changed. Re-run with: sudo $0 --apply"
    ;;
esac
