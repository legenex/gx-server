# gx cluster — gateway and model lifecycle

Configuration for the OpenAI-compatible gateway and the model process managers of
the two-node DGX Spark / ASUS GX10 cluster.

This directory is **configuration only**. It starts no distributed job, owns no
orchestration logic, and never touches `gx-max`.

---

## Files

| File | What it is | Runs on |
|---|---|---|
| `litellm/config.yaml` | The single user-facing gateway. Defines exactly the seven `gx-*` aliases and where each one resolves. | node 1 |
| `llama-swap/node01.yaml` | Model **process lifecycle** for node 1: `gx-mini`, `gx-fast`. On-demand start, idle TTL, health checks, graceful drain, memory-budget enforcement. | node 1 |
| `llama-swap/node02.yaml` | Same for node 2: `gx-reason`, plus a reserved budget and scaffold for the ComfyUI media tier. | node 2 |
| `docker-compose.gateway.yml` | Brings up LiteLLM + Postgres + node-1 llama-swap. Nothing else. | node 1 |
| `.env.sample` | Every variable documented. No real secrets, ever. | node 1 (and node 2 for `GX_SWAP_API_KEY`) |

Separation of concerns, deliberately:

- **llama-swap** decides *whether a model process is running*. It does not route.
- **LiteLLM** decides *which upstream a name maps to*. It does not pick tiers.
- **The lead's orchestrator** (`:18900`) decides *which tier* and owns cluster
  acquisition/drain for `gx-max`. Neither file here implements any of that.

---

## Port map

### Node 1 — `gx10-01` (control)

| Host port | Bind (default) | Service | Owner |
|---|---|---|---|
| `4000` | `127.0.0.1` | **LiteLLM gateway** — the only endpoint users need | this dir |
| `28080` | `127.0.0.1` | llama-swap node 1 control API + admin UI | this dir |
| `19001` | `127.0.0.1` | `gx-mini` direct (proven pre-existing endpoint) | this dir |
| `15432` | `127.0.0.1` | Postgres for LiteLLM | this dir |
| `18900` | host loopback | lead's orchestrator (`gx-max`, `gx-auto`) | **lead** |
| `30000` | — | SGLang `gx-max-rank0` — **do not touch** | **lead** |

`19001` is published by the llama-swap container, not by a container of its own:
spawned model containers join llama-swap's network namespace, so its port
mapping is what exposes them.

### Node 2 — `gx10-02` (compute)

| Address | Service | Owner |
|---|---|---|
| `192.168.100.11:28080` | llama-swap node 2 (`gx-reason`) — **must** bind the fabric address | node-2 compose (not in this dir) |
| `192.168.100.11:18800` | ComfyUI / media router (`gx-image`, `gx-video`) | **another agent — pending** |
| — | SGLang `gx-max-rank1` — **do not touch** | **lead** |

Management and SSH ride Tailscale. **Model traffic only ever uses the
ConnectX/RoCE fabric** (`192.168.100.10/.11`, `192.168.101.10/.11`). No Tailscale
address appears in any config in this directory.

---

## How each alias resolves

| Alias | Gateway upstream | Engine / node | Notes |
|---|---|---|---|
| `gx-mini` | `http://gx-llama-swap-node01:8080/v1` | llama.cpp, node 1 | Qwen3.5-4B-Q4_K_M + BF16 mmproj. Multimodal (vision is a capability of this model — there is no `gx-vision` alias). Resident, `ttl: 0`, always hot. |
| `gx-fast` | `http://gx-llama-swap-node01:8080/v1` | vLLM, node 1 | ~30–40B MoE, agentic. **Checkpoint pending.** Unloads after 30 min idle. |
| `gx-reason` | `http://192.168.100.11:28080/v1` | vLLM, node 2 | ~100–125B sparse MoE. **Checkpoint pending.** Unloads after 15 min idle. |
| `gx-max` | `http://host.docker.internal:18900/v1` | SGLang TP=2, **both** nodes | Routed through the lead's orchestrator so acquisition/drain is enforced. **Never** pointed at `:30000`. **No fallbacks.** |
| `gx-auto` | `http://host.docker.internal:18900/v1` | orchestrator | Tier selection is the lead's logic. This gateway is a pass-through. |
| `gx-image` | `http://192.168.100.11:18800/v1` | ComfyUI, node 2 | **Upstream pending.** |
| `gx-video` | `http://192.168.100.11:18800/v1` | ComfyUI, node 2 | **Upstream pending.** |

`host.docker.internal` is mapped to the host gateway by
`docker-compose.gateway.yml`. From the host the orchestrator is
`127.0.0.1:18900`; from inside the LiteLLM container `127.0.0.1` would be the
container itself, so the alias is required to reach the same endpoint.

---

## Memory budget

Each node is a **separate 128 GB unified-memory system**. They are *not* a
coherent 256 GB pool. Unified memory means CPU and GPU allocations compete for
the same bytes, so every budget below is a whole-system budget.

