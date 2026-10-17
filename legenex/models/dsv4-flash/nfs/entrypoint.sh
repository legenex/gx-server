#!/usr/bin/env bash
# nfs/entrypoint.sh — read-only NFSv4 export of the dsv4-flash weights from gx10-02 to the head.
#
# Runs inside the existing dsv41-exl3-nfs:local image (alpine + nfs-utils, the
# pattern the V4.1 kit already uses on gx10-01), started by start.sh as a
# privileged host-network container (kernel nfsd needs it). Differences from
# the V4.1 exporter, all to shrink exposure:
#   * NFSv4 ONLY: no rpcbind, no statd, no NFSv2/v3 listeners;
#   * nfsd binds the FABRIC address only (rpc.nfsd -H), never 0.0.0.0,
#     tailscale0 or the LAN;
#   * one client: the head's fabric IP; read-only; `secure` (privileged source
#     ports); all_squash to the weight owner's uid/gid, so files can stay
#     0640/0750 and no client is ever root on the export (no no_root_squash).
# On SIGTERM it unexports and stops the kernel nfsd threads it started.
set -euo pipefail

: "${NFS_BIND_IP:?fabric IP to bind nfsd to}"
: "${NFS_CLIENT_IP:?the only client allowed (head fabric IP)}"
: "${NFS_ANON_UID:?uid that owns the weights}"
: "${NFS_ANON_GID:?gid that owns the weights}"
EXPORT_DIR="${EXPORT_DIR:-/export}"

for v in "$NFS_BIND_IP" "$NFS_CLIENT_IP"; do
    [[ "$v" =~ ^192\.168\.10[01]\.[0-9]{1,3}$ ]] || { echo "refusing non-fabric address $v" >&2; exit 2; }
done
[[ "$NFS_ANON_UID" =~ ^[0-9]+$ && "$NFS_ANON_GID" =~ ^[0-9]+$ ]] || { echo "bad anon uid/gid" >&2; exit 2; }
[ -f "$EXPORT_DIR/config.json" ] || { echo "$EXPORT_DIR has no config.json" >&2; exit 2; }

mountpoint -q /proc/fs/nfsd || mount -t nfsd nfsd /proc/fs/nfsd
mkdir -p /var/lib/nfs/v4recovery /var/lib/nfs/rpc_pipefs
mountpoint -q /var/lib/nfs/rpc_pipefs || mount -t rpc_pipefs rpc_pipefs /var/lib/nfs/rpc_pipefs

printf '%s %s(ro,sync,no_subtree_check,secure,all_squash,anonuid=%s,anongid=%s,fsid=0)\n' \
    "$EXPORT_DIR" "$NFS_CLIENT_IP" "$NFS_ANON_UID" "$NFS_ANON_GID" > /etc/exports
exportfs -rav

stop() {
    echo "dsv4f-nfs: unexporting and stopping nfsd"
    exportfs -ua || true
    rpc.nfsd 0 || true
    kill "${mountd_pid:-0}" 2>/dev/null || true
    exit 0
}
trap stop TERM INT

# Kernel nfsd needs rpcbind/rpc_pipefs (same as the working V4.1 exporter).
# Bind rpcbind to the fabric address when the flag exists so port 111 is not
# on the LAN/tailscale. Fall back to rpcbind -w if -h is unsupported.
if rpcbind -w -h "$NFS_BIND_IP" 2>/tmp/rpcbind.err; then
    :
else
    echo "dsv4f-nfs: rpcbind -h unsupported ($(tr '\n' ' ' </tmp/rpcbind.err)); using rpcbind -w"
    rpcbind -w
fi
# Do not pass `--no-nfs-version 2`: alpine nfs-utils exits 1 with
# "2: Unsupported version" when the kernel already has v2 compiled out,
# and `set -e` then dies before nfsd listens (2026-10-17 window).
# `--no-nfs-version 3` is still required so the kernel does not leave +3 on.
rpc.nfsd --no-nfs-version 3 --no-udp --host "$NFS_BIND_IP" 8
vers="$(tr '\n' ' ' < /proc/fs/nfsd/versions 2>/dev/null || true)"
echo "dsv4f-nfs: nfsd versions: $vers"
echo " $vers " | grep -q '+4' || { echo "nfsd did not enable NFSv4" >&2; exit 1; }
if echo " $vers " | grep -q '+2'; then
    echo "dsv4f-nfs: refusing NFSv2 ($vers)" >&2
    exit 1
fi
# mountd still services the kernel's export-cache upcalls for v4; with v2/v3
# disabled it opens no network listener of its own.
rpc.mountd --foreground --no-udp &
mountd_pid=$!
echo "dsv4f-nfs: exporting $EXPORT_DIR to $NFS_CLIENT_IP on $NFS_BIND_IP:2049 (NFSv4, ro, all_squash)"
wait "$mountd_pid"
