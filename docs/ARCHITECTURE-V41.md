# GX Cluster V4.1 Architecture — DeepSeek V4.1 Flash rebuild (2026-09-27)

Supersedes the old L-6/L-10 topology (SGLang NVFP4 gx-max + 11 aliases) per the
mission brief. This file is the implementation contract for the rebuild.

## 1. Target topology

- Two separate 128 GB GB10 nodes, kernel 6.17.0-1032-nvidia, driver 580.173.02 (pinned).
- gx10-01 (legenex): head/orchestrator. gx10-02 (legenex-02): worker rank 1.
- Public aliases: **gx-max, gx-auto** ONLY.
- Runtime: MiaAI 2× DGX Spark EXL3 kit (submodule `mia-dsv41` @ `6f7d1590ad49a2b8995188e45d7b9db31e677452`),
  vLLM-based, OpenAI API on head `:8888` (loopback only; never public).
- Weights: EXL3 pack (stock Mia-AiLab, rev `64ba41b6c916a587db06eae2e19b7845f7be6e6b`) and
  the UNCENSORED production candidate (dealignai CRACK, rev `8a27b35fc5b145fa05ee965c7d7b243b047915f7`),
  at `/srv/models/dsv41/…`; Engram = deepseek-ai/DeepSeek-V4.1-Flash rev `dba1be0a40aa45a94ad051997016db3960a90277`
  shards 47+48 (same ENGRAM_DIR for both packs). Worker weight sync: `WEIGHT_SYNC=rsync` over LAN SSH.
- NCCL fabric: both ConnectX rails, **NCCL_IB_GID_INDEX=3** (proven, see state/EVIDENCE.md).
- No sudo anywhere: Docker + systemctl --user only; CDI GPU passthrough.

## 2. Registry v2 (`legenex/models/registry.json`, schema 2)

Configuration-driven; dashboard, orchestrator, and gateway must read from it —
no hardcoded model/node names in code. New shape:

```
{ "schema": 2,
  "cluster": {"name": "legenex-dual-gx10", "head": "gx10-01", "worker": "gx10-02"},
  "nodes": {
    "gx10-01": {"role": "head", "user": "legenex", "lan_ip": "10.60.21.37",
      "tailscale_ip": "100.105.214.61",
      "fabric": {"rail1": "192.168.100.10", "rail2": "192.168.101.10"},
      "hcas": ["rocep1s0f0", "roceP2p1s0f0"], "ssh": "gx10-01"},
    "gx10-02": {"role": "worker", "user": "legenex-02", "lan_ip": "10.60.21.41",
      "tailscale_ip": "100.73.238.4",
      "fabric": {"rail1": "192.168.100.11", "rail2": "192.168.101.11"},
      "hcas": ["rocep1s0f0", "roceP2p1s0f0"], "ssh": "10.60.21.41"}},
  "runtimes": {
    "mia-dsv41": {"kind": "mia-2x-gb10-exl3", "submodule": "mia-dsv41",
      "commit": "6f7d1590ad49a2b8995188e45d7b9db31e677452",
      "image": "ghcr.io/miaai-lab/deepseek-v4.1-flash-exl3-2x-dgx-sparks:2.9bpw",
      "api": "http://127.0.0.1:8888/v1", "served_model_id": "DeepSeek-v4.1-Flash-EXL3",
      "start": "./start.sh", "stop": "./stop.sh", "status": "./start.sh status"}},
  "models": {
    "dsv41-flash-exl3-stock": {"source": "Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
      "revision": "64ba41b6c916a587db06eae2e19b7845f7be6e6b",
      "path": "/srv/models/dsv41/model", "uncensored": false,
      "engram_dir": "/srv/models/dsv41/engram-src", "quant": "exl3-2.9bpw-mul1",
      "vision": true, "tools": true, "max_context": 600000},
    "dsv41-flash-exl3-uncensored": {"source": "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw",
      "revision": "8a27b35fc5b145fa05ee965c7d7b243b047915f7",
      "path": "/srv/models/dsv41/uncensored", "uncensored": true,
      "engram_dir": "/srv/models/dsv41/engram-src", "quant": "exl3-2.9bpw-mul1",
      "vision": true, "tools": true, "max_context": 262144,
      "serving_notes": {"gpu_mem_util": 0.85, "kv_bytes": 1073741824,
        "max_num_batched_tokens": 2048, "vllm_sparse_indexer_max_logits_mb": 256}}},
  "aliases": {
    "gx-max": {"model": "<production uncensored model id>", "runtime": "mia-dsv41",
      "mode": "direct", "description": "Explicit DeepSeek V4.1 Flash (uncensored)"},
    "gx-auto": {"model": "<production uncensored model id>", "runtime": "mia-dsv41",
      "mode": "auto", "description": "Profile/reasoning auto-selection"}},
  "profiles": {
    "fast":     {"max_num_seqs": 1, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "medium", "target": "single interactive"},
    "balanced": {"max_num_seqs": 2, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "medium", "target": "AgentOS default, 2 gens"},
    "swarm":    {"max_num_seqs": 4, "spec_method": "none",
                 "max_model_len": 262144, "reasoning_default": "low", "target": "many logical agents, 4 gens"},
    "deep":     {"max_num_seqs": 2, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "max", "target": "architecture/review, 1-2 streams"},
    "long":     {"max_num_seqs": 1, "spec_method": "dspark",
                 "max_model_len": 600000, "reasoning_default": "high", "target": "large repo context, TTFT warn"},
    "custom":   {"bounded": true, "max_num_seqs": [1, 4], "max_model_len": [8192, 600000],
                 "reasoning_default": "high", "target": "advanced, bounded"}},
  "reasoning": {
    "levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
    "mapping": {"none": {"enable_thinking": false}, "minimal": {"enable_thinking": false},
                "low": {"reasoning_effort": 50}, "medium": {"reasoning_effort": 62},
                "high": {"reasoning_effort": 75}, "xhigh": {"reasoning_effort": 90},
                "max": {"reasoning_effort": 100}},
    "numeric_range": [1, 100]},
  "capabilities": {"vision": true, "tools": true, "structured_output": true, "reasoning": true}}
```

