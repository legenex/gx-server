# gx-call and Call Agents

`gx-call` is a realtime **speech-to-speech** alias (D-040): a caller talks, the
model answers in its own voice, and the Control Center runs the agent's tools
against an authoritative intake record while the call is happening.

| | |
|---|---|
| Model | `nvidia/NVIDIA-NemotronLabs-VoiceChat-11B` @ `a4c40ca5b4fe77db13e9840ca4a2b91becf030c8` (licence openmdw-1.1, public) |
| Checkpoint on disk | `/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B` on gx10-02, 41.35 GiB, 17 files, verified against the HF API |
| Runtime | NeMo Speech branch `nemotron-labs-voicechat` @ `097dfe9e…` in the `gx-call-engine` container (torch 2.14 / cu130, the GB10 set proven by gx-comfyui and gx-music) |
| Supervisor | `gx-call.service` on gx10-02, `192.168.100.11:18840` and `127.0.0.1:18840`, bearer key `secrets/gx-call/api-key` |
| Audio | caller in: PCM16 mono **16 kHz**. Agent out: PCM16 mono **22.05 kHz** |
| Node | gx10-02 (L-2). One live call at a time; the engine unloads after 15 idle minutes |

> **Status:** the engine image has not been built yet, so no call has been
> served. The Call Agents page, the agent store, the intake state machine, the
> tools and the public API are complete and tested; the voice itself is not
> available until the image is built and the first load is measured. See
> "Current limitations" at the end.

## What a Call Agent is

An agent is **not** a copy of the model. Every agent shares the one gx-call
instance on gx10-02. The agent decides:

* what the model is told (the compiled system prompt),
* which of the six real tools it may call,
* which structured intake it has to fill in and which fields are required,
* what happens around the call: transfer, webhooks, post-call actions,
  recording and retention.

Every save creates a **new immutable version**. The agent keeps a stable id
(`agt_<24 hex>`), a status (draft / enabled / disabled / archived) and a mode
(test / production). Every call records the exact version it used, so a change
never rewrites the history of earlier calls.

## Prerequisites

* Signed in to GX-Playground.
* For a live call: a **secure context**, because the browser only gives the
  microphone to `https://…` or `http://127.0.0.1`. The page shows the HTTPS
  helper when the address is not secure (Settings explains the certificate).
* gx-call reachable on gx10-02. When it is not, the page says
  "gx-call is not answering" and starting a call fails with a plain message
  instead of silence.

## Using the page

The page has three sections.

### Agents

* **New agent** creates a complete, working agent from a template
  (IntakePilot MVA intake, or a general agent) as a **draft**.
* **Edit** opens the editor. It covers the name and branding, the eleven
  behaviour texts (role, personality, opening line, call flow, objection
  handling, conversation rules, prohibited behaviour, transfer rules, out of
  hours, voicemail, fallback, knowledge), the tools, the required and optional
  intake fields, the transfer destination, recording with its spoken notice,
  retention and the maximum call length.
  * **Check and preview** validates the configuration on the server and shows
    the exact prompt and tool list the model would receive. Invalid
    configurations are explained in place and never saved.
  * **Save new version** writes a new version. If nothing changed, it says so
    instead of creating an empty version. If somebody else saved meanwhile,
    the save is refused with a version conflict, so no edit is silently lost.
  * Webhooks, business hours and the intake schema are edited through the
    gx-call API; the editor shows them read-only and preserves them untouched.
* **Enable / Disable** decides whether the agent may take calls. Only enabled
  agents are offered to API clients.
* **Duplicate** copies an agent (optionally an older version) under a new id.
* **Archive** hides an agent. Its calls and results are kept.
* **Version history** lists every version with who saved it and how many calls
  it took.

### Call

Pick an enabled agent and press **Start the call**. The browser then:

1. asks for the microphone,
2. opens a WebSocket to this Playground (`/rt/call/<session>`), which tunnels
   it to gx-call over the fabric — the browser never talks to gx10-02,
3. streams 80 ms PCM16 frames up and plays the agent's speech back.

While the call runs you see the live transcript of both sides, the intake
record filling up with what has actually been saved (not what the model
believes), the microphone level, and whether the agent is speaking. You can
**Mute**, **Request transfer** and **End call**. Barge-in works: speaking over
the agent stops its audio immediately.

