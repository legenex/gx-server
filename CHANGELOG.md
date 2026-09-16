# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed
- **gx10-02 no longer depends on the retired `~/gx-worker` tree.** The
  live containers (`gx-llama-swap-node02`, `gx-media-router`, `gx-comfyui`)
  were bind-mounting files through `~/gx-gateway`, `~/gx-media` and
  `~/gx-kernel-lock`, which were symlinks into `gx-worker`. Those are now
  real runtime directories deployed from `legenex/gateway`, `legenex/media`
  and `legenex/host/kernel-lock`, and the containers were recreated on them.
- `integrity-audit.sh` (mirror): checks `~/gx-kernel-lock` instead of the
  `gx-worker` copy. It also compares the deployed `gx-reason.env`,
  `extra_model_paths.yaml` and media workflows, and warns if a deploy
  directory is a symlink or `~/gx-worker` reappears.
- `recover-node2.sh`: dropped the `gx-worker` `gx-node2ctl` and
  `sync-status-to-lead.sh` steps. ComfyUI status now comes from the media
  router's `/health`.
- `acceptance.sh` gx-auto test: it now matches its own routing decision
  instead of the last log line, so concurrent gx-auto traffic no longer
  causes a false FAIL.

## [0.14.0] - 2026-09-16

### Added
- **Management web UI** `gx-control-ui` (`legenex/control-ui/`, D-028).
  It runs on gx10-01 port 8088, bound to loopback and Tailscale only, as the
  user unit `gx-control-ui.service`. Pages:
  * **Dashboard:** both nodes, rails, Tailscale, models, gx-max lifecycle,
    locks and ledger, Git HEADs, services, warnings, queue.
  * **Models:** the seven aliases with facts and live state, and
    LOAD / UNLOAD / RESTART through the sanctioned paths. gx-max goes only
    through the orchestrator.
  * **Runtime** and **Cluster:** topology and live RoCE throughput.
  * **Jobs / Queue** and **Logs:** 25 predefined, redacted streams.
  * **API Playground:** chat, vision, tools, streaming, image and video,
    with curl / Python / JavaScript snippets.
  * **Docs:** 7 pages covering every operator and user topic.
  * **Settings / System:** integrity audit, kernel verifier, node-2
    reconcile, and safe restarts.
- **Control UI security:**
  * single admin account with a scrypt hash in a 0600 file outside Git,
    set with `scripts/gx-ui-passwd`;
  * server-side sessions, CSRF tokens, same-origin checks and a
    login-throttle lockout;
  * strict CSP and security headers;
  * an audit log;
  * no shell, upgrade or firmware operation;
  * upstream keys never reach the browser.
- **Control UI QA** (`npm run qa`):
  * ruff, mypy, 133 unit/API/auth/performance tests and the build check;
  * 11 Playwright tests including axe-core WCAG 2.2 AA and a mobile
    layout check;
  * gitleaks and npm audit.
  * `npm run test:live` drives the deployed UI with real model calls.
- **Orchestrator:** read-only gx-max lifecycle observability (D-029).
  * `GET /lifecycle/gx-max/events`;
  * `phase`, `phase_seconds`, `last_startup_seconds` and `idle_ttl` on
    `/lifecycle/gx-max/status`;
  * a live output buffer and `/srv/logs/gx-max-lifecycle.log`;
  * a persistent job history. Launch vector and scripts unchanged.
- **Integrity audit** also checks the installed control-UI unit against the
  rendered repo template.

- **Git sync regression suite** `ops/git-sync/tests/sync-regression.sh`
  (19 hermetic checks).

### Fixed
- **Autosync conflict-marker gate was a no-op.** `git diff --check` exits
  non-zero whenever it reports something, so under `pipefail` the
  `... | grep -q` test was always false. The versioned pre-commit hook still
  caught such commits, so nothing leaked. The gate now captures the output
  first; the hook got the same hardening.
- `gx_orchestrator/tiers.py`: stale gx-reason checkpoint note (now
  `nvidia/Qwen3.6-27B-NVFP4`, D-021).

### Security
- **B-024 opened:** the media router key on both nodes is the public
  placeholder `not-required`. The rotation procedure is documented; the
  rotation itself was not performed in this run, because the permission
  policy blocked writes to the secret stores.

## [0.13.0] - 2026-09-16

### Fixed
- **gx-max serves again** (B-022 resolved, D-025). The failing runs differed
  from the verified `4b96e49` launch in two ways, and both are reverted:
  * `--memory 106g --memory-swap 106g` removed. Equal values disable the
    container's swap, so the load transient could not spill into
    `/swapfile-sglang`.
  * Engine memory settings restored to the verified and official-cookbook
    values (`--mem-fraction-static 0.80`, context 327680, chunked prefill
    8192, CUDA-graph and running-request limits of 32). 0.70 and 0.50 are
    below the ~0.731 a TP=2 shard needs.

  Result: two-node TP=2 load in 539 s, 8/8 real inference checks passing
  directly and through the gateway, and RDMA on both rails.
- **gx-max admission can pass.** A new cluster-takeover policy replaces
  `117 GiB peak + 30 GiB reserve <= node`, which could never pass. It checks
  that the nodes are drained, the swapfile is active with headroom, there is
  no existing memory pressure, and the management plane is healthy.
  Single-node tiers keep the 30 GiB reserve.
- **Node 2's deadman leaves the load phase.** It probed its own loopback for
  `/health`, but only rank0 on node 1 serves HTTP. It now probes node 1
  over the fabric.
- **Kernel-lock verifier false failures** (D-027):
  * A held, installed kernel (`hi`) was reported MISSING.
  * Unrelated older-ABI kernel flavours proposed by `dist-upgrade` failed
    the check.

  Both nodes now pass 13/13.
- **The orchestrator shows a gx-max started outside it** as `ready` instead
  of `stopped` (DOWN→READY adoption in reconcile).
- **The orchestrator health probe** judges gx-max by the takeover
  preconditions (swapfile, swap headroom, pressure) instead of the old
  arithmetic, which always refused it.

### Added
- **`legenex/lifecycle/gx-max-safety.sh`:** phase-aware, node-local safety
  rules shared by both nodes. A kernel OOM kill or a hard `NV_ERR_NO_MEMORY`
  aborts immediately; memory+swap exhaustion, swap thrashing and fork/exec
  starvation abort only when sustained. Soft `NoLog` driver messages are
  counted, not fatal. It replaces the instantaneous 2 GiB tripwire, which
  would have killed the verified launch.
- **`legenex/lifecycle/rank0-watch.sh`:** a steady-state watchdog on node 1.
  It unwinds both nodes when a rank disappears, when node 2 is unreachable,
  or when `/health` stays down. Verified live: after rank1 was killed, the
  cluster was verified clean 69 s later.
