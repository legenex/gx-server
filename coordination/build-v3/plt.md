# PLT: platform workstream (Build V3)

Owner: platform specialist. Status: **in progress**. This file publishes the
interfaces that CAL, LIV, VOI, FLO and the other workstreams code against.
Interface sections marked **STABLE** will not change without a note here.

---

## 1. Realtime tunnel (STABLE): CAL and LIV code against this

### 1.1 What the browser or API client does

```
GET /rt/call/<session_id>             (browser: session cookie + same-origin Origin)
GET /rt/live/<session_id>
GET /rt/call/<session_id>?ticket=<t>  (API client: one-time ticket, no cookie needed)
Upgrade: websocket
```

* The URL is on the **Playground** (`ws://<host>:8090/rt/...` or
  `wss://<host>:8443/rt/...`). The browser code uses
  `new WebSocket(realtimeUrl(service, sessionId))` from
  `web/js/realtime.js` (PLT). It builds the right `ws:`/`wss:` URL from `location`.
* `session_id` must match **`^(call|live)_[0-9a-f]{32}$`**, and its prefix
  must equal the path service. Create ids with
  `gx_control_ui.realtime.new_session_id("call")`.
* Other query parameters are **dropped**. The upstream path and query are
  fixed server-side when you register the session (see 1.3).
* `Sec-WebSocket-Protocol` is forwarded. `Sec-WebSocket-Extensions` is
  **stripped**, so there is no permessage-deflate. Frames are plain RFC 6455.
* Close codes the tunnel itself sends to the client:
  * 1002: protocol error, for example a client frame that was not masked or a reserved bit set
  * 1009: frame too big
  * 1001: idle timeout, maximum duration reached, or the service shut down
  * 1011: upstream failure after the upgrade

  Before the upgrade, failures are plain HTTP JSON errors:
  * 401 `unauthenticated`
  * 403 `bad_origin` / `forbidden`
  * 404 `not_found` (unknown or foreign session)
  * 409 `session_busy`
  * 429 `too_many_connections`
  * 502 `upstream_unavailable`
  * 503 `realtime_disabled`

### 1.2 What the Playground does (you do not implement this)

1. It validates the path and id strictly, then calls
   `POST http://127.0.0.1:8088/api/realtime/authorize` with the
   `X-GX-Proxy-Token` header, the client's `Cookie` and
   `X-GX-Forwarded-For`, and a JSON body:
   `{"service","session_id","ticket"|null,"origin","host","scheme"}`.
2. It opens TCP to the fixed target for the service:
   * `call`: `192.168.100.11:18840`
   * `live`: `192.168.100.11:18850`

   The targets come from the Control Center config (`GX_RT_CALL_TARGET` and
   `GX_RT_LIVE_TARGET`). A client can never choose the target.
3. It sends the upgrade to `<upstream_path>`. The request carries
   `Authorization: Bearer <service key>` (read server-side from
   `secrets/gx-<svc>/api-key`) and `X-GX-Session: <session_id>`. It also
   carries `X-GX-Owner: <opaque owner id>` (the `sha256(owner)[:16]`) and
   `X-GX-Request-Id`. The client's cookies and authorization are removed.
4. It relays the `101` (only `Upgrade`, `Connection`, `Sec-WebSocket-Accept`
   and `Sec-WebSocket-Protocol`), then splices bytes both ways.
   * Client frames are parsed only for their headers, which enforces the
     masking and maximum frame size rules.
   * Payloads are never logged.
   * The limits are in 1.5.

### 1.3 What CAL / LIV implement on the Control Center (gx10-01)

