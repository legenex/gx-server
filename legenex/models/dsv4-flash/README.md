# dsv4-flash — DeepSeek-V4-Flash-0731 (abliterated) on both GX10 nodes

The PRIMARY deployment of D-053: vLLM 0.25.1, TP=2 over gx10-01 (rank 0 + API)
and gx10-02 (rank 1), weights
`cebeuq/DeepSeek-V4-Flash-0731-abliterated@21bd923c` over
`deepseek-ai/DeepSeek-V4-Flash-0731@7872f01b`, image built here from the
reviewed Anemll v0.1.1 logic. DeepSeek V4.1 (mia-dsv41) stays the rollback.
The orchestrator drives this kit; nothing starts at boot.

| File | Role |
|---|---|
| `pins.conf`, `vendor-files.txt`, `requirements-pypi.txt` | every third-party input, by digest / sha256 |
| `Dockerfile.runtime`, `build-image.sh` | offline (`--network=none`) hash-checked image build |
| `upstream-overlay/` | the reviewed `overlay/vllm` files + licences, `OVERLAY.SHA256SUMS` |
| `gx_probe/` | vLLM plugin: logs each rank's loaded `layers.0.attn.wo_b` sha256 (`GX_WOB_DIGEST=1`) |
| `dsv4-flash.env.canonical` | the real serving values (tracked, no secrets); `.env` = local overrides |
| `start.sh` / `stop.sh` | preflight, NFS, launch, health, memory floor / the sanctioned stop path |
| `nfs/entrypoint.sh` | NFSv4-only, read-only, fabric-bound export of the weights from gx10-02 |
| `tools/` | `overlay_check.py`, `mem_budget.py`, `refusal_check.py`, NCCL fingerprint |

## Overlay loading (why the abliterated `wo_b` wins)

vLLM 0.25.1 `DefaultModelLoader` keeps every safetensors file the index names
(the base shards stay: they hold the other 72,225 tensors), and
`safetensors_weights_iterator` yields every tensor of every file in
`_natural_sort_key` order with no de-duplication; DeepSeek-V4's and DSpark's
`load_weights` copy each one into its parameter, so the LAST copy stays.
`model-` < `model-overlay-` under that key, so the overlay file is read last.
`tools/overlay_check.py verify` proves the layout at every start (run on the
real weights 2026-10-16: OK, 92 overlay tensors, 92 base copies still in the
shards, overlay read last of 49 files). Conditions: default load format
(`--load-format safetensors` is pinned in start.sh), no multithread /
fastsafetensors / runai loader, no stray `*.safetensors`.

Runtime proof: each rank logs `GX_WOB_DIGEST` for `layers.0.attn.wo_b.weight`
(RowParallel, shard of dim 1). Expected sha256 from the files (TP=2):

| tensor | rank | overlay (must match) | base copy (must NOT) |
|---|---|---|---|
| layers.0 | 0 | `79fb3f5e923443ce…` | `d48bdc35b74d3d06…` |
| layers.0 | 1 | `9addce90db93cac2…` | `d68410f7a9602901…` |
| mtp.0 (DSpark) | 0 | `cf5743bec07b9f2f…` | `a644b76aeb50e267…` |
| mtp.0 (DSpark) | 1 | `1ced90ca4749226b…` | `8caf83ad6f382051…` |