- **`legenex/tests/gx-max-inference.sh`:** real-output checks (factual,
  reasoning, executed code, long generation, TTFT, RDMA traffic).
- **`legenex/host/kernel-lock/`:** the kernel-lock tooling, now versioned.
- **`ops/git-sync/`:** source control across both nodes (D-026).
  * gx10-01 is the only writer: debounced autosync, a secret-scan gate,
    push to `legenex/gx-server`.
  * gx10-02 is a pull-only mirror: immediate and 1-minute reconcile, with
    drift evidence saved before each reset.
  * Both nodes run a daily integrity audit.
  * Versioned hooks live in `.githooks/`.
- Unit tests: 13 safety-rule tests, 12 takeover-admission and probe tests,
  2 adoption tests, and a new unwind regression E6 (unsustained distress is
  not an abort).

### Changed
- Guard locks and ledgers moved out of the checkout to
  `/srv/projects/gx-cluster/state/guard`.
- `.gitignore` hardened for a public repository: secrets, keys, weights,
  archives, media and runtime state.
- `resource_guard.WORKLOAD_SIZING` records gx-max's steady-state residency
  (105 GiB per rank) rather than its load peak.


### Added (2026-09-16, second session)
- **gx-max failure unwind — an orphaned rank is now structurally prevented,
  and it is proven on the real workload.** Three new layers:
  `legenex/lifecycle/rank1-deadman.sh` (a watchdog that runs ON node 2, armed
  before rank0 starts, that force-removes rank1 when rank0's bootstrap socket
  goes away — it needs no ssh, which is the whole point, because every
  previous cleanup path needed to reach node 2 at exactly the moment node 2
  was starved); `legenex/lifecycle/gx-max-unwind.sh` (a dedicated failure path
  with bounded node-2 retries that *confirms* both ranks are gone, reconciles
  both ledgers, proves both locks free, restores services and then verifies
  memory return, swap, SSH/Tailscale and both ConnectX rails); and an EXIT
  trap in `gx-max-start.sh` that no failure path can miss — rank died,
  readiness timeout, `set -e`, SIGINT/SIGTERM.
  Verified live: the deadman fired on node 2 at 1 GiB MemAvailable during a
  real DeepSeek launch and the node returned to 117 GiB immediately — the
  exact condition that cost 80 minutes in B-020. Three unwind runs reported
  `UNWIND COMPLETE — cluster verified clean`.
- **`legenex/tests/unwind-tests.sh`** — a regression suite for the above,
  using placeholder containers so it runs in about a minute and costs no
  memory.
- **Phase-aware memory guards.** `GXMAX_LOAD_FLOOR_GIB` (load phase) and
  `GXMAX_ABORT_FLOOR_GIB` (steady state, enforced only once the engine answers
  `/health`). A single floor is wrong: weight loading unavoidably takes both
  nodes to near zero MemAvailable, so enforcing the steady-state reserve
  during load aborted two otherwise-healthy launches before this was measured.
- **Real `gx-image` and `gx-video` generations, verified as files.** A
  1,274,968 B PNG (1024x1024, 1043 distinct colours sampled, visually a red
  apple on a wooden table) in 26.5 s, and a 100,023 B h264 MP4 (640x640, 33
  frames @16 fps, all 33 frame hashes distinct, visually lit candle flames)
  via Wan 2.2 in 48.1 s. Node 2 stayed at 59.7 GiB / 42.8 GiB minimum
  MemAvailable respectively, swap untouched, and unload returned it to
  117 GiB. `TEST_RESULTS.md` §15.3-15.5.

