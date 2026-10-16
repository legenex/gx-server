#!/usr/bin/env bash
# mem_watch.sh — sample MemAvailable (GiB) on both nodes until stopped.
#   mem_watch.sh OUTFILE [interval_s] [worker_ssh]     (run in the background, TERM to stop)
# One line per sample: "HH:MM:SS<TAB>head<TAB>worker". A reading that cannot
# be taken is written as NA (never a number), so accept_eval.py fails on it.
set -uo pipefail
out="${1:?outfile}"; every="${2:-5}"; worker="${3:-legenex-02@10.60.21.41}"
: > "$out"
trap 'exit 0' TERM INT
while :; do
    h=$(awk '/^MemAvailable:/{printf "%.1f",$2/1048576}' /proc/meminfo 2>/dev/null)
    w=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$worker" "awk '/^MemAvailable:/{printf \"%.1f\",\$2/1048576}' /proc/meminfo" 2>/dev/null)
    [[ "$h" =~ ^[0-9]+(\.[0-9]+)?$ ]] || h=NA
    [[ "$w" =~ ^[0-9]+(\.[0-9]+)?$ ]] || w=NA
    printf '%s\t%s\t%s\n' "$(date +%T)" "$h" "$w" >> "$out"
    sleep "$every"
done
