# Models and modes

Four operator-facing modes, one gateway (`http://127.0.0.1:4000/v1`).

| Mode | What it is | Backend |
|---|---|---|
| gx-mini | always-on small uncensored helper | llama.cpp Ornith-1.5-9B Q8_0 on gx10-01 |
| gx-code | serious coding/reasoning, two independent workers | node1 + node2 (Ornith-1.5-35B-A3B Q5_K_M llama.cpp when loaded; during cutover LiteLLM also balances existing gx-fast / gx-reason) |
| gx-auto | router | orchestrator `:18900` — mini for trivial, code for coding (queued, never silent mini downgrade), max for extreme |
| gx-max | dual-worker solver + independent reviewer | both gx-code workers; DeepSeek TP=2 remains on disk as optional `GX_MAX_MODE=deepseek` |

Context defaults: mini 32–64k, code 32–64k. Huge windows are not the default.

Pins: `models.lock`. Mini files sha256-verified after download.
