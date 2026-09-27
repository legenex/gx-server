# Worker F report — Control Center dashboard FRONTEND rebuild (DeepSeek V4.1)

Status: **COMPLETE.** All 21 JS modules pass `node --check`; the new hermetic
Playwright suite (`offline.v41.spec.js`) passes 6/6 against real backend code;
the backend suite stays at 204/204 OK with the rebuilt `web/` tree.

## Scope & ownership

Owned files (all created/replaced): `legenex/control-ui/web/**` —
`index.html`, `css/app.css`, `js/{app,api,dom,chart}.js`,
`js/pages/{overview,model,performance,requests,agents,tasks,projects,files,
storage,logs,network,updates,recovery,jobs,keys,settings,common}.js`.

Deleted (retired pre-V4.1 pages; old world preserved in git tag
`pre-deepseek-v41-rebuild-20260927`): `dashboard.js`, `models.js`,
`cluster.js`, `runtime.js`, `manager.js`, `playground.js`, `creative.js`,
`backup.js`, `setup.js`, `docs.js`, `resources.js` (old page).
No `gx_control_ui/**` file was touched.

Added in `control-ui/e2e/` (ADD-only as instructed):
`fixture_server_v41.py`, `offline.v41.spec.js`, `playwright.v41.config.js`.

## Nav + router

Nav entries (index.html) + PAGES map (app.js), default route `#/overview`:
Overview, Model, Performance, Requests, Agents, Tasks, Projects, Files,
Storage, Logs, Network, Updates, Settings, Recovery, Jobs / Actions, API Keys.
Login/logout/session/CSRF handling is unchanged (auth endpoints untouched).
Old page hashes now fall back to Overview instead of rendering retired pages.

## Page → endpoint map (every endpoint cross-checked against server.py routes AND live-smoked against the fixture)

| Page | Endpoints used |
|---|---|
| overview.js | `GET /api/overview`, `GET /api/models`, `GET /api/actions`; actions via `POST /api/actions/gxmax_{start,restart,stop,drain}` (args `profile`, typed confirm `gx-max`); SSE `GET /api/stream` (queue/lifecycle → topbar refresh; polling fallback) |
| model.js | `GET /api/models` (cards, aliases, registry profiles/reasoning), `GET /api/resources` (serving profile, sizing, maintenance), `GET /api/actions` (gxmax specs); switch profile → `POST /api/actions/gxmax_{start,restart}` with `{profile, confirm:'gx-max'}` |
| performance.js | `GET /api/requests?since=&limit=500` (history records → TTFT/tps/wait/token aggregates + SVG sparkline/hbars; memory low-water marks honestly "unavailable: not exposed by an API yet") |
| requests.js | `GET /api/requests?project=&state=&profile=&since=&limit=`, `POST /api/requests/{id}/cancel`, `POST /api/requests/{id}/retry` |
| agents.js | `GET /api/agents` (coarse states, scheduler attribution, `supported_controls: []` → NO pause/resume/cancel rendered; NOT CONNECTED banner when `agentos.connected` is false) |
| tasks.js | `GET /api/agents/tasks` (kanban cards grouped by status, scheduler records; flat — no dependency graph) |
| projects.js | `GET /api/projects`, `POST /api/projects/{name}/size` |
| files.js | `GET /api/files/{roots,browse,search,dirsize,preview,download,trash}`; `POST /api/files/{upload,rename,move,mkdir,delete,trash/restore,trash/purge}` (upload = raw stream + `X-Path`/`X-Filename` headers; delete = "Move to trash"; purge needs typed `PURGE TRASH`) |
| storage.js | `GET /api/storage`, `POST /api/storage/scan` (analysis only; cleanup note links to Files→trash; no delete button) |
| logs.js | unchanged module against `GET /api/logs`, `GET /api/logs/{id}?lines=&q=` (new config-driven V4.1 stream list), text download |
| network.js | `GET /api/network` (rails, GIDs, counters, NCCL_IB_GID_INDEX pin, rank containers), `GET /api/network/diagnostics` (read-only runner) |
| updates.js | `GET /api/updates` (pins vs live, drift), `POST /api/updates/check` (upstream check; NO update button anywhere) |
| recovery.js | `GET /api/recovery` (watchdog incidents/backoff, memory events, lifecycle timeline; honest "no incidents file yet") |
| jobs.js | `GET /api/jobs`, `GET /api/actions/jobs/{id}` (phases rendered from actual orchestrator events, not a hardcoded stepper) |
| keys.js | `GET /api/keys`, `POST /api/keys`, `POST /api/keys/{id}/revoke|replace` (confirm true), `POST /api/keys/test`; honest `unavailable` banner when LiteLLM is down; aliases gx-max/gx-auto ONLY |
| settings.js | unchanged module against `GET /api/system` (system.*/infra.* audited actions, units, timers, endpoints, secrets hygiene) |
| login | `GET /api/session`, `POST /api/login`, `POST /api/logout` (unchanged conventions) |

