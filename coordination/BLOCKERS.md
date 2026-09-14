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
