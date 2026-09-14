# Operations

Day-to-day runbook for the two-node gx-cluster. Everything here runs **without
root** — Docker plus `systemctl --user`.

---

## The one endpoint

```
http://127.0.0.1:4000/v1        (LiteLLM gateway, node 1)
```

Auth is a bearer token: `LITELLM_MASTER_KEY` from `legenex/gateway/.env`.

```bash
KEY=$(grep '^LITELLM_MASTER_KEY=' legenex/gateway/.env | cut -d= -f2-)

curl -s http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gx-auto","messages":[{"role":"user","content":"hello"}]}'
```

Aliases: `gx-mini`, `gx-fast`, `gx-reason`, `gx-max`, `gx-auto`, `gx-image`,
`gx-video`. Clients never need to know which node serves which.

## Port map

| Port | Service | Node | Bind |
|---|---|---|---|
| 4000 | LiteLLM gateway | 1 | 127.0.0.1 |
| 18900 | orchestrator (gx-auto + gx-max lifecycle) | 1 | 127.0.0.1 + 172.17.0.1 |
| 28080 | llama-swap | 1 | 127.0.0.1 |
| 19001 | gx-mini direct (llama.cpp) | 1 | 127.0.0.1 |
| 15432 | Postgres (LiteLLM) | 1 | 127.0.0.1 |
| 30000 | SGLang gx-max | 1 | 0.0.0.0 — see BLOCKERS B-003 |
| 28080 | llama-swap | 2 | 192.168.100.11 + 127.0.0.1 |

## Starting and stopping

### Normal state (node 1 gateway + lifecycle)

```bash
cd legenex/gateway
docker compose --env-file .env -f docker-compose.gateway.yml up -d
```

### Orchestrator

```bash
cd legenex/orchestrator
setsid env GX_GATEWAY_KEY="$KEY" python3 -m gx_orchestrator.server \
  >> /srv/logs/gx-orchestrator.log 2>&1 < /dev/null &
```

### Node 2

```bash
ssh legenex-02@gx10-02 'cd ~/gx-gateway && docker compose --env-file .env -f docker-compose.node02.yml up -d'
```

### Everything back to normal after gx-max

```bash
./legenex/lifecycle/restore-normal.sh
```

## gx-max (both nodes)

gx-max takes over **both** nodes. It is never started at boot.

```bash
./legenex/lifecycle/gx-max-status.sh        # what is the engine doing?
./legenex/lifecycle/gx-max-start.sh         # acquire both nodes
./legenex/lifecycle/gx-max-stop.sh          # graceful drain + release + restore
./legenex/lifecycle/gx-max-stop.sh --force  # do not wait for in-flight work
```

You normally do **not** run these by hand: sending a request to the `gx-max`
alias makes the orchestrator acquire the cluster automatically, and the idle TTL
releases it again.

```bash
curl -s localhost:18900/lifecycle/gx-max/status | python3 -m json.tool
curl -s -X POST localhost:18900/lifecycle/gx-max/acquire
curl -s -X POST localhost:18900/lifecycle/gx-max/release -d '{}'
```

Timings measured on this hardware:

| Phase | Cold | Warm page cache |
|---|---|---|
| weight load | ~400 s | ~30 s |
| total to ready | ~630 s | ~240 s |
| release + reclaim | ~70 s | ~70 s |

## Which model is loaded right now?

```bash
docker ps --format '{{.Names}}\t{{.Status}}'                       # node 1
ssh legenex-02@gx10-02 'docker ps --format "{{.Names}}\t{{.Status}}"'  # node 2
curl -s localhost:18900/health/detailed | python3 -m json.tool
```

llama-swap loads models **on demand** and unloads them on an idle TTL, so an
absent container is normal, not a fault.

## Logs

| What | Where |
|---|---|
| gx-max rank 0 | `/srv/logs/gx-max-rank0.log` |
| gx-max rank 1 | `gx10-02:~/gx-max-rank1.log` |
| orchestrator + routing decisions | `/srv/logs/gx-orchestrator.log` |
| gateway | `docker logs gx-litellm` |
| llama-swap | `docker logs gx-llama-swap-node01` |
| a model | `docker logs gx-mini` / `gx-fast` / `gx-reason` |

Routing decisions are JSON lines under the `gx.routing` logger:

```bash
grep gx.routing /srv/logs/gx-orchestrator.log | tail -5 \
 | python3 -c "import sys,json;[print(json.loads(l.split('gx.routing ',1)[-1])) for l in sys.stdin]"
```

## Tests

```bash
# orchestrator unit tests (no cluster needed)
cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'

# end-to-end acceptance suite
legenex/tests/acceptance.sh                 # fast tiers only
GX_RUN_SLOW=1 legenex/tests/acceptance.sh   # include gx-max + lifecycle
legenex/tests/acceptance.sh mini fast       # just these
```

## Memory rules of thumb

Each node has 121 GiB of **unified** memory: CPU and GPU allocations compete.

| Tier | Node | Resident |
|---|---|---|
| gx-mini | 1 | ~3 GiB |
| gx-fast | 1 | ~20 GiB model, ~84 GiB pool at 0.66 |
| gx-reason | 2 | ~77 GiB (owns node 2 — do not co-schedule) |
| gx-max | 1+2 | ~93 GiB per node |

**vLLM cannot load a checkpoint bigger than roughly 55 GiB on these nodes** —
it needs `pool + checkpoint` in anonymous memory. See BLOCKERS.md B-009. Use
llama.cpp (mmap, file-backed) above that size.

## Common problems

**A tier returns "upstream command exited prematurely".**
Read the llama-swap log with `logLevel: debug` to get the exact spawn command,
then run that command by hand — llama-swap does not surface the child's stderr.
Historically this has meant a bad flag, a quoting problem, or a leftover
container squatting the model's `--name` (now handled automatically).

**A model returns empty `content`.**
Check for `reasoning_content` in the response. The Qwen models put chain-of-
thought there and will burn the whole output budget on it unless the chat
template is told not to think.

**gx-max will not start.**
`gx-max-status.sh` first. Preflight refuses to start when either ConnectX rail
is down, the model directory is missing on a node, or free memory is under
90 GiB — the message says which.

**gx-max says "not enough free memory".**
Something is still holding memory. Check `docker ps` on both nodes. Override
only if you are sure: `GXMAX_FORCE_DRAIN=1 ./legenex/lifecycle/gx-max-start.sh`.

## Never do this

- Upgrade the kernel. Both nodes are pinned to `6.17.0-1032-nvidia`; kernel 7.0
  breaks RDMA memory registration and kills gx-max.
- `apt upgrade`, `apt autoremove`, `docker system prune -a`, firmware updates.
- Change MTU, Netplan, RDMA config or routing without evidence of a fault.
- Route model traffic over Tailscale.
- Start gx-max at boot.