### Changed (2026-09-16, second session)
- **gx-max is retuned, and the 30 GiB reserve is restored — with the honest
  consequence that the guard now refuses gx-max.** `--mem-fraction-static`
  0.80 -> 0.70, `--context-length` 327680 -> 32768, `--chunked-prefill-size`
  8192 -> 4096, `--cuda-graph-max-bs-decode` 32 -> 8, `--max-running-requests`
  32 -> 8. `GXMAX_GUARD_RESERVE_GIB` back to 30 (reverting D-020's 5), and the
  admission estimate set to the measured **load peak of 117 GiB** rather than
  a steady-state figure. See D-022 / B-022.
- **gx-auto no longer acquires gx-max.** It may use gx-max when it is already
  `READY`; it may not start it. Acquisition drains gx-mini, gx-fast and
  llama-swap on *both* nodes before doing anything else, and letting an
  ordinary routed request trigger that is wrong even when it succeeds.
  Observed live: one gx-auto prompt containing the word "exhaustive" tore down
  node 1's resident models and both llama-swaps, was refused, and spent ~12 s
  putting everything back. Covered by three new regression tests.
- **Documentation corrected for B-021 throughout.** `--memory` is described
  everywhere as a backstop on the container's *charged* memory, never as the
  bound on a model's footprint. A deliberate test made this sharper than
  B-021 had it: capping a gx-max rank at `--memory 28g` — below its measured
  26.1 GiB working set — did **not** get the container killed by its cgroup;
  the node still ran to 0 MiB MemAvailable and the *global* OOM killer fired.

### Fixed (2026-09-16, second session)
- **A refused gx-max launch used to leave the cluster with no service.** The
  drain runs before the admission guard, so every refusal silently took
  gx-mini and both llama-swaps down and never put them back. A launch that
  starts no rank now restores exactly what it stopped.
- **`CONFLICTS_N1` had the same dead-name bug the node-2 list was fixed for a
  day earlier.** `vllm` and `llama-swap-node01` match no container on node 1;
  the real names are `gx-fast` and `gx-llama-swap-node01`. Node 1's llama-swap
  was therefore never drained before a gx-max run — it stayed up and free to
  spawn a model into the memory gx-max was about to claim.
- **`pkill -f rank1-deadman.sh` matched the ssh command line that was starting
  the deadman**, so the remote shell killed itself before arming anything. Now
  a pid file.
- **`docker inspect` on a missing container writes a blank line to stdout**
  before failing, so `... || echo absent` produced `"\nabsent"` and the unwind
  reported a still-running rank0 that did not exist. Normalised here and in
  `gx-max-status.sh`.
- **The unwind's ConnectX probe piped into `grep`**, swallowing the exit
  status and reporting both healthy rails as unreachable.
- **The acceptance suite's vision fixture was wrong**, not the model: it drew
  a 120x100 *rectangle* and then asserted the model would say "square".
  gx-mini described it correctly as a rectangle and was marked FAIL. The
  fixture is now an actual square, and the suite generates it itself rather
  than depending on a stray file in `/tmp` whose absence silently skipped the
  vision check entirely.

### Added (2026-09-16 session)
- **gx-reason is live for the first time; B-011 is CLOSED.**
  `nvidia/Qwen3.6-27B-NVFP4` (20.42 GiB, verified against the live HF API
  before download) deployed to node 2 and exercised end to end. The original
  B-011 repro prompt -- `"The capital of France is"` at `temperature 0`,
  which produced `////////////////////` on every run under llama.cpp -- now
  returns `" Paris."`. Through the real LiteLLM gateway, a multi-step
  reasoning question is answered correctly with `reasoning_content`
  separated from `content`. Cold start 401 s, 12.4 tok/s, ~44 GiB footprint,
  unload returns the memory in ~5 s. Full numbers in `TEST_RESULTS.md` §14.
- **gx-reason vision verified.** The replacement checkpoint is multimodal, so
  `supports_vision: true` was added to its gateway entry — and then actually
  exercised rather than assumed: a generated image of three blue circles sent
  through the gateway was described correctly as "3 blue"
  (`TEST_RESULTS.md` §14.4). No `gx-vision` alias was added; vision remains a
  model capability, per L-10.
- **B-021 recorded: `--memory` cgroup caps do not bound a model's real
  footprint on this unified-memory hardware.** Measured with gx-reason
  loaded: the node lost 44 GiB of MemAvailable while the container's own
  `memory.current` read 10.92 GiB, because the CUDA pool is not charged to
  the container cgroup. The admission guard is unaffected (it reads real
  `/proc/meminfo`); `--gpu-memory-utilization` is the real bound.

### Changed (2026-09-16 session)
- `legenex/gateway/litellm/config.yaml`: gx-reason's stale "CHECKPOINT
  PENDING VERIFICATION" block replaced with the real checkpoint, and its
  token budget corrected from the removed model's 32768 context to the
  current 65536 -- split 49152 in / 16384 out rather than gx-fast's
  57344/8192, because a single simple question measured 1468 reasoning
  tokens and `<think>` content spends the OUTPUT budget on this tier.
- `legenex/gateway/llama-swap/node02.yaml`: measured figures replace
  estimates (cold-start timing, 20.42 GiB weights, ~44 GiB real footprint),
  the "not yet live-tested" caveat is replaced with the verification record,
  the host-resilience comment's overstated claim that `--memory` enforces
  the full ceiling is corrected per B-021, and a reference to a
  non-existent `-dry-run` flag is fixed to the real `-validate`.
- `resource_guard.py`: gx-reason's 45 GiB `WORKLOAD_SIZING` entry is now
  backed by a live measurement (~44 GiB) instead of being a documented
  guess, with a warning not to re-derive it from `docker stats`.

### Fixed (2026-09-16 session)
- **`coordination/BLOCKERS.md` B-020's prescription was wrong and has been
  corrected.** It required "a human physically present at gx10-02 to
  power-cycle it". Node 2's `uptime` proves no reboot ever occurred
  (22 h continuous, straight through the incident) and
  `docker inspect gx-max-rank1` shows `OOMKilled=true` 80 minutes in. The
  playbook now says to wait out ~80 minutes and re-probe before dispatching
  a human. B-016 (no *remote* power-cycle path) is unchanged.
- Stale node-2 residency bookkeeping (`gx-max-rank1`) cleared through the
  sanctioned `resource-guard.sh` release path, which reconciled it
  automatically on read -- B-020 follow-up step 3.
- `TEST_RESULTS.md` had two different sessions both numbered `## 12`; the
  later one is renumbered to `## 13`.

### Added (2026-09-15/16 session)
- **gx-max's B-017 admission-guard deadlock resolved.** gx-max now uses its
  own smaller, explicit reserve (`GXMAX_GUARD_RESERVE_GIB`, 5 GiB) instead
  of the generic 30 GiB floor, scoped only to its two rank launches
  (`gx-max-start.sh`, `gx-max.conf`) -- every other tier's admission check
  is unaffected. Verified live: gx-max passed admission on both nodes and
  actually started rank1+rank0 for the first time ever through the real
  orchestrator (`coordination/DECISIONS.md` D-020).
- **gx-reason's engine/model replaced** (`coordination/DECISIONS.md`
  D-021): the confirmed-broken `unsloth/Qwen3.5-122B-A10B-GGUF`/llama.cpp
  combination (B-011) is rejected; `legenex/gateway/llama-swap/node02.yaml`
  now serves `nvidia/Qwen3.6-27B-NVFP4` on vLLM, the same proven engine
  image already serving gx-fast. (Config only at the time; **live-tested and
  closed out 2026-09-16** -- see below.)
- gx-mini and gx-fast independently re-verified live through the real
  gateway: gx-mini answered a factual prompt correctly, gx-fast (real
  ~2m7s cold start) correctly solved a multi-step logic question.

### Fixed (2026-09-15/16 session)
- `GXMAX_RANK_ESTIMATED_GIB` raised 90 -> 95 GiB after a real OOM-kill
  during weight loading exposed the older figure as too optimistic (see
  B-020) -- `gx-max-start.sh` and `resource_guard.py`'s `WORKLOAD_SIZING`
  both updated to match.

### Known issue introduced that session — RESOLVED 2026-09-16
- ~~**B-020: node 2 is physically wedged** (needs a human power-cycle, no
  remote path exists)~~ — **node 2 recovered itself; no power cycle was
  ever performed.** The kernel OOM-killed the orphaned rank1 after 80
  minutes and userspace un-starved on its own. See the 2026-09-16 entries
  below and the corrected `coordination/BLOCKERS.md` B-020.

### Added
- **Independent multi-agent review of this session's own changes (4
  reviewers, memory/lifecycle, networking/gx-max, routing/media security,
  recovery/docs), findings fixed.** Confirmed clean: locked gx-max config
  untouched, admission guard not weakened/bypassed, no MTU/RDMA/firmware
  changes, no Qwen3.8 resurrection. Real findings fixed: a stale
  "(PENDING)" label contradicting its own file's updated comments; D-019's
  wording implied a silent wrong-model substitution when the actual
  behavior (fallbacks globally disabled) was a safe loud error; a media
  security claim in this same CHANGELOG was true but untested by any
  regression test, now backed by a real assertion in `t_media`; and a bug
  in that test's own memory-cleanup call (`-X POST` on a GET-only `/health`
  endpoint silently short-circuited the actual cleanup) caught live when
  node 2 was still holding ~75 GiB minutes after the test reported PASS.
  One new gap found and mitigated: `gx-max-start.sh`'s node2 admission
  check was not atomic with the rank1 launch (TOCTOU) -- added an atomic
  re-check under the same lock immediately before the docker run
  (`coordination/BLOCKERS.md` B-019); caught and fixed a real bug in that
  fix itself before shipping it (the threshold arithmetic referenced
  node1-local shell variables that don't exist on node2's remote shell,
  which would have silently always passed).
