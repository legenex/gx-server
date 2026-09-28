# Current state

**This file must always reflect reality.** If you are a new agent resuming this
work, read this first, then ARCHITECTURE.md (what is locked), then BLOCKERS.md.

## LATEST UPDATE — 2026-09-29 00:13 SAST, independent review found and D-048 fixed a live gx-max false-DOWN bug

An independent reviewer (run per this closeout's own checklist) found gx-max
**intermittently and falsely reporting "model is down" to real requests, live**
— reproduced it (`ServiceUnavailableError`, no fallback) — and root-caused it to
`GxMaxLifecycle._reconcile()` reusing the boot-time readiness probe (which
sends a real completion request) for its recurring 10s background health
check; under real concurrent load that probe request can queue behind live
traffic and exceed its own timeout, marking a perfectly healthy engine DOWN
and hard-refusing the next real user message. **Fixed** (`D-048` in
`coordination/DECISIONS.md`): the recurring check now uses a cheap
`/health` + `/v1/models` liveness probe that never touches the generation
queue; the full probe stays only at boot-time transitions. 247/247 orchestrator
tests still pass; deployed via `systemctl --user restart gx-orchestrator`
(re-adopted the running engine cleanly); **re-reproduced the original failure
condition on purpose post-fix** (6 concurrent real gx-max completions, slowest
29.1s, all succeeded) and confirmed zero false-DOWN events since, where the
same load pattern had produced 4 of them in the preceding 40 minutes.

The same review's second finding — `CLAUDE.md`/`ARCHITECTURE.md`'s locked L-6
row is now stale in every particular (says SGLang/NVFP4, live system is
vLLM/EXL3 — a different engine and model family) and the approving decision
for that change lives only in the untracked `state/DECISIONS.md`, not the
git-tracked `coordination/DECISIONS.md` — was deliberately **not** self-fixed:
changing a LOCKED row needs the human's explicit sign-off. See `B-035` in
`coordination/BLOCKERS.md` for the full detail and the risk this poses to a
future session that trusts the LOCKED table at face value.

---

## PREVIOUS UPDATE — 2026-09-29 00:13 SAST, closeout: swap restored + root-caused, gx-max verified live, stale-config drift fixed (D-047, closes B-039)

**Swap (B-039) — root cause found and fast-detection added, not just recreated.**
`/swapfile-sglang` was already recreated (48G, active, in fstab, root:600) by the
time this pass started; `journalctl`/`~/.bash_history` show the actual cause was
**not** any script in this repo — `ops/dsv41-prestart-drain.sh` and every other
automated path never touch swap. It was a deliberate, interactive, sudo-authenticated
`swapoff && sed /etc/fstab && rm -f /swapfile-sglang` at 22:06:35 from a real TTY,
immediately followed in the same shell history by testing the exact idempotent
recreate snippet later staged as B-039's own suggested fix — evidence points to an
earlier session reproducing the failure to validate the repair runbook, not a bug.
Since no code path can be patched to prevent a human-authenticated `sudo rm`,
what was added instead is fast **detection**: `legenex/host/gx-hostwatch.sh`
(unprivileged, already running every 60s) now has a `check_swap()` step that
alerts within one cycle if the swapfile ever goes missing or undersized again,
instead of the previous gap where it was only ever caught on the next gx-max
launch attempt. Full detail in `coordination/BLOCKERS.md` B-039.

**gx-max booted through the real lifecycle, verified live, not just "container running."**
`legenex/lifecycle/gx-max-start.sh` (thin wrapper around `mia-dsv41/start.sh`,
vLLM TP=2, DeepSeek-V4.1-Flash-EXL3-2.9bpw) ran preflight → drain → admission →
launch → health-verify with no bypass; READY at 23:25:14, ~9 min cold boot.
Proven with real inference, not a health-check alone: direct engine call
(`17*19=323`, correct `model` field, `system_fingerprint` tp2), gateway streaming
(`curl -N` SSE, correct), gx-auto routing journal (real `context_limit:600000`,
real token counts/tok-per-s), and gx-max/gx-auto/cptr-gx-cluster all answering
correctly through the real production Open WebUI backend (as the canonical admin,
via the same session-minting Open WebUI's own sign-in uses — see below) and
through a real headless-Chrome browser against `https://chat.legenex.co` (disposable
acceptance account, D-038 pattern, for gx-auto/gx-max; `cptr/gx-cluster` is
deliberately admin-only per D-043 so was proven via the in-process admin path only).
**Note on self-identity questions:** gx-max answered "0xKitkat/Ornith-1.5-35B..." and,
on a repeat, "GPT-4" when asked its own underlying model name — reproduced with an
identical answer pattern calling the vLLM engine directly (bypassing every layer of
routing), so this is model self-confabulation (a known behavior of uncensored
fine-tunes), not a routing or config bug. Arithmetic, the API `model` field, and
`system_fingerprint` are the reliable signals and all check out.

**Real browser finding, narrow, not blocking:** a near-instant single-token-ish reply
(e.g. trivial arithmetic) can arrive with `</think>` and the visible answer in the
*same* SSE delta; Open WebUI's frontend then appears to render nothing visible (the
whole delta gets treated as reasoning). A realistic multi-chunk reply (asked the
model to explain something in two sentences) rendered correctly ("Thought for 2
seconds" then the real answer), and this is Open WebUI's own (0.11.4) frontend
parsing, not something in this repo to patch. Left for a human to decide whether to
raise upstream or work around; not a blocker for real conversational use.

**OpenWebUI/Computer restore gap fixed at the source (not just restarted by hand).**
`legenex/lifecycle/restore-normal.sh` was rewritten for V4.1 on 2026-09-27 and never
learned about `ops/dsv41-prestart-drain.sh`'s `RESTORE_MARK` file — it restored only
the node-1 control plane, so anything the drain stopped (open-webui, gx-computer,
runtime-masked RDP units) silently stayed down after every gx-max release. This is
the literal mechanism behind D-046's incident and, unfixed, would have repeated
after this pass's own gx-max boot too. `restore-normal.sh` now reads the marker,
restarts exactly what was drained (`docker start`, not a redeploy), unmasks the RDP
units without force-starting them, and archives the marker after use. Verified live:
running it (which this pass needed anyway, to bring the apps back up) restored both
containers, `chat.legenex.co` → 200, data counts unchanged.

**Data preservation, verified via read-only queries against the real production DB,
matching the last known-good fingerprint exactly:** 4 users, 59 chats, 277 messages,
1 memory, 3 files, 1 folder, 1 note; canonical Nick Allen
(`b7e76ad1-f055-45ba-8cdb-9ea80a0e2519`, admin, 59 of the 59 chats) unchanged
throughout, including after gx-max's boot/drain cycle and the provisioning fix below.

**Computer was offering two retired models; compaction was pointed at a dead one —
both were live production bugs until this pass, now fixed via the normal
provisioning path.** `legenex/computer/tools/provision.py`'s `GX_COMPUTER_MODELS`/
`MODEL_ORDER` still listed `gx-mini`/`gx-code` (retired 2026-09-27) — Computer's own
`/api/chats/models` was actually offering them live, and selecting either would have
failed since the gateway (`PUBLIC_ALIASES = (gx-max, gx-auto)`) no longer serves
them. Separately, and more seriously, the **live** Open WebUI
`chat.context_compaction.model` config was still `gx-mini` — any real conversation
crossing the 20000-token threshold would have tried to summarise through a model
that no longer exists. Fixed at the source (`GX_COMPUTER_MODELS = ["gx-auto"]`,
`COMPACTION.CONTEXT_COMPACTION_MODEL = "gx-auto"`; gx-max stays deliberately excluded
from Computer per D-043/D-044) and re-applied through `provision.py` (idempotent,
sanctioned) rather than hand-patched — verified live afterward:
`/api/chats/models` → `["gx-auto"]`, DB `chat.context_compaction.model` → `"gx-auto"`,
data counts still unchanged.

**`ops/git-sync/integrity-audit.sh` had three dead entries in its `CRITICAL` file
list** (`docker-compose.node02.yml`, `llama-swap/node0{1,2}.yaml`) — files removed
with the llama-swap retirement (`state/CLEANUP-EVIDENCE-20260927.md`) that the
audit's own file list was never updated to drop, so it was failing on a correctly
completed retirement rather than a real integrity problem. Removed from the list
(not resurrected). Audit now: `PASS=16 WARN=2 FAIL=1` on gx10-01. The one remaining
FAIL (`gx-litellm media key differs from .env`) and one of the two WARNs
(`open-webui gx-* identity entries differ from the registry`) are **not** artifacts
of this pass:
* the media-key check is a symptom of the already-flagged, still-open **B-035**
  drift (the live gateway now serves only `gx-max`/`gx-auto`; the media-router
  wiring this check expects was dropped along with everything else B-035 already
  described as undecided) — fixing it for real means resurrecting media routing or
  formally retiring L-10's media aliases, either of which needs the human, not a
  silent fix here;
* the identity-registry check invokes `gx_control_ui.owui_identity`, whose source
  no longer exists in the repo (only a stale `.pyc` survives in the abandoned
  `.kilo/worktrees/spurious-temper/` worktree) — a pre-existing tooling gap, not a
  live defect, and out of this pass's scope to reconstruct.
The other WARN (0 uncommitted paths, "autosync pending?") was this pass's own edit
to the audit script, settled by the next autosync cycle.

**Git:** all of this pass's fixes (swap detection, restore-normal.sh,
provision.py, integrity-audit.sh) went through the normal autosync pipeline —
gx10-01 HEAD stayed equal to `origin/main` throughout, no manual push needed.

**Independent review:** see the entry immediately below this one (or the next dated
entry above it, if the review ran after this was written) for what an independent
pass found and whether it was repaired.

**Root cause (evidenced, not guessed).** The DeepSeek V4.1 Flash rebuild
mission's `ops/dsv41-prestart-drain.sh` ran `docker stop open-webui` and
`docker stop gx-computer` at 11:33 to free head-node memory for gx-max's
boot (see `state/HANDOFF.md`, `state/gx-max-lifecycle.log` — 11:11:42 shows
open-webui/gx-computer among the containers that made the first boot attempt
fail on headroom). gx-max was cleanly released at 14:46 (`stop.sh`, not a
crash), but nobody restarted the two app containers afterward. `cloudflared`
stayed up throughout, correctly proxying `chat.legenex.co` → `localhost:3000`
— hence the Cloudflare 502 (nothing listening) and the blank page on `:8000`.

**Data integrity, before touching anything.** `PRAGMA integrity_check` = ok.
Counts identical to the last known-good fingerprint: 4 users, 59 chats, 277
messages (derived from `chat.history.messages`), 1 memory, 3 files, 1 folder,
1 note. Canonical Nick Allen (`b7e76ad1-f055-45ba-8cdb-9ea80a0e2519`, admin)
unchanged. Fresh backup taken before recreating anything:
`/srv/projects/gx-cluster/backups/open-webui/20260928T195959Z-pre-restart-after-dsv41-drain/`
(webui.db + counts.json + compose.yaml + image digest).

**Repair.** `docker compose -f /opt/open-webui/compose.yaml up -d` (the
external `open-webui` volume was untouched — only the container had been
removed, not the volume) and `docker compose -f
legenex/computer/docker-compose.computer.yml up -d`. Both external volumes
(`open-webui`, `gx_computer_data`) reused as-is; no schema reset. Counts
verified identical post-recreate and again after a container restart.
`chat.legenex.co` → HTTP 200, real HTML, hardened response headers present
(HSTS/nosniff/SAMEORIGIN/CORS pinned, matching the D-045 compose). Computer
`http://100.105.214.61:8000` and `127.0.0.1:8000` → 200; single identity Nick
Allen, workspace `GX-Cluster` at `/projects/gx-cluster`, read-only overlays
(`legenex/control-ui` etc.) still enforced, `docs/` still writable.

**Alias reconciliation (user decision, on discovering the conflict live).**
`legenex/gateway/litellm/config.yaml` and `state/DECISIONS.md` D-M1 confirm
gx-mini/gx-code were deliberately retired as part of the DeepSeek V4.1
rebuild (git tag `pre-deepseek-v41-rebuild-20260927`; user-approved per the
mission brief) — `gx-llama-swap-node01` no longer exists. This conflicted
with the (now-stale) assumption that gx-mini/gx-code were still live. User
chose to reconcile Open WebUI to the live two-alias reality rather than
resurrect the retired backend: `openai.api_configs["0"].model_ids` trimmed
from `[gx-auto, gx-mini, gx-code, gx-max]` to `[gx-auto, gx-max]`; `model`
rows `gx-mini`/`gx-code` set `is_active=0` (same pattern already used for
`gx-fast`/`gx-reason`) rather than deleted. Verified in the DB and via a
container restart; data counts unchanged throughout.

**gx-max verification — attempted, blocked (B-039).** On-demand acquire
through a plain chat request does not auto-boot the model (immediate 503) —
the real trigger is `legenex/lifecycle/gx-max-start.sh`, which unconditionally
re-runs the same prestart drain before admission (non-bypassable by design).
User approved a real boot for verification, accepting that Open WebUI/Computer
would be stopped again. The drain ran fine and stopped both apps again, but
admission then **hard-refused**: `/swapfile-sglang` (L-8, 48G required) does
not exist on node1's disk at all right now (only the default 16G `/swap.img`
is active; `swap_free_gib=14.1` vs `min_swap_free_gib=40.0` needed for the
load transient). This needs root (`sudo`, not available to this session) to
recreate and activate — see `coordination/BLOCKERS.md` B-039. Open
WebUI/Computer were brought back up immediately after the refusal and
re-verified (same counts, same canonical user, both 200). gx-max/gx-auto
correctly 503 until B-039 is resolved by a human; this morning's 13:23–13:44
run (before whatever removed the swapfile) already proved the model itself
works end to end (real completions, tool calls, uncensor probes), so the gap
is infrastructure (missing swapfile), not the model or the gateway wiring.

