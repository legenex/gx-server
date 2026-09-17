# gx-cluster user manual

This manual is for people who call the cluster's models and for the operator
who runs the two GX10 nodes. For deep recovery procedures, see
`OPERATIONS.md` and `RECOVERY.md`.

## 1. Using the models

**Purpose.** One OpenAI-compatible endpoint serves seven aliases.

**Prerequisites.**

* Network access to gx10-01 port 4000 (LAN or Tailscale).
* An API key. The operator creates one in the Control UI under
  **API Keys** (section 6). Keys are never stored in this repository.

| Alias | Use it for | Runs on |
|---|---|---|
| `gx-mini` | fast small tasks, vision input (uncensored Qwen3.5-4B) | node 1 |
| `gx-fast` | general chat, coding, tools, vision (uncensored Qwen3.6-35B-A3B) | node 1 |
| `gx-reason` | multi-step reasoning (interim Qwen3.6-27B until B-025 is closed) | node 2 |
| `gx-max` | the hardest prompts; uncensored DeepSeek-V4-Flash (CRACK) across **both** nodes | both |
| `gx-auto` | lets the router pick; understands Kilo Code requests; **never** starts gx-max | — |
| `gx-image` | image generation, edit (`/v1/images/edits`) and variation | node 2 |
| `gx-video` | text-to-video, image-to-video and video edit (`/v1/videos`, `/v1/videos/edits`) | node 2 |

Base URL: `http://100.105.214.61:4000/v1` (Tailscale). Step-by-step client
setup for Kilo Code, Open WebUI, curl, Python, JavaScript and agents is in the
Control UI's **Docs** page.

**Steps.**

```bash
curl http://gx10-01:4000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"gx-fast","messages":[{"role":"user","content":"Hello"}]}'
```

**Expected result.** A standard chat-completion JSON response.

**Errors.**

* `401`: bad key.
* `503 gx_max_unavailable`: gx-max could not be started. It is **never**
  silently replaced by a smaller model.
* `503 gx_max_not_running` (gx-auto): only gx-max can hold this context, and
  it is not running. Start it from the Control UI, or shorten the context.
* `403` on a model: your key is not allowed that alias.
* A slow first reply: the model is loading on demand. gx-max takes about
  9 minutes from cold, because it takes over both nodes.

**While gx-max is running,** gx-mini, gx-fast, gx-reason, gx-image and
gx-video are unavailable, because gx-max needs both whole nodes. They come
back automatically when gx-max is released (idle after 30 minutes, or by
the operator).

**Privacy.**

* Prompts are processed only on the two local nodes.
* Management traffic uses Tailscale; model traffic uses the private ConnectX
  link.
* No prompt content is written to this repository.

**Accessibility, mobile and offline use.** This is an HTTP API, so any
client works, including screen-reader-friendly chat front-ends such as Open
WebUI on gx10-01. The cluster needs no internet access to serve; it needs it
only for Git sync and model downloads.

## 2. Operating gx-max

```bash
legenex/lifecycle/gx-max-status.sh              # state of both ranks
curl -X POST http://127.0.0.1:18900/lifecycle/gx-max/acquire   # start (preferred path)
curl -X POST http://127.0.0.1:18900/lifecycle/gx-max/release   # graceful stop + restore
legenex/lifecycle/gx-max-start.sh               # direct start (same checks)
legenex/lifecycle/gx-max-stop.sh [--force]      # direct stop + restore
legenex/tests/gx-max-inference.sh               # prove it answers correctly
```

**What a start does.**

1. Drains the other GPU work on both nodes.
2. Checks that both nodes are clean: at least 100 GiB available,
   `/swapfile-sglang` active, at least 40 GiB swap free, and no existing
   memory pressure.
3. Starts rank1 on gx10-02, then rank0 on gx10-01.
4. Watches both nodes while the weights load.

During loading, the nodes legitimately drop to a few GiB of available memory
and use a lot of swap. That is expected, and it drains once the engine is
ready.

**Automatic protection.**

* **During load:**
  * a start aborts, and both nodes are unwound, on a kernel OOM kill or a
    hard driver out-of-memory error;
  * a start also aborts on memory+swap exhaustion, swap thrashing or a
    starving system, but only when the condition persists.
