#!/usr/bin/env bash
# gx-max-status.sh — report the real state of the two-node engine.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"

# tr -d: docker inspect emits a blank line on stdout for a missing container
r0=$(docker inspect -f '{{.State.Status}}' "${GXMAX_RANK0_NAME}" 2>/dev/null | tr -d '[:space:]')
r1=$(n2 "docker inspect -f '{{.State.Status}}' ${GXMAX_RANK1_NAME} 2>/dev/null" 2>/dev/null | tr -d '[:space:]')

echo "=== gx-max status ==="
printf 'rank0 (node1)   : %s\n' "${r0:-absent}"
printf 'rank1 (node2)   : %s\n' "${r1:-absent}"

if gxmax_healthy; then
  echo "http health     : OK (200)"
  model=$(curl -fsS -m 5 "http://127.0.0.1:${GXMAX_PORT}/v1/models" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null)
  printf 'served model    : %s\n' "${model:-?}"
  printf 'in flight       : %s\n' "$(gxmax_inflight)"
else
  echo "http health     : DOWN"
fi

printf 'MemAvailable    : node1=%sGiB node2=%sGiB\n' \
  "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" \
  "$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" 2>/dev/null || echo '?')"

echo "--- RoCE fabric ---"
rdma link show 2>/dev/null | sed 's/^/  /' || echo "  (rdma tool unavailable)"
for d in /sys/class/infiniband/*/; do
  n=$(basename "$d")
  x=$(cat "$d/ports/1/counters/port_xmit_data" 2>/dev/null || echo 0)
  printf '  %-14s cumulative xmit: %.2f GB\n' "$n" "$(echo "$x*4/1000000000" | bc -l)"
done
