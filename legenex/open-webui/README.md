# Open WebUI (gx10-01)

Production chat UI for the GX-Cluster. Host-networked on gx10-01, persistent
volume `open-webui`. Talks to LiteLLM on loopback.

| | |
|---|---|
| Version | `0.11.4` (pinned; `ghcr.io/open-webui/open-webui:v0.11.4`) |
| Container | `open-webui` |
| Compose (live) | `/opt/open-webui/compose.yaml` |
| Compose (tracked) | `legenex/open-webui/docker-compose.yml` |
| Volume | `open-webui` → `/app/backend/data` (SQLite `webui.db`) |
| URL | `http://127.0.0.1:3000` and `http://100.105.214.61:3000`; public `https://chat.legenex.co` via cloudflared |
| LiteLLM | `http://127.0.0.1:4000/v1` (host network). Do not point this container at Tailscale `:4000`. |
| Models listed | `gx-auto`, `gx-mini`, `gx-code`, `gx-max` |

Do not wipe the `open-webui` volume. Do not rename it.

## Memory (persisted ConfigVar)

Effective live values (SQLite `config` table; env vars seed a **fresh**
volume only):

| Key | Value |
|---|---|
| `memories.enable` | true |
| `memories.system_context.enable` | true |
| `memories.background_review.enable` | true |
| `memories.review_interval_turns` | 10 |
| `memories.user_char_limit` | 8000 |
| `memories.context_char_limit` | 12000 |
| `folders.enable` | true |
| `notes.enable` | true |
| `user.permissions.features.memories` | true |

User permission: Admin Panel → Users → Permissions → Features → Memories.

Builtin Memory tools are enabled on the GX identity entries (`gx-mini`,
`gx-code`, `gx-auto`, `gx-max`) with native function calling. The OpenAI-compat
API (`/api/chat/completions`) injects those tools when the request comes from
the UI (`session_id`). Direct API clients that omit `session_id` do not get
hidden builtin tools.

`gx-mini` and `gx-code` both emit native OpenAI `tool_calls` against LiteLLM.
Very small models can be inconsistent at *autonomous* memory management; use
`gx-code` when you want the model to save/search/delete memories itself.

## Folders / Projects

Open WebUI folders are the project workspaces: grouped chats, optional folder
system prompt, attached knowledge/files. Notes are enabled. No custom
replacement is required.

## Backup

Timestamped copies live under `/srv/projects/gx-cluster/backups/open-webui/`.
Each run contains:

- `webui.db` — SQLite online backup (`sqlite3.backup`, not a raw copy of a live DB)
- `compose.yaml`
- `image-inspect.txt`
- `config-keys.json` (secrets redacted)
- `counts.json`

Example: `/srv/projects/gx-cluster/backups/open-webui/20260925T062453Z/`.

## Restore

1. Stop the container: `docker stop open-webui`
2. Copy the chosen `webui.db` over the volume file
   `/var/lib/docker/volumes/open-webui/_data/webui.db` (remove stale
   `webui.db-wal` / `webui.db-shm` next to it).
3. Start: `docker compose -f /opt/open-webui/compose.yaml up -d`
4. Check `GET http://127.0.0.1:3000/api/version`, then that users and chats
   match `counts.json`.

Do not restore an older schema over a newer one without checking alembic.

## Identity sync

Control Center keeps GX model entries via
`python3 -m gx_control_ui.owui_identity check|apply` from
`legenex/control-ui`. Verified against Open WebUI `0.11.4`.
