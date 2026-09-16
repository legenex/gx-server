# Troubleshooting

## Errors / troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| `401` from the gateway | missing or wrong key | check `Authorization: Bearer …` |
| `400 … model` | alias typo | use one of the seven `gx-*` names |
| `429` | more than 32 requests in flight | back off and retry |
| Request hangs for minutes on `gx-fast` / `gx-reason` | cold start | wait; set client timeout ≥ 10 min; or LOAD it first |
| `503 gx_max_unavailable … admission REFUSED` | a node is not clean (memory, swap, pressure, another resident) | read the reason; check Runtime; retry when clear |
| `503 gx_max_unavailable … exited 3/4/5` | a rank died, safety abort, or gx10-02 unreachable during load | check Jobs → lifecycle output and the rank logs; the unwind already ran |
| gx-mini/gx-fast/gx-reason show "drained" | gx-max owns the cluster | release gx-max (Models → gx-max → Unload) |
| gx-image returns `503` + `Retry-After` | another generation holds the slot | wait and retry |
| gx-image/gx-video `502` | ComfyUI rejected or failed the graph | Logs → media router / ComfyUI |
| gx-reason says "unavailable: node2 llama-swap unreachable" | gx10-02 down or its llama-swap stopped | Cluster page; Settings → Restart llama-swap (gx10-02) |
| Dashboard shows HEADs differ | a push is pending or gx10-02 has not reconciled | wait a minute; Settings → Reconcile gx10-02 |
| Everything in the UI is stale | backend restarting or SSH to gx10-02 slow | the page retries; check `systemctl --user status gx-control-ui` |

Quick checks from gx10-01:

```bash
curl -sS http://127.0.0.1:4000/health/liveliness                 # gateway alive
curl -sS http://127.0.0.1:18900/health/detailed | python3 -m json.tool
curl -sS http://192.168.100.11:18800/health | python3 -m json.tool
python3 -m gx_orchestrator.status_cli   # from legenex/orchestrator
free -g; swapon --show; cat /proc/pressure/memory
```

## Admin / recovery

**After gx-max leaves the cluster half-drained** (for example after an
interrupted release): Settings → **Restore normal workloads**, or

```bash
legenex/lifecycle/restore-normal.sh
```

**A rank is stuck or a load hangs:** Models → gx-max → **Force release**
(type `FORCE RELEASE`). This calls the orchestrator with `force=true`, which
stops both ranks, clears the ledger and restores normal workloads.

**Verify nothing is left behind after gx-max:**

```bash
docker ps --filter name=gx-max                                   # gx10-01: nothing
ssh legenex-02@gx10-02 'docker ps --filter name=gx-max; cat ~/.gx-guard/rank1-deadman.pid 2>/dev/null'
cat /srv/projects/gx-cluster/state/guard/node{1,2}-residency.json   # {}
```

**gx10-02 unreachable over Tailscale:** probe the fabric first
(`nc -vz 192.168.100.11 22`). If the kernel answers, the node is alive but
userspace may be starved; the deadman removes an orphaned rank 1 by itself.
Wait and re-probe before asking for physical access (see B-020).

**Service restarts** (all user units, no sudo):

```bash
systemctl --user restart gx-orchestrator.service      # refused by the UI during a gx-max transition
systemctl --user restart gx-control-ui.service
docker restart gx-litellm gx-llama-swap-node01
ssh legenex-02@gx10-02 'docker restart gx-media-router'
```

**Kernel pin check (read-only):**
`legenex/host/kernel-lock/verify-kernel-lock.sh` on each node. Expected:
13 passed, 0 warnings, 0 failed. **Never upgrade to kernel 7.0**; it breaks
RDMA memory registration and gx-max.

**Never, without a proven reason and human sign-off:** `apt upgrade`,
`apt autoremove`, kernel or firmware updates, `docker system prune -a`,
netplan/MTU/RDMA changes, `git push --force`.

## FAQ

**Is this one 256 GB machine?** No. Two independent 128 GB machines. Only
gx-max spans both, as two cooperating shards.

**Why did my gx-fast request make gx-max slower / fail?** It didn't: while
gx-max is running, gx-fast is drained and the gateway returns an error for it.

**Can gx-auto start gx-max?** No. It only uses gx-max when it is already
READY.

**Will a failed gx-max request be answered by another model?** Never. You get
HTTP 503.

**Why does gx-max take so long to load?** 163 GiB of weights loaded serially
onto two unified-memory nodes, plus engine warm-up. About 9 minutes.

**How long does gx-max stay loaded?** Until 30 minutes pass without a
request, or someone releases it.

**Why is swap nearly full during a gx-max load?** Expected: the loader's
staging buffers spill to `/swapfile-sglang` and are released once the
weights are on the GPU.

**Can I use the fabric addresses from my laptop?** No; they are a direct
cable between the two nodes.

**Where do I get an API key?** From the administrator (a LiteLLM virtual key).

**How do I reset the control-UI password?** On gx10-01:
`legenex/control-ui/scripts/gx-ui-passwd`. See the Control UI page.

**Does the control UI keep my prompts?** No. The playground sends them to
the gateway and shows the answer; only pass/fail, latency and a 200-character
excerpt of the last result per alias are stored (in
`/srv/projects/gx-cluster/state/control-ui/`). The gateway does not log
prompt bodies.
