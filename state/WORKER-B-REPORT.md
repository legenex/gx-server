# Worker B report — registry + gateway rewrite (V4.1 rebuild, 2026-09-27)

Scope honored: only `legenex/models/**`, `legenex/gateway/**`. No git commands
run (repo autosyncs). No secrets in any tracked file (env references only).

## What changed

### legenex/models/ (registry, schema 2)

- `registry.json` — REWRITTEN to schema 2 exactly per docs/ARCHITECTURE-V41.md
  §2: cluster/nodes (both IPs + both fabric rails + HCAs per node), top-level
  `fabric` NCCL notes (`NCCL_IB_GID_INDEX=3`, both rails,
  `NCCL_SOCKET_IFNAME=enP7s7`, `NCCL_IB_HCA`), `runtimes.mia-dsv41` (commit
  6f7d1590ad49a2b8995188e45d7b9db31e677452, image
  ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw, loopback
  `http://127.0.0.1:8888/v1`, served id `DeepSeek-v4.1-Flash-EXL3`,
  start/stop/status), models `dsv41-flash-exl3-stock` (Mia-AiLab rev
  64ba41b6c916a587db06eae2e19b7845f7be6e6b, /srv/models/dsv41/model, max_context
  600000) and `dsv41-flash-exl3-uncensored` (dealignai rev
  8a27b35fc5b145fa05ee965c7d7b243b047915f7, /srv/models/dsv41/uncensored,
  max_context 262144, serving_notes gpu_mem_util 0.85 / kv_bytes 1073741824 /
  max_num_batched_tokens 2048 / vllm_sparse_indexer_max_logits_mb 256), aliases
  `gx-max` (mode direct) + `gx-auto` (mode auto), both →
  dsv41-flash-exl3-uncensored, per-alias descriptions. Schema-1 ideas preserved:
  pinned revisions, identity blocks (DeepseekV41ForCausalLM, exl3 2.9bpw, MIT,
  vision/multimodal tags, verified-HF notes). Six profiles with the documented
  values; reasoning levels + mapping + numeric_range; capabilities.
- `registry.py` — NEW stdlib-only loader/validator (`load`/`loads`/`validate`,
  `RegistryError` collects every violation with precise messages). No I/O at
  import. Validator follows §2 exactly (spec_method optional — `custom` omits
  it, `long` omits `dspark_tokens`, both per the documented shape).
- `tests/test_registry.py` (+ `__init__.py` package markers) — NEW, 25 tests:
  shipped-file validation, required keys, IPv4 formats, runtime pins, both
  model packs + serving_notes, alias bindings, six profiles (max_num_seqs
  1..4), reasoning mapping completeness, no-secret scan, plus negative tests
  (bad IP/revision/mode/model/runtime, out-of-range seqs, mapping gap,
  missing profile, wrong schema, bad serving note, bad JSON).

### legenex/gateway/

- `litellm/config.yaml` — REWRITTEN: model_list only `gx-max` + `gx-auto` →
  `openai/` @ `http://host.docker.internal:18900/v1`, api_key
  `os.environ/GX_ORCHESTRATOR_API_KEY` (orchestrator accepts both alias names
  and maps them to `DeepSeek-v4.1-Flash-EXL3`). Kept + documented: empty
  fallbacks (explicit NO SILENT FALLBACK comment), `num_retries: 0`,
  `disable_cooldowns: true`, master key via `os.environ/LITELLM_MASTER_KEY`,
  `database_url: os.environ/DATABASE_URL` (added — previously env-only),
  `turn_off_message_logging: true`, `global_max_parallel_requests: 16` (raised,
  with the orchestrator-queue rationale comment), hook callback, health-check
  discipline, privacy settings. `model_info` per alias carries
  description/mode/uncensored/vision/tools/reasoning metadata. Comment notes
  that per-key rpms/parallel caps live in the LiteLLM DB via the UI.
- `litellm/gx_hooks/gx_budget_hook.py` — REFACTORED: (1) captures
  X-GX-Project/-Agent/-Task/-Intent/-Priority/-Profile/-Reasoning from the
  incoming request and forwards them via `data["forwarded_headers"]` (+ copy in
  `metadata["gx_attribution"]`, same mechanism the old hook used for its
  budget); (2) metrics line extended with project/agent/task/profile/reasoning
  + `queue_wait_ms` (parsed from the orchestrator's
  `x-gx-queue-wait-ms` response header via `_hidden_params.additional_headers`;
  still never full prompts); (3) per-tier clamps removed; context-budget guard
  kept for both aliases using gx_orchestrator.budget (import from /app/gx_lib
  wrapped in try/except — if the V4.1 orchestrator refactor drifts the budget
  interface, the guard degrades to pass-through, never raises into the request
  path). Context limit sourced from GX_REGISTRY (registry schema 2 alias →
  model max_context), fallback GX_MAX_CONTEXT_TOKENS, then 262144.