## UPDATE — 2026-09-26, security cleanup completed (D-045)

Supersedes nothing below (the D-043 integration still stands, verified again
after these changes: `verify.py --compaction` **38/38**, live, post-change).

**Credentials (D-045, closes B-036):**

* **`LITELLM_MASTER_KEY` rotated.** The last master-key consumer — the hermes
  agent on the London VPS (tailnet `srv1906439`) — was first migrated to its
  own scoped key `hermes` (gx-mini/gx-code/gx-auto/gx-max, rpm 300, 4
  parallel), updated in place on the VPS including the stale copy its
  dashboard inherited from the jarvis sidecar. After 7+ minutes of zero
  master-key traffic the master was rotated with the B-024/B-027 procedure
  (backup → new value → `env -i` recreate of gx-litellm → restart
  gx-orchestrator + gx-control-ui). Old value refused (401), new value
  verified through Control Center key management, orchestrator auth and a
  full gx-auto round trip. The key table is now exactly eight purpose-named
  scoped keys; `kilo-code` (broad, unused since 09-23) is blocked
  (reversibly) and two never-used unnamed keys were revoked.
* **`GX_SWAP_API_KEY` NOT yet rotated** — staged and ready: `rotate-swap-key.sh`
  (bearer keys via a curl config on a pipe, never argv; preflight PASS
  2026-09-26 22:23 UTC; backs up and rolls back). Executing reloads all text
  models: ~1 min for gx-mini, ~4 min for gx-code, gx-auto follows; ~5 min
  total with gx-max down. Needs an authorized window (B-037).

**Orchestrator auth (D-044) live and verified:** every route except `/health`,
`/healthz` and `/` needs `Authorization: Bearer $GX_ORCHESTRATOR_API_KEY`
(constant-time, fail-closed). Tests: 4 auth unit tests, `test_lifecycle_events`
10/10, Control Center auth tests 2/2. `tests.test_server` still has 2
pre-existing drift failures (B-038; the baseline commit had 6 errors).

**Open WebUI hardened (D-045, staged in B-037):** HSTS, `nosniff`, referrer
policy, `SAMEORIGIN`, CORS/Socket.IO pinned, session secret persisted (same
value, nobody logged out). Data volume reused; counts identical before/after
(4 users, 59 chats, 1 memory, 3 files, 1 folder, 1 note). Backup
`backups/open-webui/20260926T221601Z-prehardening/`. The disabled Nous Portal
connection is confirmed gone (zero references in the config table).

**Computer containment re-verified from inside the container:** all unit
`ExecStart` paths into the repo read-only, `.env` symlink dangles, ordinary
source writable, `gx-max` absent from Computer's connection (models
gx-auto/gx-mini/gx-code, key rpm 120 / 4 parallel / 600k tpm), and llama-swap
upstreams are unreachable from any container on `gx_gateway` (B-034 items
1/3/4/5 closed).

**Known drift the human must decide on (B-035):** the live gateway serves
gx-mini, gx-code, gx-auto, gx-max (gx-fast/gx-reason/gx-image/gx-video/
gx-voice/gx-music retired from the gateway) and gx-max defaults to the
dual worker with SGLang as `GX_MAX_MODE=deepseek` — L-6/L-10 in CLAUDE.md
still describe the old topology. No decision entry records approval, so the
locked rows were left untouched.

**Also noted:** GX-Playground was deliberately stopped 2026-09-24 (its user
unit no longer exists) — pre-existing this pass, not resurrected. The
remaining Cloudflare decisions (Always Use HTTPS/HSTS zone toggle, Access
with MFA, shorter JWT expiry), the Nous provider-side revocation (interactive
login) and the transcript cleanup (after the swap rotation) all need the
human.

## UPDATE — 2026-09-26 (Open WebUI ↔ Computer integration repaired end to end, D-043)

Supersedes the two 2026-09-25 entries below: everything they list as "not
done" or "UI step" is now done, through the apps' own APIs.

* **Open WebUI data untouched.** 4 users, 59 chats, 277 messages, 1 memory and
  3 files are unchanged, with identical content hashes against the pre-change
  backup. Added: the **GX-Cluster** folder and its instructions note.
* **Identity:** Computer's only user is the Open WebUI admin, with the same
  login name, display name and avatar. No SSO exists between these versions;
  see D-043.
* **Computer:**
  * Workspace **GX-Cluster** = `/projects/gx-cluster`; the empty
    `/home/cptr` default was retired.
  * `CLAUDE.md` is injected via `.cptr/system.md` placeholders.
  * LiteLLM connection with key `gx-computer` (4 aliases, default `gx-auto`).
  * Gateway key lives only in Open WebUI.
  * CORS pinned, `cap_drop: ALL`.
  * Git is read-only inside Computer, with read-only directory overlays for
    everything the host runs unattended.
  * The gateway `.env` now lives in `/srv/projects/gx-cluster/secrets/` (the
    tree holds a symlink).
  * `.cptr/*` is gitignored and refused by autosync.
* **Open WebUI:**
  * LiteLLM connection moved off the **master key** to virtual key
    `open-webui`.
  * Computer connection with 5 conversation headers. The model is
    **GX-Cluster - /projects/gx-cluster** (`cptr/gx-cluster`), admin-only.
  * Compaction **on**: gx-mini summariser, 20000 threshold, sized to
    gx-mini's real 32 768 window (B-032).
  * Community sharing and Arena off.
* **Credentials:** rotated and verified `POSTGRES_PASSWORD`,
  `LITELLM_UI_PASSWORD`, `GX_MEDIA_API_KEY`, `GX_VOICE_API_KEY`, twice. The
  second rotation followed a 14-minute overlay gap (B-033).
  `LITELLM_MASTER_KEY` and `GX_SWAP_API_KEY` await a decision (B-033).
  Security follow-ups needing sign-off: B-034.
* **Proof (final run, 2026-09-26 ~22:10 SAST):**
  * `python3 legenex/computer/tools/verify.py --compaction`: **37/37**, live
    end to end. Every Computer file operation is checked on the host, plus
    git, terminal, overlays, the gateway, agent inference and the long-chat
    compaction.
  * `ops/git-sync/integrity-audit.sh`: 19 PASS / 0 WARN / 0 FAIL.
  * `ops/git-sync/tests/sync-regression.sh`: 19/19.
  * `legenex/computer/tools/test_tools.py`: 14/14.
  * Independent security re-review: C-1, H-1b, H-2 and M-2 closed. Open items
    are B-033, B-034 and B-035.
