# Worker A report — orchestrator + lifecycle rebuild for DeepSeek V4.1 Flash EXL3

Date: 2026-09-27
Scope: `legenex/orchestrator/**` and `legenex/lifecycle/**` per ARCHITECTURE-V41.
The old model stack (gx-mini / gx-fast / gx-code / gx-reason, SGLang,
llama-swap, tiers.py, classifier.py, dual_worker.py) is RETIRED AND DELETED.
There is ONE model now: DeepSeek-V4.1-Flash-EXL3-2.9bpw (uncensored), served
by the Mia kit (vLLM, OpenAI API at http://127.0.0.1:8888/v1, served id
`DeepSeek-v4.1-Flash-EXL3`). Routing = choosing a (profile, reasoning) pair,
never a model.

## Status: COMPLETE — all suites green

| Suite | Command | Result |
|---|---|---|
| Orchestrator (Python) | `python3 -m unittest discover -s legenex/orchestrator/tests -t .` | **247 tests, OK** |
| Lifecycle bash (offline) | `GX_TESTS_OFFLINE=1 python3 -m unittest discover -s legenex/lifecycle/tests -t .` | **38 tests, OK** |
| Compile | `python3 -m py_compile gx_orchestrator/*.py tests/*.py` | OK |

## Files

New modules (`legenex/orchestrator/gx_orchestrator/`):
- `profiles.py` — registry v2 loader/validator (profiles, reasoning mapping,
  nodes, runtimes, models, aliases). Reasoning kwargs: none/minimal →
  `{"enable_thinking": false}`; low/medium/high/xhigh/max →
  `{"reasoning_effort": 50/62/75/90/100}`; numeric 1-100 accepted.
