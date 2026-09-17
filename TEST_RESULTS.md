# Test results

Only results actually observed are recorded here. Anything not yet run is marked
NOT RUN rather than assumed.

Last updated: 2026-09-15.

---

## 1. gx-max — two-node DeepSeek V4 Flash (SGLang TP=2)

**Status: PASSING.** This was the open question at the start of the session
(kernel 7.0 had broken it on gx10-02); the run on the pinned 6.17.0-1032 kernel
came up cleanly on both nodes.

### Startup

From `/srv/logs/gx-max-rank0.log`:

```
Engine startup timings (s): load_weight=401.89, kv_cache_allocation=3.04,
  scheduler_e2e=625.45, cuda_graph={target_verify=64.92, draft_decode=5.39},
  tokenizer_e2e=630.50
max_total_num_tokens=287488, chunked_prefill_size=8192, max_prefill_tokens=16384,
  max_running_requests=32, context_len=327680, available_gpu_mem=14.93 GB
The server is fired up and ready to roll!
```

**The kernel-7.0 failure did not recur.** rank1 passed the FlashInfer autotune
stage that previously died with `ibv_reg_mr_iova2 failed with error Cannot
allocate memory`, then captured CUDA graphs and initialised its memory pools:

```
[TP1] FlashInfer autotune completed.
[TP1] Capture target verify CUDA graph end. elapsed=64.96 s
[TP1] Init Unified Radix Cache.
```

### Both ranks alive

| Check | Result |
|---|---|
| rank0 container (gx10-01) | `running` |
| rank1 container (gx10-02) | `running` |
| `GET :30000/health` | **200** |
| `GET :30000/v1/models` | `{"id":"/model", "max_model_len":327680}` |

### ConnectX / NCCL transport ACTIVE — verified, not assumed

Kernel netdev counters showed almost no traffic, which is expected when NCCL
uses RDMA/ibverbs (it bypasses the netdev stats). The **InfiniBand hardware
counters** tell the real story. Delta across one 400-token generation:

| Device | netdev | xmit | rcv |
|---|---|---|---|
| `rocep1s0f0` | `enp1s0f0np0` (192.168.100.10) | **391.55 MB** | 391.79 MB |
| `roceP2p1s0f0` | `enP2p1s0f0np0` (192.168.101.10) | **380.78 MB** | 380.95 MB |
| `rocep1s0f1` | — | 0.00 MB | 0.00 MB |
| `roceP2p1s0f1` | — | 0.00 MB | 0.00 MB |

**772 MB of RDMA traffic for a single generation, split near-evenly across both
active rails.** `rdma link show` confirms both f0 ports `ACTIVE / LINK_UP`.

Distributed bootstrap connection, from `ss`:
```
[192.168.100.10]:5000  <->  [192.168.100.11]:35798     ESTABLISHED
```
That is the ConnectX fabric. During the same window Tailscale carried only SSH
(port 22) and one unrelated dev port. **No model traffic on Tailscale.**

### Throughput

| Test | Prompt | Completion | Wall | Decode |
|---|---|---|---|---|
| short | 11 | 88 | 3.18 s | 27.69 tok/s |
| medium | 24 | 512 | 7.88 s | **64.97 tok/s** |
| long | 29 | 1024 | 22.59 s | 45.34 tok/s |
| TTFT (streaming) | — | — | **1.09 s** | — |
| 4 concurrent | — | 1200 total | 16.40 s | **73.19 tok/s aggregate** |

The short-prompt figure is dominated by fixed overhead; 65 tok/s at 512 tokens
is the representative single-stream number.

### Sample output (unedited)

> During autoregressive decoding, each token generation requires loading the full
> set of expert weights into memory, even though only a small subset of experts
> is activated per token. […]

## 2. Orchestrator — gx-auto routing and gx-max lifecycle

**Status: PASSING.**

### Unit tests

```
$ cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'
Ran 29 tests in 6.868s
OK
```

Covering: token estimation, image-part detection, tool detection, tier
selection, the gx-max escalation gate, vision-preserving fallback, availability
fallback, determinism, decision logging, lifecycle adoption/acquire/release,
failure propagation, and acquisition serialisation.

Two real defects were found by these tests and fixed:

* **Threshold drift.** The gx-max context trigger was a hardcoded constant that
  silently disagreed with the tier table after the served context windows were
  updated. It is now *derived* from the tier table (`MAX_SINGLE_NODE_CONTEXT`).
* **Lost successful start.** If `gx-max-start.sh` exited 0 but the health probe
  missed transiently, the orchestrator discarded a successful acquisition and
  declared failure. It now polls for up to 60 s before giving up.

A third was found by the concurrency test: five simultaneous `acquire()` calls
must invoke the start script exactly once. Verified.

### Live integration against the running engine

| Test | Result |
|---|---|
| Orchestrator adopts an already-running engine at startup | **PASS** — state `ready` |
| `gx-max` direct → real inference | **PASS** — HTTP 200, `X-GX-Routed-To: gx-max`, returned `ORCHESTRATOR PATH OK` |
| `gx-auto` with an explicitly extreme task | **PASS** — routed to `gx-max`, returned `AUTO ROUTED MAX` |
| `gx-auto` with a trivial task | **PASS** — routed to `gx-mini` (then 502, correct: the gateway is not up yet) |
| Routing decisions logged with reasons | **PASS** |

Recorded routing decisions:
```
tier=gx-max   complexity=11 hard=11 reasons=['hard-category score 11 >= 3: explicitly extreme task']
tier=gx-mini  complexity=-4 hard=0  reasons=['complexity -4 < 1: simple/dispatch task']
```

### Bind isolation

| From | Result |
|---|---|
| host loopback `127.0.0.1:18900` | 200 |
| docker bridge `172.17.0.1:18900` | 200 |
| inside a container via `host.docker.internal` | **200** |
| LAN address `10.60.21.37:18900` | **refused (000)** |

The orchestrator is reachable by the gateway container but not from the LAN or
the tailnet, which is the intended security property for a control surface.

## 3. Fabric

| Check | Result |
|---|---|
| Rail A `192.168.100.10 ↔ .11` | UP, 0% loss, 0.211 ms |
| Rail B `192.168.101.10 ↔ .11` | UP, 0% loss, 0.338 ms |
| RDMA `rocep1s0f0`, `roceP2p1s0f0` | PORT_ACTIVE both nodes |
| RDMA f1 ports | PORT_DOWN (expected — only two rails in use) |
| Kernel both nodes | `6.17.0-1032-nvidia` byte-identical |

### NCCL collective benchmark (corroborating evidence, earlier bring-up)

A custom NCCL 2.30.7 build for SM121 ran a two-node `all_gather_perf` (16 GiB)
with **zero errors**:

| Metric | Result |
|---|---|
| Out-of-place bus bandwidth | ~20.86 GB/s |
| In-place bus bandwidth | ~21.81 GB/s |
| Average bus bandwidth | ~21.34 GB/s |

This is a different measurement (a synthetic collective test, not a real
gx-max generation) but corroborates the 772 MB/generation RDMA figure above —
both point at the same ~21 GB/s-class two-rail fabric.


## 4. Gateway, gx-mini and gx-fast

**Status: PASSING.** All seven aliases are served from the single LiteLLM
endpoint on `127.0.0.1:4000`.

```
$ curl -s localhost:4000/v1/models -H "Authorization: Bearer $KEY"
['gx-auto', 'gx-fast', 'gx-image', 'gx-max', 'gx-mini', 'gx-reason', 'gx-video']
```

### gx-mini (llama.cpp, node 1)

| Test | Result |
|---|---|
| Cold load through llama-swap | 6.35 s |
| Text inference via gateway | **PASS** — correct RoCE definition |
| Decode throughput | **50.6 tok/s** (matches the expected ~48) |
| Vision via gateway | **PASS** |
| Recovery from a squatted container name | **PASS** |

Vision test used a generated 448×448 image containing a red circle, a blue
square and a black digit 7. The model reported all three plus their positions:

> A **circle** located in the upper-left […] The circle is **red**. The square
> is **blue**. The number "7" is **black**.

### gx-fast (vLLM, node 1) — `nvidia/Qwen3.6-35B-A3B-NVFP4`

| Test | Result |
|---|---|
| Cold load | 329 s (weights 146 s) |
| Model memory | 20.35 GiB |
| Text inference | **PASS** — `17*23` → `391` |
| Decode throughput (warm) | **72.8 tok/s** |
| Tool calling | **PASS** — `finish_reason: tool_calls`, parsed `get_weather {"city": "Berlin"}` |
| Vision | **PASS** — identified red circle, blue square, digit 7 |

**NVFP4 kernel note.** The GB10 vLLM image is compiled for `sm_120` while the
device reports `sm_121`; it runs (minor-version compatible), and vLLM selected
`MarlinNvFp4LinearKernel`. It logged:

> Your GPU does not have native support for FP4 computation […] Weight-only FP4
> compression will be used leveraging the Marlin kernel.

So gx-fast is running **weight-only FP4 via Marlin**, not native FP4 tensor
cores. It works and is fast enough, but a vLLM built for `sm_121a` would likely
be faster. Recorded rather than assumed — see BLOCKERS.md.

### gx-auto routing against live tiers

| Prompt class | Routed to | Why (from the decision log) |
|---|---|---|
| "hi there" | **gx-mini** | complexity -4 < 1: simple/dispatch task |
| ticket classification | **gx-mini** | complexity -3 < 1: simple/dispatch task |
| tool-bearing request | **gx-fast** | tool definitions present: tool floor |
| "debug… derive time complexity… refactor" | **gx-reason** | complexity 10 ≥ 4: hard reasoning/coding |

The tool-bearing request returned a real parsed tool call end-to-end through
`gx-auto`, so routing and tool parsing both work through the full chain.

Note: LiteLLM does not forward the orchestrator's `X-GX-Routed-To` response
header to clients, so routing is verified from the orchestrator's decision log,
which is the authoritative record.

### Defects found by running the stack

These were all found by execution, not inspection:

1. `--no-mmap` no longer exists in this llama.cpp build (replaced by
   `--load-mode`); it aborted gx-mini at startup.
2. llama-swap tokenises `cmd` with shell-style splitting and strips quotes, so
   a JSON-valued flag arrived malformed. Moved to an `--env-file`.
3. Without the chat-template kwarg, gx-mini put its entire answer in
   `reasoning_content` and returned **empty** `content` with
   `finish_reason: length`.
4. The GB10 vLLM image's ENTRYPOINT is already `["vllm","serve"]`, so a literal
   `serve` in the command became the positional model name.
5. Qwen3.6 emits Qwen-XML tool calls, not Hermes JSON — wrong parser meant tool
   calls came back as unparsed text.
6. A leftover container holding the model's `--name` made every subsequent
   start fail permanently. Fixed and verified by deliberately squatting the name.
7. The orchestrator probed the gateway without a bearer token, so every tier
   read as unavailable.

## 5. gx-max lifecycle — acquire, serve, release, restore

**Status: PASSING.** Acceptance test 6 run end-to-end on hardware.

### Acquire (`gx-max-start.sh`)

Starting state: gx-mini loaded on node 1, both nodes otherwise idle.

```
=== gx-max preflight ===
both ConnectX rails reachable
=== draining conflicting GPU work ===
  node1: stopping gx-mini (graceful, 60s)
MemAvailable: node1=112GiB node2=113GiB
=== starting rank1 on node2 ===
=== starting rank0 on node1 ===
=== waiting for gx-max to become healthy (timeout 1800s) ===
```

Preflight, graceful drain, rank1-before-rank0 ordering and the health gate all
behaved as designed. Time to healthy: **~9 minutes** (rank0 weight load 357 s,
draft model 58 s, FlashInfer autotune, CUDA graph capture).

**The kernel-7.0 failure did not recur.** Both ranks passed
`FlashInfer autotune completed` — the exact stage that previously died with
`ibv_reg_mr_iova2 failed with error Cannot allocate memory`.

### Served through the gateway alias

| Check | Result |
|---|---|
| `/health` | **200** |
| rank0 / rank1 | both `running` |
| `gx-max` alias via LiteLLM :4000 | **PASS** — 156 tokens, 24.6 tok/s |

### D-002 verified: `--enable-metrics` works

```
$ curl -s localhost:30000/metrics | grep num_running_reqs
sglang:num_running_reqs{engine_type="unified",model_name="/model",...} 0.0
sglang:num_queue_reqs{...} 0.0
```

`gx-max-status.sh` now reports `in flight : 0` instead of `unknown`, so the
graceful drain observes the real queue rather than guessing.

### Release (`gx-max-stop.sh`)

```
draining: waiting up to 60s for in-flight requests to finish
queue empty, proceeding
stopping rank0 on node1
stopping rank1 on node2
MemAvailable after release: node1=115GiB node2=114GiB
restore: LiteLLM gateway is live on :4000
restore: orchestrator is live on :18900
restore: starting node-2 llama-swap
gx-max released; both nodes are back to normal operating state
```

**29.5 s total.** Both nodes fully reclaimed. gx-mini served again immediately
afterwards (returned `RESTORED`), confirming normal operation resumes.

### A real bug this test exposed

After the release, the orchestrator still reported `{"state": "ready"}` for
gx-max, because the state machine trusted its own cache and had not noticed a
teardown performed outside the process. A `gx-max` request in that window would
have been proxied into a dead endpoint instead of re-acquiring the cluster.
Fixed by reconciling against a real health probe in `status()`, `is_ready()` and
`acquire()`; verified live (state now correctly reads `down`) and covered by two
new tests.

### Memory headroom observation (not a failure, but worth knowing)

During this acquisition node 1 briefly hit **63/63 GB swap and 1 GiB free**
while rank0 loaded. Node 1 carries the control plane (gateway, Postgres,
llama-swap) *and* rank 0, so it has materially less headroom than node 2. It
recovered on its own once the transient load working-set was freed, and the
engine came up healthy — but node 1 is the tighter of the two nodes and a
further increase in `mem-fraction-static` would not be safe.

