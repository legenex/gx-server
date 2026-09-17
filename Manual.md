# gx-cluster user manual

This manual is for people who call the cluster's models and for the operator
who runs the two GX10 nodes. For deep recovery procedures, see
`OPERATIONS.md` and `RECOVERY.md`.

## 1. Using the models

**Purpose.** Eight public aliases (D-036). One OpenAI-compatible endpoint
serves seven of them. The eighth, `gx-music`, has its own authenticated API
on GX-Playground (section 7).

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
| `gx-music` | songs and instrumentals with ACE-Step 1.5 XL: `http://100.105.214.61:8090/v1/music/*` (not a chat model) | node 2 |

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

Creating moved to **GX-Playground** (section 7). The Control Center's
**Creative** page shows the Library size and active jobs and links to the
Playground. The notes below still describe the job behaviour.

**Purpose.** Make images and videos without writing API calls, and keep
every result.

**Steps.**

1. Open GX-Playground → **Images** or **Video**. Pick a tab: Generate,
   Edit or Variation; Text to Video, Image to Video or Video Edit.
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

**Errors and waiting.** Jobs are never refused for lack of memory; they wait
and say why. "gx-max owns the cluster": the job starts after gx-max is
released. "Unloading gx-reason to make room": in the Auto profile the
scheduler unloads an idle gx-reason (idle ≥ 5 min) or idle music for you.
"Maintenance mode is on": the job starts when Maintenance ends. An upload is refused if its type or size is wrong. Delete asks you
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

**Disk preflight.** Before anything downloads, the page shows the free space
on the target node, the download size, the 50 GiB headroom it keeps and
the verdict: SAFE, TIGHT or BLOCKED. Only SAFE plans are staged; the others
link to **Storage & Cleanup**.

**Errors.** Gated models need a Hugging Face token: save one under
**Hugging Face token**. The page then says "token saved". It is stored with mode 0600 and never shown again.
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

## 7. GX-Playground: images, video, music, voice, flows and realtime

**Purpose.** The creative app. Open `http://100.105.214.61:8090/` (Tailscale)
or `http://127.0.0.1:8090/` on gx10-01. The Control Center sign-in works here
too; a second sign-in is not needed when both apps are opened on the same host.

**Prerequisites.** A Control Center account. The **Playground** link in the
Control Center navigation opens it; **Control Center** in the Playground top
bar goes back.

**What is where.** The left rail has three groups:

| Group | Pages |
|---|---|
| **Create** | Dashboard · Creative Flows · Images · Video · Music · Voice |
| **Realtime** | Live · Call Agents |
| **Manage** | Library · History · Models · Logs · Settings |

On a phone the rail becomes a bottom bar with three buttons, one per group.

### 7.0 Use HTTPS for Live and Call Agents (microphone and camera)

**Purpose.** Browsers only allow the microphone and camera in a *secure
context*. `http://100.105.214.61:8090` is not one, so **Live** and **Call
Agents** cannot capture audio there. Two addresses work:

* `http://127.0.0.1:8090/` — only when you are sitting at gx10-01;
* `https://100.105.214.61:8443/` — from anywhere on the tailnet, once you trust
  the Playground's certificate authority.

**Steps (once per computer).**

1. Download the certificate authority: open `http://100.105.214.61:8090/pg/ca.crt`
   (or `https://100.105.214.61:8443/pg/ca.crt`). It is the **public**
   certificate; the private key never leaves gx10-01 and is mode 0600.
2. Trust it:
   * **macOS** — open it in Keychain Access → System → set *Always Trust*.
   * **Windows** — Install Certificate → Local Machine → *Trusted Root
     Certification Authorities*.
   * **Linux (Chrome/Chromium)** — Settings → Privacy and security →
     Security → Manage certificates → Authorities → Import → trust for
     identifying websites.
   * **Firefox** — Settings → Privacy & Security → Certificates → View
     Certificates → Authorities → Import.
3. Open `https://100.105.214.61:8443/` and sign in.

**Expected result.** No certificate warning, and the Live and Call Agents pages
stop showing the "this page needs a secure context" callout.

