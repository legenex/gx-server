# Open WebUI Computer (gx10-01)

Official Open WebUI Computer on gx10-01. Workspaces are the real host tree
`/home/legenex/Documents/Projects/Server` mounted at `/projects`.

| | |
|---|---|
| Version | `0.9.21` (`ghcr.io/open-webui/computer:0.9.21`; package `cptr 0.9.21`) |
| Compose | `legenex/computer/docker-compose.computer.yml` (project `gx-computer`) |
| Container | `gx-computer` |
| Persistent volume | `gx_computer_data` → `/data` |
| Project mount | host `/home/legenex/Documents/Projects/Server` → `/projects` (read/write) |
| gx-cluster path | `/projects/gx-cluster` (same inode as the host checkout) |
| Management URL | `http://100.105.214.61:8000` (Tailscale) and `http://127.0.0.1:8000` (loopback) |
| Internal health | `http://127.0.0.1:8000/api/health` |
| OpenAI gateway | `http://127.0.0.1:8000/v1` (from host-network Open WebUI) |
| LiteLLM from Computer | `http://gx-litellm:4000/v1` on Docker network `gx_gateway` |
| Restart | `unless-stopped` |

Binds: Tailscale `100.105.214.61:8000` and loopback only. Not on `0.0.0.0`,
not on ConnectX, not privileged, no Docker socket. The real project tree is
mounted read/write; `legenex/gateway/.env` is overlaid read-only with
`env.hidden` so Computer cannot read or rewrite gateway secrets.

When adding the LiteLLM connection, set Models to
`gx-auto,gx-mini,gx-code,gx-max` (do not auto-discover; loopback LiteLLM
also lists internal workers `gx-code-01` and `gx-code-02`).

## Architecture

```
OpenWebUI (host :3000)
  -> personal memory / folders / notes
  -> Computer gateway http://127.0.0.1:8000/v1   (after a Computer API key exists)
       model ids: cptr/<workspace-folder-name>   e.g. cptr/gx-cluster

Computer (gx-computer)
  -> /projects (real filesystem, git, terminal)
  -> LiteLLM http://gx-litellm:4000/v1
       live aliases: gx-mini, gx-code, gx-auto, gx-max
       (loopback also has internal workers gx-code-01, gx-code-02)
  -> Grok CLI at /usr/local/bin/grok (binary only; login is inside Computer)
```

Workspace default model file: `/projects/gx-cluster/.cptr/model` → `gx-auto`.
Computer-specific instructions: `/projects/gx-cluster/.cptr/system.md`.

## First login

Computer starts with no users. The first-run URL with a one-time token is in
`docker logs gx-computer` (`http://…:8000/?token=…`). Open that on Tailscale,
create the admin account, then continue with Connections / Agents / Gateway
below. The token works once.

## Computer → LiteLLM (local GX models)

In Computer, after login:

1. Settings → Admin → Connections → Add
2. Provider: **OpenAI**
3. API Type: **Chat Completions**
4. Base URL: `http://gx-litellm:4000/v1`
5. API Key: the LiteLLM master key from Control Center → Connections (Reveal). Do not paste it into Git.
6. Models: leave empty to auto-discover, or `gx-auto,gx-mini,gx-code,gx-max`
7. Save. Default chat model: **gx-auto**. Gateway model (Settings → Admin → Gateway): **gx-auto**.

Do not connect Computer to OpenAI, Anthropic, Gemini, or another paid cloud
inference provider.

## Grok

The Grok Build CLI binary is bind-mounted at `/usr/local/bin/grok` (`grok 1.0.41`).
Host `~/.grok/auth.json` is **not** mounted.

In Computer:

1. Settings → Admin → Agents → Add
2. Type: **Grok**
3. Command: `/usr/local/bin/grok`
4. Home: `/data/grok-home` (keeps login on the Computer volume)
5. Save, then open a Computer terminal and run `grok login` (or set `XAI_API_KEY` in that Home). Re-detect until status is **ready**.

Model ids look like `agent:<profile-id>/<model>`.

## Open WebUI → Computer gateway

Gateway keys can only be created in the Computer UI (Settings → Admin → Gateway).
Shown once, stored hashed.

Then in Open WebUI:

1. Settings → Admin → Connections → Manage OpenAI API Connections → Add
2. URL: `http://127.0.0.1:8000/v1`
3. API Key: the `sk-cptr-…` key
4. Custom headers:

```json
{
  "X-OpenWebUI-Chat-Id": "{{CHAT_ID}}",
  "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
  "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
  "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
  "X-OpenWebUI-Task": "{{TASK}}"
}
```

5. Verify, Save. Workspaces appear as `cptr/<folder-name>` (this repo: `cptr/gx-cluster` once `/projects/gx-cluster` is added as a workspace).

Add `/projects` (or `/projects/gx-cluster`) as a workspace in Computer after first login.

## Verified 2026-09-25 (from inside `gx-computer`, no UI login needed)

| Check | Result |
|---|---|
| `/projects/gx-cluster` is the real checkout | `git rev-parse --show-toplevel` = `/projects/gx-cluster`, HEAD equals the host HEAD |
| create / edit / rename / delete via `/projects/gx-cluster/...` | each step seen on the host immediately; temp file removed |
| `git status`, `git log` from the workspace | work |
| `legenex/gateway/.env` inside Computer | 148-byte placeholder (`env.hidden`), read-only overlay; real 866-byte file not visible |
| Gateway | `GET http://gx-litellm:4000/v1/models` → `gx-mini, gx-code, gx-code-01, gx-code-02, gx-max, gx-auto` |
| Open WebUI → Computer | from inside `open-webui` (host network): `http://127.0.0.1:8000/api/health` and `http://100.105.214.61:8000/api/health` → 200 |
| Ports | only `100.105.214.61:8000` and `127.0.0.1:8000`; no docker.sock, not privileged |

Computer has **no users yet** (`users` table empty). The first admin login,
the LiteLLM connection, the gateway key and the Open WebUI connection above
are interactive authenticated steps and have not been performed; the
workspace-in-Open WebUI integration is therefore unverified end to end.
Read-only overlays (verified: `touch` fails with EROFS, normal source writes and `git status` work) also cover `gx-cluster/.git/hooks`, `.githooks` and `ops/git-sync`, so Computer cannot plant code that the host would run. Scope note: the whole `Server/` tree is mounted, so any other secret-bearing
file under it is visible to Computer agents (only the gateway `.env` is
overlaid).

## Backup

Copy the named volume `gx_computer_data` (`app.db`, `config.toml`, `uploads/`)
plus workspace `.cptr/` folders (chats travel with the project). See
https://docs.openwebui.com/ecosystem/computer/operate/data-and-backups
