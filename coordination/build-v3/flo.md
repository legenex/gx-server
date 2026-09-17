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

---

## 21:30 — frontend landed (second FLO session)

### What was verified, not assumed

* **Backend claim re-checked.** `cd legenex/control-ui && .venv/bin/python -m unittest
  discover -s tests -p 'test_flows*.py' -v` → **58 tests, OK** (`tests.test_flows` 44 +
  `tests.test_flows_engine` 14). Note: `python -m unittest tests.test_flows` fails with
  `ModuleNotFoundError: support` — the suite must be run with `discover -s tests`
  (that is what `npm run test:unit` does).
* **Frontend claim re-checked and it was true:** `web/flows/` did not exist, there was no
  `web/js/pages/flows.js`, and `flows-ui` had **no entry point** (no `main.tsx`), no app
  shell, no stylesheet and no `scripts/check-fresh.mjs` even though `package.json`
  referenced it. `npm run lint` had never passed: 30 errors across 8 files.

### What this session wrote (all inside FLO's ownership)

| File | Purpose |
|---|---|
| `flows-ui/src/main.tsx` | entry point; exports `mount(container, host)`, imports the two stylesheets |
| `flows-ui/src/App.tsx` | the missing application: flow browser + editor shell (toolbar, palette, canvas/outline, inspector, all dialogs, autosave wiring, run polling, keyboard shortcuts, conflict/offline/draft recovery) |
| `flows-ui/src/styles.css` | the `gxf-*` design layer (the Playground tokens are inherited; no inline styles — CSP) |
| `flows-ui/src/env.d.ts` | Vite ambient types for the CSS side-effect imports |
| `flows-ui/scripts/check-fresh.mjs` | the separate bundle check: manifest integrity, no source maps/HTML, no eval/`new Function`/`document.write`, no external origin **loads**, no credentials, size budget, and a source fingerprint so a stale committed bundle fails QA |
| `web/js/pages/flows.js` | the vanilla page wrapper: reads `/flows/manifest.json`, injects the hashed stylesheet, dynamically imports the hashed entry, and hands the island the host (request/toast/pickAsset/upload/showAsset/confirm/setQuery) |
| `web/flows/**` | the committed build output |
| `e2e/offline.h-flows.spec.js` | 9 offline browser tests |
| `control-ui/docs/20-creative-flows.md` | the in-UI docs page |

