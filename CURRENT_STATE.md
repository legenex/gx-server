# Current state

**This file must always reflect reality.** If you are a new agent resuming this
work, read this first, then ARCHITECTURE.md (what is locked), then BLOCKERS.md.

Last updated: 2026-09-14 23:59 CEST, by the lead agent on gx10-01, after a
full autonomous session covering resource-ownership hardening, a gateway/
orchestrator incident on node 1, a Qwen3.8 retirement, and independent review
of gx-auto routing and the media router. **Node 2 remains physically wedged
and untouched throughout — nothing below was validated against it.**

---

## One-paragraph summary

**Node 1 is healthy and hardened; node 2 needs a physical power cycle.**
gx-mini and gx-auto are verified working end-to-end through the gateway
tonight. gx-fast is correctly wired but was deliberately not cold-started
(memory-safety hold — see below). gx-reason, gx-max, gx-image and gx-video all
correctly and honestly report `unavailable`/`node2_offline` rather than faking
health, thanks to a fixed tier-health-probe bug found and fixed this session.
A resource-ownership/admission-control layer now makes tonight's root cause —
two large models resident on one node at once — structurally refused rather
than merely discouraged, proven by concurrency tests, not just asserted. A
second, unrelated incident on node 1 (an unmanaged 80 GiB container, plus a
crash-looping gateway and a never-started orchestrator) was found and fixed.
See `coordination/BLOCKERS.md` for the exact, short list of things that need a
human or a live node 2.

## ⚠ Node 2: physically wedged, power cycle required

Node 2's kernel is alive — ICMP on `192.168.100.11` replies with 0% loss,
sub-millisecond RTT — but **userspace is starved**: SSH does not complete a
banner exchange over Tailscale or the fabric, its llama-swap does not answer.