(full values: `tools/overlay_check.py digests <dir>`; dim-0 values are also
listed in case vLLM's layout differs; `compare` classifies automatically.)

## Memory

Pool per rank = `gpu_memory_utilization x MemTotal (121.6 GiB)`. 0.74 =
90.0 GiB = ~79 GiB weights + ~11 GiB KV/workspace (fp8 KV, 131072 window,
8 sequences). vLLM pre-allocates the KV pool, so 8-way load adds little beyond
it. Measured idle MemAvailable (no model): head 106.0-113.7 GiB, worker
115.0-117.2 GiB, so after load head 12.0-19.7 and worker 21.0-23.2 GiB (4 GiB
out-of-pool overhead). `start.sh` refuses a node that would end below 15 GiB
and fails the start if either node is below it after load. If the head is
busy, free memory first (e.g. stop test databases) — never lower the floor.

## Image (built 2026-10-16 on gx10-02, offline)

`gx/dsv4-flash-vllm:0.25.1-b12x7dc6fb8f-fi0472b9b3-gx1` =
`sha256:ce6ccb53ecc9362639da1f529e9161186e030176e6067ae4bf3503170e95fc87`
(19.8 GB; base `sha256:30a38a1d…` = vllm/vllm-openai v0.25.1 arm64). Installed:
vllm 0.25.1, flashinfer-python 0.6.15 (built from 0472b9b3), b12x 0.15.3
(7dc6fb8f), cuda-python 13.3.1, cutlass-dsl 4.5.2, tvm-ffi 0.1.9 (the base
already ships these five at the pinned versions; the hashes were still
checked), torch 2.11.0+cu130, nvidia-nccl-cu13 2.28.9 (unchanged). All 12
overlay files verified in the image; the overlay-proof plugin is registered.
It is on gx10-02 only: gx10-01 had ~33 GB free, and loading 19.8 GB would leave
< 15 GB. Free >= ~20 GB on gx10-01 first (owner action), then on gx10-01:
`ssh legenex-02@10.60.21.41 'docker save gx/dsv4-flash-vllm:0.25.1-b12x7dc6fb8f-fi0472b9b3-gx1' | docker load`
and check `docker image inspect -f '{{.Id}}'` equals the id above.

## Activation runbook (lead, inside 01:00-05:00 SAST)

Keys never go on a command line, the screen, `ps` or shell history: the
operator headers live in a 0600 file under `$XDG_RUNTIME_DIR` and every call
uses `curl -H @"$HF"`; `accept.sh` reads the gateway key from its file.

```bash
# gx10-01 — 0. operator token (once) and the headers file (each session)
( umask 077; mkdir -p /srv/projects/gx-cluster/secrets/orchestrator
  [ -s /srv/projects/gx-cluster/secrets/orchestrator/operator-token ] || \
    openssl rand -hex 24 > /srv/projects/gx-cluster/secrets/orchestrator/operator-token )
HF=$(legenex/models/dsv4-flash/tools/op_headers.sh <your-name>)
trap 'rm -f "$HF"' EXIT          # the headers file dies with this shell
O=http://127.0.0.1:18900

# 1. readiness (read-only)
curl -s -H @"$HF" $O/models/status | jq '.active, .lifecycle.state'          # "dsv41-rollback"
curl -s -H @"$HF" $O/models/rollback-readiness | jq '.deployments[] | {role, rollback_ready}'

# 2. the switch (async). A running switch answers 409 to a second activate.
curl -s -H @"$HF" -X POST $O/models/activate -d '{"target":"dsv4-flash-primary","confirm":true}' | jq .switch.id
watch -n 15 "curl -s -H @$HF $O/models/status | jq -c '.switch.state, .switch.steps[-1]'"
#   expect "succeeded"; on any failure the switcher restores V4.1 itself ("rolled_back").
#   Audited overrides only if needed: "override_window":true + "emergency_reason",
#   "cut_in_flight":true, "reactivate":true.

# 3. V4 Flash gateway config: copy, recreate ONLY gx-litellm, identity-hmac verify
legenex/models/dsv4-flash/tools/gateway_config.sh dsv4

# 4. acceptance: overlay digests (PRIMARY proof of the abliterated weights),
#    refusal check (corroboration), bench c=1,2,8, memory floor, no rank restarts
legenex/models/dsv4-flash/tools/accept.sh            # -> "ACCEPTANCE: GO"

# rollback at any time (window, or override_window + emergency_reason):
curl -s -H @"$HF" -X POST $O/models/activate -d '{"target":"dsv41-rollback","confirm":true}'
legenex/models/dsv4-flash/tools/gateway_config.sh landing   # after it succeeded

# end of session (the EXIT trap does it too; or: tools/op_headers.sh --cleanup)
rm -f "$HF"
```

Kit-level stop if the orchestrator is unavailable: `legenex/models/dsv4-flash/stop.sh`.

Identity signing key (done 2026-10-16 by the lead): `legenex/gateway/identity-hmac.sh`
provision / verify / rollback. ROTATION: rollback, provision, then
`systemctl --user restart gx-orchestrator` (its unit loads the gateway .env,
and an environment key beats the key file). The V4.1/V4 Flash wake by
X-GX-Priority for AI OS / AgentOS relies on their LiteLLM key aliases being
exactly `ai-os` and `agentos`.

## Window plan, night of 2026-10-16/17 (01:00-05:00 SAST)

Preflight run 2026-10-16 ~16:00 SAST, read-only (V4.1 resident, untouched):

| Check | Result |
|---|---|
| weights on gx10-02 | VERIFY.json 60/60 (2026-10-15T23:53Z), dir 0750 legenex-02, overlay order proof OK (92 tensors, overlay read last). The head has no copy by design: it reads over NFS |
| image on both nodes | `sha256:ce6ccb53…` on gx10-01 AND gx10-02 (= `DSV4F_IMAGE_ID`) |
| `config.dsv4-flash.yaml` | loaded by the live LiteLLM image in a throwaway proxy: 5 aliases, windows 122880/24576/98304/98304/65536, merge off, no errors |
| identity HMAC | provisioned and verified by the lead (prefix `cd068f60…`) |
| **operator token** | **MISSING**: `/models/*` answers 503 "switching is disabled" -> step W0 |
| fabric | rocep1s0f0 ACTIVE on both, RoCE v2 IPv4 at GID index 3 (head .10, worker .11) |
| NFS | nfsd module present on gx10-02, nothing on :2049; the alpine NFS image is shipped by start.sh |
| CDI GPU | `nvidia.com/gpu=all` on gx10-01 (and gx10-02) |
| orchestrator | live 82ca712, active `dsv41-rollback`, READY, idle TTL 1800 |
| memory model | today's idle after V4.1 release: head 113.4-115.8, worker 116.8-117.0 GiB -> after load at 0.74: head ~19-22, worker ~23 GiB (floor 15). Below 110 GiB on the head the start refuses |
| tools | `tools/accept.sh` (overlay digests, refusal, bench c=1,2,8 + memory watcher), `tools/bench.py` (the V4.1 baseline method), `tools/refusal_check.py`, `tools/mem_watch.sh` |

Timed plan (all on gx10-01, in ONE operator shell started with
`HF=$(legenex/models/dsv4-flash/tools/op_headers.sh <name>); trap 'rm -f "$HF"' EXIT`;
every call `curl -s -H @"$HF" ...`; `tools/op_headers.sh --cleanup` if the shell was lost):

| Time | Step | Go criterion / action on failure |
|---|---|---|
| before 01:00 | **W0** operator token (owner): `umask 077; openssl rand -hex 24 > /srv/projects/gx-cluster/secrets/orchestrator/operator-token` (read per request: no restart) | `GET /models/status` -> 200, `active: dsv41-rollback` |
| 01:00 | **W1** `GET /models/rollback-readiness`; `df -h /` on gx10-01 >= 15 GB; no AI OS work orders running. DEFAULT: do NOT release V4.1 by hand: the switch stops it through its own stop path, waits up to 60 s for memory to return and refuses to boot unless each node has >= 109 GiB. OPTIONAL (only to shorten that wait): when `GET /models/status` shows `lifecycle.state` `ready`, `curl -s -H @"$HF" -X POST $O/lifecycle/gx-max/release -d '{}'` (never `docker stop`), then go to W2 at once | both `rollback_ready: true`; else NO-GO (stay on V4.1) |
| 01:05 | **W2** STATE GATE first: `curl -s -H @"$HF" $O/models/status | jq -r .lifecycle.state` must be `ready` or `down`. If it is `acquiring`, wait until it is `ready` and let the switch do the release; if `releasing`, wait for `down`. Never activate during `acquiring` or `releasing`. Then `POST /models/activate {"target":"dsv4-flash-primary","confirm":true}`; poll `/models/status` every 30 s | drain <= 10 min (else `aborted`, nothing stopped; retry with `cut_in_flight` only if the owner agrees) |
| ~01:08 | V4.1 stopped, preflight (memory, image, holds), V4 Flash boot (budget 40 min, `DSV4F_READY_TIMEOUT`) | the switcher itself rolls back on ANY failure (`rolled_back`, V4.1 back in ~10 min) |
| by ~01:50 | **W3** switch `succeeded`: all checks ok (ranks, identity, completion, fabric_rdma, streaming, tool_call, continuation, tool_call_thinking) | if `rolled_back`: collect `~/.cache/gx-dsv4-flash/run/*-fail.log`, stay on V4.1, end the window |
| ~01:55 | **W4** `tools/gateway_config.sh dsv4`: copies `config.dsv4-flash.yaml` over the live `config.yaml`, recreates ONLY gx-litellm, runs `identity-hmac.sh verify` | prints "identity signing verified". The live `config.yaml` is tracked, so autosync commits it: that IS the post-activation config |
| ~02:00 | **W5** `tools/accept.sh` (~30-45 min; reads the gateway key from its file) | `ACCEPTANCE: GO`: overlay (primary proof), refusal (<= 1 per mode; read the printed matches), bench (tokens > 0, no think leak, tools), memory (>= 15 GiB both nodes, no NA, >= 10 samples), ranks (no restart) |
| ~02:45 | **W6 (optional drill, only if >= 75 min remain)** V4 -> V4.1 -> V4: activate `dsv41-rollback` (~2 min stop + ~7.5 min V4.1 cold load + ~3 min validation), `tools/gateway_config.sh landing`; then activate `dsv4-flash-primary` again (as W2-W3) and `tools/gateway_config.sh dsv4` | each leg `succeeded` and each gateway_config run verified; time each leg |
| **03:30 hard stop** | if V4 Flash is not accepted by 03:30: `POST /models/activate {"target":"dsv41-rollback","confirm":true}`. If it answers **409** (a switch is still running), wait for that switch to finish (`/models/status`; it rolls itself back on failure), then roll back if V4 Flash is still active. Then `tools/gateway_config.sh landing` (recreates ONLY gx-litellm + identity-hmac verify) | V4.1 `succeeded` and verify OK before 04:00 |
| 05:00 | window closes | |

NO-GO triggers at any point: an `overlay` FAIL (the base weights are serving), a
refusal FAIL, MemAvailable < 15 GiB on either node under c=8, `rollback_failed`
(then run `legenex/models/dsv4-flash/stop.sh`, then `POST /lifecycle/gx-max/acquire`
for V4.1), or any rank container restarting. If the boot fails on container
permissions, set `DSV4F_RUN_AS_SERVICE_USER=0` in the kit's local `.env` and
switch again (root containers, the V4.1 kit's model).

Identity key housekeeping: `identity-hmac.sh rollback` moves the key file to
`identity-hmac.disabled-<UTC>` and keeps up to 3 `.env` backups
(`gateway.env.bak-identity-hmac-<UTC>`), all 0600 in the secrets store, so the
key still exists on disk after a rollback. To erase it:
`rm -f /srv/projects/gx-cluster/secrets/orchestrator/identity-hmac.disabled-* /srv/projects/gx-cluster/secrets/gateway.env.bak-identity-hmac-*`.

## Security mapping (independent review)

1 base by digest, local build, no anemll images · 2 tarballs + wheels sha256,
pip `--no-index`, wheels `--require-hashes`, overlay path asserted and files
hash-checked · 3 `BUILD_NVEP=0`, NCCL fingerprint equal before/after · 4
`VLLM_NO_USAGE_STATS`, `DO_NOT_TRACK`, `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE`
in image and containers · 5 no `--trust-remote-code` (start.sh refuses) · 6
API `127.0.0.1:8890`; NCCL/Gloo/`VLLM_HOST_IP` on `enp1s0f0np0` (rail 1);
nfsd bound to 192.168.100.11 for 192.168.100.10 only · 7 caches 0700 per node,
weights 0750/0640, containers run as the service user with all capabilities
dropped · 8 no dashboard.

Residual: the torch/vLLM TCPStore on the master port (29531) binds all
interfaces (same as V4.1's 29521); closing it needs a host firewall (root,
owner action). The NFS server container is privileged (kernel nfsd needs it).
