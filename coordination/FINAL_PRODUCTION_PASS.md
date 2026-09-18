# FINAL PRODUCTION PASS — 2026-09-18

Persistent execution state. Resume from this file. Do not rediscover.

Git at start: `bf6c7e05fb2784cfcf5de71180edc8e56f1319cf` main == origin/main

## Live facts (2026-09-18 21:41Z)

| Node | MemAvailable | Swap used | Notes |
|---|---|---|---|
| GX10-01 | 48.9 GiB | ~2.8 GiB | gx-mini + gx-fast containers Up 9h; LiteLLM healthy; CC :8088 200; PG :8090 200; OWUI :3000 200 |
| GX10-02 | 62.6 GiB | ~1.6 GiB | gx-reason Up ~1h; media-router/comfyui/llama-swap healthy; gx-call/voice/live/music user units active |

gx-reason vLLM log: `speculative_config=None` — DFlash NOT active.
DFlash2 dir `/srv/models/vllm/Qwen3.8-27B-DFlash2`: config.json + model.safetensors 1090932091 bytes. **No spec.json**. Garbage 0-byte file present.
node02.yaml passes `--speculative-config /models/dflash2/spec.json` (file missing on disk).

## Workstreams

| ID | Status | Owner | Notes |
|---|---|---|---|
| A models/DFlash | IN PROGRESS | builder | Prove real inference; activate DFlash; residency |
| B Control Center CSRF/lifecycle | IN PROGRESS | builder | Central CSRF retry; Load/Unload/Restart |
| C Creative Flow engine | IN PROGRESS | builder | Edges persist; CSRF on Run; real DAG |
| D OpenWebUI | PENDING | | Native tools, DSML, ask_user, image_url |
| E Call agents | PENDING | | MVA + Workers Comp real sessions |
| F acceptance/git/deploy | PENDING | | Browser, gitleaks, sync, review |

## Changed files

(none yet this pass)

## Tests

(none yet this pass)

## Unresolved

- DFlash speculative_config=None
- CSRF mutation failures reported in UI
- Creative Flow Run CSRF
- Workers Comp intake template missing (only intakepilot_mva)
- gx-call-test container Exited (1)
- LiteLLM /v1/models unauthenticated returned []