- `gx-gateway-ts-proxy.py` — `PUBLIC_MODELS = ("gx-max", "gx-auto")`; docstring
  updated; nothing else changed.
- `docker-compose.gateway.yml` — REWRITTEN: services `litellm` + `litellm-db`
  ONLY (llama-swap-node01 dropped, docker-socket mount gone with it). Kept:
  gx_gateway network, litellm config/hooks/gx_lib bind mounts exactly as
  before, postgres + DB volume, restart policies, loopback binds, extra_hosts
  host-gateway, healthchecks, mem/oom caps, logging. Env passthrough now:
  master key, DATABASE_URL assembly, UI creds, GX_ORCHESTRATOR_API_KEY,
  metrics log path, + GX_REGISTRY/GX_MAX_BASE/GX_MAX_MODEL_ID (safe defaults).
  Top comment documents the llama-swap retirement (2026-09-27) and that
  lifecycle is orchestrator → mia-dsv41.
- `.env.sample` — REWRITTEN: removed GX_SWAP_API_KEY, all llama-swap vars
  (GX_SWAP_*, GX_MINI_*, GX_LLAMA_SWAP_IMAGE, GX_SWAP_OOM/mem) and media/voice
  keys; kept LITELLM_MASTER_KEY, POSTGRES_PASSWORD, LITELLM_UI_USERNAME /
  PASSWORD, GX_ORCHESTRATOR_API_KEY, bind/port macros (GX_LITELLM_BIND/PORT,
  GX_LITELLM_TS_BIND, GX_PG_PORT, GX_NET_NAME), images, caps; ADDED
  GX_MAX_BASE / GX_MAX_MODEL_ID / GX_REGISTRY / GX_RUNTIME_DIR with safe
  defaults + comments.
- DELETED (retired): `llama-swap/` (node01.yaml, node02.yaml, env/*, dflash2/),
  `docker-compose.node02.yml`, and `deploy-node2.sh` (its sole purpose was
  deploying the node-2 llama-swap compose; keeping it would dangle). All
  recoverable via git tag `pre-deepseek-v41-rebuild-20260927`.
- `README.md` — REWRITTEN with the retirement note pointing at that tag, the
  two-alias resolution table, new port map, updated security notes.

## What passed

- `python3 -m unittest legenex.models.tests.test_registry` → 25/25 OK.
- `python3 -m py_compile` on registry.py, test_registry.py,
  gx_budget_hook.py, gx-gateway-ts-proxy.py → OK.
- `yaml.safe_load` on litellm/config.yaml and docker-compose.gateway.yml → OK
  (PyYAML 6.0.1 present).
- `docker compose -f docker-compose.gateway.yml config` → OK (validated
  against the live secrets env via the .env symlink).
- Grep over everything kept in legenex/gateway/** and legenex/models/**: no
  remaining functional references to llama-swap / GX_SWAP_* / old aliases /
  deleted files — only explanatory retirement comments.

## Deferred / notes for the parent

1. Root `.env.sample` left untouched: it is the legacy community-stack sample
   (GH_USER/LLM_ROOT_PATH/…). Its LITELLM_*/POSTGRES_* entries do mirror gateway
   vars, but the file serves the retired root compose scaffolding owned outside
   my scope — recommend the parent retire or update it wholesale.
2. Other workers' trees (orchestrator/, control-ui/, lifecycle/, scripts/,
   tests/) still reference llama-swap / old aliases — expected; they are
   mid-refactor per the work split. The hook's guarded budget import will
   survive their `tiers.py` → `profiles.py` change (tiers import removed here).
3. The compose now passes GX_REGISTRY (default
   /srv/projects/gx-cluster/state/registry.json) to the litellm container.
   Someone should add a step (deploy or orchestrator boot) that copies
   legenex/models/registry.json there, or point GX_REGISTRY in the real env at
   the checkout path.
4. `fake_stream` was dropped from both aliases: it existed for the old
   non-streaming dual-worker path. If the new orchestrator relays
   non-streaming, re-add it per its server behavior.
5. Virtual keys in the LiteLLM DB still carry old model lists; first deploy
   should trim each key's models to {gx-max, gx-auto} via the UI (documented in
   config.yaml and .env.sample).