Fixes to files that already existed (also FLO's): `package.json` build script now stamps
the bundle; `eslint.config.js` lints `scripts/*.mjs` without type information;
`Canvas.tsx` `onConnectEnd` widened (xyflow types the refused branch as fully populated,
but a drop on empty canvas really does leave it null); `Inspector.tsx`, `fields.tsx`,
`preview.tsx` keyed their fetched/derived state instead of resetting it inside an effect
(`react-hooks/set-state-in-effect`); `ui.tsx` no longer writes a ref during render;
`store.ts` uses `Reflect.deleteProperty`; `autosave.ts` validates the parsed draft;
`api.ts`/`Dialogs.tsx`/`test/model.test.ts` small type cleanups. `TemplatesDialog`'s
`onSaveCurrent` became optional (the flow browser has no current flow). Nodes added from
the library are now placed to the right of the flow instead of on top of each other.

### QA results (all run, not assumed)

```
legenex/control-ui:  .venv/bin/python -m unittest discover -s tests -p 'test_flows*.py'
                     → Ran 58 tests … OK
                     .venv/bin/python -m unittest discover -s tests -p 'test_docs.py'
                     → Ran 9 tests … OK   (the new docs page renders)
legenex/playground/flows-ui:  npm run qa
                     → tsc clean · eslint 0 problems · vitest 13/13 passed
                     → check:fresh: flows bundle OK: 3 files, 511.9 KiB (budget 1600 KiB)
                     npm run build → 184 modules, assets/flows-<hash>.js 491.8 kB,
                                     assets/style-<hash>.css 32.0 kB
build-check (with the exclusion the lead already added):
  GX_BUILD_ROOT=…/playground/web GX_BUILD_PAGES='dashboard,flows,images,…' \
  GX_BUILD_NAV_IN_HTML=0 GX_BUILD_BUDGET_KB=600 GX_BUILD_EXCLUDE=flows \
  node ../control-ui/scripts/build-check.mjs
  → build check OK: 36 modules, 39 files, 580.6 KiB total (budget 600 KiB)
```

**Budget warning for the lead:** with `flows` excluded the hand-written frontend is at
**580.6 / 600 KiB (97 %)**. `web/js/pages/flows.js` cost 3 KiB. The next page will need
`GX_BUILD_BUDGET_KB` raised.

### Offline browser tests — 9/9 passing

`legenex/playground/e2e/offline.h-flows.spec.js`, run against the real fixture backend
behind the real Playground proxy:

1. creates a flow and keeps it in the URL
2. adds nodes, connects compatible ports and refuses incompatible ones
3. refuses an incompatible connection dragged on the canvas
   (real drag on the xyflow handles → *“Prompt accepts text, not audio. Add a conversion
   node in between.”*, and the edge is not created)
4. configures a node, autosaves and survives a reload
5. **runs a flow and shows the result on the node** (text → Generate Image; the node
   reaches `data-status="succeeded"`, its image preview renders, the status bar shows
   *Done*, and the run history lists `gx-image`)
6. creates a flow from a built-in template
7. outline view lists nodes in execution order with their ports (+ `axeCheck`,
   WCAG 2.2 AA clean, no horizontal overflow)
8. reports backend failures instead of showing an empty page
9. works at phone width (the library becomes a closable bottom sheet)

**How they were run, and why that matters.** The three shared files below are not FLO's,
so the repo checkout still has no `flows` route. The suite was therefore executed against
a throw-away copy of `legenex/playground` in the session scratchpad with exactly the
integration lines in the next section applied, symlinked back to the real `control-ui`
and `common`:

```
GX_E2E_BACKEND_PORT=18691 GX_E2E_PORT=18693 npx playwright test --project=offline offline.h-flows
  → 9 passed
```

Once the lead applies those lines to the repo the same command works from
`legenex/playground` with no scratch copy. **Nothing outside FLO's ownership was edited
in the checkout.**

No `e2e/flows_fixture.py` was needed after all: the fixture server builds the real
`App`, so `app.flows` and all its services already work against the existing stubs
(catalogue, templates, CRUD, versions and a real run all exercised above). The
`fixture_server.py` hook listed at the top of this file is **withdrawn** — the lead has
nothing to add there.

## Integration requests for the lead

Five edits, all additive. Copy-paste ready.

**1. `legenex/playground/web/js/routes.js`** — one line, after `dashboard`:

```js
  flows: () => import('./pages/flows.js'),
```

**2. `legenex/playground/web/index.html`** — one `<li>` in `<ul id="nav-create">`,
between Dashboard and Images (the `flow` icon already exists in `web/js/icons.js`):

```html
          <li><a class="rail-link" href="#/flows" data-page="flows"><span class="rail-ic" data-icon="flow"></span><span class="rail-label">Creative Flows</span></a></li>
```

**3. `legenex/playground/scripts/build-check.mjs`** — add `flows` to the page list
(`GX_BUILD_EXCLUDE = 'flows'` is already there, thank you):

```js
process.env.GX_BUILD_PAGES = 'dashboard,flows,images,video,music,voice,library,history,models,logs,settings';
```

**4. `legenex/playground/gx_playground/server.py`, `SPA_ROUTE`** — add `flows`:

```python
SPA_ROUTE = re.compile(r"/(dashboard|flows|images|video|music|voice|library|history|models|logs|settings)"
                       r"(/[a-z0-9_\-]{0,64}){0,2}")
```

**5. `legenex/playground/gx_playground/server.py`, `ALLOW`** — this block (verified
against every path `web/js/pages/flows.js` and the island actually call; the browser
island uses **GET/POST only**, so no PUT/DELETE is needed here — the PUT/DELETE hook
mentioned at the top of this file is for the **public** `/v1/flows*` API, which the
Playground does not proxy):

```python
        # Build V3 FLO: Creative Flows (the browser island uses GET/POST only)
        ("GET,POST", r"/api/flows"),
        ("GET", r"/api/flows/(catalog|options)"),
        ("POST", r"/api/flows/(validate|ai/generate)"),
        ("GET,POST", r"/api/flows/(templates|secrets)"),
        ("GET", r"/api/flows/templates/(tpl_[0-9a-f]{24}|builtin_[a-z0-9_]{2,40})"),
        ("POST", r"/api/flows/templates/(tpl_[0-9a-f]{24}|builtin_[a-z0-9_]{2,40})/(duplicate|delete)"),
        ("POST", r"/api/flows/secrets/[A-Za-z][A-Za-z0-9_\-]{0,63}/delete"),
        ("GET,POST", r"/api/flows/flow_[0-9a-f]{24}"),
        ("POST", r"/api/flows/flow_[0-9a-f]{24}/(delete|duplicate|run)"),
        ("GET", r"/api/flows/flow_[0-9a-f]{24}/(versions|runs)"),
        ("GET", r"/api/flows/flow_[0-9a-f]{24}/versions/[0-9]{1,7}"),
        ("POST", r"/api/flows/flow_[0-9a-f]{24}/versions/[0-9]{1,7}/restore"),
        ("GET", r"/api/flow-runs"),
        ("GET", r"/api/flow-runs/frun_[0-9a-f]{24}"),
        ("POST", r"/api/flow-runs/frun_[0-9a-f]{24}/cancel"),
        ("GET", r"/api/flow-runs/frun_[0-9a-f]{24}/nodes/[A-Za-z0-9_\-]{1,40}"),
        ("POST", r"/api/flow-runs/frun_[0-9a-f]{24}/nodes/[A-Za-z0-9_\-]{1,40}/cancel"),
```

`/api/media/assets/<id>` and `/api/media/upload` (asset previews, the Library picker and
uploads inside a flow) are already allowed by the existing media rule — no change.

**6. Restart.** After those edits, `gx-playground.service` needs a restart
(BUILD_V3 rule 9: it is the lead's, not FLO's). `gx-control-ui.service` also needs one if
it has not been restarted since `routes_flo.py` landed. Nothing else has to be rebuilt:
`web/flows/` is committed.

**Do not** add a `/flows` entry to `web/index.html`'s asset references or to the CSP —
the bundle is same-origin under `/flows/…` and `script-src 'self'` already covers the
dynamic `import()`. The island loads no external font, script, style or image; the
`check:fresh` script fails the build if that ever changes.

## What live GPU acceptance still needs (scheduled by the lead)

Everything above ran without touching a GPU. The evidence still missing is a real run on
the cluster; suggested minimum, to `/srv/logs/acceptance/build-v3/flo/`:

1. The five shared-file edits applied and `gx-playground` + `gx-control-ui` restarted.
2. Open `http://127.0.0.1:8090/#/flows` signed in, create a flow from the built-in
   template that only needs **gx-image** (one image node), press **Run flow**, and keep
   the run-history row: node status, duration, model, asset id.
3. A second run of the same flow **without changing anything** to prove the cache path
   (`reused from cache`, no new job on the media router).
4. One multi-service flow — image → `video.i2v` (Wan on gx10-02) → `voice.tts` →
   `compose.add_voice` → `compose.export` — to prove the queueing and the resource waits.
   This one loads Wan and gx-voice on node 2, so it must **not** overlap the lead's media
   memory measurement or a gx-max window, and `state/guard/node2.gxmax-hold` must be absent.
5. `gx-music` is the only service FLO calls that is a supervisor rather than a job API;
   one `music.instrumental` node is enough to prove it.

No new model, no new container and no new memory footprint belong to FLO: every node
calls a service another workstream already admitted through `resource-guard.sh`.

## Blockers

* **B-FLO-1 (needs the lead, 5 lines):** the page is unreachable in the repo checkout
  until the five edits above are applied — `routes.js`, `index.html`, `build-check.mjs`
  and the two `server.py` edits are all shared files. Everything else is done and proven
  in a scratch copy; no code change is needed on FLO's side.
* **Not FLO's, reported not fixed:** in the full offline suite run (scratch copy, real
  repo `web/`) `offline.d-music-ai.spec.js` fails two tests
  ("create form follows the conditioning order and tags are real tokens",
  "vocal requests never silently become instrumental"). That is MUS work in progress —
  `web/js/pages/music.js` and `web/css/app.css` are being edited right now. FLO did not
  touch them.

