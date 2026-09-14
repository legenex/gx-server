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

## 4. Not yet run

| Acceptance test | Status |
|---|---|
| gx-mini text + vision inference | **NOT RUN** — blocked on gx-max releasing node 1 |
| gx-fast inference / tool use / vision | **NOT RUN** — model downloading |
| gx-reason inference | **NOT RUN** — model downloading |
| gx-auto full multi-class routing against live tiers | **PARTIAL** — logic verified, live tiers pending |
| Full lifecycle drain/acquire/release cycle | **NOT RUN** |
| gx-image real generation | **NOT RUN** — ComfyUI not built |
| gx-video real generation | **NOT RUN** — ComfyUI not built |
| Gateway restart recovery | **NOT RUN** |
| Remote access via Tailscale endpoint | **NOT RUN** |
