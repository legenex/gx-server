# Test results

Only results actually observed are recorded here. Anything not yet run is marked
NOT RUN rather than assumed.

Last updated: 2026-09-14.

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

## 6. Remaining

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
