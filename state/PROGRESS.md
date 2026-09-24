# PROGRESS

Updated: 2026-09-24T21:25:00Z

## Complete — daily-use closeout

- OpenWebUI Server Connection Error: host-network OWUI could not reach Tailscale `:4000` because LiteLLM is loopback-only (boot-safe). Connection persisted as `http://127.0.0.1:4000/v1`. Browser chats: gx-mini MINI_OK, gx-code CODE_OK, gx-auto Hello, gx-max MAX_OK + dual-worker banner.
- Public Tailscale `/v1/models` filtered to four modes via `gx-gateway-ts-proxy`. Internal gx-code-01/02 still callable.
- Connections page in GX Cluster primary nav (live URL, masked/reveal key, Kilo 7.7.9, OpenWebUI, curl/Python, Test Gateway).
- AgentOS Control Center + supervisor pointed at `Documents/Projects/AgentOS`, model `gx-code`. `/api/ask-live` returned AGENTOS_OK via gx-code.
- gx-max streaming for OpenWebUI: orchestrator emits SSE of the completed dual-worker answer.
- Restore drill of `eaf40302` passed; LiteLLM dump restored in throwaway Postgres; OpenWebUI db integrity ok.
- Secret scan PASS. `gx status` / `gx doctor` 13/13.

## Remaining (not project failures)

- Offsite mutable restic: **NOT CONFIGURED**
- Hermes VPS apply: **DEFERRED**
- OpenWebUI gx-auto card still *describes* gx-fast/gx-reason; picker/API expose only the four public modes
- OpenWebUI offers v0.11.4 in a toast; **do not update** unless asked

## Post-reboot (2026-09-24 22:31/22:36)

Proven: new `who -b` timestamps, authenticated four-model list, worker inference, four OpenWebUI browser chats with assistant rows in `webui.db`, Connections, AgentOS, playground/media still gone. Dump warning was stale `/home/legenex/agentos` find root; removed; dump replay exit 0.
