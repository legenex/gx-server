#!/usr/bin/env bash
# gx-max-status.sh — report the real state of the two-node engine.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"

echo "=== gx-max status ==="
printf 'rank0 (node1)   : %s\n' "$(docker inspect -f '{{.State.Status}}' "${GXMAX_RANK0_NAME}" 2>/dev/null || echo absent)"
printf 'rank1 (node2)   : %s\n' "$(n2 "docker inspect -f '{{.State.Status}}' ${GXMAX_RANK1_NAME} 2>/dev/null || echo absent" 2>/dev/null || echo unreachable)"

if gxmax_healthy; then
  echo "http health     : OK (200)"
  echo "served model    : $(curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/v1/models" | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || echo '?')"
  echo "in flight       : $(gxmax_inflight)"
else
  echo "http health     : DOWN"
fi

echo "MemAvailable    : node1=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)GiB node2=$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" 2>/dev/null || echo '?')GiB"

echo "--- RoCE fabric ---"
rdma link show 2>/dev/null | awk '{print "  "$0}' || echo "  (rdma tool unavailable)"
for d in /sys/class/infiniband/*/; do
  n=$(basename "$d")
  x=$(cat "$d/ports/1/counters/port_xmit_data" 2>/dev/null || echo 0)
  printf '  %-14s cumulative xmit: %.2f GB\n' "$n" "$(echo "$x*4/1000000000" | bc -l)"
done
