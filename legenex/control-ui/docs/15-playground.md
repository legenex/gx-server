# GX-Playground

GX-Playground is the creative application: `http://100.105.214.61:8090/`
(Tailscale) or `http://127.0.0.1:8090/` on gx10-01.

* **Sign-in.** It shares the Control Center sign-in: one session covers both.
* **Separate roles.** The Control Center stays the admin tool: resources,
  storage, models, keys and setup.
* **Boot.** `gx-playground.service` starts at boot. It never loads a model:
  models load only when a job needs them, through the normal queues and
  Resource Control.

## Pages

| Page | What it does |
|---|---|
| Dashboard | quick create (image, video, music); status of gx-image, gx-video and gx-music; a simple resource widget (profile, status, queue); active jobs with their waiting reason; recent creations; recent errors; capacity |
| Images | generate, edit, variation, re-prompt, reuse settings or seed, compare, history rail, fullscreen, download, favourite, rename, delete, Make Video |
| Video | text-to-video, image-to-video, video edit, variation / re-prompt, player, history, download, favourite, rename, delete |
| Music | the ACE-Step studio (see "gx-music") |
| Library | one Library for images, video and music: grid/list, search, filters, sort, preview/play, open in editor, rename, favourite, duplicate, download, lineage, select / select all / clear, bulk favourite / unfavourite / delete / ZIP |
| History | every job with its phase, waiting reason, time, result or error |

## Resource widget

* **What it shows.** The active profile (Auto, Text, Media, Music, Max) and a
  short status line for Text, Image, Video, Music and Max.
* **Profile changes.** You can switch between those five profiles. A switch
  that drains work asks first; Max asks you to type `gx-max`.
* **Maintenance.** Only in the Control Center (**Open Advanced Resource
  Controls**).

## Architecture

* **One backend.** The Playground serves its own page and forwards an
  allow-listed set of API calls to the Control Center backend on the same
  host. There is one Library, one job queue and one session store.
* **Proxy trust.** The proxy adds a shared local token and the real client
  address, so login throttling and the audit log see the real client.
* **No direct access.** The browser never reaches gx10-02, the LiteLLM master
  key, Docker or a shell.

| Item | Where |
|---|---|
| Code | `legenex/playground/` |
| Unit | `~/.config/systemd/user/gx-playground.service` |
| Log | `/srv/logs/gx-playground/playground.log` |
| Health | `curl -s http://127.0.0.1:8090/pg/health` |
| Library files | `/srv/projects/gx-cluster/media` |
| Install / repair | `legenex/playground/scripts/install.sh` |
