# OpenWebUI

Container `open-webui`, host network port **3000**, volume `open-webui`. Compose: `/opt/open-webui/compose.yaml`.

Chats live in the Docker volume (`webui.db`). Backed up as `open-webui-data.tar.gz` inside restic dumps (verified archive lists `webui.db-wal`).

Connect the UI to `http://127.0.0.1:4000/v1` with the LiteLLM master key (already in gateway `.env`, never committed). Preferred picker:

- gx-auto (default)
- gx-mini
- gx-code
- gx-max

Internal aliases (`gx-fast`, `gx-reason`, `gx-code-01`, `gx-code-02`, media) should stay hidden in the user picker.
