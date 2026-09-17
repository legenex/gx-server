# CAL: gx-call (NemotronLabs VoiceChat 11B) and Call Agents workstream log

Owner: CAL specialist. Contract: `coordination/BUILD_V3.md`, PLT interfaces in
`coordination/build-v3/plt.md`. Newest log entries last.

## Status (living)

| Item | State |
|---|---|
| Model | `nvidia/NVIDIA-NemotronLabs-VoiceChat-11B` @ `a4c40ca5b4fe77db13e9840ca4a2b91becf030c8` (openmdw-1.1, public), downloading to `/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B` on gx10-02 |
| Runtime | investigation in progress (see "Runtime investigation") |
| Service | `gx-call.service` (node 2, `192.168.100.11:18840` + `127.0.0.1:18840`), key `secrets/gx-call/api-key` |
| Footprint | pending first measured load |

## Interfaces other workstreams need (PLT, LIV, LEAD)

* **Node-2 service:** `gx-call` on `192.168.100.11:18840`, bearer key
  `/srv/projects/gx-cluster/secrets/gx-call/api-key` (0600 on both nodes).
* **Engine container:** `gx-call-engine`. Residency-ledger entry name: `gx-call-engine`
  (same as the container, as PLT section 5.1 asks). Workload class `medium`.
* **Open health:** `GET /health` follows PLT section 5 (`state`, `busy`, `pinned`,
  `active_sessions`, `memory.{pending_gib,resident_gib,estimate_gib}`).
* **Unload:** `POST /v1/call/unload {"if_idle": true}` (409 while a call is live or a
  pin is honoured); `{"if_idle": false, "reason": "gxmax"}` ends live calls and unloads.
* **Tunnel upstream path:** `/v1/call/sessions/<call_id>/ws?join=<node-2 join token>`.
  The join token is created by gx-call when the Control Center creates the session and is
  never sent to a client (it lives only in the registered upstream path).

## Runtime investigation

(filled in as evidence arrives)

## Log

- 2026-09-17 16:35: started. Download of the pinned checkpoint started on gx10-02.
