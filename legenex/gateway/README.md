# gx cluster — gateway (V4.1 rebuild, 2026-09-27)

The OpenAI-compatible public gateway for the two-node DGX Spark cluster, and
nothing else. This directory is **configuration only**: no orchestration
logic, no model lifecycle, no routing intelligence.

## What changed in the V4.1 rebuild

- **Public aliases are now exactly two**: `gx-max` and `gx-auto`, both resolving
  to the lead's orchestrator on `:18900`. The orchestrator owns the request
  queue, profile/reasoning selection, and the DeepSeek V4.1 Flash EXL3 runtime
  (Mia kit, submodule `mia-dsv41`, served model id `DeepSeek-v4.1-Flash-EXL3`,
  OpenAI API loopback `:8888` on the head node).
- **llama-swap is RETIRED (2026-09-27).** The old model stack — `gx-mini`,
  `gx-code`, `gx-fast`, `gx-reason` and every old checkpoint — is deleted from
  both nodes, and `llama-swap/` (node01.yaml, node02.yaml, env/, dflash2/),
  `docker-compose.node02.yml` and `deploy-node2.sh` are deleted from the tree.
  All of it lives in git history under tag
  **`pre-deepseek-v41-rebuild-20260927`**. Model lifecycle is owned by the
  orchestrator → `mia-dsv41`; nothing here starts, stops or probes a model.
- Registry: `legenex/models/registry.json` (schema 2) is the single source of
  truth for nodes, runtimes, model packs, aliases, profiles and reasoning
  levels. Loader/validator: `legenex/models/registry.py` (+ tests).

## Files

| File | What it is | Runs on |
|---|---|---|
| `litellm/config.yaml` | The single user-facing gateway. Defines exactly the two aliases `gx-max`, `gx-auto`, both → `http://host.docker.internal:18900/v1`. | node 1 |
| `litellm/gx_hooks/gx_budget_hook.py` | Pre-call hook: forwards `X-GX-*` attribution headers to the orchestrator, applies a context-budget guard for both aliases, writes one privacy-safe metrics line per request. | node 1 (in container) |
| `docker-compose.gateway.yml` | Brings up LiteLLM + Postgres. Nothing else. | node 1 |
| `gx-gateway-ts-proxy.py` (+ `.sh`, `systemd/`) | Tailscale front door on :4000; filters `/v1/models` to the two public aliases. | node 1 |
| `.env.sample` | Every variable documented. No real secrets, ever. | node 1 |

Separation of concerns, deliberately:

- **LiteLLM** decides *which upstream a name maps to*. It does not pick tiers,
  profiles or reasoning, and has no fallbacks — ever.
- **The lead's orchestrator** (`:18900`) owns the queue, profile/reasoning
  selection and the model lifecycle (DOWN→ACQUIRING→READY→RELEASING via
  `mia-dsv41`). Neither file here implements any of that.

## Port map — node 1 (`gx10-01`, control)

| Host port | Bind (default) | Service | Owner |
|---|---|---|---|
| `4000` | `127.0.0.1` (+ Tailscale via ts-proxy) | **LiteLLM gateway** — the only endpoint users need | this dir |
| `15432` | `127.0.0.1` | Postgres for LiteLLM | this dir |
| `18900` | host loopback | lead's orchestrator (`gx-max`, `gx-auto`) | **lead** |
| `8888` | host loopback | Mia kit OpenAI API — **never public, never pointed at from here** | **lead** |

Model/NCCL traffic between the nodes uses the ConnectX/RoCE fabric
(`192.168.100.x` / `192.168.101.x`) with `NCCL_IB_GID_INDEX=3` and both rails
(registry `fabric` block). Tailscale carries management and SSH only.

## How each alias resolves

| Alias | Gateway upstream | Mode | Notes |
|---|---|---|---|
| `gx-max` | `http://host.docker.internal:18900/v1` | direct | Explicit DeepSeek V4.1 Flash (uncensored). **No fallbacks.** |
| `gx-auto` | `http://host.docker.internal:18900/v1` | auto | Profile/reasoning selection is the orchestrator's autoroute. This gateway is a pass-through. |

`host.docker.internal` is mapped to the host gateway by
`docker-compose.gateway.yml`. From the host the orchestrator is
`127.0.0.1:18900`; from inside the LiteLLM container `127.0.0.1` would be the
container itself, so the alias is required.

## Starting and stopping

All commands run as an unprivileged user in the `docker` group. No `sudo`.

```bash
cd legenex/gateway
cp .env.sample .env && chmod 600 .env && $EDITOR .env   # fill in every REQUIRED value

# The real file lives OUTSIDE the project tree (the tree is mounted into other
# services); legenex/gateway/.env is a symlink:
#   /srv/projects/gx-cluster/secrets/gateway.env
# Edit that file directly. Never `sed -i` the symlink.

# Validate without starting anything
docker compose -f docker-compose.gateway.yml config >/dev/null && echo OK

# Start the gateway (starts NO model)
docker compose -f docker-compose.gateway.yml up -d
docker compose -f docker-compose.gateway.yml ps
docker compose -f docker-compose.gateway.yml down
```

### Smoke tests (safe — none of these touch a model)

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness
curl -fsS -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models
```

`/v1/models` must list exactly two ids: `gx-max`, `gx-auto`.

## Security notes

- **No secret is in any file here.** Credentials come from the environment
  only (`os.environ/` in LiteLLM, `${VAR}` in compose). `.env.sample` contains
  the literal word `CHANGEME`. `.env` must never be committed.
- **No credentials in URLs.** `DATABASE_URL` is assembled by compose from
  `POSTGRES_USER` / `POSTGRES_PASSWORD`, so the password exists in one place.
- **Fail closed.** Every required variable is declared `${VAR:?...}` in compose.
- **Loopback by default.** LiteLLM and Postgres publish to `127.0.0.1`. To reach
  the gateway from another machine, bind it to the **Tailscale** address — the
  admin UI shares port 4000 with the API, so exposing one exposes both.
- **`allow_model_creation: false`, `store_model_in_db: false`.** The alias set
  is fixed by a reviewed file in git; a UI session cannot add an unaudited
  upstream.
- **Privacy-safe logging.** `turn_off_message_logging` and
  `redact_user_api_key_info` are on: prompts and completions are never written
  to logs or the database. The hook's metrics line carries metadata only
  (timing, token counts, `X-GX-*` attribution values — never prompt text).
- **Background health checks are off.** LiteLLM probing the orchestrator would
  add queue noise for no user request.
- **No silent model substitution.** `num_retries: 0`, `fallbacks: []`,
  `context_window_fallbacks: []`, `content_policy_fallbacks: []`,
  `disable_cooldowns: true`. A failed request returns an error. There is one
  model family; `gx-max` must never be answered by anything else (especially
  not the stock pack).
- **The docker-socket mount is gone** with llama-swap. Neither service in the
  compose file needs it — the previous root-equivalent surface no longer
  exists in this stack.
