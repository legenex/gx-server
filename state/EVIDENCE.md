# EVIDENCE

## Post-reboot acceptance (2026-09-24 22:31/22:36 SAST boots)

Genuine new boots (`who -b` / `last reboot`):

- GX10-01: **2026-09-24 22:36 SAST** (previous 2026-09-23 23:57)
- GX10-02: **2026-09-24 22:31 SAST** (previous 2026-09-23 23:56)
- Kernel both nodes: `6.17.0-1032-nvidia`

| Check | Result |
|---|---|
| Public `GET /v1/models` (Tailscale, authenticated) | gx-mini, gx-code, gx-auto, gx-max |
| Unauthenticated `/v1/models` | HTTP 401 (auth left enabled) |
| gx-code-01 / gx-code-02 | `CODE01_REBOOT_OK` / `CODE02_REBOOT_OK`; llama-swap direct `SWAP01/02_REBOOT_OK` |
| gx-mini / gx-code / gx-auto / gx-max | `MINI_REBOOT_OK`; `reboot_pool_marker`→`CODE_REBOOT_OK`; `AUTO_REBOOT_OK`; `MAX_REBOOT_OK` + `solver=gx-code-01 reviewer=gx-code-02` |
| gx-code worker pool | six `gx-code` calls hit both `gx-llama-swap-node01:8080` and `192.168.100.11:28080` |
| OpenWebUI browser | new chats: assistant `OWUI_MINI_REBOOT_OK`, `OWUI_CODE_REBOOT_OK`, `OWUI_AUTO_REBOOT_OK`, gx-max `a + b` + dual-worker banner. No Server Connection Error. Picker/API ids: gx-auto, gx-mini, gx-code, gx-max. Shots `state/post-reboot-20260924/` |
| Connections | public `http://100.105.214.61:4000/v1`, OWUI `http://127.0.0.1:4000/v1`, key masked, Reveal/Copy, Kilo 7.7.9 gx-code, Test Gateway PASS |
| AgentOS | running since 22:36, NRestarts=0, `/api/ask-live` `AGENTOS_REBOOT_OK` via gx-code |
| Playground / media | masked; :8090/:8443 closed both nodes; no media containers |
| dump-databases WARN | stale `find` root `/home/legenex/agentos` (missing). Required dumps (LiteLLM, PageFlo, FinancialOS, OpenWebUI) already succeeded. Path removed; dump replay exit 0 with `DUMP-PATH.txt` |
| Restic | latest `7101853e` / `602f2bfa` stamp `20260924T203135Z`; `restic check --read-data-subset=2%` no errors |
| gx status / gx doctor | PASS / 13/13 |
| Git gx-cluster | `275b12a` == origin/main == gx10-02; working trees clean |

## Post-closeout live check (2026-09-24 21:55 SAST)

`who -b` / `last reboot` / `journalctl --list-boots`:

- GX10-01 last boot: **2026-09-23 23:57:50 SAST** (still running)
- GX10-02 last boot: **2026-09-23 23:56 SAST** (still running)

These boots predate the closeout changes. A reboot after the closeout was not observed on either node.

Live acceptance on this boot (kernel `6.17.0-1032-nvidia` both nodes):

| Check | Result |
|---|---|
| Tailscale | gx10-01 100.105.214.61, gx10-02 100.73.238.4 active |
| RoCE | both 200 Gb/s ACTIVE; ping 192.168.100.11 and 192.168.101.11 ok |
| MemAvailable | gx10-01 61.7 GiB; gx10-02 87.5 GiB |
| Gateway / Postgres / OpenWebUI / GX-Cluster / AgentOS | healthy |
| AgentOS | running since 2026-09-24 11:41 (fix); ask-live AGENTOS_OK via gx-code |
| gx-backup.timer | active; next Fri 03:32 |
| Playground | masked; :8090/:8443 closed both nodes |
| Media units gx10-02 | call/live/music/voice masked; no media containers |
| Public models | gx-mini gx-code gx-auto gx-max |
| Inference | mini MINI_OK; code CODE_OK; code-01/02 OK; auto routed coding; max dual-worker |
| OpenWebUI browser | MINI_OK, CODE_OK, Hello, 2+2=4 solver=gx-code-01 reviewer=gx-code-02; no Server Connection Error |
| Connections | public Tailscale :4000/v1, OWUI 127.0.0.1:4000/v1, Kilo gx-code, key masked |
| Backup page | Recovery tested; latest 20260924T102220Z; offsite Not configured |
| gx status / gx doctor | PASS / 13/13 |
| Git | 275b12a == origin/main == gx10-02; working trees clean |

