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

## Node addresses

| Node | LAN | Tailscale (management only) | ConnectX rail A | ConnectX rail B |
|---|---|---|---|---|
| gx10-01 (`legenex`) | `10.60.21.37` | `100.105.214.61` | `192.168.100.10` | `192.168.101.10` |
| gx10-02 (`legenex-02`) | `10.60.21.41` | `100.73.238.4` | `192.168.100.11` | `192.168.101.11` |

Mac management device (Tailscale): `100.104.35.71`.

SSH between nodes uses the Tailscale hostname/user, never the fabric address:
`ssh legenex-02@gx10-02` from node 1, `ssh legenex@gx10-01` from node 2. The
LAN addresses above are for local health checks only, not routine SSH.

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
legenex/scripts/gx-status.sh              # one-shot: both nodes + every alias, human or --json
docker ps --format '{{.Names}}\t{{.Status}}'                       # node 1
ssh legenex-02@gx10-02 'docker ps --format "{{.Names}}\t{{.Status}}"'  # node 2
curl -s localhost:18900/health/detailed | python3 -m json.tool
```

llama-swap loads models **on demand** and unloads them on an idle TTL, so an
absent container is normal, not a fault. `/health/detailed` and `gx-status.sh`
report a real per-tier state (`ready`/`stopped`/`loading`/`queued`/
`unavailable`/`failed`) derived from each tier's own upstream, not just from
whether the gateway itself is up — a tier whose real upstream (e.g. node 2's
llama-swap) is unreachable is never reported as healthy.

## Launching a model container by hand — do not use a bare `docker run`

Any medium/large/exclusive workload (gx-fast, gx-reason, gx-max, ComfyUI, or
a one-off diagnostic) must go through the admission guard, not a bare
`docker run`. Bypassing it is exactly what wedged node 2 (see
`coordination/BLOCKERS.md` B-012 and `ARCHITECTURE.md` §9).

```bash
legenex/lifecycle/gx-safe-run.sh <node> <workload-name> <class> <estimated-gib> -- <docker run ...>
legenex/lifecycle/resource-guard.sh status <node>     # what does the ledger think is resident?
legenex/lifecycle/resource-guard.sh check <node> <name> <class> <gib>   # dry-run the admission math
```

llama-swap-managed tiers (gx-mini, gx-fast, gx-reason) don't need this
directly — llama-swap's own group exclusivity already serialises them, and
`gx-max-start.sh`/`gx-max-stop.sh` already route through the guard
internally. It matters for anything started outside that path.

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
cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'   # 116 tests

# lifecycle/resource-guard bash tests (no cluster needed)
cd legenex/lifecycle && python3 -m unittest discover -s tests -p 'test_*.py'  # 9 tests

# media router unit/protocol tests (no GPU, no node 2 needed)
cd legenex/media/router && ./qa.sh                                            # 43 tests

# end-to-end acceptance suite (needs the live gateway)
legenex/tests/acceptance.sh                 # fast tiers only
GX_RUN_SLOW=1 legenex/tests/acceptance.sh   # include gx-max + lifecycle
legenex/tests/acceptance.sh mini fast       # just these

# node2-dependent, run only once node 2 is confirmed clean
legenex/scripts/gx-reason-diagnose.sh       # B-011 GPU-vs-CPU comparison, unload-gated
legenex/tests/gx-max-validate.sh            # full acquire->serve->release->restore cycle
```

## Memory rules of thumb

Each node has 121 GiB of **unified** memory: CPU and GPU allocations compete.
The admission guard (`ARCHITECTURE.md` §9) enforces a 30 GiB reserve floor
against these automatically — this table is for human intuition, not the
authoritative numbers (those live in `resource_guard.WORKLOAD_SIZING`).

| Tier | Node | Resident |
|---|---|---|
| gx-mini | 1 | ~10 GiB |
| gx-fast | 1 | ~22-25 GiB model, up to ~86 GiB pool |
| gx-reason | 2 | ~95 GiB measured (B-011; design budget said 78 GiB — reconcile when next touched). Owns node 2 exclusively — never co-schedule |
| gx-max | 1+2 | ~90 GiB ceiling per rank |

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

**gx-max says "not enough free memory" / admission refused.**
Something is still holding memory. Check `legenex/lifecycle/resource-guard.sh
status <node>` and `docker ps` on both nodes. `GXMAX_FORCE_DRAIN=1` no longer
bypasses the admission guard (as of 2026-09-14) — a hard guard an env var
can switch off is not a hard guard. If the ledger disagrees with reality
(e.g. a container was removed outside the guarded path), it self-reconciles
against `docker inspect` on the next check; if it's still wrong, clear the
stale entry with `legenex/lifecycle/resource-guard.sh` (see its `release`
subcommand) rather than editing the ledger file by hand.

## Stale RDP session on gx10-02

gx10-02 also serves GNOME remote desktop (RDP, port 3389) for occasions where
a human needs a graphical session, not just SSH. GDM automatic login is
**deliberately disabled** there:

```text
AutomaticLoginEnable = false
# AutomaticLogin = legenex-02
```

This avoids a stale local `seat0` X11/Wayland session colliding with a remote
RDP login attempt. If RDP accepts the TCP connection but the desktop will not
connect:

```bash
# on gx10-02 over SSH
loginctl list-sessions
```

If `legenex-02` has a stale local graphical session, terminate **only** that
session, never the SSH session, and never run
`loginctl terminate-user legenex-02` while relying on SSH to do it (that can
tear down your own connection too). Restart GDM only if needed. The desired
end state is: GDM login screen present, no auto-logged-in `legenex-02` seat0
session.

An open TCP port (22 or 3389) only proves a listener exists, not that the
service is healthy — see `RECOVERY.md` for the application-level SSH-banner
check, which is the same signature this stale-session issue can be confused
with.

## Never do this

- Upgrade the kernel. Both nodes are pinned to `6.17.0-1032-nvidia`; kernel 7.0
  breaks RDMA memory registration and kills gx-max.
- `apt upgrade`, `apt autoremove`, `docker system prune -a`, firmware updates.
- Change MTU, Netplan, RDMA config or routing without evidence of a fault.
- Route model traffic over Tailscale.
- Start gx-max at boot.
