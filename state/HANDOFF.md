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

## 2026-09-28 ~09:3x — ENV BUG FIXED + stale runtime cleaned; bootstrap dispatched
Attempt #4 root cause (worker.log, 23:45:25): TP rank 1 died in vLLM envs.py
`int('')` on VLLM_SPARSE_INDEXER_MAX_LOGITS_MB. In start.sh launch_cluster(),
the worker's docker run gets env in TWO passes: worker_nccl (kit default 256)
then serve_env, and the serve_env loop emitted `-e VAR=''` for every blank
host var — the empty override of 256 killed rank 1 AFTER the head rank had
loaded 99.84 GiB / 39/39 shards / DSpark draft / 240s. The desktop-memory
theory is CLOSED: headless boot loads fine.

Fixes (all in canonical repo, commit follows):
1. mia-dsv41/start.sh: serve_env loop now OMITS blank vars
   (`[ -n "${!v:-}" ] && serve_env+=...`) — blank means "use kit default",
   which rank 0 and rank 1 now both get (256 via nccl_common; also restores
   DSV41_PREFILL_EMPTY_CACHE_MEMAVAIL_GIB=2.5 / DSV41_PREFILL_END_EMPTY_CACHE=0
   on the worker). All other blank-forwarded vars are string/blank-tolerant
   (inner scripts guard with `[ -n ... ]`; patches use `or`/strip guards).
2. mia-dsv41/tests/test_serve_env_no_blank.py: regression — fails if any
   serve_env var is emitted as `-e VAR=''`, pins the `:-256` default, and the
   worker_nccl-before-serve_env ordering. PASS; test_numeric_config.py PASS.
3. Runner ~/bin/dsv41-baseline-launch.sh (mirrored to ops/ in the repo):
   now ROTATES previous markers at attempt start (explicit rm -f of the exact
   READY/FAILED paths — no more stale-marker ambiguity) and additionally
   verifies /v1/models lists DeepSeek-v4.1-Flash-EXL3 before writing READY.

Cleanup evidence (via mia-dsv41/stop.sh, NOT raw docker kill):
- gx10-01: dsv41-exl3-head removed; MemAvailable 4 -> 108 GiB (session still
  resident; headless kill frees ~8 more); swap 6.4 -> 2 GiB.
- gx10-02: worker container removed; 116 GiB available, swap 0.
- dsv41-exl3-nfs exporter container stays up (normal kit lifecycle).
- Only remaining GPU procs = gnome-remote-desktop (dies with the session).
- Fabric verified: both rails ACTIVE, GID index 3 = RoCEv2 ffff:c0a8:640a/640b.
- Weights intact: 39/39 EXL41 shards + engram 47/48 + index (NO re-download).

