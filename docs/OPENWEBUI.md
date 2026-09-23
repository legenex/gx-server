# OpenWebUI

Container `open-webui`, host network port **3000**, volume `open-webui`. Compose: `/opt/open-webui/compose.yaml`. `restart: always`.

Chats live in the Docker volume (`webui.db`). Backed up as `open-webui-data.tar.gz` inside restic dumps.

Connect the UI to `http://100.105.214.61:4000/v1` (LiteLLM). The GX connection’s API key must be the live LiteLLM master key from `legenex/gateway/.env` (never committed). A stale 25-character key hid `gx-code` from the picker until it was replaced.

## Picker (normal choices)

- gx-auto (default: `ui.default_models`)
- gx-mini
- gx-code
- gx-max

`ui.model_order_list` is that order. Connection `openai.api_configs` `"1"` `model_ids` is the same four. Internal workers (`gx-code-01`, `gx-code-02`, `gx-fast`, `gx-reason`, media) stay out of the normal picker.

Verified 2026-09-23: temp admin session `/api/models` returned exactly those four ids. Live prompts:

| Mode | HTTP | Note |
|---|---|---|
| gx-mini | 200 | `PONG-OWUI-MINI` |
| gx-code | 200 | `PONG-OWUI-CODE` |
| gx-auto | 200 | `Hello` |
| gx-max | 200 | dual-worker banner `solver=gx-code-01 reviewer=gx-code-02` |

Existing data: **24 chats, 116 messages** still present after verification (temporary closeout users deleted).