* **Re-apply anything:** `python3 legenex/computer/tools/provision.py`
  (idempotent).
* **Backups:** `/srv/projects/gx-cluster/backups/computer-repair-20260925T225949Z/`
  (pre-change) and `…-post` (post-change).

## UPDATE — 2026-09-25 (verification pass, Open WebUI + Computer)

Verified live: memory CRUD (in-process, throwaway test user, cleaned up, legit
memory byte-identical); Computer workspace create/edit/rename/delete visible on
the host; gateway models reachable from inside `gx-computer`; Open WebUI →
Computer `:8000/api/health` 200. Added read-only overlays for `.git/hooks`,
`.githooks`, `ops/git-sync` in Computer. **Not done (need an admin in the UI):**
Computer first-admin claim (users=0, setup token still in `docker logs`),
Computer gateway key + Open WebUI connection, and enabling Open WebUI context
compaction (`chat.context_compaction.*`, still off; see
`legenex/open-webui/README.md`).

## LATEST UPDATE — 2026-09-25 (Open WebUI 0.11.4 + Computer 0.9.21)

Open WebUI on gx10-01 is pinned to **0.11.4** (`open-webui`, host `:3000`,
volume `open-webui`). Personal memory, system-context injection, background
review (10 turns, 8000/12000 char budgets), folders and notes are enabled.
Identity entries (`gx-mini`, `gx-code`, `gx-auto`, `gx-max`) use native
function calling and Memory builtin tools. Backup:
`/srv/projects/gx-cluster/backups/open-webui/20260925T062453Z/`.

Open WebUI Computer is **0.9.21** as `gx-computer`, Tailscale
`http://100.105.214.61:8000` + loopback `:8000`, volume `gx_computer_data`,
host `/home/legenex/Documents/Projects/Server` → `/projects`. It joins
`gx_gateway` and reaches LiteLLM at `http://gx-litellm:4000/v1`. First admin
login, LiteLLM connection, Grok login, and Computer gateway key are UI steps
(`legenex/computer/README.md`).

Live public aliases are unchanged: **gx-mini, gx-code, gx-auto, gx-max**.
Loopback also lists internal workers **gx-code-01, gx-code-02**. gx-max was
not started.

## LATEST UPDATE — 2026-09-17 21:35 SAST (Build V3 integration: the complete creative + realtime product is deployed) — read this first

Eleven public aliases (L-10 as amended by D-040). This pass took the Build V3
workstreams from "code written" to "deployed and provable", and fixed the
reason that was hard to see: **the deployed apps were serving a snapshot of the
checkout taken at start-up** (B-031).

### The deployment bug that hid everything else (B-031, D-041)

`Static`/`StaticFiles` read `web/` into memory once per process. The Playground
had been running since 10:29 while the frontend was edited until 17:17, so:

* 21 of 32 files were **stale**, and
* 11 files that existed on disk returned **HTTP 404** because they were created
  after start-up — including `js/routes.js` and the whole Voice, Models, Logs
  and Settings pages.

Both servers now re-read a file when its mtime, size or inode changes, and
`legenex/{playground,control-ui}/scripts/deploy.sh` is the only sanctioned
deployment path: it rebuilds what needs rebuilding, restarts only when the
Python package changed, and then **fails unless the ETag of every served file
matches sha256 of the file in the checkout**. Current: `43 files served match
the checkout; 0 stale, 0 not served`.

### The deployed GX-Playground (verified in a real browser)

`legenex/playground/e2e/live.navigation.spec.js` runs against the deployed site
and **passes 14/14**. Evidence and screenshots:
`/srv/logs/acceptance/build-v3/plt/navigation-final/`.

| Group | Pages |
|---|---|
| **Create** | Dashboard · Creative Flows · Images · Video · Music · Voice |
| **Realtime** | Live · Call Agents |
| **Manage** | Library · History · Models · Logs · Settings |

No dead links, no placeholder pages, the Control Center link is in the header,
and every page opens without console or network errors, is keyboard-reachable
with a visible focus ring, has an accessible name on every visible control, and
reports **no axe WCAG 2.2 AA violations**.

Two page-level defects were found and fixed by that spec:
* **Music was dead** — `web/js/pages/music.js` used six `music-form.js` exports
  (`aiPanel`, `conditioningPreview`, `lockButton`, `referencePanel`,
  `styleTagEditor`, `vocalControls`) and never imported the module, so the page
  rendered only "This page could not be loaded: aiPanel is not defined".
* **Creative Flows had no frontend entry point at all** — no `main.tsx`, no app
  shell, no stylesheet, so `vite build` could never have run.

### Services

| Node | Service | Address | State |
|---|---|---|---|
| gx10-01 | Control Center | :8088 | healthy |
| gx10-01 | GX-Playground | :8090 **and :8443 (HTTPS)** | healthy |
| gx10-01 | LiteLLM gateway | :4000 | healthy |
| gx10-01 | orchestrator | :18900 | healthy |
| gx10-02 | media router | 192.168.100.11:18800 | **2.5.0** (was 2.4.1), 15 workflows, LoRA mounts read-only |
| gx10-02 | gx-music supervisor | :18820 | healthy, engine unloaded |
| gx10-02 | **gx-voice** supervisor | :18830 | healthy (new) |
| gx10-02 | **gx-live** supervisor | :18850 | healthy (new), estimate 34 GiB |
| gx10-02 | gx-call supervisor | :18840 | **not yet running** — its engine image is still being built |

**HTTPS is now real.** It had never been wired: `gx_playground/tls.py` existed
but there were no certificates, no port and no setup script. There is now a
local private CA in `/srv/projects/gx-cluster/secrets/playground-tls/` (keys
0600), the unit runs `python3 -m gx_playground.tls ensure` at every start
(creates on a fresh node, renews within 30 days of expiry), and the listener
binds loopback + Tailscale only. `https://100.105.214.61:8443/` and
`https://127.0.0.1:8443/` answer; the public CA certificate downloads from
`/pg/ca.crt`. **This is what the Live and Call Agents pages need for
`getUserMedia`** — plain `http://100.105.214.61:8090` is not a secure context.

### Database

All eight migrations are applied to `/srv/projects/gx-cluster/media/metadata/library.db`
(`010` provenance, `020` WAN, `030` flows, `040` voice, `050` call, **`060` live**,
**`070` images**, `080` platform). 060 and 070 did not exist at the start of this
pass; both were dry-run against a copy before being applied. `integrity_check ok`,
29 assets intact, 38 tables across the `img_ / live_ / call_ / voice_ / wan_ /
flow_ / plt_` prefixes.

### Measured footprints

| Workload | Baseline | Minimum | Growth | Notes |
|---|---|---|---|---|
| T2I 1024x1024 x1 | 113.8 | 56.69 | 57.1 GiB | previously measured |
| T2V 640x640 x33 | 112.2 | 40.26 | 72.0 GiB | previously measured |
| **I2V 640x640 x33** | **111.9** | **40.59** | **71.3 GiB** | **measured this pass** — the missing one; 0 s below the reserve, 82.2 s total, memory returned to 112.5 GiB. Evidence `/srv/logs/acceptance/media-footprint-20260917T183849Z/` |
| Wan t2v + LoRA pair, 640x640 x49 | 112.56 | 39.26 | 73.30 GiB | WAN live acceptance, 1 087 samples at 1 Hz |
| gx-live (MiniCPM-o 4.5) | — | — | 34 cold / 31 resident, 102 s | probe 1, in the registry |

I2V costs essentially the same as T2V of the same size. Neither can run beside
gx-reason (≈32 GiB): 71.3 + 32 + 30 > 121.6 GiB.

### Live facts (21:35)

| | Kernel | MemAvailable | Swap used | Disk free |
|---|---|---|---|---|
| gx10-01 | 6.17.0-1032-nvidia | 48 GiB | 6.8 GiB of 63 | 483 GiB (45 % used) |
| gx10-02 | 6.17.0-1032-nvidia | 111 GiB | 3 GiB of 63 | 217 GiB (76 % used) |

Resource profile **auto**; no maintenance hold, no gx-max hold, no pins.
gx-max has not been started at any point in this pass.

### What each Build V3 workstream proved