**Error handling.** If a page still says the context is insecure, you are on
the `http://` address — the callout links to Settings, which repeats these
steps. If the browser rejects the certificate, it was issued for
`gx10-01`, `gx10-01.taila7ef6a.ts.net`, `100.105.214.61` and `127.0.0.1` only;
use one of those names.

**Privacy.** The certificate authority is local to this cluster. It is not
published, and trusting it does not affect any other website.

**Mobile.** The same page works on a phone; the certificate must be installed
in the phone's own trust store first, which on iOS also needs
Settings → General → About → Certificate Trust Settings.

**Steps (music).**

1. Open **Music** → **Create**. Write a prompt and add style tags (type a tag
   and press Enter, or browse the tag groups).
2. Write lyrics with the section buttons (`[Verse]`, `[Chorus]`, …), or tick
   **Instrumental (no vocals)**. You can instead write a **Song description**;
   the language model then writes the lyrics, tempo and key itself.
3. Optionally set Duration, BPM, Key and Time signature, and the number of
   tracks (1-4). **Advanced** holds seed, steps, sampler and planner settings.
4. Press **Generate**. The job card shows queued → loading model → generating
   → saving, with the reason whenever it waits.
5. When it is complete, the track card has a waveform, a player and WAV, FLAC
   and MP3 downloads. **Remix/Cover**, **Repaint** (redo a time range) and
   **Extend** create new tracks linked to the source; the original is never
   changed.

**Expected result (measured 2026-09-17).**
* A 30 s song with vocals takes about 2 minutes on a cold engine (107 s load
  plus 14 s generation).
* With the engine loaded, a remix or repaint takes about 5 s.
* The downloads are WAV, FLAC and MP3.

**Music API.** Create a key under **API Keys** with gx-music ticked, then see
the Control Center **Docs → gx-music** page (`POST /v1/music/generations`,
`GET /v1/music/{id}`, `GET /v1/music/{id}/content?format=mp3`). Each key sees
only its own jobs. Load and unload are not part of the API.

**Errors.**
* "gx-max owns the cluster": the job runs after the release.
* "Waiting for gx-video to finish on gx10-02: … must be available; … is": a
  video is being made. Music and a cold video do not fit together while
  gx10-02 keeps its 30 GiB reserve, so the track starts when the video is
  done.
* A failed job shows plain words and a **Retry** button, never an internal
  error.

**Memory safety (videos and music, D-038).** gx10-02 always keeps 30 GiB
free:
* **Video while music is idle:** the music engine is unloaded first (this
  takes a few seconds, and the next track reloads it in about 100 s).
* **Video while music is working or pinned** (or the Music profile is
  active): the video waits, and its card says "Waiting for gx-music to
  release enough gx10-02 memory", with the numbers.
* **Video edit strength:** 0.5 or more (the keyframe edit) needs more memory
  than gx10-02 can give while keeping the reserve, so it is refused at once
  with an explanation. Use a strength below 0.5 (restyle) instead. B-028
  tracks a two-stage version.
* The turbo model has no extract, lego, complete or music guidance, so those
  controls are not shown.

### Images: models, edit modes and masks (Build V3)

**Purpose.** Generate images with a choice of model, and edit existing images
so that the requested change really happens while the rest stays.

**Prerequisites.** A signed-in Playground session. gx10-02 must be able to
take the job (Resource Control); otherwise it waits and says why.

**Models** (the **Model** menu at the top of the Images panel):
* **Qwen Image 2512**: the default generator; strong prompt following and
  text in images.
* **Qwen Image Edit 2511**: the default editor; follows written
  instructions and keeps faces and composition.
* **VisionmasterPro_V3**: an SDXL photoreal model. It can generate, redraw a
  whole image (Restyle, Full transformation) and repaint a masked area. It
  does **not** follow edit instructions, so describe the result you want.

**Steps (generate).**
1. **Images** → **Generate**, pick a **Model**, write a prompt.
2. Pick a size (each model offers its own), the number of images and, for
   Qwen, the quality. VisionmasterPro_V3 has a **Quality tags** switch.
3. Press **Generate**.

**Steps (edit).**
1. Pick a result and press **Edit**, or upload or choose a source image.
2. Pick an **Edit mode**: Change / replace, Add, Remove, Restyle,
   Background, Subject or Full transformation. The hint under the buttons
   says what the mode does.