```python
from .realtime import new_session_id          # "call_<32 hex>" / "live_<32 hex>"

# in your POST /api/call/sessions (browser) or POST /v1/call/sessions (API):
sid = new_session_id("call")
# ... create the session on your node-2 service with that id ...
rec = h.app.realtime.register(
    "call", sid,
    owner=f"user:{h.session.username}",      # browser
    # owner=f"key:{ident['key']}",           # API (the 16-hex key digest from _key_identity)
    upstream_path=f"/v1/call/sessions/{sid}/ws",   # your service's WS path (+ optional fixed query)
    ttl_s=3600,                               # how long the session may be connected to (<= 4 h)
    meta={"agent_id": "..."},                 # small, non-secret; shown in Logs
)
ticket = h.app.realtime.issue_ticket(sid, owner=rec.owner)   # API clients only (60 s, single use)
ws_path = f"/rt/call/{sid}"                                  # + f"?ticket={ticket}" for API clients
# when the session ends:
h.app.realtime.end(sid, disposition="completed")             # completed|abandoned|failed|timeout|transferred
```

* `h.app.realtime` is a `gx_control_ui.realtime.RealtimeRegistry`. Sessions
  live in memory. A Control Center restart ends every session. Your service
  should end its side when the WS closes.
* `register()` rejects:
  * bad ids
  * an unknown service
  * an `upstream_path` that does not match `^/[A-Za-z0-9._~/\-]{1,200}(\?[A-Za-z0-9._~=&\-]{0,200})?$`
  * `meta` larger than 2 KiB
* Tickets are HMAC-SHA256 with a per-process key. They are bound to the
  session id and the owner, expire after 60 s, and are **single use**. A
  reused ticket is refused with 401 `ticket_used`.
* Browser path: the session cookie is checked. The `Origin` header is
  required and must equal the Host. The session owner must be
  `user:<signed-in user>`.
* Ticket path: the ticket's owner must equal the session's owner.
* `h.app.realtime.list(owner=...)` / `.get(sid)` / `.stats()` feed Resource
  Control and the Logs page.

### 1.4 What CAL / LIV implement on node 2 (your service)

* The WS endpoint at the `upstream_path` you registered, on
  `192.168.100.11:<port>`.
* Require `Authorization: Bearer <your key>`, compared in constant time.
  Refuse anything else with 401 **before** the 101.
* Validate that `X-GX-Session` matches the path id and that the session exists.
* Answer a standard RFC 6455 `101`. Do not negotiate extensions (the tunnel
  strips them).
* Send a WS ping at least every 30 s (or data), because the tunnel idle
  timeout is 120 s.

### 1.5 Limits (Playground env, defaults)

| Env | Default | Meaning |
|---|---|---|
| `GX_PG_RT_ENABLED` | `1` | `0` answers 503 `realtime_disabled` |
| `GX_PG_RT_IDLE_S` | `120` | no bytes in either direction, then close (1001) |
| `GX_PG_RT_MAX_S` | `14400` | hard cap per connection (also capped by the session `ttl_s`) |
| `GX_PG_RT_MAX_FRAME` | `4194304` | largest client frame payload (1009 above) |
| `GX_PG_RT_MAX_BYTES` | `4294967296` | bytes per direction per connection |
| `GX_PG_RT_PER_OWNER` | `4` | concurrent tunnels per owner (429 above) |
| `GX_PG_RT_TOTAL` | `32` | concurrent tunnels overall |

There is one tunnel per session id. A new authorised connection for the same
session **replaces** the old one, which is closed with 1001.

Structured logs, one JSON line each (`gxcommon.metrics`, service `gx-playground`):
* `{"kind":"metric","event":"tunnel.open", ...}`
* `{"kind":"metric","event":"tunnel.close", ...}`
* `{"kind":"metric","event":"tunnel.refused", ...}` (before the 101)

The fields are:

* `alias` (`gx-call`/`gx-live`), `session_id`, `request_id`, `scheme`
* `owner` (hash), `user` (`user:<name>` or `key:<16hex>`), `via` (`session`/`ticket`)
* `upstream_status`, `close_reason`, `duration_ms`
* `bytes_in`, `bytes_out`, `frames_in`, `frames_out`

