# gx-control-ui (V4.1 rebuild)

The GX Cluster Control Center backend for the DeepSeek V4.1 Flash EXL3
single-model rebuild. The entire media / music / voice / call / live / flows /
playground / per-tier model-manager surface of the previous dashboard was
**retired** in this rebuild: those modules and their routes are gone, and any
request to a retired `/api/...` path gets `410 Gone` with a short JSON hint
(`{"error": {"code": "gone", "message": "retired with the media stacks (...)"}}`).
The previous implementation is preserved in the git tag
`pre-deepseek-v41-rebuild-20260927`.

What remains (and what is new):

* **Core**: `server.py` (auth/CSRF/static/security framework kept, routes
  rewired), `auth.py`, `config.py`, `models.py` (registry schema 2 reader),
  `services.py` (orchestrator + scheduler + lifecycle probes), `actions.py`
  (typed-argument ActionRunner, audited), `views.py`, `resources.py` (guard
  protocol: profiles, pins, Maintenance holds), `setup.py` (gx-max / gx-auto
  client connections), `logs.py` (config-driven log streams), `api_keys.py`
  (LiteLLM virtual keys), `hf.py` (HuggingFace access, media classes are
  labels only now), `docs.py`, `hostfacts.py`, `redact.py`, `util.py`.
* **New modules**: `filemanager.py` (allowlist + trash), `requests_view.py`
  (scheduler relay, prompt bodies never stored), `agentos_adapter.py` +
  `agents_view.py` (read-only AgentOS), `projects_scanner.py`,
  `storage_scanner.py` (analysis only), `netview.py` (read-only fabric
  diagnostics), `updates_view.py` (pins vs live; check-only, never updates),
  `recovery_view.py`, `sse.py` (live queue/lifecycle/telemetry stream).

Everything is **stdlib-only** and hermetically tested:

```bash
cd legenex/control-ui
python3 -m unittest discover -s tests   # 204 tests, all green, no cluster access
```

The full API surface (every route and JSON shape) is documented in
`state/WORKER-C-REPORT.md` for the frontend rebuild.