`gx-max` never silently falls back to stock; `uncensored: true` is the production
requirement and is verified by `ops/uncensor-verify` (behavioral suite).

## 3. Orchestrator v2 (`gx_orchestrator/`)

Refactor, keep stdlib-only + tests. Changes:

- `tiers.py` → replaced by `profiles.py` (profile table from registry; no model tiers).
- `config.py`: `GX_MAX_BASE=http://127.0.0.1:8888/v1`, `GX_MAX_MODEL_ID=DeepSeek-v4.1-Flash-EXL3`;
  registry path; queue paths under `/srv/projects/gx-cluster/state`.
- `health.py`: single-model health (Mia `/health` + `/v1/models`) per node + fabric checks.
- `lifecycle.py`: keep the DOWN→ACQUIRING→READY→RELEASING machine; start script =
  `mia-dsv41/start.sh` with profile env overlay; health = :8888 `/health` + real completion
  probe; stop = `mia-dsv41/stop.sh` + memory return verification; NO boot autoload.
- `classifier.py` → `autoroute.py`: deterministic intent→(profile, reasoning) mapping
  (no model choice — one model). Inputs: X-GX-Intent headers, content features, context size.
  Keep journal + tests style.
- `resource_guard.py`: sizing table updated (gx-max rank0/1 ≈ 105 GiB V4.1), rest reused.
- NEW `scheduler.py`: request-level admission queue (§4).
- `server.py`: routes + relay through scheduler; auth unchanged (constant-time bearer).
- `dual_worker.py`, old SGLang paths: removed.
- `status_cli.py`: updated to new status shape.

## 4. Scheduler (new `gx_orchestrator/scheduler.py`)

- Record: `id, project, agent, task, priority, profile, reasoning, state(queued|active|
  done|error|cancelled|timeout), enqueue_ts, start_ts, done_ts, timeout_at, prompt_tokens,
  completion_tokens, cached_tokens, ttft_ms, tps, error`.
- Priorities: `interactive(0) > critical-review(1) > orchestrator(2) > normal-worker(3) > background(4)`.
- Limits (from registry/`config`): global active per profile (max_num_seqs), per-project
  active cap (default 2), per-project queued cap (default 8). One project cannot starve
  others: strict priority + round-robin within priority across projects.
- Attribution: `X-GX-Project`, `X-GX-Agent`, `X-GX-Task`, `X-GX-Priority` headers (hooked
  from LiteLLM; defaults "unknown").
- Persistence: JSON state file under `/srv/projects/gx-cluster/state/scheduler/queue.json`,
  atomic write on every mutation; restore on restart (active→error "control-plane restart").
- APIs: status snapshot, cancel queued, cancel active (best effort), retry, history
  (ring buffer JSONL), metrics aggregation.
- Timeout: per-request soft timeout (default from profile; 600 s), expiry → cancelled+error.

