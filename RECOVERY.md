# Recovery

What to do when something is broken. Ordered from "most likely" to "worst".

---

## 0. Orient first

```bash
cd /home/legenex/Documents/Projects/Server/gx-cluster
./legenex/lifecycle/gx-max-status.sh
curl -s localhost:18900/health/detailed | python3 -m json.tool
docker ps --format '{{.Names}}\t{{.Status}}'
ssh legenex-02@gx10-02 'docker ps --format "{{.Names}}\t{{.Status}}"; free -g'
```

Read `CURRENT_STATE.md` and `coordination/BLOCKERS.md` before changing anything.

## 1. The gateway is down

```bash
cd legenex/gateway
docker compose --env-file .env -f docker-compose.gateway.yml up -d
until curl -fsS localhost:4000/health/liveliness; do sleep 2; done
```

If LiteLLM will not start, check `docker logs gx-litellm`. A missing or renamed
`.env` is the usual cause — every secret comes from there and the compose file
fails closed if one is absent.

**The `.env` is not in git** (by design). If it is lost, regenerate it; the
required keys are listed in `legenex/gateway/.env.sample`.

## 2. One alias is broken, the rest work

The gateway and routing are fine; a single model process is not starting.

```bash
docker logs gx-llama-swap-node01 2>&1 | grep -v 'GET /health' | tail -20
```

Set `logLevel: debug` in the relevant `legenex/gateway/llama-swap/nodeNN.yaml`,
restart llama-swap, retry the request, then copy the `Executing start command`
line out of the log and run it by hand. That is the only way to see the child
process's stderr.

Clear a stuck model:

```bash
docker rm -f gx-mini gx-fast              # node 1
ssh legenex-02@gx10-02 'docker rm -f gx-reason'   # node 2
```

## 3. gx-max will not come up

```bash
./legenex/lifecycle/gx-max-status.sh
tail -50 /srv/logs/gx-max-rank0.log
ssh legenex-02@gx10-02 'tail -50 ~/gx-max-rank1.log'
```

Checks, in order:

1. **Kernel.** `uname -r` on BOTH nodes must be `6.17.0-1032-nvidia`. If either
   has moved to 7.0, that is the fault — RDMA memory registration fails during
   FlashInfer autotune. Boot back into the pinned kernel.
2. **Fabric.** `ping -c2 192.168.100.11` and `ping -c2 192.168.101.11`, then
   `rdma link show` — both `f0` ports must be `ACTIVE / LINK_UP`.
3. **Memory.** Both nodes need ~90 GiB free. Stop conflicting containers.
4. **Model present on both nodes** at
   `/srv/models/deepseek/DeepSeek-V4-Flash-0731-NVFP4`.
5. **Image present on both nodes**: `lmsysorg/sglang:dev-v4f-2dgx-v2`.

Force a clean slate:

```bash
./legenex/lifecycle/gx-max-stop.sh --force --no-restore
docker rm -f gx-max-rank0; ssh legenex-02@gx10-02 'docker rm -f gx-max-rank1'
./legenex/lifecycle/gx-max-start.sh
```

**Do not** "fix" gx-max by switching it to vLLM or changing the model. That is a
locked decision — see ARCHITECTURE.md L-6.

## 4. A node is wedged / was rebooted

Nothing in this stack auto-starts models at boot, by design.

```bash
# node 1
cd legenex/gateway && docker compose --env-file .env -f docker-compose.gateway.yml up -d
cd ../orchestrator && setsid env GX_GATEWAY_KEY="$KEY" python3 -m gx_orchestrator.server \
  >> /srv/logs/gx-orchestrator.log 2>&1 < /dev/null &

# node 2
ssh legenex-02@gx10-02 'cd ~/gx-gateway && docker compose --env-file .env -f docker-compose.node02.yml up -d'
```

Or simply `./legenex/lifecycle/restore-normal.sh`, which does both and waits for
health.

**After any reboot, check the CDI spec exists** — it lives on tmpfs and is
regenerated each boot. Without it every GPU container fails at once:

```bash
ls -l /var/run/cdi/nvidia.yaml && nvidia-ctk cdi list | head
```

## 5. Everything is broken / start from scratch

The repository is the source of truth. A fresh agent or operator can rebuild
from `ARCHITECTURE.md` + `MODELS.md` + this file.

```bash
git -C /home/legenex/Documents/Projects/Server/gx-cluster log --oneline -10
git status --short
```

Order of restoration:

1. Verify kernel `6.17.0-1032-nvidia` on both nodes and the ConnectX rails.
2. Recreate `legenex/gateway/.env` from `.env.sample`.
3. Bring up the node-1 gateway, then the orchestrator, then node-2 llama-swap.
4. `legenex/tests/acceptance.sh` and fix whatever fails.
5. Only then try `GX_RUN_SLOW=1 legenex/tests/acceptance.sh`.

## Rollback

Every change is a git commit on `legenex-dual-gx10`.

```bash
git log --oneline -20
git revert <sha>          # preferred: keeps history honest
git checkout <sha> -- <path>   # restore one file
```

Model weights are **not** in git. They live under `/srv/models` on each node and
are re-fetchable with `legenex/scripts/download-model.sh <hf-id> <dest>`, which
refuses to run if it would breach the free-space reserve.

Rolling gx-max back to its exact verified argument vector: set
`GXMAX_ENABLE_METRICS=0` in `legenex/lifecycle/gx-max.conf`. That reproduces the
2026-09-14 configuration byte-for-byte.

## What needs a human

See `coordination/BLOCKERS.md`. The ones that matter most:

- **B-001** the kernel pin has no `apt-mark hold`, and kernel 7.0 is still
  installed on both nodes. Needs root.
- **B-002** no sudo on either node.
- **B-003** SGLang `:30000` is bound `0.0.0.0` with no auth.
