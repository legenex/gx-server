# AgentOS

Canonical checkout and serving tree: `/home/legenex/Documents/Projects/AgentOS` via `agentos-control-center.service` and `agentos-supervisor.service` (user systemd, enabled).

`AGENTOS_CC_REPO` / `AGENTOS_CC_PROD_REPO` point at that checkout. Do not recreate `/home/legenex/agentos` unless you want a separate mirror.

Gateway: `http://127.0.0.1:4000/v1` (`AGENTOS_CC_LLM_MODEL=gx-code`). Do not rebuild AgentOS.

Policy file: `hermes/model-routing.yaml`

| Role | Model |
|---|---|
| Default / coding | `gx-code` |
| Background / auto | `gx-auto` |
| Classifiers | `gx-mini` |
| Review | `gx-max` |

Control Center: `http://127.0.0.1:4173`  
`AGENTOS_CC_LLM_URL=http://127.0.0.1:4000/v1`  
`AGENTOS_CC_LLM_MODEL=gx-code`

Verified 2026-09-23:

- Health: llm available, model `gx-code`
- One real job: `POST /api/ask-live` returned Python via gx-code (`llm_used: true`)
- Two concurrent `/api/ask-live` jobs: both `gx-code`, both `llm_used: true`
- gx-max is not a Control Center button; dual-worker gx-max is on the gateway (`gx-max` alias). Not rebuilt.

Hermes VPS reconcile was not applied remotely from this session. Local yaml is the intended config. **DEFERRED.**