* **While serving:**
  * node 2's deadman removes rank1 if rank0 disappears;
  * node 1's watcher unwinds both nodes if either rank disappears or node 2
    is unreachable.
* **After any unwind:** normal services are restored and the result is
  verified.

**Logs.** All logs are in `/srv/logs`:

| File | Contents |
|---|---|
| `gx-max-safety-node1-*.tsv` | per-sample memory, swap, PSI and verdict |
| `gx-max-rank0.log` | rank0 engine log |
| `gx-max-rank0-watch.log` | node 1 watcher |
| `~/gx-max-rank1-deadman.log` (on node 2) | node 2 deadman |

## 3. Kernel lock check

```bash
legenex/host/kernel-lock/verify-kernel-lock.sh     # read-only; exit 0 = lock intact
```

Never upgrade to kernel 7.0 (L-4). `INFO` lines about older-ABI 6.8 kernel
packages are expected and harmless.

## 4. Source control (operators)

Edit only on gx10-01. Changes are committed and pushed to
`github.com/legenex/gx-server` automatically after 45 quiet seconds, and
gx10-02 follows within seconds. Details and troubleshooting are in
`ops/git-sync/README.md`.

**Privacy.** The repository is **public**:

* never put keys, tokens or passwords in tracked files;
* use `legenex/gateway/.env` (ignored) or
  `/srv/projects/gx-cluster/secrets/`.

**Error handling.** Automatic commits are blocked when a secret is
detected. See `/srv/logs/gx-git-sync/secret-blocks.log`; it lists file names
and rules only, never values.

## 5. The management web UI (gx-control-ui)

**Purpose.** One place to see both nodes, the fabric, every model alias,
gx-max's lifecycle, Git sync and logs; to load and unload models through
the sanctioned paths; to try the API; and to read the full user
documentation (Docs tab).

**Prerequisites.**

* Tailscale access to gx10-01, or a shell on gx10-01.
* The admin password. At installation a random one is written to
  `/srv/projects/gx-cluster/secrets/control-ui/initial-admin-password`
  (mode 0600, readable only by `legenex`). Set your own with the helper
  below; that also deletes the file.

**Steps.**

1. Open `http://100.105.214.61:8088/` (Tailscale) or
   `http://127.0.0.1:8088/` on gx10-01.
2. Sign in as `admin`.
3. Use the left navigation. It collapses behind the ☰ button on phones.

| Page | Use it to |
|---|---|
| Dashboard | See at a glance whether both nodes, both rails, Tailscale, services, models and Git sync are healthy |
| Models | Read each alias's facts and live state; LOAD / UNLOAD / RESTART (gx-max: type `gx-max`) |
| Runtime | Inspect memory, swap, PSI, containers and units per node |
| Cluster | See the two-node topology and live RoCE throughput |
| Jobs / Queue | Follow a gx-max load or release phase by phase; see waiting requests and the media queue |
| Logs | Read 25 predefined, redacted log streams; filter; download an excerpt |
| API Playground | Send real chat, vision, tool, image and video requests; copy curl / Python / JavaScript |
| Docs | Read the full user and operator guide |
| Create | Generate or edit images and videos; results land in the Media Library |
| Media Library | Browse, search, favourite, rename, download (ZIP), make variations and delete media |
| Model Manager | See installed models and bindings; search Hugging Face; stage, test, assign, roll back and delete models |
| API Keys | Create, test, replace and revoke gateway keys |
| Settings / System | Check versions and sync; run the integrity audit, the kernel verifier, a node 2 reconcile, or a safe restart |

**Set or reset the password** (on gx10-01):

```bash
cd ~/Documents/Projects/Server/gx-cluster
legenex/control-ui/scripts/gx-ui-passwd            # prompts twice
legenex/control-ui/scripts/gx-ui-passwd --status
```

**Expected result.** Changing the password signs every browser out
immediately.

**Service.**

* Unit: `gx-control-ui.service` (user unit; starts at boot).
* Restart: `systemctl --user restart gx-control-ui`.
* Logs: `/srv/logs/gx-control-ui/control-ui.log` and `audit.log`.
* Health: `curl -sS http://127.0.0.1:8088/api/ready`.
* Reinstall: `legenex/control-ui/scripts/install.sh`.

