#!/usr/bin/env bash
# mem_watch.sh — sample MemAvailable on both nodes until stopped; print the minimum.
#   mem_watch.sh OUTFILE [interval_s] [worker_ssh]     (run in the background, kill to stop)
# Read-only (/proc/meminfo); used by accept.sh around the 8-way load.
set -uo pipefail
out="${1:?outfile}"; every="${2:-5}"; worker="${3:-legenex-02@10.60.21.41}"
: > "$out"
trap 'awk -F"\t" "NR==1||\$2<h{h=\$2} NR==1||\$3<w{w=\$3} END{printf \"min MemAvailable head=%.1f GiB worker=%.1f GiB over %d samples\n\", h, w, NR}" "$out"; exit 0' TERM INT
while :; do
    h=$(awk '/^MemAvailable:/{printf "%.1f",$2/1048576}' /proc/meminfo)
    w=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$worker" "awk '/^MemAvailable:/{printf \"%.1f\",\$2/1048576}' /proc/meminfo" 2>/dev/null || echo 999)
    printf '%s\t%s\t%s\n' "$(date +%T)" "$h" "$w" >> "$out"
    sleep "$every"
done
