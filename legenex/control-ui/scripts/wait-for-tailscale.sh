#!/usr/bin/env bash
# Wait (bounded) for this host's Tailscale IPv4 so the UI binds its management
# address as well as loopback. Same lesson as the orchestrator's
# wait-for-docker0.sh: a bind that races interface bring-up silently degrades
# to loopback-only for the life of the process.
#
# Never fails the start: after the deadline the UI still starts on loopback
# and logs that the Tailscale bind was skipped; systemd's Restart= retries
# only on a crash, so this keeps a broken tailnet from blocking local access.
set -u
for _ in $(seq 1 60); do
  ip="$(tailscale ip -4 2>/dev/null | head -n1)"
  if [ -n "${ip}" ] && ip -4 addr show 2>/dev/null | grep -q "inet ${ip}/"; then
    exit 0
  fi
  sleep 2
done
echo "wait-for-tailscale: no Tailscale IPv4 after 120 s; starting on loopback only" >&2
exit 0