### Node 1 — ceiling ~100 GB, headroom ≥ 28 GB

| Tier (llama-swap group) | Cap | Members |
|---|---|---|
| `resident` | ≤ 14 GB | `gx-mini` (~9–10 GB measured footprint) |
| `heavy` | ≤ 86 GB | exactly one at a time — today `gx-fast` at `--gpu-memory-utilization 0.66` |

### Node 2 — ceiling ~100 GB, headroom ≥ 28 GB

| Tier | Cap | Members |
|---|---|---|
| `heavy` | ≤ 78 GB | one at a time — `gx-reason` at `--gpu-memory-utilization 0.61` |
| `media` | ≤ 22 GB | reserved for ComfyUI (`gx-image`, `gx-video`) |

**What makes this structural rather than aspirational:**

1. `heavy` groups use `swap: true`, so llama-swap will only ever have one heavy
   model loaded.
2. `resident` / `media` use `persistent: true`, so heavy models neither evict
   them nor are evicted by them — the two caps simply add.
3. `--gpu-memory-utilization` is **pinned**, not adaptive, so a heavy model's
   footprint cannot drift upward under load.

**Rule for anyone adding a model:** it must join exactly one group. A model in
no group falls into llama-swap's implicit default group, where it can coexist
with a heavy model and blow the budget.

---

## Starting and stopping

All commands run as an unprivileged user in the `docker` group. **No `sudo`
anywhere.** No host packages, no privileged containers, no host networking, no
kernel/netplan/RDMA changes.

```bash
cd legenex/gateway
cp .env.sample .env && chmod 600 .env && $EDITOR .env   # fill in every REQUIRED value

# One-time: build the llama-swap image (upstream has no docker CLI, so it
# cannot spawn model containers; the repo Dockerfile adds it).
docker compose -f docker-compose.gateway.yml build llama-swap-node01

# Validate without starting anything
docker compose -f docker-compose.gateway.yml config >/dev/null && echo OK

# Start the gateway (starts NO model)
docker compose -f docker-compose.gateway.yml up -d

# Status / logs
docker compose -f docker-compose.gateway.yml ps
docker compose -f docker-compose.gateway.yml logs -f litellm

# Stop the gateway. Does not stop gx-max, and does not stop model containers
# that llama-swap already spawned — unload those first (see below).
docker compose -f docker-compose.gateway.yml down
```

### Autostart without root

`systemctl --user` with lingering already enabled on node 1:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/gx-gateway.service <<'UNIT'
[Unit]
Description=gx cluster gateway (LiteLLM + llama-swap node 1)
After=default.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=%h/Documents/Projects/Server/gx-cluster/legenex/gateway
ExecStart=/usr/bin/docker compose -f docker-compose.gateway.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.gateway.yml down

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now gx-gateway.service
```

Adjust `WorkingDirectory` if the checkout lives elsewhere. Nothing here needs
`systemctl` (system scope) or a root-owned unit file.

### Unloading a model

```bash
# List what llama-swap currently has running
curl -fsS -H "Authorization: Bearer $GX_SWAP_API_KEY" http://127.0.0.1:28080/running

# Unload one model (graceful; honours cmdStop + unloadTimeout)
curl -fsS -X POST -H "Authorization: Bearer $GX_SWAP_API_KEY" \
  http://127.0.0.1:28080/api/models/unload/gx-fast

# Unload everything on node 1
curl -fsS -X POST -H "Authorization: Bearer $GX_SWAP_API_KEY" \
  http://127.0.0.1:28080/api/models/unload
```

Do this before any operation that needs node-1 memory back.

### Smoke tests (safe — none of these load a model)

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness
curl -fsS -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models
curl -fsS http://127.0.0.1:28080/health
```

`/v1/models` must list exactly seven ids: `gx-mini`, `gx-fast`, `gx-reason`,
`gx-max`, `gx-auto`, `gx-image`, `gx-video`.

---

## Pending — placeholders awaiting verified information

Nothing below was guessed. Each placeholder is written so that a start attempt
**fails fast** rather than silently serving the wrong thing.