- **Full acceptance suite run clean, with one real near-miss caught and
  fixed live.** `legenex/tests/acceptance.sh` (non-slow suite): 12 PASS,
  1 FAIL (gx-reason, expected -- B-011), 1 SKIP (no vision test fixture).
  gx-auto's routing test passed all three cases cleanly for the first time
  (previously blocked by the orchestrator bind race, D-019). Mid-run, node 2
  dropped to ~10 GiB available (below the 30 GiB reserve floor): t_reason
  loaded gx-reason, t_auto's routing check reloaded it, and t_media then
  started ComfyUI via plain `docker compose up` with nothing checking what
  was already resident -- see BLOCKERS.md B-018. Caught live, unloaded
  gx-reason and freed ComfyUI's cache, memory back to 113 GiB within
  seconds. Hardened `t_reason` and `t_media` in the test suite itself to
  check/unload before this can recur in a future run.
- **Found and fixed a real production gap: gx-orchestrator had been
  silently unreachable from the gateway container all session.** An
  apparent gx-auto classifier test failure ("expected gx-reason, got
  gx-mini") turned out to be a complete false positive from a stale
  log-tail read -- investigation found zero routing decisions had been
  logged all day, because every gx-auto request from LiteLLM's container
  was failing to connect to the orchestrator. Root cause: this morning's
  boot raced `docker0` getting its IPv4 address, so the orchestrator's
  second bind (`172.17.0.1:18900`, what `host.docker.internal` resolves to
  from any container) failed and was silently swallowed (bind is
  best-effort per-address, not fatal), leaving it loopback-only for over
  2.5 hours. Fixed with an `ExecStartPre` wait-for-docker0 check in
  `gx-orchestrator.service` (now also checked into the repo at
  `legenex/orchestrator/systemd/`, previously only a live file) so it
  either starts fully bound or fails loudly and retries, rather than
  degrading silently. Verified the classifier itself was never broken by
  re-running the exact failing prompt once connectivity was fixed -- it
  correctly chose gx-reason. See `coordination/DECISIONS.md` D-019.
- **gx-max validated through the real orchestrator for the first time --
  found and fixed one real bug, surfaced one genuine unresolved
  architecture decision (BLOCKERS.md B-017).** `legenex/tests/gx-max-validate.sh`
  drives acquisition through the actual `POST /lifecycle/gx-max/acquire`
  production path rather than calling the shell scripts directly, and had
  never been run to completion before. Fixed: the fabric-rail preflight
  used `ping`, which needs CAP_NET_RAW -- fails under
  `gx-orchestrator.service`'s (correct, intentional) `NoNewPrivileges=true`
  hardening, so gx-max could never acquire through its real entry point,
  only via a direct interactive shell invocation. Replaced with a
  capability-free TCP-connect probe in both `gx-max-start.sh` and
  `gx-max-validate.sh`. With that fixed, the next attempt reached the
  resource-ownership admission guard and was correctly, hard-refused: gx-max
  needs ~90 GiB/rank by locked design, which does not leave the standard
  30 GiB reserve floor the B-012 guard enforces for every exclusive-class
  workload. `gx-max.conf`'s own comment (written the same day the guard
  shipped) already named this exact collision as needing a human decision;
  not resolved unilaterally in either direction. Verified cleanup was
  correct on both nodes after the refusal: both ranks stopped, ledger
  released, ~113/115 GiB available restored, no orphaned containers.
- **`gx-max-start.sh`: fixed a container-name bug in its conflict-drain
  list.** `CONFLICTS_N2` named `comfyui` and `llama-swap-node02`, neither of
  which matches any real container on node 2 (`gx-comfyui`,
  `gx-llama-swap-node02`) -- `docker inspect` on a nonexistent name silently
  falls through to "not running", so the drain step was a no-op for both.
  Found only because this session was the first time the media stack was
  actually deployed and running when the script was read closely. Fixed the
  names and added `gx-media-router` to the list (stopping ComfyUI without it
  would leave the router up but broken). This directly affects gx-max safety:
  before this fix, starting gx-max while the media stack was resident would
  not have drained it first.
- **`acceptance.sh`'s new media test now frees ComfyUI's cache afterward.**
  Measured: ComfyUI keeps ~70 GiB of model weights resident after a
  generation rather than releasing them (correct engine behaviour, not a
  leak, but the test suite must not be the reason node 2 is quietly sitting
  on ~70 GiB afterward). Added an explicit `/free` call at the end of
  `t_media`.
- **gx-image / gx-video: real end-to-end validation, first time both were
  actually deployed and exercised.** Built `gx-comfyui:sm121` and
  `gx-media-router:1.0.0` on node 2 (first build on this recovered node;
  confirmed torch 2.14.0+cu130 with `sm_120` cubins, matching the
  Dockerfile's documented sm_121-compatibility rationale). Started both
  containers with the documented memory interlock respected (node 2 fully
  idle first). Real requests through the full production path (LiteLLM
  gateway -> router -> ComfyUI -> GPU): a genuine 1024x1024 image in 28s
  (Lightning workflow) and a genuine playable MP4 in 58s
  (wan22-t2v-a14b-lightning), both visually inspected, not just
  status-code-checked. Confirmed the security boundary holds: ComfyUI
  itself (8188) is unreachable from node 1, only the router (18800)
  answers. `legenex/tests/acceptance.sh`'s `t_media` no longer skips --
  replaced with real generation + content-fetch checks against the
  gateway and the router, so this stays a regression test rather than a
  one-off manual check. Fixed two stale "UPSTREAM PENDING" comments in
  `legenex/gateway/litellm/config.yaml` that no longer matched reality
  (the router has existed and been unit-tested since 2026-09-14; this
  session is the first time it was actually built, started, and proven
  end-to-end).
- **Integrated the ChatGPT project-seed bundle** into the canonical docs:
  node LAN/Tailscale IP addresses, a corroborating NCCL `all_gather_perf`
  benchmark, the finding that no BMC/IPMI/Redfish/MCTP remote-power path
  exists on either node (`coordination/BLOCKERS.md` B-016), and the
  GDM-auto-login/RDP stale-session recovery procedure. Added root-level
  `DECISIONS.md`/`TASKS.md`/`TEST_PLAN.md`/`HANDOFF.md` and `docs/chatgpt/`
  reference copies. See `coordination/DECISIONS.md` D-018.

### Fixed
- **Documentation drift against the live machines.** `CURRENT_STATE.md` and
  `coordination/BLOCKERS.md` still described node 2 as physically wedged;
  it had already been power-cycled and recovered (`recover-node2.sh`,
  16 PASS / 0 FAIL). Node 1's gateway container had exited cleanly (a
  benign restart artifact, not a crash) and was brought back up.