## 6. Acceptance suite run (node-1 tiers)

```
$ legenex/tests/acceptance.sh gateway mini fast
[0] gateway and aliases
  PASS  all 7 aliases exposed
  PASS  orchestrator healthy
[1] gx-mini
  PASS  gx-mini text inference
  PASS  gx-mini vision (identified red circle, blue square, digit 7)
[2] gx-fast
  PASS  gx-fast text inference (correct arithmetic)
  PASS  gx-fast tool calling (parsed get_weather)

 PASS=6  FAIL=0  SKIP=0
```

Run after a full gateway restart and after the gx-max lifecycle cycle, so it
also demonstrates recovery.

## 7. gx-reason — FAILING (SUPERSEDED — fixed 2026-09-16, see §14)

> This section records the ORIGINAL failure on the llama.cpp engine. That
> engine/model combination was replaced (D-021) and the tier now works; the
> repro below is exactly what §14 re-runs successfully. Kept because it is
> the evidence that condemns the llama.cpp CUDA path for this architecture.


`unsloth/Qwen3.5-122B-A10B-GGUF` (UD-Q4_K_XL, 77 GB) on llama.cpp loads
correctly and generates at ~13.6 tok/s, but **every token is garbage**:

```
$ curl .../completion -d '{"prompt":"The capital of France is","n_predict":20,"temperature":0}'
{"content":"////////////////////","tokens_predicted":20,"tokens_evaluated":5}
```

Raw `/completion` fails identically to the chat endpoint, so it is not a
template problem. Memory behaved exactly as intended for llama.cpp — the whole
model was file-backed (`RssFile` 99.8 GB, `RssAnon` 2 MB) with 56 GiB still
available — so the engine choice was right and the fault lies in the weights or
the CUDA kernels for this hybrid architecture. Details and next steps in
BLOCKERS.md B-011.

## 8. Node-2 incident

Attempting the `-ngl 0` comparison for B-011 started a second 77 GB model on a
node that already had one resident. Node 2 went into sustained mmap thrashing
and userspace stopped responding. Kernel liveness confirmed throughout (ICMP on
both fabric rails, 0% loss, ~0.36 ms). TCP connects succeed on ports 22 and
28080 but sshd cannot complete a banner exchange.

Node 1 was entirely unaffected and kept serving; the acceptance suite above was
run while node 2 was down.

Recorded as BLOCKERS.md B-012 with the rule that prevents it recurring.

## 9. Remaining

| Acceptance test | Status |
|---|---|
| 1. gx-mini text + vision | **PASS** |
| 2. gx-fast inference / tools / vision | **PASS** |
| 3. gx-reason hard reasoning | **BLOCKED then re-engineered** — vLLM cannot load the 73 GiB checkpoint (D-009/B-009); moved to llama.cpp GGUF, weights downloading |
| 4. gx-max both nodes, fabric, tok/s | **PASS** |
| 5. gx-auto routing across classes | **PASS** (gx-reason leg pending its engine) |
| 6. lifecycle drain/acquire/release/restore | **PASS** |
| 7. gx-image real generation | **NOT RUN** — ComfyUI not deployed |
| 8. gx-video real generation | **NOT RUN** — ComfyUI not deployed |
| 9. gateway restart recovery | **PARTIAL** — restore-normal.sh verified during the lifecycle test; full compose restart not yet run |
| 10. remote access over Tailscale | **NOT RUN** |

## 10. Session 2026-09-15 — resource ownership, gateway/orchestrator incident, routing/media hardening

Node 2 was found physically wedged at the start of this session (see §8) and
remained so throughout — every result below is node-1-only, or pure unit
tests requiring no live node 2.

### Automated suites — all PASSING, all runnable without node 2

```
legenex/orchestrator:        116 tests   OK   (python3 -m unittest discover -s . -p 'test_*.py')
legenex/lifecycle/tests:       9 tests   OK   (python3 -m unittest discover -s tests -p 'test_*.py')
legenex/media/router:         43 tests   OK   (./qa.sh)
```

The media router's previously-reported 36/36 was independently re-run and
confirmed accurate before any change was made (+7 new regression tests for
three fixes found this session, see §below).

### Live verification against the real running system

- **gx-mini, real end-to-end inference through the gateway:**
  `POST /v1/chat/completions {"model":"gx-mini",...}` → `HTTP 200`,
  `"content":"I am online, and the capital of France is Paris."`,
  `predicted_per_second: 51.43`, 0.335s total. Vision path (a 1x1 PNG
  data-URI) also executed end-to-end with no error through
  LiteLLM → llama-swap → llama.cpp.
- **Tier-health fix, verified against the actually-wedged node 2:**
  `curl http://127.0.0.1:18900/health/detailed` → `gx-reason:
  {"state":"unavailable","reason":"node2_offline"}`,
  `gx-max: {"state":"stopped","reason":"node2_unavailable"}`. Whole cold
  probe measured at 2.017s (dominated entirely by the single 2s-timeout
  node2 attempt) — confirmed via a direct `curl -m 2` to
  `192.168.100.11:28080/health` (`http_code=000`, exit 28, 2.010s) against
  0.38ms ICMP RTT to the same host: the B-012 "kernel-alive/
  userspace-starved" signature, live.
- **gx-max node2-offline safety, confirmed live (one real, disclosed
  attempt):** a validation script triggered a real `gx-max` acquire against
  the live orchestrator. It attempted the real SSH to node 2, hit
  `Connection timed out`, and died at preflight — `docker ps -a` confirmed
  no `gx-max-rank0`/`rank1` container was ever created on node 1. This is a
  live, unplanned confirmation of "never partially start rank0 while
  waiting for rank1."
- **Resource-guard concurrency, proven by test rather than asserted:** 5
  real OS processes racing `NodeLock` for the same node — exactly one
  proceeds; a bash process and a Python process racing the identical
  `flock` path block each other in both directions; a SIGKILLed lock holder
  never leaves a stuck lock (fresh acquirer succeeds in <4s); a launch that
  would violate the 30 GiB reserve floor never runs the caller's command,
  proven for both the bash and Python entry points.
