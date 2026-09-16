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