## Daily-use closeout (2026-09-24)

### OpenWebUI

- Failure: Settings connection `http://100.105.214.61:4000/v1` connection-refused. LiteLLM published only `127.0.0.1:4000` and dummy `127.0.0.2:4000` (`GX_LITELLM_TS_BIND`). OpenWebUI is host-network; loopback TCP succeeded, Tailscale TCP failed.
- Fix: persisted `openai.api_base_urls[1]=http://127.0.0.1:4000/v1` with live master key. Users/chats preserved.
- Browser (admin session): chats stored assistant `MINI_OK`, `CODE_OK`, `Hello`, `MAX_OK` + `solver=gx-code-01 reviewer=gx-code-02`. Screenshot `owui-browser/gx-max.png`. No Server Connection Error. Picker ids: gx-auto, gx-mini, gx-code, gx-max.

### Public models

- Tailscale `GET /v1/models` (via `gx-gateway-ts-proxy`): gx-mini, gx-code, gx-auto, gx-max.
- Loopback still lists gx-code-01/02. Direct calls `CODE01_OK` / `CODE02_OK`.

### Connections

- Primary nav `#/connections`. Playwright: Healthy gateway, Reveal present, key masked (`sk-••••52cd` suffix only), no full secret in snapshot. Kilo 7.7.9, recommended gx-code.
- Kilo-shaped request to Tailscale `gx-code`: `KILO_OK`.

### AgentOS

- Units now `ExecStart` `Documents/Projects/AgentOS/...`. Health `llm.available true model gx-code`. `POST /api/ask-live {"question":"Reply exactly AGENTOS_OK"}` → `ok true llm_used true`.

### Backup drill

- `restic check --read-data-subset=2%` no errors.
- Restore `eaf40302` 21.612 GiB / 155785 files. OpenWebUI db integrity ok (4 users, 24+ chats). LiteLLM dump → throwaway Postgres 16, 86 tables.
- Temporary restore tree deleted.

### gx-max streaming

- Dual-worker is non-streaming internally. Orchestrator now emits SSE of the finished answer when `stream=true` so OpenWebUI shows the reply.

## Media / Playground retirement (2026-09-24)

### Backup before removal

- Stamp `20260923T091634Z` local snapshot `24ba5393` listed playground source, media/voice/music/call/live trees, compose files, `gx-playground.service`, `/srv/projects/gx-cluster/media` library (41 audio / 20 images / 29 videos / 52 voice / 11 metadata), secrets and state.
- Git `legenex/gx-server` holds the same source.

### Runtime stopped

| Component | Action |
|---|---|
| gx-playground.service :8090/:8443 | stop, disable, mask; unit archived `Documents/Archive/disabled-units-20260924T0015Z/` |
| gx-call, gx-live, gx-music, gx-voice (gx10-02) | disable --now, mask |
| docker compose `gx-media` on gx10-02 | `down`; container `gx-media-router` removed |
| LiteLLM aliases gx-fast, gx-reason, gx-image, gx-video, gx-voice | removed from live `legenex/gateway/litellm/config.yaml` |
| llama-swap gx-code aliases gx-fast (node1) and gx-reason (node2) | **kept** (internal; gx-code workers) |

### Gateway after cleanup

`GET /v1/models`: gx-auto, gx-code, gx-code-01, gx-code-02, gx-max, gx-mini (COUNT 6).

### Mode pings (2026-09-24)