## API.md section for the lead to paste (FLO does not own `legenex/playground/API.md`)

Suggested position: after the Library section, before Music.

```markdown
## Creative Flows (Build V3 FLO)

Session routes, same conventions as everything else (cookie + `X-CSRF-Token`,
same origin, JSON). The browser island uses **GET and POST only**; `PUT` and
`DELETE` exist on the public `/v1/flows*` API, which the Playground does not
proxy.

| Method | Path | Body / result |
|---|---|---|
| GET | `/api/flows/catalog` | `{version, port_types, categories, nodes[]}` — the node types, their typed ports, fields and availability. The UI hard-codes no node type. Cached 30 s. |
| GET | `/api/flows/options` | `{image_models, image_sizes, edit_modes, lora_presets, voices, llm_models, errors}` — the dynamic choices for select fields. A source that failed appears in `errors`, and the nodes that need it are shown as unavailable. |
| GET | `/api/flows?q=` | `{flows: [{id, name, description, version, updated_at, node_count, node_types, last_run}]}` |
| POST | `/api/flows` | `{name?} \| {graph} \| {template_id} \| {asset_id}` → the new flow (201) |
| GET | `/api/flows/<flow_id>` | `{id, name, version, graph, readiness[], last_run}` |
| POST | `/api/flows/<flow_id>` | `{graph, version}` → the saved flow. `409 version_conflict` when someone else saved first; `422` with `detail.issues[]` when the graph breaks a rule. |
| POST | `/api/flows/<flow_id>/delete` | `{confirm: true}` |
| POST | `/api/flows/<flow_id>/duplicate` | `{}` → the copy (201) |
| GET | `/api/flows/<flow_id>/versions` | `{versions: [{version, name, created_at, author}]}` |
| POST | `/api/flows/<flow_id>/versions/<n>/restore` | `{}` → the flow |
| POST | `/api/flows/<flow_id>/run` | `{mode, node_id?, run_id?, version?}` → the run (202). `mode` is `full`, `node`, `from`, `downstream`, `regenerate` or `rerun_failed`. |
| GET | `/api/flows/<flow_id>/runs?limit=` | `{runs: [...]}` |
| GET | `/api/flow-runs/<run_id>` | the run with `nodes{}` — poll this while `status` is `queued` or `running` |
| POST | `/api/flow-runs/<run_id>/cancel` | `{}` |
| GET | `/api/flow-runs/<run_id>/nodes/<node_id>` | the node run plus `logs[]` and the exact `payload` that was sent |
| POST | `/api/flow-runs/<run_id>/nodes/<node_id>/cancel` | `{}` |
| GET/POST | `/api/flows/templates` | list / save the current flow as a template |
| POST | `/api/flows/templates/<id>/(duplicate\|delete)` | built-ins can be duplicated, not deleted |
| GET/POST | `/api/flows/secrets` | `{secrets: [{name, updated_at, length}]}` / `{name, value}`. Values are never returned. |
| POST | `/api/flows/secrets/<name>/delete` | `{}` |
| POST | `/api/flows/validate` | `{graph}` → `{readiness: [...]}` without saving |
| POST | `/api/flows/ai/generate` | `{prompt, model}` → `{graph, warnings[], model, model_used, attempts, readiness[], seconds}`. Rate limited to 10/min per user; the request can take minutes. |

Node run status values: `pending`, `queued`, `waiting`, `running`,
`succeeded`, `cached`, `reused`, `failed`, `cancelled`, `skipped`, `bypassed`,
`blocked`, `interrupted`. Run status: `queued`, `running`, `succeeded`,
`failed`, `cancelled`, `interrupted`.
```