No payloads and no keys are logged.

Hermetic tests: `legenex/playground/tests/test_tunnel.py` (29 cases, real
Playground + real Control Center + stub node-2 service). A stub you can reuse
for your own service tests is `legenex/playground/tests/wsutil.py`
(`Client`, `StubService`, `encode`, `read_frame`).

### 1.6 Browser helper (`web/js/realtime.js`)

```js
import { realtimeUrl, secureContextInfo, openRealtime } from '../realtime.js';
realtimeUrl('call', sid)            // "ws(s)://<same host:port>/rt/call/<sid>"
secureContextInfo()                 // {secure, https, httpsUrl, reason}: show the HTTPS helper when !secure
const ws = openRealtime('live', sid, { protocols: [], binaryType: 'arraybuffer' })
```

---

## 2. HTTPS listener (STABLE)

* The Playground serves the **same app** at `https://<host>:8443/`, bound to
  127.0.0.1 and the Tailscale address only.
* It uses a local private CA, stored in
  `/srv/projects/gx-cluster/secrets/playground-tls/` (keys 0600).
* `https://100.105.214.61:8443` is a secure context, so `getUserMedia` works
  there once the CA is trusted.
* **Download the CA certificate** (public, no key) from
  `https://<host>:8443/pg/ca.crt` or `http://<host>:8090/pg/ca.crt`.
* Pages that need the microphone or camera call
  `secureContextInfo()` (`web/js/realtime.js`). When it reports an insecure
  context, render the shared helper `secureContextCallout()` (same module).
  The helper links to Settings, which shows the HTTPS steps.
* Cookies on HTTPS use a separate `__Host-gxui_session` cookie (Secure,
  HttpOnly, SameSite=Strict). An HTTPS request also accepts the plain HTTP
  cookie, so signing in on HTTP carries over to HTTPS, but not the other
  way round. The HTTP flow is unchanged.
* `Permissions-Policy` on the app document is
  `microphone=(self), camera=(self)`, and every other feature is denied. API
  responses deny everything. The Playground is a single hash-routed
  document, so only the Live and Call pages ever call `getUserMedia`.

---

## 3. Navigation (STABLE): how to add your page

`legenex/playground/web/index.html` now has three groups:

* `<ul id="nav-create">`: Dashboard, Images, Video, Music, and then
  **FLO** adds Creative Flows after Dashboard, and **VOI** adds Voice after Music.
* `<ul id="nav-realtime">`: **LIV** adds Live and **CAL** adds Call Agents.
  The group stays hidden until it has a link.
* `<ul id="nav-manage">`: Library, History, Models, Logs, Settings (PLT).

Copy an existing `<li>` exactly:

```html
<li><a class="rail-link" href="#/voice" data-page="voice"><span class="rail-ic" data-icon="mic"></span><span class="rail-label">Voice</span></a></li>
```

Icons added by PLT in `web/js/icons.js` are:

| Icon | Use |
|---|---|
| `flow` | Creative Flows |
| `mic` | Voice |
| `camera` | Live |
| `phone` | Call Agents |
| `logs` | Logs |
| `settings` | Settings |
| `shield` | security and HTTPS |

On phones, the rail becomes a bottom bar with the three group buttons. Each
button opens that group's list as a menu. This is automatic.

---

## 4. Observability helper (STABLE)

A shared, stdlib-only helper lives at
`legenex/common/gxcommon/metrics.py`. Each consumer imports it differently:

* Control Center: `from .obs import metric`.
* Node-2 supervisors: put `<repo>/legenex/common` on `sys.path` and use
  `from gxcommon.metrics import Metrics`.

```python
m = Metrics("gx-call", node="gx10-02", file=os.environ.get("GX_METRICS_FILE"))
m.emit("model.load", alias="gx-call", outcome="ok", duration_ms=81234,
       mem_available_before_gib=113.2, mem_available_min_gib=71.9, footprint_gib=41.3)
with m.timer("generation", alias="gx-voice", operation="tts", user="admin") as t:
    ...; t.set(audio_seconds=12.4)          # outcome=ok, or failed with error_code on exception
```