- `scheduler.py` — request-level admission: strict priorities
  (interactive=0 … background=4), round-robin across projects within a
  priority, global active capacity = the running profile's `max_num_seqs`
  (`set_capacity()` fired by the lifecycle's on_ready), per-project
  active/queued caps, global queued cap, 429-with-position rejection, atomic
  `queue.json` persistence (restore marks active→error "control-plane
  restart"), JSONL history ring with compaction + restart re-seed, timeout
  reaper, cancel (queued / active→cancelling, relays check per chunk),
  retry, drain, `wait()`.
- `autoroute.py` — gx-auto intent→(profile, reasoning): interactive→fast/medium,
  implementation→balanced/medium, architecture→deep/high, debugging→deep/medium
  (+max on hard-debug evidence), validation→deep/max, burst→swarm/low,
  long-context→long/high; `X-GX-Profile` / `X-GX-Reasoning` overrides
  (validated, unknown = 400); 96k long-context rule; envelope stripping +
  evidence dilution.
- `lifecycle.py` — `GxMaxLifecycle` Mia adapter: env overlay
  (SERVED_MODEL_NAME, MAX_NUM_SEQS, SPEC_METHOD, DSPARK_TOKENS,
  MAX_MODEL_LEN, MODEL_HOST, ENGRAM_DIR, NCCL_IB_GID_INDEX=3,
  WEIGHT_SYNC=rsync, serving_notes→KV env), readiness probe = /health +
  /v1/models id + a real "17*19=323" completion, phase markers parsed from
  the wrappers, release with container-exit + MemAvailable (±5 GiB)
  verification on BOTH nodes, profile switch = drain + stop + start, NO
  boot autoload (adopt-only), idle reaper, injectable
  `settle_seconds` / `mem_return_wait_s` for hermetic tests.
- `server.py` — see routes below. Queue-wait header per the gateway contract.
- `status_cli.py` — `gx status` (V4.1 shape, see below).
- `health.py` — head (/health + served-id verify), worker (SSH + container +
  fabric ping per rail), MemAvailable on both nodes, TTL cache.
- `config.py` — env-driven Config (GX_MAX_BASE=http://127.0.0.1:8888/v1,
  GX_MAX_MODEL_ID, GX_REGISTRY, scheduler knobs, container names
  dsv41-exl3-head / dsv41-exl3-worker).
- `resource_guard.py` — WORKLOAD_SIZING now only `gx-max-rank0` /
  `gx-max-rank1` @ 105 GiB (Mia containers).
- `budget.py`, `upstream.py` — unchanged, reused.

Deleted: `tiers.py`, `classifier.py`, `dual_worker.py`.

Lifecycle bash (`legenex/lifecycle/`):
- `gx-max-start.sh` — thin wrapper around `mia-dsv41/start.sh`: guard
  admission (D-025), node-2 hold (D-036), profile env overlay, phase markers,
  served-id-verified READY + startup seconds, ledger registration, unwind on
  failure after a container is launched.
- `gx-max-stop.sh` — graceful drain → stop head then worker → ledger release
  → hold clear → MemAvailable report → restore-normal. Markers preserved.
- `gx-max-unwind.sh` — kept (container-name driven via conf); ledger release
  IDs corrected to the workload ids `gx-max-rank0/rank1`.
- `gx-max-status.sh` — new container names + served-id verification.
- `gx-max.conf` — V4.1 rewrite: Mia kit dir, containers, port 8888, served
  id, profile overlay defaults; admission + safety numbers unchanged.
- `lib.sh` — SGLang arg builders removed; gxmax_ready/serves_model,
  vLLM /metrics in-flight, profile_env renderer.
- Kept unchanged: `resource-guard.sh`, `gx-max-safety.sh`, `node2-holds.sh`,
  `gx-safe-run.sh`, `restore-normal.sh`, `rank0-watch.sh`, `rank1-deadman.sh`
  (conf-driven container names now point at the dsv41 containers).

Tests: `registry_fixtures.py` (schema-2 fixture), `test_profiles.py`,
`test_scheduler.py`, `test_autoroute.py`, `test_lifecycle.py`,
`test_lifecycle_events.py`, `test_health.py`, `test_context_budget.py`,
`test_status_cli.py`, `test_relay.py`, `test_server.py`,
`test_resource_guard.py`, plus `server_harness.py` (scriptable stub
upstream + fake lifecycle + real Handler/Scheduler on ephemeral ports).
Deleted (retired): `test_classifier.py`, `test_kilo_routing.py`,
`test_mini_real_window.py`, `test_routing_d039.py`, `kilo_fixtures.py`,
`CLASSIFIER_TEST_MATRIX.md`.

## API routes (orchestrator, default 127.0.0.1:18900 + 172.17.0.1:18900)

Auth: every route except `/health` (`/healthz`, `/`) requires
`Authorization: Bearer $GX_ORCHESTRATOR_API_KEY` (constant-time; unset key ⇒
401 on everything).

### GET
- `/health` → `{"status":"ok","service":"gx-orchestrator"}` (no auth)
- `/health/detailed` → `{"status","model":<model block>,"queue":<scheduler status>,"gateway"}`
- `/text/status` → `{"model":<model block>,"queue":<scheduler status>,"aliases":{"gx-max":{"last_request":…},"gx-auto":{…}},"generated_at"}`
- `/v1/models` → `{"object":"list","data":[{"id":"gx-max",…},{"id":"gx-auto",…}]}`
  (exactly two aliases; anything else is a 400 at inference time)
- `/routing/decisions?request_id=…|fingerprint=…&limit=` → `{"data":[…]}`
  (gx-auto decision + completed records; features/reasons only, never prompt text)
- `/lifecycle/gx-max/status` → lifecycle status dict (below)
- `/lifecycle/gx-max/events?after=&limit=` →
  `{"seq","events":[{"seq","source","line"}],"active_job","history"}`
  (read-only; after/limit must be integers or 400)
- `/scheduler/status` → `{"capacity","active","queued","projects",
  {"<project>":{"active","queued"}},"oldest_wait_seconds","limits":{…},"records":[…],"generated_at"}`
- `/scheduler/history?limit=` → `{"data":[record,…]}`

### POST
- `/lifecycle/gx-max/acquire` `{"profile":"fast","timeout":900}` →
  `{"status":"ready", …lifecycle status…}`; unknown profile 400
  `invalid_profile`; acquisition failure 503 `gx_max_unavailable`.
- `/lifecycle/gx-max/release` `{"force":false}` → `{"status":"released",…}`
- `/lifecycle/gx-max/restart` `{"profile":"deep","force":false}` → release + acquire
- `/lifecycle/gx-max/drain` `{"timeout":300}` → `{"drained","still_active",…scheduler status}`
- `/scheduler/cancel` `{"id","reason"}` → `{"cancelled","state"|"reason"}`
- `/scheduler/retry` `{"id"}` → re-submitted record dict
- `/v1/chat/completions`, `/v1/completions` — body is OpenAI-shaped with
  `"model"` ∈ {`gx-max`, `gx-auto`} (else 400 `invalid_model`). Headers the
  orchestrator consumes: `X-GX-Request-Id`, `X-GX-Project`, `X-GX-Agent`,
  `X-GX-Task`, `X-GX-Intent`, `X-GX-Priority` (one of
  interactive|interactive-worker|normal-worker|agent-worker|background),
  `X-GX-Profile`, `X-GX-Reasoning`.

### Lifecycle status dict
`{"state":"down|acquiring|ready|releasing","since","last_used","waiters",
"detail","last_error","phase":"idle|preflight|overlay|loading|warming|ready|
serving|draining_requests|stopping_containers|memory_recovery|released|failed",
"phase_since","last_startup_seconds","idle_ttl","in_flight","profile",
"idle_seconds","seconds_in_state"}`

### Inference behaviour (gx-max and gx-auto)
1. Validate profile/reasoning (400 `invalid_profile` on unknown names —
   never a silent default).
2. Context budget on the PROFILE's `max_model_len`; certain overflow → 400
   `context_length_exceeded` with `error.gx_budget` and `x-should-retry: false`,
   BEFORE any acquisition (nothing queued, nothing acquired).
3. Lifecycle gate: READY passes; DOWN/ACQUIRING + `interactive` triggers the
   acquisition (fire-and-forget) and the request queues; any other priority
   while down → 503 `model_down` with `lifecycle_state`.
4. Scheduler submit; queue-full → 429 `queue_full` with `X-GX-Queue-Position`
   and `Retry-After: 10`.
5. Relay to `http://127.0.0.1:8888/v1/...` with `model` rewritten to the
   served id and `chat_template_kwargs` injected from the reasoning mapping.
   Non-streaming: the complete upstream JSON body is forwarded byte-for-byte.
   Streaming: SSE chunks pass through; an admin cancel aborts mid-stream.
   An engine context refusal carrying exact counts is corrected ONCE and
   retried immediately (never resent unchanged).
6. Response headers: `X-GX-Routed-To` (gx-max|gx-auto), `X-GX-Profile`,
   `X-GX-Reasoning`, `X-GX-Request-Id`, `X-GX-Context-Limit`,
   `X-GX-Output-Tokens`, `X-GX-Output-Clamped`, and — per the gateway budget
   hook contract (`gx_budget_hook.py` reads
   `response_obj._hidden_params["additional_headers"]["x-gx-queue-wait-ms"]`) —
   `x-gx-queue-wait-ms`, stamped ONLY when the request was queued (absent ==
   direct path, never queued).

### `gx status` (`status_cli.py`)
Registry facts (model id + uncensored flag), orchestrator `/text/status`
(cluster state, profile, queue, node one-liners), node-1 local facts (kernel
pin check against 6.17.0-1032-nvidia, RAM/swap, container scan). `--json`
emits the report dict; `--orchestrator-base` overrides the URL.

## Findings / follow-ups for other workers

1. **systemd**: `gx-orchestrator.service` (user unit in
   `~/.config/systemd/user/`, NOT owned by Worker A) is currently **enabled
   and active** (running since 2026-09-27 15:51). The work item asked for
   "not enabled, comment only" — it is enabled; the deployment worker should
   confirm this is intended and that the unit's ExecStart points at the
   rebuilt checkout.
2. The gateway (`gx_budget_hook.py`) already forwards all `X-GX-*` attribution
   headers and reads `x-gx-queue-wait-ms`; no gateway change needed.
3. `restore-normal.sh` still restores the llama-swap containers
   (`gx-llama-swap-node01/02`); with the old models retired the gateway
   worker may want to revisit what "normal" is.
4. Real-registry smoke: `legenex/models/registry.json` (schema 2, owned by
   Worker B) loads cleanly through `gx_orchestrator.profiles` (profiles:
   balanced/custom/deep/fast/long/swarm; served id
   DeepSeek-v4.1-Flash-EXL3; fabric IPs in nodes; serving_notes present).

## Key fixes made during this session (since the interruption)

- `scheduler.py`: init-time history file load; `wait()` terminal-state
  resolution through the history ring; unique tmp file per persist (two
  concurrent persisters no longer delete each other's temp file); comment
  syntax error.
- `lifecycle.py`: injectable `settle_seconds` / `mem_return_wait_s`; process-
  group kill on script timeout (`start_new_session` + `killpg`) so a wedged
  kit child cannot stall the acquire worker; stdout fd closed after the
  pump ends (ResourceWarnings gone).
- `server.py`: unhandled `RegistryError` on a gx-max `X-GX-Profile` override
  crashed the connection instead of a clean 400 — now caught and returned as
  `invalid_profile`.
- Test-harness race: the trailing journal/metrics record is written after
  the response is flushed; the harness polls for it (`wait_for_journal`).
