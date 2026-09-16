#!/bin/bash
# Read-only verification of the GX10 kernel lock (L-4). Safe to run any time,
# no root needed (checks that need root degrade to SKIP, never to PASS).
#
# 2026-09-16 fixes (D-027):
#  * Check 3 required dpkg's abbreviated status to start with "ii". A HELD
#    installed package reports "hi" (desired=hold, status=installed), so the
#    correctly held locked kernel was reported MISSING. It now checks the
#    real installed state (db:Status-Status) and reports the selection
#    separately.
#  * Check 5 failed on ANY simulated linux-* install. A dist-upgrade that
#    installs unrelated, OLDER-ABI kernel flavours (6.8.0-1062-nvidia*) does
#    not touch the held packages, the locked 6.17 set, or the name-based
#    GRUB_DEFAULT pin. Proposals are now classified:
#      THREAT (FAIL) - removes any linux-* package, or installs/upgrades a
#                      held package, the locked 6.17 set, or an HWE meta
#      NEWER  (WARN) - installs a kernel ABI newer than the locked one; the
#                      GRUB pin still boots 6.17, but a newer image on disk
#                      is what a broken pin would fall through to
#      OTHER  (INFO) - unrelated/older kernel flavours; no effect on the pin
LOCKED_KVER="6.17.0-1032-nvidia"
LOCKED_SHORT="6.17.0-1032"
PASS=0; FAIL=0; WARN=0
ck(){ case "$2" in
  ok)   echo "  [PASS] $1"; PASS=$((PASS+1)) ;;
  warn) echo "  [WARN] $1"; WARN=$((WARN+1)) ;;
  skip) echo "  [SKIP] $1" ;;
  *)    echo "  [FAIL] $1"; FAIL=$((FAIL+1)) ;;
esac; }

echo "=========== $(hostname) ==========="

echo "1. Running kernel"
echo "     $(uname -r)"
[ "$(uname -r)" = "$LOCKED_KVER" ] && ck "running kernel is $LOCKED_KVER" ok || ck "running kernel is NOT $LOCKED_KVER" no

echo "2. GRUB_DEFAULT pin"
GD=$(grep -E '^[[:space:]]*GRUB_DEFAULT=' /etc/default/grub)
echo "     $GD"
echo "$GD" | grep -q "$LOCKED_KVER" && ck "GRUB_DEFAULT still points at $LOCKED_KVER" ok || ck "GRUB_DEFAULT no longer points at $LOCKED_KVER" no
grep -rqE '^[[:space:]]*GRUB_DEFAULT=' /etc/default/grub.d/ 2>/dev/null \
  && ck "a grub.d snippet overrides GRUB_DEFAULT" no || ck "no grub.d override of GRUB_DEFAULT" ok
if [ -r /boot/grub/grub.cfg ]; then
  grep -q "with Linux $LOCKED_KVER'" /boot/grub/grub.cfg \
    && ck "generated grub.cfg contains the pinned menu entry" ok \
    || ck "generated grub.cfg has NO '$LOCKED_KVER' menu entry (pin would dangle)" no
else
  ck "grub.cfg not readable without root; menu entry not re-checked" skip
fi

echo "3. Locked kernel still installed on disk"
ST=$(dpkg-query -W -f='${db:Status-Status}' "linux-image-$LOCKED_KVER" 2>/dev/null)
WANT=$(dpkg-query -W -f='${db:Status-Want}' "linux-image-$LOCKED_KVER" 2>/dev/null)
echo "     linux-image-$LOCKED_KVER: status=${ST:-absent} selection=${WANT:-?}"
[ "$ST" = "installed" ] && ck "linux-image-$LOCKED_KVER installed (selection: ${WANT})" ok \
  || ck "linux-image-$LOCKED_KVER NOT installed (status=${ST:-absent})" no
MST=$(dpkg-query -W -f='${db:Status-Status}' "linux-modules-$LOCKED_KVER" 2>/dev/null)
[ "$MST" = "installed" ] && ck "linux-modules-$LOCKED_KVER installed" ok || ck "linux-modules-$LOCKED_KVER NOT installed (status=${MST:-absent})" no
[ -f "/boot/vmlinuz-$LOCKED_KVER" ] && [ -f "/boot/initrd.img-$LOCKED_KVER" ] \
  && ck "/boot has vmlinuz and initrd for $LOCKED_KVER" ok || ck "/boot is missing vmlinuz or initrd for $LOCKED_KVER" no