- **`recover-node2.sh`: MemAvailable check was silently broken.** The
  accidental-workload-detection step's `awk` invocation was passed to the
  `remote()` SSH helper as multiple shell words instead of one pre-quoted
  string, so OpenSSH reassembled it unquoted on the far end and the remote
  shell hit a syntax error on the `awk` script's parentheses. The check has
  been silently WARNing instead of verifying node 2's real memory headroom
  since it was written. Fixed by passing the whole remote command as a
  single string.
- **`gx-reason-diagnose.sh`: the CPU-only comparison container couldn't
  start at all.** It intentionally omitted `--device nvidia.com/gpu=all` to
  force a "true" CPU-only path, but `legenex/llama-cpp-spark`'s
  `llama-server` binary is dynamically linked against `libcuda.so.1`, which
  is only present when the CDI GPU device is mounted. Without it the process
  exited immediately (exit 127, "cannot open shared object file") before
  ever touching the model — a missing-library failure, not a CPU-vs-GPU
  compute result, and the first run of this script against the recovered
  node 2 (2026-09-15) silently reported it as a script FATAL rather than a
  real diagnosis. Fixed by keeping the GPU device attached (so the binary
  can load) while keeping `--n-gpu-layers 0` (so the actual compute still
  runs on CPU only).

### Diagnosed
- **BLOCKERS.md B-011 root cause isolated.** Re-ran the (now-fixed)
  gx-reason GPU-vs-CPU comparison against the recovered node 2: GPU path
  reproduces the exact recorded garbage output
  (`////////////////////`), CPU-only path with identical weights and
  sampling params is coherent (`"The capital of France is Paris."`). This
  isolates the fault to the CUDA execution path of this specific
  `legenex/llama-cpp-spark` build's `qwen3_5_moe` hybrid
  (GDN + full-attention) kernel implementation, not the checkpoint/quant.
  Rebuilt the image against current upstream `llama.cpp` master (no pinned
  commit in the Dockerfile, so this picked up everything upstream as of
  2026-09-15) to test the "stale build" hypothesis — build succeeded, but
  re-running the identical comparison against the new binary reproduced the
  exact same GARBAGE/SANE split byte-for-byte. Rules out a stale build;
  **B-011 remains OPEN**, now narrowed to either an unfixed upstream CUDA
  kernel bug or something specific to this GB10/sm_121 environment. Old
  image kept as `legenex/llama-cpp-spark:pre-b011-fix-backup` on node 2 for
  rollback; nothing was pushed to any registry. See BLOCKERS.md B-011 for
  the remaining next steps (upstream issue research, an alternate quant, or
  a bisect) — none attempted this session, each is a real, separate piece
  of work.

### Removed
- **Qwen3.8 permanently retired.** `vllm-qwen38-uncensored` (a standalone,
  always-on vLLM container, ~80 GiB resident, unrelated to the gx-mini/
  gx-fast/gx-reason/gx-max/gx-auto/gx-image/gx-video tier set) and its
  checkpoint at `/opt/models/Qwen3.8-27B-Uncensored-NVFP4` have been removed
  from gx10-01 by the human operator. All active runtime/download/routing/
  lifecycle support for Qwen3.8 has been removed from the canonical repo:
  `setup/setup-flashnext.sh` (deleted — the file existed solely to prepare
  Qwen3.8-Flash-Next), the `QWEN38_ROOT` variable and the two Qwen3.8 download
  entries in `setup/download-models.sh`, `QWEN38_ROOT` in `.env.sample`,
  `vllm-qwen38-uncensored` from the gx-max conflict-drain list in
  `legenex/lifecycle/gx-max-start.sh`, and the `Qwen3.8-27B-NVFP4-DFlash2` /
  `Qwen3.8-Flash-Next-NVFP4` entries in `LiteLLM/config.yaml.sample` and
  `llama-swap/config.yaml.sample`. Qwen3.8 is **not** gx-fast and must not be
  reintroduced under any tier alias. Historical rationale for why the
  100-125B class has no Qwen3.6/Qwen3.8 checkpoint (see `MODELS.md`) is
  unaffected and remains as comparative context, not active support.
- Retiring this container also resolved tonight's memory-safety incident on
  gx10-01: it held ~80 GiB resident with no lifecycle management, leaving as
  little as ~9 GiB available system-wide. See `CURRENT_STATE.md` and
  `coordination/BLOCKERS.md` for the resource-ownership work this motivated.

### Changed
- **Sample configs**: `LiteLLM/config.yaml.sample` and `llama-swap/config.yaml.sample`
  are regenerated from the live configs, so they now document all 31 LiteLLM models
  and all 25 llama-swap models instead of the stale 21/22. Secrets and private data
  are replaced with placeholders: `<LLM_ROOT_PATH>`, `<REPO_CONFIG_PATH>`,
  `<FLASHNEXT_REPO>`, `<HOME>`, `<IMAGE_NAMESPACE>` and a new `<LAN_HOST_IP>`.
  The samples now differ from the live files only by those redactions plus the
  sample-only `disable_master_key_return: true` hardening flag.

### Fixed
- **Sample configs**: the committed `LiteLLM/config.yaml.sample` previously contained
  a real LAN IP address; it is now `<LAN_HOST_IP>`.
- **llama-swap**: the DFlash2 KV pool sizing notes now live in the real config too,
  so they survive future sample regeneration.

---

## [0.12.1] — 2026-09-07

### Fixed
- **Qwen3.8-27B-NVFP4-DFlash2**: prompts of roughly 24k tokens or more failed with
  HTTP 500 (`Out of memory even after retracting all other requests in the decode
  batch`). SGLang sized its KV pool at only 23,879 tokens, so `--allow-auto-truncate`
  cut the prompt to exactly the pool size and left zero slots for decoding; with
  `--max-running-requests 1` there was nothing to retract and the request aborted.
  The recipe now sets `--max-total-tokens 131072` and raises `--mem-fraction-static`
  from 0.53 to 0.58, giving a 131,072-token pool (5.5x larger) for 5.26 GB of KV
  cache, with ~57 GB of device memory still free.
- **LiteLLM**: the `Qwen3.8-27B-NVFP4-DFlash2` entry advertised
  `max_input_tokens: 262144`, far beyond what the server could hold, so oversized
  prompts failed mid-stream instead of being rejected. Input and output limits now
  sum to the real pool size (98,304 + 32,768 = 131,072).
- **LiteLLM (sample config)**: `model_info` for the same model was nested under
  `litellm_params`, where LiteLLM ignores it. It is now a sibling key.

---

## [0.12.0] — 2026-09-03