## Final run of this session (after two real defects found by the tests)

Two defects the tests caught and this session fixed:

1. **`aria-prohibited-attr` (serious, WCAG 2.2 AA).** `@xyflow/react` renders a connection
   handle as a plain `<div>`, and `NodeCard.tsx` put an `aria-label` on it — prohibited on a
   generic role. The handle is now `aria-hidden="true"` with the description moved to its
   `title`; the port is named by the visible text next to it, and connecting without a
   pointer already goes through the Outline's Connect dialog. Found by `axeCheck` on the
   canvas view, which the spec now runs on all three views (browser, canvas, outline).
2. **The Playground QA gate would have failed on the bundle.** `scripts/qa.sh` step 1
   requires a final newline in every tracked file under `web/`, and Vite emits none.
   `npm run build` now appends it (and `npm run check:fresh` fails when it is missing), so
   `scripts/qa.sh` needs **no** change from the lead.

Also fixed while wiring the shell: leaving the page through the nav rail now flushes the
autosave before the editor unmounts (the local draft was the only safety net before).

```
legenex/playground/flows-ui: npm run qa
  → tsc clean · eslint 0 problems · vitest 13/13 · flows bundle OK: 3 files, 511.9 KiB
legenex/playground (scratch copy with the 5 integration lines):
  npx playwright test --project=offline offline.h-flows   → 9 passed (22.9 s)
  npx playwright test --project=offline                   → 46 passed, 8 failed (6.2 min)
```

The 8 failures in the full suite are **not FLO's** and were failing before this work as
well — MUS (`offline.d-music*.spec.js`, 6) and PLT/history
(`offline.f-history-resources.spec.js` History, `offline.g-platform.spec.js` Models) —
while `web/js/pages/music.js`, `models.js` and `web/css/app.css` are being edited by those
workstreams. All 9 flows tests pass inside the full-suite run too (45-53 in the log).

**Optional extra for the lead:** adding `['flows', 'Creative Flows']` to `PAGES` in
`legenex/playground/e2e/helpers.js` makes `offline.a-shell.spec.js` cover the flow browser
in its "every page renders without console errors, CSP violations or axe violations
(dark and light)" test. Verified safe: axe is clean on the flow browser, the canvas and
the outline in both views.
