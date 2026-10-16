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

Prerequisites (once, any time): the image on BOTH nodes with the id pinned in
`dsv4-flash.env.canonical` (`DSV4F_IMAGE_ID`), gx10-01 disk for it (see the
build record), this branch merged and deployed on gx10-01, and an operator
token in `/srv/projects/gx-cluster/secrets/orchestrator/operator-token`
(0600, >= 16 chars).

```bash
# gx10-01 — 0. operator token (once)
umask 077; mkdir -p /srv/projects/gx-cluster/secrets/orchestrator
openssl rand -hex 24 > /srv/projects/gx-cluster/secrets/orchestrator/operator-token
OT=$(cat /srv/projects/gx-cluster/secrets/orchestrator/operator-token)
OK=$(grep ^GX_ORCHESTRATOR_API_KEY= legenex/gateway/.env | cut -d= -f2-)
H=(-H "Authorization: Bearer $OK" -H "X-GX-Operator-Token: $OT")

# gx10-01 — 0b. identity signing key (once), shared by LiteLLM and the orchestrator
openssl rand -hex 32 > /srv/projects/gx-cluster/secrets/orchestrator/identity-hmac
#   and set GX_IDENTITY_HMAC_KEY=<same value> in the gx-litellm container env.
#   Without it every identity is anonymous (fail closed: no pins, no odin lane).

# gx10-01 — 1. deploy the code (merge, then): restart the control plane
systemctl --user restart gx-orchestrator          # adopts the running V4.1 (still active)
cp legenex/gateway/litellm/config.yaml <live gx-litellm config>; docker restart gx-litellm
#   (landing config: gx-max/gx-auto keep their V4.1 model_info)
curl -s "${H[@]}" http://127.0.0.1:18900/models/status | jq .active          # "dsv41-rollback"
curl -s "${H[@]}" http://127.0.0.1:18900/models/rollback-readiness | jq      # both rollback_ready

# gx10-01 — 2. kit preflight (launches nothing; V4.1 must be idle-released or it refuses)
legenex/models/dsv4-flash/start.sh preflight
#   expect: overlay order proof OK, GID indices, memory head/worker "fits": true

# gx10-01 — 3. the switch (async; poll status)
curl -s "${H[@]}" -X POST http://127.0.0.1:18900/models/activate \
     -d '{"target":"dsv4-flash-primary","confirm":true}' | jq
watch -n 15 "curl -s ${H[*]} http://127.0.0.1:18900/models/status | jq '.switch.state, .switch.steps[-1]'"
#   expect state succeeded; checks rank_node1/2, completion, served_identity,
#   fabric_rdma, streaming, tool_call, tool_result_continuation all ok.
#   On any failure the switcher restores V4.1 by itself: state rolled_back.
#   Explicit, audited overrides only if needed: "override_window":true +
#   "emergency_reason", "cut_in_flight":true, "reactivate":true (no "force").
#   Name yourself for the audit trail: -H "X-GX-Operator: <name>".

# gx10-01 — 3b. after "succeeded": the V4 Flash gateway config
cp legenex/gateway/litellm/config.dsv4-flash.yaml <live gx-litellm config>; docker restart gx-litellm

# gx10-01 — 4. acceptance (after succeeded)
grep GX_WOB_DIGEST <(docker logs dsv4f-head 2>&1) > /tmp/wob-head.log
ssh legenex-02@10.60.21.41 'docker logs dsv4f-worker 2>&1 | grep GX_WOB_DIGEST' > /tmp/wob-worker.log
scp /tmp/wob-*.log legenex-02@10.60.21.41:/tmp/
ssh legenex-02@10.60.21.41 'python3 -I ~/.cache/gx-dsv4-flash/kit/tools/overlay_check.py compare \
    /srv/models/dsv4-flash-0731-abliterated/21bd923c /tmp/wob-head.log /tmp/wob-worker.log'   # "ok": true
GX_GATEWAY_KEY=... python3 -I legenex/models/dsv4-flash/tools/refusal_check.py     # PASS
awk '/MemAvailable/' /proc/meminfo; ssh legenex-02@10.60.21.41 "awk '/MemAvailable/' /proc/meminfo"  # >= 15 GiB each
# 8-way load: python3 gx_bench.py ... c=8 on gx-fast, sample MemAvailable on both nodes during it (>= 15 GiB)
```

Rollback at any time (inside the window, or override_window + emergency_reason;
then put back `config.yaml` in gx-litellm):
`curl ... -X POST /models/activate -d '{"target":"dsv41-rollback","confirm":true}'`.
Kit-level stop if the orchestrator is unavailable: `legenex/models/dsv4-flash/stop.sh`.

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