| WS | State |
|---|---|
| **IMG** | Model selector traced end to end (`image_model` is the real field); VisionmasterPro_V3 verified free of Qwen-specific prompt rewriting, filtering and adapters; the edit-strength suspect was already fixed and is pinned by a regression test; migration 070 added; near-duplicate detection added. Live generation acceptance is scripted and ready but **not yet run**. |
| **WAN** | Router 2.5.0 deployed; **real LoRA acceptance**: 4 generations, branch placement traced on the real graph (no shared nodes), same seed with/without the pair gives different sha256 and +34 % bitrate, ffprobe confirms 49 decoded frames. |
| **VOI** | **Deployed and accepted**: 7 real generations, 56/56 checks, verified independently with ffprobe (matching the service's own loudness to 0.1 dB) and offline ASR (WER 0.0-0.167). Measured 9.45 GiB cold / 6.5 GiB resident / 35.0 s. Unload 13/13 including 409-while-busy and 409-while-pinned. Gateway hop proven with real audio. |
| **LIV** | Control Center integration, Live page, migration 060, supervisor running. Live GPU acceptance in progress. |
| **CAL** | `test_calls.py` 3 failures + 1 error → **15/15 OK**; two were real bugs (a falsy audit sink, and `end_session` waiting on a poller that never runs offline). Call Agents page deployed and axe-clean. Engine image build outstanding. |
| **FLO** | Backend was real (58 tests); the **editor had no entry point at all** — no `main.tsx`, no app shell, no stylesheet. Written, built, deployed, 9/9 offline E2E including a real end-to-end flow run and axe-clean on all three views. |
| **MUS** | Page was dead on arrival (`aiPanel is not defined`) and is now live. Music acceptance in progress. |
| **PLT/LEAD** | Deployment contract, HTTPS, the realtime tunnel, registry, migrations, the Hugging Face diagnosis, and the deployed-site gates. |

### gx-reason: NEW MODEL, live and accepted (D-042, closes B-030)

gx-reason serves **`wyattearp/Qwen3.8-27B-Uncensored-NVFP4`** @
`91ec573a3d8e660b78b7161395e4a5b6247c2c8b` — "Qwen3.8-27B Dense Uncensored
NVFP4". Installed, revision-pinned and verified on 2026-09-18: all 22 files
checked against that revision on disk (26.61 GiB, 14 sha256-checked,
`.gx-manifest.json` written).

This replaced **both** earlier identities at the user's explicit instruction:

* the interim `nvidia/Qwen3.6-27B-NVFP4` — **deleted from gx10-02**, not a
  fallback, not to be reinstalled;
* the previously planned `iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` —
  **abandoned**. **B-030 is closed as obsolete**: the Hugging Face gate still
  exists, but the model behind it is no longer wanted, so nothing about
  gx-reason is waiting on a human.

Not to be confused with the retired `gx10-vllm/Qwen3.8-27B-Uncensored` runtime,
which stays retired — that is a local runtime folder, this is an upstream NVFP4
repository at a pinned revision.

**The engine did not change.** The checkpoint is
`Qwen3_5ForConditionalGeneration`, the same hybrid-attention family the existing
`jstarkg/vllm-gb10-flashnext:0.28-sm121-r6` image already serves for gx-fast;
support was verified inside the deployed image before any config was edited.
Two flags changed with reasons: `--quantization modelopt` removed (this
checkpoint declares `compressed-tensors`/`nvfp4`, which vLLM auto-detects), and
`--speculative-config` (MTP) removed for bring-up.

**Measured live, 2026-09-18** (not inherited from the old model):

| | |
|---|---|
| Cold load | 392 s |
| Decode | ~9 tok/s (no MTP) |
| MemAvailable idle / loaded / after unload | 114.68 / 63.45 / 114.68 GiB |
| Real node footprint | **51.2 GiB** at `--gpu-memory-utilization 0.42` |
| Context | 65536 |

Accepted end to end: multi-step reasoning (11:36 / 156 km, correct), a coding
debug task (named both the in-place mutation and the even-length bug),
reasoning separated from content, tool calling through `qwen3_xml`, and
**vision** — it read the red jacket out of a real generated image. Served
through LiteLLM as `gx-reason`; gx-auto escalates to it for hard prompts
(proved in `gateway-text.jsonl`). Unloads cleanly and the memory returns.

## Previous update — 2026-09-17 11:00 SAST (final integration pass: gx-music, GX-Playground, Resource Control)

**Eight public aliases (L-10 amended by D-036).** Everything below was
measured live. Evidence: `TEST_RESULTS.md` §20 and
`/srv/logs/acceptance/final-20260917T075129Z/`.

| Alias | Model | Where | State |
|---|---|---|---|
| gx-mini | HauhauCS Qwen3.5-4B Q4_K_M | gx10-01 llama.cpp | resident, accepted |
| gx-fast | kyaky Qwen3.6-35B-A3B NVFP4 | gx10-01 vLLM | resident, accepted |
| gx-reason | **interim** `nvidia/Qwen3.6-27B-NVFP4` | gx10-02 vLLM, on demand (ttl 900) | serving; the required iSkye model is still gated (**B-025**, no HF token reached gx10-01) |
| gx-max | `dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4` | both nodes, SGLang TP=2 | accepted again today via the Control Center MAX profile (acquire 671 s, release 50 s); never auto-started |
| gx-auto | deterministic router | gx10-01 orchestrator | Kilo routing mini / fast / reason verified live |
| gx-image | Qwen-Image-2512 / Edit-2511 | gx10-02 ComfyUI via media router **2.3.0** | on demand |
| gx-video | Wan 2.2 A14B t2v / i2v / keyframe edit | gx10-02 ComfyUI via router 2.3.0 | on demand |
| **gx-music** | `ACE-Step/acestep-v15-xl-turbo` @d4a0b288 (+ 5 Hz LM 4B, runtime ACE-Step-1.5 @ca1e85f) | gx10-02: `gx-music.service` supervisor (192.168.100.11:18820) plus an on-demand engine container | on demand, idle unload after 10 min, router-mediated eviction **on** |

**Services on gx10-01.**

| Service | Address | Role |
|---|---|---|
| Control Center (`gx-control-ui.service`) | :8088 | admin |
| **GX-Playground** (`gx-playground.service`) | :8090 on 127.0.0.1 and 100.105.214.61 | creative app, plus the public music API `/v1/music/*` authenticated with LiteLLM virtual keys |
| LiteLLM | :4000 | gateway |
| orchestrator | :18900 | gx-auto routing and gx-max lifecycle |
| llama-swap node01 | — | gx-mini, gx-fast |

**New in the Control Center.**
* **Resource Control:** profiles Auto / Text / Media / Music / Max /
  Maintenance, a live resource map, and LOAD / UNLOAD / DRAIN / PIN / UNPIN
  through admission. The compatibility view is computed from live data.
* **Storage & Cleanup:** both nodes, SAFE / REVIEW / PROTECTED
  classification, opaque ids, re-checked before deleting, never
  `prune -a`.
* **Setup:** Kilo Code, Open WebUI and generic OpenAI clients.
* **Model Manager:** disk preflight.
* **Other:** a gx-music card, and a Creative page and nav link pointing to the
  Playground.

**State files and holds.**
* Guard state lives in `/srv/projects/gx-cluster/state/guard/`:
  `profile.json`, `pins.json`, `node{1,2}.maintenance-hold` and
  `node2.gxmax-hold`.
* The gx-max drain sets `node2.gxmax-hold` and waits for the music
  supervisor to unload (verified: container, ledger and processes).
* The router, the music supervisor, the guard and gx-reason's llama-swap
  command all honour Maintenance.

**Current live facts (10:26).**

| | Kernel | RAM available | Swap used | Disk free |
|---|---|---|---|---|
| gx10-01 | 6.17.0-1032-nvidia | 57 GiB | 4 GiB of 63 | 492 GiB (44 %) |
| gx10-02 | 6.17.0-1032-nvidia | 56 GiB | 3 GiB of 63 | 328 GiB (63 %) |

Both nodes are HEALTHY in Storage & Cleanup, and the kernel verifier passes
13/13 on each. The old "gx10-02 at 98 %" state (B-026) no longer applies.

**Fixed in this pass.**
* B-027: a gx-max release could recreate gx-litellm with a stale media-key
  placeholder.
* Music API: `completed` now means downloadable; revoke takes effect
  immediately; API remix lineage is kept.
* Playground → Control Center link keeps the host.
* API key Replace keeps the name.
* Music renders interrupted by gx-max are re-queued.

**Open.**
* **B-025:** a human must save the HF token in Model Manager.
* **B-023:** monitored; the node-1 gx-max load transient still reaches the
  swap ceiling for a moment.

## Previous update — 2026-09-17 (V2 migration)

**The cluster runs the V2 model set, and the Control UI can now create media,
manage models and issue API keys.** Everything below was measured on the live
cluster. Evidence is in `TEST_RESULTS.md` §19 and `/srv/logs/acceptance/`.

### Models now bound (source of truth: `legenex/models/registry.json`)

| Alias | Model (pinned revision) | Node / runtime | State |
|---|---|---|---|
| gx-mini | `HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive` @c09cdbcd, Q4_K_M + mmproj | gx10-01 llama.cpp | accepted, preloaded |
| gx-fast | `kyaky/Qwen3.6-35B-A3B-Uncensored-NVFP4` @33d5cf83 | gx10-01 vLLM 0.28 | accepted, preloaded, ttl 0 |
| gx-reason | **interim** `nvidia/Qwen3.6-27B-NVFP4` @0893e160 | gx10-02 vLLM 0.28 | the required `iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070` is gated and neither node has an HF token (**B-025**) |
| gx-max | `dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4` @c66fe384 | both nodes, SGLang TP=2, cookbook cell `fp4` (b12x MoE runner) | accepted (D-032); rollback is `GXMAX_MODEL_DIR=…/DeepSeek-V4-Flash-0731-NVFP4 GXMAX_QUANT_CELL=nvfp4` |
| gx-auto | deterministic classifier (D-030) | gx10-01 orchestrator | Kilo-aware; never starts gx-max |
| gx-image | Qwen-Image-2512 + tumblr LoRA; Qwen-Image-Edit-2511 | gx10-02 ComfyUI via media router 2.2 | generate / edit / variation |
| gx-video | Wan 2.2 A14B T2V / I2V + uncensored LightX2V LoRAs; keyframe video edit | gx10-02 ComfyUI via media router 2.2 | t2v / i2v / video edit |

### Control UI additions (D-034, D-035)

New pages: **Create**, **Media Library**
(`/srv/projects/gx-cluster/media`, SQLite), **Model Manager** (HF search and
lookup, stage, verify, test-serve, assign with automatic rollback,
accept, delete-if-unused) and **API Keys** (LiteLLM virtual keys; the secret
is shown once and the master key never reaches the browser). New docs pages:
Kilo Code, Open WebUI, Clients, Media, Model Manager. The unit now has
`MemoryMax=1G`. A local-only `acceptance` account exists for automated live
tests (password in `…/secrets/control-ui/acceptance-password`, 0600; login
only from 127.0.0.1).

### What changed in the running system

* **LiteLLM** was recreated with the new context/output limits and gx-video
  `mode: video_generation`. **llama-swap node01** preloads gx-mini and gx-fast.
  gx-fast has `gpu_memory_utilization 0.34`. It was reloaded through the UI
  at that value; with mini and fast loaded, node 1 has 58 GiB MemAvailable
  (§19.1).
* **Orchestrator** restarted. It has the new classifier and a routing journal in
  `/srv/logs/gx-auto-routing.jsonl` (`GET /routing/decisions`). It returns 503
  `gx_max_not_running` instead of silently downgrading.
* **Media router 2.2.0** is deployed on gx10-02 with `legenex/media/deploy-node2.sh`.
  It adds uploads, edits, variations, i2v and video edit. It frees ComfyUI
  after 600 s idle, and on request through `POST /v1/admin/free`. It refuses a
  job node 2 cannot hold: video needs about 76 GiB free, so it does not run
  while gx-reason is loaded. Unload gx-reason in the UI first.
  **ComfyUI** now runs with `--reserve-vram 40`, but that does not bound its
  host-side cache: a video edit can leave only 14 GiB free on node 2.
* **node-2 llama-swap:** gx-reason's start command first asks the router to
  free ComfyUI (`free_node`). The file is deployed to `~/gx-gateway/node02.yaml`,
  and the container was restarted.
* **B-024 resolved:** the media router key is rotated on both nodes. The hashes
  match, and it was not printed.

### Open

* **B-025:** gx-reason still uses the interim 27B model. Fix: accept the
  model terms at huggingface.co/iSkye/Qwen3.8-Flash-Next-NVFP4-ablit-a070,
  save a read token in Settings → Model Manager (or
  `/srv/projects/gx-cluster/secrets/hf/token`, 0600), then stage it on
  gx10-02 from the Model Manager.
* **B-026:** deleting obsolete checkpoints was refused by the agent
  permission layer. The exact paths are listed in BLOCKERS.md. gx10-02 is at
  **98 % disk (≈20 GB free)**.
* **Concurrent work:** a separate session on gx10-02 is building and testing a
  `gx-music` engine under `/srv/projects/gx-music-staging` (container
  `gx-music`). It is not part of this repo yet. It uses about 28 GB of models
  plus about 23 GB of images on the node-2 disk.
* **B-023** (thin node-1 memory margin during gx-max load) remains. The
  CRACK run measured 8.6 GiB minimum MemAvailable and 64/64 GiB swap on
  node 1.

---

## PREVIOUS UPDATE — 2026-09-16 ~18:30-20:40 SAST

**The cluster passed a full live acceptance, and it now has a management web
UI.** All seven aliases served real output. gx-max completed a full
load → inference → release cycle **through the new UI**. Evidence is in
`TEST_RESULTS.md` §17.

### Control UI — `http://100.105.214.61:8088/` (Tailscale) or `http://127.0.0.1:8088/`

| | |
|---|---|
| Service | `gx-control-ui.service`: user unit on gx10-01, enabled at boot, `MemoryMax=512M` |
| Code | `legenex/control-ui/` (D-028): stdlib Python backend, ES-module frontend, 7 docs pages |
| Auth | user `admin`; scrypt hash in `/srv/projects/gx-cluster/secrets/control-ui/auth.json` (0600). The initial random password is in `…/control-ui/initial-admin-password` (0600). Set your own with `legenex/control-ui/scripts/gx-ui-passwd`; that deletes the file. |
| Logs | `/srv/logs/gx-control-ui/control-ui.log` (JSON), `audit.log` |
| Health | `curl -sS http://127.0.0.1:8088/api/ready` |
| Restart | `systemctl --user restart gx-control-ui` |
| QA | `cd legenex/control-ui && npm run qa` (hermetic); `npm run test:live` (real cluster) |

Pages: Dashboard, Models (sanctioned LOAD / UNLOAD / RESTART), Runtime,
Cluster, Jobs / Queue, Logs, API Playground, Docs, Settings / System.

### What changed in the running system

* **Orchestrator** (restarted 18:37, gx-max down): read-only
  `GET /lifecycle/gx-max/events`, and phase fields on `/status` (D-029). The
  launch vector and scripts are unchanged.
* **Autosync:** the conflict-marker gate was a no-op under `pipefail` and is
  fixed. The pre-commit hook had been covering it. There is a new
  `ops/git-sync/tests/sync-regression.sh` (19/19).
* **Integrity audit** also checks the control-UI unit.

### Verified this session

| | |
|---|---|
| gx-mini / gx-fast / gx-reason / gx-auto / gx-image / gx-video | real output through the UI (§17.3) |
| gx-max | UI LOAD → 537 s → TP=2 / nnodes=2 live → 8/8 direct + 8/8 gateway + UI checks, 42.9–45.1 tok/s → UI graceful release → 114 GiB free on both nodes |
| RDMA | 9.2–9.6 GiB per port on both rails of both nodes during the gx-max cycle |
| Kernel lock | 13/0/0 on both nodes |
| Git | three HEADs converge; gx10-02 push disabled; audits FAIL=0 |

### Open

* **B-024:** media router key is the placeholder `not-required`. Rotation
  steps are in BLOCKERS.md; they need a human.
* **B-023:** the thin node-1 swap margin during a gx-max load is unchanged.
  This run measured 2.5 GiB MemAvailable and 64/64 GiB swap. Whether the
  drain should also stop Open WebUI and AgentOS remains a human decision.

---

## LATEST UPDATE — 2026-09-16 ~12:50-14:30 CEST — previous update

**All seven public tiers serve real output, gx-max included. Source control
moved to GitHub `legenex/gx-server`, with automatic, gated sync across both
nodes.**

| Tier | State | Evidence (TEST_RESULTS.md §16) |
|---|---|---|
| `gx-mini` | **SERVING** | text answer; vision identified red circle, blue square and digit 7 |
| `gx-fast` | **SERVING** | correct arithmetic; tool call parsed |
| `gx-reason` | **SERVING** | hard reasoning correct (15:35) |
| `gx-max` | **SERVING on demand** | TP=2 on both nodes, loads in 508–539 s; 8/8 real checks directly and through the gateway; RDMA on both rails |
| `gx-auto` | **SERVING** | routes to mini and reason correctly; a gx-max-worthy prompt is downgraded, never acquires |
| `gx-image` | **SERVING** | real generation through the gateway |
| `gx-video` | **SERVING** | real generation, 120,975-byte MP4 |

### gx-max: the earlier "does not fit" conclusion was wrong (B-022 resolved, D-025)

The failing runs did not use the verified `4b96e49` launch:

* `--memory 106g --memory-swap 106g` gave the ranks zero swap;
* `--mem-fraction-static` was 0.70 or 0.50, below the ~0.731 a TP=2 shard
  needs.

Restored now:

* the exact verified vector (it also matches the current official SGLang
  DGX Spark NVFP4 cell);
* no cgroup cap on the ranks;
* a gx-max-specific **cluster-takeover admission** (clean start,
  `/swapfile-sglang` active, at least 40 GiB swap free, at least 100 GiB
  MemAvailable, no pressure, healthy management plane) instead of
  `peak + 30 GiB`. Single-node tiers keep the 30 GiB reserve.

**Load behaviour is a controlled transient:**

* node 1 goes to 2.6–3.3 GiB MemAvailable and fills 64 GiB of swap for a few
  seconds;
* node 2 peaks at 52–55 GiB of swap;
* both drain once the weights are loaded.

**Steady state:** about 15 GiB (node 1) and 17 GiB (node 2) MemAvailable,
swap flat. Node 1's load-time swap headroom is the thinnest margin in the
system; see **B-023**.

**Protection, node-local on both nodes:**

* `gx-max-safety.sh`, phase-aware:
  * aborts immediately on a kernel OOM kill or a hard NV OOM;
  * aborts on exhaustion, thrashing or starvation only when sustained;
  * counts soft `NoLog` driver messages without aborting.
* `rank1-deadman.sh` on node 2. It now probes rank0 health over the fabric.
* New `rank0-watch.sh` on node 1.

Proven live:

| Failure | Result |
|---|---|
| Failure during load | verified clean |
| rank1 killed | clean at +69 s |
| rank0 killed (deadman only) | rank1 removed at +95 s |

### Kernel lock verifier fixed (D-027)

* A held, installed kernel reports dpkg status `hi`, which the verifier
  treated as MISSING.
* Unrelated older-ABI 6.8 kernel proposals from `dist-upgrade` are now
  INFO, not FAIL.

Both nodes: 13 passed, 0 failed. Nothing was installed or removed, and GRUB
was not touched.

### Source control (D-026) — see `ops/git-sync/README.md`

* **gx10-01** is the only writer: autosync after 45 quiet seconds, behind
  path and gitleaks gates, pushing `origin/main`.
* **gx10-02** is a pull-only clone at
  `/home/legenex-02/Documents/Projects/Server/gx-cluster`. It reconciles
  immediately after each push and every minute, and saves drift evidence
  before each reset.
* **Daily integrity audit** on both nodes.
* **Old remote** renamed `community-upstream`, with push disabled.
* **Rollback:** the pre-migration bundle is at
  `/srv/projects/gx-cluster/backups/gx-server/`; the tag is
  `pre-github-migration-20260916`.
* **Runtime state** (guard locks and ledgers) moved to
  `/srv/projects/gx-cluster/state/guard`.

**Operator gotcha:** after any Git operation that *replaces* files on
gx10-01 (branch switch, reset), run
`docker restart gx-llama-swap-node01 gx-litellm`. Their bind mounts pin the
old inodes. This bit gx-mini once during the migration.

---

## PREVIOUS UPDATE — 2026-09-16 ~09:00-11:00 CEST (gx-max "does not fit" — SUPERSEDED above)

**Six of the seven public tiers serve real output. The seventh, `gx-max`, does
not fit on this hardware, and that is now measured rather than suspected.**

| Tier | State | Evidence |
|---|---|---|
| `gx-mini` | **SERVING** | real answer through the gateway |
| `gx-fast` | **SERVING** | real answer through the gateway |
| `gx-reason` | **SERVING** | bat-and-ball answered correctly, `reasoning_content` separated |
| `gx-max` | **REFUSED, not serving** | admission guard refuses on measured numbers; see below |
| `gx-auto` | **SERVING** | routes correctly and never escalates to gx-max |
| `gx-image` | **SERVING** | real 1024x1024 PNG, 26.5 s, visually correct |
| `gx-video` | **SERVING** | real 33-frame h264 MP4, Wan 2.2, 48.1 s, visually correct |

### gx-max: the 30 GiB reserve is not achievable, and neither is gx-max

Eight instrumented two-node runs today. Loading one TP=2 rank takes a
121.63 GiB node from ~110 GiB MemAvailable down to **between 437 MiB and 0
MiB — on both nodes** — and ends in a kernel global OOM kill of the SGLang
scheduler. Every tuning lever was tried and **none of them moves that peak**:
`--mem-fraction-static` (measured identical troughs at 0.50 and 0.70),
`--context-length`, `--chunked-prefill-size`, `--cuda-graph-max-bs-decode`,
`--max-running-requests`, the container `--memory` cap (106g down to 28g), and
`--load-format` (`layered` is unsupported for this NVFP4 path;
`runai_streamer` loads but changes nothing).

The reason is arithmetic, not configuration: the checkpoint is 163.48 GiB, so
at the locked `--tp 2` each rank holds ~82 GiB of weights — two thirds of a
node — before any KV cache, and the loader adds ~26 GiB of host-side pinned
memory on top. `--mem-fraction-static` only ever moves the *steady state*.

**What this means in practice:** `gx-max` is exposed by the gateway but the
admission guard refuses every acquisition, before launching anything, because
117 GiB (the measured peak) plus any reserve exceeds a 121.63 GiB node. That
refusal is deliberate and correct. Making gx-max runnable needs a human
decision about a LOCKED constraint — the model, the quantisation, or the node
count. **The numbers and the three options are in `coordination/BLOCKERS.md`
B-022; what was changed and why is in `coordination/DECISIONS.md` D-022.**

Do not "fix" this by lowering `GXMAX_GUARD_RESERVE_GIB`. It was tested: even a
5 GiB reserve does not make gx-max fit, because the peak is the whole node.

### The orphan-rank failure from B-020 is fixed and proven on the real workload

This was the other half of the task and it is done. Three layers now:
`rank1-deadman.sh` (a watchdog resident **on node 2**, armed before rank0
starts, that force-removes rank1 when rank0 disappears — no ssh required),
`gx-max-unwind.sh` (a dedicated failure path with bounded node-2 retries that
*confirms* both ranks are gone and verifies memory, swap, locks, ledgers and
SSH/Tailscale/fabric health), and an EXIT trap in `gx-max-start.sh` that no
failure path can miss.

Proven, not asserted: five synthetic tests pass, and on the real DeepSeek
workload the deadman fired on node 2 at 1 GiB MemAvailable and the node came
straight back to 117 GiB — the exact condition that cost 80 minutes in B-020.
Three unwind runs reported `UNWIND COMPLETE — cluster verified clean`.

One node-2 wedge did still occur (run 5) and took **17 minutes** to clear
itself, against 80 minutes for B-020. During it, node 2 answered ICMP **and
TCP:22 on the ConnectX fabric** while Tailscale was completely dark — so the
fabric is the better liveness probe during a wedge. See B-020 for the
corrected, no-physical-intervention-first recovery procedure.

### Measured today

| Item | Value |
|---|---|
| gx-image | 1,274,968 B PNG, 1024x1024, 26.5 s, 1043 distinct colours sampled |
| gx-video | 100,023 B h264 MP4, 640x640, 33 frames @16 fps, all 33 frames distinct, 48.1 s |
| Media unload | node2 47 → 114 → 117 GiB, swap unchanged |
| gx-image / gx-video min MemAvailable | 59.7 GiB / 42.8 GiB — both well above the 30 GiB floor |
| gx-max per-rank load peak | ~117 GiB of a 121.63 GiB node |
| gx-max steady state (if it could load) | ~26 GiB free at `--mem-fraction-static 0.70` |
| Checkpoint integrity | node1 and node2 shard manifests byte-identical |

Full numbers: `TEST_RESULTS.md` §15.

---

## EARLIER UPDATE — 2026-09-16 ~07:30-08:15 CEST

**Both nodes are healthy and gx-reason works for the first time.** The two
things that were blocking this project are both closed:

**1. Node 2 is back — and it recovered ITSELF. No power cycle happened.**
The previous update said node 2 was physically wedged and needed a human at
the machine (B-020, B-016). That turned out to be wrong, and the evidence is
unambiguous: node 2's `uptime` shows a continuous 22 h since a boot at
2026-09-15 09:33 — hours *before* the incident. What actually ended it is
visible in `docker inspect gx-max-rank1`: `OOMKilled=true`,
`FinishedAt=2026-09-15T22:44:48Z`. The orphaned rank1 held the node for 80
minutes, the kernel's OOM killer eventually reclaimed it against its own
`--memory 106g` cap, and userspace un-starved on its own. The host-resilience
design from B-012 worked; it was just slow. `recover-node2.sh` now passes
every substantive check. **Operational consequence: for this failure shape,
wait ~80 minutes and re-probe before dispatching a human.** See B-020 for the
corrected record.

**2. B-011 is RESOLVED — gx-reason serves correct output through the
gateway.** The Qwen3.5-122B-A10B GGUF/llama.cpp tier that produced
`////////////////////` for every prompt has been replaced, per D-021, with
`nvidia/Qwen3.6-27B-NVFP4` on vLLM (the same image already proven for
gx-fast). Downloaded, deployed and live-tested end to end today. The clinching
result is the original repro prompt, run identically against the new engine:

```
"The capital of France is"  ->  " Paris."      (was "////////////////////")
```

and through the real LiteLLM gateway, a multi-step reasoning question
answered correctly (bat-and-ball: `$0.05`, not the `$0.10` trap) with
`reasoning_content` properly separated. vLLM drives the *same* GDN
linear-attention kernels llama.cpp got wrong — confirming D-021's diagnosis
that the architecture was never the problem, that llama.cpp build's CUDA
implementation of it was.

**All four text tiers are now working: gx-mini, gx-fast, gx-reason, and
gx-max remains the one unproven tier** (its last acquisition OOM-killed
rank0 mid-load, which is what caused incident 2 above; the 90->95 GiB
estimate change from D-020 has not been re-tested yet).

**One new finding, recorded as B-021:** `--memory` cgroup caps do NOT bound a
model's real footprint on this hardware. Measured with gx-reason loaded: the
node lost 44 GiB of MemAvailable while the container's own `memory.current`
read 10.92 GiB — the CUDA pool is not charged to the container cgroup on
DGX Spark unified memory. The admission guard is unaffected (it reads the
node's real `/proc/meminfo`), and `--gpu-memory-utilization` is what actually
bounds the pool, but several memory-budget comments in this repo overstated
what the cgroup cap enforces.

**Measured, live, today (full numbers in `TEST_RESULTS.md`):**

| Item | Value |
|---|---|
| gx-reason checkpoint on disk (node 2) | 21,921,697,184 B = 20.42 GiB, 3 shards, matches HF exactly |
| gx-reason cold start | 401 s to first token (~225 s weights, 177 s engine init) |
| gx-reason generation | 12.4 tok/s (dense 27B, unified memory) |
| gx-reason real node footprint | ~44 GiB (MemAvailable 114 -> 70 GiB) |
| gx-reason unload | 70 -> 116 GiB MemAvailable in ~5 s |
| Reasoning-token cost of one simple question | 1468 of 1690 completion tokens |

The last figure is why the gateway's `gx-reason` output budget was raised
from 8192 to 16384 tokens (and input lowered to 49152 to match the 65536
context): on a reasoning tier, `<think>` content spends the output budget.

**What remains, in priority order:** re-run `gx-max-validate.sh` to test the
D-020 fix; real end-to-end `gx-image`/`gx-video` generations; then the
root-requiring items (kernel `apt-mark hold`, watchdog) that still need a
human with sudo. See `TASKS.md`.

---

Last updated: 2026-09-15 12:35 CEST, by the lead agent on gx10-01, after a
full autonomous two-node completion pass (Phases 1-6 of the recovery/
validation task: node 2 recovery, resource-ownership deployment to node 2,
gx-reason diagnosis, real gx-image/gx-video E2E validation, a first-ever
gx-max acquisition attempt through the real orchestrator, and a full
acceptance-suite run). This update is based on **live checks and real
inference/generation against both nodes**, not a re-read of prior notes.
See `CHANGELOG.md`'s `[Unreleased]` section for the full list of fixes and
findings; the highlights: two real production bugs found and fixed
(gx-orchestrator boot-race, D-019; gx-max-start.sh's dead conflict-drain
list), one real, unresolved architecture decision surfaced and documented
rather than worked around (gx-max vs the 30 GiB reserve floor, B-017), and
gx-image/gx-video validated end-to-end for the first time (real generations,
visually inspected).

---

## One-paragraph summary — HISTORICAL, as of 2026-09-15 12:35

> Everything from here down is the 2026-09-15 snapshot, kept for the record.
> The power cycle it mentions is the *earlier* B-012 recovery, not the B-020
> incident — B-020 needed no power cycle at all (see the top of this file).
> For current state, read the LATEST UPDATE section above.

**Both nodes were healthy at the time of this 2026-09-15 update.** Node 2 — reported in the previous update
as physically wedged, needing a power cycle — has since been power-cycled by
the human operator and is **confirmed clean**: `recover-node2.sh` passed all
16 checks (kernel, driver, Docker, both ConnectX rails, `/swapfile-sglang`,
disk, no stale containers, no accidental large workload, llama-swap reachable
both locally and over the fabric). On node 1, the gateway container
(`gx-litellm`) had exited cleanly about an hour before this session started
(its Postgres connection was administratively terminated — benign, not a
crash loop) and has been restarted and re-verified healthy. The orchestrator
correctly reports all four tiers as `stopped`/`unloaded` (normal, on-demand)
and `gx-max` as `down`. Separately, in the session immediately before this
one, `BLOCKERS.md` B-011 (gx-reason produces garbage output) was narrowed
from "unknown cause" to "isolated to the CUDA/GDN kernel execution path of
this llama.cpp build for the `qwen3_5_moe` hybrid architecture" — **still
OPEN**, not fixed, ruled out as a checkpoint/quant or stale-build problem.

## Verified live this session (2026-09-15, ~10:30–10:40 CEST)

| Check | Result |
|---|---|
| Node 1 kernel | `6.17.0-1032-nvidia` (confirmed via `uname -r`) |
| Node 1 memory | 112 GiB free / 114 GiB available of 121 GiB, swap 0/63 GiB used |
| Node 2 SSH (`ssh legenex-02@gx10-02`) | **reachable**, banner completes normally |
| Node 2 kernel | `6.17.0-1032-nvidia` (matches node 1) |
| Node 2 memory | 116 GiB available of 121 GiB, swap 2.8/63 GiB used |
| Node 2 `nvidia-smi` | OK — GB10, driver `580.173.02`, 47°C |
| Both ConnectX/RoCE rails (`192.168.100.x`, `192.168.101.x`) | ACTIVE / LINK_UP, 0% loss, sub-ms RTT, both directions |
| `/swapfile-sglang` (48 G) | present on **both** nodes |
| `recover-node2.sh` (report-only) | **16 PASS / 0 FAIL / 0 WARN / 1 SKIP** (nothing to clear) |
| Node 1 `gx-litellm` (gateway) | was `Exited (128)`; restarted via documented `docker compose up -d`; now `Up`, `health: healthy`, `HTTP 200` on `/health/liveliness` |
| Node 1 `gx-llama-swap-node01`, `gx-litellm-db` | healthy throughout, unaffected |
| Node 1 `gx-orchestrator.service` (systemd --user) | `active running`, `/health/detailed` returns `status: ok`, all four tiers `stopped`/`usable: true`, `gx-max: down` |
| Node 2 `gx-llama-swap-node02` | healthy, reachable on loopback **and** from node 1 over the fabric (`192.168.100.11:28080`) |
| Node 2 ComfyUI / gx-reason / gx-max | all correctly report `STOPPED` / `NOT_PROVISIONED` (on-demand; nothing auto-started) |

**Why the gateway had exited:** `docker logs gx-litellm` showed a clean
graceful shutdown triggered by `"terminating connection due to administrator
command"` from Postgres — i.e. something explicitly closed the DB connection
(most likely a `docker restart`/compose action during the prior session,
consistent with all node-1 containers showing "Up about an hour" at session
start). Not a crash, not a bug. Restarted per `RECOVERY.md` §1 and confirmed
healthy.

## Node 2: RESOLVED — was physically wedged, now recovered

Previously (2026-09-14 session): node 2's kernel stayed alive (ICMP replied
0% loss) but userspace was starved — SSH could not complete a banner
exchange, llama-swap did not answer. Root cause: two ~77 GB mmap'd models
resident on a 121 GiB node at once (see `coordination/BLOCKERS.md` B-012 for
the full incident and the admission-control layer built in response).

**Since then:** the human operator physically power-cycled node 2. This
session ran the documented, report-only `legenex/scripts/recover-node2.sh`
and it passed every check (see table above) — node 2 is not just "pinging
again", it is verified clean: correct kernel, both fabric rails up, the
required swapfile present, no stale containers, no accidental resident
workload, and its own llama-swap answering both locally and across the
fabric.

**Update, later the same session:** the resource-guard ledger/lock module
(`legenex/lifecycle/` + `legenex/orchestrator/`) has since been deployed to
node 2 and independently verified there too — a normal launch is admitted
correctly against node 2's own real `/proc/meminfo` and lock file, and a
deliberately oversized synthetic launch is correctly refused. `gx-hostwatch.sh`
is also now running as a `systemd --user` timer on node 2. See
`coordination/BLOCKERS.md` B-012 for the exact evidence. **Node 2 now has the
same structural protection node 1 has**, with one piece still separate:
`gx-max-start.sh`'s rank1 launch still uses its original real remote `flock`
convention rather than having been rewritten to call through the newly
deployed module — real, not-yet-done work, distinct from "the module isn't
there."

**Still true / not yet done:**
- No BMC/IPMI/Redfish/MCTP remote power path exists on either node — a
  future B-012-style wedge still requires a human physically present (see
  `coordination/BLOCKERS.md` B-016).
- `legenex/scripts/gx-reason-diagnose.sh` and `legenex/tests/gx-max-validate.sh`
  have **not** been re-run against the freshly-recovered node 2 by this
  session (this integration deliberately did not start any large model — see
  below); the gx-reason diagnostic *was* already re-run in the session
  immediately prior (see next section).

**Note on process:** this file, and specifically the "no ledger deployed"
claim above, was edited concurrently by a second agent session working the
same repo at the same time, without any coordination protocol between the
two. Both sessions' edits landed in git history (see `coordination/
DECISIONS.md` D-018 and `coordination/BLOCKERS.md`'s own note on this). Not
adversarial — the concurrent session's claims were independently verifiable
and checked out — but a real operational gap worth a human's attention if
multiple agents are going to work this repo at once going forward.

## B-011 — gx-reason garbage output: narrowed, still OPEN

Carried over from the prior session, immediately before this one (see
`CHANGELOG.md` "Unreleased" and `coordination/BLOCKERS.md` B-011 for full
detail — summarized here because it materially changes what "gx-reason
status" means):

- Fixed two script bugs that were silently invalidating diagnostics:
  `recover-node2.sh`'s memory check (an SSH quoting bug made it WARN instead
  of actually checking), and `gx-reason-diagnose.sh`'s CPU-only comparison
  (it omitted the CDI GPU device entirely, so the "CPU-only" container failed
  to start at all — `llama-server` is dynamically linked against
  `libcuda.so.1`, which requires the device to be mounted even to run
  `--n-gpu-layers 0`).
- With both fixed, re-ran the real GPU-vs-CPU comparison against the
  recovered node 2: **GPU path reproduces the exact original garbage
  (`////////////////////`); CPU-only path with identical weights and
  sampling is coherent** (`"The capital of France is Paris."`). This rules
  out the checkpoint/quant.
- Rebuilt `legenex/llama-cpp-spark` from current upstream `llama.cpp` master
  to rule out a stale build — build succeeded, **identical GARBAGE/SANE
  split reproduced byte-for-byte** on the new binary. Rules out a stale
  build too.

**Conclusion: the bug is real and isolated to the CUDA/GDN kernel execution
path for the `qwen3_5_moe` hybrid architecture on this hardware** — either an
upstream `llama.cpp` correctness bug for `sm_121`/GB10, or something specific
to this driver/CUDA combination. **gx-reason cannot be served correctly on
GPU today.** Running it CPU-only is not a real fix (a 122B-class MoE model on
CPU is far too slow to be a usable tier) and has not been deployed. See
`coordination/BLOCKERS.md` B-011 for the three concrete next steps (upstream
issue research, an alternate quant, or a bisect) — none attempted yet, each
is real, separate work requiring a human decision before starting (a new
quant means a new multi-GB download).

## Second, unrelated node-1 incident from the prior session (found and fixed then)

1. **`gx-litellm` had lost its Docker network attachment entirely** and was
   crash-looping against an unreachable `litellm-db:5432`. Fixed by
   recreating it via `docker compose up -d litellm`.
2. **The orchestrator was not running at all** — no process, no systemd unit
   had ever existed for it. Fixed: started it and added
   `~/.config/systemd/user/gx-orchestrator.service` (enabled, hardened,
   binds only `127.0.0.1,172.17.0.1:18900`). Confirmed still `active running`
   this session.

Separately, **`vllm-qwen38-uncensored`** (~80 GiB resident, unrelated to the
seven-alias tier set) was identified as a major memory-safety risk and has
been **permanently retired** — container and checkpoint deleted, all active
runtime/download/routing/lifecycle references removed from the repo
(`c0076f8`, see `coordination/DECISIONS.md` D-015). Confirmed this session:
`/opt/models/` on node 1 no longer contains a Qwen3.8 directory.

## Resource ownership (built in response to B-012, unchanged this session)

Direct response to the B-012 root cause. Full detail in `ARCHITECTURE.md`
§9 and `coordination/DECISIONS.md`; summary here:

- `legenex/orchestrator/gx_orchestrator/resource_guard.py` +
  `legenex/lifecycle/resource-guard.sh` — one shared arithmetic module
  (bash shells out to the same Python module): workload classes (small/
  medium/large/exclusive), a 30 GiB minimum-reserve floor checked against
  both a residency ledger AND live `/proc/meminfo`, a flock-backed
  cross-process `NodeLock`.
- `legenex/lifecycle/gx-safe-run.sh` — the sanctioned replacement for a bare
  `docker run` on any medium/large/exclusive container.
- `gx-max-start.sh`/`gx-max-stop.sh` route both rank launches through the
  hard, non-bypassable admission guard; `_do_acquire()` unwinds any
  partially-started rank on a failed acquire instead of leaking it.
- Docker `--memory`/`--memory-swap` caps and `--oom-score-adj` biasing on
  every model container.
- `legenex/host/gx-hostwatch.sh` — dependency-free watchdog (systemd
  `--user` timer), logs and alerts only.

**Now deployed and verified on both nodes** — see "Node 2" above for the
2026-09-15 deployment evidence. `gx-max-start.sh`'s rank1 launch itself still
uses its original real remote `flock` convention rather than calling through
the module directly; that rewrite is separate, not-yet-done work.

## Hardware

| | gx10-01 (node 1, control) | gx10-02 (node 2, compute) |
|---|---|---|
| DGX Spark version | 7.5.0 | 7.5.0 |
| Kernel | `6.17.0-1032-nvidia` — **confirmed live 2026-09-15** | `6.17.0-1032-nvidia` — **confirmed live 2026-09-15** |
| Arch / Python / Docker | aarch64 / 3.12.3 / 29.2.1 | aarch64 / 3.12.3 / 29.2.1 (confirmed) |
| GPU / driver / CUDA | GB10, 580.173.02, CUDA 13.0 | GB10, 580.173.02 — confirmed live |
| RAM | 121 GiB | 121 GiB |
| Swap | 63 GiB (`/swap.img` + `/swapfile-sglang` 48 G) | 63 GiB (same two files) — confirmed live |
| sudo | **password required** | **password required** |
| User lingering | enabled | disabled (T-1 in `coordination/WORKER_TASKS.md`, blocked on the human) |
| Linux user | `legenex` | `legenex-02` |
| LAN | `10.60.21.37` | `10.60.21.41` |
| Tailscale (management only) | `100.105.214.61` | `100.73.238.4` |
| ConnectX rail A | `192.168.100.10` | `192.168.100.11` |
| ConnectX rail B | `192.168.101.10` | `192.168.101.11` |

Mac management device Tailscale IP: `100.104.35.71`.

GPU passthrough is **CDI** (`--device nvidia.com/gpu=all`) on both nodes. There
is no `nvidia` docker runtime and no `/etc/docker/daemon.json`.

## Fabric

| Rail | node 1 | node 2 | state |
|---|---|---|---|
| A `enp1s0f0np0` / `rocep1s0f0` | 192.168.100.10 | 192.168.100.11 | **ACTIVE, confirmed live 2026-09-15** (both directions) |
| B `enP2p1s0f0np0` / `roceP2p1s0f0` | 192.168.101.10 | 192.168.101.11 | **ACTIVE, confirmed live 2026-09-15** (both directions) |

Custom NCCL 2.30.7 built for SM121; a two-node `all_gather_perf` (16 GiB)
passed with zero errors, ~21.3 GB/s average bus bandwidth — see
`TEST_RESULTS.md`. A single 400-token gx-max generation independently moved
772 MB of RDMA traffic split near-evenly across both rails.

Tailscale is management/SSH only — confirmed by measurement, not assumption.
`ssh legenex-02@gx10-02` (Tailscale) is the correct SSH endpoint; SSH directly
to `192.168.100.11` is refused — the fabric addresses are not SSH endpoints.

## What is running right now

**Node 1** (verified via `docker ps`, `systemctl --user`, direct `curl`):

| Service | Port | State |
|---|---|---|
| LiteLLM gateway (`gx-litellm`) | 4000 (loopback) | **healthy** (restarted this session, see above) |
| Postgres (`gx-litellm-db`) | 15432 (loopback) | **healthy** |
| llama-swap node 1 (`gx-llama-swap-node01`) | 28080 / 19001 (loopback) | **healthy** |
| gx-orchestrator (systemd `--user`) | 18900 (loopback + docker bridge) | **active running**, `/health/detailed` → `ok` |
| open-webui | — | **healthy** (not part of the gx tier set) |
| gx-mini / gx-fast / gx-reason (llama-swap-managed) | via llama-swap | **stopped** — on-demand, correct |
| gx-max rank 0/1 (SGLang) | 30000 | **stopped** — `down`, correct |

**Node 2** (verified live at end of this session — `free -h`, `docker ps`):

| Service | State |
|---|---|
| llama-swap node 2 (`gx-llama-swap-node02`) | **healthy**, both loopback and fabric-reachable |
| gx-comfyui + gx-media-router | **healthy, running idle** (built and started this session — first time ever). Low footprint at idle (~2-3 GiB); ComfyUI's per-generation model cache is explicitly freed after each test (see B-018 for the one gap this doesn't close: a generation run by hand, outside the test suite, still leaves ~70 GiB cached until `/free` is called or `docker compose down`) |
| gx-reason | `STOPPED` — unloaded after testing, correct |
| gx-max rank 1 | `STOPPED` — never successfully started this session, see B-017 |
| GPU owner | `free` |
| MemAvailable | **113 GiB** |

