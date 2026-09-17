# gx-music

`gx-music` is the eighth alias (D-036): music generation with ACE-Step 1.5 XL
on gx10-02.

| | |
|---|---|
| Music model (DiT) | `ACE-Step/acestep-v15-xl-turbo` @ `d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee` |
| Song planner (5 Hz LM) | `ACE-Step/acestep-5Hz-lm-4B` @ `0a3ec94b557aea7d508da38b31cfe7341f6ff737` |
| VAE and text encoder | `ACE-Step/Ace-Step1.5` @ `19671f406d603126926c1b7e2adc169acbcade22` |
| Runtime | `ace-step/ACE-Step-1.5` @ `ca1e85fe9430179831e6bc6be790c332190a3866`, image `gx-music-engine:acestep15-ca1e85f-t214` (torch 2.14 / cu130, the set proven on GB10) |
| Node | gx10-02. The light supervisor starts at boot; the engine loads with the first job (~85-100 s) and unloads after 10 idle minutes |
| Memory | 24-28 GiB loaded; admission needs 32 GiB plus the 30 GiB reserve. It fits next to gx-reason |

## What it can do

* **Input:** a prompt with style tags, or a natural-language description (the
  language model then writes the lyrics, tempo, key and length).
* **Lyrics:** structured lyrics (`[Intro]`, `[Verse]`, `[Chorus]`, …), or
  instrumental.
* **Musical controls:** vocal language, duration (10-600 s), BPM, key and
  time signature.
* **Generation controls:**
  * seed (a fixed seed with fixed settings gives identical audio);
  * batch of 1-4;
  * 1-20 inference steps;
  * the ODE or SDE sampler;
  * language-model planning, including its temperature, CFG and top-p;
  * prompt enhancement.
* **Working from audio:** a reference track, remix / cover, repaint (redo a
  time range) and extend.
* **Output:** WAV (32-bit float master), FLAC (24-bit) and MP3 (320 kbps), all
  48 kHz stereo.

**Not available** with the turbo model: extract, lego and complete (they need
ACE-Step XL base), and music-model guidance / CFG (turbo is distilled). The
Playground shows only the controls the installed model supports.

## Lifecycle

* **gx-max first.** gx-max always wins. Its drain sets a hold on gx10-02 and
  waits until the supervisor has unloaded the engine (container, ledger entry
  and processes all verified gone) before rank 1 starts. Music jobs submitted
  meanwhile wait with the reason "gx-max owns the cluster" and run after the
  release.
* **Maintenance.** No new loads; a running track finishes, then the engine
  unloads.
* **Sharing gx10-02 with ComfyUI.** When image or video weights hold the
  memory, the supervisor asks the media router to free them. This goes through
  the same path gx-reason uses, never ComfyUI directly. The router's record of
  what is loaded therefore stays true, and its next job is admitted with the
  full cold-start requirement.

## Music API

The API runs on GX-Playground. It needs a gateway key that allows `gx-music`:
create one in **API Keys** and tick gx-music. Each key sees only its own jobs.

```bash
export GX_API_KEY=YOUR_GX_API_KEY
curl -s http://100.105.214.61:8090/v1/music/generations \
  -H "Authorization: Bearer $GX_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt": "warm lo-fi beat", "style_tags": ["lo-fi", "chill"], "instrumental": true, "duration": 30}'
# -> {"id": "mus-…", "status": "queued", …}
curl -s http://100.105.214.61:8090/v1/music/mus-… -H "Authorization: Bearer $GX_API_KEY"
curl -s -o song.mp3 "http://100.105.214.61:8090/v1/music/mus-…/content?index=0&format=mp3" \
  -H "Authorization: Bearer $GX_API_KEY"
```

| Method | Path | |
|---|---|---|
| POST | `/v1/music/generations` | new track |
| POST | `/v1/music/remix` | cover of `source` (`{"job_id", "index"}` or `{"upload_id"}`), `strength` 0-1 |
| POST | `/v1/music/edits` | repaint `start`-`end` seconds of `source` |
| POST | `/v1/music/extend` | add `seconds` at the `end` or `start` |
| POST | `/v1/music/uploads` | raw audio body (WAV/FLAC/MP3/OGG/M4A, ≤ 64 MB), header `X-Filename` |
| GET | `/v1/music/{id}` | status and tracks |
| GET | `/v1/music/{id}/content?index=&format=` | WAV / FLAC / MP3 (409 while the job is not `completed`) |
| GET | `/v1/music/{id}/lineage` | parents and children |
| POST | `/v1/music/{id}/cancel` | cancel (a running render finishes and is discarded) |
| GET | `/v1/music/jobs`, `/v1/music/model`, `/v1/music/tags` | list, capabilities, tag suggestions |

**Job status:**

* `queued` → `waiting_for_resource` → `loading_model` → `generating` →
  `saving` → `completed` (or `failed` / `cancelled`).
* `completed` is reported only after the tracks are saved to the Library, so
  every format can be downloaded at that point.
* Poll every 2-5 s.
* A key revoked or replaced in the Control Center stops working immediately.
  A key revoked elsewhere stops within 15 s.

Load and unload are not part of the API (403). Use **Resource Control**
instead.