3. Write the instruction (Qwen) or a description of the result
   (VisionmasterPro_V3).
4. Optional: open **Limit the edit to an area (mask)** and paint the area that
   may change. You can use the brush, the eraser, **Undo** (Ctrl+Z),
   **Invert** and **Clear**. There are two ways to do it without a mouse:
   * focus the painting area and use the arrow keys (Shift = faster), then
     Space or Enter to paint (E switches to the eraser);
   * enter rectangles in percent and press **Add rectangle**.
   VisionmasterPro_V3 needs a mask for every mode except Restyle and Full
   transformation.
5. **Edit quality** (Qwen): **Fast** (4 steps) or **High quality** (about
   20 steps with real guidance; slower; also uses the negative prompt).
6. **Strength** is shown only where it changes something: Full
   transformation (how far from the source layout) and the
   VisionmasterPro_V3 modes (how much is redrawn).
7. Press **Apply edit**. The result is a new image linked to its source.

**Expected result.** The requested change is visible. Without a mask, Qwen
keeps identity and composition as far as the mode asks. With a mask, pixels
outside it are the source's own. Measured timings and before/after examples
are in `coordination/build-v3/img.md`.

**Errors.**
* "… needs a mask for …": paint the area, or pick Restyle or Full
  transformation.
* "Full transformation changes the whole image": clear the mask.
* "the mask is empty": the painted area is too small; paint more.
* "Waiting for … gx10-02 memory": another tenant (usually gx-reason) holds
  the node; the job starts when it is released.
* **Run again** on a masked edit is refused: only the mask's size and
  coverage are stored, so paint it again.

**Mobile.** The panel stacks above the result. The mask painter works with
touch (the page does not scroll while you paint on it), and the rectangle
fields fall back to two columns.

**Accessibility.** Every control has a label. The model menu is a native
select. Edit modes and quality are toggle buttons with pressed state. The
painter has keyboard painting and a text alternative (rectangles), and the
selected coverage is announced.

**Offline.** Nothing is generated without gx10-02. The page still opens,
and a submitted job shows the connection error.

**Privacy.** Sources, masks and results stay on the cluster. The Library
keeps the mask's size, coverage, checksum and rectangles, not the mask
image. VisionmasterPro_V3 is an NSFW-capable checkpoint (licence
creativeml-openrail-m).

### Video: Wan 2.2 LoRAs, presets and history (Build V3)

**Purpose.** Apply your own Wan 2.2 LoRAs to text-to-video without editing a
ComfyUI graph, keep reusable presets, and look up or repeat every video you
made.

**Prerequisites.**
* LoRA files (`.safetensors`) on **gx10-02** under
  `/srv/models/video/loras/` (subfolders included; suggested:
  `wan22/paired`, `wan22/high_noise`, `wan22/low_noise`, `wan22/general`) or
  `/srv/models/shared/loras/`. Existing files can stay where they are.
* Wan 2.2 LoRAs usually come as two files, one for the high-noise and one for
  the low-noise expert; name them `<name>_high_noise` / `<name>_low_noise`
  (or put them in `high_noise/` and `low_noise/`) so they pair automatically.

**Steps.**
1. Open **Video** → **Text to Video** and write a prompt.
2. Under **LoRAs** press **Add LoRA**. Press **Rescan** if you just copied
   files. Search, filter and sort; **Details** shows each file's path on
   gx10-02, size, compatibility and high/low class. Press **Add**.
3. Set the **High noise** and **Low noise** strengths (0.0-1.5; 0.8 is the
   default for a pair). For a general LoRA choose **Applies to** (high, low or
   both). Reorder with **Move up / Move down**; switch one off with
   **Enabled**. With two or more LoRAs, **Use 0.5 for each** is offered as a
   starting point; your values are never changed unless you press it.
4. Optional: pick a **Preset** and press **Apply** (the form stays
   editable), or **Save as…** to store the current settings. **Manage**
   renames, duplicates and deletes presets.
5. Optional: **Advanced settings** (shift, CFG, steps, switch step, sampler,
   scheduler) and **Preview workflow**, which shows the high and low chains,
   the exact file names and strengths, the model files and the ComfyUI
   workflow JSON (copy or download).