### Added
- **Qwen3.8-27B-NVFP4-DFlash2**: llama-swap/SGLang recipe with DFlash2 speculative decoding.
- **Qwen3.8-Flash-Next-NVFP4**: llama-swap/SGLang recipe with HashK PLE and NEXTN speculative decoding.
- **`setup/setup-flashnext.sh`**: prepare the Flash-Next checkpoint and HashK PLE artifact.
- **`setup/download-models.sh`**: download the Qwen3.8 checkpoints into a dedicated `QWEN38_ROOT` path and disable Xet by default for resumable HTTP downloads.
- **LiteLLM**: expose the new Qwen3.8 models through the unified gateway.
- **GitHub Actions**: validate Python, shell, Docker Compose, and SonarQube quality on pushes to `main`.

---

## [0.11.1] — 2026-08-11

### Fixed
- **`benchmark-models.sh`**: replaced `eval` of hand-built command strings in
  `run_benchy`/`run_quality` with array-based invocations. A model name (or
  `--quality-extra-args` value) containing shell metacharacters could previously
  be interpreted as shell code instead of a literal argument.
- **`docker-compose.yml`/`.sample`**: restored the `${IMAGE_NAMESPACE:-${GH_USER}}`
  fallback on all image references — it was accidentally dropped to a bare
  `${IMAGE_NAMESPACE}` in 0.11.0, breaking image pulls for any `.env` that only
  sets `GH_USER`.

---

## [0.11.0] — 2026-08-11

### Added
- **`llama-qwen36-27b`**: second always-on persistent llama.cpp service (Qwen3.6-27B
  abliterated, Q4_K_M, ~16.5 GB) alongside the existing always-on 4B, exposed on
  `19002`. Stays resident regardless of what llama-swap loads/evicts.
- **`LiteLLM/complexity_hook.py`**: `COMPLEX`/`REASONING` tiers now route to the new
  always-on 27B instead of falling back to the 4B — the complexity router has real
  tiering again instead of collapsing everything onto one model. Also clamps
  client-supplied `temperature` into the `[0, 2]` OpenAI-compatible range instead of
  passing invalid values through.
- New `llama-swap` model entries: `Qwen3.6-35B-A3B-int4-AutoRound`,
  `Qwen3.6-35B-A3B-PrismaQuant-4.75bit`, `Qwen3.5-122B-A10B-NVFP4` (txn545 single-Spark
  MTP checkpoint, plus `llama-swap/scripts/patch-qwen35-nvfp4-runtime.sh` to register
  its custom model class before vLLM starts), and the AEON-7 DFlash/Multimodal-NVFP4-MTP
  variants (`Qwen3.6-27B-AEON-Ultimate-Uncensored-DFlash` /
  `-Multimodal-NVFP4-MTP`), all mirroring forum-recommended single-Spark recipes.
- **`sglang`** service in `docker-compose.yml` (scitrera DGX Spark image) as an
  additional serving backend alongside vLLM/llama.cpp/Ollama.
- **`LLM_OLLAMA_ROOT_PATH`**: optional dedicated host path for the Ollama model cache,
  independent of `LLM_ROOT_PATH`; defaults to `${LLM_ROOT_PATH}/ollama` if unset.
- **`benchmark-models.sh --size-order`**: sort matched models smallest-to-largest
  before running, plus a per-model RAM-budget lookup so overnight runs are easier to
  compare at a glance.
- `docker-compose.yml`/`.sample`: `logging.max-size`/`max-file` on the `llama-swap`
  service — the default json-file driver never rotated, and one oversized logged
  response (seen: a 7.5 MB `/api/events` entry) could corrupt the file for `docker
  logs`' full-history reader while `docker logs -f` kept working, making the log look
  silently "frozen".

### Changed
- **`overnight.sh`**: rewritten to run under `set -euo pipefail`, log each model's
  benchmark output to its own timestamped file under `test-results/overnight-logs/`
  in addition to a master log, and continue past a failed model instead of aborting
  the whole sequence.
- Default `ollama` service in `docker-compose.yml` commented out (superseded by the
  dedicated always-on llama.cpp services); `IMAGE_NAMESPACE` no longer silently falls
  back to `${GH_USER}` in image references, so a missing `IMAGE_NAMESPACE` now fails
  loudly instead of resolving to an unintended tag.
- `scripts/start-ds4-deepseek.sh`: DSpark speculative decoding now opt-in
  (`ENABLE_DSPARK=0` by default) — measured peak usage with DSpark enabled
  (~104–113 GiB against the ~121.69 GiB unified pool) reliably triggered real
  `NVRM: Out of memory` driver errors that could destabilize the whole graphical
  session, not just the process; without DSpark the model fits comfortably
  (~83–90 GiB). Also adds `--gpu-vram`/`--batched-session` args.

### Removed
- `Qwen3.6-27B-PrismaQuant-5.5bit` / `Qwen3.6-27B-uncensored-heretic-vllm` dropped
  from `parse_metrics.py` and `overnight.sh` in favor of
  `Qwen3.6-35B-A3B-PrismaQuant-4.75bit`.
- `Nemotron-3-Nano-30B-A3B-NVFP4` commented out of `llama-swap/config.yaml.sample`.

---

## [0.10.5] — 2026-08-04