Shared additions: `api.js` gained `openStream()` (SSE `/api/stream` with
graceful polling fallback — pages treat it as pure enhancement) and
`uploadFile()` (streamed `/api/files/upload`); `runAction()` now passes
declared action args. `js/chart.js` is a new dependency-free SVG chart util
(sparkline + horizontal bars, createElementNS only). CSS additions are
appended to `app.css` (kanban, timeline, charts, crumbs, files layout); the
existing conventions/components are reused everywhere else.

## What was verified

* `node --check` on all 21 modules: OK.
* **Live smoke against a real running backend**: `e2e/fixture_server_v41.py`
  (new; real `gx_control_ui` server code + schema-2 registry fixture + stub
  orchestrator/LiteLLM/AgentOS) — every page endpoint answered 200 with the
  documented shapes (see the smoke transcript evidence in the session log).
* **Browser e2e**: `npx playwright test -c e2e/playwright.v41.config.js` —
  6 passed: login gate, all 16 pages render with zero console noise
  (no fabricated data, no failed requests), Overview (4 model tiles =
  2 packs + 2 aliases, uncensored/production badges, queue counts from the
  scheduler stub, both fabric rails, 4 lifecycle actions), Model (registry
  sources, 3 profile buttons with effective values, reasoning ladder, typed
  "gx-max" confirmation), Requests (history rows, verbatim privacy note,
  cancel/retry wiring, state filter), Agents/Tasks (coarse states only,
  zero Pause/Resume/Cancel buttons, 3 kanban cards). axe (wcag 2.2 AA tags)
  serious/critical = 0 on Overview.
* Backend regression: `python3 -m unittest discover -s tests` → 204 OK.
* Endpoint cross-check: every literal/templated path in `web/js/**` was
  matched against `server.py` `@route` patterns — no JS references a retired
  (410) or non-existent route. Table above.

## Honest-data rules applied

* Every "unavailable" is the backend's `reason` verbatim ("unavailable: …").
* Agents page: `supported_controls` is empty upstream → no agent controls
  rendered, explicit note; NOT CONNECTED banner when the adapter says so.
* Performance: memory low-water marks are NOT invented — the page states they
  are not exposed by an API yet.
* Requests/Performance: privacy note from the backend is shown; no prompt
  content can appear (server allow-list).
* Updates: check-only; there is no update/apply button anywhere.
* Storage: analysis-only; the only deletion path is Files → trash → purge
  (typed `PURGE TRASH`).

## Deferred / notes for other workers

* **Legacy e2e files are stale** (`fixture_server.py`, `offline.ui.spec.js`,
  `live.*.spec.js`, helpers.js `PAGES`/login assert the old 11-alias world)
  and will fail against the rebuilt stack. I could only ADD files in e2e/, so
  the legacy config/specs are untouched; the V4.1 suite runs via its own
  config. Follow-up (owner: whoever owns e2e upkeep): retire/rewrite the
  legacy files, point the main `playwright.config.js` webServer at
  `fixture_server_v41.py`, and update `helpers.js` `login()` (it hardcodes
  the old default page title 'Dashboard').
* The backend keeps `/api/docs`, `/api/backup/*`, `/api/setup`,
  `/api/connections` (kept by Worker C) that no page consumes now; if a
  Docs/Connections page returns, it should be built on the refreshed V4.1
  content, not the pre-V4.1 doc set in `control-ui/docs/` (still stale).
* Worker A note honoured: `POST /lifecycle/gx-max/drain` treated as success;
  scheduler cancel/retry bodies are `{"id": "<request_id>"}` (relay is
  server-side anyway).
* `benchmark_run` exists as an action (Jobs page can run it) but bench
  results have no read API — surfaced honestly on Performance.