6. Press **Generate video**.

**Expected result.** The job card shows queued → waiting (with the reason) →
generating → saving → complete. The video plays in the viewer and appears in
**Video history** with its LoRAs, strengths, seed and run time. **Details**
there shows the stored workflow, the ComfyUI prompt id, **Asset metadata** and
**Reuse in Creative Flows**; **Load settings into the form** and **Run
again** repeat it.

**Pairing by hand.** LoRA library → **Pair files by hand** → choose a
high-noise and a low-noise file → **Pair**. **Unpair** splits a pair. Pairs
are stored in the application database; files are never renamed.

**Errors.** Failures say what happened, for example "A selected LoRA file is
no longer on gx10-02" (rescan), "The high-noise file of a paired LoRA is
missing", "not compatible with Wan 2.2 T2V-A14B", "compatibility could not be
determined" (allow it in the library only if you trust the file), "ComfyUI
does not list a selected LoRA yet" (rescan), "gx10-02 ran out of memory"
(fewer LoRAs, shorter or smaller video) or "ComfyUI rejected the generated
workflow". The **Errors** section lists recent failures with technical
details. A queued or waiting job can be cancelled from its card.

**Mobile and accessibility.** The LoRA list, library and presets stack on
phones. Every slider, switch and move button has a name that includes the
LoRA; reordering works with the keyboard; the dialogs and history pass the
axe WCAG 2.2 AA checks.

**Offline and privacy.** Everything runs on the cluster. The browser receives
file names and paths on gx10-02, never keys. Exported workflow files contain
model file names only (no host paths, addresses or keys). LoRA files are only
read (their headers), never changed.

**Limits.** LoRAs apply to text-to-video only; at most 8 per expert; the
gateway `gx-video` API does not take LoRAs. More in the Control Center
**Docs → Video LoRAs** page.

**Library.** One Library holds every image, video and track. You can search
prompts, titles, lyrics and tags, filter by type, model and operation, and
sort. You can select, select all or clear, then run a bulk action:
favourite, delete (confirmed) or ZIP download. **Lineage** shows the parents
and children of an item. Nothing is ever deleted automatically.

**Mobile, accessibility, offline.**
* On phones the rail becomes a bottom bar and the forms stack.
* Every control has a label. The waveform has a keyboard slider, and the
  tabs use the arrow keys.
* Reduced motion is respected. The pages pass the axe WCAG 2.2 AA checks.
* The app needs gx10-01, not the internet.

**Privacy.**
* Files stay in `/srv/projects/gx-cluster/media` on gx10-01. Uploads are
  copied to gx10-02 only for processing.
* The browser never receives an API key or reaches gx10-02 directly.

## 8. Resource Control, Maintenance, and Storage & Cleanup (Control Center)

**Resource Control.**

* **Profiles** say what may run.
  * **Auto** (default) shares the nodes and unloads idle work when needed.
  * **Text** keeps gx-reason.
  * **Media** keeps image/video.
  * **Music** keeps gx-music.
  * **Max** acquires gx-max after a typed confirmation.
  * **Maintenance** stops new heavy work.
* **Resource map.** Shows what is loaded on each node, the available memory
  and the 30 GiB reserve.
* **Manual controls.** LOAD, UNLOAD, DRAIN, PIN and UNPIN go through the same
  admission checks as jobs, so a button never overcommits a node. A refusal
  says why and what would make room.
* **Compatibility table.** Computed from live numbers; it shows which
  runtimes can share a node.
* **The 30 GiB reserve** applies to images, video and music as well. A job
  starts only if the node still has 30 GiB available after the job's own
  memory, counting what another load in progress has not taken yet. Waiting
  jobs show the needed and available memory, the blocker and what happens
  next.

**Maintenance.**

1. Turn it on under **Resource Control** (it asks for confirmation).
2. Running work finishes. New model loads, media jobs and music jobs wait
   with the reason "Maintenance mode is on".
3. Turn it off to resume them.

**Storage & Cleanup.**

1. Choose a node and press **Scan**.
2. Items are marked:
   * **SAFE**: caches and dangling images;
   * **REVIEW**: models not bound to any alias, older checkpoints;
   * **PROTECTED**: anything in use, bound, mounted or written to. It is
     never deletable.