No large model is resident on either node right now. Real inference/generation
WAS run against gx-mini, gx-fast, gx-reason (confirmed broken), gx-image, and
gx-video this session — see the Tier status table below for results.

## Tier status

| Alias | Model | Engine | Node | State right now |
|---|---|---|---|---|
| gx-mini | Qwen3.5-4B Q4_K_M + BF16 mmproj | llama.cpp | 1 | **WORKING.** Real text inference verified live this session through the gateway; stopped/on-demand now |
| gx-fast | `nvidia/Qwen3.6-35B-A3B-NVFP4` | vLLM | 1 | **WORKING.** Real text inference + tool-calling (`get_weather`) verified live this session; stopped/on-demand now |
| gx-reason | ~~`unsloth/Qwen3.5-122B-A10B-GGUF`~~ **REPLACED 2026-09-15** → `nvidia/Qwen3.6-27B-NVFP4` | ~~llama.cpp~~ → vLLM | 2 | **Old combination confirmed broken and rejected (B-011). Replacement fully configured (D-021), NOT yet live-tested** — node2 went down (B-020) before the new checkpoint could be downloaded. Highest-priority next step once node2 is back |
| gx-max | `nvidia/DeepSeek-V4-Flash-0731-NVFP4` | SGLang TP=2 | 1+2 | **DOWN. B-017's admission refusal is RESOLVED (D-020) — both ranks passed admission and rank1/rank0 both started for the first time ever this session.** Then rank0 was OOM-killed during weight loading, and the orphaned rank1 left node2 wedged — **node2 is currently down, see B-020, needs a physical power cycle.** Not yet fully validated end-to-end |
| gx-auto | — | orchestrator | 1 | **WORKING — a real, live-production bug was found and fixed this session (D-019).** The orchestrator's `172.17.0.1` bind failed at this morning's boot (race with `docker0` getting its address) and nobody noticed for 2.5+ hours: every gx-auto request from the LiteLLM container was silently unable to reach the classifier. Fixed (ExecStartPre wait-for-docker0) and reverified: all 3 routing test cases pass, including correct escalation to gx-reason for a hard-reasoning prompt |
| gx-image | Qwen-Image 2512 (+Lightning LoRA) / HiDream I1 (not wired) | ComfyUI | 2 | **WORKING — real E2E generation verified for the first time this session.** Built+started the media stack (previously never deployed), real 1024x1024 image via the gateway in 28s, visually inspected (a genuine hummingbird/sailboat, not noise). Stopped again after testing (on-demand is the intent, though nothing currently auto-restarts it — see B-018) |
| gx-video | Wan 2.2 A14B (LTX 2.3 not used — licence) | ComfyUI | 2 | **WORKING — real E2E generation verified for the first time this session.** Real playable MP4 via the router's async contract in 58s. No "hd"/no-LoRA tier wired yet (unchanged gap) |

