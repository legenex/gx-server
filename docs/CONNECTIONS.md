# Connections

Open **GX Cluster → Connections** (`http://100.105.214.61:8088/#/connections`).

That page is the live source of truth. It reads the running gateway and the `LITELLM_MASTER_KEY` from `legenex/gateway/.env` (admin session, masked until Reveal).

## Gateway

| Field | Production value |
|---|---|
| Status | Healthy when LiteLLM answers on loopback :4000 |
| Public OpenAI base URL | `http://100.105.214.61:4000/v1` (Tailscale proxy) |
| Internal URL (OpenWebUI on gx10-01) | `http://127.0.0.1:4000/v1` |
| API path | `/v1` |
| Auth | Bearer, enabled |
| Public models | `gx-mini`, `gx-code`, `gx-auto`, `gx-max` |

Docker still binds LiteLLM to loopback only (`GX_LITELLM_TS_BIND=127.0.0.2`) so compose can start at boot. `gx-gateway-ts-proxy.service` publishes the Tailscale address after Tailscale is up. `GET /v1/models` on Tailscale lists the four public modes. `gx-code-01` / `gx-code-02` remain callable on loopback for gx-code load-balancing and gx-max.

## Kilo Code

Installed on gx10-01: extension **7.7.9** (schema `~/.config/kilo/kilo.jsonc`).

- Provider: OpenAI Compatible (`@ai-sdk/openai-compatible`)
- Base URL: `http://100.105.214.61:4000/v1`
- API key: Reveal on the Connections page
- Recommended model: `gx-code`
- Alternatives: `gx-mini`, `gx-auto`, `gx-max`

## OpenWebUI

Host-network container on port 3000. Production connection (already applied):

- Type: External
- URL: `http://127.0.0.1:4000/v1`
- Auth: Bearer
- Key: gateway master key
- Model IDs: gx-auto, gx-mini, gx-code, gx-max
- Ollama: off

## curl

```
export GX_API_KEY=YOUR_GX_API_KEY
curl http://100.105.214.61:4000/v1/chat/completions \
  -H "Authorization: Bearer $GX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gx-code","messages":[{"role":"user","content":"Reply exactly CODE_OK"}]}'
```

Do not paste real keys into this file.
