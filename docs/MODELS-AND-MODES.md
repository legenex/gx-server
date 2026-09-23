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