**Known media gap, unchanged:** HiDream-I1-Full and a no-LoRA "quality" Wan
variant are documented in `MODELS.md` as available checkpoints but have no
`_gx`-enabled template in `legenex/media/workflows/` yet.

## Automated test count (unchanged this session — no code touched)

168 tests passing across three independent suites, all runnable without a
live cluster:

```
legenex/orchestrator:        116 tests   (python3 -m unittest discover -s . -p 'test_*.py')
legenex/lifecycle/tests:       9 tests   (python3 -m unittest discover -s tests -p 'test_*.py')
legenex/media/router:         43 tests   (./qa.sh)
```

## Repository layout (unchanged this session)

```
legenex/orchestrator/gx_orchestrator/
  resource_guard.py      workload sizing, admission math, NodeLock, ResidencyLedger
  health.py               per-tier real-upstream health probing
  status_cli.py           `gx status` implementation
legenex/lifecycle/
  resource-guard.sh       bash-side admission-control library
  gx-safe-run.sh          sanctioned replacement for a bare `docker run`
  tests/                  bash-side resource-guard tests
legenex/host/
  gx-hostwatch.sh         dependency-free host resilience watchdog
  systemd/                its service+timer templates
legenex/scripts/
  gx-status.sh            `gx status` entry point
  recover-node2.sh        node-2 recovery checklist (report-only until --apply)
  gx-reason-diagnose.sh   B-011 GPU-vs-CPU diagnostic, unload-gated
legenex/tests/
  gx-max-validate.sh      full acquire->serve->release->restore validation
```

