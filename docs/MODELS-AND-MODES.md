# Models and modes

Four operator-facing modes, one gateway (`http://127.0.0.1:4000/v1`).

| Mode | What it is | Backend |
|---|---|---|
| gx-mini | always-on small uncensored helper | llama.cpp Ornith-1.5-9B Q8_0 on gx10-01 |
| gx-code | serious coding/reasoning, two independent workers | Ornith-1.5-35B-A3B Q5_K_M llama.cpp on **both** nodes (`gx-code-01`, `gx-code-02`; LiteLLM load-balances `gx-code`) |
| gx-auto | router | orchestrator `:18900` — mini for trivial, code for coding (queued, never silent mini downgrade), max for extreme |
| gx-max | dual-worker solver + independent reviewer | both gx-code workers; DeepSeek TP=2 remains on disk as optional `GX_MAX_MODE=deepseek` |

Context defaults: mini 32–64k, code 32–64k. Huge windows are not the default.

Pins: `models.lock`. Mini files sha256-verified after download.

Gateway (also Tailscale): `http://100.105.214.61:4000/v1`

## MVA Progress Tracker (when it appears)

The app is **not** in this workspace. Leave the cluster endpoint as above. Intended mapping:

| Work | Mode |
|---|---|
| Continuous / light tasks | `gx-mini` |
| Weekly planning | `gx-code` |
| Major review | `gx-max` |
| General / default | `gx-auto` |

No further Progress Tracker work in this project.

## Hermes (remote)

Hermes lives on the VPS, not on these GX10s. Local AgentOS `hermes/model-routing.yaml` already points `gx-gateway` at `http://100.105.214.61:4000/v1` with gx-mini / gx-code / gx-auto / gx-max.

**Hermes remote reconciliation: DEFERRED.** Do not treat that as a GX cluster failure.

## Gateway list

`GET /v1/models` (authenticated) returns:

- `gx-mini`
- `gx-code` (load-balanced)
- `gx-code-01` (internal worker, gx10-01)
- `gx-code-02` (internal worker, gx10-02)
- `gx-auto`
- `gx-max`

`gx-code-01` / `gx-code-02` stay in LiteLLM because gx-max dual-worker pins solver and reviewer. They are not primary operator modes. OpenWebUI `openai.api_configs` already restricts the picker to `gx-auto`, `gx-mini`, `gx-code`, `gx-max`.

llama-swap on node 2 still accepts the historical upstream id `gx-reason` as an **alias of gx-code**. That alias is internal to llama-swap, not a LiteLLM public model.

## Retired (not running, recoverable)

These names are no longer user-facing and their services are stopped/masked:

GX-Playground, gx-image, gx-video, gx-music, gx-voice, gx-call, gx-live, gx-fast, gx-reason (as a public LiteLLM alias), media router, ComfyUI.

Source: `legenex/gx-server` (`legenex/playground`, `legenex/media`, `legenex/voice`, `legenex/music`, `legenex/call`, `legenex/live`). Restic snapshot `24ba5393` (stamp `20260923T091634Z`) already contained that tree plus `/srv/projects/gx-cluster/media` and the playground unit file. Unit files archived at `Documents/Archive/disabled-units-20260924T0015Z/`.
