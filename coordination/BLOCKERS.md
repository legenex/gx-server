# Blockers

Things that need a human, or that are known-broken and not yet fixed.
Severity: **S1** = blocks the locked architecture. **S2** = blocks a deliverable.
**S3** = risk / hygiene.

---

## B-001 (S1) — The kernel pin is weaker than it appears
**Needs:** a human with sudo.

The kernel is pinned only by `GRUB_DEFAULT` on both nodes. But:

* `apt-mark showhold` returns **nothing** on either node.
* `linux-image-nvidia-hwe-24.04` and `linux-image-7.0.0-1019-nvidia` are
  **still installed** on both nodes.

So an `apt upgrade` can move the meta-package, and an `apt autoremove` could
delete 6.17.0-1032 and leave GRUB pointing at an entry that no longer exists.
Kernel 7.0 is the exact thing that broke gx-max (D-001).

Mitigating: `/etc/apt/apt.conf.d/20auto-upgrades` does not exist, so there are no
unattended upgrades.

**Suggested fix (not applied — needs root):**
```
sudo apt-mark hold linux-image-6.17.0-1032-nvidia linux-headers-6.17.0-1032-nvidia \
                   linux-image-nvidia-hwe-24.04 linux-headers-nvidia-hwe-24.04
```
Run on **both** nodes.

## B-002 (S1) — No sudo on either node
**Needs:** a human, or a decision that it is not needed.

`sudo -n true` requires a password on both gx10-01 and gx10-02. Everything in
this build is therefore designed to run **rootless**: Docker (the user is in the
`docker` group) plus `systemctl --user`. That is sufficient for the whole stack.

It is **not** sufficient for: `apt-mark hold` (B-001), editing
`/etc/docker/daemon.json`, host firewall rules (B-003), or persisting the CDI
spec (B-004).

Note: user lingering is **enabled on gx10-01** but **disabled on gx10-02**, so
node-2 user services will not survive logout/reboot until
`loginctl enable-linger legenex-02` is run there.

## B-003 (S2) — SGLang :30000 is bound 0.0.0.0 with no authentication
**Needs:** a decision, then either a bind change or a firewall rule.

gx-max's HTTP server listens on all interfaces with no auth, so it is reachable
from the LAN **and** from the tailnet (which includes other machines). Anyone who
can reach the host can run inference, read `/get_server_info`, and call
`/flush_cache`.

The argument vector is LOCKED, so changing `--host` is a decision, not a fix I
should make unilaterally. Options:
1. Change `--host` to `127.0.0.1` in `gx-max.conf` — clean, but rank1 must still
   reach rank0's bootstrap on the fabric address (that is a *separate* port,
   5000, so this is probably safe and should be tested).
2. Leave the bind and add a host firewall rule (needs root).

Until then, treat the tailnet as trusted.

## B-004 (S3) — CDI spec lives on tmpfs
`/var/run/cdi/nvidia.yaml` is regenerated each boot. GPU passthrough uses CDI
(`--device nvidia.com/gpu=all`) because **no `nvidia` docker runtime is
registered** on either node (`docker info` shows `runc` only, and
`/etc/docker/daemon.json` does not exist). If the spec ever fails to regenerate,
every GPU container breaks at once. Worth a boot-time health check.

Consequence for config: the base repo's `--runtime nvidia --gpus all` and
compose `deploy.resources.reservations.devices` forms **do not work here**.

## B-005 (S3) — 164 GB DeepSeek checkpoint is duplicated by hand
There is no shared filesystem. `/srv/models` is a plain directory on root ext4 on
both nodes, and the gx-max weights were copied node-to-node with rsync. There is
**no checksum manifest**, so silent divergence between the two copies would only
show up as a confusing runtime failure.

Also: node 1's copy is root-owned, node 2's is user-owned.

## B-006 (S3) — No observability on either node
No node_exporter, no DCGM exporter, no Prometheus, no log shipping. Diagnosis is
currently "ssh in and read files". `--enable-metrics` (D-002) is the first step.

## B-007 (S3) — LTX 2.5 is gated
`Lightricks/LTX-2.5` is `gated: "auto"` and needs a human to accept terms at
huggingface.co and provide an `HF_TOKEN`. **Not blocking** — LTX 2.3 is ungated
and ships first. Flagged because 2.5 is better-packaged for ComfyUI.

## B-008 (S3) — Licence review for commercial media use
Qwen-Image, Wan 2.2 and HiDream are Apache-2.0/MIT and clean. **LTX 2.3 uses the
LTX-2 Community License and its pipeline pulls a Gemma-3 text encoder under the
Gemma Terms.** If the stated ad-creative use case is commercial, a human should
read both before gx-video is used for client work.

## B-009 (S2) — vLLM cannot load checkpoints larger than ~55 GiB on these nodes
**Status:** worked around, not fixed.

Measured this session: vLLM reserves its GPU pool and then loads weights into
*additional anonymous* memory (`RssAnon` 36.6 GB vs `RssFile` 48 MB mid-load).
On unified memory that means the load needs roughly `pool + checkpoint`, so a
73 GiB checkpoint cannot be loaded on a 121 GiB node. Full evidence in
DECISIONS.md D-009.

Consequence: any future tier planned for vLLM must keep its checkpoint under
roughly 55 GiB, or use llama.cpp instead.

Worth revisiting if a vLLM build appears that loads directly into the reserved
pool, or that exposes an equivalent of SGLang's
`--weight-loader-drop-cache-after-load`.

## B-010 (S3) — gx-fast runs weight-only FP4, not native FP4
The available GB10 vLLM image (`jstarkg/vllm-gb10-flashnext:0.28-sm121-r6`) is
compiled for `sm_120`; the device is `sm_121`. It runs (minor-version
compatible) but vLLM logs:

> Your GPU does not have native support for FP4 computation […] Weight-only FP4
> compression will be used leveraging the Marlin kernel. This may degrade
> performance for compute-heavy workloads.

gx-fast still measures 72.8 tok/s, so this is a performance note rather than a
fault. A vLLM built for `sm_121a` would likely be faster.

## B-011 (S2) — RESOLVED 2026-09-16 — gx-reason GGUF produced garbage output on this llama.cpp build; tier re-engined onto vLLM
**Status: RESOLVED.** gx-reason serves correct output through the real
gateway for the first time. The replacement decided in
`coordination/DECISIONS.md` D-021 (`nvidia/Qwen3.6-27B-NVFP4` on vLLM, the
same image already proven for gx-fast) has been downloaded, deployed and
live-tested end to end on node 2.

**The evidence that closes this blocker — the original repro, inverted:**

```
# 2026-09-15, llama.cpp CUDA path (the bug):
prompt "The capital of France is" -> "////////////////////"

# 2026-09-16, vLLM + nvidia/Qwen3.6-27B-NVFP4, identical prompt,
# identical temperature 0, same /v1/completions endpoint, over the fabric:
prompt "The capital of France is" -> " Paris."          HTTP 200
```

Full A-E sequence, all passed 2026-09-16 (numbers in `TEST_RESULTS.md`):

* **A** — node 2's llama-swap publishes exactly one model, `gx-reason`, on
  the fabric (`192.168.100.11:28080`), auth enforced (an unauthenticated
  `/v1/models` is refused).
* **B** — cold start 401 s to first token (~225 s weight load, 177 s engine
  init/warmup). vLLM resolves `--quantization modelopt` to `modelopt_mixed`
  and correctly detects the checkpoint's mixed NVFP4/FP8/MXFP8 layers.
* **C** — the B-011 repro prompt above. Coherent.
* **D** — through the real LiteLLM gateway: a multi-step reasoning question
  answered correctly (the bat-and-ball problem: `$0.05`, not the `$0.10`
  trap), 1690 completion tokens of which 1468 were reasoning tokens,
  `reasoning_content` correctly separated from `content`, 12.4 tok/s.
* **E** — unload returns the memory: node 2 went from 70 GiB to 116 GiB
  MemAvailable within ~5 s of `POST /api/models/unload`.

**Why this is a real fix and not a lucky swap:** vLLM's logs show it
selecting the GDN linear-attention kernels for this checkpoint
(`Using Triton/FLA GDN prefill kernel`, `GDN decode kernel: cuda`) — the
same hybrid linear-attention/full-attention code path that llama.cpp got
wrong. The architecture is not the problem and never was; that specific
llama.cpp CUDA implementation of it is. That is exactly what D-021
predicted, so the diagnosis and the fix agree.