## Known gaps

See `coordination/BLOCKERS.md` for the full list with severities. The ones
that matter most right now:

* **B-017 (S1, new this session)** gx-max cannot acquire through the real
  orchestrator: its locked ~90 GiB/rank footprint doesn't leave the
  admission guard's 30 GiB reserve floor. Needs a human decision between
  three documented options — not a bug, a genuine unresolved design
  collision. See ARCHITECTURE.md §5 and BLOCKERS.md B-017.
* **B-011** gx-reason is functionally broken on GPU (garbage output),
  isolated to a CUDA/GDN kernel bug — confirmed this session it is NOT a
  stale-build problem (rebuilt from current llama.cpp master, identical
  result). Still OPEN.
* **B-018 (S2, new this session)** ComfyUI's `docker compose up` start does
  not go through the resource-ownership admission guard at all — a real
  near-miss (node 2 hit ~10 GiB available mid-testing) was caught live and
  the test suite hardened, but the underlying gap in the launch path itself
  is not fixed.
* **B-001** the kernel pin has no `apt-mark hold`; kernel 7.0 is still
  installed on both nodes. Needs root.
* **B-003** SGLang `:30000` is bound `0.0.0.0` with no auth.
* **B-013** no writable git remote is configured — commits on
  `legenex-dual-gx10` not yet pushed anywhere.
