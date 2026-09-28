# Mission Handoff — GX10 DeepSeek V4.1 Flash Clean Rebuild

If context is compacted or a new agent takes over:

1. Read this file, then state/PROGRESS.md, state/BACKLOG.md, state/DECISIONS.md,
   state/EVIDENCE.md, state/BLOCKERS.md.
2. The mission brief lives in the conversation that started 2026-09-27
   ("GX DUAL GX10 DEEPSEEK V4.1 FLASH CLEAN REBUILD"); key points mirrored in
   DECISIONS.md D-M1..M4.
3. Canonical repo: /home/legenex/Documents/Projects/Server/gx-cluster (this
   state/ dir). gx10-01 is the only Git writer (autosync → PUBLIC repo — never
   put secrets in tracked files). gx10-02 is pull-only.
4. Environment: no sudo; Docker + systemctl --user; CDI GPUs; SSH node2 =
   ssh legenex-02@gx10-02; ConnectX rails 192.168.100.10/11 + 192.168.101.10/11.
5. Protected: /home/legenex/Documents/Projects/gx-backup, Documents/Backups/GX/
   ZIPs, git tag pre-deepseek-v41-rebuild-20260927 on both remotes.
6. Do not raw-`docker stop` model containers — use the resource-guard /
   orchestrator path while old stack still runs (D-036 discipline applies
   until the old stack is decommissioned in PHASE 7-8).

## Phase 15 headless transition — what is happening (2026-09-27 16:5x)
The head node cannot boot the model with the desktop running (needs 111.5 GiB
MemAvailable; 106 with desktop, ~112+ without). Per mission Phase 12, the detached
runner ~/bin/dsv41-baseline-launch.sh:
1. terminates ONLY the graphical wayland session (VS Code desktop, Chrome, gnome die —
   the DROID mission process dies with it, by design and expected),
2. waits for memory, runs mia-dsv41/start.sh (stock baseline; rsync to worker ~387G,
   image ship, two-rank boot ~25 min),
3. verifies health + a 17*19=323 completion probe,
4. writes /srv/logs/dsv41-baseline-READY or -FAILED; full log /srv/logs/dsv41-first-launch.log.
Everything else survives: SSH (session 3), Tailscale, systemd user services
(orchestrator :18900, ts-proxy :4000), LiteLLM containers, all user app containers
(temporarily stopped: gigpilot, financialos, pageflo, open-webui, gx-computer — docker start them after).
GUI restoration: console login at gdm (or reboot; model must be stopped first).
RESUME THE MISSION once the marker appears (or after ~40 min regardless — check the
log tail). Next steps on resume: Phase 16-17 tests + benchmark, then Phase 18-20.


## 2026-09-27 17:55 — Gate A in flight (fast-track mode)
- Headless transition DONE (wayland session 4 terminated; MemAvailable 116.1G passed
  the 111.5G preflight). Headless is now the standing state; GUI restore = console
  login at gdm (or reboot after stopping the model).
- Baseline launch RUNNING detached: log /srv/logs/dsv41-first-launch.log; markers
  /srv/logs/dsv41-baseline-READY or -FAILED. Timeline: image built+shipped OK (SM121
  ext compile), EXL3 pack rsync to worker ~113MB/s (ETA ~18 min from 17:55), then
  engram 190G sync (~28 min), then two-rank boot (~25 min). READY expected ~19:05.
- ON READY: Gate A verification = ops/bench/run_bench.py --suite quick (TTFT +
  decode tok/s, direct :8888), then stop via mia-dsv41/stop.sh (verify memory return
  both nodes), then restart via systemctl --user start gx-dsv41-bootstrap.service
  (or ./start.sh) to prove clean restart. Record evidence in EVIDENCE.md.
- ON FAILED: diagnose from the log tail (rsync/rank boot/OOM), fix, re-dispatch via
  systemctl --user start gx-dsv41-bootstrap.service (unit wraps the same script,
  NOT enabled at boot).
