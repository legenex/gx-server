# EVIDENCE

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