* **B-016** no BMC/IPMI/Redfish path on either node — a future wedge needs a
  human physically present.
* `gx-max-start.sh`'s rank1 launch still doesn't call through the (now
  node-2-deployed) resource-guard module directly — it uses its original
  real remote `flock` convention. Cosmetic/consistency gap, not a safety one.
* ~~gx-image/gx-video real end-to-end generation has never been run through
  the gateway~~ — **done this session**, both confirmed working with real,
  visually-inspected output.

## How to resume

```bash
cd /home/legenex/Documents/Projects/Server/gx-cluster
legenex/scripts/gx-status.sh                   # one-shot cluster status, human + --json
curl -s localhost:18900/health/detailed        # orchestrator + tier view
cd legenex/orchestrator && python3 -m unittest discover -s . -p 'test_*.py'
```

Node 2 is already recovered — do not re-run `recover-node2.sh --apply`
speculatively; the report-only form is safe to re-run any time to re-confirm.

Suggested next real work (see `TASKS.md` for the full prioritized list):

```bash
legenex/tests/gx-max-validate.sh               # full two-node lifecycle, unverified since node2's recovery
# gx-reason: do NOT re-attempt the same diagnostic — B-011 is already isolated.
# Next step there is a human decision (upstream issue, new quant, or a bisect).
```