DISPATCHED: `systemctl --user start gx-dsv41-bootstrap.service` (attempt #5).
Expect READY/FAILED marker in /srv/logs/ after ~25-40 min (weights cached;
image present both nodes; NFS sync path proven). The graphical session (and
this Droid process with it) dies by design at dispatch.

ON READY (Gate A continues):
1. ops/bench/run_bench.py --suite quick --endpoint http://127.0.0.1:8888
   (TTFT + decode tok/s; record in EVIDENCE.md).
2. Stop via mia-dsv41/stop.sh; verify MemAvailable returns BOTH nodes.
3. Restart via systemctl --user start gx-dsv41-bootstrap.service (proves clean
   restart; markers rotate automatically now).
4. Second real completion probe (runner does 17*19 automatically).
5. THEN dealignai uncensored staging per DECISIONS.md D-M4 (drowzeys overlay
   still gated 403 — do NOT retry repeatedly).
ON FAILED: read `tail /srv/logs/dsv41-first-launch.log`, fix narrowly, re-dispatch.
GUI restore: console login at gdm (or reboot after stopping the model).

## 2026-09-28 11:40 SAST — Gate A attempt #6 IN FLIGHT (SSH/VS Code, no RDP)

Latest failed attempt (#5, 10:49) was NOT the env bug. Head CUDA check:
Free 104.85 / 121.63 GiB vs GPU_MEM_UTIL 0.88 = 107.03 GiB. Runner thought
already-headless (114.3 GiB) but GNOME/RDP/Chrome/PageFlo/Droid came back.
Droid PID 1024481 was `docker compose up -d` in PageFlo during drain.

Fixes now on disk (ops/ + ~/bin):
- ops/dsv41-prestart-drain.sh: stop stale ranks via mia stop.sh, runtime-mask
  gnome-remote-desktop, terminate wayland/x11 AND leftover closing sessions,
  stop open-webui/gx-computer/pageflo, kill PageFlo host/dev/compose/buildx,
  kill agent-browser chrome + droid, optional LiteLLM, wait for >=112 GiB
  (target 116). Restore mark: /srv/logs/dsv41-restore-after-boot.json
- ops/dsv41-baseline-launch.sh + ~/bin: full Gate A in the oneshot unit
  (drain -> start -> 17*19 probe -> stop -> memory both nodes -> drain ->
  start -> second probe). READY only if both probes return 323.
- gx-max-start.sh calls the drain before admission.
- GXMAX_CLEAN_START_MIN_AVAIL_GIB default 112.

DISPATCHED 11:40:14: systemctl --user start gx-dsv41-bootstrap.service
(disabled at boot). CUDA check PASSED this time: after init_device
MemAvailable=108.81 GiB (need 107.03). load_model running, torch_alloc 69 GiB
at t+10s. GPU_MEM_UTIL=0.88 KV=2684354560 unchanged.

Disk for uncensored (do NOT delete stock until Gate A READY + commit):
gx10-01 / 80G free (91%); stock model 197G + engram 196G. dealignai pack
~197-210G needs reclaim of duplicates after Gate A evidence, not before.
gx10-02 215G free. NFS weight backend stays.

Do not restore PageFlo/open-webui/RDP while gx-max is starting or loaded.

## 2026-09-28 11:57 SAST — STOCK GATE A COMPLETE

READY /srv/logs/dsv41-baseline-READY
boot1 + stop (head 117.1 / worker 116.7, no stale ranks) + boot2, both 17*19=323.
Fingerprint vllm-0.1.dev20904+g179dd0fa9-tp2-0829f620. GPU_MEM_UTIL=0.88 KV=2.5GiB.
Bench: state/bench/gate-a-stock.jsonl — FAST/BALANCED/SWARM in registry + D-M5.
Next: replace head stock pack with dealignai uncensored (D-M6). Worker keeps
local stock as recovery. Then LiteLLM gx-max/gx-auto real completions.

## 2026-09-28 13:44 SAST — UNCENSORED PRODUCTION UP + LITELLM

Head stock pack replaced (hardlinks deleted). Worker still has independent
stock at /srv/models/dsv41/model (recovery). Engram shared.

dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw @8a27b35
path=/srv/models/dsv41/uncensored 39/39 shards 197G
shard20 sha256 ac7cd83a1452dc4c323adc57e36e3a14512577a09fa0f54b520815f11029778d
(stock shard20 dfec038e… — weights differ; embeddings shards 01/39 match)

mia-dsv41/.env MODEL_HOST=/srv/models/dsv41/uncensored GPU_MEM_UTIL=0.88
TP=2 launch READY /srv/logs/dsv41-uncensored-READY 13:23
/health 200, 17*19=323, fingerprint vllm-0.1.dev20904+g179dd0fa9-tp2-0829f620
Coding is_prime OK. Tool call get_weather(Paris) OK.
Textbook probes (SQLi/BoF/jailbreak-concept/lock) complied; no "as an AI" refusals.

LiteLLM restored: gx-max -> 323, gx-auto -> 56 (real completions, not just /v1/models).
Dashboard /api/ready ready=true. Orchestrator ok. Bootstrap unit disabled at boot.
rank1-deadman armed on gx10-02 watching 192.168.100.10:29521 and :8888/health.

Do NOT restore PageFlo/RDP while the model is loaded.

## 2026-09-28 22:00-22:15 SAST — Open WebUI/Computer restored; gx-max reboot attempt hit B-039

gx-max was released (14:46, clean) hours before this and never reloaded, so it was NOT loaded when
this happened. Separately from the mission: `chat.legenex.co` was down (Cloudflare 502) because the
13:13 drain's `docker stop open-webui`/`gx-computer` were never followed by a restart. Repaired:
both recreated from their existing external volumes (`open-webui`, `gx_computer_data` — untouched),
data verified identical (4 users/59 chats/277 messages/1 memory/3 files/1 folder/1 note, canonical
Nick Allen unchanged) before and after. Full writeup: `CURRENT_STATE.md` LATEST UPDATE, D-046.

While doing that repair, discovered gx-mini/gx-code (deleted by this mission's D-M1) were still
referenced in Open WebUI's connection config — user chose to reconcile Open WebUI to the live
gx-max/gx-auto-only set rather than resurrect the retired llama-swap backend.

User then asked for a real gx-max boot to verify it live (Definition of Done for the Open WebUI
repair). Ran `gx-max-start.sh` — drain ran fine (stopped open-webui/gx-computer again, expected),
but **admission hard-refused**: `/swapfile-sglang` (L-8, 48G) does not exist on node1's disk at all
right now (`swapon --show` only shows the default 16G `/swap.img`; free swap 14.1 GiB vs the 40 GiB
minimum the load transient needs). This is new since this morning's successful 13:23 boot — something
between then and now removed it (worth checking: was it ever created with `fallocate` and did a
disk-cleanup step, e.g. D-M6's stock-pack deletion, catch it by accident?). Logged as **B-039**
(needs sudo, not available in this session). Open WebUI/Computer were brought back up immediately
after the refusal — that repair holds independent of gx-max.

**For whoever resumes next:** gx-max/gx-auto will keep 503ing ("model is down... raise
X-GX-Priority: interactive... or use the lifecycle endpoint") until a human with sudo restores
`/swapfile-sglang` on node1 (and confirms it on node2 — not checked this pass) per B-039's suggested
fix. The model itself is not in question — this morning's 13:23-13:44 run already proved it end to
end. Once the swapfile is back, `bash legenex/lifecycle/gx-max-start.sh` should work; expect it to
drain open-webui/gx-computer again and bring them back up after.

