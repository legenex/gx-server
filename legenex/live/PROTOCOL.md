# gx-live realtime protocol — `gx-live.v1`

One WebSocket per live session. It carries **JSON events** (text frames) and
**media** (binary frames) in both directions at the same time: the browser
keeps sending microphone audio and camera frames while the assistant's
speech plays, which is what makes interruption possible.

```
browser / API client ──WS──▶ GX-Playground /rt/live/<session_id>     (gx10-01, tunnel, plt.md §1)
                              ──WS (fabric)──▶ gx-live supervisor      (gx10-02 :18850)
                                                ──WS (loopback)──▶ gx-live engine (MiniCPM-o 4.5)
```

The protocol version is announced in `session.created.protocol`. A client
that does not understand the major version must close the socket.

## 1. Creating a session

| Client | Request | Auth |
|---|---|---|
| Browser (GX-Playground) | `POST /api/live/sessions` | session cookie + `X-CSRF-Token`, same origin |
| API client | `POST /v1/live/sessions` (on the Playground, port 8090 / 8443) | `Authorization: Bearer <gateway virtual key that allows gx-live>` |

Body (all fields optional):

```json
{"instructions": "You are Jarvis ...",        // extra system instructions, <= 2000 chars
 "language": "en",                              // en | zh
 "tools": true,                                 // application tools (section 5)
 "output_audio": true,                          // false = text-only replies
 "vad": {"threshold": 0.5, "silence_ms": 700},  // 0.3..0.9, 300..2000
 "max_response_tokens": 256}                    // 32..1024
```

Answer `201`:

```json
{"session_id": "live_<32 hex>", "ws_path": "/rt/live/live_<32 hex>",
 "protocol": "gx-live.v1", "expires_at": 1789660000, "model_state": "unloaded|loading|ready|busy",
 "waiting": null}
```

API clients get `ws_path` with `?ticket=<one-time ticket>` (single use, 60 s,
bound to the session and the key). Browsers connect without a ticket: the
tunnel checks the session cookie and the Origin.

Only **one live session can be active on gx10-02 at a time** (one model
instance, one streaming context). A second create answers `409 session_busy`
with the owner-neutral reason. A create while gx-max holds the node, or in
Maintenance, answers `503` with `waiting.code` = `gx_max_active` /
`maintenance`. A create while memory is short succeeds and the socket
reports `model.state = waiting` with the numeric reason until the model can
load (or the wait gives up after 30 minutes).

End a session: send `session.stop` on the socket, or
`POST /api/live/sessions/<id>/end` / `POST /v1/live/sessions/<id>/end`.
Closing the socket without that leaves the session resumable for 60 s
(reconnect with the same `ws_path`; API clients need a new ticket from
`POST /v1/live/sessions/<id>/ticket`), then it ends as `abandoned`.

## 2. Binary frames

Every binary frame starts with an 8-byte header, big-endian:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | `kind` |
| 1 | 1 | `version` = `1` |
| 2 | 2 | `response` — the assistant response number for output audio, `0` for input |
| 4 | 4 | `seq` — per-kind sequence number, starts at 0 |

| kind | Direction | Payload | Limits |
|---|---|---|---|
| `0x01` mic audio | client → server | PCM signed 16-bit little-endian, mono, **16 000 Hz** | 20–500 ms per frame (320–8000 samples, even byte count) |
| `0x02` camera frame | client → server | one JPEG image | ≤ 512 KiB, ≤ 2 frames/s are used (extra frames are dropped), longest side ≤ 1920 px |
| `0x11` assistant audio | server → client | PCM signed 16-bit little-endian, mono, **24 000 Hz** | typically 1 s per frame |

Frames of an unknown kind or version close the socket with 1003.
Assistant audio of a response the client has seen interrupted
(`response.interrupted`) must be dropped by the client; the `response` field
makes that possible without timing guesses.

## 3. Client → server events

| type | Fields | Meaning |
|---|---|---|
| `session.update` | `camera` (bool), `output_audio` (bool), `muted` (bool) | Runtime switches. `muted` stops speech detection without closing the mic stream. |
| `input.text` | `text` (1–4000 chars) | A typed message in the same conversation. The assistant answers in text and (if enabled) speech. The latest camera frame, if the camera is on, is attached. |
| `input.audio.commit` | — | End the current utterance now (push-to-talk). |
| `response.cancel` | — | Stop the current response (the Stop button). |
| `playback.state` | `playing` (bool), `buffered_ms` (int) | The client's speaker state. Used for barge-in decisions and interruption latency. |
| `ping` | `t` (client ms) | Answered with `pong`. |
| `session.stop` | — | End the session cleanly. |

Unknown event types are answered with an `error` (`unknown_event`, not
fatal). Events larger than 64 KiB close the socket with 1009.

## 4. Server → client events

