# Worker C report — Control Center backend (DeepSeek V4.1 rebuild)

Status: **COMPLETE.** All 204 tests green (`cd legenex/control-ui && python3 -m
unittest discover -s tests`), every module compiles and imports, stdlib only,
no cluster access needed by the suite.

## Removals (git tag `pre-deepseek-v41-rebuild-20260927` keeps the old world)

* Modules deleted: `media_*`, `music*`, `voice/call/live/realtime`,
  `wan_video`, `image_catalog`, `owui_identity`, `playground`, `flows/`,
  `routes_{mus,voi,flo,cal,liv,img,wan,plt}`, `activity`, `obs`,
  `node2_services`, `catalog`, `footprints`, `netguard`, `model_manager`,
  `storage`, `storage_scan`, `routes_v2`, `migrations/` (+ ~28 retired test
  files).
* Aliases gone everywhere: `gx-mini`, `gx-code`, `gx-fast`, `gx-reason`,
  `gx-image`, `gx-video`, `gx-music`, `gx-voice`, `gx-call`, `gx-live`,
  `llama-swap`, `SGLang`, `ComfyUI` (hf.py now labels media repos "cannot be
  staged here" and never suggests a media alias or runtime).
* Retired API prefixes return **410 Gone** with
  `{"error":{"code":"gone","message":"retired with the media stacks ..."}}`:
  `/api/playground`, `/api/media`, `/api/music`, `/api/voice`, `/api/creative`,
  `/api/manager`, `/v1/music`, `/api/setup/openwebui/identity`, and the rest
  of `server.RETIRED_PREFIXES`.
* `hostfacts.USER_UNITS` no longer monitors `gx-playground.service` /
  `gx-music.service`.

## Config / fixture notes for the frontend worker

* Registry is schema 2 (`legenex/models/registry.json`): `nodes` (gx10-01
  head / gx10-02 worker, fabric rail1/rail2 IPs, hcas), `runtimes`
  (mia-dsv41 commit/image), `models` (stock + uncensored EXL3 packs,
  revisions, paths), `aliases` (gx-max, gx-auto ONLY), `profiles`
  (fast/balanced/swarm/deep/long/custom), `reasoning` ladder
  (none..max + numeric range).
* Orchestrator contract used: `GET /health/detailed`,
  `GET /scheduler/status`, `GET /scheduler/history`,
  `POST /scheduler/{cancel,retry}`, `GET /lifecycle/gx-max/status`,
  `GET /lifecycle/gx-max/events?limit=400`,
  `POST /lifecycle/gx-max/{acquire,release,drain}` — all with
  `Authorization: Bearer $GX_ORCHESTRATOR_API_KEY`. Bodies are relayed as-is.
* Offline / unreachable is always honest: `{"available": false, "reason":
  "..."}`; `/api/keys` degrades to `{"keys": [], "unavailable": true,
  "reason": ...}`; the SSE stream still ticks with `available:false` events.
* Uploads: only `/api/files/upload`, streamed, capped 512 MiB; every other
  body capped 512 KiB (`413` beyond). All POSTs need the session cookie +
  `X-CSRF-Token` + same-origin `Origin`/`Referer`.
* `X-Frame-Options` is now the valid token `DENY` (was `DENIED`).

## API surface (64 routes)

Auth model: `public` (health/ready/session/login) vs `session` (everything
below needs the admin cookie). Methods other than the listed ones → 405.

### Pages (GET, JSON)

| Route | Shape (keys) |
|---|---|
| `/api/overview` | `overall, nodes[], services[], rails[], tailscale, rdma_ok, gxmax{state,phase,detail,profile,last_error,waiters}, queue{available,queued,active,ui_running}, registry_ok, locks, ledger, git, problems[], cache_age` |
| `/api/nodes` | `node1, node2` (each: node_summary + `psi, load, temperature, containers, docker_stats, units, memory`), `services[], orchestrator, scheduler, gxmax, ledger` |
| `/api/cluster` | `explanation` (single-model, 2×128GB text), `nodes[], rails[], tailscale, management{ssh_node2,note}, gxmax, queue` |
| `/api/models` | `models[]` = registry cards (`state: available|loaded`, `state_detail`, `kind: model`) + alias rows `gx-max/gx-auto` (`kind: alias, state, state_detail, mode, description, model, runtime, uncensored`); `registry{schema, registry_ok, cluster, aliases{gx-max,gx-auto}, capabilities, runtimes, fabric[], nodes, models, profiles, reasoning}` |
| `/api/jobs` | `gxmax{state...}, gxmax_active_job, gxmax_history[], gxmax_events[] (redacted), queue, ui_jobs[]` |
| `/api/actions` | `actions[]` (name, label, description, danger, confirm_phrase, needs_confirm, advanced, args[]) |
| `/api/actions/jobs/{id}` | one job dict (`state: running|succeeded|failed, output[], result`) |
| `/api/requests` | `status{available,...}|{available:false,reason}`, `history{available, records[], count, filters, note}` |
| `/api/agents` | `agents[]` (coarse: name, state working|idle, requests{active,queued,done,error}), `agentos{connected,...}`, `supported_controls: []`, `notes[]` (no pause/resume/cancel exists) |
| `/api/agents/tasks` | `scheduler_records[]` (allow-listed fields), `kanban_cards[]`, `agentos_connected, note` |
| `/api/projects` | `root, projects[]` (name, path, mtime, is_git, branch, head, remote, dirty, dirty_files, last_commit, size_bytes, size_measured_at, scheduler{active,queued}|null, children[]) |
| `/api/storage` | `head{name,...}, worker{orchestrator_facts{available,...}, du{}, docker{}, largest[20], stale_cache[], duplicates[]}, cleanup_note` (analysis only; deletions go through Files→trash) |
| `/api/network` | `offline, rails[]` (head_ip, worker_ip, hca, interface, ethtool{speed_mbps,link}, counters, gids[], nccl_gid_index_pin, gids_pinned, ping_worker, ok, level), `rank_containers[], diagnostics_note` |
| `/api/network/diagnostics` | `available, pairs[] (both-direction pings), ibv_devinfo` (offline: `{available:false,reason,pairs:[]}`) |
| `/api/updates` | `registry_ok, pins[]{kind: runtime_commit|image_digest|model_revision, name, pin, live, match, detail}, drift[], mia_remote, policy` |
| `/api/recovery` | `gxmax, lifecycle_events, lifecycle_history, hostwatch_tail, memory_events, watchdog{available, incidents[], restart_attempts, backoff_state, reason}` |
| `/api/resources` | `profile, profiles, serving_profiles, maintenance, gxmax{sizing{rank_gib:105,...}, admission...}, nodes, queue, pins, reserve_gib` |
| `/api/resources/profile/plan` (GET ?profile=) | planned guard-file diff + explanation |
| `/api/logs` | `streams[]` (id, label, node, kind: file|glob|docker|journal, target redacted) |
| `/api/logs/{id}?lines=&q=` | `{lines[], count, query, error}` (node-2 streams via fixed ssh `tail -n N -- <path>`) |
| `/api/system` | versions, repo, git, kernel pin, endpoints, units (retired units filtered), timers, runtime_dirs, secrets hygiene (name+state only), sessions, actions[], jobs |
| `/api/setup`, `/api/connections` | `gateway{status,models:[gx-max,gx-auto]}, api_key{masked,key:null}, text_aliases, aliases, kilo{config_example,recommended_model:gx-auto,...}, openwebui{...}, generic{examples}` |
| `/api/keys` | `{keys[], gateway_url}` or honest `{keys:[], unavailable, reason}` |
| `/api/docs`, `/api/docs/{slug}` | doc registry kept |
| `/api/files/roots` | `roots[]` (the 6 allowed roots, §7) |
| `/api/files/browse?path=` | `path, entries[]{name,path,type,size,mtime,mode}, roots` |
| `/api/files/search?root=&q=`, `/api/files/dirsize?path=`, `/api/files/preview?path=` (64 KiB text-only, binary → 415), `/api/files/download?path=` | as named |
| `/api/files/trash` | `trash_root, entries[]{id, original, trashed_name, ts, size, type, user, manifest_sha, still_present}, purge_token_hint` |
| `/api/backup/status` | kept (routes_backup) |
| `/api/stream` (SSE) | `event: queue|lifecycle|telemetry` every ~2 s; `queue` carries `_count_state` sums |

### Actions (POST)

* `/api/actions/{name}` — body may contain `confirm` plus only the declared
  args (anything else → 400 `refused`). 202 + job dict; poll
  `/api/actions/jobs/{id}`. Action set: `gxmax_start(profile)` and
  `gxmax_restart(profile)` (typed confirmation `"gx-max"`),
  `gxmax_stop`/`gxmax_drain` (confirm true), `health_check`,
  `benchmark_run(name)`, `scheduler_cancel(request_id)`,
  `scheduler_retry(request_id)`, `trash_restore(trash_id)`, `purge_trash`
  (typed confirmation `"PURGE TRASH"`, admin), `update_check`,
  `system.refresh|…|restart_ui`, `infra.restart_litellm|restart_orchestrator`.
* `/api/resources/gxmax/{start|stop|restart|drain}` (start/restart take
  `profile`), `/api/resources/gxmax/pin` (`alias` gx-max only, `on`),
  `/api/resources/profile` (`target` auto|max|maintenance|fast|balanced|...).
* `/api/requests/{id}/cancel`, `/api/requests/{id}/retry` (relay; id
  `^[A-Za-z0-9_\-]{1,128}$`).
* `/api/files/upload` (multipart, 512 MiB), `/api/files/delete|rename|move|mkdir`,
  `/api/files/trash/restore` (`id`), `/api/files/trash/purge` (`confirm`:
  `"PURGE TRASH"`).
* `/api/keys` (create), `/api/keys/{id}/revoke|replace` (confirm true),
  `/api/keys/test` (probe a pasted key), `/api/connections/test`,
  `/api/connections/key/reveal`, `/api/setup/test`, `/api/updates/check`,
  `/api/storage/scan`, `/api/backup/{now|verify}`, `/api/logout`.

## Privacy / security invariants the frontend can rely on

* Scheduler records pass an allow-list (`RECORD_FIELDS`): no prompt bodies are
  ever stored, relayed or displayed (Requests page `note` says so).
* Every response is redacted (`redact.py`): secrets, bearer tokens, URL
  credentials, PEM blocks; `/api/system` shows secret *names + state* only.
* File writes are allowlist-rooted, trash-locked, protected-path and
  active-model-path refused; DELETE always moves to
  `/srv/cache/trash/<ts>-<id>-<name>` with a manifest entry.
* Audit log: every action start/finish/refusal lands in
  `/srv/logs/gx-control-ui/audit.log` as JSON.

## Tests

`tests/` (all hermetic, temp dirs, stub orchestrator/AgentOS via
`support.StubUpstream`, registry fixture `support.SAMPLE_REGISTRY`):
`test_server` (auth/CSRF/410/limits/keep-alive/acceptance account),
`test_actions`, `test_filemanager`, `test_requests_view`,
`test_agentos_adapter`, `test_projects_scanner`, `test_storage_scanner` (+recovery),
`test_netview`, `test_updates_view`, `test_views_models`, `test_resources`,
`test_logs`, `test_client_setup`, `test_perf`, plus the retained
`test_auth`, `test_redact`, `test_docs`, `test_hf_access`,
`test_orchestrator_auth`. **204 tests, OK.**

## Notes for the other workers

* Worker A (orchestrator): the UI probes only the §3-4 contract listed above;
  `POST /lifecycle/gx-max/drain` answering `{"state":"draining"}` is treated
  as success; `scheduler_cancel/retry` bodies are `{"id": "<request_id>"}`.
* Worker B (registry): schema-2 fixture in `tests/support.py` matches; a
  non-schema-2 registry degrades honestly everywhere and never leaks
  pre-V4.1 aliases back to a page.