**Cause:** two ~77 GB mmap'd models were resident on a 121 GiB node at once
(gx-reason plus a one-off CPU-only diagnostic container started with a bare
`docker run`, bypassing llama-swap's own model-group exclusivity entirely).
Because mmap pages are reclaimable, the OOM killer did not fire — the node
thrashed indefinitely rather than shedding load. Full account in
`coordination/BLOCKERS.md` B-012.

**Repair, done tonight:** a resource-ownership/admission-control layer (see
below) that makes this exact shape of double-large-load structurally refused,
not just documented as a rule. It cannot protect node 2 until node 2 is back
and the same tooling is deployed there.

**Needs:** a physical power cycle by a human. Nothing here can recover it
remotely. Do not repeatedly poll it — `legenex/scripts/recover-node2.sh` is
ready to run, report-only, the moment it's back.

## Second, unrelated node-1 incident tonight (found and fixed)

While node 2 was already down, node 1 independently had two real problems,
neither caused by node 2:

1. **`gx-litellm` had lost its Docker network attachment entirely** (empty
   `NetworkSettings.Networks`) and was crash-looping against an unreachable
   `litellm-db:5432`. Fixed by recreating it via
   `docker compose -f legenex/gateway/docker-compose.gateway.yml up -d litellm`.
   Verified: `HTTP 200` on `/health/liveliness`, `RestartCount=0`.
2. **The orchestrator (`gx-auto`/`gx-max` control plane, port 18900) was not
   running at all** — no process, no systemd unit had ever existed for it,
   despite an earlier version of this file claiming it was "running,
   healthy". Fixed: started it and added
   `~/.config/systemd/user/gx-orchestrator.service` (enabled, hardened —
   `NoNewPrivileges`, `ProtectSystem=strict`, binds only
   `127.0.0.1,172.17.0.1:18900`, never `0.0.0.0`).

Separately, **`vllm-qwen38-uncensored`** — a standalone, unmanaged, always-on
vLLM container holding ~80 GiB resident, entirely unrelated to the gx-mini/
gx-fast/gx-reason/gx-max/gx-auto/gx-image/gx-video tier set — was identified
by the human operator as a major memory-safety risk (it left as little as
~9 GiB available system-wide) and has been **permanently retired**: container
and checkpoint deleted by the operator, systemd unit disabled, Docker restart
policy set to `no`, and every active runtime/download/routing/lifecycle
reference to it removed from this repo (`c0076f8`). **Qwen3.8 is not gx-fast
and must never be reintroduced under any tier alias.**

## Resource ownership (new this session)

Direct response to the B-012 root cause. Full detail in `ARCHITECTURE.md`
§9 and `coordination/DECISIONS.md`; summary here:

- `legenex/orchestrator/gx_orchestrator/resource_guard.py` +
  `legenex/lifecycle/resource-guard.sh` — one shared arithmetic module (not
  duplicated in bash vs Python): workload classes (small/medium/large/
  exclusive), a 30 GiB minimum-reserve floor checked against both a
  residency ledger AND live `/proc/meminfo`, a flock-backed cross-process
  `NodeLock`.
- `legenex/lifecycle/gx-safe-run.sh` — the sanctioned replacement for a bare
  `docker run` on any medium/large/exclusive container; this is the tool the
  B-012 diagnostic should have used.
- `gx-max-start.sh`/`gx-max-stop.sh` now route both rank launches through a
  hard, non-bypassable admission guard (`GXMAX_FORCE_DRAIN` can no longer
  skip it), and `_do_acquire()` in `lifecycle.py` now unwinds any
  partially-started rank on a failed acquire instead of leaking it.
- Docker `--memory`/`--memory-swap` caps and `--oom-score-adj` biasing (700-
  950 for model tiers, 100 for the gateway/db/llama-swap) on every model
  container, so the kernel's OOM killer sacrifices these before host daemons
  even if something is ever started outside the guarded path.
- `legenex/host/gx-hostwatch.sh` — a dependency-free watchdog (systemd
  `--user` timer) checking sshd (banner-level, not just TCP), tailscaled,
  responsiveness and memory pressure. Logs and alerts only; no remediation.

**Proven by test, not asserted:** two concurrent launch requests for one node
cannot both proceed (5 real racing OS processes); a launch that would violate
the 30 GiB reserve never runs; two large/exclusive workloads can never
coexist on one node regardless of the arithmetic (the literal B-012 shape); a
SIGKILLed lock holder never leaves a stuck lock.

**What remains convention, not enforcement:** node 2 has no deployed ledger
yet (unreachable tonight) — its rank1 launch uses a real remote `flock` as a
documented convention, not yet backed by a residency ledger. Nothing stops a
human/agent from still typing `docker run` directly; `gx-safe-run.sh` is the
documented one-line-longer sanctioned alternative, not a kernel-enforced
prohibition. Protecting sshd/tailscaled/systemd/NetworkManager *directly*
(rather than via OOM-score bias on our own containers) is confirmed to
require root — see `coordination/BLOCKERS.md`.

## Hardware

| | gx10-01 (node 1, control) | gx10-02 (node 2, compute) |
|---|---|---|
| Kernel | `6.17.0-1032-nvidia` | `6.17.0-1032-nvidia` (last known) |
| Arch / Python / Docker | aarch64 / 3.12.3 / 29.2.1 | identical (last known) |
| GPU / driver / CUDA | GB10, 580.173.02, CUDA 13.0 | identical (last known) |
| RAM | 121 GiB | 121 GiB |
| Swap | 63 GiB (`/swap.img` + `/swapfile-sglang` 48 G) | 63 GiB (same two files, last known) |
| sudo | **password required** | **password required** |
| User lingering | enabled | disabled (last known) |

GPU passthrough is **CDI** (`--device nvidia.com/gpu=all`) on both nodes. There
is no `nvidia` docker runtime and no `/etc/docker/daemon.json`.

## Fabric

| Rail | node 1 | node 2 | state (last known) |
|---|---|---|---|
| A `enp1s0f0np0` / `rocep1s0f0` | 192.168.100.10 | 192.168.100.11 | ACTIVE, 0.211 ms |
| B `enP2p1s0f0np0` / `roceP2p1s0f0` | 192.168.101.10 | 192.168.101.11 | ACTIVE, 0.338 ms |

Tailscale is management/SSH only — confirmed by measurement, not assumption.
`ssh legenex-02@gx10-02` (Tailscale) is the correct SSH endpoint; SSH directly
to `192.168.100.11` is refused — the fabric addresses are not SSH endpoints.

## What is running right now (node 1, verified via `legenex/scripts/gx-status.sh`)

| Service | Port | State |
|---|---|---|
| LiteLLM gateway | 4000 (loopback) | **healthy** |
| Postgres (LiteLLM) | 15432 (loopback) | **healthy** |
| llama-swap node 1 | 28080 / 19001 (loopback) | **healthy** |
| gx-orchestrator | 18900 (loopback + docker bridge) | **healthy**, systemd-managed |
| gx-hostwatch | — (timer) | **running**, logging to `/srv/logs/gx-hostwatch.log` |
| gx-mini (llama.cpp) | via llama-swap | **loaded**, resident |
| gx-fast (vLLM) | via llama-swap | **stopped** (on-demand; not cold-started tonight, see below) |
| gx-max rank 0/1 (SGLang) | 30000 | **stopped** (`node2_unavailable`) |

Node 1 `MemAvailable`: ~106-112 GiB at idle (was ~9 GiB before the Qwen3.8
retirement). Node 2 is unknown beyond kernel-level ICMP liveness.

## Tier status

| Alias | Model | Engine | Node | State tonight |
|---|---|---|---|---|
| gx-mini | Qwen3.5-4B Q4_K_M + BF16 mmproj | llama.cpp | 1 | **WORKING** — real inference + vision path verified live through the gateway tonight |
| gx-fast | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM | 1 | **wired correctly, not cold-started tonight** — model path (22G, 3 shards) and image verified present on disk; deliberately not started under the memory-safety hold. Stale "PENDING-VERIFY" comments in `node01.yaml`/`litellm/config.yaml` should be cleaned up next session — the values themselves are real |
| gx-reason | `unsloth/Qwen3.5-122B-A10B-GGUF` UD-Q4_K_XL | llama.cpp | 2 | **unavailable — node2_offline**, correctly reported (was previously B-011 "loads but garbage output"; diagnostic tooling now exists — `legenex/scripts/gx-reason-diagnose.sh` — but not run, node 2 is down) |
| gx-max | `nvidia/DeepSeek-V4-Flash-0731-NVFP4` | SGLang TP=2 | 1+2 | **stopped — node2_unavailable**, correctly refused at preflight (verified live: a validation attempt tonight died at the SSH-timeout preflight step in ~10s, no container ever created on either node) |
| gx-auto | — | orchestrator | 1 | **WORKING** — two real routing bugs found and fixed this session (see `coordination/DECISIONS.md`); 51 classifier tests |
| gx-image | Qwen-Image 2512 (+Lightning LoRA) / HiDream I1 (not wired — see gap below) | ComfyUI | 2 | **unavailable — node 2 offline.** Router code independently re-verified: 43/43 tests pass (was 36/36, now includes 3 new security-regression tests), 3 real fixes applied (auth-bypass, cross-kind workflow mixing, header-injection defense-in-depth) |
| gx-video | Wan 2.2 A14B (LTX 2.3 not used — licence) | ComfyUI | 2 | **unavailable — node 2 offline.** Same router verification as gx-image. No "hd"/no-LoRA tier wired yet (gap, see below) |

**Known media gap, found tonight, not yet fixed:** `MODELS.md` documents
HiDream-I1-Full and a no-LoRA "quality" Wan variant as available checkpoints,
but neither has a `_gx`-enabled template in `legenex/media/workflows/` yet —
`gx-image` cannot serve HiDream today, and `gx-video` has no "hd"-equivalent
the way `gx-image` does (standard/hd). Not blocking tonight; worth deciding
before it's assumed done.

## Automated test count tonight

168 tests passing across three independent suites, all runnable without
node 2:

```
legenex/orchestrator:        116 tests   (python3 -m unittest discover -s . -p 'test_*.py')
legenex/lifecycle/tests:       9 tests   (python3 -m unittest discover -s tests -p 'test_*.py')
legenex/media/router:         43 tests   (./qa.sh)
```

Plus live verification against the real running system tonight (not just unit
tests): gx-mini real inference + vision through the gateway; the tier-health
fix confirmed against the actually-wedged node 2; the resource guard's
node-lock and reserve-floor logic; and one real (accidental, harmless, fully
disclosed) `gx-max` acquisition attempt that correctly failed at preflight in
~10s with no container ever created on either node — a live, unplanned
confirmation of the "never partially start rank0" safety property.

## Repository layout (what this session added, on top of the prior session's work)

```
legenex/orchestrator/gx_orchestrator/
  resource_guard.py      workload sizing, admission math, NodeLock, ResidencyLedger
  health.py               per-tier real-upstream health probing (replaces gateway-liveness proxy)
  status_cli.py           `gx status` implementation
legenex/lifecycle/
  resource-guard.sh       bash-side admission-control library (same arithmetic as resource_guard.py)
  gx-safe-run.sh          sanctioned replacement for a bare `docker run`
  tests/                  bash-side resource-guard tests
legenex/host/
  gx-hostwatch.sh         dependency-free host resilience watchdog
  systemd/                its service+timer templates
legenex/scripts/
  gx-status.sh            `gx status` entry point
  recover-node2.sh        node-2 recovery checklist (report-only until --apply)
  gx-reason-diagnose.sh   B-011 GPU-vs-CPU diagnostic, unload-gated
legenex/tests/
  gx-max-validate.sh      full acquire->serve->release->restore validation for tomorrow
```

## Known gaps

See `coordination/BLOCKERS.md` for the full list with severities. The ones
that matter most:

* **B-001** the kernel pin has no `apt-mark hold`, and kernel 7.0 is still
  installed on both nodes (last known). Needs root.
* **B-003** SGLang `:30000` is bound `0.0.0.0` with no auth.
* **New tonight** — protecting sshd/tailscaled/systemd/NetworkManager
  directly (not just via OOM-score bias on our own containers) and arming
  the hardware watchdog both require root; exact commands are ready and
  documented, not applied.

## How to resume

```bash
cd /home/legenex/Documents/Projects/Server/gx-cluster
legenex/scripts/gx-status.sh                   # one-shot cluster status, human + --json
curl -s localhost:18900/health/detailed        # orchestrator + tier view
cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'
```

When node 2 physically comes back:

```bash
legenex/scripts/recover-node2.sh               # report-only by default; --apply to act
GX_RUN_SLOW=0 legenex/tests/acceptance.sh       # node1-safe tiers first
legenex/scripts/gx-reason-diagnose.sh           # B-011 GPU-vs-CPU, only once node 2 is clean
legenex/tests/gx-max-validate.sh                # full two-node lifecycle, only once node 2 is clean
```