**Error handling.**

* "Sign-in failed / too many failed logins": wait 15 minutes, or restart
  the service.
* An operation is refused: the message says why. Typical reasons are
  gx-max owning the cluster, another operation still running, or the
  admission guard's 30 GiB reserve.
* "backend is not reachable": the service is restarting. The page
  recovers by itself.

**Mobile behaviour.** The layout is responsive down to phone width. Wide
tables scroll inside their own box, and the navigation becomes a drawer.

**Accessibility behaviour.**

* Semantic HTML with a skip link and visible focus.
* Statuses are always shown as text, not by colour alone.
* Supports reduced motion.
* Dark theme by default, with a light theme toggle.
* Checked with axe-core against WCAG 2.2 AA; no violations were found on
  any page.

**Offline behaviour.** The UI needs the cluster. It works without internet
access, except for the GitHub HEAD check, which then shows as unreachable.

**Privacy.**

* Playground prompts go only to the local gateway.
* The UI stores only pass/fail, latency and a 200-character excerpt of the
  last result per alias.
* Logs shown in the browser are redacted.
* The browser never receives an API key.

## 6. Creating media, managing models and API keys (Control UI)

### Create and Media Library

**Purpose.** Make images and videos without writing API calls, and keep
every result.

**Steps.**

1. Open **Create**. Pick a tab: Generate Image, Edit Image, Generate Video,
   Image to Video or Edit Video.
2. Type a prompt. For the edit tabs, choose a source from the library or
   upload one (PNG, JPEG or WebP images; MP4, MOV or WebM videos; up to
   150 MB).
3. Press **Generate**. The job card shows its phase: queued, loading
   models, generating, saving.
4. When the job is ready, press **Open in Library**.

**Expected result.** An image arrives in about 15–30 s. A 3-second video
takes about 45–75 s; the first job after an idle period takes longer while
the models load. Edits never overwrite: the result is a new item linked to
its source.

**Errors.** "gx-max is running": media is unavailable until gx-max is
released. An upload is refused if its type or size is wrong. Delete asks you
to type `DELETE`.

**Privacy.** Files stay in `/srv/projects/gx-cluster/media` on gx10-01. Uploads
are copied to gx10-02 for processing only and deleted afterwards.

**Mobile, accessibility and offline use.** The grid reflows to one column on
phones. Every tile has a text label and keyboard-operable controls; the tabs
support the arrow keys. The media library works without internet access.

### Model Manager

**Purpose.** Replace a model safely, and remove models that are no longer used.

**Steps.**

1. Open **Model Manager**. Check the **Alias bindings** table (current
   repository, revision, previous model) and **Installed models**.
2. To install a model, paste a Hugging Face URL or ID, or search. Review the
   classification (full model, GGUF, LoRA or adapter), the size and the
   licence, then press **Stage** on the node.
3. After the download is verified, press **Test-serve**. This loads the model
   in a temporary server and asks a real question.
4. Press **Assign to <alias>** and confirm. The UI changes the binding,
   restarts the model server and asks a real question through the gateway.
   If that fails, it rolls back automatically.
5. When you are happy, press **Accept**. The previous model's row then
   offers **Delete**.

**Errors.** Gated models need a Hugging Face token: save one under
**Hugging Face token**. It is stored with mode 0600 and never shown again.
Delete is disabled for anything still in use. Operations are refused while
gx-max is running.

**Privacy.** The token is sent only to huggingface.co. Model-card
instructions are never executed.

### API Keys

**Purpose.** Give each client (Kilo Code, Open WebUI, scripts, agents) its
own revocable key.

**Steps.**

1. Open **API Keys**, enter a name and pick the allowed aliases. gx-max is
   unticked by default. Optionally set an expiry and limits.
2. Press **Create key**. Copy the key immediately; it is shown **once**.
3. Press **Test this key** to check `/v1/models` and a real gx-mini reply.
4. Later, use **Replace** (a new secret with the same settings; the old one
   stops working) or **Revoke**. Revoke asks you to type the key's name.

**Expected result.** The client works with base URL
`http://100.105.214.61:4000/v1` and the new key. A revoked key gets `401`.

**Privacy.** The UI never stores the secret and never shows the gateway
master key.