When the call ends, the page opens the finished call so you can read the
result straight away.

### History

Every call you started, and every call started with your gateway key, with its
outcome, transfer state, timings, transcript, the tools that ran, the final
structured result and the recording. **Delete transcript and recording**
removes the content and keeps the metadata and the summary.

## The tools an agent may use

At most **five** per agent (the model card's recommendation). Every one of them
runs on the Control Center against real state, not in the model's imagination:

| Tool | What it really does |
|---|---|
| `update_intake_fields` | Validates and writes caller answers into the authoritative intake record (phone numbers, dates, US states and enums are normalised; bad values are rejected and the model is told) |
| `check_business_hours` | Answers from the agent's configured hours and time zone |
| `lookup_accident_state_rules` | Filing deadline, negligence rule and no-fault status for a US state from a local table (all 51 entries) |
| `request_warm_transfer` | Sets the transfer state, notifies the configured destination with a summary |
| `send_webhook` | Posts the current record to the agent's webhooks (SSRF-checked, HMAC-signed) |
| `end_call` | Records the outcome and ends the call a few seconds after the goodbye |

## Expected result

* A call reaches state `live` within a few seconds of `session.ready`, the
  agent speaks first, and every answer the caller gives appears in the intake
  panel with its field name.
* When the required fields are complete, the call's intake disposition is
  `intake_complete`; a warm transfer makes it `transferred`.
* A recorded call produces one mixed audio asset in the Library with
  `operation = recording` and `source_ref = <session id>`.

## Error handling

| What you see | What it means |
|---|---|
| "gx-call is not answering" | The supervisor on gx10-02 is not reachable. Agents can still be edited; no call can start. |
| "gx-call is not configured on this Control Center (no service key)" | `secrets/gx-call/api-key` is missing or too short on gx10-01. |
| "gx-max is starting on the cluster; calls resume when it is released" | `node2.gxmax-hold` exists. gx-max owns both nodes; wait for the release. |
| "this agent is not enabled" | API clients may only call enabled agents. |
| "Microphone and camera need a secure connection" | The page is not a secure context. Use the HTTPS address or `http://127.0.0.1:8090`. |
| The call was disconnected | The tunnel closed (idle 120 s, the cap of 4 h, or the service stopped). The call is finalised with the outcome it had. |

## Mobile, accessibility and offline behaviour

* The page works down to phone width; the section tabs are a real ARIA
  tablist with arrow-key, Home and End support, and every icon-only button has
  a label. The live transcript is an `aria-live="polite"` log, so a screen
  reader follows the conversation. The offline suite runs axe (WCAG 2.2 AA) on
  the agent list, the editor, the Call tab, the history and the call detail.
* A live call needs the network by definition. If the connection drops, the
  page says so and the call is finalised; nothing is queued for later.
* The microphone is released and the audio graph torn down as soon as the call
  ends or you leave the page.

## Privacy

* Audio is never stored unless the agent has recording **and** its spoken
  notice enabled, and the notice is added to the prompt so the agent says it.
* The persisted event feed holds metadata only: transcripts live in their own
  table and are removed by **Delete transcript and recording** and by the
  agent's retention period.
* Metrics and logs never contain transcripts, prompts or keys.
* Integration secrets are write-only: they are stored 0600 on gx10-01 and are
  never returned to the browser.

## Public API

Machine clients use a LiteLLM virtual key that allows `gx-call` (never a
session cookie), on `/v1/call/…`: list enabled agents, create a session, get a
one-time WebSocket ticket, read and update the intake state, request a
transfer, end the call and fetch the result. See the API documentation page.

## Current limitations

* **The engine image is not built.** A build was started on gx10-02 on
  2026-09-17 and cancelled during the wheel-building stage. Nothing has been
  inferred yet, and the memory footprint is an estimate (48 GiB) rather than a
  measurement.
* NVIDIA's realtime NIM container is published for linux/amd64 only and its
  vLLM path needs a patched vLLM that is not in any public release, so the
  engine uses NeMo's native PyTorch pipeline with three documented GB10
  patches (mmap checkpoint load, early bf16 cast, hybrid KV/Mamba cache).
* One live call at a time.
* Typed turns are not supported by the model: it takes caller audio only.