3. Tick SAFE items and press **Clean**. Every item is re-checked on the node
   just before deletion.
4. REVIEW items additionally need Maintenance mode and a typed confirmation.

The page never runs `docker system prune -a`. Health is shown as HEALTHY,
WATCH (< 150 GiB or ≥ 85 %), LOW (< 75 GiB or ≥ 92 %) or CRITICAL (< 30 GiB
or ≥ 97 %).

**Privacy and safety.**
* Item ids are opaque, so the browser cannot name arbitrary paths.
* All deletions are audited.

## 9. Connecting clients (Control Center → Setup)

* **Kilo Code.**
  * Settings → Providers → Custom provider → Connect.
  * Base URL `http://100.105.214.61:4000/v1`, your key, model **gx-auto**
    (recommended).
  * The page also shows a copyable `~/.config/kilo/kilo.jsonc` using
    `{env:GX_API_KEY}`.
  * **Test connection** runs a real Kilo-shaped request and shows the tier it
    was routed to.
* **Open WebUI.**
  * User menu → **Admin Panel** → **Settings** → **Admin → AI → Connections**
    → **OpenAI API** → **+** (Add Connection).
  * Fields: Connection Type External, URL `http://100.105.214.61:4000/v1`,
    Auth Bearer with your key in **API Key**, API Type Chat Completions.
  * Press **Verify Connection**, then **Save**.
  * **Model identity:** the same tab shows whether the Open WebUI model entries
    of the aliases match the model registry. **Sync identity from the
    registry** fixes any that do not.
    * **Purpose:** asked "what model are you?", gx-mini answers with its
      alias and its real model
      (`HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive`, from
      Qwen/Qwen3.5-4B) instead of inventing one.
    * **What it touches:** only the gx-* model entries; chats, users and
      connections are never changed. Model Manager re-syncs automatically
      after an assignment.
    * **Privacy:** the prompts contain only public model facts.
* **Other OpenAI clients.** The page shows curl, Python and JavaScript
  examples with `YOUR_GX_API_KEY`.

## 10. Deploying a change to either web app (operators)

**Purpose.** To make sure the browser is really being served the checkout. Both
apps cache their static files in memory. They now re-read a file when it
changes on disk, but the Python package is still loaded once per process, and
Creative Flows ships a bundle that has to be built — so "I edited the source"
and "the site changed" are two different statements, and only this script
settles which one happened.

**Prerequisites.** You are on gx10-01. Nothing else is deploying (the script
takes `state/build-v3/restart.lock` itself).

**Steps.**

```bash
legenex/playground/scripts/deploy.sh          # build if needed, restart if needed, verify
legenex/playground/scripts/deploy.sh --verify # verify only; change nothing
legenex/playground/scripts/deploy.sh --force  # restart even when nothing looks stale

legenex/control-ui/scripts/deploy.sh          # the same for the Control Center
```

**Expected result.** The last two lines are:

```
43 files served match the checkout; 0 stale, 0 not served

DEPLOY OK — the browser is being served this checkout
```

**Error handling.**

* `N stale` — the running server is answering with an older copy of a file.
  Run without `--verify` so it restarts.
* `HTTP 404 (on disk but not served)` — the file exists in the checkout but the
  server will not serve it. Check that the path has no leading dot and is
  inside `web/`.
* `could not take ... restart.lock` — another deploy is running. Wait.
* `the Creative Flows bundle did not build, but web/js/pages/flows.js ships it`
  — run `cd legenex/playground/flows-ui && npm run build` and read its error.

**Note on the Control Center.** Its `deploy.sh` also **applies any pending
database migration**, writing `library.pre-<name>.db` next to `library.db`
first, and prints the applied list afterwards. Never edit a migration file that
has already been applied — add a new one.

**Why this exists.** The deployed Playground once served a seven-hour-old copy
of itself while the source on disk was current: 21 files were stale and 11 —
including the whole Voice, Models, Logs and Settings pages — returned 404
because they had been created after the service started. Every symptom read as
"the UI is broken" rather than "the UI is not deployed". See B-031 and D-041.