**Still true and still binding:** the GGUF/llama.cpp combination below is
confirmed broken and REJECTED for this tier — do not re-attempt it. The
three "remaining next steps" at the end of this entry (file an upstream
issue, try another quant, bisect llama.cpp) are now OPTIONAL upstream
citizenship, not blockers on this cluster: nothing here depends on
llama.cpp for gx-reason any more. gx-mini still uses that build and is
unaffected (it is dense, and never exercises the hybrid path).

**Original finding, preserved below for the record (still accurate: this is
why the GGUF/llama.cpp combination is rejected, not merely "not tried
again"):**

`unsloth/Qwen3.5-122B-A10B-GGUF` (UD-Q4_K_XL) loads cleanly on
`legenex/llama-cpp-spark:latest` (build b10948, commit 5f436dddb) — model and
`mmproj-F16.gguf` both load, no architecture warnings, health check passes, and
it generates at ~13.6 tok/s. **But every token is garbage.**

```
$ curl .../completion -d '{"prompt":"The capital of France is","n_predict":20,"temperature":0}'
{"content":"////////////////////", "tokens_predicted":20, "tokens_evaluated":5, ...}
```

The raw `/completion` endpoint fails identically to `/v1/chat/completions`, so
this is **not** a chat-template or `enable_thinking` problem — the compute graph
itself is producing garbage.

Memory is NOT the cause, and this is worth recording because it is the opposite
of B-009:

```
VmRSS:    99766300 kB
RssAnon:      2032 kB      <-- essentially nothing anonymous
RssFile:  99763920 kB      <-- the whole model is file-backed mmap
```

56 GiB stayed available with the full 77 GB model mapped. llama.cpp's mmap
behaviour is exactly what D-009 predicted; the engine choice was right, the
weights or the kernels are the problem.

**Leading hypothesis:** this llama.cpp build's CUDA path does not correctly
implement the `qwen3_5_moe` hybrid architecture (3× linear-attention/GDN layers
to 1× full-attention). gx-mini runs the same build correctly, but Qwen3.5-**4B**
is dense, so it never exercises the hybrid path.

**Update 2026-09-15, node 2 recovered — hypotheses 1 and 3 tested, both
narrowed, GPU-CUDA-kernel cause confirmed, still OPEN:**

* Ran the `--n-gpu-layers 0` GPU-vs-CPU comparison (`gx-reason-diagnose.sh`,
  fixed first — see CHANGELOG.md, it originally couldn't even start the
  CPU-only container: `legenex/llama-cpp-spark`'s `llama-server` is linked
  against `libcuda.so.1`, so the CDI device must stay attached even for a
  CPU-only run; `--n-gpu-layers 0` alone is what forces the compute path).
  Result: **GPU path GARBAGE (`////////////////////`, byte-identical to the
  original repro), CPU-only path SANE (`"The capital of France is Paris."`)
  with the exact same weights and sampling params.** This rules out the
  checkpoint/quant (hypothesis 2) — confirms the fault is in the CUDA
  execution path specifically, not the GGUF weights.
* Then rebuilt `legenex/llama-cpp-spark` from current upstream
  `llama.cpp` master (hypothesis 3) — `git clone
  https://github.com/ggml-org/llama.cpp` has no pinned commit, so this
  pulled whatever was HEAD as of 2026-09-15. Build succeeded cleanly
  (~140s). **Re-ran the identical GPU-vs-CPU comparison against the new
  binary: byte-identical result — GPU still GARBAGE, CPU still SANE.**
  Old image kept as a rollback tag,
  `legenex/llama-cpp-spark:pre-b011-fix-backup`, on node 2 only (this was a
  local rebuild, nothing was pushed to any registry). No functional
  regression found — the new binary loads, serves and shuts down
  identically, just with the same wrong GPU output.

**Conclusion: hypothesis 3 (stale build) is ruled out.** The bug is real,
reproducible, isolated to the CUDA/GDN kernel execution path for this
architecture, and is either (a) still present in current upstream
`llama.cpp` for `qwen3_5_moe` on `sm_121`/GB10, or (b) specific to
something about this exact hardware/driver combination
(580.173.02 / CUDA 13.0/13.1) that upstream's own test matrix does not
cover. **B-011 remains OPEN.** gx-reason cannot be served correctly on GPU
today; running it CPU-only is not a real fix (violates the "no accidental
CPU execution" requirement, and a 122B-class MoE model on CPU is far too
slow to be a usable tier) and was not deployed as a workaround.

**Remaining next steps, each a real piece of work, not attempted this
session:**
1. File or search an upstream `ggml-org/llama.cpp` issue for `qwen3_5_moe`
   CUDA/GDN kernel correctness on `sm_121` (Blackwell/GB10) — needs
   internet research and likely a minimal repro, not just this cluster's
   context.
2. Try a different quant — `unsloth/Qwen3.5-122B-A10B-GGUF` `UD-IQ4_XS`
   (60.2 GB) or `bartowski/Qwen_Qwen3.5-122B-A10B-GGUF`. This means
   downloading a new multi-GB checkpoint to node 2 — a real disk/bandwidth
   commitment a human should sign off on before it starts, not something
   to launch unilaterally mid-session.
3. Bisect llama.cpp history between a commit known to predate the
   `qwen3_5_moe` hybrid-attention implementation and current master, to
   find the exact change that introduced or never fixed this — slow, but
   would turn "GPU path is broken" into an actionable upstream bug report.

## B-012 (S2) — node 2 was wedged by running two 77 GB models at once
**Status: RESOLVED, admission control now deployed on both nodes.** Node 2
was physically power-cycled and is confirmed clean (`recover-node2.sh`,
16 PASS / 0 FAIL / 0 WARN, 2026-09-15) — see CURRENT_STATE.md. The
resource-ownership/admission-control layer (see below) now runs on node 2
too, not node 1 only — deployed and independently verified there later the
same day (2026-09-15): a normal-sized launch is correctly admitted against
node 2's own real `/proc/meminfo` and its own local lock file, and a
deliberately oversized synthetic launch is correctly refused. See the
"Update 2026-09-15" paragraph below for the deployment details.

While gx-reason (77 GB mmap) was loaded, a second llama.cpp container was
started on node 2 to run a CPU-only comparison — another 77 GB mmap on a 121 GiB
node. The node went into sustained mmap thrashing: userspace stopped responding
(SSH over both Tailscale **and** the fabric hung, llama-swap stopped answering)
while the kernel stayed alive (ICMP on 192.168.100.11 kept replying with 0% loss
and sub-millisecond RTT).

Because mmap pages are reclaimable, the OOM killer does not necessarily fire —
the node can thrash indefinitely rather than shedding load.

**Rule: never run two large models on the same node, even briefly, even for a
diagnostic.** Unload the resident model through llama-swap first:

```
curl -X POST http://192.168.100.11:28080/api/models/unload -H "Authorization: Bearer $GX_SWAP_API_KEY"
```

**Useful diagnostic note:** ICMP on the ConnectX rail is a good liveness signal
that distinguishes "node is dead" from "node is alive but userspace is starved".

**Repair, shipped 2026-09-14:** a resource-ownership/admission-control layer
(`ARCHITECTURE.md` §9) makes this exact shape of double-large-load
structurally refused on node 1, proven by concurrency tests (5 real racing
processes, SIGKILL-recovery, a launch that would violate the 30 GiB reserve
never runs the caller's command). `legenex/lifecycle/gx-safe-run.sh` is the
sanctioned replacement for the bare `docker run` that caused this incident.
**Update 2026-09-15, later the same day:** the line above ("no ledger/lock
module deployed") is now stale — closed this session. `legenex/lifecycle/`
and `legenex/orchestrator/` (the `resource_guard.py` admission module plus
the bash `resource-guard.sh`/`gx-safe-run.sh` wrappers) were rsynced to node
2 at the same relative path, and independently verified working against
node 2's own real `/proc/meminfo` and its own local flock file: a small
admission check was admitted correctly, and a deliberately oversized
(500 GiB) synthetic launch through `gx-safe-run.sh` was correctly refused
(exit 2) without ever running the wrapped command. `gx-hostwatch.sh` was
also installed as a `systemctl --user` timer on node 2 (lingering is enabled
there) and confirmed running a real check cycle every 60s, logging to
`/srv/logs/gx-hostwatch.log` on node 2. This closes the "not yet true of
node 2" gap for the admission-control/lock half of B-012's repair.
`gx-max-start.sh`'s rank1 launch itself still uses its original real remote
`flock` convention (unchanged, still correct) rather than having been
rewritten to call through the newly-deployed module — that rewrite is a
separate, not-yet-done piece of work, distinct from "the module isn't there
at all."

**Remaining gap, found and partially closed 2026-09-14:** even with the
admission guard, if node 2 hangs mid-acquisition *after* rank0/rank1 already
started (the realistic version of tonight's timing), the rank(s) were not
being automatically torn down — `GxMaxLifecycle._do_acquire()` only recorded
the error and set state=DOWN. Fixed: it now runs a best-effort
`gx-max-stop.sh --force` before reporting the failure. `n2()` in `lib.sh`
still only bounds the TCP-connect phase (`ssh -o ConnectTimeout=10`), not a
stuck banner exchange — `legenex/scripts/recover-node2.sh` and
`gx-max-validate.sh` wrap their own SSH calls in a hard `timeout` for this
reason, but `lib.sh`'s `n2()` itself was not changed (touching it affects
every node2-touching call in the lifecycle scripts and deserved a dedicated,
careful pass rather than a rushed one at the end of this session).

**Post-recovery forensics, 2026-09-15, from node 2's boot -1 journal (captured
before rotation, raw excerpt in operator's session scratch space — not
committed to the repo, since it is a full boot log with no redaction pass):**
confirms and sharpens the timeline, does not overturn the diagnosis above.

* `gx-llama-swap-node02` (container `2b6e54e2...`, running continuously
  since before the incident and still running today under the same
  container ID) started failing its own Docker health check at **20:00:54**
  SAST — `"timed out starting health check"`.
* The local GNOME/X11 session (`gdm-x-session`, unrelated to any model
  container) then failed to allocate a GPU 2D engine context twice —
  **20:01:10** and **20:04:55–20:05:00** — with the kernel logging
  `NV_ERR_NO_MEMORY` from `_memdescAllocInternal` /
  `kernel_graphics_context.c`. This is a **desktop-session GPU allocation
  failure**, not a model process's allocation failing — but on a unified-
  memory GB10 it is consistent with the same global exhaustion: once host
  RAM is saturated by two 77 GB mmap'd models, any new GPU context
  (including the display compositor's) can fail to allocate too. It does not
  by itself prove the GPU allocator was the trigger rather than a downstream
  symptom of the host-memory exhaustion already documented above.
* Direct evidence of the userspace stall itself: a dockerd log line
  timestamped internally at `20:03:56.443` was not actually written to the
  journal until **20:05:33** — a 97-second delivery delay on the local
  logging pipe, the same class of stall B-012 already describes for SSH and
  llama-swap's own HTTP port.
* **Could not identify the exact two model containers**: the two 77 GB
  llama.cpp containers active during the incident window (candidates:
  `e28812e3...`, `b1199982...`, `8b0bc402...`, started 19:57–19:59) were
  already removed by Docker (`docker inspect` now returns "no such object"
  for all three) before this recovery session began, so their image/name
  cannot be recovered post hoc. Only the always-on `gx-llama-swap-node02`
  gateway container survived with an inspectable history.

**Conclusion:** this is corroborating evidence for the existing B-012
diagnosis (host-memory exhaustion from two large mmap'd models causing
userspace starvation), not a new or different root cause. Treat any claim
that this is a distinct "NVIDIA/unified-memory exhaustion" root cause
requiring an architecture change as unverified — the repair already shipped
(admission control, 30 GiB reserve, node lock) addresses the actual
mechanism observed here.

## B-013 (S2) — cannot push; no writable remote is configured
**Needs:** a human to add a remote this account can write to.

All work is committed locally on `legenex-dual-gx10` (8 commits ahead of
`origin/main`). Pushing is not possible:

```
$ git push --dry-run origin legenex-dual-gx10
remote: Permission to mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama.git denied to legenex.
fatal: ... The requested URL returned error: 403
```

The only configured remote is the **upstream community repo**, owned by another
account. The backup remote named in CLAUDE.md
(`gitea.martin-bierschenk.de/...`) is **not configured** here at all.

Nothing was force-pushed and no remote was modified. To publish, add a fork or
the gitea backup as a remote, e.g.:

```
git remote add legenex <your-writable-repo-url>
git push -u legenex legenex-dual-gx10
```

Credentials must come from a credential helper or SSH key — never put a token
in the remote URL.

## B-014 (S3) — hardware watchdog exists but is unarmed
**Needs:** a human with sudo. Investigated 2026-09-14, not applied — arming
an automatic-reboot mechanism is a hardware-safety decision, not a routine
engineering one.

`/dev/watchdog`/`/dev/watchdog0` exist (SBSA Generic Watchdog, 10s default
timeout) but `RuntimeWatchdogSec` is commented out in
`/etc/systemd/system.conf` and `RuntimeWatchdogUSec=0` confirms it live. No
NVIDIA/ASUS daemon arms it either. Arming it would have automatically
rebooted node 2 during tonight's B-012 incident instead of requiring a
physical power cycle.

**Suggested fix (not applied — needs root, and needs deciding on both
nodes):**
```
# /etc/systemd/system.conf.d/watchdog.conf
[Manager]
RuntimeWatchdogSec=30s
RebootWatchdogSec=10min
```
then `sudo systemctl daemon-reexec` (system.conf needs a PID1 re-exec, not
just a daemon-reload) or a reboot. Verify with
`systemctl show -p RuntimeWatchdogUSec` (expect `30000000`) and `wdctl`.

**Caveats a human should weigh before applying:** this forces an
uncontrolled hard reset with no graceful container/model shutdown if it
trips; the timeout must be chosen consciously against this host's own
GUI/remote-desktop session and gx-max's long cold-start; must be configured
on both nodes to help node 2's failure mode specifically, and node 2 is
unreachable to even attempt it right now.

## B-015 (S3) — sshd/tailscaled/systemd/NetworkManager cannot be OOM-protected without root
**Needs:** a human with sudo, if this protection is wanted beyond the
indirect mitigation already shipped.

Confirmed by inspection 2026-09-14, not assumed: these services' cgroups
(`/sys/fs/cgroup/system.slice/{ssh,tailscaled,NetworkManager}.service/memory.max`)
are `root:root 644` — unwritable by this account, and `systemctl show`
confirms `OOMScoreAdjust=0` on all of them, unmodifiable without a privileged
`systemctl set-property` or editing the system unit.

**Mitigation already shipped, not equivalent to direct protection:** every
model container now carries a positive `--oom-score-adj` (700-950) so the
kernel's OOM killer picks them over these daemons (which sit at the default
0) in an actual memory-pressure event — without ever touching the daemons
themselves. This helps but does not protect against pure page-cache/mmap
thrashing the way B-012 actually manifested (userspace-starved, not
OOM-killed) — that class of failure is addressed by the admission-control
layer (B-012 repair, above) preventing the double-load in the first place,
plus the B-014 hardware watchdog as a last resort once armed.

## B-016 (S3) — no remote power-cycle path exists for either node
**Needs:** a decision on whether to add one; no fix applied.

Confirmed during the ChatGPT project-file integration (2026-09-15, source:
the operator's own investigation while node 2 was down for B-012): a full TCP
scan of node 2 during its B-012 wedge showed only ports 22 (SSH, accepting
but not completing a banner) and 3389 (RDP, accepting but not negotiating)
open. **No usable BMC, IPMI, Redfish, or MCTP management path was found on
either GX10.** ConnectX does not provide a motherboard-level reset channel.

**Consequence:** the only recovery path from a B-012-style wedge is a human
physically pressing the power button. `recover-node2.sh` explicitly does not
attempt a remote reboot for this reason (see its own header comment). If
node 2 wedges again while unattended (e.g. overnight, while travelling), it
stays down until someone is physically present — the B-014 hardware watchdog,
if armed, is the only automatic mitigation for that scenario.

**Related, separate finding (GX10-02, not this incident):** GDM automatic
login was previously enabled on gx10-02, which could leave a stale local
graphical (seat0) session colliding with remote RDP login attempts. It has
been disabled (`AutomaticLoginEnable = false` in the GDM config) — see
`OPERATIONS.md` for the recovery procedure if a stale session recurs. This
does not affect SSH.

## B-017 (S1) — SUPERSEDED 2026-09-16 by B-022 — gx-max cannot acquire through the real orchestrator: the 30 GiB admission-guard reserve floor and gx-max's own ~90 GiB/rank footprint are structurally incompatible

> **Superseded.** B-017 correctly identified the collision but was closed
> (D-020) by picking a gx-max-only reserve of 5 GiB with no measurements
> behind it. The measurements now exist — four real two-node runs on
> 2026-09-16 — and they change the picture: the collision is real, it cannot
> be tuned away, and the right reserve is a human decision, not a number an
> agent picks to make an admission check pass. See **B-022** and
> `coordination/DECISIONS.md` **D-022**. The deadlock B-017 described (the
> guard refusing every launch) is still the current behaviour, and it is now
> deliberate rather than accidental.
**Status, 2026-09-15: RESOLVED — option 1 applied (D-020).** gx-max's own
admission check now uses a smaller, explicit `GXMAX_GUARD_RESERVE_GIB`
(5 GiB) instead of the generic 30 GiB floor, scoped to only its own two
rank launches (`gx-max-start.sh`) -- every other tier keeps the 30 GiB
floor unchanged. Verified live: `gx-max-validate.sh` got past this exact
refusal for the first time ever through the real orchestrator, and rank1
+ rank0 both passed admission and started. **This did not fully validate
gx-max** -- it surfaced a second, distinct issue (rank0 OOM-killed during
weight loading, then an orphaned rank1 wedged node2) tracked as B-020,
which is what actually blocks final gx-max sign-off now, not this reserve
collision. See D-020 for the full resolution record. The original
analysis is kept below for the record.

**First real test, 2026-09-15.** `legenex/tests/gx-max-validate.sh` — which
drives acquisition through the actual production path
(`POST /lifecycle/gx-max/acquire` on the real orchestrator), not by calling
`gx-max-start.sh` directly — had never been run to completion before. Doing
so surfaced two real, previously-undiscovered issues, both now understood:

**1. Fixed this session:** the fabric-rail preflight check used `ping`,
which needs `CAP_NET_RAW`. `gx-orchestrator.service` runs with
`NoNewPrivileges=true` (correct, intentional hardening for a control-plane
service — see its unit file comment), which blocks a process from gaining
*any* capability via exec, file capabilities included, and this host's
`net.ipv4.ping_group_range` is empty so there is no unprivileged-ping
fallback either. Confirmed empirically: `systemd-run -p NoNewPrivileges=true
ping ...` fails immediately with `socket: Operation not permitted`. This
meant `gx-max-start.sh` could **never** succeed when invoked through its
real, locked, production entry point — only when run directly from an
interactive shell, which is almost certainly how every previous "verified
working" gx-max run (including the worker's own W-1 validation on
2026-09-14, `~/gx-worker/run-gx-max.sh` on node 2) was actually launched.
Fixed in both `gx-max-start.sh` and `gx-max-validate.sh`: replaced the
`ping` check with a capability-free TCP connect probe (a fast "Connection
refused" proves the peer answered; a real timeout means unreachable).

**2. NOT fixed, genuinely blocked:** with the fabric check passing, the next
attempt reached the resource-ownership admission guard
(`legenex/orchestrator/gx_orchestrator/resource_guard.py`, shipped
2026-09-14 as the B-012 repair) and was **correctly, hard-refused**:

```
node1 admission guard REFUSED gx-max-rank0:
  live MemAvailable 113.8 GiB - new 90.0 GiB leaves 23.8 GiB,
  below the 30.0 GiB reserve floor
```

This is not a bug in the guard's arithmetic — it is a genuine, documented,
**unresolved collision between two separately-locked decisions**:

* L-6 / `gx-max.conf`: gx-max is *locked* to take over each node almost
  entirely (~90–93 GiB/rank on a ~121 GiB node), and `gx-max.conf`'s own
  comment (written 2026-09-14, the same day the guard shipped) says so
  explicitly: *"Why gx-max is NOT capped down to leave the same 30 GiB
  reserve as the other tiers: gx-max is DOCUMENTED and LOCKED to take over
  the node... Capping it at ~91 GiB to force a 30 GiB floor would make it
  OOM-kill itself under its own verified-working footprint... If the 30 GiB
  floor must hold even during a gx-max run, that is an
  admission-control/architecture decision for a human, not something
  encoded here as a silent tightening."*
* The B-012 admission guard enforces a uniform 30 GiB reserve floor for
  every `exclusive`-class workload, gx-max included, with no
  per-tier override.

The comment already named the exact decision this needs, before this
session ever ran into it live. I am not making that call unilaterally in
either direction: loosening the guard for gx-max risks silently
reintroducing a B-012-shaped hole; leaving it as-is means gx-max, a locked,
flagship, explicitly-required tier, can **never** acquire through its real
production path, only by bypassing the orchestrator entirely (which defeats
the point of the orchestrator's serialisation/state-machine guarantees and
is not how a real client request would reach it).

**Cleanup verified correct on both nodes after the refusal** — this is the
never-downgrade rule and the admission guard working exactly as designed,
not a failure of either: `gx-max post-failure cleanup: both ranks stopped,
ledger released`, orchestrator state `acquiring -> down`, `POST
.../release` 200, node1 back to 113 GiB available, node2 back to 115 GiB
available, no orphaned containers, no stuck locks. The drain step correctly
stopped `gx-comfyui`/`gx-media-router`/`gx-llama-swap-node02` first (see the
`gx-max-start.sh` `CONFLICTS_N2` fix, same session) before the guard was
even reached.

**Options for the human to choose between** (none applied):
1. Give gx-max's own admission check a smaller, explicitly-documented
   reserve (e.g. 3–5 GiB, enough to keep sshd/the orchestrator/docker
   itself alive, not 30) instead of the generic tier floor — the most
   direct read of what the `gx-max.conf` comment was already asking for.
2. Lower `GXMAX_RANK_ESTIMATED_GIB` (currently 90) if there is real
   evidence the true footprint is smaller than documented — not attempted;
   the current figure is consistent with `gx-max.conf`'s own "~93 GiB"
   note and D-013's measured 95 GiB figure for the (broken) gx-reason
   process, so shrinking it without new measurement would be guessing.
3. Accept that gx-max is acquired by a human running `gx-max-start.sh`
   directly (bypassing the orchestrator's queueing/state-machine), and
   have the orchestrator's `/lifecycle/gx-max/acquire` remain permanently
   unable to serve a real request — a real, user-visible product gap for
   the `gx-max`/`gx-auto` aliases documented in ARCHITECTURE.md.

## B-018 (S2) — ComfyUI's compose-based start bypasses the resource-ownership admission guard entirely

**Update 2026-09-17 (partial mitigation):** media router 2.2.0 now performs its own memory admission from `/proc/meminfo`, calibrated to measured footprints (see D-031 amendments). gx-reason's start first asks the router to free ComfyUI. ComfyUI's compose start still bypasses the resource guard's ledger.

**Needs:** a decision on wiring `docker-compose.media.yml`'s `comfyui` service
through the admission system, or an accepted convention (mirroring the
gx-reason/gx-comfyui manual-unload discipline already documented in
`legenex/media/README.md`).

**Found 2026-09-15, running the acceptance suite end to end.** `t_reason`
loads gx-reason (~95 GiB), then `t_auto`'s routing check reloads it again
after `t_reason`'s own test finished, then `t_media` starts ComfyUI via a
plain `docker compose up -d` — which, unlike `gx-safe-run.sh`/`gx_guard_run`,
never calls the resource-ownership admission guard at all. Measured result:
node 2 dropped to **~10 GiB available**, well under the 30 GiB reserve
floor, with nothing in the launch path positioned to refuse it — this is
the same *shape* of gap B-012's admission control was built to close,
just via a legitimate `docker compose` start instead of a bare `docker run`.

**Mitigated, not fixed, this session:** `legenex/tests/acceptance.sh`'s
`t_reason` now unloads gx-reason after its own test, and `t_media` now
independently re-checks/unloads gx-reason immediately before starting
(belt-and-suspenders, since either test alone leaving it loaded can bite
the other). This closes the gap for the acceptance suite's own run order,
not for the general case — a human or agent starting
`docker-compose.media.yml` by hand while gx-reason happens to be loaded is
still only protected by the documented convention in `media/README.md`
("Memory interlock — read before starting anything"), not by anything
enforced in code.

**Real fix, not attempted:** either (a) wrap the ComfyUI service start in
`gx-safe-run.sh`/`gx_guard_run` the same way gx-max's ranks are, so a
launch while gx-reason is resident is structurally refused rather than
merely documented, or (b) accept the current manual-discipline convention
as sufficient for media (lower risk than gx-max, since ComfyUI's failure
mode observed here was "admission floor breached", not "node wedged" —
mmap thrashing specifically requires two *mmap'd* large processes, and
ComfyUI's weights are not mmap'd the way llama.cpp's are).

## B-019 (S2) — gx-max's node2 admission check is not atomic with the actual rank1 launch (TOCTOU)
**Status: narrower mitigation applied 2026-09-15; the fuller fix (ledger on
node2) is still not done, tracked separately under B-012's "remaining
gap."**

**Found 2026-09-15 by an independent reviewer agent, verified by inspection
(not yet reproduced live — this is a narrow timing window, not something
that reliably reproduces on demand).** `gx-max-start.sh`'s node2 admission
check (`gx_guard_check node2 gx-max-rank1 exclusive ...`, using a `/proc/meminfo`
snapshot fetched over SSH) and the actual rank1 `docker run` are **not**
in the same critical section:

* **node1's rank0** is correctly atomic: `gx_guard_run` (the sanctioned
  path) holds node1's lock across check → launch → register as one
  critical section.
* **node2's rank1** is not: the admission check runs once, unlocked, then
  later the actual launch acquires only an ad hoc `flock` on
  `$HOME/.gx-guard/node2.lock` on node2 itself — a different lock domain,
  not backed by the residency ledger the check just consulted (this gap is
  already noted in B-012's "remaining gap" text and in the script's own
  comments, but its TOCTOU consequence hadn't been named until now).

**Consequence:** in the window between the check and the launch, any other
memory-affecting operation on node2 — a manually-started `docker compose up`
for ComfyUI (B-018), a routed request respawning `gx-reason` before
`gx-llama-swap-node02` is confirmed stopped, a second concurrent gx-max
attempt racing past this same window — is invisible to this check and not
refused. This is distinct from B-018: B-018 is "ComfyUI's own start path
has no guard at all"; B-019 is "gx-max's *own* guard can go stale before
its *own* launch."

**Mitigation applied:** rather than restructure the whole check-then-launch
sequence (which needs the ledger deployed to node2 to do properly -- a
bigger change, still not done), added a second, narrower check: an atomic
re-validation of raw `MemAvailable` against `GXMAX_RANK_ESTIMATED_GIB +
GX_GUARD_RESERVE_GIB`, computed to a literal number on node1 and executed
*inside* the same held `flock`, immediately before the `docker run`. This
closes the most dangerous part of the window (something else consuming
node2's memory between the unlocked admission check and the lock being
acquired for the actual launch) without needing the full ledger. It
re-validates raw headroom, not the fuller ledger-aware residency
accounting, so it is a mitigation, not the complete fix the original
finding named.

**Caught a real bug in this mitigation itself before shipping it:** the
first version embedded `$((GXMAX_RANK_ESTIMATED_GIB + GX_GUARD_RESERVE_GIB))`
literally inside the remote command string -- but that arithmetic would
have run on node2's shell, which has never heard of those node1-local
variable names and treats undefined names as 0 in arithmetic context,
so the check would have silently always passed. Fixed by computing the
threshold to a literal number on node1 before building the remote command
string. Verified live against the real node2 (not just unit tests): with
the threshold set below current MemAvailable it proceeds; with it set
above, it correctly refuses with the exact "REFUSED: node2 MemAvailable=…"
message and exit code 9, all inside the held lock. Given the current
GXMAX_RANK_ESTIMATED_GIB=90 and GX_GUARD_RESERVE_GIB=30, this check will
refuse on today's node2 (113 GiB < 120 GiB needed) exactly like the earlier
Python admission guard already does -- consistent with, not a new
consequence of, B-017.

**Also flagged by the same review, S3, lower priority:**
* `drain_node2()` uses a uniform `docker stop -t 60` for every container,
  including `gx-reason`, whose own `node02.yaml` `cmdStop` documents a
  200s graceful-shutdown budget. Draining via gx-max-start.sh gives it a
  third of that before SIGKILL — not a memory-safety risk (SIGKILL still
  reclaims), but undercuts the "let in-flight work finish" intent.
* `gx-orchestrator.service`'s retry-on-bind-failure behavior (D-019) relies
  on systemd's *default* `StartLimitBurst`/`StartLimitIntervalSec` being
  wide enough — works today, but is incidental rather than guaranteed.
  Consider an explicit `StartLimitIntervalSec=0` for a hard guarantee.

## B-020 (S1) — RESOLVED, and the orphan-rank failure mode is now FIXED AND TESTED (2026-09-16)

> **Update 2026-09-16 — the fix, and what it is verified against.**
>
> B-020's root cause was structural, not incidental: *every* cleanup path for
> a failed gx-max launch had to reach node 2 over ssh, and node 2 is exactly
> the host being starved at that moment. The unwind's single ssh timed out,
> the orphan held ~90 GiB for 80 minutes, and the kernel eventually reclaimed
> it. Three things now close that hole:
>
> 1. **`legenex/lifecycle/rank1-deadman.sh`** — a watchdog that runs ON
>    node 2, armed before rank0 is even started, already resident (a few
>    hundred KiB of bash, no allocation in its loop) before any pressure
>    appears. It watches rank0's liveness by TCP-connecting to the
>    torch-distributed bootstrap store on the RoCE fabric — the one socket
>    only rank0 binds, for the engine's whole life — and force-removes rank1
>    itself when rank0 is gone. It needs nothing from outside the host.
> 2. **`legenex/lifecycle/gx-max-unwind.sh`** — a dedicated failure path,
>    separate from the graceful `gx-max-stop.sh`. It never drains, retries
>    node 2 with bounded backoff instead of giving up on one timed-out ssh,
>    *confirms* both ranks are gone rather than trusting a stop that returned
>    0, reconciles both ledgers, proves both locks are free, and verifies
>    memory return, swap behaviour and SSH/Tailscale/fabric health per node.
> 3. **An EXIT trap in `gx-max-start.sh`** so the unwind cannot be missed: not
>    by the rank-died path, not by the readiness timeout, not by `set -e`, not
>    by SIGINT/SIGTERM.
>
> **Verified, not asserted.** Deadman: fires when rank0 never appears
> (20 s startup grace, container removed, node-2 llama-swap restored); fires
> 10 s after a rank0 that *had* been seen disappears; fires on its own memory
> floor. Unwind: exercised on a synthetic two-rank failure and on **two real
> DeepSeek launches**, reporting `UNWIND COMPLETE — cluster verified clean`
> with all 11 checks green, both ranks confirmed gone, and MemAvailable back
> to 115 GiB / 117 GiB. In one of those real runs the deadman fired first, on
> node 2, at 1 GiB MemAvailable — the exact B-020 trigger condition — and the
> node came straight back to 117 GiB instead of being held for 80 minutes.
>
> **Two bugs this testing found in the tooling itself**, both now fixed:
> `pkill -f rank1-deadman.sh` matched the *ssh remote command line* that was
> starting the deadman, so the remote shell killed itself before arming
> anything (now a pid file); and `docker inspect` on a missing container
> writes a blank line to stdout before failing, so `... || echo absent`
> yielded `"\nabsent"` and the unwind reported a still-running rank0 that did
> not exist (now normalised).
>
> **Corrected recovery guidance for this failure class** — do NOT open with
> "a human must go to the machine":
>
> 1. Record the time. Do not dispatch anyone yet.
> 2. Re-probe SSH and Tailscale periodically (every ~30 s is plenty).
> 3. Also probe the **ConnectX fabric** (`192.168.100.11`,
>    `192.168.101.11`). Measured 2026-09-16: during a node-2 wedge the
>    fabric answers ICMP *and* accepts TCP on port 22 while Tailscale is
>    completely dark — so the fabric is the better liveness signal. SSH over
>    it still fails at "timed out during banner exchange", which is the
>    B-012 signature (kernel alive, userspace starved) and is itself
>    diagnostic.
> 4. Give the kernel/OOM path time. The documented escalation window is
>    **90 minutes** from the start of the wedge; B-020 itself resolved in 80.
> 5. When SSH answers, run `legenex/scripts/recover-node2.sh`.
> 6. Restart node 2's control plane if the drain left it down
>    (`RECOVERY.md` §4).
> 7. Only if the host has not recovered inside the escalation window is
>    physical intervention warranted.
>
> **B-016 is unchanged**: there is still no BMC/IPMI/Redfish remote power
> path on either node. Nothing here claims a remote power-cycle capability.

### Original record (2026-09-15/16)

**Status, 2026-09-16 07:34-07:47 CEST: RESOLVED, and the original
prescription was WRONG.** Node 2 is healthy and back in service. No human
ever power-cycled it, and none needed to.

**Correction — how it actually ended (evidence, collected 2026-09-16
07:41-07:46 from node 2 itself):**

* `uptime` on gx10-02 reports **22h07m at 07:41, i.e. boot at 2026-09-15
  09:33** — more than 13 hours *before* the incident began at 23:24. The
  node was never rebooted, never power-cycled, and never lost power. The
  kernel that was running when it wedged is the one running now.
* `docker inspect gx-max-rank1` gives the mechanism:
  `StartedAt=2026-09-15T21:24:47Z`, `FinishedAt=2026-09-15T22:44:48Z`,
  `ExitCode=1`, **`OOMKilled=true`**. The orphaned rank1 held the node for
  **80 minutes** and was then killed by the kernel's OOM killer, against
  its own `--memory 106g` cgroup cap. Its final log line is the expected
  distributed-job error (`Rank 0 scheduler died during initialization`),
  i.e. it died still waiting for the rank0 that had already been killed on
  node 1.
* Once that container died, node 2's userspace un-starved on its own. By
  the time this was checked (07:34) `recover-node2.sh` passed 14/14
  substantive checks with 116 GiB available, and SSH answered its banner in
  4 ms.
* `gx-llama-swap-node02` had exited cleanly (`ExitCode=0`,
  `OOMKilled=false`) at `21:24:46Z` — **one second before rank1 started**.
  That was the gx-max drain doing its job, not a casualty of the incident.
  It stayed down afterwards only because `docker stop` suppresses
  `restart: unless-stopped`; a human ran the documented `docker compose
  up -d` (RECOVERY.md §4) at 07:35:10 and it came back healthy on both
  loopback and the fabric.

**What this changes for the playbook (the operationally important part):**
the B-012/B-020 failure shape — fabric/ICMP alive, SSH banner starved by a
single huge resident container — **is not necessarily terminal, and does
not automatically require a human at the machine.** The kernel's OOM killer
did eventually reclaim the node, because the offending workload was
correctly capped (`--memory`) and correctly biased for sacrifice
(`--oom-score-adj 950`) — the host-resilience design from B-012 worked, it
just took 80 minutes rather than seconds. Before dispatching a human, wait
out at least that long and re-probe. B-016 (no *remote* power-cycle path
exists) is unchanged and still true; what is now known is that this
particular failure may not need one.

**Not to be over-read:** there is no evidence about what node 2's userspace
was doing minute-by-minute during those 80 minutes (nothing could reach it
to observe), and one self-recovery is not a guarantee of the next. A
workload that is *not* memory-capped, or that starves the node without
tripping a cgroup limit, could still wedge it terminally. The original
B-012 incident is the precedent for that shape.

**On the leftover `gx-max-rank1` container:** the exited container is
deliberately left in place on node 2 as the forensic record of this incident
(it is where `OOMKilled=true` and the 80-minute window are readable). It
holds no memory, and it cannot block a future launch: `gx-max-start.sh`
already runs `docker rm -f` on both rank names before starting either
(lines 126-127). The stale node-1 ledger entry for it HAS been cleared, via
the sanctioned `resource-guard.sh` release path — which reconciled it
automatically on read, incidentally re-proving the reconciliation design.

**Original prescription, preserved for the record (it said a physical
power cycle was required; that turned out to be unnecessary):**

**Sequence, 2026-09-15, ~23:24-23:41 CEST, immediately after the B-017 fix
(D-020) was applied and verified:** `legenex/tests/gx-max-validate.sh
--cleanup-on-exit` was run through the real orchestrator to validate the
fix.

1. Admission passed on both nodes for the first time ever through the real
   orchestrator (see D-020) — rank1 started on node2, then rank0 started on
   node1 (`ledger += gx-max-rank0`, "admitted: 95.0GiB projected of
   121.0GiB node total; MemAvailable leaves 20.0GiB (reserve floor
   5.0GiB)").
2. ~20s later, rank0's own log shows: `RuntimeError: Rank 0 scheduler died
   during initialization (exit code: -9). If exit code is -9 (SIGKILL), a
   common cause is the OS OOM killer.` — a real kernel OOM-kill during
   weight loading, not a code bug. `gx-max-rank0`'s `--oom-score-adj 950`
   (highest in the stack) means the kernel correctly chose to sacrifice it
   over any host daemon — the host-resilience design worked as intended,
   it just wasn't enough margin to let the launch itself succeed.
3. `_do_acquire()`'s best-effort cleanup then tried to reach node2 to stop
   the now-orphaned rank1 and hung: `ssh: connect to host 100.73.238.4
   port 22: Connection timed out` (`100.73.238.4` is node2's Tailscale
   address — the correct SSH endpoint per `CURRENT_STATE.md`; this is a
   real reachability failure, not a wrong-address bug). Cleanup could not
   complete; rank1 (~90-95 GiB) was very likely left resident on node2 with
   nothing tearing it down.
4. Node 2 has been unreachable ever since, with the EXACT B-012 signature,
   independently re-confirmed by three different probes so this is not a
   single flaky check:
   - Both fabric rails (`192.168.100.11`, `192.168.101.11`) answer ICMP
     instantly, 0% loss — the kernel is alive.
   - A raw TCP connect to `192.168.100.11:22` completes (SYN/ACK) — the
     network stack is answering.
   - But `ssh legenex-02@gx10-02` (Tailscale) times out before the TCP
     handshake even completes, and a direct SSH attempt to the fabric IP
     gets **"Connection timed out during banner exchange"** — TCP connects
     but sshd cannot complete the banner. That is userspace starvation,
     not a dead host, exactly B-012's documented signature.
   - `legenex/scripts/recover-node2.sh` (report-only, safe) was run and
     independently confirms the same: PASS on ICMP, FAIL on every SSH-
     dependent check, with the identical `100.73.238.4` timeout.
5. Node 1 was confirmed fully clean and unaffected: no stray gx-max
   container, memory back to 115 GiB available, orchestrator reports
   `gx_max.state: down`, and the residency ledger's stale `gx-max-rank0`
   entry was reconciled away automatically on the next `resource-guard.sh
   status node1` call (proving the reconciliation-on-read design works,
   not just the happy path). **Node 1's own state is not the concern here
   — node 2's physical availability is.**

**Root cause, best available diagnosis without node2 access:** almost
certainly a repeat of B-012's exact mechanism (sustained memory/mmap
pressure from a large resident model starves userspace faster than it OOM-
kills), triggered this time by rank1 being left running, unsupervised,
alone (its TP=2 peer already dead), likely retrying its distributed
bootstrap connection to a rank0 that no longer exists — plausible
additional CPU/memory churn on top of the base ~90-95 GiB residency. This
is a real, live incident, not a hypothesis to re-verify remotely: there is
nothing further to learn or fix by SSH/ping alone, and B-016 already
establishes there is no remote recovery path for this exact shape of
failure.

**What was done in response, all on node1 / in the repo (nothing required
node2 access):**
- `GXMAX_RANK_ESTIMATED_GIB` raised 90 -> 95 GiB (`gx-max-start.sh`,
  `resource_guard.py`) so future admission checks reflect the documented
  ~93-95 GiB measured working set rather than the more optimistic 90 GiB
  figure that just proved insufficient once (see D-020's follow-up note).
  This does not guarantee a future launch cannot still OOM — gx-max is
  locked to run at the very edge of a 121 GiB node's capacity by design
  (L-6) — it makes the guard's own arithmetic more honest about how little
  slack really exists.
- Confirmed node1's own ledger/lock/memory state is fully clean (see point
  5 above) so this incident does not block any node1-only work.

**What a human needs to do, in order:**
1. Physically power-cycle gx10-02 (no remote path exists — B-016).
2. Run `legenex/scripts/recover-node2.sh` (report-only, safe) to confirm a
   clean recovery, exactly as after the original B-012 incident.
3. Check for and force-remove any orphaned `gx-max-rank1` container and
   clear `legenex/lifecycle/.state/node2-residency.json`'s best-effort
   entry on node1 if still present (it is advisory bookkeeping only, not
   itself dangerous, but should not be left stale).
4. Once node2 is confirmed clean, this repo already has ready-to-deploy,
   not-yet-tested work waiting on it: the gx-reason replacement (B-011/
   D-021) needs its checkpoint downloaded and the A-E test sequence run,
   and `gx-max-validate.sh` should be re-run to see whether the 90->95 GiB
   estimate change is sufficient or whether gx-max needs a human decision
   on a different fix entirely (e.g. a smaller `--mem-fraction-static`,
   which touches the argument vector `gx-max.conf` calls LOCKED — that
   would need explicit sign-off, not a unilateral change).

**This is the one item in this session's work that could not be completed
without a human physically present — see the top-level completion report
for the rest.**

## B-021 (S2) — `--memory` cgroup caps do not bound a model's real footprint on this unified-memory hardware
**Needs:** no immediate action, but every memory budget in this repo that
cites a `--memory` cap as the thing keeping a node safe should be read with
this correction in mind. A decision is needed only if we ever want a *hard*
enforced ceiling rather than a cooperative one.

**Found 2026-09-16**, while measuring gx-reason's real footprint during the
B-011 fix verification. With the model fully loaded and idle on node 2:

| Measurement | Value |
|---|---|
| Node 2 MemAvailable before load | 114 GiB |
| Node 2 MemAvailable with gx-reason loaded | 70 GiB |
| **Real node-level footprint** | **~44 GiB** |
| `gx-reason` container `memory.current` (cgroup v2) | **10.92 GiB** |
| `docker stats` MemUsage for the same container | 9.65 GiB / 45 GiB (21%) |
| Largest process RSS inside it (`VLLM::EngineCor`) | 5.78 GiB |

About **33 GiB of the 44 GiB is invisible to the container's memory
cgroup.** On DGX Spark/GB10 the CUDA allocator's pool comes out of the same
physical unified memory as host RAM, but is not charged to the container's
`memory.current`. So `--memory 45g` on gx-reason (and by the same mechanism
`--memory 86g` on gx-fast, `--memory 106g` on the gx-max ranks) does **not**
cap what those containers actually take from the node.

**What still works, and why this is S2 not S1:**

* The admission guard was never relying on the cgroup number — it reads the
  node's real `MemAvailable` from `/proc/meminfo`
  (`resource_guard.compute_admission`), which does see the full 44 GiB. The
  pre-launch safety check is therefore still sound.
* `--gpu-memory-utilization` (0.35 for gx-reason, 0.66 for gx-fast) and
  SGLang's `--mem-fraction-static` are the parameters that genuinely bound
  the pool, and they measured true: 0.35 x 121 GiB ~= 42 GiB predicted vs
  ~44 GiB observed.
* The cap is still a real backstop for host-side allocation, and is not
  cosmetic: it is what fired on the orphaned `gx-max-rank1` in B-020
  (`OOMKilled=true` against its 106 GiB cap), which is how node 2 recovered
  without a power cycle.

**What is now known to be wrong in the docs:** `node02.yaml`'s
host-resilience comment called `--memory` "an enforced backstop for the SAME
ceiling the group above already assumes structurally". That is true only for
the host-side share. The comment has been corrected in place; `node01.yaml`
carries the same original wording and should get the same correction when it
is next touched.

**If a hard ceiling is ever actually required** the options are (a) keep
sizing via `--gpu-memory-utilization`, which is what we do today and what
the measurements support, or (b) investigate whether the NVIDIA container
stack on this platform can charge device allocations to the cgroup at all —
research, not a config change, and not worth doing unless a real incident
demands it.

## B-022 (S1) — RESOLVED 2026-09-16 (D-025): gx-max serves again. The "does not fit" conclusion below was wrong.

**Resolution.** The failed runs recorded below were not the verified
configuration. They ran with `--memory 106g --memory-swap 106g`, which sets
the container's swap limit to 0, and with `--mem-fraction-static` at 0.70 or
0.50, both below the ~0.731 a TP=2 shard needs. With the verified `4b96e49`
vector restored, no cgroup cap, and a gx-max-specific takeover admission
policy in place of `peak + 30 GiB`, gx-max loaded in 539 s and served
correct output on both the direct and gateway paths. Numbers are in D-025
and TEST_RESULTS.md §16. The original text is kept below as a record of the
investigation. Residual load-time swap headroom on node 1 is tracked as
B-023.

### Original entry (superseded)

## B-022 (S1) — gx-max cannot hold a 30 GiB MemAvailable reserve on 128 GB nodes; the floor and the locked model are arithmetically incompatible

**Opened:** 2026-09-16. **Status:** OPEN — needs a human decision on the
reserve value. Everything else about gx-max is fixed and proven; this is the
one remaining item, and it is a policy number, not a bug.

**This supersedes B-017's "option 1" (D-020), which set a gx-max-only reserve
of 5 GiB.** That was the right shape of answer with the wrong number and no
measurements behind it. The numbers now exist.

### The arithmetic

One node is 121.63 GiB usable (`124546 MiB`, as `torch.cuda.mem_get_info`
reports it to SGLang — the whole unified pool). The locked checkpoint is
163.48 GiB on disk, 155.77 GiB of which is MoE expert weights. At the locked
`--tp 2`, each rank holds roughly half:

```
per-rank weights            ~82 GiB          (67% of the node)
+ CUDA context / NCCL       ~2 GiB
+ host-side SGLang procs    ~6 GiB
+ node baseline             ~5-9 GiB
------------------------------------------------
steady-state footprint      ~95-99 GiB
node total                  121.63 GiB
=> maximum achievable MemAvailable   ~23-27 GiB
```

There is no 30 GiB left to reserve. And during weight loading it is far
worse: measured troughs of **8-12 GiB on node 1 and 1 GiB on node 2**.

### Why tuning cannot fix it

`--mem-fraction-static` was the obvious lever and it does not work. Measured
directly, at 0.50 and at 0.70, the load-phase trough is **identical** —
because the trough is the model weights landing in NVIDIA-driver-held unified
memory, not the KV/static pool. The fraction only moves the *steady state*
(0.80 -> 0.70 buys back 12.2 GiB). Every other lever tried — context length,
chunked prefill size, CUDA-graph batch tier, max running requests — moves the
steady state by single GiB and the transient by nothing.

The only things that would move the weight term are a different model, a
different quantisation, or more than 2 nodes. All three are LOCKED (L-6).

### What is already done and proven

* Engine retuned (D-022): steady-state MemAvailable improves from ~15 GiB
  (the 2026-09-14 verified-working configuration) to ~26 GiB.
* Memory guards made phase-aware, so the unavoidable load transient is no
  longer mistaken for a steady-state breach.
* The orphan-rank failure that made B-020 an 80-minute outage is fixed and
  tested on the real workload — see B-020's update.

### The decision the human needs to make

1. **Set a gx-max-specific steady-state reserve of ~20 GiB** and accept a
   documented load-phase excursion to ~1-8 GiB. This is what the hardware
   actually permits, it is what the 2026-09-14 verified-working run already
   did (it ended at 14.93 GiB free), and it keeps gx-max in the product.
2. **Keep 30 GiB as an absolute floor** and accept that gx-max is
   permanently un-runnable on this hardware — the admission guard refuses it,
   correctly, and `gx-max` becomes a tier the gateway exposes but never
   serves. This is a coherent choice; it just needs to be a chosen one.
3. **Change a locked decision** (smaller model, heavier quantisation, or
   more nodes) so the weights fit with 30 GiB to spare. Requires L-6 to be
   reopened.

Until this is decided, `GXMAX_GUARD_RESERVE_GIB` stays at **30** and the
admission guard refuses gx-max rather than quietly admitting it under a
number nobody approved.


## B-023 (S3) — node 1's gx-max load transient reaches the swap ceiling

**Status:** OPEN, monitored, not blocking. **Found:** 2026-09-16.
**Retested 2026-09-17** (real gx-max + gx-music takeover through the
Control Center MAX profile, evidence
`/srv/logs/acceptance/final-20260917T075129Z/gxmax-takeover*`): acquire took
671 s, and inference answered through the gateway. Minimum MemAvailable was
9356 MiB on node 1 and 11 695 MiB on node 2. Peak swap use was 65 531 MiB
on node 1 (the ceiling) and 40 895 MiB on node 2. Steady state was
16.2 GiB / 17.6 GiB available. Release took 50 s and restored normal
operation. The node-1 load transient still touches the swap ceiling briefly.
The safety monitor did not trip, and SSH and Tailscale stayed responsive.
Unchanged: this stays monitored, and the drain decision below is still open.

During the verified gx-max load, node 1 used all 64 GiB of swap for about
5 s. MemAvailable was 2.6 GiB at that point, and PSI full peaked at 22%.
Node 2, which has no control plane, peaked at 51.6 GiB. The 2026-09-14 run
showed the same "63/63 GB". The load succeeds, and the management plane
stayed responsive throughout (fork+exec at most 6 ms). But the margin is
thin: node 1 carries the gateway, Postgres, Open WebUI, AgentOS and a
desktop session.

**Protection in place.** `gx-max-safety.sh` aborts and unwinds if
MemAvailable stays under 512 MiB **and** swap free stays under 2 GiB for
30 s. `--oom-score-adj 950` makes SGLang the kernel's victim.

**Options, none of which touch a locked value:**

* stop non-cluster workloads on node 1 (Open WebUI, AgentOS, desktop apps)
  before a gx-max launch;
* raise node 1's swap. This needs root and is explicitly not done (L-8
  says keep, not add).

**Human decision needed:** whether gx-max-start.sh may stop Open WebUI and
AgentOS as part of its drain.


## B-024 (S3) — RESOLVED 2026-09-17 — the media router's bearer key was the public placeholder `not-required`

**Resolution:** rotated automatically during the V2 migration. A new random
key was written to `legenex/gateway/.env` (gx10-01) and `~/gx-media/.env`
(gx10-02) without being printed, and LiteLLM and the router were recreated. The
SHA-256 hashes match on both nodes, and the media acceptance suite passed through the
gateway with the new key.

**Status (historical):** OPEN, needs a human. **Found:** 2026-09-16 (control-UI run).

`GX_MEDIA_API_KEY` in `legenex/gateway/.env` on gx10-01 and in
`~/gx-media/.env` on gx10-02 is the literal string `not-required`. That
string is published in the public `.env.sample`. The router listens only on
the point-to-point fabric address (`192.168.100.11:18800`), so the practical
exposure is small: anything that can reach the fabric can generate media.
Still, the key protects nothing.

The control UI shows it as a warning (Dashboard → warnings, Settings →
credential hygiene), without the value.

**Why the agent did not fix it.** Rotating it writes to both secret stores,
and the permission policy blocked that write during this run.

**Fix (about 2 minutes, no model impact).** Run on **gx10-01**:

```bash
cd ~/Documents/Projects/Server/gx-cluster/legenex/gateway
umask 077; cp -p .env /srv/projects/gx-cluster/secrets/gateway.env.bak-$(date +%Y%m%dT%H%M%S)
NEW="sk-$(openssl rand -hex 32)"
sed -i "s|^GX_MEDIA_API_KEY=.*|GX_MEDIA_API_KEY=${NEW}|" .env
printf '%s' "$NEW" | ssh legenex-02@gx10-02 'umask 077; K=$(cat); cp -p ~/gx-media/.env ~/gx-media/.env.bak; sed -i "s|^GX_MEDIA_API_KEY=.*|GX_MEDIA_API_KEY=${K}|" ~/gx-media/.env'
unset NEW
ssh legenex-02@gx10-02 'cd ~/gx-media && docker compose -f docker-compose.media.yml up -d --no-deps router'
docker compose --env-file .env -f docker-compose.gateway.yml up -d --no-deps litellm
systemctl --user restart gx-orchestrator.service gx-control-ui.service   # only while gx-max is down
```

Then verify: control UI → Playground → gx-image and gx-video, and Settings →
credential hygiene shows `GX_MEDIA_API_KEY: set`.

## B-025 (S2) — gx-reason's required model is gated and no Hugging Face token exists

**Status:** OPEN, needs a human. **Found:** 2026-09-17 (V2 migration).
**Re-checked 2026-09-17 10:25 (final integration pass), still blocked.**
The user reported saving a token in the Control Center, but it never reached
gx10-01:
* `/srv/projects/gx-cluster/secrets/hf/` is empty (no `token`), and there is
  no `~/.cache/huggingface/token` on either node;
* the Control Center access log has no `POST /api/manager/hf-token`. The
  admin session that signed in at 08:39 made no POST requests and never
  opened Model Manager. The audit log has no `hf.token.set` event;
* anonymous access to the model's `config.json` still returns HTTP 401.

The gx10-02 disk is no longer a constraint (328 GiB free, B-026). The
interim `nvidia/Qwen3.6-27B-NVFP4` keeps serving gx-reason and passed the
real Kilo reasoning case in this pass. **Action:** sign in at
http://100.105.214.61:8088 → Model Manager → *Hugging Face token* → paste the
read token → *Save*. The page should then show "token saved". Then run
look-up → Stage → Verify → Test-serve → Assign as below. Nothing else is
needed from the human.

`iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` is `gated: auto`. The HF API
returns 401 for its files from both nodes, and neither node has a token
(`~/.cache/huggingface/token` and `/srv/projects/gx-cluster/secrets/hf/token`
are absent). gx-reason therefore stays on the interim `nvidia/Qwen3.6-27B-NVFP4`
(D-033). No substitute was chosen.

**What was checked:** the repository id, gating and size via the HF API
(105 935 758 025 bytes). Anonymous file access returns 401.

**Smallest action:**
1. While logged in to huggingface.co, open the model page and accept the terms.
2. Create a read token.
3. Control UI → Model Manager → *Hugging Face token* → save it (stored 0600,
   never shown again).
4. In Model Manager, look up the repository, then *Stage on gx10-02* →
   *Test-serve* → *Assign to gx-reason*. The assignment restarts llama-swap on
   node 2, runs a real completion and rolls back automatically on failure.

**Disk:** node 2 needs about 99 GiB free for this download. It had about
20 GB when this was written; it now has 328 GiB (B-026 resolved).

## B-027 (S2) — RESOLVED 2026-09-17 — a gx-max release recreated gx-litellm with the media-key placeholder

**Found by** the integrity audit during the final integration pass. **Cause:**
gx-orchestrator started at 00:48, before `legenex/gateway/.env` received
the rotated media key at 01:27 (B-024). It therefore held
`GX_MEDIA_API_KEY=not-required` in its environment. `gx-max-stop.sh` →
`restore-normal.sh` ran from it and executed `docker compose --env-file .env
up -d`, and a caller's variable overrides `--env-file`. gx-litellm was
recreated with the placeholder at 10:03 (image and video through the
gateway would have been refused; the Control Center and Playground talk to
the router directly and were unaffected).
**Fix:** `restore-normal.sh` now unsets every name defined in `.env` before
running compose, so the file always wins. Regression test:
`legenex/lifecycle/tests/test_restore_normal_sh.py`. gx-litellm was
recreated from `.env`, and gx-orchestrator was restarted (it now holds the
current key). The audit check `gx-litellm media key matches .env` passes.

## B-026 (S2) — RESOLVED 2026-09-17 — obsolete checkpoints were not deleted; gx10-02 disk was at 98 %

**Status: RESOLVED.** The user deleted the obsolete checkpoints. Re-checked on
the filesystem on 2026-09-17 at 09:10:

* **Gone:** both `DeepSeek-V4-Flash-0731-NVFP4` copies, `Qwen3.6-35B-A3B-NVFP4`
  (gx10-01), the gx10-02 122B GGUF and the gx10-02 122B NVFP4.
* **Disk now:** gx10-01 has 492 GB free (44 %). gx10-02 has 328 GB free
  (63 %), which Storage & Cleanup rates HEALTHY. Disk no longer blocks
  anything, including the gx-reason replacement.
* **Still on disk, offered only as REVIEW in Storage & Cleanup:**
  * `/srv/models/gguf/Qwen3.5-4B` (3.2 GB, the gx-mini rollback);
  * `hidream_i1_full_fp8.safetensors` (16 GB on gx10-02, referenced by no
    workflow).
* **Registry:** it records the gx-max and gx-fast rollbacks as deleted
  (`on_disk: false`). A gx-max rollback now needs a fresh download.

**Found:** 2026-09-17. Original entry below.

The migration request asked for superseded weights to be removed after
acceptance. The agent's permission layer refused the delete (irreversible
deletion under `/srv`, which CLAUDE.md also lists as needing sign-off). No
workaround was attempted.

Superseded after acceptance. Each replacement has produced real output:

| Node | Path | Size | Superseded by |
|---|---|---|---|
| gx10-01 | `/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4` | 164 G | CRACK (D-032). This is also the gx-max rollback. |
| gx10-02 | `/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4` | 164 G | same |
| gx10-01 | `/srv/models/vllm/Qwen3.6-35B-A3B-NVFP4` | 22 G | kyaky uncensored (D-030) |
| gx10-01 | `/srv/models/gguf/Qwen3.5-4B` | 3.2 G | HauhauCS (D-030) |
| gx10-02 | `/srv/models/gguf/Qwen3.5-122B-A10B` | 73 G | retired in B-011 |
| gx10-02 | `/srv/models/vllm/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10` | 74 G | retired (root-owned: needs `docker run --rm -v /srv/models/vllm:/m alpine rm -rf /m/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10`) |
| gx10-02 | `/srv/models/image/diffusion_models/hidream_i1_full_fp8.safetensors` | 16 G | no workflow references it |

**Keep:** `/srv/models/vllm/Qwen3.6-27B-NVFP4` on gx10-02. It is the live
interim gx-reason model until B-025 is closed.

**How:**
* **Model Manager route:** Model Manager → *Accept* on the alias, then
  *Delete* on the row. The UI refuses to delete anything still referenced.
* **Shell route:** `rm -rf` on each path above, on the named node.

Deleting both old DeepSeek directories removes the gx-max rollback. If a
rollback is still wanted, keep the gx10-01 copy and re-sync it from there.

Node 2 was at 850 G of 916 G (20 G free) at 03:20 on 2026-09-17. Part of the
recent growth is a separate `gx-music` workstream on gx10-02
(`/srv/models/music` 28 G, two `gx-music-engine` images of about 23 G) and
20 G of Docker build cache. That work is outside this repo; ask its owner
before pruning it.
