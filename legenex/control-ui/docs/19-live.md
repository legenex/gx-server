# gx-live

`gx-live` is the realtime assistant: you talk, it answers out loud, and you can
cut it off mid-sentence. It sees through the camera, and it can hand a hard
question to one of the bigger GX text models and speak the answer back.

| | |
|---|---|
| Model | `openbmb/MiniCPM-o-4_5` @ `503e754207c94da6bb26850b4469f367c9ea3582` (apache-2.0) |
| Runtime | transformers 4.51.0 remote code, torch 2.14.0+cu130, SDPA (no flash-attn on sm_121), bfloat16. Image `gx-live-engine:minicpmo45-503e754-t214` |
| Node | gx10-02, `192.168.100.11:18850` (fabric and loopback only; never a browser) |
| Memory | measured 2026-09-17: 34 GiB admission estimate (load transient included), ~31 GiB resident, ~102 s cold start |
| Sessions | **one at a time.** The model is held exclusively by the open session |
| Protocol | `gx-live.v1`, one WebSocket per session (`legenex/live/PROTOCOL.md`) |

## Prerequisites

* The microphone and camera need a **secure context**. `http://127.0.0.1:8090`
  is one; `http://100.105.214.61:8090` is not. Use the HTTPS listener
  (`https://<host>:8443`) from another device and install the GX-Playground
  certificate once — Settings explains how. The Live page shows this helper by
  itself when the page is insecure.
* gx10-02 must have ~34 GiB free above the 30 GiB reserve. If it does not, the
  session still starts and the page says what it is waiting for, with numbers.
* gx-max holding the node, or Maintenance, refuses a new session (and ends a
  running one).

## Starting a session

1. Open **Live** (Realtime group).
2. Choose the options on the right:
   * **Language** — English or Chinese, the two languages the model speaks.
   * **Personality and rules** — extra system instructions, up to 2000
     characters. They are sent to gx10-02 for the session and are never stored
     in the database.
   * **Let the assistant use tools** — see *Tools* below.
   * **Speak the answers** — off gives captions only.
   * **Pause before answering** — how long you may pause mid-sentence
     (0.30–2.00 s) before the assistant takes its turn.
   * **Voice detection** — raise it in a noisy room.
   * **Longest answer** — 32–1024 tokens. Short answers feel much faster.
3. Press **Start session**. The browser asks for the microphone, then the page
   shows *Loading MiniCPM-o 4.5* (about 100 s cold) and then *Listening*.
4. Talk. Stop talking, and the assistant answers.

Expected result: your words appear as a line in the conversation, the
assistant's line fills in as it speaks, and **This session** counts the turns
and the median first-audio and turn latency.

## Interrupting

Just start talking while the assistant speaks. The microphone never stops, so
speech onset interrupts the answer: the queued audio is dropped immediately in
the browser and generation stops on gx10-02. The line is marked *interrupted*
and the interruption latency is recorded. **Stop** does the same thing with a
button, for when you would rather not talk over it.

## Camera

The camera button streams one still per second (at most two; each under
512 KiB) alongside the audio. Ask "what am I holding?" and the model answers
from the latest frame. Turning the camera off stops the capture and releases
the device. Nothing is recorded: frames exist in memory on gx10-02 for the
current turn.

## Typing

The message box sends a typed turn in the same conversation, with the latest
camera frame attached if the camera is on. Enter sends, Shift+Enter makes a
new line. **Send turn** ends your spoken turn immediately instead of waiting
for the pause — useful in a noisy room.

## Tools

Tools run **on gx10-01, in the Control Center**, never in the model and never
in the browser. Every call is checked against a strict schema first; an
invalid call is refused without running. The tool panel shows what ran, how
long it took and whether it worked.

| Tool | What it does |
|---|---|
| `get_time` | The local date and time on gx10-01. |
| `delegate_to_gx` | Hands the task to `gx-auto`, `gx-fast` or `gx-reason` through the gateway and speaks the answer. **Never gx-max.** If gx-reason has to load first, the panel says so and the call waits. |
| `search_library` | Searches your Media Library (titles, prompts, tags) and names up to five matches. Which assets it surfaced is recorded with the session. |
| `fetch_url` | Fetches a public page through `netguard` (no private, loopback or link-local addresses) and reads up to 4000 characters of its text. |

## Ending a session, and what is kept

**End session** stops the conversation and releases the model; it asks first if
there is a conversation you have not saved. Closing the tab or leaving the page
ends it too — the model is never left held by a browser that went away.

What the Control Center keeps (`live_*` tables, migration `060_live.sql`):

* the session record: who, when, how long, how it ended, the model identity,
  load and wait time, turn and interruption counts;
* per-turn timings (first audio, turn, interruption latency) — no text;
* the tool calls: name, argument **names**, timing, outcome, delegated model —
  never the arguments' content;
* which Library assets the session referenced;
* the transcript **only if you press Save transcript**.

Audio and camera frames are never written to disk, on either node, and never
appear in a log or a metric line.

**Delete conversation** removes a saved transcript at once. Transcripts are
deleted automatically 30 days after the session ends.

## Errors and what they mean

| What you see | What happened |
|---|---|
| *Waiting for memory* with numbers | gx10-02 does not have 34 GiB spare above the reserve yet. The session waits (up to 30 minutes) and starts by itself. |
| *Another live session is running* | gx-live holds the model for one session at a time. |
| A 503 with `gx_max_active` or `maintenance` | gx-max has the node, or an administrator is in Maintenance. |
| *The session was refused (policy)* | the tunnel refused the socket: a stale ticket, a foreign session, or too many connections. |
| *The server closed the session* (1001) | gx-max took the node, the service restarted, or the tunnel was idle for two minutes. |
| *The microphone could not be opened* | permission was refused, or another application holds the device. The page says which. |

## Mobile, keyboard and screen readers

The page works at phone width: the columns stack and the controls wrap. Every
control is reachable by keyboard and has a visible focus ring; the
microphone, camera and stop controls are buttons with labels, not gestures.
The conversation is a polite live region, so a screen reader announces each new
line without interrupting itself, and the model state is announced when it
changes. Nothing depends on colour alone, and the level meter is decorative
(it has a numeric `aria-valuenow`, and the state text says what is happening).
There is no audio-only information: everything the assistant says is also a
caption, and **Speak the answers** can be turned off entirely.

## Offline behaviour

There is none, by design: a live conversation needs the model. With no
connection the page says the session could not be started and offers to try
again; nothing is queued, because a stale spoken turn is worse than no turn.

## API

The same thing over HTTP, with a gateway key that allows `gx-live`:

```
POST /v1/live/sessions        {"config": {...}}   -> {"session_id", "ws_path", "links"}
POST /v1/live/sessions/<id>/ticket                -> a fresh 60 s single-use ws_path
GET  /v1/live/sessions/<id>                       -> the record
GET  /v1/live/sessions/<id>/events                -> the metadata event log
POST /v1/live/sessions/<id>/end
```

The WebSocket is `wss://<playground>/rt/live/<session_id>?ticket=<t>` and
speaks `gx-live.v1` (`legenex/live/PROTOCOL.md`): JSON events as text frames,
16 kHz PCM up, 24 kHz PCM down, JPEG camera stills up. The ticket is single
use and lasts 60 seconds. A key only ever sees its own sessions, and a browser
cookie is never accepted on `/v1/live/*`.
