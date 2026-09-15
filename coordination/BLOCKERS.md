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

## B-011 (S2) — gx-reason GGUF produces garbage output on this llama.cpp build
**Status: OPEN. gx-reason is NOT working.**

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
**Status: incident understood; structural admission control now shipped on
node 1 (see below); node 2 itself still needs a physical power cycle and has
no ledger deployed yet.**

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
**Not yet true of node 2**: it has no ledger/lock module deployed (it has
been unreachable all session) — `gx-max-start.sh`'s rank1 launch uses a real
remote `flock` as a documented convention only. Deploy `legenex/lifecycle/`
and `legenex/orchestrator/` to node 2 once it's reachable to close this gap
fully.

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
