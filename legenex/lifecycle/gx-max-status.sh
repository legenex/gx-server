#!/usr/bin/env bash
# gx-max-status.sh (V4.1) — report the real state of the one two-node engine.
# The containers are the Mia kit's: dsv41-exl3-head on node 1,
# dsv41-exl3-worker on node 2. Health means /health AND /v1/models
# advertising the served model id -- a proxy answering without the model is
# a config fault, not a healthy engine.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${here}/lib.sh"

# tr -d: docker inspect emits a blank line on stdout for a missing container
r0=$(docker inspect -f '{{.State.Status}}' "${GXMAX_RANK0_NAME}" 2>/dev/null | tr -d '[:space:]')
r1=$(n2 "docker inspect -f '{{.State.Status}}' ${GXMAX_RANK1_NAME} 2>/dev/null" 2>/dev/null | tr -d '[:space:]')

echo "=== gx-max status ==="
printf 'head  (node1, %s) : %s\n' "${GXMAX_RANK0_NAME}" "${r0:-absent}"
printf 'worker(node2, %s) : %s\n' "${GXMAX_RANK1_NAME}" "${r1:-absent}"

if gxmax_healthy; then
  echo "http health      : OK (200)"
else
  echo "http health      : DOWN"
fi
if gxmax_serves_model; then
  echo "served model id  : ${GXMAX_SERVED_MODEL_ID} (verified)"
else
  echo "served model id  : NOT verified (expected ${GXMAX_SERVED_MODEL_ID})"
fi
printf 'in flight        : %s\n' "$(gxmax_inflight)"

printf 'MemAvailable     : node1=%sGiB node2=%sGiB\n' \
  "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" \
  "$(n2 "awk '/MemAvailable/{print int(\$2/1048576)}' /proc/meminfo" 2>/dev/null || echo '?')"

echo "--- RoCE fabric ---"
rdma link show 2>/dev/null | sed 's/^/  /' || echo "  (rdma tool unavailable)"
for d in /sys/class/infiniband/*/; do
  n=$(basename "$d")
  x=$(cat "$d/ports/1/counters/port_xmit_data" 2>/dev/null || echo 0)
  printf '  %-14s cumulative xmit: %.2f GB\n' "$n" "$(echo "$x*4/1000000000" | bc -l)"
done