`emit()` writes one JSON line to stdout (your journal or log). With
`file=`, it also appends to that JSONL file. The Control Center Logs page
reads `/srv/logs/gx-metrics/*.jsonl` on gx10-01 only.

Every line has these fields:

| Field | Content |
|---|---|
| `ts` | ISO 8601 time |
| `kind` | always `"metric"` |
| `service` | the emitting service |
| `node` | the node it runs on |
| `event` | the event name (see below) |
| `outcome` | `ok` / `failed` / `refused` / `cancelled` / `waiting`, when it applies |
| `duration_ms` | when it applies |
| `user` | owner, when known: `user:<name>` or `key:<16hex>` |

Event names and their extra fields:

| event | extra fields |
|---|---|
| `model.load` | alias, footprint_gib, mem_available_before_gib, mem_available_min_gib, startup_s |
| `model.unload` | alias, reason (`idle`/`evicted`/`gxmax`/`maintenance`/`manual`/`shutdown`) |
| `queue.wait` | alias, job_id, wait_ms, waiting_reason |
| `admission.wait` | alias, reason_code, reason, required_gib, available_gib, pending_gib |
| `generation` | alias, job_id, operation, error_code, audio_seconds, frames, width, height |
| `realtime.session` | service, session_id, disposition, bytes_in, bytes_out |
| `realtime.latency` | service, session_id, stage (`asr`/`llm`/`tts`/`first_audio`/`turn`), ms |
| `tunnel.open` / `tunnel.close` | service, session_id, owner, close_reason, upstream_status, bytes_in, bytes_out |
| `memory.sample` | mem_available_gib, swap_used_gib, pending_gib, resident (dict alias → GiB) |
| `failure` | component, error_code, message (redacted, ≤ 200 chars) |
| `flow.run` | flow_id, run_id, nodes, error_code |
| `flow.node` | flow_id, run_id, node_id, node_type, error_code |
| `call.disposition` | session_id, agent_id, disposition, turns |
| `voice.synthesis` | alias, operation, audio_seconds, rtf |
| `music.render` | job_id, audio_seconds |

The helper enforces these rules:

* Keys named `text`, `prompt`, `transcript`, `content`, `audio`, `image`,
  `messages`, `body`, `authorization`, `cookie`, `key` (but not `job_id`),
  `token`, `ticket`, `secret` or `password`, or containing `api_key`, are
  **dropped**.
* Strings are cut to 200 chars and `sk-…`/Bearer shapes are redacted.
* Only scalars, flat lists of up to 20 scalars, and a single flat dict
  (`resident`) are kept.

---

## 5. Node-2 pending-memory contract (D-038 generalised): STABLE

Every node-2 tenant supervisor (gx-music :18820, gx-voice :18830,
gx-call :18840, gx-live :18850) serves an **open** `GET /health`:

```json
{"service": "gx-voice", "state": "unloaded|loading|ready|busy|unloading|failed",
 "busy": false, "pinned": false, "active_jobs": 0, "active_sessions": 0,
 "memory": {"pending_gib": 0.0, "resident_gib": 0.0, "estimate_gib": 14.0}}
```

The fields mean:

* `pending_gib`: memory this tenant has been granted, or is loading, that
  MemAvailable does not show yet. It is 0 when the tenant is idle or
  fully loaded.
* `resident_gib`: the measured size while loaded. It is 0 when the tenant
  is unloaded.

The shared reader is `legenex/common/gxcommon/node2_tenants.py`:

```python
from gxcommon.node2_tenants import PeerTenants
peers = PeerTenants.from_env(exclude="gx-voice")   # GX_NODE2_PEERS overrides the default map
peers.pending_gib()        # sum over reachable peers (a peer that is "loading" without the field counts its estimate)
peers.snapshot()           # {name: {reachable, state, busy, pending_gib, resident_gib, ...}}
```

