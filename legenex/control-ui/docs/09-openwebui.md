# Open WebUI setup

Open WebUI connects to the gateway as an OpenAI-compatible connection. The
**Setup → Open WebUI** page shows these values live, with copy buttons and a
connection test. Verified against Open WebUI **0.11.4** (container `open-webui`
on gx10-01, image `ghcr.io/open-webui/open-webui:v0.11.4`).

Production is already wired. On gx10-01:

| Field | Value |
|---|---|
| OpenAI API | on |
| Ollama | off |
| URL | `http://127.0.0.1:4000/v1` |
| Auth | Bearer, gateway key |
| API Type | Chat Completions |
| Model IDs | `gx-auto`, `gx-mini`, `gx-code`, `gx-max` |

Do not point this host-network container at Tailscale `:4000`.

Operator notes, memory settings, backup and restore:
`legenex/open-webui/README.md`.

## Memory, folders, notes

Memories, Memory System Context, background review (every 10 turns), folders
and notes are enabled. User permission **Features → Memories** is on.
Identity entries use native function calling and the Memory builtin-tool
category.

Personalization → Memory is the manual editor. Models with native tools can
also add/list/update/delete memories.

## Computer workspaces

Open WebUI can list Computer workspaces as models `cptr/<name>` once a
Computer gateway key exists. See [Computer](/#/docs/computer).

## Model identity

Open WebUI lists the aliases straight from the gateway. Without a model entry
it sends no system prompt, and a small model then answers "which model are
you?" from its training data.

The Control Center keeps one Open WebUI model entry per text alias
(`gx-mini`, `gx-code`, `gx-auto`, `gx-max`):

* **Name:** the alias.
* **Description:** names the underlying model.
* **System prompt:** a short, factual one built from `legenex/models/registry.json`.
* **Function calling:** native. **Builtin tools:** Memory on; other system
  tools off for ordinary chat.

**How the entries stay current:**
* **Setup → Open WebUI → Model identity** shows whether each entry matches the
  registry. **Sync identity from the registry** writes the missing or
  outdated ones.
* **Model Manager** re-syncs after an assignment or a rollback.
* The daily integrity audit reports any drift.
* From a shell: `cd legenex/control-ui && python3 -m gx_control_ui.owui_identity check`
  (or `apply`).
