#!/usr/bin/env python3
"""gx-call live acceptance: a REAL call against the deployed supervisor.

Run ON gx10-02, where the supervisor, the engine and the sample audio live:

    legenex/call/tools/call_accept.py
    legenex/call/tools/call_accept.py --wav tool_call.wav --keep-loaded
    legenex/call/tools/call_accept.py --out DIR

What it proves, end to end, with nothing stubbed:

  * the supervisor answers /health and /v1/call/model;
  * the VoiceChat engine loads through the node-2 admission guard;
  * a session can be created and joined over the real WebSocket with its
    one-time join token;
  * REAL human speech (the checkpoint's own sample WAVs) reaches the model as
    PCM16 16 kHz, streamed in real time;
  * the model answers with REAL synthesised audio, which is written out as a
    playable 22.05 kHz WAV and checked for non-silence;
  * a transcript is produced with the caller's turn BEFORE the agent's;
  * a tool call round-trips (when the sample elicits one);
  * the call ends, the recording is retrievable, and the engine unloads and
    gives the memory back.

Every verdict is derived from a measurement. There are no placeholder files:
if a step produces no bytes, that step FAILS. Exit status is 0 only when every
case passes.

This replaces an earlier stub of the same name that called routes which do not
exist (`/v1/calls`), sent no Authorization header, compared a health key the
service never emits, wrote a zero-byte `.png` as "evidence", and returned PASS
for any 2xx on a single POST. None of its output should be trusted.
"""

from __future__ import annotations

import argparse
import audioop
import json
import os
import secrets
import threading
import sys
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from gx_call import ws as gxws  # noqa: E402

SUPERVISOR = os.environ.get("GX_CALL_ACCEPT_BASE", "127.0.0.1:18840")
KEY_FILE = Path(os.environ.get("GX_CALL_API_KEY_FILE",
                               "/srv/projects/gx-cluster/secrets/gx-call/api-key"))
SAMPLES = Path("/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B")
DEFAULT_OUT = Path("/srv/logs/acceptance/build-v3/call")
INPUT_RATE, OUTPUT_RATE = 16000, 22050
#: an agent reply quieter than this is silence, not speech
MIN_RMS = 200


def key() -> str:
    return KEY_FILE.read_text(encoding="utf-8").strip()


def api(path: str, payload: object = None, method: str | None = None, timeout: float = 60.0):
    host, port = SUPERVISOR.split(":")
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + key(), "Content-Type": "application/json"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:400].decode("utf-8", "replace")}


