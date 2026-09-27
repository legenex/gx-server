# AgentOS adapter facts (for dashboard integration, from read-only map 2026-09-27)

- AgentOS Control Center runs ON gx10-01: http://127.0.0.1:4173 (also Tailscale
  100.105.214.61). Stdlib HTTP, no auth (private-net + same-origin enforcement).
- READ-ONLY APIs usable by the GX dashboard Agents/Tasks/Projects pages:
  - GET /api/hermes → profiles[] {profile, model, gateway, alias, distribution},
    gateways[] {profile, running, note} — COARSE state only (running/stopped).
  - GET /api/kanban (?board=<slug>) → board counts {ready,todo,blocked,done,archived},
    cards[] {id,title,status,assignee,priority,board}. NO dependency graph.
  - GET /api/projects → merged local descriptors + live Hermes registry (slug, name,
    lifecycle_state, kanban_board, primary_profiles, registration, live_counts).
  - GET /api/buzz, /api/routing, /api/provider-health, /api/gx10, /api/history.
- NOT SUPPORTED upstream (dashboard must NOT fabricate buttons/data):
  fine-grained agent states (queued/tool-use/reviewing), per-agent metrics
  (calls/tokens/duration), pause/resume/cancel, task dependency graph.
- Attribution: AgentOS/Hermes does NOT send X-GX-* headers today. The in-repo
  Control Center LLM client (integrations/agentos-control-center/app/config.py,
  llm_headers) can be patched to add them (small, in-repo change — do it in the
  AgentOS-integration phase). Hermes-core clients live on the VPS (out of scope now;
  documented as follow-up).
- Repointing AgentOS at the new model: canonical hermes/model-routing.yaml
  (gx-gateway provider, models gx-*) + reconcile-model-routing.py; Control Center
  env AGENTOS_CC_LLM_MODEL (default gx-code today) → gx-auto.
