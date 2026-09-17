# MUS — music UX, style tags, vocals, Build with AI, reference analysis (Build V3, workstream MUS)

Owner: music specialist. Status: implemented on gx10-01 (code + tests); live GPU
generation acceptance pending node-2 availability (see "Blocked / not yet proven").

This file was reconstructed by the lead on 2026-09-17 from the code and the
evidence under `/srv/logs/acceptance/build-v3/mus/`, after the workstream's own
log was found missing while the code was already committed. Statements below
are limited to what the code and recorded evidence show.

## Scope (from BUILD_V3.md)

Music UX order, style tags, vocals fix, Build with AI, reference analysis.

## What was built

| Layer | Files |
|---|---|
| gx-music service (gx10-02, port 18820) | `legenex/music/gx_music/` — `service.py` (conditioning preview, job plan, `check_vocals_before_render`), `validation.py`, `server.py` (route surface + `TAG_GROUPS`/`TagIndex`), `engine.py` (`create_sample`, engine calls), `store.py`, `config.py`, `audio.py`, `analysis_dsp.py` (measured tempo/key only), `errors.py` |
| Control Center (gx10-01) | `gx_control_ui/routes_mus.py` (conditioning preview + MUS routes), `music_ai.py` (Build with AI, Improve My Prompt, lyric writing, `apply_build`, `merge_improvement`, `_vocal_consistency`, validation/repair), `music_reference.py` (URL classification, oEmbed fetch through `netguard`, measured-summary presentation), one App block + import in `server.py:205` |
| Playground | `web/js/pages/music.js` (guided create order, section toolbar, style-tag picker, planner controls, remix/repaint/extend, locks/undo), referenced by `gx_playground/server.py:77` (ALLOW entries) |
| Music engine | ACE-Step 1.5 XL turbo `ACE-Step/acestep-v15-xl-turbo` + 5 Hz planner LM `ACE-Step/acestep-5Hz-lm-4B`, container `gx-music-engine:acestep15-ca1e85f-t214` (see `legenex/control-ui/docs/16-music.md`) |
| Tests | `legenex/music/tests/test_gx_music.py`, `legenex/music/tests/test_analysis_dsp.py`, `legenex/control-ui/tests/test_music_ai.py`, `test_music.py`, `test_music_reference.py`, `legenex/playground/e2e/offline.d-music.spec.js`, `offline.d-music-ai.spec.js`, `legenex/control-ui/e2e/music_stub.py` |
| QA gate | `legenex/music/qa.sh` (5 stages: byte-compile, shell syntax, unit/protocol tests, DSP tests, no-literal-credentials) |
| Docs | `legenex/control-ui/docs/16-music.md` |

## Design decisions (for DECISIONS.md)

* **Style tags are a curated vocabulary, not free text.** `server.py` defines
  `TAG_GROUPS` (genre, mood, instrument, vocal, production, tempo, era) whose
  entries are descriptors the ACE-Step caption format accepts; the full model
  vocabulary in `gx_music/genres_vocab.txt` is merged in and searchable through
  `TagIndex.search(q, limit)` with prefix matching. The picker therefore cannot
  emit a tag the model does not understand.
* **Vocals are validated before render, not after.** `check_vocals_before_render`
  is called on the render path (`service.py:450`) so an instrumental/vocal
  contradiction or a vocal-language mismatch fails fast instead of producing
  audio that ignores the request. `music_ai._vocal_consistency` applies the same
  rule to AI-proposed settings, and a locked field is never overwritten.
* **Build with AI replaces only unlocked fields** and preserves the seed
  (`music_ai.apply_build`); Improve My Prompt merges onto the current form
  (`merge_improvement`) and both are undoable in the Playground. Invalid AI
  output is repaired/validated server-side (`validate_settings`, `_repair`)
  rather than trusted.
* **Reference analysis claims only what is measured.** `analysis_dsp.py` is
  explicit that genre, instrumentation, vocals and mood are *not* claimed; it
  reports tempo, meter and key with the chroma profile used. Reference URLs are
  classified and fetched through `gx_control_ui/netguard.py`, never directly.

## Evidence (real, under `/srv/logs/acceptance/build-v3/mus/`)

* `dsp-selftest-and-eval.txt` — 5/5 DSP self-tests OK, then a 31-track eval:
  tempo exact 27/31, octave-tolerant 29/31, key exact 19/31; best log-chroma
  profiles `current` and `aarden` (19/31), `temperley` 11/31, `shaath` 16/31.
* `dsp-eval-summary.txt` — an earlier run of the same eval recorded tempo exact
  25/31, octave-tolerant 27/31, key exact 19/31. Both runs are kept; the newer
  run supersedes the older one.
* `dsp-eval-31-tracks.txt` — per-track lines (job id, tempo/library tempo/three
  octave candidates, library key, detected key, relative candidates, beats per
  bar, time signature, confidence).
* `vocal-bug-historic-jobs.json` — the historic job that motivated the vocals
  fix: `style_tags` contain `female vocals` while the request is not
  instrumental and `lyrics` is empty, with planner `thinking: true`. This is the
  recorded reproduction input for the pre-render vocal check.

## Measured footprints

* Engine memory: **24-27 GiB loaded** (documented in `docs/16-music.md`).
  Admission requires 32 GiB plus the 30 GiB reserve. It fits next to gx-reason
  and must never share a node with a cold video job.
* First-job load ~85-100 s; idle unload after 10 idle minutes.
* A 1 Hz `MemAvailable` measurement during the first load of this build has NOT
  been recorded here — see "Blocked / not yet proven".

## Blocked / not yet proven

* **Live end-to-end generation for this build is not yet re-run.** A media
  footprint probe currently has exclusive use of node 2, so the gx-music engine
  cannot be loaded. Everything above is code + hermetic/unit/DSP evidence.
  Smallest human action: none — rerun the music live acceptance when the node-2
  probe finishes.
* **Key detection accuracy is 19/31 (≈61%)** on the 31-track eval. This is a
  measured limitation of the DSP path, not a regression, and is why key is
  presented as a detected estimate with confidence rather than as ground truth.
* **Full `qa.sh` DSP stage** needs numpy/scipy or the engine image; on a host
  with neither it reports SKIPPED (by design) and must be run on gx10-02.

## Tests to run

1. `legenex/music/qa.sh` (hermetic; on gx10-02 for the DSP stage).
2. `python3 -m unittest tests.test_analysis_dsp` in `legenex/music`.
3. `(cd legenex/control-ui && npm run qa)` — covers `test_music_ai.py`,
   `test_music.py`, `test_music_reference.py`.
4. `(cd legenex/playground && npm run qa)` — covers the offline music specs.
5. Live: one real create + one real reference analysis against gx-music on
   node 2, with the result written back here as the acceptance record.