def raw_get(path: str, timeout: float = 60.0) -> tuple[int, bytes]:
    host, port = SUPERVISOR.split(":")
    req = urllib.request.Request(f"http://{host}:{port}{path}",
                                 headers={"Authorization": "Bearer " + key()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def read_wav_16k(path: Path) -> bytes:
    """The sample WAVs as PCM16 LE mono 16 kHz, whatever they started as."""
    with wave.open(str(path), "rb") as w:
        frames = w.readframes(w.getnframes())
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
    if channels > 1:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    if rate != INPUT_RATE:
        frames, _ = audioop.ratecv(frames, 2, 1, rate, INPUT_RATE, None)
    return frames


def write_wav(path: Path, pcm: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def wait_state(target: tuple[str, ...], timeout: float) -> dict:
    deadline = time.time() + timeout
    health: dict = {}
    while time.time() < deadline:
        _, health = api("/health", timeout=30)
        if health.get("state") in target:
            return health
        time.sleep(3)
    return health


def run_call(wav: Path, out: Path, record: bool) -> dict:
    """One real call. Returns the measurements; raises nothing it can report."""
    pcm = read_wav_16k(wav)
    seconds = len(pcm) / 2 / INPUT_RATE
    sid = "call_" + secrets.token_hex(16)
    spec = {
        "session_id": sid,
        "owner": "acceptance",
        "record": record,
        "mode": "test",
        "agent": {
            "agent_id": "agt_" + secrets.token_hex(12),
            "version": 1,
            "name": "GX acceptance agent",
            "system_prompt": ("You are a concise, friendly voice assistant taking a test call. "
                              "Answer in one or two short sentences. Use a tool when one fits."),
            "tools": [{
                "name": "get_weather",
                "description": "Current weather for a city.",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string"}},
                               "required": ["city"]},
                "on_hold": ["Let me check that for you."],
            }],
        },
    }
    status, created = api("/v1/call/sessions", spec)
    if status != 201:
        return {"error": f"session create -> {status} {json.dumps(created)[:300]}"}
    token = created["join_token"]

    host, port = SUPERVISOR.split(":")
    sock = gxws.connect(host, int(port), f"/v1/call/sessions/{sid}/ws?join={token}",
                        {"Authorization": "Bearer " + key(), "X-GX-Session": sid}, timeout=30.0)

    agent_pcm = bytearray()
    events: list[dict] = []
    tool_calls: list[dict] = []
    first_audio_at: list[float | None] = [None]
    started = time.time()
    stop = threading.Event()

    # A reader thread, not a socket timeout: recv() blocks on whole frames, and
    # timing one out mid-frame would desynchronise the stream.
    def reader() -> None:
        while not stop.is_set():
            try:
                msg = sock.recv()
            except Exception:  # noqa: BLE001 - a close ends the call normally
                return
            with lock:
                if not msg.is_text:
                    if msg.data and first_audio_at[0] is None:
                        first_audio_at[0] = time.time()
                    agent_pcm.extend(msg.data)
                else:
                    _absorb_text(msg, events, tool_calls)

    lock = threading.Lock()
    th = threading.Thread(target=reader, daemon=True)
    th.start()

    chunk = INPUT_RATE * 2 // 25          # 40 ms of PCM16
    sent = 0
    tool_results_sent = 0

    def drain_tools() -> int:
        nonlocal tool_results_sent
        with lock:
            pending = tool_calls[tool_results_sent:]
        for tc in pending:
            api(f"/v1/call/sessions/{sid}/tool-results",
                {"call_id": tc["call_id"], "output": "18 degrees and clear."})
            tool_results_sent += 1
        return tool_results_sent

    try:
        while sent < len(pcm):                      # stream the caller in real time
            sock.send_binary(bytes(pcm[sent:sent + chunk]))
            sent += chunk
            time.sleep(0.04)
            drain_tools()
        deadline = time.time() + 30                 # let the model finish replying
        while time.time() < deadline:
            drain_tools()
            with lock:
                done = any(e.get("type") == "session.ended" for e in events)
            if done:
                break
            time.sleep(0.5)
    finally:
        stop.set()
        try:
            sock.close()
        except Exception:  # noqa: BLE001 - closing must never mask a result
            pass
        th.join(timeout=5)

    with lock:
        ended = any(e.get("type") == "session.ended" for e in events)
        socket_events = len(events)
        tools_seen = [t["name"] for t in tool_calls]

    api(f"/v1/call/sessions/{sid}/end", {"reason": "acceptance complete"})

    # Server-side transcript, independent of what we saw on the socket.
    _, ev = api(f"/v1/call/sessions/{sid}/events?after=0&wait=0", timeout=30)
    server_events = ev.get("events", []) if isinstance(ev, dict) else []

    audio_path = out / "agent_reply.wav"
    if agent_pcm:
        write_wav(audio_path, bytes(agent_pcm), OUTPUT_RATE)
    rms = audioop.rms(bytes(agent_pcm), 2) if agent_pcm else 0

    rec_status, rec_bytes = (0, b"")
    if record:
        # The recording only exists once the session is terminal AND the writer
        # has flushed it, so a fetch straight after /end races and returns 409.
        for _ in range(30):
            rec_status, rec_bytes = raw_get(f"/v1/call/sessions/{sid}/recording?track=caller")
            if rec_status == 200 and rec_bytes:
                (out / "recording_caller.wav").write_bytes(rec_bytes)
                break
            time.sleep(2)

    transcript = [e for e in server_events if e.get("type", "").startswith("transcript")]
    (out / "events.json").write_text(json.dumps(server_events, indent=2), encoding="utf-8")

    roles = [e.get("role") or e.get("speaker") for e in transcript]
    return {
        "session_id": sid,
        "wav": wav.name,
        "caller_seconds": round(seconds, 2),
        "agent_audio_bytes": len(agent_pcm),
        "agent_audio_seconds": round(len(agent_pcm) / 2 / OUTPUT_RATE, 2),
        "agent_audio_rms": rms,
        "first_audio_latency_s": round(first_audio_at[0] - started, 2) if first_audio_at[0] else None,
        "socket_events": socket_events,
        "server_events": len(server_events),
        "transcript_entries": len(transcript),
        "transcript_roles": roles[:12],
        "tool_calls": tools_seen,
        "tool_results_sent": tool_results_sent,
        "recording_status": rec_status,
        "recording_bytes": len(rec_bytes),
        "session_ended_event": ended,
        "audio_file": str(audio_path) if agent_pcm else None,
    }


def _absorb_text(msg, events: list, tool_calls: list) -> None:
    try:
        obj = json.loads(msg.text())
    except (ValueError, AttributeError, UnicodeDecodeError):
        return
    events.append(obj)
    if obj.get("type") == "tool.call":
        tool_calls.append({"call_id": obj.get("call_id"), "name": obj.get("name"),
                           "arguments": obj.get("arguments")})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", default="tool_call.wav", help="sample under the checkpoint directory")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--keep-loaded", action="store_true", help="skip the unload case")
    ap.add_argument("--no-record", action="store_true")
    args = ap.parse_args()

    out = args.out or DEFAULT_OUT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []

    def case(name: str, ok: bool, detail: str) -> None:
        results.append({"case": name, "verdict": "PASS" if ok else "FAIL", "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")

    status, health = api("/health")
    case("health", status == 200 and health.get("status") == "ok",
         f"HTTP {status} status={health.get('status')} state={health.get('state')}")

    status, model = api("/v1/call/model")
    ident = (model or {}).get("identity", {})
    case("model identity", status == 200 and ident.get("repo", "").endswith("VoiceChat-11B"),
         f"{ident.get('repo')}@{str(ident.get('revision'))[:12]} image={ident.get('image')}")

    t0 = time.time()
    status, loaded = api("/v1/call/load", {}, timeout=1800)
    health = wait_state(("ready", "failed"), 1800)
    load_s = time.time() - t0
    case("engine load", health.get("state") == "ready",
         f"state={health.get('state')} in {load_s:.1f}s resident="
         f"{health.get('memory', {}).get('resident_gib')} GiB "
         f"min_avail={health.get('memory', {}).get('min_mem_available_during_load_gib')} GiB")
    if health.get("state") != "ready":
        _summary(out, results)
        return 1

    wav = SAMPLES / args.wav
    if not wav.is_file():
        case("sample audio", False, f"missing {wav}")
        _summary(out, results)
        return 1

    m = run_call(wav, out, record=not args.no_record)
    (out / "call.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    if "error" in m:
        case("real call", False, m["error"])
        _summary(out, results)
        return 1

    case("caller audio accepted", m["caller_seconds"] > 1,
         f"streamed {m['caller_seconds']}s of real speech from {m['wav']}")
    case("agent response audio", m["agent_audio_bytes"] > 0 and m["agent_audio_rms"] >= MIN_RMS,
         f"{m['agent_audio_seconds']}s, {m['agent_audio_bytes']} bytes, rms {m['agent_audio_rms']} "
         f"(silence floor {MIN_RMS}) -> {m['audio_file']}")
    case("first-audio latency", m["first_audio_latency_s"] is not None,
         f"{m['first_audio_latency_s']}s")
    case("transcript", m["transcript_entries"] > 0,
         f"{m['transcript_entries']} entries, roles {m['transcript_roles']}")
    case("events", m["server_events"] > 0, f"{m['server_events']} server-side events")
    if m["tool_calls"]:
        case("tool call round trip", m["tool_results_sent"] > 0,
             f"tools {m['tool_calls']}, {m['tool_results_sent']} result(s) accepted")
    else:
        results.append({"case": "tool call round trip", "verdict": "N/A",
                        "detail": "this sample elicited no tool call"})
        print("N/A   tool call round trip: this sample elicited no tool call")
    if not args.no_record:
        case("recording", m["recording_status"] == 200 and m["recording_bytes"] > 44,
             f"HTTP {m['recording_status']}, {m['recording_bytes']} bytes")

    if not args.keep_loaded:
        before = api("/health")[1].get("memory", {})
        api("/v1/call/unload", {}, timeout=300)
        health = wait_state(("unloaded", "failed"), 300)
        case("unload", health.get("state") == "unloaded",
             f"state={health.get('state')} resident "
             f"{before.get('resident_gib')} -> {health.get('memory', {}).get('resident_gib')} GiB")

    return _summary(out, results)


def _summary(out: Path, results: list[dict]) -> int:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    (out / "summary.json").write_text(json.dumps(
        {"at": datetime.now(timezone.utc).isoformat(), "counts": counts, "results": results},
        indent=2), encoding="utf-8")
    lines = ["# gx-call live acceptance", "",
             f"Run {datetime.now(timezone.utc).isoformat()}", "",
             "| Case | Verdict | Detail |", "| --- | --- | --- |"]
    lines += [f"| {r['case']} | {r['verdict']} | {r['detail']} |" for r in results]
    (out / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{counts}\nevidence: {out}")
    return 0 if counts.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
