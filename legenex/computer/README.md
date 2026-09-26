# Open WebUI Computer (gx10-01)

Official Open WebUI Computer on gx10-01, fully integrated with the production
Open WebUI. Workspaces are the real host tree
`/home/legenex/Documents/Projects/Server` mounted at `/projects`. Nothing here
needs a UI setup step: `tools/provision.py` builds the whole integration and
`tools/verify.py` proves it end to end.

| | |
|---|---|
| Version | `0.9.21` (`ghcr.io/open-webui/computer:0.9.21`; package `cptr 0.9.21`) |
| Compose | `legenex/computer/docker-compose.computer.yml` (project `gx-computer`) + ignored `.env` |
| Container | `gx-computer` (runs as uid 1000 `cptr`, `no-new-privileges`, 2 GiB limit) |
| Persistent volume | `gx_computer_data` → `/data` (`app.db` SQLite, `config.toml`, `uploads/`) |
| Project mount | host `/home/legenex/Documents/Projects/Server` → `/projects` (read/write) |
| Workspace | **GX-Cluster** = `/projects/gx-cluster` (the gx10-01 checkout itself; same inode) |
| Management URL | `http://100.105.214.61:8000` (Tailscale), `http://gx10-01:8000`, `http://127.0.0.1:8000` |
| Health | `http://127.0.0.1:8000/api/health` |
| OpenAI gateway | `http://127.0.0.1:8000/v1` (used by host-networked Open WebUI) |
| LiteLLM from Computer | `http://gx-litellm:4000/v1` on Docker network `gx_gateway` |
| Restart | `unless-stopped`; recreate with `docker compose -f legenex/computer/docker-compose.computer.yml up -d` |

Binds: Tailscale `100.105.214.61:8000` and loopback only. Not on `0.0.0.0`,
not on ConnectX, not in the Cloudflare tunnel (its only ingress is
`chat.legenex.co → :3000`), not privileged, no Docker socket.

## Identity

The canonical identity is the Open WebUI admin account. Computer has **one**
user, its admin, with the **same login name as the Open WebUI e-mail**. Its
display name and avatar are copied from Open WebUI by `tools/provision.py`, so
Computer greets and labels the same person. The account keeps Computer's own
password (set at first login). There is no second or orphaned identity.

Why not single sign-on (checked against the installed source, 2026-09-26):

* cptr 0.9.21 supports only `password`, `pam` and `trusted_header` auth. It has
  no OAuth/OIDC, and it cannot accept an Open WebUI session.
* Open WebUI 0.11.4 is an OIDC *client* only. It cannot issue identities to
  other apps.
* `trusted_header` would need a new authenticating reverse proxy on gx10-01.
  That is an L-2 change and needs sign-off. In that mode cptr also
  auto-creates any asserted user without enforcing `pending`, and it has no
  break-glass login.
* The Open WebUI password hash is **not** copied. Computer's own agents and
  terminal run as the same uid that owns `/data/app.db`, so the hash of the
  public-facing Open WebUI account would become readable by agent tooling.

## Workspace, instructions and what Computer writes

* Registered through `PUT /api/state/workspace`. The empty first-login default
  workspace (`/home/cptr`) was retired, which removed its DB row only.
  `/projects` holds no other project.
* `/projects/gx-cluster/.cptr/system.md` is the workspace system-prompt
  template. It keeps cptr's placeholders (`{{CPTR_CONTEXT}}`, `{{MEMORY}}`,
  `{{INSTRUCTIONS}}`, `{{SKILLS}}`, `{{FILE_TREE}}`), so the repository's
  `CLAUDE.md` is injected (about 20 k characters in total). It also adds the
  GX rules and the public-repo rules.
* `.cptr/model` = `gx-auto` is the default model for gateway chats in this
  workspace.
* Computer writes chat transcripts (including tool output), attachments, task
  logs, screenshots, memory and audio under `.cptr/`, and generated images in
  the workspace root. The repo is **public and autosynced**, so `.gitignore`
  ignores `**/.cptr/*` except `system.md` and `model`, plus
  `/generated-image-*` and `/edited-image-*`. `CPTR_AUTO_GITIGNORE_DOT_CPTR=false`
  stops Computer rewriting `.gitignore`. `tools/test_tools.py` guards these rules.
* Read-only overlays: `legenex/gateway/.env` (placeholder `env.hidden`),
  `.git/hooks`, `.githooks`, `ops/git-sync`. Normal source edits and
  `git add/commit` work.

## Local inference (Computer → LiteLLM)

One connection, **GX LiteLLM (local)**: OpenAI Chat Completions at
`http://gx-litellm:4000/v1`, with an allow-list of `gx-auto`, `gx-mini`,
`gx-code` and `gx-max`. The default chat model is `gx-auto`. There are no
cloud providers.

