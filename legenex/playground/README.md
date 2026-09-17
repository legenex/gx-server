# GX-Playground

The creative web app of the GX10 cluster (D-037): images, video and music
(ACE-Step) with one Library. It runs on **gx10-01**, port **8090**, on
`127.0.0.1` and the Tailscale address `100.105.214.61`.

* **User guide:** `Manual.md` §7.
* **Operator guide:** `legenex/control-ui/docs/15-playground.md` and
  `16-music.md`.
* **API contract:** [API.md](API.md).

## How it works

* **Proxy and front end.** `gx_playground` is a small, dependency-free
  Python server. It serves the static single-page app in `web/` and forwards
  an allow-listed set of API paths to the Control Center backend
  (`127.0.0.1:8088`).
* **Shared backend.** Sessions, jobs, the Library and admission all live in
  that one backend, so the Playground cannot bypass them.
* **Trust between the two.** The proxy adds a local shared token
  (`/srv/projects/gx-cluster/secrets/control-ui/proxy-token`, mode 0600) and
  the real client address. The backend trusts that address only when the
  request carries the token and comes from loopback.
* **Public music API.** `/v1/music/*` is forwarded with the caller's
  `Authorization` header and never with cookies.
* **What the proxy never exposes:** gx10-02, Docker, a shell, engine paths,
  or the LiteLLM master key.

## Install, run, repair (gx10-01)

```bash
legenex/playground/scripts/install.sh --check   # show what would change
legenex/playground/scripts/install.sh           # install/refresh the user unit and (re)start it
systemctl --user status gx-playground
curl -s http://127.0.0.1:8090/pg/health
tail -f /srv/logs/gx-playground/playground.log
```

The unit starts at boot and never loads a model.

## Tests

```bash
cd legenex/playground
npm run qa          # formatting, ruff, proxy unit tests, build budget, offline browser E2E + axe, gitleaks
npm run test:live   # REAL generations through http://127.0.0.1:8090 (gx10-01 only; uses GPU time)
```

**Offline tests.** They start the real Control Center backend with synthetic
cluster data (`../control-ui/e2e/fixture_server.py`) behind the real proxy.

**Live tests.**

* They sign in with the loopback-only `acceptance` account.
* They exercise real music, image, video and Library flows, then delete
  every asset they created.
* Traces, screenshots and video are off, so no secret can be recorded.
* Set `GX_EVIDENCE_DIR` to keep a JSON log of the run.

`node_modules` is a symlink to `../control-ui/node_modules`, and linting
reuses the Control Center `.venv`. Run the Control Center QA once first.
