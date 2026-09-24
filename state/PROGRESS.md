# PROGRESS

Updated: 2026-09-24T10:20:00Z

## Complete — daily-use closeout

- OpenWebUI Server Connection Error: host-network OWUI could not reach Tailscale `:4000` because LiteLLM is loopback-only (boot-safe). Connection persisted as `http://127.0.0.1:4000/v1`. Browser chats: gx-mini MINI_OK, gx-code CODE_OK, gx-auto Hello, gx-max MAX_OK + dual-worker banner.
- Public Tailscale `/v1/models` filtered to four modes via `gx-gateway-ts-proxy`. Internal gx-code-01/02 still callable.
- Connections page in GX Cluster primary nav (live URL, masked/reveal key, Kilo 7.7.9, OpenWebUI, curl/Python, Test Gateway).
- AgentOS Control Center + supervisor pointed at `Documents/Projects/AgentOS`, model `gx-code`. `/api/ask-live` returned AGENTOS_OK via gx-code.
- gx-max streaming for OpenWebUI: orchestrator emits SSE of the completed dual-worker answer.
- Restore drill of `eaf40302` passed; LiteLLM dump restored in throwaway Postgres; OpenWebUI db integrity ok.
- Secret scan PASS. `gx status` / `gx doctor` 13/13.

## Remaining (not project failures)

- Reboot persistence: **BLOCKED BY EXTERNAL AUTHORITY** (`sudo` password required)
- Offsite mutable restic: **NOT CONFIGURED**
- Hermes VPS apply: **DEFERRED**