| # | What | Where | Current placeholder | Needed from |
|---|---|---|---|---|
| 1 | `gx-fast` checkpoint path | `llama-swap/node01.yaml` macro `gx_fast_model_dir` | `/models/vllm/PENDING-VERIFY-gx-fast` | lead (agent verifying the ~30–40B MoE) |
| 2 | `gx-fast` serving image | `llama-swap/node01.yaml` macro `vllm_image` | `PENDING-VERIFY-gx-fast-vllm-image` | lead |
| 3 | `gx-fast` parser flags | `llama-swap/node01.yaml` cmd | `--tool-call-parser` / `--reasoning-parser` omitted | depends on (1) |
| 4 | `gx-fast` real context window | `litellm/config.yaml` `model_info` | provisional 57344 in / 8192 out | depends on (1) |
| 5 | `gx-reason` checkpoint path | `llama-swap/node02.yaml` macro `gx_reason_model_dir` | `/models/vllm/PENDING-VERIFY-gx-reason` | lead (agent verifying the ~100–125B sparse MoE) |
| 6 | `gx-reason` serving image + quantisation flags | `llama-swap/node02.yaml` macro `vllm_image` | `PENDING-VERIFY-gx-reason-vllm-image` | lead |
| 7 | `gx-reason` real context window | `litellm/config.yaml` `model_info` | provisional 24576 in / 8192 out | depends on (5) |
| 8 | `gx-max` model id the orchestrator expects | `litellm/config.yaml` `model:` | `openai/gx-max` | **lead** — SGLang itself reports the model literally as `/model`; if the orchestrator passes the field straight through, this becomes `openai//model` |
| 9 | `gx-max` KV pool size | `litellm/config.yaml` `model_info` | conservative 65536 in / 16384 out | **lead** — must be ≤ SGLang `--max-total-tokens`, or long prompts abort on first decode |
| 10 | `gx-max` orchestrator auth | `.env` `GX_ORCHESTRATOR_API_KEY` | `not-required` | **lead** |
| 11 | `gx-image` / `gx-video` router address and contract | `litellm/config.yaml` api_base; `llama-swap/node02.yaml` commented `gx-media` block | `http://192.168.100.11:18800/v1` | another agent (ComfyUI router spec) |
| 12 | node-2 llama-swap must publish on the fabric | node-2 compose (not in this directory) | — | node-2 agent: `ports: ["192.168.100.11:28080:8080"]` |

---

## Security notes

- **No secret is in any file here.** Credentials come from the environment
  only (`os.environ/` in LiteLLM, `${env.*}` in llama-swap). `.env.sample`
  contains the literal word `CHANGEME`. `.env` must never be committed.
- **No credentials in URLs.** `DATABASE_URL` is assembled by compose from
  `POSTGRES_USER` / `POSTGRES_PASSWORD`, so the password exists in one place.
- **Fail closed.** Every required variable is declared `${VAR:?...}` in compose,
  and llama-swap refuses to start if `GX_SWAP_API_KEY` is unset.
- **Loopback by default.** LiteLLM, llama-swap, `gx-mini` and Postgres all
  publish to `127.0.0.1`. To reach the gateway from another machine, bind it to
  the **Tailscale** address — not the LAN, and not `0.0.0.0` by reflex. The
  LiteLLM admin UI shares port 4000 with the API, so exposing one exposes both.
- **`allow_model_creation: false`, `store_model_in_db: false`.** The alias set is
  fixed by a reviewed file in git; a UI session cannot add an unaudited upstream.
- **Privacy-safe logging.** `turn_off_message_logging` and
  `redact_user_api_key_info` are on: prompts and completions are never written to
  logs or the database. Only metadata is retained.
- **llama-swap mounts `/var/run/docker.sock`.** That is root-equivalent on the
  host and is unavoidable for a component that spawns model containers. It is
  contained by the loopback bind and the bearer token. Do not publish `:28080`
  to the LAN.
- **Background health checks are off.** If LiteLLM probed every model, llama-swap
  would cold-start all of them at once and exhaust unified memory.
- **No silent model substitution.** `num_retries: 0`, `fallbacks: []`,
  `context_window_fallbacks: []`, `content_policy_fallbacks: []`. A failed
  request returns an error. This matters most for `gx-max`, which must never be
  answered by a different model.

---

## Things that will bite you

- **No `nvidia` docker runtime on gx10-01.** `docker info` lists `runc` only;
  GPUs come from CDI (`/var/run/cdi/nvidia.yaml`). Every spawned container uses
  `--device nvidia.com/gpu=all`, matching the live `gx-max-rank0`. Do **not**
  "fix" these to `--runtime nvidia --gpus all` — that fails on this node.
- **`container_name: gx-llama-swap-node01` is load-bearing.** `node01.yaml`
  spawns models with `--network container:gx-llama-swap-node01` (macro
  `swap_netns`). Rename one and you must rename the other.
- **llama-swap schema version.** These configs use the current schema, where
  group routing lives under `routing.router.settings.groups`. Older releases used
  a top-level `groups:` key. If the deployed binary rejects the `routing:` block,
  the correct fix is to **delete that whole section** — llama-swap then falls
  back to one model at a time, which is strictly *more* conservative than the
  documented budget. Never "fix" it by loosening the groups. Validate with
  `llama-swap -config <file> -dry-run` before restarting.
- **`docker compose down` does not stop model containers.** They are spawned as
  siblings via the docker socket, not as compose services. Unload them through
  llama-swap's `/unload` endpoint first.
- **Never point `gx-max` at `:30000`.** It bypasses the orchestrator's
  acquisition/drain and can collide with a live distributed job across both
  nodes.