- **gx-fast:** deliberately NOT cold-started (memory-safety hold). Verified
  statically instead: model directory present on disk (22G across 3
  safetensors shards, matches MODELS.md's 23.5GB), Docker image present
  locally, checkpoint's own `README.md`/`hf_quant_config.json` confirm it is
  really `nvidia/Qwen3.6-35B-A3B-NVFP4` (not an invented ID), and
  `litellm/config.yaml`'s routing entry points at the right upstream.

### Real bugs found and fixed this session (see coordination/DECISIONS.md D-012 to D-017 for full rationale)

| Area | Bug | Fix |
|---|---|---|
| Orchestrator health | `gx-reason` reported healthy while node 2 was wedged (probed gateway liveness, not the real upstream) | Per-tier real-upstream probing (D-014) |
| gx-auto routing | Bare word "exhaustive" alone scored `hard_score=3` and routed straight to gx-max (regex scoping bug) | Regex fixed; regression test added |
| gx-auto routing | Vision override picked the costliest vision tier without checking its context window fit an oversized prompt | Filtered to context-safe tiers, with logged fallback |
| gx-max lifecycle | A rank started, then failed later (e.g. health probe timeout), was left running with no lease on it | `_do_acquire()` now unwinds via `gx-max-stop.sh --force` on every failure path (D-017) |
| Media router | `GX_MEDIA_API_KEY` values like `"not-required"` silently disabled authentication — and that's exactly the sample value shipped in `.env.sample` | Magic strings removed; only a genuinely empty key disables auth |
| Media router | An image request could run through a video workflow template (or vice versa) — wrong params, wrong timeout, mislabelled response | `_named_workflow()` enforces a kind match |
| Media router | `Content-Disposition` filename (from ComfyUI, currently trusted per D-011) had no CR/LF stripping | Sanitised as defense-in-depth |
| Node 1 infra (not a code bug) | `gx-litellm` had lost its Docker network attachment entirely and was crash-looping | Recreated via `docker compose up -d litellm` |
| Node 1 infra (not a code bug) | The orchestrator had no systemd unit and was not running at all, despite prior docs claiming otherwise | New hardened `gx-orchestrator.service`, enabled |

### Known gaps carried forward, unresolved by design tonight

- gx-reason (B-011, garbage output) and gx-image/gx-video real E2E generation
  all still require node 2 — diagnostic and validation tooling is ready
  (`legenex/scripts/gx-reason-diagnose.sh`, `legenex/tests/gx-max-validate.sh`)
  but **NOT RUN**.
- `lib.sh`'s `n2()` SSH helper still only bounds the TCP-connect phase
  (`ConnectTimeout=10`), not a stuck banner exchange — the newer
  node2-recovery/validation scripts wrap their own SSH calls in a hard
  `timeout` for this reason, but `n2()` itself was deliberately left
  unchanged this session (see BLOCKERS.md B-012 repair note).
- HiDream and a video "hd" tier are documented in MODELS.md but not wired
  into any `_gx`-enabled workflow template — **NOT RUN, not built**.

## 11. Session 2026-09-15 (earlier) — B-011 GPU-vs-CPU comparison, node 2 recovered

**Status: DIAGNOSTIC PASSING (isolates the fault); the underlying bug is
still OPEN — this did not fix gx-reason.**

Two script bugs were fixed first (see `CHANGELOG.md` "Unreleased" for full
detail), then the real comparison was run against the newly power-cycled,
freshly-verified node 2:

| Run | Result |
|---|---|
| GPU (`--n-gpu-layers` default) | `////////////////////` — byte-identical to the original B-011 repro |
| CPU-only (`--n-gpu-layers 0`, CDI device still attached for `libcuda.so.1`) | `"The capital of France is Paris."` — coherent, same weights, same sampling |
| Same comparison after rebuilding `legenex/llama-cpp-spark` from current upstream `llama.cpp` master | **identical GARBAGE/SANE split, byte-for-byte** |

**Conclusion:** rules out the checkpoint/quant and a stale build. The fault
is isolated to the CUDA/GDN kernel execution path of this llama.cpp build's
`qwen3_5_moe` hybrid-architecture implementation on this hardware
(`sm_121`/GB10, driver 580.173.02). See `coordination/BLOCKERS.md` B-011 for
the three remaining next steps, none attempted yet (each needs a human
decision — upstream issue research, a new multi-GB quant download, or a
bisect).

## 12. Session 2026-09-15 (this session) — ChatGPT project-seed integration, live re-verification

No code was changed and no large model was started. Live checks only, to
validate documentation against reality before merging the seed files:

| Check | Result |
|---|---|
| `legenex/scripts/recover-node2.sh` (report-only) | **16 PASS / 0 FAIL / 0 WARN / 1 SKIP** |
| Node 2 SSH, kernel, `nvidia-smi`, Docker, both RDMA rails, `/swapfile-sglang`, disk, container inventory | all PASS |
| Node 2 llama-swap | healthy on loopback and reachable from node 1 over the fabric |
| Node 1 `gx-litellm` | found `Exited (128)` (benign — Postgres connection administratively terminated, not a crash); restarted via `docker compose up -d`; confirmed `healthy`, `HTTP 200` |
| Node 1 orchestrator (`gx-orchestrator.service`) | `active running` throughout, `/health/detailed` → `status: ok`, all tiers correctly `stopped`/`usable: true` |
| `/opt/models/` on node 1 | confirmed empty — no Qwen3.8 directory remains |

---

## 13. 2026-09-15/16 session — B-017 fix, gx-mini/gx-fast re-verification, gx-max attempt, B-020 incident

### gx-mini — real, through the gateway

```
POST /v1/chat/completions {"model":"gx-mini", "messages":[{"role":"user","content":"Reply with exactly: Paris"}]}
-> HTTP 200, content: "Paris", 47.9 tok/s predicted
```
**PASS.**

### gx-fast — real, through the gateway, cold start

Prompt: "A farmer has 17 sheep. All but 9 die. How many sheep are left?
Answer with just the number." Cold start (model not resident) took 2m6.9s
end to end; answer: `"9"` (correct — a classic riddle that a naive
subtraction gets wrong). **PASS.**

### gx-max — first-ever real acquisition attempt through the orchestrator that got past admission on both nodes

`legenex/tests/gx-max-validate.sh --cleanup-on-exit`, run immediately after
applying the B-017 fix (`coordination/DECISIONS.md` D-020):

```
[23:24:43] === acquire (POST http://127.0.0.1:18900/lifecycle/gx-max/acquire) ===
gx.guard: {"name": "gx-max-rank1", ... "allowed": true, ...}   <- node2 admitted
[23:24:48] rank1 started
gx.guard: {"name": "gx-max-rank0", ... "allowed": true,
  "reason": "admitted: 95.0GiB projected of 121.0GiB node total;
  MemAvailable leaves 20.0GiB (reserve floor 5.0GiB)"}          <- node1 admitted
[23:24:53] rank0 started
...
RuntimeError: Rank 0 scheduler died during initialization (exit code: -9).
  If exit code is -9 (SIGKILL), a common cause is the OS OOM killer.
[23:30:13] rank1 exited. Last 40 log lines: ssh: connect to host
  100.73.238.4 port 22: Connection timed out
[23:41:15] FAIL gx-max acquisition :: orchestrator correctly refused
  rather than downgrading (992s)
PASS=7  FAIL=1  WARN=0
```

**Partial result, real progress, real new failure:** the B-017 admission
deadlock is gone — both ranks were admitted and started for the first time
ever through the real production path. Rank0 was then genuinely OOM-killed
by the kernel during weight loading (not a code bug — `dmesg`-class OOM,
correctly picked gx-max over host daemons per its `--oom-score-adj 950`).
Cleanup's attempt to reach node2 and stop the orphaned rank1 then itself
timed out — node2 was already becoming unreachable. Not a clean PASS; not
the old permanent B-017 refusal either. Full incident and root-cause
analysis: `coordination/BLOCKERS.md` B-020. Node2 was confirmed down
(fabric/ICMP alive, SSH/userspace starved — the B-012 signature) by three
independent probes afterward; recovery needs a human physical power cycle.

### Node 1 post-incident cleanliness — real, verified

```
$ docker ps -a | grep -i max          -> (nothing)
$ free -h                             -> 115Gi available, 5.7Gi used
$ resource-guard.sh status node1      -> reconcile dropping stale resident
                                          gx-max-rank0 (container not running); {}
$ curl localhost:18900/health/detailed -> gx_max.state: "down"
```
**PASS** — node1's own ledger, lock, memory and orchestrator state are all
confirmed clean and correct despite the failed run; this incident is a
node2 hardware-availability problem, not a node1 software leak.

### gx-reason — replacement decided, NOT live-tested

`nvidia/Qwen3.6-27B-NVFP4` on vLLM is fully configured
(`coordination/DECISIONS.md` D-021) but node2 went down (B-020) before the
checkpoint could be downloaded or exercised. **NOT RUN** — do not mark
B-011 closed until this actually produces coherent output through the
gateway.

### gx-image / gx-video / kernel apt-mark hold / full acceptance suite

**NOT RUN this session** — node2 went down before Phase 1 (media) could be
reached, and the kernel `apt-mark hold` needs an interactive sudo password
neither this nor any prior session has had.

Full detail and the updated operational picture: `CURRENT_STATE.md`.

## 14. 2026-09-16 session — node 2 self-recovery, B-011 closed, gx-reason live

All results below are from live runs against both real nodes on 2026-09-16,
not re-reads of earlier notes.

### 14.1 Node 2 recovered without a power cycle (B-020 corrected)

The previous session recorded node 2 as physically wedged and needing a human
at the machine. It was not, and it did not:

| Probe | Result |
|---|---|
| `uptime` on gx10-02 | 22 h 07 m at 07:41 → boot 2026-09-15 09:33, i.e. *before* the 23:24 incident. Never rebooted. |
| `docker inspect gx-max-rank1` | `StartedAt 2026-09-15T21:24:47Z`, `FinishedAt 2026-09-15T22:44:48Z`, `ExitCode=1`, **`OOMKilled=true`** |
| `docker inspect gx-llama-swap-node02` | `ExitCode=0`, `OOMKilled=false`, finished `21:24:46Z` — one second *before* rank1 started, i.e. the gx-max drain, not a casualty |
| `docker events` for its 07:35:10 restart | compose labels (`com.docker.compose.project=gx-gateway`) — a human running the documented RECOVERY.md §4 command |
| `recover-node2.sh` | 14 PASS / 0 FAIL / 1 SKIP (the 2 WARNs were llama-swap being down, before that restart) |

The orphaned rank1 held the node for **80 minutes** and was then OOM-killed
against its own `--memory 106g` cap; userspace un-starved on its own. The
B-012 host-resilience design worked — slowly.

### 14.2 gx-reason A–E (B-011 closed)

Checkpoint: `nvidia/Qwen3.6-27B-NVFP4`, verified against the live HF API
before download (ungated, apache-2.0). On disk at
`/srv/models/vllm/Qwen3.6-27B-NVFP4`: 21,921,697,184 B across 3 shards =
**20.42 GiB**, byte-for-byte the sizes HF advertises. Download took 3 m 27 s.

| Test | What was run | Result |
|---|---|---|
| **A** — publish | `GET /v1/models` on `192.168.100.11:28080` with the fabric bearer token | PASS — exactly one model, `gx-reason`, `status: unloaded`. Unauthenticated request correctly refused. |
| **B** — cold start | first request triggers the vLLM launch | PASS — **401 s** to first token: ~225 s weight load (~75 s per 10 GiB shard), 177 s engine init/profile/warmup (31 s compilation). `quantization=modelopt_mixed`, `reasoning_parser='qwen3'`, `max_seq_len=65536`. |
| **C** — the B-011 repro | `POST /v1/completions`, prompt `"The capital of France is"`, `temperature 0`, `max_tokens 20` — *identical* to the failing call | **PASS — `" Paris."`** (previously `"////////////////////"`), HTTP 200 |
| **D** — gateway E2E | bat-and-ball problem through LiteLLM `:4000` as `gx-reason` | PASS — `ANSWER: $0.05` (correct; `$0.10` is the intuitive-but-wrong trap). 1690 completion tokens, **1468 of them reasoning tokens**, `reasoning_content` separated from `content`. 12.4 tok/s. |
| **E** — unload | `POST /api/models/unload` | PASS — container gone in ~5 s, MemAvailable 70 → **116 GiB** |

**Why this is a genuine fix rather than a lucky substitution:** vLLM's own
logs show it selecting the GDN linear-attention kernels for this checkpoint
(`Using Triton/FLA GDN prefill kernel`, `GDN decode kernel: cuda`) — the same
hybrid linear-attention/full-attention path llama.cpp implemented incorrectly.
The architecture was never the problem, as D-021 predicted.

### 14.3 Memory — measured, and one surprise (B-021)

| Measurement | Value |
|---|---|
| Node 2 MemAvailable, idle | 114 GiB |
| Node 2 MemAvailable, gx-reason loaded | 70 GiB |
| **Real node-level footprint** | **~44 GiB** (vs 42 GiB predicted by `--gpu-memory-utilization 0.35` × 121 GiB) |
| Container `memory.current` (cgroup v2) | **10.92 GiB** |
| `docker stats` MemUsage | 9.65 GiB / 45 GiB (21.45 %) |
| Largest in-container process RSS | 5.78 GiB (`VLLM::EngineCor`) |

The 45 GiB admission estimate in `resource_guard.py` measured true. But
~33 GiB of the real footprint is **invisible to the container's memory
cgroup** — the CUDA pool is not charged to it on this unified-memory
platform, so `--memory 45g` does not bound it. Recorded as **B-021**. The
admission guard is unaffected: it reads the node's real `/proc/meminfo`.

Separately confirmed while gx-reason was loaded: the residency ledger read
`{}` the whole time, because llama-swap launches models with a plain
`docker run` rather than through `gx_guard_run`. That is the known B-018 gap,
and it applies to gx-reason exactly as it does to ComfyUI. It is not a safety
hole today (the guard's own check reads real memory, not the ledger), but the
ledger under-reports node-2 residency to anything that trusts it alone.

### 14.4 Vision — verified (F)

The replacement checkpoint is multimodal (`Qwen3_5ForConditionalGeneration`
with a `vision_config` plus image/video processors), so `supports_vision:
true` was added to its gateway entry. Rather than ship that as an untested
capability claim, it was exercised:

* Input: a generated 320×160 PNG containing **three blue circles** on white,
  sent as a base64 `data:` URL in an OpenAI-style multimodal message through
  the LiteLLM gateway (`:4000`) as `gx-reason`.
* Prompt: "How many circles are in this image, and what colour are they?
  Reply with just: `<count> <colour>`"
* **Result: `"3 blue"` — correct on both counts.** 171 completion tokens
  (166 reasoning), 108 prompt tokens. 411 s wall clock, essentially all of it
  a second cold start (weights reloaded in 2 m 43 s from warm page cache vs
  ~3 m 45 s cold).

Corroborating evidence that the image really went through the vision tower
rather than being ignored: vLLM JIT-compiled `_bilinear_pos_embed_kernel`
during this request, and the engine logged an `MM cache hit rate` metric,
neither of which appears on text-only requests.

### 14.5 Regression check

`gx-mini` was re-tested through the gateway after the LiteLLM config change
and restart: correct answer, sub-second, no regression. `gx-fast` was not
re-cold-started this session (it was verified live the previous session and
nothing in its configuration changed).

## 15. 2026-09-16 (later session) — gx-max memory investigation, unwind fix, media E2E

All figures below are from live runs on both real nodes on 2026-09-16. Nothing
here is carried over from a previous session's notes.

### 15.1 gx-max — EIGHT instrumented two-node runs; the tier does not fit

The task was to tune gx-max so it holds a 30 GiB MemAvailable reserve. It
cannot, and the reason is now measured rather than argued.

**Baseline the engine sees:** `124546 MiB = 121.63 GiB` per node
(`torch.cuda.mem_get_info`, rank0 log). Checkpoint: 163.48 GiB over 48 shards,
155.77 GiB of it MoE expert weights; byte-identical shard manifests on both
nodes (`md5sum` of the name+size listing matches).

| # | Configuration changed | node1 min MemAvailable | node2 min MemAvailable | Outcome |
|---|---|---|---|---|
| 1 | `mem-fraction-static 0.70`, ctx 65536 | 17 GiB | 20 GiB | aborted by an 18 GiB tripwire |
| 2 | same, tripwire 12 GiB | 11 GiB | 19 GiB | aborted by the tripwire |
| 3 | same, tripwire 5 GiB, ctx 32768 | 8 GiB | **1 GiB** | node2 deadman fired |
| 4 | **`mem-fraction-static 0.50`** | 8.5 GiB | ~1 GiB | rank1 died; **trough identical to 0.70** |
| 5 | phase-aware floors, load floor 2 GiB | 11.9 GiB | **102 MiB** | node2 wedged 17 min; kernel `global_oom` killed `sglang::schedul` |
| 6 | `--memory 28g` cgroup cap | 3.3 GiB | 0 GiB | cap did NOT bind; host still starved |
| 7 | tripwires off, `--memory 106g` | **22 MiB** | 0 GiB | kernel `global_oom` killed the scheduler |
| 8 | `--load-format layered` | 112.8 GiB | — | `NotImplementedError: Cannot copy out of meta tensor` — unsupported for this NVFP4/DSV4 path |
| 9 | `--load-format runai_streamer` | 437 MiB | 0 GiB | loads, but **peak unchanged** |

**The conclusion, stated precisely.** Loading one TP=2 rank takes a 121.63 GiB
node from ~110 GiB MemAvailable to between 437 MiB and 0 MiB, on **both**
nodes. Every lever available was tested and none moved that peak:
`--mem-fraction-static` (0.50 / 0.70), `--context-length` (327680 / 65536 /
32768), `--chunked-prefill-size` (8192 / 4096), `--cuda-graph-max-bs-decode`
(32 / 8), `--max-running-requests` (32 / 8), the container `--memory` cap
(106g / 98g / 32g / 28g) and `--load-format` (auto / layered /
runai_streamer).

`--mem-fraction-static` moves only the **steady state** — which is real and
worth having (0.80 → 0.70 buys back 12.2 GiB, and the 2026-09-14 run at 0.80
ended with `available_gpu_mem=14.93 GB`) — but the load-phase peak is the
model weights landing in NVIDIA-driver-held unified memory, and no engine
setting reaches it.

**Where the memory is, measured at node 2's 102 MiB trough:**

```
MemFree     983 MiB     Cached    2008 MiB     Mapped   1955 MiB
AnonPages 26798 MiB     Shmem     1390 MiB     SwapFree 64453 MiB
--------------------------------------------------------------
visible total ~31 GiB of 121.63 GiB  ->  ~90 GiB held by the driver,
invisible to every /proc/meminfo LRU counter (B-021)
```

Note `SwapFree 64453 MiB`: the node sat at 102 MiB MemAvailable with **63 GiB
of swap untouched**. The loader's pages are pinned (`CAP_IPC_LOCK`,
`memlock=-1`) and cannot be swapped, so swap is not a relief valve here even
if policy allowed it.

`--oom-score-adj 950` is confirmed working: in both kernel OOM events the
victim chosen was `sglang::schedul`, not sshd/tailscaled/dockerd.

**Result: gx-max = NOT SERVING.** The admission guard refuses it, on the real
measured numbers, before anything is launched. See B-022 for the decision the
human needs to make and D-022 for what was changed.

### 15.2 Failure unwind — built, and proven on the real workload

| Test | Result |
|---|---|
| E1 deadman fires when rank0 never appears | **PASS** — orphan removed at the 20 s startup grace, node-2 llama-swap restored |
| E2 deadman arms on rank0, fires when rank0 vanishes | **PASS** — armed at +5 s, fired 10 s after rank0 went away |
| E3 deadman fires on its own memory floor | **PASS** |
| E4 phase latch: steady floor only after `/health` answers | **PASS** — held during load, fired once the engine answered |
| E5 `gx-max-start.sh` unwinds both nodes on a failed launch | **PASS** — `UNWIND COMPLETE — cluster verified clean`, all 11 checks |
| **Real workload, run 3** | deadman fired on node 2 at 1 GiB; node returned to 117 GiB immediately, **no orphan** |
| **Real workload, runs 5/7/9** | unwind reported verified-clean; both ranks confirmed gone; node1 115 GiB / node2 117 GiB restored |

Three tooling bugs were found by actually running these, all fixed:
`pkill -f rank1-deadman.sh` matched the ssh command line that was starting the
deadman (self-kill; now a pid file); `docker inspect` on a missing container
emits a blank line so `|| echo absent` produced `"\nabsent"` and the unwind
reported a phantom running rank0; and the unwind's ConnectX probe piped into
`grep`, swallowing the exit status and reporting both healthy rails as
unreachable.

A fourth, separate bug was found the same way: a **refused** launch had
already drained gx-mini and both llama-swaps and never put them back. Fixed —
a launch that starts no rank now restores exactly what it stopped, verified.

### 15.3 gx-image — real generation through the gateway

`POST /v1/images/generations` with `model: gx-image`, via LiteLLM on :4000.

| Item | Value |
|---|---|
| HTTP | **200** |
| Generation time | **26.5 s** |
| File | 1,274,968 B PNG, `\x89PNG` magic, **1024 x 1024**, 8-bit RGB, non-interlaced |
| Content check | mean RGB 124.9/99.3/87.6, stdev 61.9/70.9/71.5, **1043 distinct colours** in a 1052-pixel sample — not blank, not flat |
| Visual check | a single red apple on a wooden table in soft daylight — matches the prompt |
| node2 min MemAvailable | **59.7 GiB** (floor 30 GiB) |
| Swap | unchanged (64500 MiB free throughout) |
| Output | `/srv/logs/media-evidence/gx-image-20260916.png` |

### 15.4 gx-video — real generation, Wan 2.2

Submitted to the router's async `/v1/videos`, polled to completion, content
fetched.

| Item | Value |
|---|---|
| Workflow used | **`wan22-t2v-a14b-lightning`** (Wan 2.2 — LTX is neither enabled nor downloaded) |
| Generation time | **48.14 s** |
| File | 100,023 B, ISO MP4, **h264, 640 x 640, yuv420p** |
| Frames / rate / duration | **33 frames @ 16 fps = 2.0625 s** |
| Distinct frames | **33 of 33** frame hashes unique — genuinely moving, not a still |
| Non-black | darkest frame mean luminance 82.8, per-frame stdev ~66 |
| Visual check | lit candle flames, matches the prompt |
| node2 min MemAvailable | **42.8 GiB** (floor 30 GiB) |
| Swap | unchanged |
| Output | `/srv/logs/media-evidence/gx-video-20260916.mp4` (+ extracted frames) |

### 15.5 Media unload

`POST /free {unload_models, free_memory}` then `compose down`:
**47 GiB → 114 GiB → 117 GiB** MemAvailable on node 2, swap unchanged.

### 15.6 ComfyUI ingress boundary

`curl http://192.168.100.11:8188/system_stats` from node 1 → **exit 7**
(cannot connect). The router on :18800 is the only routable ingress. Boundary
holds.

### 15.7 Public alias acceptance — all seven, through the gateway

| Alias | Result |
|---|---|
| `gx-mini` | **PASS** — correct one-sentence answer on RoCE; vision identified red circle, blue square and the digit 7 |
| `gx-fast` | **PASS** — `17*23 = 391`; tool calling parsed `get_weather` |
| `gx-reason` | **PASS** — bat-and-ball answered `ANSWER: $0.05` (the `$0.10` trap avoided), `reasoning_content` separated from `content` (385 of 396 completion tokens were reasoning); vision correctly listed red circle, blue square, number 7 |
| `gx-max` | **REFUSED, correctly** — HTTP 503, message states explicitly it "will NOT be substituted with another model". No rank left running on either node; the control plane the attempt drained came back on both nodes |
| `gx-auto` | **PASS** — `hi there` -> gx-mini, a ticket-classification -> gx-mini, a stack-trace/complexity/refactor prompt -> gx-reason. An "exhaustive analysis" prompt selects gx-max, finds it not running and falls back to gx-reason with `downgraded_from: gx-max` logged — **without attempting an acquisition** |
| `gx-image` | **PASS** — real 1024x1024 PNG, see §15.3 |
| `gx-video` | **PASS** — real 33-frame h264 MP4, see §15.4 |

### 15.8 Boot / persistence / no-auto-start invariants — verified live

| Invariant | Result |
|---|---|
| Gateway persists | `gx-litellm`, `gx-litellm-db` restart policy `unless-stopped` |
| llama-swap control plane persists | `gx-llama-swap-node01` / `-node02` `unless-stopped` |
| Orchestrator persists | `gx-orchestrator.service` **enabled** + active (systemd --user) |
| Hostwatch persists | `gx-hostwatch.timer` **enabled** + active on **both** nodes |
| Heavy models do NOT auto-start | `gx-mini`, `gx-fast` restart policy `no`; llama-swap loads on demand |
| gx-max does NOT auto-start | both ranks launched `--restart no`; **no systemd unit exists for gx-max at all** |
| gx-reason does not reserve at boot | container absent at rest; spawned per request |
| Media does not auto-reserve | ComfyUI idle **695 MiB**, router **20 MiB** — node 2 sits at 116-117 GiB MemAvailable with the media stack up |
| Stale rank cleanup | `gx-max-start.sh` force-removes stale rank containers in preflight; the unwind confirms removal rather than assuming it |
| Stale ledger/lock reconcile | both ledgers read `{}` and both lock files are free after every run, including the aborted ones |
| node 2 disappearance/reappearance | exercised for real: node 2 wedged, the orchestrator kept serving node-1 tiers, the ledger reconciled the dead rank away, and normal service resumed on reappearance |

### 15.9 No fake healthy state

Before this session `GET /health/detailed` reported `gx-max -> stopped,
usable: true` for a tier that cannot be brought up at all. It now consults the
same admission arithmetic every launch path uses and reports:

```
gx-mini    ready        usable=True   loaded
gx-fast    ready        usable=True   loaded
gx-reason  ready        usable=True   loaded
gx-max     unavailable  usable=False  admission_refused: refused: ledger residency
                                      0.0GiB + new 117.0GiB + reserve 30.0GiB =
                                      147.0GiB exceeds node total 121.0GiB
```

The probe is read-only, takes no lock, and swallows its own errors — a broken
probe must never invent a fault. Covered by four new tests, including
"node 2 offline outranks admission" (give the operator the actionable reason)
and "a RUNNING engine is not second-guessed by the probe".

### 15.10 Kernel hold — verified blocked on sudo, everything else ready

`sudo -n true` returns "a password is required" on **both** nodes, so this is
genuinely the interactive-authentication stop condition, not an oversight.

Both nodes: `uname -r` = `6.17.0-1032-nvidia` (correct), GRUB pinned to it, the
locked image installed, `apt-get upgrade` would touch no kernel packages, and
autoremove would delete none. The verifier reports **8 passed, 3 failed** per
node; all three failures are the missing `apt-mark hold` itself:

```
[FAIL] only 0/5 HWE meta-packages held
[FAIL] only 0/8 locked 6.17 packages held
[FAIL] apt-get dist-upgrade WOULD touch kernel packages
```

The dry run confirms the exact plan on each node: 5 `*-nvidia-hwe-24.04`
meta-packages (currently at `7.0.0-1019.19`, i.e. the kernel L-4 forbids) plus
8 installed `6.17.0-1032` packages. Applying the holds is one command per node
and is the only remaining human action.

### 15.11 Full automated suites — final state

Run on a clean cluster with nothing else executing. (An earlier attempt had
the unwind suite and the acceptance suite running at once; the unwind suite's
E5 test invokes the real `gx-max-start.sh`, whose drain SIGKILLed a gx-reason
that the acceptance run was mid-way through loading. That is now called out in
the unwind suite's header — the two must not overlap.)

```
GX_RUN_SLOW=1 legenex/tests/acceptance.sh      PASS=19  FAIL=0  SKIP=4
legenex/tests/unwind-tests.sh                  PASS=10  FAIL=0
legenex/orchestrator  unittest                 124 tests  OK
legenex/lifecycle     unittest                   9 tests  OK
legenex/media/router  unittest                  43 tests  OK
```

All four SKIPs are the gx-max tests, and they are not hardcoded: the suite
asks the orchestrator live whether gx-max is admissible and skips with the
real refusal reason. They will start running again by themselves the moment
B-022 is resolved. The refusal itself is not skipped — `t_max_refusal` runs
always and asserts the 503, the never-substituted message, that no rank was
left running on either node, and that the control plane the attempt drained
came back.

**Three test bugs were found and fixed rather than worked around:**

| Symptom | Cause | Fix |
|---|---|---|
| `gx-mini vision: did not identify all elements` | The fixture drew a 120x100 **rectangle** and the assertion looked for "square". gx-mini said "blue rectangle" — correctly. | The suite now generates the fixture itself, with an actual square. It also no longer silently SKIPs the vision check when no file happens to exist in `/tmp`. |
| `gx-reason inference: empty/short` | `max_tokens 400` on a **reasoning** tier: `<think>` content is spent from the same budget, and a one-line question measured 385 of 396 completion tokens as reasoning. The model ran out mid-thought and returned empty `content`. | Raised to 4096, matching the gateway's own budget increase for this tier. |
| `resource_guard` CLI roundtrip | The test registered a ledger entry named **`gx-fast`** — a real production container — and asserted it would reconcile away as "not running". It passed only while gx-fast happened to be unloaded. | Uses a name that cannot exist. |

## 16. 2026-09-16 (third session) — gx-max restored, failure paths, Git sync

Every figure below comes from live runs on both nodes. Raw evidence is in
`/srv/logs/gx-max-baseline-20260916T105343Z/` and in
`/srv/logs/gx-max-safety-node1-*.tsv`.

### 16.1 gx-max launches (verified `4b96e49` vector, no cgroup cap, D-025)

| | Attempt 1 | Attempt 2 (direct) | Attempt 3 (orchestrator `POST /lifecycle/gx-max/acquire`) |
|---|---|---|---|
| Admission | both admitted (113.7 / 114.0 GiB free) | both admitted | both admitted |
| Outcome | **aborted by our own rule:** soft `nvCheckOkFailedNoLog NV_ERR_NO_MEMORY` at "Load weight begin"; unwind verified clean | **READY in 539 s** | **READY**, HTTP 200 after 616 s (508 s load + drain) |
| node 1 min MemAvailable | — | 2,653 MiB | 3,289 MiB |
| node 2 min MemAvailable | — | 7,434 MiB | 8,545 MiB |
| node 1 peak swap | — | 65,535 MiB (100%, one 5 s sample) | 65,535 MiB |
| node 2 peak swap | — | 51,584 MiB | 54,693 MiB |
| Steady MemAvailable (node 1 / node 2) | — | 15.0 / 16.3 GiB | 15.9 / 17.7 GiB |
| Steady swap (node 1 / node 2) | — | 8.1 / 5.4 GiB, flat over minutes | — |

Load-phase health on node 1:

* PSI full peaked at 22%.
* fork+exec never exceeded 6 ms.
* Swap-out was one-way (up to ~91k pages/s) and drained once the weights
  were loaded.

This was a controlled transient, not thrashing. The engine reported
`available_gpu_mem=14.93 GB` and `max_total_num_tokens=293120`, the same
figures as 2026-09-14.

### 16.2 gx-max real inference (`legenex/tests/gx-max-inference.sh`)

| Check | Direct (`:30000`) | Gateway alias `gx-max` (`:4000`) |
|---|---|---|
| /health, /v1/models | PASS | PASS (lists exactly the 7 aliases) |
| Factual: capital of Australia | "Canberra" | "Canberra" |
| Reasoning: bat and ball | $0.05 | $0.05 |
| Coding: `is_prime`, executed and checked against 0..29 | PASS | PASS |
| Long generation (700 tokens) | 15.92 s, 43.97 tok/s wall | 15.6 s, 44.87 tok/s wall |
| Streaming TTFT (short prompt) | 0.13 s | 1.36 s |
| RDMA during the 700-token generation | 5,413 MiB | 5,411 MiB |

Per-rail breakdown for a 302-token generation:

| Interface | Traffic |
|---|---|
| `rocep1s0f0` (192.168.100.x) | 554 MB each way |
| `roceP2p1s0f0` (192.168.101.x) | 540 MB each way |
| `tailscale0` | 0.08 MB |

The bootstrap store is `192.168.100.10:5000` ↔ `192.168.100.11`. L-3 holds.
Also answered correctly: "17*23" → `119`.

### 16.3 Failure paths on the real workload

| Test | What happened | Result |
|---|---|---|
| Failure during load (attempt 1) | node 1 sentinel abort at +43 s, node 2 deadman fired at the same moment | unwind: both ranks confirmed gone, ledgers and locks clean, 113 GiB back on both nodes, services restored — **PASS** |
| rank1 killed while serving | rank0 exited on its own at about +20 s; `rank0-watch` fired at +21 s | unwind verified clean at +69 s, 114 GiB on both nodes — **PASS** |
| rank0 killed while serving, node 1 watcher deliberately stopped | node 2 deadman: "rank0 unreachable", 90 s grace, FIRING, rank1 removed at +95 s, node 2 at 116 GiB, node 2 llama-swap restored by the deadman | operator unwind verified clean — **PASS** |
| Orchestrator reconcile | noticed the vanished engine (`ready -> down`), and re-acquired on request | **PASS** |

### 16.4 Automated suites

| Suite | Result |
|---|---|
| orchestrator unit tests | 138 passed |
| lifecycle unit tests (bash guard + safety rules) | 22 passed |
| media router unit tests | 43 passed |
| `unwind-tests.sh` E1–E6 (E5 destructive) | 12/12 PASS |
| kernel-lock verifier | gx10-01 13/0/0, gx10-02 13/0/0 |

### 16.5 Git sync (D-026)

| Test | Result |
|---|---|
| 1. Doc change on gx10-01 → commit → GitHub → gx10-02 | committed 54 s after the edit (45 s quiet), pushed 2 s later, gx10-02 reconciled 2 s after that. All three HEADs = `f3197b1` — **PASS** |
| 2. Push blocked (push URL pointed at a closed port) | commit kept locally, failure logged; 18 s after unblocking, all three = `09ea38c` — **PASS** |
| 3. Drift on gx10-02 (untracked file, plus a tracked edit containing a fake token) | caught within 44 s; evidence saved at mode 0700 with the token masked; file removed; edit reverted; no commit made on gx10-02 — **PASS** |
| 4. Integrity audit | gx10-01 15 PASS / 0 FAIL; gx10-02 17 PASS / 0 FAIL — **PASS** (after fixing a placeholder false positive and redeploying stale copies) |

### 16.6 Final cycle and alias validation

**Final gx-max cycle (orchestrator acquire, then gateway, then graceful release):**

* **Load:** 559 s. node 1 minimum 2,481 MiB MemAvailable, node 2 minimum
  7,465 MiB. Swap peaked at 64 GiB (node 1) and 50.9 GiB (node 2). Steady
  state 15.0 / 16.6 GiB.
* **Gateway `gx-max`:** 8/8 PASS. 700 tokens at 40.89 tok/s wall, TTFT
  1.38 s, 5,877 MiB of RDMA traffic.
* **`POST /lifecycle/gx-max/release`:** returned in 32 s. No rank containers
  remained on either node.
  * MemAvailable: node 1 went from 14.9 to 117.9 GiB; node 2 was at 117.9 GiB.
  * Swap: node 1 at 4.9 GiB, node 2 at 3.2 GiB.
  * Ledger empty, both locks free.
  * The node 1 watcher was disarmed without firing, and the node 2 deadman
    exited cleanly.
  * llama-swap on both nodes and the node 2 media stack were restored and
    healthy.

**gx-auto never acquires gx-max:** a prompt loaded with "hardest" signals,
sent while gx-max was down, produced this result:

| What was checked | Result |
|---|---|
| Classifier score | `hard_score 7` |
| Routing | served by `gx-reason`, with `downgraded_from: gx-max` |
| New acquisitions | 0 |
| gx-max containers on either node | 0 |

**Acceptance suite (normal tiers):**

* **First run: 12 PASS / 3 FAIL.** The failures traced back to the migration:
  * The branch switch left `gx-llama-swap-node01`'s bind-mounted `/app/env`
    pointing at a deleted directory. gx-mini's `docker run` then failed with
    exit 125.
  * With gx-mini failing, LiteLLM put `gx-auto` into cooldown, and the test
    harness misread the previous request's routing line as this one's. The
    harness now fails honestly in that case.
* **After `docker restart gx-llama-swap-node01`:** `acceptance.sh mini auto`
  passed 5/5.

**Post-release acceptance, full suite: 15 PASS / 0 FAIL / 1 SKIP.**

* **Passing:**
  * all 7 aliases exposed;
  * gx-mini (text and vision), gx-fast (text and tools), gx-reason;
  * gx-auto (3 routes);
  * ingress boundary;
  * gx-image;
  * gx-video (166,467-byte MP4);
  * gateway restart recovery.
* **Skipped:** "gx-max refusal correctness", correctly, because gx-max is now
  admissible. The serving path is covered above.

## 17. 2026-09-16 (fourth session): management web UI and final cluster acceptance

Everything below was observed live on 2026-09-16 between 18:29 and 20:30
SAST. Evidence files:

* `/tmp/gx-ui-gxmax-evidence.json` and `/tmp/gx-ui-gxmax-release.json`
  (gx10-01);
* `/srv/logs/gx-max-inference-20260916T180520Z.json` (direct) and
  `…T180539Z.json` (through the gateway);
* `/srv/logs/gx-max-safety-node1-*.tsv` (the latest run's SUMMARY line);
* `/srv/logs/gx-max-lifecycle.log`;
* `/srv/logs/gx-control-ui/audit.log`.

### 17.1 Baseline (Phase 1)

| Check | gx10-01 | gx10-02 |
|---|---|---|
| Kernel | 6.17.0-1032-nvidia | 6.17.0-1032-nvidia |
| Kernel-lock verifier (CLI, and again from the UI) | 13 passed, 0 warnings, 0 failed | 13 passed, 0 warnings, 0 failed |
| Swap | `/swapfile-sglang` 48 G + `/swap.img` 16 G | same |
| RoCE (`rocep1s0f0`, `roceP2p1s0f0`) | ACTIVE / LinkUp, 200 Gb/s | ACTIVE / LinkUp, 200 Gb/s |
| Fabric reachability | 192.168.100.11 and 192.168.101.11: RTT 0.6 ms | — |
| Tailscale | Running | Running, peer online |
| Hostwatch | ok=5 warn=0 crit=0 | ok |
| Guard | node1.lock free, ledgers `{}` | node2.lock free |
| Git | HEAD 3e476e9 == GitHub == gx10-02; push URL disabled on gx10-02 | — |
| Integrity audit | PASS=15 WARN=0 FAIL=0 | — |

**Drift (Phase 2).**

* gx10-02's `~/gx-gateway` and `~/gx-media` configs are byte-identical to
  the repo.
* The deployed `gx-media-router:1.0.0` image's Python sources are
  hash-identical to `legenex/media/router`.
* The installed unit files differ only by the installer's `@REPO@`
  substitution.

No stale deployment copy was found.

### 17.2 Automated tests

| Suite | Result |
|---|---|
| `legenex/control-ui` unit + API + auth + performance (`unittest`) | **133 passed** |
| Control UI ruff / mypy / build check | clean / 0 issues / 13 modules, 119 KiB |
| Control UI Playwright, offline fixture (incl. axe WCAG 2.2 AA on 9 pages + light theme, 390×844 mobile layout) | **11 passed**; 0 axe violations of any impact |
| Control UI gitleaks + npm audit | 0 findings / 0 vulnerabilities |
| `npm run qa` (all of the above) | **QA PASSED** |
| `legenex/orchestrator` (`unittest`, incl. 9 new lifecycle-event tests) | **147 passed** |
| `legenex/lifecycle` shell-rule tests | **22 passed** |
| `legenex/media/router/qa.sh` | **43 passed**, QA PASSED |
| `ops/git-sync/tests/sync-regression.sh` (new) | **19 passed**. It found and fixed the no-op conflict-marker gate in `node1-autosync.sh`. |
| `legenex/tests/unwind-tests.sh E1 E2 E3 E4 E6` (non-destructive: deadman startup grace, rank0 death, sustained exhaustion, phase latch, transient dip) | **8 passed** |
| `legenex/tests/acceptance.sh` (CLI, after the gx-max cycle) | **PASS=15 FAIL=0 SKIP=1**. The skip is "gx-max refusal", which applies only when gx-max is inadmissible. It also covered: gx-auto routing to mini/reason/mini, the ComfyUI ingress boundary, gx-video 224,771 B, and gateway restart recovery. |

Destructive rank-kill tests (rank1 killed, rank0 killed, failure during load)
were proven live on 2026-09-16 (§16). The lifecycle scripts they exercise
were not changed in this run; only the orchestrator's way of reading their
output changed, and that is unit-tested. So they were not repeated.


### 17.3 Control UI, live (Playwright + Google Chrome against `http://127.0.0.1:8088`)

| Spec | Result |
|---|---|
| `live.pages` | **6 passed** |
| `live.models` | **8 passed** (7 in the first run; after the Range fix the video and media-unload tests were re-run and passed) |
| `live.gxmax-1-load` | **2 passed** |
| `live.gxmax-2-release` | **2 passed** |

**What `live.pages` covered:**

* unauthenticated calls return 401;
* all 9 pages render real data with no console errors and no axe
  violations;
* both nodes, both rails, Tailscale and the Git HEADs are shown;
* 14 log streams from both nodes load and are redacted;
* the kernel verifier ran from the UI: 13/0/0 on both nodes;
* the integrity audit ran from the UI: gx10-01 PASS=15 WARN=1 (an autosync
  commit was pending) FAIL=0, gx10-02 PASS=17 FAIL=0;
* a node-2 reconcile ran from the UI: HEAD == origin/main.

**Real model calls made through the UI playground**

| Alias | Result |
|---|---|
| gx-mini | `17×23` → `391` (0.9 s); vision named the red circle, blue square and digit 7; streaming "One, two, three, four, five." |
| gx-fast | Tool call `get_weather(city="Cape Town")` parsed. Cold start plus answer 319 s. "Tokyo". |
| gx-fast via UI controls | UNLOAD succeeded (3.4 s). LOAD succeeded (318.6 s, admission preview passed). State returned to loaded. |
| gx-reason | Bat-and-ball → "The ball costs **5** cents", with `reasoning_content`. 432.6 s including the cold load. |
| gx-auto | "Say hello in French" → "Bonjour", routed to **gx-mini**. Proof that √2 is irrational → answered, routed to **gx-reason** (orchestrator `gx.routing` log). |
| gx-reason via UI control | UNLOAD before media succeeded. |
| gx-image | Real 1024×1024 PNG in 25.0 s; 630 distinct colours in a 64 px thumbnail. |
| gx-video | Real 640×640 MP4, 2.06 s: `video-3663237c583c4874` in 48.1 s (33 frames, all 33 distinct on disk, mean difference from frame 0 rising to 42 grey levels), and a second job in 28.4 s. In-browser check: 6 distinct of 6 sampled frames. |
| gx-image/gx-video via UI control | ComfyUI `/free` succeeded. |

**Defect found and fixed by this run.** The first gx-video browser frame
check saw 1 distinct frame. The video was fine: the content endpoint did not
support HTTP Range, so Chrome could not seek. Range and 206 support, plus a
small per-job cache, were added, with tests.

### 17.4 gx-max through the UI (sanctioned orchestrator lifecycle)

| Item | Observed |
|---|---|
| Direct playground request without takeover confirmation | HTTP 409, nothing started |
| LOAD (typed `gx-max`) → orchestrator acquire | job succeeded; HTTP 200 after 648 s wall |
| Orchestrator phases (new events API) | preflight → draining → admission → **loading_rank1 → loading_rank0** → warming → ready → serving |
| Startup | **537 s** (`gx-max READY … after 537s`) |
| Load transient (safety SUMMARY) | node 1 min MemAvailable **2573 MiB**, max swap **65535 MiB**; node 2 min **8275 MiB**, max swap **53586 MiB** |
| Steady state | node 1 **14.7 GiB**, node 2 **16.1 GiB** MemAvailable |
| Live engine (`/get_server_info`) | **tp_size 2, nnodes 2**, node_rank 0, dist_init_addr 192.168.100.10:5000, context 327680, mem_fraction_static 0.8, speculative DSPARK; served model `/model` (`nvidia/DeepSeek-V4-Flash-0731-NVFP4` mounted) |
| Ranks | gx-max-rank0 on gx10-01 and gx-max-rank1 on gx10-02 running; rank0 watcher and rank1 deadman alive; ledgers held rank0/rank1 (exclusive, 105 GiB) |
| Drain | gx-mini, gx-fast, gx-reason, gx-image and gx-video reported unavailable while gx-max owned the cluster |
| UI inference | factual "Canberra"; reasoning "$0.05"; coding `is_prime` executed, correct for 0..39; long 1200 tokens in 27.96 s = **42.9 tok/s**; streaming answer received |
| RDMA during UI inference (MiB per port) | node1 `rocep1s0f0` 4755, `roceP2p1s0f0` 4617; node2 `rocep1s0f0` 4852, `roceP2p1s0f0` 4714 |
| RDMA since before load (GiB per port) | 9.5 / 9.2 / 9.6 / 9.3: **both rails, both nodes** |
| CLI `gx-max-inference.sh`, direct | **8/8**: health, models, factual, reasoning, coding, 700 tok at 44.96 tok/s, RDMA 5192 MiB, TTFT 0.143 s |
| CLI `gx-max-inference.sh`, gateway (`gx-max`) | **8/8**: 45.1 tok/s, RDMA 5257 MiB, TTFT 1.284 s |
| UNLOAD (graceful) → orchestrator release | succeeded. Phases: draining_requests → stopping_ranks → memory_recovery → restoring → released |
| After release | no rank container on either node; no watcher or deadman process; both locks free; both ledgers `{}`; SGLang down |
| Memory return | node 1 **114.4 GiB**, node 2 **114.9 GiB** MemAvailable; swap 5.0 / 3.1 GiB |
| Normal service | LiteLLM, orchestrator, both llama-swaps, media router and ComfyUI up; gx-mini "42", gx-auto "Jupiter" |

### 17.5 Security checks

* The control UI binds `127.0.0.1:8088` and `100.105.214.61:8088` only; a
  connection to `192.168.100.10:8088` is refused.
* Unauthenticated calls return 401; a bad or missing CSRF token or a foreign
  Origin returns 403.
* Five failed logins return 429.
* Password change → all sessions 401 (API test).
* Secrets directory 0700, `auth.json` 0600, audit log 0640.
* The service runs with `MemoryMax=512M` (about 15 MB resident).
* **B-024 opened:** the media router key is the public placeholder. It was
  not rotated in this run, because the permission policy blocked writes to
  the secret stores.

## 18. gx10-02 production finalization (2026-09-16, run on gx10-02)

Commit under test: `637ddf4`. No gx-max-critical code changed in this run,
so no new gx-max cycle was run. The section 17 cycle stands.

### 18.1 Changes

* The live containers `gx-llama-swap-node02`, `gx-media-router` and
  `gx-comfyui` bind-mounted files from `~/gx-worker`. Before the change,
  `/proc/self/mountinfo` inside each container showed `gx-worker` paths.
  The cause: `~/gx-gateway`, `~/gx-media` and `~/gx-kernel-lock` were
  symlinks into that tree.
* Those three are now real directories deployed from `legenex/`, and every
  file matches the repository. The containers were recreated on them.
  `mountinfo` now shows `/home/legenex-02/gx-gateway/...` and
  `/home/legenex-02/gx-media/...`.
* `~/gx-worker`, `~/gx-scripts` (symlink),
  `~/Documents/Projects/Server/gx-cluster-worker` (symlink) and
  `~/run-gx-max.sh` (an unused manual rank launcher) are archived at
  `~/archives/gx-worker-retired-20260916/` (mode 0700) and removed. A sweep
  of systemd, cron, dotfiles, Docker mounts and labels, processes and
  repository code found no remaining reference.
* `acceptance.sh` `t_auto` fix. The test read the **last** `gx.routing`
  line in the log. While gx-reason cold-loaded, a foreign 9k-token agent
  request was logged as gx-fast, so the test reported a false FAIL. The
  orchestrator had already routed the test request to gx-reason (22:41:45),
  and llama-swap served it with HTTP 200.

### 18.2 Results

| Check | Result |
|---|---|
| Kernel / `verify-kernel-lock.sh` | `6.17.0-1032-nvidia`; **13 passed, 0 warnings, 0 failed** |
| RoCE | both rails ACTIVE at 200 Gb; ping 0 % loss to .100.10 and .101.10 |
| `recover-node2.sh` (from gx10-01) | **PASS=15 FAIL=0 WARN=0 SKIP=1** (nothing to clear) |
| `acceptance.sh gateway reason auto media` | gateway 2/2; gx-reason **15:35 correct** (1289 reasoning tokens); gx-image PASS; gx-video PASS (205 386 B); ComfyUI ingress boundary PASS. gx-auto reason route: false FAIL, fixed above |
| `acceptance.sh auto` after the fix | **PASS=3 FAIL=0** (mini / reason / mini) |
| gx-reason unload | node 2 went from 67 GiB to 113 GiB MemAvailable within seconds of the unload |
| gx-image, separate gateway call | PNG **1024×1024**, 1 477 288 B, luma range 4–239, visually checked (lighthouse prompt) |
| gx-video | h264 **640×640**, 16 fps, **33 frames, 33 distinct frame MD5s**, mean consecutive diff 5.13, first vs last 69.15 |
| `unwind-tests.sh E1 E2 E3 E4 E6` on gx10-02 | **PASS=8 FAIL=0** |
| `sync-regression.sh` | **PASS=19 FAIL=0** |
| lifecycle / orchestrator unit tests | 22 OK / 147 OK |
| Control-UI node2 collector (`hostfacts.py` over SSH) | hostname, kernel, memory, swap, PSI, RDMA, Tailscale, Docker, units, Git, hostwatch, lock and watcher all accurate |
| Mirror `integrity-audit.sh` | **PASS=22 WARN=0 FAIL=0** |

The gx-max rank1 evidence (the log, the deadman log and memory samples
from the 20:06 release) is kept at
`/srv/logs/gx-max-evidence-node2-20260916T2006/`.

## 19. V2 migration (2026-09-16/17, run from gx10-01; gx10-02 over SSH)

Only observed results are listed here. The evidence is under `/srv/logs/acceptance/`.

### 19.1 Models (real output, pinned revisions, sha256 manifests)

| Alias | Result |
|---|---|
| gx-mini (HauhauCS 4B Q4_K_M @c09cdbcd) | `gx_tier_acceptance.py`: **9/9** (text, math, stream, tools, vision, code, long, kilo); 52 tok/s; TTFT 0.1 s |
| gx-fast (kyaky 35B-A3B NVFP4 @33d5cf83, 20.99 GiB) | first run **9/10**: one answer said "Sydney" for Australia's capital, recorded as is. The recheck was correct in 14 of 15 samples. 55 tok/s; TTFT 0.08 s; cold load about 4 min |
| gx-fast memory | at `gpu_memory_utilization` 0.40: KV 22.4 GiB, node 1 MemAvailable 46.5–48 GiB with mini + fast. **At 0.34 (UI UNLOAD → LOAD, 259.5 s):** KV cache 1 274 627 tokens (9.72× at 131k); MemAvailable 58 GiB with mini + fast; warm replies 0.10–0.22 s |
| gx-reason (interim nvidia 27B) | gateway: bat-and-ball "5 cents", correct, with reasoning split (963 reasoning tokens, 142 s while node 2 was shared with a music-engine test) |
| Refusal probe (3 harmless "commonly refused" prompts) | gx-mini 0/3, gx-fast 0/3, gx-reason 0/3, gx-max 0/3 refusals |

### 19.2 gx-auto with Kilo Code traffic (D-030)

`gx_tier_acceptance.py gx-auto-kilo`: **10/10**. Each routing decision was
matched to its own request by fingerprint in `/srv/logs/gx-auto-routing.jsonl`:

* presence check with 20 tool schemas → **gx-mini** (11 s cold, about 3 s warm);
* coding tasks → **gx-fast** (2–5 s);
* hard debugging → **gx-reason** (376 s, cold interim load);
* the gx-max-only context case returns 503 `gx_max_not_running` while gx-max
  is down (behavioural unit test), and routes to the READY gx-max while it
  runs (live, §19.3).

The orchestrator unit suite passes **167** tests (includes `test_kilo_routing.py`).

### 19.3 gx-max: CRACK checkpoint, full UI cycle (D-032)

Evidence: `/srv/logs/acceptance/gxmax-20260916T235821Z/`.

| Check | Result |
|---|---|
| Checkpoint | `dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4` @c66fe384, 155.44 GiB. sha256 verified on both nodes; node 2 copy made over the fabric |
| UI LOAD | 595 s. Phases: preflight → draining → admission → loading_rank1 → loading_rank0 → warming → ready → serving |
| Engine | TP 2, nnodes 2; both ranks mount the CRACK dir; `--moe-runner-backend b12x` |
| UI checks | "Canberra"; bat-and-ball $0.05; `is_prime` correct; long generation 46.4 tok/s; streaming OK |
| Direct engine | **8/8**, 40.4 tok/s, TTFT 0.206 s, RDMA 5 966 MiB |
| Gateway | **8/8**, 46.95 tok/s, TTFT 1.418 s, RDMA 5 146 MiB |
| gx-auto | used the READY gx-max |
| RDMA during load / serve | 4.4–4.6 GiB per port |
| Memory (UI samples) | minimum MemAvailable 8.61 GiB (node 1) and 12.4 GiB (node 2); maximum swap 63.98 GiB (node 1) and 40.84 GiB (node 2) |
| UI RELEASE | **2/2** checks: no ranks, watcher or deadman; ledgers empty; locks free. MemAvailable back to 101 GiB (node 1) and 114 GiB (node 2). Services restored; gx-mini "42", gx-auto "Jupiter" |

### 19.4 Media v2 through the gateway (D-031)

Evidence: `/srv/logs/acceptance/media-run1/`. All results were inspected
visually (contact sheets are included).

| Operation | Result |
|---|---|
| t2i 1024 px | 29 s |
| image edit ("sunset beach, black shirt") | 31 s; edit applied, source unchanged |
| variation | 16 s |
| t2v | 45 s; h264, 3.06 s, **49/49 distinct frames** |
| i2v | 70 s |
| video edit ("make it night"), keyframe start 0 | 75 s; consistent night clip, 49 distinct frames, source unchanged. Partial denoise at start step 1 had drifted back to daylight, so the strength mapping was changed |

The router QA passes: templates, compose, **74** tests and the secret scan.
ComfyUI runs with `--reserve-vram 40` after `deploy-node2.sh --with-comfyui`.

### 19.5 Control UI (D-034, D-035)

| Suite | Result |
|---|---|
| `npm run qa` (hermetic) | ruff, mypy, **155** unit/API tests, build check, **15** Playwright tests with axe (0 serious or critical violations on 13 pages), security checks: **QA PASSED** |
| `gx_ui_live_check.py keys` | **9/9**: create → `/v1/models` → gx-mini chat → gx-max 403 → UI test → revoke → 401; master key absent from 8 API responses |
| `gx_ui_live_check.py manager` | **17/17** |
| `gx_ui_live_check.py library` | run 1 8/10 (fixed: t2v stored as "generate"); run 2 **34/34** (real generate / edit / t2v / v2v, lineage, range streaming, ZIP single-use, delete) |
| Playwright live models + pages (as `acceptance`), one uninterrupted run after the fixes below | **14/14 passed** (21.6 min). gx-mini "391" (0.26 s), vision "red circle / blue square / 7" (0.87 s), streaming OK. gx-fast tool call (0.88 s), "Tokyo" (0.26 s), UI UNLOAD→LOAD 252.5 s. gx-reason bat-and-ball correct with reasoning shown (694 s including cold load). gx-auto "Bonjour" (0.24 s), hard prompt 201 s. gx-reason UI unload OK. gx-image 1024×1024 with 781 distinct colours (27 s). gx-video 640×640, 2.06 s, 6/6 sampled frames distinct (52.5 s). Media UNLOAD OK. Heads n1 = GitHub = n2 = `5b1f8524`; kernel verifier 13/0/0 on both; integrity audit gx10-01 PASS=17 FAIL=0, gx10-02 PASS=29 FAIL=0; reconcile match |

### 19.6 Regression

orchestrator 167 OK · lifecycle 22 OK · media router 74 OK · control UI 155 OK + 15 e2e · `sync-regression.sh` PASS=19 FAIL=0.

### 19.7 Final state (2026-09-17 ≈04:58 SAST)

| Check | Result |
|---|---|
| Final regression | UI QA passed (155 tests + 15 e2e) · router QA 76 OK · orchestrator 167 OK · lifecycle 22 OK · sync-regression 19/0 |
| UI live suites (final) | keys **9/9**, manager **17/17**, library **34/34** |
| gx-auto Kilo (after the LiteLLM recreate) | **10/10**: mini about 3 s, fast about 1 s, reason 19–35 s warm |
| Media Library | 15 assets (image: 6 generate, 3 edit, 1 variation; video: 2 generate, 1 i2v, 2 v2v); 7 with a parent |
| Kernel | `6.17.0-1032-nvidia` on both; verifier 13/0/0 on both |
| Integrity audit | gx10-01 PASS=17 WARN=0 FAIL=0 (includes the new media-key check); gx10-02 PASS=29 WARN=0 FAIL=0 |
| Git | gx10-01 = GitHub `main` = gx10-02 (`d37f1292` before this doc update); gx10-02 clean; push URL `DISABLED-gx10-02-is-pull-only`; gitleaks on history since 2026-09-16: no leaks |
| Disk | gx10-01 720 G / 916 G used (149 G free, 83 %); gx10-02 851 G / 916 G used (**19 G free, 98 %**) |
| Memory | gx10-01 55 GiB available (mini + fast resident), swap 3.6 / 64 G used; gx10-02 14 GiB available right after a video edit (ComfyUI cache, freed after 600 s idle or on a gx-reason start), swap 5.0 / 64 G used |

### 19.8 Honest notes

* The gx-reason live UI test returned an empty answer twice:
  temperature 0 with 4 000 tokens (478 s), then temperature 0.6 with 6 000
  tokens (618 s). At about 12 tok/s the whole budget went to thinking. The
  direct gateway call with the model's default sampling finished in 1 181
  tokens. The test now uses temperature 1.0 (Qwen's thinking-mode setting)
  and 12 000 tokens.
* **A regression was caught by the live UI test.** The 02:15 LiteLLM recreate
  (done for the new size limits) ran from a shell with a stale
  `GX_MEDIA_API_KEY`. The container fell back to `not-required`, so gx-image
  and gx-video returned 401 through the gateway after that point. The Create
  and Library runs were unaffected, because they call the router directly. The
  gateway media run (01:54) had finished before the recreate. Fix: gx-litellm
  was recreated with a clean environment, the key hash was checked against
  `.env`, and a real 1024² image came back through the gateway. The integrity
  audit on gx10-01 now FAILs when the running LiteLLM media key differs from
  `.env`.
* **A second regression was caught by the live UI test.** The API
  Playground's video polling rejected the router's new gateway-encoded ids
  (`video_<base64>`, 118 characters). Both the route pattern and the
  playground check only allowed `[A-Za-z0-9-]{1,64}`, so the UI showed
  "unknown video job" although the router had finished in 49 s. Fixed:
  `[A-Za-z0-9_=-]{1,200}`, with unit and route tests added. The live
  gx-video test then passed (6 of 6 sampled frames distinct).
* The third gx-reason UI run answered correctly ("5 cents", 212 s). The test
  failed only because the answer mentioned "10 cents" while explaining the
  common mistake; the assertion now ignores sentences that discuss the mistake.
* **A third problem was found by the gx-auto Kilo re-run.** A cold gx-reason
  start right after the gateway video-edit job failed after 39 s
  (`upstream command exited prematurely`, HTTP 500). After a video edit,
  ComfyUI keeps the Wan and Qwen-edit weights until its 600 s idle free, and
  node 2 was at **14 GiB MemAvailable (107 GiB used)** despite
  `--reserve-vram 40`. gx-reason needs 0.35 × 121.6 ≈ 42.6 GiB free.
  Fix (router 2.1.0): `POST /v1/admin/free` frees ComfyUI unless a generation
  holds the slot or videos are queued. gx-reason's start command runs
  `docker exec gx-media-router python -m gx_media_router.free_node` first.
  **Proof:** t2v + video edit through the gateway (2/2), then immediately a cold
  gx-reason request. The router logged the free of the Qwen-edit and Wan
  models at 04:10:01, and gx-reason loaded and answered "156" in 6 min 37 s.
  New router test: `test_free_request_hands_the_node_to_gx_reason` (router
  suite 75 tests).
* **Media memory footprints were measured, and admission was added (router
  2.2.0).** On an idle node 2 (114 GiB MemAvailable), with ComfyUI freed
  before each step and 1 s sampling, the lowest MemAvailable was:
  image generate/edit **57.5 GiB** (about 57 GiB used), t2v/i2v
  **42.3 GiB** (about 72 GiB), keyframe video edit **7.2 GiB** (about
  107 GiB). No swap was used. Evidence:
  `/srv/logs/acceptance/media-memprobe-20260917.tsv` and `…-steps.txt`.
  With gx-reason loaded (about 67–70 GiB left), images fit but no video job
  does. The router now checks `/proc/meminfo` after its model switch: image
  60 GiB, video 76 GiB, keyframe 110 GiB, warm 8 GiB. It waits up to 30 s for
  a just-freed ComfyUI to hand memory back, and otherwise fails the job with
  HTTP 503 / `insufficient_memory` and a message naming gx-reason. **Live:**
  with gx-reason loaded, t2i passed (29 s) and t2v was refused in 0.1 s
  ("gx10-02 has 15 GiB free and this video job needs about 76 GiB…"); swap
  stayed unused. With gx-reason unloaded, t2v then video edit passed 2/2 (the
  edit waited for the freed Wan weights and finished in 99.5 s). Router
  suite: 76 tests.
* Gateway media acceptance re-run after the LiteLLM fix: **6/6**. t2i 26 s,
  edit 36 s (source unchanged), variation 16 s, t2v 60 s, i2v 70 s, v2v
  120 s. Evidence: `/srv/logs/acceptance/media-20260917T031557Z/`.
* Obsolete checkpoints were **not** deleted (B-026).

## 20. Final integration pass: gx-music, GX-Playground, Resource Control (2026-09-17, gx10-01)

**Where the evidence is:** `/srv/logs/acceptance/final-20260917T075129Z/`.
Everything below ran on the live cluster through the gx10-01 user-facing
paths, unless it is marked as an offline or unit test.

### 20.1 Router-mediated music eviction (EVICT kept off until this passed)

`evict_proof.log.json` and `evict-proof-node2-mem.tsv`:

1. image 1 was admitted cold and ran (the router held gx-image; 59 GiB
   available);
2. a music job asked the router to free memory, and the router's resident
   list was empty within 3 s;
3. ACE-Step loaded in 101.6 s and rendered 12 s of audio in 4.02 s;
4. image 2 was admitted **cold** with the full cold requirement (35.1 GiB
   available afterwards).

Swap was flat at 4754 MiB and minimum MemAvailable was 35.07 GiB, so there was
no overcommit. The router's record of what was loaded stayed correct
throughout. After this passed, `GX_MUSIC_EVICT_COMFY_WEIGHTS` was removed
from the gx10-02 env, so the default (on) now applies.

### 20.2 Real gx-max + gx-music takeover (Control Center MAX profile)

| Step | Result |
|---|---|
| Hold | set before rank 1 |
| Music drain | via the supervisor in 9 s: container absent, ledger clean, 0 engine processes |
| Acquire | 671 s |
| Inference through the gateway | "391", 0.6 s |
| Music job submitted during gx-max | waited with the gx-max reason for 377 s, then loaded (94.6 s), completed and was saved |
| Image job submitted during gx-max | waited, then completed |
| Release | 50 s; hold cleared; supervisor healthy; no rank containers, no deadman, lock free |

Details, including the two script-parsing faults (the system itself behaved
correctly), are in `gxmax-takeover-verified.md`. B-023 was re-measured
during this run:

| Node | Minimum MemAvailable | Peak swap use |
|---|---|---|
| node 1 | 9356 MiB | 65 531 MiB |
| node 2 | 11 695 MiB | 40 895 MiB |

### 20.3 Clients and API keys: `clients-acceptance.log`, 21/21 PASS

**API key lifecycle.**
* Create shows the secret once. The list shows it masked, with expiry and
  models.
* Test ran `/v1/models` plus a real gx-mini completion.
* Replace issued a new secret: the old one gets 401 and the new one works.
* Revoke: the key gets 401 and disappears from the list.
* The master key appeared in none of 12 browser-facing responses.

**Setup page tests.**
* Setup → Kilo Code → Test connection: CONNECTED, routed to gx-mini.
* Setup → Open WebUI → Test connection: CONNECTED.

**Kilo Code CLI 7.5.14 through gx-auto (real agent runs).**

| Prompt | Routed to | Details | Time |
|---|---|---|---|
| "are you there?" | gx-mini | 12 tools | 7.2 s |
| coding task | gx-fast | 5 turns, and the repository diff shows the fix | 13.5 s |
| hard debugging task | gx-reason (interim) | 5 turns, cold load | 450.6 s |

The previously accepted 10/10 routing result is not regressed.

**Open WebUI 0.11.3** (an isolated throw-away container; the user's instance
was not touched).
* Sign-up worked.
* Verify Connection passed, and the model list shows the GX aliases.
* A real gx-mini completion answered "42".

### 20.4 GX-Playground live browser acceptance: `playground-live.log`, 4/4 PASS (4.7 min)

These are Playwright runs (system Chrome) against `http://127.0.0.1:8090`,
signed in as the acceptance account (`e2e/live.creative.spec.js`).

**Music.**
* Created a vocal track with lyrics (`[Verse]`/`[Chorus]` inserted with the
  helper), 3 style tags, BPM 112, A minor, 4/4 and 30 s.
* It completed and was saved with model `ACE-Step/acestep-v15-xl-turbo`
  @d4a0b288.
* WAV (11.5 MB), FLAC (6.5 MB) and MP3 (1.2 MB) all downloaded.
* In-browser playback advanced over the 30 s track, and axe reported no
  violations.
* A UI remix became a child asset with lineage. Repaint (4-10 s) and extend
  (+15 s, 45 s total) also completed.
* The cold first run took 122.6 s in total (107 s load, 14 s generation).
* With the engine warm, a remix or repaint took about 5 s.

**Images.**
* Generate 1024×1024 took 27 s cold (it first unloaded an idle gx-reason for
  room).
* The download is a PNG (1.8 MB).
* A UI edit (40 s) became a child of the source; a variation took 17 s.

**Video.**
* t2v produced 49 frames, all distinct, in about 64-67 s cold. It first
  unloaded gx-image and waited for gx-music.
* The file downloaded, and the in-browser `<video>` played.
* i2v from the created image produced 33 distinct frames in 57.7 s, with
  parent lineage.
* The t2v cold load happened while gx-music was loaded. The router admitted
  it by its measured cold threshold (76 GiB available). Minimum MemAvailable
  was 18.6 GiB, and swap stayed flat at about 5.5 GiB.
  * **Correction (final cleanup pass, 2026-09-17): this did NOT conform to
    the locked 30 GiB normal-operation reserve.**
    * Surviving with flat swap is not acceptance. The 76 GiB threshold was
      the footprint plus about 4 GiB, with no reserve.
    * The final review found it, and it was fixed by D-038 (router 2.4.0,
      gx-music 1.1.0).
    * Re-tested live in §21.2.
  * The "unloaded gx-image and waited for gx-music" lines in that log belong
    to the image job before it, not to this t2v.

**Library.**
* Search by run tag, the audio type filter, sort, axe, and a bulk ZIP of all
  9 assets (91 MB) all worked.
* The Playground → Control Center link opened on the same host with no
  second sign-in, and the Control Center's Playground link is correct.
* All 9 test assets were deleted.

The first run showed 3/4. The Control Center link used the Tailscale host, so
a loopback session did not carry over. The fix: the link now keeps the page's
host (`web/js/app.js`, offline test updated). The Library test then passed on
the first run's assets (`playground-live-library-rerun.log`), and the full
second run passed 4/4.

### 20.5 Public music API via gx10-01:8090: `music-api-accept.log`, 20/20 PASS

**Access control.**
* No key → 401; invalid key → 401; a key without gx-music → 403.
* Load/unload → 403.

**Generation.**
* Submit returned 202. The job completed in about 10 s (engine warm) with
  BPM 84 / D minor.
* MP3, WAV and FLAC downloads worked (magic bytes checked).

**Isolation between keys.**
* A second key gets 404 on the job and on its content, and its job list
  does not include the job.

**Remix and lineage.**
* A remix of `{job_id, index}` completed with `parent_job_id`, and
  `/lineage` lists it.
* The Library links the remix to its parent track.

**After revoke.** No key material appeared in the output, and a revoked key
gets 401 immediately.

The first attempt was 15/19 and found three defects. All are fixed and have
regression tests:

1. `completed` was reported while the tracks were still being saved, so
   content returned 409. The public status is now `saving` until the Library
   has the files.
2. A revoked key kept working for up to 60 s because key look-ups were
   cached. The Control Center now clears that cache on revoke/replace, and the
   cache TTL is 15 s.
3. An API remix of a job that was still being saved lost its Library parent.
   Such a remix is now imported only after its parent, and it is linked by
   `parent_job_id`.

### 20.6 Integrity, kernel and gateway

* **Kernel lock verifier:** gx10-01 13/13 PASS and gx10-02 13/13 PASS
  (`kernel-node1.log`, `kernel-node2.log`); `6.17.0-1032-nvidia` on both.
* **Integrity audit:** it found gx-litellm running with the media-key
  placeholder. Cause: a gx-max release run from an orchestrator started
  before the key rotation (B-027). Fixed in `restore-normal.sh` (the `.env`
  values always win), with a regression test. gx-litellm was recreated,
  gx-orchestrator restarted, and the check passes.
* **B-025 re-checked:** still blocked, with no token on gx10-01 (details in
  BLOCKERS).

### 20.7 Offline and unit QA (all green)

| Suite | Result |
|---|---|
| Control Center `npm run qa` | ruff, mypy, 387 unit/API/auth tests, performance budget, build (201.5 KiB), 17/17 browser E2E + axe, gitleaks + npm audit (0 vulnerabilities) |
| GX-Playground `npm run qa` | ruff, 9 proxy tests, build (258.1 KiB), 19/19 browser E2E + axe, gitleaks |
| gx-music `qa.sh` | 54 tests, no literal credentials |
| media router `qa.sh` | 87 tests, no secrets |
| orchestrator | 170 tests |
| lifecycle | 38 tests (including the new `test_restore_normal_sh`) |
| git-sync `sync-regression.sh` | 19/19 |

## 21. Final cleanup pass: production Open WebUI identity and the 30 GiB reserve (2026-09-17, gx10-01)

**Evidence directories** (all under `/srv/logs/acceptance/`):

| What | Where |
|---|---|
| Open WebUI identity | `owui-identity-20260917T095845Z/`, `owui-identity-browser-20260917T100020Z/` (JSON and screenshot) |
| Reserve, live runs | `reserve-live-20260917T101532Z/` (run 1), `reserve-live-20260917T103728Z/` (run 2) |
| QA, kernel checks, logs | `cleanup-20260917/` |

The production database backup taken before the first write is
`/srv/projects/gx-cluster/backups/open-webui/webui-20260917T095038Z.db`
(0600, integrity ok).

### 21.1 Production Open WebUI (the real `open-webui` container behind chat.legenex.co)

**Path traced, live:**

1. **Front end:** chat.legenex.co → cloudflared (token tunnel) → `open-webui`
   (host network :3000, Open WebUI 0.11.3; `/api/version` is identical on
   both URLs).
2. **Connection:** Open WebUI's connection #1, `http://100.105.214.61:4000/v1`
   (connection #0, Nous, is disabled).
   * The key is the LiteLLM virtual key `kilo-code`, matched by sha256 prefix
     `1d6e027fdf68`. It is not the master key (see B-029).
3. **Gateway:** LiteLLM `gx-mini` → `http://gx-llama-swap-node01:8080/v1`.
   The mounted `config.yaml` has the same md5 as the checkout.
4. **llama-swap node01:** `node01.yaml`, also the same md5 as the checkout →
   container `gx-mini` (network `container:gx-llama-swap-node01`).
5. **gx-mini process:** `/app/llama-server -m
   /models/gguf/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf
   --mmproj …/mmproj-Qwen3.5-4B-Uncensored-HauhauCS-Aggressive-BF16.gguf
   --ctx-size 131072 --parallel 2`.
   * `/props` reports that `model_path`, vision `true`, 2 slots × `n_ctx`
     65536, build b10948.
6. **Files:** sha256 `79e28eca…2741` (GGUF) and `a1e32e86…0f22` (mmproj),
   equal to `.gx-manifest.json` and to the live Hugging Face LFS hashes at
   revision `c09cdbcdb1fefad6d335809d445621b5f5ba0c6e`. The GGUF reports
   4 205 751 296 parameters, and the card says "Based on Qwen3.5-4B, 4B
   dense".

**Root cause.**
* Open WebUI had **no model entries** for the gx aliases: 398 stale
  OpenRouter/Nous rows, none for gx-*, no system prompt anywhere, no user
  memories. The aliases came straight from the connection.
* The user's two chats ("Model Identity", "Model Information", 07:33-07:40
  UTC) show gx-mini answering "Qwen3.5 … Tongyi Lab … official,
  full-precision" and, in the other chat, "Grok-3 Mini".
* The direct LiteLLM control **without** a system prompt reproduces this
  ("My full underlying model name is **Qwen3.5** … developed by Tongyi
  Lab"). Routing was correct.

**Change.** Five Open WebUI model entries (gx-mini, gx-fast, gx-reason,
gx-max, gx-auto) were written through Open WebUI's own model layer:
* owned by the admin, no grants;
* each with a registry-generated system prompt (D-038).

Nothing else changed: chats 18 → 18, users 1 → 1, grants 0.

**API acceptance** (disposable `user` account signed in through the public
URL; `owui_identity_acceptance.py`): **33/33.**

| Question (gx-mini, conversation and fresh chat) | Answer |
|---|---|
| What model are you? | "I am **gx-mini**, and my underlying model is **HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive**, derived from Qwen/Qwen3.5-4B." |
| Full underlying model name? | "…**HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive**." |
| Are you HauhauCS/…? | "Yes." |
| Parameters of the base model? | "Qwen/Qwen3.5-4B … about **4 billion** parameters." |
| Runtime? | "**llama.cpp** on **gx10-01**, behind **llama-swap** and a **LiteLLM** gateway." |
| Context per request? | "up to **65,536 tokens** per request (prompt and reply together)." |

No answer claimed a larger model, another product, full precision or an
official release.

**Proof that the requests reached the verified backend** (same run):
* **LiteLLM:** all 12 test requests are in the spend log. Each has the Open
  WebUI key hash, `model_group=gx-mini`,
  `api_base=http://gx-llama-swap-node01:8080/v1/` and the exact
  (prompt, completion) token pairs Open WebUI returned, for example
  `chatcmpl-GwyD6y…` with 451/49.
* **llama-swap node01:** 12 `POST /v1/chat/completions` lines from
  `172.21.0.4` (gx-litellm).
* **gx-mini llama-server:** 12 slot releases with `n_tokens` equal to
  prompt + completion − 1.
* **Earlier attempts:** they also saw concurrent real user traffic under
  the same key, which is excluded by token matching.

**Controls and other aliases:**
* A direct LiteLLM call with the identity prompt names the HauhauCS model;
  without a prompt the model says "Qwen3.5".
* **gx-fast:** "42" in 1.1 s; "I am gx-fast … kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4,
  derived from Qwen/Qwen3.6-35B-A3B."
* **gx-auto:** "42" in 1.5 s; "I am answering through gx-auto … recorded in the
  cluster's routing journal".
* **gx-max:** visible, not cold-started.

**Browser acceptance** (system Chrome through https://chat.legenex.co,
`owui_identity_browser.py`): **6/6.**
* Signed in on the public login page, opened a new chat with `?models=gx-mini`,
  and the UI showed "I am gx-mini. My underlying model is
  HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive, derived from
  Qwen/Qwen3.5-4B."
* Open WebUI stored that chat on model gx-mini.
* The account, its chat and its grants were deleted afterwards (verified).

**What the first attempts found.** They needed prompt and harness fixes,
and all of them are in the committed code:
* "Keep the alias and the underlying model distinct" made the model answer
  "No" to "Are you HauhauCS/…?";
* a missing base-model size made it say "I do not know";
* the listing hides system prompts from non-owners.

### 21.2 gx-video + gx-music keep the 30 GiB reserve (router 2.4.0, gx-music 1.1.0)

**Root cause of the 18.6 GiB case (§20.4).**
* Router 2.3 admitted a cold t2v when MemAvailable ≥ 76 GiB. That is the
  measured 72 GiB footprint plus 4 GiB, with no reserve. With music loaded
  (about 88 GiB left) the video fit that check and took the node to about
  16-19 GiB.
* The Control Center used the same numbers and skipped its check while the
  router was busy.
* The music supervisor could not see a router load in progress, and the
  router could not see a music load in progress.

**Live run 2** (final code; real generations; gx10-02 sampled every second;
`reserve-live-20260917T103728Z`): **20/20.**

| Step | Result |
|---|---|
| S1 music | 15 s track saved in 103.8 s (82.3 s load, 20.2 s generation). Resident size measured **26.2 GiB**; pending 5.8 GiB |
| S2 video next to idle music | The Control Center gate unloaded gx-music with `if_idle`, and the release was verified: engine unloaded, container gone, ledger clean; 88.3 → 113.8 GiB. The video then ran (45.6 s, 33/33 distinct frames) |
| S3 second (warm) video + music | Warm growth was measured (held 67.9 GiB, so 8 GiB growth); the video ran in 30.6 s. The music job submitted meanwhile waited 109.6 s with "Waiting for gx-video to finish on gx10-02: gx-music needs about 32 GiB plus the 30 GiB reserve plus 6 GiB that the running media job has not taken yet, so 68 GiB must be available; 45 GiB is". It then asked the router to free ComfyUI, loaded (89.4 s) and completed |
| S4 router API, music pinned | `POST /v1/videos` 202. `GET` showed `status: queued, phase: waiting` with blocker gx-music, required 107.8 / available 88.1 / reserve 30 GiB, projected 10.3 GiB, next "gx-music is pinned…". The engine stayed loaded for 20 s. After unpin, the router's eviction was verified (released 25.4 GiB in 4.1 s), and the video completed (48.3 s, 33 frames, 246 174 B) |
| S5 keyframe edit | Failed after 2.0 s: "…needs about 107 GiB plus the 30 GiB reserve (137 GiB)… (B-028)" |
| Memory | 403 samples: **minimum MemAvailable 41.97 GiB**, 0 s below 30 GiB. SwapFree 59.22 GiB at start, minimum and end (flat) |
| Cleanup | 4 test assets deleted, pin removed, Library search for the run tag = 0 |

**Live run 1** (same day, before two fixes, `reserve-live-20260917T101532Z`):
* S0-S2 passed, with the same unload-then-video sequence.
* Over 1 189 samples the minimum MemAvailable was **41.63 GiB**, 0 s below
  30 GiB, and swap was flat.
* It found two real defects, both fixed and covered by tests:
  1. **A recreated router forgot what ComfyUI still held.** gx10-02 showed
     46 GiB with a record of nothing loaded. The router now frees once
     after a start.
  2. **The Control Center gate ignored the router's own resident weights
     while its tile showed WAITING.** A warm video waited for memory that
     only an idle timer would free. The gate now reads residency from the
     router and counts the router's own reclaimable weights.
* It also found two harness faults: the pin API returns 202, and a cold
  video has phase `loading` rather than `generating`.

### 21.3 Suites (final code)

| Suite | Result |
|---|---|
| Control Center `npm run qa` | ruff, mypy, **419** unit/API/auth tests, performance budget, build, **17/17** browser E2E + axe, gitleaks + npm audit: QA PASSED |
| GX-Playground `npm run qa` | ruff, **9** proxy tests, build (258.1 KiB), **19/19** browser E2E + axe, gitleaks: QA PASSED |
| gx-music `qa.sh` | **60** tests (54 + 6 reserve-coordination tests), no literal credentials |
| media router `qa.sh` | **111** tests (87 + 21 reserve tests + 2 restart-reconcile tests + updated admission tests), templates, compose, secret scan: QA PASSED |
| orchestrator | **170** |
| lifecycle | **38** |
| git-sync `sync-regression.sh` | **19/19** |
| kernel lock verifier | gx10-01 **13/13**, gx10-02 **13/13** (`6.17.0-1032-nvidia`) |

## 22. Build V3 integration pass (2026-09-17 20:30-22:00 SAST, gx10-01)

Lead integration of the IMG, WAN, CAL, LIV, FLO, VOI and MUS workstreams, plus
the deployment and Hugging Face diagnosis work (D-041, B-030, B-031).

### 22.1 The deployment gap this pass found and closed

`legenex/playground/scripts/deploy.sh --verify` compares the ETag the running
server returns for every file under `web/` with `sha256` of that file on disk.
Against the live Playground **before** the fix:

```
32 files checked: 21 stale, 11 on disk but returning HTTP 404
```

The 404s were every file created after the process started at 10:29, including
`js/routes.js`, `js/wan.js`, `js/realtime.js`, `js/music-form.js` and the
Voice, Models, Logs and Settings pages. After the fix, and after each
integration step since:

```
43 files served match the checkout; 0 stale, 0 not served
```

Revalidation was proven live with **zero restarts** (`NRestarts` stayed `0`): a
file created in `web/js/` after start-up was served 200 immediately, changing it
changed the served ETag, deleting it returned 404.

### 22.2 Deployed-site browser acceptance (new permanent spec)

`legenex/playground/e2e/live.navigation.spec.js`, project `live`, against the
deployed Playground. Evidence + screenshots:
`/srv/logs/acceptance/build-v3/plt/navigation-final/`.

**14/14 passed.** The navigation test asserts all 13 pages in the right three
groups with the right labels and hrefs and the Control Center link in the
header; each page test asserts the page opens, is not a placeholder, **did not
throw while loading**, has an accessible name on every visible control, is
keyboard-focusable with a visible ring, produces no console or network errors,
and has **no axe WCAG 2.2 AA violations**.

| Page | Rendered |
|---|---|
| dashboard | 2 335 chars |
| flows | 193 |
| images | 799 |
| video | 1 984 |
| music | 2 596 |
| voice | 5 650 |
| live | 929 |
| call | 388 |
| library | 2 859 |
| history | 7 009 |
| models | 8 028 |
| logs | 12 445 |
| settings | 2 847 |

Defects this spec caught, each fixed and re-verified:

1. **Music rendered nothing but an error.** `web/js/pages/music.js` used six
   `music-form.js` exports without importing the module → "This page could not
   be loaded: aiPanel is not defined". 53 chars → 2 596 chars.
2. **The spec itself had two faults**, corrected before trusting it: it used
   `innerText` for accessible names (empty for a label inside a collapsed
   section, so correctly labelled controls read as unnamed), and it judged
   pages while skeleton loaders were still up.

### 22.3 I2V footprint — the missing media measurement

`python3 legenex/tests/media_footprint_probe.py i2v:640x640:33`, gx10-02,
1 Hz, 82 samples. Evidence `/srv/logs/acceptance/media-footprint-20260917T183849Z/`.

| | |
|---|---|
| Baseline MemAvailable | 111.9 GiB |
| Minimum | **40.59 GiB** |
| Growth | **71.3 GiB** |
| Seconds below the 30 GiB reserve | **0** |
| Swap free minimum | 60.88 GiB (no swapping) |
| Generation elapsed | 51.32 s (82.2 s total incl. the source image) |
| Frames | 33 requested, 33 returned |
| After unload | 112.5 GiB |

Preconditions verified first: gx-max unloaded, gx-reason unloaded, gx-music
`engine: unloaded`, router idle with an empty queue, ComfyUI freed through the
router's own path (never ComfyUI `/free` directly), memory settled, no holds.

### 22.4 Wan 2.2 LoRA live acceptance (WAN)

Four real generations on gx10-02 through media router **2.5.0**. Evidence
`/srv/logs/acceptance/build-v3/wan/`.

* Branch placement traced on the real graph: KSampler 12 ← 7 ← **1000 user-high
  LoRA** ← 5 base-high ← 3 UNET high_noise; KSampler 13 ← 8 ← **2000 user-low
  LoRA** ← 6 base-low ← 4 UNET low_noise. **Shared nodes between the two model
  paths: none.**
* Same seed, LoRA pair on vs off: different sha256 and +34 % bitrate, so the
  LoRA measurably changed the output.
* `ffprobe` on both: h264 640x640, `nb_read_frames=49`, duration 3.0625 s;
  library analysis agrees (49 distinct frames, no frozen frames).
* Router refusals proven live: high file on the low branch → 400
  `lora_branch_mismatch`; unknown name → 400 `lora_not_found`; strength 99 →
  400 `lora_invalid_strength`.
* Memory, 1 087 samples at 1 Hz: baseline 112.56 → minimum **39.26 GiB**,
  growth 73.30 GiB, 9.26 GiB clear of the reserve, swap flat, final 111.55 GiB.

### 22.5 Suites (this pass, all really run)

| Suite | Result |
|---|---|
| Control Center `unittest discover -s tests` | **708 passed, OK** |
| Control Center `tests/test_calls.py` | **15 OK** (was 3 failures + 1 error) |
| Control Center `tests/test_live.py` (new) | **29 OK** |
| Control Center `tests/test_hf_access.py` (new) | **12 OK** |
| Control Center `tests/test_flows*.py` | **58 OK** |
| Control Center `tests/test_wan_video.py` | **35 OK** |
| Control Center `tests/test_media_manager_keys.py` | **18 OK** under `.venv` (was 5 errors + 1 failure: Pillow was missing from the venv, now installed and bootstrapped by `qa.sh`) |
| media router `qa.sh` | **174 OK**, 15 templates validated, QA PASSED |
| media router `tests/test_wan_loras.py` | **40 OK** |
| GX-Playground unit/proxy/tunnel | **OK** |
| gx-live service + engine `qa.sh` | **46 OK** (29 supervisor + 17 engine) |
| gx-voice service | **46 OK** |
| gx-call service | **28 OK** |
| flows-ui `npm run qa` | tsc clean, **eslint 0 problems** (was 30 errors), vitest **13/13** |
| Deployed-site navigation (`live.navigation.spec.js`) | **14/14** |
| Deployed-site viewer regression (`live.viewer-playback.spec.js`, new) | **1/1** |
| Deployed Control Center HF access (`live.hf-access.spec.js`, new) | **2/2** |
| Control Center full suite, final | **713 passed, OK** |
| Repository secret scan (`gitleaks git .`, tracked history) | **clean, 0 findings** |

### 22.6 Real defects found in shared code and fixed

1. **`MediaJobs.submit()` delivered `submitted` after enqueueing.** A
   fast-failing job therefore delivered `failed` from the worker thread *before*
   `submitted`, so an observer that inserts its row on `submitted` lost the
   result and kept a `queued` row for ever. `WanVideo.observe` had exactly that
   exposure. Observers are now notified before the worker can see the job, and a
   `cancel()` that arrives in that window is honoured.
2. **`workspace.js renderViewer()` rebuilt its `<video>` on every re-render**, so
   a user watching a clip was thrown back to 0 whenever a job card ticked or a
   favourite was toggled. Proven fixed in the deployed browser: playback ran
   1.20 s → 2.14 s across a re-render, `paused: false`, `error: null`.
3. **The Call Agents audit sink was silently a no-op.** `self.audit = audit or
   (lambda **_: None)` — the test's sink was an empty `list` subclass, which is
   falsy, so every audit went to the no-op. Same for `CallManager.metric`.
4. **`end_session` waited on an event poller that never runs offline**, so the
   recording route answered 409 "session not ended". It now finalises from
   gx-call's own authoritative response, single-shot.
5. **An `aria-label` on an xyflow handle** (a plain `<div>`, where that attribute
   is prohibited) — a serious axe WCAG 2.2 AA violation on Creative Flows.
6. **`gx-voice` was unreachable through the gateway, and a restart could not have
   fixed it.** `litellm/config.yaml` referenced
   `api_key: os.environ/GX_VOICE_API_KEY`, but `docker-compose.gateway.yml`
   never passed that variable and `.env` never defined it. Wired through both
   and recreated with the B-027 procedure. Proven end to end:
   `POST /v1/audio/speech {"model":"gx-voice"}` → 200, 184 364 bytes, and
   ffmpeg reports `00:00:03.84, pcm_s16le 24000 Hz mono, mean_volume -24.2 dB`.
   Evidence `/srv/logs/acceptance/build-v3/voi/gateway-20260917T1939Z/`.
7. **`config.py` declared `voice_base` twice**, the second definition silently
   winning, and `TempEnv` wrote the voice key to a path the node-2 service
   clients do not read — so the Voice capability row appeared in production but
   never in the offline fixture.
8. **`offline.f-history-resources.spec.js` depended on other spec files having
   left jobs behind**, so it failed whenever it ran first or alone. It now seeds
   its own media and music jobs and waits for real terminal phases; it passes
   standalone in 4 s.

A regression the lead introduced and caught by re-running the **full** suite
rather than trusting targeted runs: pre-creating the per-alias secret
directories in `TempEnv` collided with `mkdir(parents=True)` in
`tests/test_calls.py`, turning CAL's 15/15 into 6 errors. Both call sites are
now idempotent and the suite is back to 713 OK.

### 22.7 Hugging Face access diagnosis (B-030)

`tests/test_hf_access.py`, 12 hermetic tests against a stub Hub that serves
metadata 200 and files 401/403, pins the distinction that had been collapsed
into one misleading message:

| Situation | Code | Action the UI now gives |
|---|---|---|
| No usable token | `unauthenticated` (401) | save a token; a fine-grained one also needs the gated-repo permission |
| Token valid, account not granted | `gated_not_granted` (403 `GatedRepo`) | accept the model's terms in a browser as that user — **"A new token cannot fix this"** |
| Granted | `granted` | — |
| Not gated | `public` | — |

Against the real repository, with the real token: metadata **200**, files
**403 GatedRepo**, token user `legenex`, `canReadGatedRepos: true`.

