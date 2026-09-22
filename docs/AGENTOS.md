# AgentOS

Live process: `/home/legenex/agentos` via `agentos-control-center.service`.

Git checkout: `/home/legenex/Documents/Projects/AgentOS` (same GitHub `legenex/agent-os`).

Gateway: `http://100.105.214.61:4000/v1` (`GX10_GATEWAY_KEY`).

Policy file: `hermes/model-routing.yaml`

| Role | Model |
|---|---|
| Default / coding | `gx-code` |
| Background / auto | `gx-auto` |
| Classifiers | `gx-mini` |
| Review | `gx-max` |

Control Center env `AGENTOS_CC_LLM_MODEL=gx-code`.

Do not rebuild AgentOS. Hermes VPS reconcile was not run from this session (would need the VPS). Local yaml is updated; apply on the VPS with `scripts/reconcile-model-routing.py` when that host is next touched.
