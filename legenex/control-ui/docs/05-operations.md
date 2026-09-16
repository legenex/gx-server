# Operations

## Model loading / unloading

| Alias | LOAD does | UNLOAD does | Automatic behaviour |
|---|---|---|---|
| `gx-mini` | llama-swap starts it (seconds) | llama-swap stops it | resident; reloads on the next request |
| `gx-fast` | llama-swap starts vLLM on gx10-01 | llama-swap stops it | loads on demand; unloads after 30 min idle |
| `gx-reason` | llama-swap starts vLLM on gx10-02 | llama-swap stops it | loads on demand; unloads after 15 min idle |
| `gx-max` | orchestrator **acquire** (two-node takeover) | orchestrator **graceful release** | acquires on a direct request; releases after 30 min idle |
| `gx-image` / `gx-video` | — (first generation loads) | ComfyUI frees image **and** video weights | weights stay warm until freed |
| `gx-auto` | — | — | routing only |

In the control UI: **Models** → the alias card → LOAD / UNLOAD / RESTART.
Before LOAD the UI runs the same admission check as the lifecycle scripts and
refuses when it would leave less than 30 GiB free, or while gx-max owns the
cluster.

From a shell on gx10-01 (same operations):

```bash
set -a; . legenex/gateway/.env; set +a
# load gx-fast (returns when it is healthy)
curl -sS -H "Authorization: Bearer $GX_SWAP_API_KEY" http://127.0.0.1:28080/upstream/gx-fast/health
# unload gx-reason on node 2
curl -sS -X POST -H "Authorization: Bearer $GX_SWAP_API_KEY" http://192.168.100.11:28080/api/models/unload/gx-reason
# gx-max
curl -sS -X POST http://127.0.0.1:18900/lifecycle/gx-max/acquire -d '{}'
curl -sS -X POST http://127.0.0.1:18900/lifecycle/gx-max/release -d '{}'
curl -sS http://127.0.0.1:18900/lifecycle/gx-max/status
```

> **Warning** Never start `gx-max-rank0` or `gx-max-rank1` with `docker run`
> or `docker start`. Only the orchestrator lifecycle (or
> `legenex/lifecycle/gx-max-start.sh` under it) arms the admission check,
> locks, deadman, watcher and unwind.

## gx-max explained

**What it is.** DeepSeek V4 Flash, NVFP4, 163 GiB of weights, split across
two GPUs with tensor parallelism (TP=2). Each rank holds half the model, so
each node gives up almost all of its memory to it. That is why gx-max is a
*takeover*, not "one more model".

**Lifecycle (sanctioned path only):**

1. **queued** — a direct gx-max request or LOAD arrives; concurrent callers
   wait behind one acquisition.
2. **preflight** — model files and image exist on both nodes; both RoCE rails
   answer.
3. **draining** — gx-mini, gx-fast, llama-swap (gx10-01) and gx-reason,
   ComfyUI, the media router, llama-swap (gx10-02) are stopped gracefully.
4. **admission** — clean-start policy on both nodes (see Resource safety).
   A refusal restores everything that was drained.
5. **loading rank 1** — rank 1 starts on gx10-02 under node 2's lock; the
   node-2 deadman is armed.
6. **loading rank 0** — rank 0 starts on gx10-01 under node 1's lock.
7. **warming** — weights load, CUDA graphs capture. Live safety runs on both
   nodes.
8. **ready / serving** — `/health` answers; the rank 0 watcher is armed;
   requests are served on `127.0.0.1:30000` behind the orchestrator.
9. **release** (graceful) — waits for in-flight requests, stops rank 0 then
   rank 1, clears the ledger, measures memory, then **restores** the normal
   control planes. Models come back on demand.

**Why it takes about 9 minutes cold.** Loading ~82 GiB of weights per rank
onto unified memory, serially (to limit the host-memory spike), plus engine
start-up and CUDA graph capture. Measured cold loads: 508–559 s.

**Why normal models are unloaded.** Each rank needs almost the whole node.
Leaving gx-fast or gx-reason loaded would push the load into the kernel OOM
killer. The drain is automatic and reversed on release or failure.

**The load transient is expected.** On gx10-01, `MemAvailable` briefly drops
to about 2.5–3.3 GiB and swap can reach the full ~64 GiB for a few seconds;
gx10-02 peaks at about 51–55 GiB of swap. Both settle to roughly 15–18 GiB
free once serving. The safety layer aborts only on a real OOM or a
*sustained* problem (B-023 tracks the thin node-1 margin).

**gx-max never silently downgrades.** If acquisition is refused or fails, the
caller gets HTTP 503 with the reason. No other model answers in its place.

**gx-auto does not acquire gx-max.** Only a direct `gx-max` request, the
Models page LOAD button, or the orchestrator acquire endpoint can start it.

**Evidence that both rails carry traffic:** the Cluster page and the gx-max
card show RDMA byte counters for both ConnectX ports; they climb by
gigabytes during load and generation.

## Git sync

* **gx10-01 is the only writer.** Its checkout
  (`~/Documents/Projects/Server/gx-cluster`) auto-commits changes after 45
  quiet seconds, behind a path gate and a gitleaks secret scan, and pushes to
  `https://github.com/legenex/gx-server` (`main`). The repository is
  **public**: never put a secret in a tracked file.
* **gx10-02 is a pull-only mirror.** Its push URL is disabled. It reconciles
  right after each push and every minute, saving any local drift as evidence
  before resetting to `origin/main`.
* **Daily integrity audit** on both nodes.
* The Dashboard and Settings pages show all three HEADs (gx10-01, GitHub,
  gx10-02) and whether they match. Settings has "Reconcile gx10-02" and "Run
  integrity audit".

| Unit | Node | Purpose |
|---|---|---|
| `gx-git-watch.service` | gx10-01 | change watcher (45 s quiet) |
| `gx-git-autosync.timer` | gx10-01 | 1-minute fallback |
| `gx-git-daily-audit.timer` | both | daily audit |
| `gx-git-reconcile.timer` | gx10-02 | 1-minute reconcile |

After a branch switch or reset on gx10-01, run
`docker restart gx-llama-swap-node01 gx-litellm` (their bind mounts pin old
files). Details: `ops/git-sync/README.md`.

## Log locations

All of these are also available, redacted, on the **Logs** page.

| Log | Where |
|---|---|
| Orchestrator | gx10-01 `/srv/logs/gx-orchestrator.log` |
| gx-max acquire/release output | gx10-01 `/srv/logs/gx-max-lifecycle.log` |
| gx-max rank 0 | gx10-01 `/srv/logs/gx-max-rank0.log` |
| gx-max rank 1 | gx10-02 `~/gx-max-rank1.log` |
| gx-max safety samples | gx10-01 `/srv/logs/gx-max-safety-node1-*.tsv`, gx10-02 `~/gx-max-node2-mem.tsv` |
| rank 0 watcher | gx10-01 `/srv/logs/gx-max-rank0-watch.log` |
| rank 1 deadman | gx10-02 `~/gx-max-rank1-deadman.log` |
| LiteLLM | `docker logs gx-litellm` (gx10-01) |
| llama-swap | `docker logs gx-llama-swap-node01` / `gx-llama-swap-node02` |
| Media router / ComfyUI | `docker logs gx-media-router` / `gx-comfyui` (gx10-02) |
| Git sync | `/srv/logs/gx-git-sync/` on both nodes |
| Hostwatch | `/srv/logs/gx-hostwatch.log` on both nodes |
| Control UI | gx10-01 `/srv/logs/gx-control-ui/control-ui.log` and `audit.log` |