The default peer map points at `http://192.168.100.11:<port>/health` for
media-router, music, voice, call and live. Each consumer applies it
differently:

* The media router uses its own copy in `gx_media_router/tenants.py`
  (`PeerTenants`), because the router container is self-contained. A
  contract test keeps the two parsers identical.
* gx-music subtracts `peers.pending_gib()` in its admission.
* New supervisors subtract router + music + other peers.

Admission rule, unchanged: `MemAvailable - Σ peers' pending - own growth >= 30 GiB`.

### 5.1 Unload API used by the gx-max drain and Resource Control

`POST /v1/<svc>/unload` with `{"if_idle": true}` and the bearer key:

| Response | Meaning |
|---|---|
| 200 | `{"state":"unloaded", ...}` |
| 409 | busy or pinned |

The drain (`node2-holds.sh`) calls it with `{"if_idle": false, "reason": "gxmax"}`.
A supervisor MUST stop sessions or jobs and unload when it sees that call
**or** a fresh `node2.gxmax-hold`. It then verifies three things:

* its container is gone,
* its ledger entry is gone (`node2-residency.json` key = container name), and
* its engine processes are gone.

Container names that the drain verifies:

| Service | Container |
|---|---|
| gx-voice | `gx-voice-engine` |
| gx-call | `gx-call-engine` |
| gx-live | `gx-live-engine` |

**Tell PLT in your own ws file if you use another name.**

---

## 6. Activity sources for the Playground Logs page (STABLE)

`GET /api/activity?kind=&status=&q=&since=&limit=` (PLT) merges these
sources for the signed-in user:

* media jobs
* music jobs
* realtime sessions
* the audit log (this user's own actions)
* metrics lines with `user == user:<name>`
* any registered source

To add yours, put this in `App.__init__` in your block:

```python
self.activity.register("voice", lambda user, since, limit: [...])
```

Each item looks like this:

```python
{"id": str, "kind": "voice", "title": str, "status": "ok|failed|running|waiting|cancelled",
 "at": epoch_float, "duration_ms": int|None, "error": str|None, "link": "#/voice?job=..."|None,
 "detail": {small flat dict, no prompts/transcripts}}
```

The page applies `redact()` to every string.

---

## 7. Measured footprints needed from VOI / CAL / LIV (please fill in your ws file)

Resource Control shows "not measured yet" until your file contains a line
in exactly this format:

```
FOOTPRINT gx-voice node=gx10-02 cold_gib=<need incl. load transient> resident_gib=<steady> startup_s=<cold start> measured=<YYYY-MM-DD> evidence=<path>
```

PLT parses these lines from `coordination/build-v3/{voi,cal,liv}.md`
into `legenex/models/registry.json` (`aliases.<alias>.measured`). Resource
Control reads only the registry.

---

## 8. Catalogue extras for the Playground Models page (STABLE)

`GET /api/catalog` (PLT) lists the eleven aliases. It draws on the registry
(repository, revision, runtime, quantization, context, capabilities,
components with file *names* only, `variants`, memory, startup), the live
Resource Control state, and the measured footprint.

To add a section, for example WAN's LoRA library or IMG's model selector,
put this in your `App.__init__` block:

```python
self.catalog_extras["video_loras"] = lambda app: {"count": 12, "items": [{"name": "...", "pair": "...", "base": "Wan 2.2 T2V"}]}
```

The data must be small and JSON-safe, with no paths under `/srv`, no keys and
no prompts. Image variants belong in the registry, as
`aliases.gx-image.variants` (a list or a dict of
`{id,label,family,repository,revision,licence,capabilities,workflows,default,status,measured}`).
The page renders them automatically.

---

## Progress log

- 2026-09-17 16:50: published interfaces 1-7, and regrouped the nav.