| Mode | Result |
|---|---|
| gx-mini | 200, `MINI-OK`, 0.50 s |
| gx-code-01 | 200, `NODEONE`, 25.72 s (cold start) |
| gx-code-02 | 200, `NODETWO`, 23.73 s (cold start) |
| gx-auto | 200, greeting on mini, 0.55 s |
| gx-max | 200, dual-worker banner solver=gx-code-01 reviewer=gx-code-02, 5.75 s |

### Disk

| Node | Before | After | Reclaimed |
|---|---|---|---|
| gx10-01 | 675 G used / 195 G free (78%) | 658 G / 212 G (76%) | ~17 G (`gx-call-engine` image) |
| gx10-02 | 769 G used / 101 G free (89%) | 491 G / 379 G (57%) | ~278 G (image/video/music/live/voice/voicechat/shared/llm/comfy caches + media-router image) |

Kept: Ornith 9B + 35B GGUF, DeepSeek optional gx-max weights, llama.cpp runtime, gateway, Postgres, OpenWebUI, GX-Cluster, backup system.

### UI

Playwright snaps `state/ui-snaps-20260924/`. Dashboard Cluster **Healthy**. Models cards: gx-mini, gx-code, gx-auto, gx-max only.

### OpenWebUI

Picker already filtered: `openai.api_configs` model_ids `gx-auto, gx-mini, gx-code, gx-max`. Default `gx-auto`. Chats/users untouched.

### AgentOS

`agentos-control-center.service` ExecStart path `/home/legenex/agentos/.../server.py` is missing (live tree absent). Service was restart-looping (status 2); stopped to end the storm. Git checkout still at `Documents/Projects/AgentOS`. Not caused by this media cleanup.

### gx doctor / status

13/13 PASS. `gx status` PASS. Port 8090 not listening.

## Connectivity (2026-09-23)

- gx10-01 hostname `gx10-01`, Tailscale `100.105.214.61`
- gx10-02 Tailscale `100.73.238.4`, SSH `Host gx10-02` as `legenex-02`
- RoCE A `192.168.100.10` ↔ `192.168.100.11`
- RoCE B `192.168.101.10` ↔ `192.168.101.11`
- `gx doctor` 13/13

## Backup

- Stamp `20260923T091634Z` (UI Back Up Now) — pre-cleanup verified point
- Local gx10-01 `24ba5393` 22.167 GiB
- Local gx10-02 `fa6b8856` 9.235 GiB
- Peer copies `f5588ad0` / `bbee85a2`
- Prior verified stamp `20260923T090136Z` (`4ebe7225` / `a35af18c`) kept
- `restic check --read-data-subset=5%` no errors; UI Verify 2% no errors
- Restore `models.lock` sha256 `b70269f6…56f65` match
- Password files SHA-256 identical on both nodes; mode 0600
- GX10-02 disk: unused Docker images pruned (~102 GB) so copy could land; llama-cpp `:latest` retagged

## Coding suite

See `docs/BENCHMARKS.md`. Results JSON: `tests/ornith-suite/results/summary.json`. **5/6 PASS, 1 PARTIAL.**

## Concurrency

- gx-code-01 1.85 s 51 t/s NODEONE-PROOF
- gx-code-02 2.10 s 52 t/s NODETWO-PROOF
- node2 llama.cpp log: eval 110 tokens, 57.53 t/s
- Post job `gx-code` LB0–LB3 0.16–0.21 s

## gx-auto

`/srv/logs/gx-auto-routing.jsonl`: greeting/summary/reason → gx-mini; scripts/coding/debug → gx-code. Busy coding: decision tier gx-code, 11890 ms, not mini.

## gx-max

`gx_max: {mode: dual-worker, solver: gx-code-01, reviewer: gx-code-02, verdict: fail}` then repair; 46.4 s. OpenWebUI gx-max 6.6 s with the same banner after merging system turns.

## OpenWebUI / AgentOS / UI

- OWUI picker four modes; 24 chats remain
- AgentOS `/api/ask-live` gx-code, concurrent pair both gx-code
- Playwright: all seven primary pages authenticated; Backup last snapshot `20260923T090136Z`