It authenticates with the LiteLLM virtual key **`gx-computer`**, which is
limited to those four aliases and visible in Control Center → API Keys. It is
never the master key. The source copy is
`/srv/projects/gx-cluster/secrets/computer/litellm-api-key` (0600). Computer
stores it Fernet-encrypted in its DB.

## Gateway (Open WebUI → Computer)

* Computer gateway key **`open-webui`** (`sk-cptr-…`). Computer stores only
  its SHA-256. The plaintext exists only in Open WebUI's connection store.
  The key acts as the Computer admin.
* Open WebUI connection: `http://127.0.0.1:8000/v1`, auto-discovered models,
  tag `computer`, with these headers:

  ```json
  {"X-OpenWebUI-Chat-Id": "{{CHAT_ID}}", "X-OpenWebUI-Message-Id": "{{MESSAGE_ID}}",
   "X-OpenWebUI-User-Message-Id": "{{USER_MESSAGE_ID}}",
   "X-OpenWebUI-User-Message-Parent-Id": "{{USER_MESSAGE_PARENT_ID}}",
   "X-OpenWebUI-Task": "{{TASK}}"}
  ```

  Computer maps the chat and message ids to its own chat tree. It routes
  title, tag and follow-up tasks (`X-OpenWebUI-Task`) to the plain model
  instead of the agent.
* The workspace appears in Open WebUI's model selector as
  **"GX-Cluster - /projects/gx-cluster"** (id `cptr/gx-cluster`).
* **Admin-only by design.** The model has no Open WebUI model row, so
  non-admin users can neither list nor call it (verified: HTTP 400). Gateway
  chats run tools with auto-approval as the Computer admin, and Open WebUI is
  public. **Never create a public model entry or access grant for `cptr/*`.**

## Security settings

* `CPTR_CORS_ALLOWED_ORIGINS` is pinned to Computer's own origins. The upstream
  default `*` with credentials let any same-host app drive the API. Socket.IO
  (chat streaming) uses the same list, so **every hostname used to open
  Computer must be listed**. The MagicDNS FQDN is set via
  `GX_COMPUTER_CORS_ORIGINS` in the ignored `.env`. Other same-host origins
  (for example `:3000`) and foreign origins get HTTP 400.
* Agents in Computer can read what the `cptr` uid can read: the project tree
  (minus the overlays) and Computer's own `/data`. That includes the encrypted
  `gx-computer` key and its decryption secret, which is why that key is
  least-privilege. The gx10-01 secrets store is not mounted.
* The Grok CLI binary is mounted at `/usr/local/bin/grok`. No agent profile is
  configured, and host `~/.grok` is not mounted. Using it needs an xAI login
  inside Computer.

## Tooling (`tools/`)

| Command (from `legenex/computer/tools`) | What it does |
|---|---|
| `python3 provision.py` | Idempotent: LiteLLM keys, Computer profile, connection, default model, workspace and gateway key, then Open WebUI connections, the GX-Cluster folder and note, and compaction. `--rotate-gateway-key` replaces the gateway key in both apps. |
| `python3 verify.py [--compaction]` | Live end-to-end check (32 checks with `--compaction`). Every Computer file operation is checked on the host. It cleans up after itself. |
| `python3 -m unittest -v test_tools` | Offline guards: redaction, `.cptr` ignore rules, template placeholders. |

Admin calls use short-lived sessions minted **inside** each container with
that app's own signing secret: the same tokens their login endpoints issue.
The secrets never leave the containers, and no output contains a credential.
No password is read, set or reset.

## Backup and recovery

Backups (pre-change, 2026-09-26):
`/srv/projects/gx-cluster/backups/computer-repair-20260925T225949Z/`
(`computer/app.db` via the SQLite backup API, `config.toml`, compose, `.env`,
`env.hidden`, `.cptr` folder, container inspect; plus the Open WebUI side, see
`legenex/open-webui/README.md`). A post-repair snapshot sits next to it with
the suffix `-post`.

Restore Computer:

1. `docker stop gx-computer`
2. Copy `app.db` and `config.toml` into the volume
   (`/var/lib/docker/volumes/gx_computer_data/_data/`, owner uid 1000), and
   remove stale `app.db-wal` / `app.db-shm`.
3. `docker compose -f legenex/computer/docker-compose.computer.yml up -d`
4. `python3 legenex/computer/tools/provision.py`, then
   `python3 legenex/computer/tools/verify.py`.

If the volume is lost completely, start the container, claim the first admin
with the one-time `/?token=` URL from `docker logs gx-computer`, using the same
login name as the Open WebUI e-mail. Then run `provision.py`. It rebuilds
everything else and rotates the gateway key into Open WebUI.
