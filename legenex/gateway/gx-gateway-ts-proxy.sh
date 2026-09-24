#!/usr/bin/env bash
# Bind the OpenAI-compatible gateway on this host's Tailscale IPv4 after
# Tailscale is up. Docker keeps GX_LITELLM_BIND=127.0.0.1 (and the dummy
# GX_LITELLM_TS_BIND=127.0.0.2) so compose can start at boot before the
# tailscale0 address exists. External clients (Kilo Code, SDKs) use this
# userspace proxy: <tailscale-ipv4>:4000 -> 127.0.0.1:4000.
set -euo pipefail

deadline=$((SECONDS + 120))
ip=""
while (( SECONDS < deadline )); do
  ip="$(tailscale ip -4 2>/dev/null | head -n1 || true)"
  if [[ -n "${ip}" && "${ip}" == 100.* ]] && ip -4 addr show 2>/dev/null | grep -q "inet ${ip}/"; then
    break
  fi
  ip=""
  sleep 2
done

if [[ -z "${ip}" ]]; then
  echo "gx-gateway-ts-proxy: no Tailscale IPv4 after 120s" >&2
  exit 1
fi

echo "gx-gateway-ts-proxy: listening on ${ip}:4000 -> 127.0.0.1:4000" >&2
exec socat "TCP-LISTEN:4000,bind=${ip},fork,reuseaddr,keepalive" TCP:127.0.0.1:4000,keepalive
