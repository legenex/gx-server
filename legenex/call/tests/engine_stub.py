"""A stand-in for the gx-call engine container (no GPU, no model).

Speaks the engine protocol of ``engine/gx_call_engine.py`` over a real
WebSocket (stdlib framing from ``gx_call.ws``) and uses the REAL
``gx_call_tracker.Tracker`` to produce the gx-call.v1 events, so supervisor
and Control Center tests exercise the same event stream the model produces.

Scripted "model": when the caller has spoken and then been quiet for 480 ms,
the stub transcribes the utterance as ``utterance N``, answers with one
second of a 300 Hz tone (agent speech) and, if the session has any tool,
first calls one of them once with fixed arguments and waits for the result
(like the model's two-phase function calling). The tool it picks is
``update_intake_fields`` when the agent has it, otherwise the first tool the
agent does have, so an agent whose only tool is ``end_call`` exercises that
path. Caller speech while the stub is "speaking" stops the tone (barge-in).

Like the real engine, the socket is drained by its own thread, so a tool
result can arrive while the "model" is waiting for it and audio keeps being
buffered meanwhile.
"""

from __future__ import annotations

import hmac
import json
import math
import queue
import secrets
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[1] / "engine"))

from gx_call.ws import WebSocket, WSClosed, accept_key  # noqa: E402
from gx_call_tracker import Tracker, rms_dbfs_pcm16  # noqa: E402

IN_CHUNK = 1280 * 2          # 80 ms of 16 kHz PCM16
OUT_SAMPLES = 1764           # 80 ms at 22.05 kHz


def tone_pcm16(samples: int, freq: float = 300.0, amp: float = 0.4, phase: int = 0) -> bytes:
    return b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * (i + phase) / 22050)))
                    for i in range(samples))


def speech_like(seconds: float, rate: int = 16000, amp: float = 0.3) -> bytes:
    """Caller audio for tests: a loud 220 Hz tone."""
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * 220 * i / rate))) for i in range(n))


def silence(seconds: float, rate: int = 16000) -> bytes:
    return b"\x00\x00" * int(seconds * rate)


class EngineStub:
    def __init__(self, key: str, port: int = 0, tool_args: dict | None = None) -> None:
        self.key = key
        self.state = "ready"
        self.sessions = 0
        self.configs: list[dict] = []
        self.tool_results: list[dict] = []
        self.tool_args = tool_args or {"fields": {"caller_name": "Jane Doe", "accident_state": "Texas"}}
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # noqa: A003
                pass

            def do_GET(self):  # noqa: N802
                if self.path == "/health":
                    body = json.dumps({"service": "gx-call-engine", "state": stub.state}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, f"Bearer {stub.key}") or self.path != "/v1/engine/stream":
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if stub.state != "ready":
                    self.send_response(409)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept_key(self.headers["Sec-WebSocket-Key"]))
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                ws = WebSocket(self.connection, self.rfile, mask_outgoing=False, require_masked=True)
                stub.state = "busy"
                stub.sessions += 1
                try:
                    stub.run(ws)
                finally:
                    stub.state = "ready"

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ session
    def run(self, ws: WebSocket) -> None:
        lock = threading.Lock()

        def send(obj) -> None:  # noqa: ANN001
            with lock:
                if isinstance(obj, bytes):
                    ws.send_binary(obj)
                else:
                    ws.send_text(json.dumps(obj))

        cfg = json.loads(ws.recv().text())
        self.configs.append(cfg)
        tools = [t["name"] for t in cfg.get("tools", [])]
        tool_name = "update_intake_fields" if "update_intake_fields" in tools else (tools[0] if tools else None)
        send({"type": "session.ready", "protocol": "gx-call.v1", "input_rate": 16000, "output_rate": 22050,
              "chunk_ms": 80, "prefill_ms": 5, "prompt_chars": len(cfg.get("system_prompt", "")),
              "voice": "Aria", "tools": sorted(tools)})
        tracker = Tracker(emit=send)
        results: dict[str, threading.Event] = {}
        inbox: queue.Queue = queue.Queue()
        ended = threading.Event()
        why = {"reason": "ended"}

        def reader() -> None:
            """The engine never stops reading the socket, not even during a tool call."""
            try:
                while True:
                    msg = ws.recv()
                    if not msg.is_text:
                        inbox.put(msg.data)
                        continue
                    body = json.loads(msg.text())
                    if body.get("type") == "tool.result":
                        self.tool_results.append(body)
                        ev = results.get(body.get("call_id"))
                        if ev:
                            ev.set()
                    elif body.get("type") == "session.end":
                        why["reason"] = body.get("reason", "ended")
                        break
            except (WSClosed, OSError):
                why["reason"] = "disconnected"
            finally:
                ended.set()
                for ev in list(results.values()):
                    ev.set()
                inbox.put(None)

        rt = threading.Thread(target=reader, name="engine-stub-reader", daemon=True)
        rt.start()

        buf = bytearray()
        speaking = 0          # remaining agent frames
        utterance = 0
        heard = False
        quiet = 0
        tool_done = False
        phase = 0
        while not ended.is_set():
            data = inbox.get()
            if data is None:
                break
            buf.extend(data)
            while len(buf) >= IN_CHUNK:
                chunk = bytes(buf[:IN_CHUNK])
                del buf[:IN_CHUNK]
                loud = rms_dbfs_pcm16(chunk) > -30
                text = asr = ""
                if loud:
                    heard, quiet = True, 0
                    if speaking:
                        speaking = 0  # barge-in: yield
                else:
                    quiet += 1
                if heard and quiet >= 6:
                    heard = False
                    asr = f"utterance {utterance}"
                    utterance += 1
                    if tool_name and not tool_done:
                        tool_done = True
                        call_id = "tc_" + secrets.token_hex(8)
                        results[call_id] = threading.Event()
                        send({"type": "tool.call", "call_id": call_id, "name": tool_name,
                              "arguments": self.tool_args, "raw": "<TOOLCALL>[...]</TOOLCALL>", "known": True})
                        results[call_id].wait(5)
                    speaking = 12
                    text = "Thank you, I have noted that."
                if speaking:
                    out = tone_pcm16(OUT_SAMPLES, phase=phase)
                    phase += OUT_SAMPLES
                    speaking -= 1
                    level = -8.0
                else:
                    out = b"\x00\x00" * OUT_SAMPLES
                    level = -120.0
                try:
                    send(out)
                except (WSClosed, OSError):
                    ended.set()
                    break
                tracker.step(in_pcm16=chunk, in_wall=time.time(), out_level_dbfs=level, out_ms=80,
                             text_delta=text, asr_delta=asr, asr_reset=bool(asr) and utterance > 1)
        summary = {**tracker.finish(), "reason": why["reason"], "type": "engine.stats", "rtf_session": 0.01}
        try:
            send({"type": "session.ended", "summary": summary})
        except (WSClosed, OSError):
            pass
        try:
            ws.close()
        except OSError:
            pass


if __name__ == "__main__":  # manual use: python3 engine_stub.py KEY PORT
    s = EngineStub(sys.argv[1], int(sys.argv[2]))
    print("engine stub on", s.port)
    socket.socket().close()
    threading.Event().wait()
