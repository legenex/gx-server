# LIV — gx-live (MiniCPM-o 4.5) workstream log

Owner: LIV specialist. Contract: `coordination/BUILD_V3.md`. Newest entries first.

## Status (living)

| Item | State |
|---|---|
| Model | `openbmb/MiniCPM-o-4_5` @ `503e754207c94da6bb26850b4469f367c9ea3582` (apache-2.0, public), at `/srv/models/live/MiniCPM-o-4_5` on gx10-02 (verified) |
| Runtime | transformers remote code (4.51.0) on the proven GB10 torch 2.14/cu130 stack, SDPA (no flash-attn for sm_121). Image `gx-live-engine:minicpmo45-503e754-t214` built from `legenex/live/engine/Dockerfile` on gx10-02 |
| Service | `gx-live.service` (node 2, `192.168.100.11:18850` + `127.0.0.1:18850`), key `secrets/gx-live/api-key` |
| Footprint | measured 2026-09-17 (probe 1): see FOOTPRINT line below |

## Interfaces other workstreams need (PLT, CAL, LEAD)

* **Node-2 service:** `gx-live` on `192.168.100.11:18850` (+ `127.0.0.1:18850`), bearer key
  `/srv/projects/gx-cluster/secrets/gx-live/api-key` (0600 on both nodes).
* **Engine container / ledger entry:** `gx-live-engine` (plt.md 5.1), class `medium`, node `node2`.
* **Open health (plt.md 5):** `GET /health` → `{"service": "gx-live", "state":
  "unloaded|waiting|loading|ready|busy|unloading|failed", "busy", "pinned", "active_sessions",
  "active_jobs": 0, "queue": 0, "waiting": {code, reason, required_gib, available_gib, pending_gib} | null,
  "idle_seconds", "memory": {"estimate_gib", "resident_gib", "pending_gib", "reserve_gib"}}`.
  `busy` = a live session is open (the model is exclusively held by it).
* **Unload:** `POST /v1/live/unload {"if_idle": true}` → 409 `busy` (session live or loading) /
  `pinned`; `{"if_idle": false, "reason": "gxmax"}` ends the session (`session.ended` reason `gx_max`)
  and unloads with verification (container gone, ledger entry gone, no engine processes). A fresh
  `node2.gxmax-hold` does the same within 1 s.
* **Tunnel upstream path (PLT §1.3):** `/v1/live/sessions/<live_id>/ws?join=<node-2 join token>`. The
  join token is created by gx-live when the Control Center creates the session, is compared in
  constant time, and never reaches a client. The supervisor also requires the bearer key and
  `X-GX-Session` == path id, and pings every 20 s.
* **Tool bridge (Control Center → node 2, fabric):** `GET /v1/live/sessions/<id>/tool-calls?wait=25`
  (long poll) and `POST /v1/live/sessions/<id>/tool-calls/<call_id>` (`{"kind": "progress"|"result", ...}`).
* **Shared realtime pieces (CAL):** stdlib RFC 6455 endpoints in `legenex/common/gxcommon/rtws.py`
  (`accept(handler)`, `connect(host, port, path, headers=)`, masking/size/close rules, tests in
  `legenex/common/tests/test_rtws.py`). Browser capture/playback worklets in
  `legenex/playground/web/js/realtime/` (see the log entry when they land).
* **Protocol:** `legenex/live/PROTOCOL.md` (`gx-live.v1`).

## Measured footprint (PLT parses this line)

FOOTPRINT gx-live node=gx10-02 cold_gib=34 resident_gib=31 startup_s=102 measured=2026-09-17 evidence=/srv/logs/acceptance/build-v3/liv/probe1-feasibility

* Weights on GPU after load: 20.7 GiB (torch allocated). During speech turns torch holds 24-26 GiB
  (reserved up to 29.5 GiB after a native-duplex test). CUDA context, ONNX speaker model and Python
  add ~3 GiB.
* MemAvailable on gx10-02 fell from 60.8 to a minimum of 28.1 GiB during probe 1 (1 Hz samples,
  `mem.tsv`), i.e. ~32.7 GiB at peak with other tenants steady. The probe had been admitted with a
  30 GiB estimate, so the 30 GiB reserve was undershot by ~1.9 GiB for ~20 s at the end of the
  probe (native-duplex test). The service estimate is therefore **34 GiB** (class `medium`).
* Cold start: 97 s weight load (disk-bound while other downloads ran; 94-186 s observed) + 5 s TTS init.

## Log

- 2026-09-17 16:40: model downloaded and verified (`hf-verify.py`, 54 files, 18.67 GiB,
  `.gx-manifest.json`). Engine image `gx-live-engine:minicpmo45-503e754-t214` built on gx10-02
  (torch 2.14.0+cu130, transformers 4.51.0, onnxruntime 1.21.0, minicpmo-utils 1.0.6, silero-vad 6.2.1,
  ruamel.yaml pinned to 0.18.10 because 0.19 breaks hyperpyyaml).
- 2026-09-17 16:56: **GB10 feasibility (probe 1, admission-guarded, evidence
  `/srv/logs/acceptance/build-v3/liv/probe1-feasibility/`)**:
  * transformers remote code + SDPA works on sm_121 (no flash-attn). torchaudio 2.11 needs TorchCodec for
    `load/save`; the engine installs a soundfile shim (`gx_live_engine/compat.py`).
  * image understanding OK ("fossil of an ancient bird embedded in rock"), ASR via `chat()` 1.4 s for 4 s of
    speech and exact; ASR between turns does not disturb the streaming session.
  * half-duplex streaming speech: prefill 0.28-0.57 s, first audio 1.33-1.70 s after prefill, 1.5-1.9x
    faster than realtime. Breaking the generator mid-answer (interruption) leaves the session usable.
  * vision inside a speech turn OK ("I see cars, a road, trees, buildings, and signs").
  * tool calls: with text input the model emits exact `<tool_call>` JSON (get_time, delegate_to_gx
    gx-fast); in a speech session with the tool block appended to the voice prompt it did NOT call the
    tool; a `<tool_response>` prefill was then spoken correctly. Prompt variants under test (probe 2).
  * **native full-duplex (`as_duplex`) is not realtime on GB10**: 0.74 s per 1 s unit while listening, but
    1.5-1.6 s per unit while speaking (prefill 0.5 s + generate 1.05 s). The shipped mode is therefore
    VAD turn-taking with barge-in over a full-duplex transport (mic and camera keep streaming while the
    assistant speaks; speech onset interrupts). Duplex numbers are kept here as the measured reason.