## 5. Gateway (`legenex/gateway/`)

- `litellm/config.yaml`: aliases only `gx-max`, `gx-auto` → `http://host.docker.internal:18900/v1`.
  Keep: master key from env, no-fallback discipline, turn_off_message_logging, litellm-db.
  Drop gx-mini/gx-code/worker routes. Keep virtual-key pattern (hermes, open-webui,
  gx-computer) with trimmed model lists.
- `gx_hooks/gx_budget_hook.py`: add attribution header capture + queue metrics; keep
  privacy-safe JSONL (no full prompts).
- `gx-gateway-ts-proxy.py`: `PUBLIC_MODELS = ("gx-max", "gx-auto")`.
- llama-swap: RETIRED (old models deleted; Mia owns lifecycle). Compose: litellm + litellm-db only.

## 6. Dashboard — GX Cluster Control Center (`legenex/control-ui/`)

Refactor core, replace media. Keep: auth (scrypt/CSRF/sessions), ActionRunner, route
decorator, SPA skeleton, hostfacts. Remove: media_*, music*, voice, calls, live,
realtime, flows/, playground, wan_video, owui media sync. Pages (web/js/pages/*):
Overview, Model (controls+profiles+reasoning+effective values), Performance (live+history),
Requests, Agents, Tasks, Projects, Files (safe trash), Storage, Logs, Network, Updates,
Settings, Recovery, Jobs/Actions, Keys, Login. All data real (backend endpoints or explicit
NOT CONNECTED). Backend new modules: `requests_view.py`, `agentos_adapter.py`,
`projects_scanner.py`, `filemanager.py` (allowed roots + trash), `storage_scanner.py`,
`netview.py`, `updates_view.py`, `recovery_view.py`. Live updates: SSE channel for
queue + telemetry (new, small).

## 7. Allowed roots for the file manager

`/home/legenex/Documents/Projects`, `/home/legenex/Documents/Backups`,
`/home/legenex/Documents/Archive`, `/srv/models`, `/srv/cache`, `/srv/logs`.
Trash at `/srv/cache/trash/` with manifest (original path, ts, size). Purge is a separate,
audit-logged action. Never: /, /etc, /boot, /usr, active model files, protected backups.

## 8. gx CLI (`legenex/cli/gx.py`, symlinked to ~/.local/bin/gx)

`status, doctor, start, stop, restart, drain, max, auto, profile, queue, requests, logs,
benchmark, models, nodes, storage, backup, update`. Simple output, no Docker/systemd
knowledge needed. Reuses orchestrator + control-ui APIs (localhost).

## 9. Benchmarks

`ops/bench/`: reproducible suite (startup, load, TTFT, prefill/decode tps, aggregate,
memory low-water at boot/idle/short/32k/64k/128k/2-stream/4-stream, speculation on/off ×
1/2/4 streams, queue behavior) + history JSONL. `ops/bench/coding-bench/`: disposable repo
multi-agent workflow (orchestrator→inspector→architect→implementer(s)→tester→reviewer→
repair→validator) with full metrics.

## 10. Recovery

- gx-hostwatch REUSED (model-agnostic) + NEW DeepSeek watchdog (rank health, MemAvailable
  low-water vs measured marks, bounded restarts with exponential backoff, incident JSONL).
- gx-backup recipe updated to V4.1 (pins: submodule commit, image digest, model revisions,
  engram revision, env templates; restore = download + configure + smoke; non-destructive
  restore validation).
- Pre-V4.1 recovery retained via git tag `pre-deepseek-v41-rebuild-20260927` (both repos)
  + protected ZIPs (see state/EVIDENCE.md).

## 11. Hostinger KVM4 control plane

`ssh hermes-vps` (hermes@191.215.40.202, verified BatchMode) — root is human-gated.
Hermes/Buzz already deployed; DashFlo production lives there (do not disturb).
Plan: read-only live inventory first; Tailscale presence verified live (docs say tailnet
100.70.255.106); deploy GX control-plane stack (dashboard proxy/auth + scheduler state +
metrics history) as `docker compose` under the hermes account, no port collisions
(DashFlo on 4000 per mission brief — verify live). Fallback if rootless install impossible:
production-ready compose + docs for later install.

## 12. Security (unchanged disciplines)

Secrets in `/srv/projects/gx-cluster/secrets` or ignored `.env` only (PUBLIC repo!).
Dashboard auth mandatory; model-control APIs auth'd; raw :8888 loopback only; file manager
root-enforced; audit log for model ops, settings, files, cleanup, updates.
