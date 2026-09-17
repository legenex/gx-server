#!/usr/bin/env bash
# 1 Hz memory sampler (BUILD_V3 rule 2): MemAvailable, swap and whether the
# gx-voice engine container exists, as CSV. Runs on the node it measures.
#   mem-sampler.sh OUT.csv [SECONDS] [INTERVAL]      (SECONDS=0: until killed)
set -euo pipefail
out="${1:?usage: mem-sampler.sh OUT.csv [SECONDS] [INTERVAL]}"
seconds="${2:-0}"
interval="${3:-1}"
container="${GX_VOICE_ENGINE_CONTAINER:-gx-voice-engine}"
mkdir -p "$(dirname "$out")"
echo "epoch,mem_available_gib,swap_used_gib,engine_present" > "$out"
start=$(date +%s)
while :; do
  now=$(date +%s.%N)
  read -r avail swapt swapf < <(awk '/^MemAvailable:/{a=$2} /^SwapTotal:/{t=$2} /^SwapFree:/{f=$2} END{print a, t, f}' /proc/meminfo)
  present=0
  docker inspect -f '{{.Id}}' "$container" >/dev/null 2>&1 && present=1
  awk -v n="$now" -v a="$avail" -v t="$swapt" -v f="$swapf" -v p="$present" \
    'BEGIN{printf "%.3f,%.2f,%.2f,%d\n", n, a/1048576, (t-f)/1048576, p}' >> "$out"
  if [ "$seconds" != 0 ] && [ $(( $(date +%s) - start )) -ge "$seconds" ]; then break; fi
  sleep "$interval"
done
