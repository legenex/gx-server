#!/usr/bin/env bash
# wait-for-docker0.sh — block until docker0 has an IPv4 address, or fail.
#
# Exists because `After=docker.service` alone is not sufficient: dockerd
# being "started" per systemd does not guarantee docker0's address is
# assigned yet. See gx-orchestrator.service for the incident this fixes.
set -uo pipefail
for _ in $(seq 1 30); do
  if ip -4 addr show docker0 2>/dev/null | grep -q 'inet '; then
    exit 0
  fi
  sleep 1
done
echo "docker0 has no IPv4 address after 30s" >&2
exit 1