| type | Fields |
|---|---|
| `session.created` | `protocol`, `session_id`, `model` {alias, repository, revision}, `config`, `limits` {audio_in_rate, audio_out_rate, max_frame_ms, max_jpeg_bytes, frames_per_s} |
| `model.state` | `state` = `unloaded` \| `waiting` \| `loading` \| `ready` \| `busy`, `reason` (human sentence), `waiting` {code, reason, required_gib, available_gib, pending_gib} \| null, `load_ms` (when it became ready) |
| `session.ready` | `load_ms` (null when the model was already loaded) |
| `input.speech.started` | `at_ms` (session clock), `during_response` (bool) |
| `input.speech.stopped` | `at_ms`, `duration_ms` |
| `transcript.user` | `turn`, `text`, `source` = `speech` \| `text` (speech transcripts arrive after the reply has started) |
| `response.started` | `response`, `turn`, `trigger` = `speech` \| `text` \| `tool` |
| `transcript.assistant.delta` | `response`, `text` (captions; aligned to the audio chunk they belong to) |
| `response.done` | `response`, `status` = `completed` \| `interrupted` \| `failed`, `text`, `metrics` {first_audio_ms, first_text_ms, turn_ms, audio_ms} |
| `response.interrupted` | `response`, `reason` = `barge_in` \| `client_cancel` \| `new_input`, `latency_ms` (speech detected → generation stopped) |
| `tool.call` | `call_id`, `name`, `arguments`, `status` = `running` |
| `tool.progress` | `call_id`, `state` = `waiting` \| `loading` \| `running`, `detail`, `model` (for delegations) |
| `tool.result` | `call_id`, `name`, `ok`, `summary` (≤ 600 chars), `model`, `latency_ms`, `error` {code, message} \| null |
| `metrics` | `turn`, `first_audio_ms`, `turn_ms`, `interrupt_ms`, `tool_ms`, `delegation_ms` (whichever apply) |
| `pong` | `t`, `server_ms` |
| `error` | `code`, `message`, `fatal` (a fatal error is followed by `session.ended`) |
| `session.ended` | `reason` = `completed` \| `abandoned` \| `timeout` \| `gx_max` \| `maintenance` \| `failed` \| `replaced`, `duration_s`, `turns` |

Latency definitions (all measured on gx10-02, milliseconds):

* **first audio**: end of the user's utterance (VAD stop, or commit / text
  received) → first assistant audio frame sent. The VAD silence window
  (`silence_ms`) comes on top of this for the person speaking.
* **turn**: end of utterance → `response.done`.
* **interruption**: speech detected during a response → generation stopped
  and `response.interrupted` sent. The browser adds its own playback flush
  (it drops queued audio at once).
* **tool / delegation**: `tool.call` → `tool.result`.

## 5. Tools

Tools are **application tools with a strict schema**, executed on gx10-01 by
the Control Center, never by the model or the browser. MiniCPM-o 4.5 is asked
to emit `<tool_call>{"name": ..., "arguments": {...}}</tool_call>` (its chat
template's native format); the engine stops the spoken reply as soon as the
text starts with a tool call, so the call is never spoken. Invalid calls
(unknown name, extra or missing arguments, wrong types, over-long strings)
are refused without being executed and reported as `tool.result` with
`ok: false`.

| Tool | Arguments | What runs |
|---|---|---|
| `get_time` | `{}` | Local date and time on gx10-01 (Africa/Johannesburg unless configured). |
| `delegate_to_gx` | `model` ∈ `gx-auto`, `gx-fast`, `gx-reason`; `task` (1–4000 chars) | A chat completion through the LiteLLM gateway, server-side, with the session's owner as the end user. **Never gx-max.** If gx-reason is not loaded, `tool.progress` says so (`loading`, with the Resource Control reason) and the call waits up to 10 minutes. |
| `search_library` | `query` (1–200 chars), `type` ∈ `image`, `video`, `audio`, `any` | The Media Library search (titles, prompts, tags) for the session's owner; returns up to 5 items (title, type, created). |
| `fetch_url` | `url` (http/https, ≤ 2000 chars) | A text fetch through `netguard` (no private, loopback or link-local targets; size and time limits). Returns ≤ 4000 characters of text. |

The result is given back to the model as a `<tool_response>` and the model
speaks the answer. Tool activity is shown on the Live page and recorded
(names, timings, outcome; not the arguments' content) in the session record.

## 6. Privacy

* Audio and camera frames exist only in memory on gx10-02 for the current
  turn and are never written to disk or logs.
* The session record (Control Center database) holds times, counts,
  latencies, tool names and outcomes, and error codes. Transcripts are kept
  only in the browser unless the user presses **Save transcript**, which
  stores them as a Library text asset.
* Metric lines never contain transcript, prompt, audio or image content
  (`gxcommon.metrics` drops those fields).

## 7. Close codes

| Code | Meaning |
|---|---|
| 1000 | session ended normally (see the preceding `session.ended`) |
| 1001 | server shutting down, gx-max takeover or idle tunnel |
| 1003 | unsupported binary frame |
| 1007 | invalid JSON or UTF-8 |
| 1008 | policy (bad token, session replaced, frame rules) |
| 1009 | frame or event too big |
| 1011 | internal failure |
| 4409 | another connection took over this session |