- Then P1: stock uncensor battery (ops/uncensor-verify/verify_uncensored.py) →
  delete stock pack (head disk 94%) → download dealignai CRACK 210G to
  /srv/models/dsv41/uncensored → update registry path/env → load → battery compare →
  production select (see DECISIONS.md 2026-09-27 entries).
- B-M1 (drowzeys overlay gate): still open, needs user click; dealignai is the path.
- Stopped user-app containers (restore after model work settles): gigpilot x5,
  financialos x3, pageflo x2, open-webui, gx-computer.
- All 6 workers green; e2e legacy retired; restore-normal.sh V4.1 (38 tests OK);
  dashboard backend live on :8088; orchestrator (new code) live on :18900 with
  scheduler; LiteLLM still running OLD config (recreate with new compose at P3).


## 2026-09-27 19:0x — Gate A retry #3: headless via systemd unit (kit defaults)
- Evidence: two resident-session boots failed on vLLM's free-memory check
  (101.45 GiB free vs 103.38 needed @0.85; 102.2 vs 107.03 @0.88 earlier). The
  user's console session (VS Code + kilo + droid ~8G) makes the head ~2G short
  at every utilization that still fits KV. Worker rank loads FINE (torch_alloc
  96.3G; NFS backend works: worker streamed weights over fabric, ~90s).
- Action: restored kit defaults in mia-dsv41/.env (GPU_MEM_UTIL=0.88,
  DSV41_BOOT_MARGIN_GIB=12 default; WEIGHT_SYNC=nfs + NFS_CLIENTS=192.168.100.11
  stay — NFS backend proven, worker needs no local copy) and launched
  `systemctl --user start gx-dsv41-bootstrap.service` (unit runs the same
  dsv41-baseline-launch.sh headless: terminates ONLY the wayland session,
  waits for >=104.5G, boots, writes /srv/logs/dsv41-baseline-READY/-FAILED).
- THE GRAPHICAL SESSION WILL DIE (this Droid process with it). Expected. Resume
  the mission after: check `ls /srv/logs/dsv41-baseline-*` and
  `grep -a "runner\|dsv41-exl3" /srv/logs/dsv41-first-launch.log | tail`.
- Weights are all staged; boot should take ~25 min (NFS load fast on worker,
  head loads from page cache).
- ON READY (Gate A continues): quick benchmark (ops/bench/run_bench.py
  --suite quick --endpoint http://127.0.0.1:8888), record tok/s; then stop via
  mia-dsv41/stop.sh + verify MemAvailable returns both nodes; then
  systemctl --user start gx-dsv41-bootstrap.service again to prove clean
  restart. THEN restart stopped services (litellm stays, wiki dashboards,
  agentos, pageflo, gigpilot, financialos, open-webui, gx-computer).
- Stopped services/containers to restart after READY: nick-wiki-dashboard,
  agentos-control-center, agentos-supervisor (systemctl --user start);
  gigpilot x5, financialos x3, pageflo x2, open-webui, gx-computer (docker start).

## 2026-09-28 — FINAL STATE: awaiting MANUAL bootstrap trigger (user decision)
The mission process will NOT start the headless bootstrap itself (user cancelled
the trigger three times). Everything is staged; trigger when ready with:

    systemctl --user start gx-dsv41-bootstrap.service

(from any SSH/terminal session on gx10-01; survives the session kill; NOT
enabled at boot). The runner: verifies weights, terminates ONLY the graphical
wayland session, waits for >=104.5G MemAvailable, runs mia-dsv41/start.sh
(kit defaults: GPU_MEM_UTIL=0.88, margin 12, WEIGHT_SYNC=nfs,
NFS_CLIENTS=192.168.100.11), verifies :8888/health + a 17*19=323 probe, and
writes /srv/logs/dsv41-baseline-READY or -FAILED. Full log:
/srv/logs/dsv41-first-launch.log. NOTE: a stale dsv41-baseline-FAILED marker
from attempt #3 may sit next to the new one — trust the [runner] log tail.
After READY: resume the mission (Gate A benchmarks, then dealignai per
DECISIONS.md). Ranks are currently DOWN; worker holds no residual memory.