### Added
- **`scripts/recipe_tool.py import-sparkrun`**: translate an upstream
  [sparkrun/spark-arena recipe](https://sparkrun.dev/recipes/format/)
  (`model`/`runtime`/`container`/`defaults`/`command` with `{placeholder}`
  substitution) directly into a `llama-swap` `config.yaml` model block —
  `{port}`/`{host}` map to llama-swap's own `${PORT}`/`${host}` launch macros
  rather than the recipe's literal defaults, every other placeholder bakes in
  from `defaults`, and the container is wrapped with this stack's usual
  `--runtime nvidia --gpus all --ipc=host --network container:llama-swap`
  plus an HF cache mount (sparkrun recipes assume the runtime downloads the
  model by HF id on first launch). Supports `vllm` and `llama-cpp` runtimes —
  the two this stack already knows how to run; other runtimes are rejected
  with a clear error rather than producing something broken. Same dry-run/
  `--apply`/auto-backup behavior as `import`.

---

## [0.10.4] — 2026-08-04

### Added
- **`scripts/recipe_tool.py`** & **`scripts/recipe.sh`**: recipe import/export for
  `llama-swap/config.yaml` model blocks. `recipe.sh export <model>` writes a model's
  `cmd`/`ttl`/`checkEndpoint`/`proxy`/etc. block to a standalone, versionable file
  under `recipes/`; `recipe.sh import <recipe-file>` applies it back into
  `config.yaml` (dry-run diff by default, `--apply` to write, auto-backs up the
  config first). Round-trips comments and block-scalar (`>`) formatting elsewhere
  in the file via `ruamel.yaml`, so importing/exporting a single model doesn't
  disturb the rest of a hand-maintained config.
- **`llama-swap/scripts/lib-wait-mem-stable.sh`**: shared `wait_for_stable_memory`
  helper, sourced by all three vLLM launch scripts (`launch-vllm-auto.sh`,
  `launch-qwen35-122b.sh`, `launch-qwen35-122b-hybrid.sh`). Polls `MemAvailable`
  until two consecutive readings agree, guarding against a launch reading a
  `/proc/meminfo` snapshot taken mid-teardown of the previous model's container
  (docker/CUDA context release is async, so an immediate read can undercount
  free memory or race an in-flight unload).

---

## [0.10.3] — 2026-07-06

### Changed
- **`Qwen3.5-122B-A10B-int4-AutoRound`**: Added DFlash speculative decoding (`z-lab/Qwen3.5-122B-A10B-DFlash`, 15 spec tokens) to the launch script for massive throughput gains.
- **`Qwen3.5-27B-Uncensored-DFlash-NVFP4`**: Updated attention backend to `flashinfer` in `llama-swap/config.yaml` to improve agentic tool-calling accuracy.
- **`Qwen3.6-27B-PrismaSCOUT-NVFP4`** & **`Qwen3.6-27B-uncensored-heretic-vllm`**: Updated attention backend to `flashinfer` to maximize quality across NVFP4 models.
- **`overnight.sh`**: Expanded script to cover all 20 registered models and added `--quality-mode full` for rigorous 69-scenario `tool-eval-bench` validation.

### Tooling
- Upgraded `tool-eval-bench` CLI to v2.1.0 to enforce strict agentic fact extraction in Hard Mode.

---

## [0.10.2] — 2026-06-25

### Changed
- **`Qwen3.5-122B-A10B-int4-AutoRound`**: Optimized launch script (`launch-qwen35-122b.sh`) for single-user interactive latency by reducing `--max-num-seqs` (10 → 3) and `--max-num-batched-tokens` (32768 → 8192).
- **`Nemotron-3-Nano-30B-A3B-NVFP4`**: Optimized prefill performance in `config.yaml.sample` to prevent 180s HTTP timeouts on large prompts. Halved context to 131k, reduced concurrency to 4, enabled chunked prefill, and added `expandable_segments` PyTorch alloc config to prevent unified-memory swap thrashing.

---

## [0.10.1] — 2026-06-17

### Removed
- **`Qwen3.5-122B-A10B-hybrid-int4fp8`** model registration removed from
  `llama-swap/config.yaml.sample` and `benchmark-models.sh`. The hybrid checkpoint
  loads successfully but generates garbled/incoherent output (confirmed even at
  temperature=0, with and without the `mods/fix-qwen3.5-hybrid-int4fp8` patches), so it
  is no longer exposed as a selectable model. The `mods/` and launch script remain in
  place for future work once the checkpoint is rebuilt. Use
  `Qwen3.5-122B-A10B-int4-AutoRound` instead.

---

## [0.10.0] — 2026-06-12

### Added
- **`mods/fix-qwen3.5-hybrid-int4fp8/`** *(spark-vllm-docker)*: New mod applying three
  stacked optimizations to the Qwen3.5-122B-A10B-int4-AutoRound inference path, lifting
  throughput from 28.3 → ~51 tok/s (+80%) on a single DGX Spark:
  - `patch_inc.py`: Patches vLLM's INC quantization backend to detect FP8 dense layers
    in a hybrid checkpoint and dispatch them through CUTLASS block-wise FP8 GEMM
    (`Fp8LinearMethod`) instead of the BF16 fallback (+8.8%). Works with vLLM 0.21+.
  - `patch_int8_lmhead.py`: INT8 LM Head v2 — replaces the per-token Python loop with a
    single batched 2D Triton GEMV kernel with `@triton.autotune` for SM121 (+~40%).
  - `host/build-hybrid-checkpoint.py`: One-time host-side script that merges MoE expert
    weights (INT4, from Intel AutoRound) with dense layer weights (FP8 E4M3, from the
    official Qwen/Qwen3.5-122B-A10B-FP8 checkpoint) into a single hybrid safetensors
    checkpoint (~9 GB smaller than the INT4 original).
  - `host/add-mtp-weights.py`: Surfaces the MTP speculative-decoding tensors already
    present in the AutoRound checkpoint by adding them to the index of the hybrid
    checkpoint, enabling MTP-2 (`num_speculative_tokens=2`, ~80% accept rate, +25%).
- **`recipes/qwen3.5-122b-hybrid-int4fp8.yaml`** *(spark-vllm-docker)*: Recipe for the
  hybrid model; includes the new mod, sets `--speculative-config mtp:2`, single-GPU,
  `--kv-cache-dtype fp8`, `--attention-backend FLASHINFER`.
- **`llama-swap/scripts/launch-qwen35-122b-hybrid.sh`**: llama-swap launch script for the
  hybrid checkpoint with adaptive `gpu_memory_utilization`, MTP-2 speculative decoding,
  and inline mod application at container start.

---

## [0.9.0] — 2026-06-12

### Added
- **`docker-compose.yml`**: New `llama-qwen35-4b` always-on service — dedicated persistent
  llama.cpp instance for Qwen3.5-4B-Q4_K_M; stays loaded regardless of llama-swap evictions,
  sized for the STT→LLM→TTS pipeline (low latency, always warm, ctx 131072).
- **`LiteLLM/complexity_hook.py`**: Custom `CustomLogger` pre-call hook that rewrites `model`
  to a complexity-tiered target before LiteLLM routes the request. Fires on all endpoints
  (including `/v1/responses`) where the native `auto_router` type is unsupported.
- **`LiteLLM/router.json`**: Semantic router config for embedding-based intent routing
  (requires `OPENAI_API_KEY` for the embedding call).
- **`scripts/start-ds4-deepseek.sh`**: Startup script for the DS4 DeepSeek node.

### Changed
- **`LiteLLM/config.yaml.sample`**: Add `auto_router1` (complexity-based, 4 tiers: SIMPLE →
  4B, MEDIUM → 27B, COMPLEX → 35B, REASONING → 122B) and `semantic-router` (embedding-based
  intent routing) model entries.
- **`docker-compose.yml`** / **`docker-compose.yml.sample`**: Mount `router.json` and
  `complexity_hook.py` into the LiteLLM container; add optional `OPENAI_API_KEY` env var.

### Chore
- **`.gitignore`**: Ignore `ds4/` (compiled binaries + model), `logs/`, `*.o`, `*.pid`.

---

## [0.8.0] — 2026-06-12

### Fixed
- **`launch-vllm-auto.sh`**: Broken `/models/vllm` volume mount when `LLM_ROOT_PATH` already
  points to the vllm directory (e.g. `/home/user/LLMs/vllm`). Script was appending `/vllm`,
  producing a double-`vllm` path that Docker auto-created as an empty directory — all vllm
  models started with an empty `/models/vllm` mount and failed with "chat template not found".
  Default fallback updated from `/home/user/LLMs` → `/home/user/LLMs/vllm`.

### Changed
- **`llama-swap/config.yaml.sample`**: Set all model `ttl: 0` (was `ttl: 3600`/`600`/`300`).
  Models now stay loaded in VRAM until the swap mechanism evicts them when a different model
  is requested, instead of auto-unloading after an idle timeout.

### Changed *(config.yaml — gitignored, applied manually)*
- **Qwen3.6-35B-A3B-FP8**: Removed `GMEM_OVERRIDE=0.7069`; model now uses adaptive
  `gpu_memory_utilization` based on free memory at launch time.
- **Qwen3.6-35B-A3B-FP8**: Fixed `MODEL_HOST_PATH` from `/models/vllm/Alibaba/…` →
  `/models/Alibaba/…` so the adaptive launcher can read `config.json` inside the llama-swap
  container (where `LLM_ROOT_PATH` is mounted as `/models`).
- **Qwen3.6-35B-A3B-FP8**: Lowered `GMEM_MIN` `0.55` → `0.40` so adaptive does not abort
  when TTS services are running and effective free memory yields u_cap ≈ 0.44.

---

## [0.7.0] — 2026-05-29

### Fixed
- `docker-compose.yml`: Comment out static vllm sidecar service; fix llama-server port to 19000
- `docker-compose.yml`: Pass `LLM_ROOT_PATH` into llama-swap container env so launch scripts
  can build correct host-side volume paths
- `.env.sample`: Add missing `REGISTRY` and `IMAGE_NAMESPACE` variables
- `.env.sample`: Make `POSTGRES_DB` and `POSTGRES_USER` configurable (were hardcoded)
- `docker-compose.yml.sample`: Fully sync structure and comments with live config

### Changed
- `docker-compose.yml`: Align live compose structure with sample for easier diffing

### Added
- Qwen3.6-27B-PrismaSCOUT-NVFP4 and Qwen3.6-27B variants to `config.yaml.sample`

### Docs
- `benchmark-models.sh`: Document usage in README
- Standardize vllm-node image tags across all model blocks; add German non-technical explanation

---

## [0.6.0] — 2026-05-18

### Security
- Remove hardcoded credentials and absolute paths from `docker-compose.yml`

### Fixed
- Qwen3.6-35B-A3B-Uncensored: Disable thinking mode; expand context to 64K
- Qwen3.6-27B: Resolve OOM crash at startup

### Docs
- Sync `docker-compose.yml.sample` with live stack configuration (secrets scrubbed)
- `config.yaml.sample`: Clarify two vllm image families (vllm-node vs vllm/vllm-openai)
- README: Add private-registry support documentation

---

## [0.5.0] — 2026-05-06

### Added
- Private registry support: `REGISTRY` and `IMAGE_NAMESPACE` env vars for GitLab, Harbor,
  Nexus, and other non-ghcr.io registries
- `benchmark-models.sh`: Interactive wizard, quality detail report, robust model unload
- `benchmark-models.sh`: Tool-eval-bench integration for tool-call quality scoring
- `benchmark-models.sh`: `--arena` mode, coherence detection, spark-arena-cli integration
- `benchmark-models.sh`: S/M/L concurrent request groups and `--resume`

### Fixed
- Issues #6, #7, #8 in setup and build scripts

### Docs
- Expand launcher reference: `GMEM_OVERRIDE`, system RAM ceiling, environment variable plumbing

---

## [0.4.0] — 2026-05-03

### Added
- **`launch-vllm-auto.sh`**: `GMEM_OVERRIDE` knob — numeric value pins `gpu_memory_utilization`
  statically; `"adaptive"` / unset computes dynamically from free memory
- 126.5 GB system RAM ceiling (`SYSTEM_RAM_CEILING_GIB`) to prevent GB10 unified-memory crash
  when total system RAM approaches the hardware limit
- 5 GiB `u_cap` buffer (`GMEM_FREE_BUFFER_GIB`) to bridge `MemAvailable` vs `cudaMemGetInfo`
  discrepancy at vLLM startup
- `VLLM_SERVE_PREFIX` env var for images whose entrypoint is already `vllm serve`
  (e.g. `vllm/vllm-openai`)
- `PRE_LAUNCH_CMD` env var for in-container patching or setup before `vllm serve`

### Fixed
- Launcher: Use `/proc/meminfo` instead of `nvidia-smi` for memory queries (GB10 compatibility)
- Launcher: Shell-only implementation (awk/sed/grep) — no Python in the minimal llama-swap image
- Adaptive gmem for mod-script models; corrected pp display in output

---

## [0.3.0] — 2026-04-28

### Added
- **`launch-vllm-auto.sh`**: Generic adaptive `--gpu-memory-utilization` for vLLM — estimates
  required VRAM from safetensor weights + KV cache + safety headroom, picks the smallest
  utilization that satisfies the estimate within `[GMEM_MIN, GMEM_MAX]`

### Fixed
- `llama-swap` dynamic VRAM allocation for 122B model; fix docker-compose networking
- `benchmark-models.sh`: Support llama-benchy installed via pip in addition to uvx
- `gpu_memory_utilization` floor for 122B raised `0.60` → `0.82`

---

## [0.2.0] — 2026-04-19

### Added
- Sample configs for LiteLLM and llama-swap (sanitized)
- `benchmark-models.sh`: S/M/L concurrent groups, `--resume`, `--arena` mode,
  coherence detection, spark-arena-cli integration
- `tool-eval-bench` runner; tuned 122B launcher for tool calling
- README, TUTORIAL.md with setup guide, benchmarks, and model download commands

### Fixed
- `benchmark-models.sh`: Stabilize script; enhance coherence checks

---

## [0.1.0] — 2026-03-29

### Added
- Unified DGX Spark / Grace-Blackwell AI orchestration stack
- llama-swap orchestrator for on-demand model loading/eviction
- vLLM support for safetensors models (FP8, NVFP4, compressed-tensors)
- llama.cpp support for GGUF models
- Ollama support for pulled models via modelfile format
- LiteLLM unified API gateway on port 14000
- GB10 unified-memory optimizations across all services
- Docker publish workflow for multiple image builds

---

<!-- version diff links — update tags in GitHub after each release -->
[Unreleased]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.12.0...HEAD
[0.12.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mARTin-B78/dgx-spark_lite-llm_llama-swap_vllm_llama-cpp_ollama/releases/tag/v0.1.0
