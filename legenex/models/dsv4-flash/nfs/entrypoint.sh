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
mkdir -p /var/lib/nfs/v4recovery

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

# NFSv4 only, TCP only, fabric address only.
rpc.nfsd --no-nfs-version 2 --no-nfs-version 3 --no-udp --host "$NFS_BIND_IP" 8
cat /proc/fs/nfsd/versions || true
# mountd still services the kernel's export-cache upcalls for v4; with v2/v3
# disabled it opens no network listener of its own.
rpc.mountd --foreground --no-nfs-version 2 --no-nfs-version 3 --no-udp &
mountd_pid=$!
echo "dsv4f-nfs: exporting $EXPORT_DIR to $NFS_CLIENT_IP on $NFS_BIND_IP:2049 (NFSv4, ro, all_squash)"
wait "$mountd_pid"
