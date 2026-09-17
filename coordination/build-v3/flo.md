# FLO — Creative Flows (workstream status)

Owner: FLO specialist. Status: **in progress** (started 2026-09-17 16:40 SAST).

## What FLO consumes from other workstreams (please keep these stable)

FLO never re-implements another service. Flow nodes call the services
through the objects attached to `App`:

| WS | FLO calls | Used by nodes |
|---|---|---|
| core | `app.media.submit(body, user=..., ip=...)`, `app.media.get(id)`, `app.media.cancel(id, user=...)` | Generate Image, Image Edit, Image-to-Image, Character Reference, Generate/Text/Image-to-Video, Extend Video |
| IMG | the `image_model` field in the media job body (VisionmasterPro_V3 selector) and the list of image models (read from `app.manager.registry()["aliases"]["gx-image"]["variants"]` or IMG's documented listing) | Generate Image |
| WAN | the video job body field that references a saved LoRA preset **by id**, and a read-only preset listing | Wan LoRA node, video nodes |
| MUS | `app.music.submit(operation, body, user=..., via="flow")`, `app.music.get(job_id)`, `app.music.cancel(...)` (structured request fields as documented by MUS) | Music nodes, Ambient Sound |
| VOI | `app.voice` (interface as documented in `voi.md`) | Voice nodes |

FLO only tags provenance (`flow_id`, `flow_run_id`, `flow_node_id`) on the
Library rows those services create, and only where the columns are NULL.

## Hooks FLO adds to shared files (small, one block each)

* `gx_control_ui/server.py`: `App.__init__` attaches `self.flows`; the module
  import `routes_flo`; PUT/DELETE and body handling for `/v1/flows*`,
  `/v1/flow-runs/*`, `/v1/assets/*`.
* `gx_control_ui/routes_v2.py`: `_key_identity` also returns the key's
  `models` list (FLO authorises flow runs per alias).
* `legenex/playground`: routes.js line, nav `<li>`, `SPA_ROUTE`, `ALLOW`,
  PUT forwarding for `/v1/flows/*`, build-check page list and the separate
  bundle check.
* `legenex/control-ui/scripts/build-check.mjs`: `GX_BUILD_EXCLUDE` (skip the
  generated React bundle directory; it is validated by its own check).
* `legenex/control-ui/e2e/fixture_server.py`: one call into
  `e2e/flows_fixture.py` (stub services for the offline flow tests).

Progress, evidence and blockers follow below as they land.

## Progress log

- 17:10 backend landed: `gx_control_ui/flows/` (catalogue 67 node types, schema, graph, hashing, store,
  services, nodes, engine, ai, templates, wiring), `routes_flo.py`, migration `030_flows.sql`,
  hooks in `server.py` (App block, PUT/DELETE + 1 MiB body for `/v1/flows`, import line).
  Public API registered with the lead's `public_api()` hook (`/v1/flows`, `/v1/flow-runs`, `/v1/assets`).
  Tests: `tests/test_flows.py` (44), `tests/test_flows_engine.py` (14) pass; ruff + mypy clean for FLO files.
- Integrates IMG (`app.image_catalog`, `image_model`, per-model sizes/qualities, `edit_mode`),
  WAN (`app.wan.preset()`, `resolve_preset()` + `generate()`, t2v only), VOI (`app.voice` submit/get/cancel,
  `create_voice`, `list_voices`, `dialogue` segments, `flow` provenance, `auto_save`), MUS (`app.music.submit`
  with `description` / `prompt` / `lyrics` / `instrumental`; `lyrics_source=planner` only when MUS accepts it).
- Not FLO's (reported, not changed): ruff findings in call_agents.py, call_intake.py, footprints.py,
  music_ai.py, voice.py, tests/test_music_reference.py; mypy finding in routes_plt.py:49.