echo "4. Held kernel packages"
HELD=$(apt-mark showhold 2>/dev/null | sort)
if [ -z "$HELD" ]; then echo "     (none)"; else echo "$HELD" | sed 's/^/     /'; fi
NMETA=$(echo "$HELD" | grep -cE -- '-nvidia-hwe-24\.04$')
NLOCK=$(echo "$HELD" | grep -cE -- '6\.17\.0-1032')
[ "$NMETA" -ge 5 ] && ck "all 5 NVIDIA HWE meta-packages held (found $NMETA)" ok || ck "only $NMETA/5 HWE meta-packages held" no
[ "$NLOCK" -ge 8 ] && ck "locked 6.17 runtime set held (found $NLOCK)" ok || ck "only $NLOCK/8 locked 6.17 packages held" no

# classify_line "Inst pkg [old] (new ...)" -> THREAT|NEWER|OTHER
ver_gt_locked(){ # $1 = X.Y.Z-N ; true if newer than LOCKED_SHORT
  [ "$1" != "$LOCKED_SHORT" ] && [ "$(printf '%s\n%s\n' "$1" "$LOCKED_SHORT" | sort -V | tail -1)" = "$1" ]
}
classify_line(){
  local op pkg abi
  op=$(echo "$1" | awk '{print $1}'); pkg=$(echo "$1" | awk '{print $2}')
  if [ "$op" = "Remv" ]; then echo THREAT; return; fi
  if echo "$HELD" | grep -qx -- "$pkg"; then echo THREAT; return; fi
  case "$pkg" in *"$LOCKED_SHORT"*|*-nvidia-hwe-24.04) echo THREAT; return ;; esac
  abi=$(echo "$pkg" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+-[0-9]+' | head -1)
  if [ -n "$abi" ] && ver_gt_locked "$abi"; then echo NEWER; return; fi
  echo OTHER
}

echo "5. Simulated apt upgrade / full-upgrade cannot touch the locked boot path"
for op in upgrade dist-upgrade; do
  OUT=$(apt-get -s $op 2>/dev/null | grep -E '^(Inst|Remv)' | grep -E ' linux-(image|modules|headers|tools|nvidia|signed|generic)' || true)
  if [ -z "$OUT" ]; then
    ck "apt-get $op proposes no kernel packages" ok; continue
  fi
  T=""; N=""; O=""
  while IFS= read -r l; do
    case "$(classify_line "$l")" in
      THREAT) T+="$l"$'\n' ;; NEWER) N+="$l"$'\n' ;; *) O+="$l"$'\n' ;;
    esac
  done <<< "$OUT"
  if [ -n "$T" ]; then ck "apt-get $op WOULD touch the locked/held kernel set:" no; printf '%s' "$T" | sed 's/^/       /'
  else ck "apt-get $op touches no held/locked kernel package and removes nothing" ok; fi
  [ -n "$N" ] && { ck "apt-get $op would add a NEWER kernel ABI (GRUB pin still boots $LOCKED_KVER):" warn; printf '%s' "$N" | sed 's/^/       /'; }
  [ -n "$O" ] && { echo "  [INFO] apt-get $op would add $(printf '%s' "$O" | grep -c .) unrelated older-ABI kernel package(s); no effect on the pin:"; printf '%s' "$O" | awk '{print "       "$1" "$2}'; }
done

echo "6. Simulated autoremove cannot delete the locked kernel"
OUT=$(apt-get -s autoremove 2>/dev/null | grep -E '^Remv' | grep -E 'linux-' || true)
[ -z "$OUT" ] && ck "autoremove would remove no kernel packages" ok \
  || { ck "autoremove WOULD remove kernel packages:" no; echo "$OUT" | sed 's/^/       /'; }

echo "7. Unattended upgrade paths"
[ "$(dpkg-query -W -f='${db:Status-Status}' unattended-upgrades 2>/dev/null)" = installed ] \
  && ck "unattended-upgrades is INSTALLED - review it" no || ck "unattended-upgrades not installed" ok
systemctl show nvidia-spark-run-apt-upgrade-once.service -p ConditionResult 2>/dev/null | grep -q 'ConditionResult=no' \
  && ck "nvidia-spark-run-apt-upgrade-once is inert (done-flag set)" ok \
  || ck "nvidia-spark-run-apt-upgrade-once could still fire full-upgrade+reboot" no

echo
echo "  ---- $(hostname): $PASS passed, $WARN warnings, $FAIL failed ----"
[ "$FAIL" -eq 0 ] || exit 1
