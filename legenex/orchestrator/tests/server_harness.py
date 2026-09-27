"""Shared harness for the orchestrator server tests (V4.1).

* `StubUpstream` -- a scriptable OpenAI-compatible upstream (the Mia kit's
  vLLM API). Each POST to /v1/chat/completions consumes the next scripted
  response: a JSON completion, an SSE stream, an HTTP error, ...
* `FakeLifecycle` -- a lifecycle that is READY (or DOWN, on demand) without
  ever running a script; it records acquire/begin_use/end_use calls.
* `OrchestratorHarness` -- the real Handler + real Scheduler + real registry
  fixture, wired to those two, served on an ephemeral port.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import http.server  # noqa: E402
import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

from gx_orchestrator.config import Config
from gx_orchestrator.health import ClusterHealth, TierStatus, AliasState
from gx_orchestrator.lifecycle import (
    AcquisitionError,
    LifecycleStatus,
    PHASE_IDLE,
    PHASE_SERVING,
    State,
)
from gx_orchestrator.scheduler import Scheduler
from gx_orchestrator.server import Handler, RoutingJournal, TextMetrics

API_KEY = "test-orchestrator-key"
MODEL_ID = "DeepSeek-v4.1-Flash-EXL3"


# --------------------------------------------------------------------------
# Scriptable upstream
# --------------------------------------------------------------------------

def completion_body(content="323", prompt_tokens=17, completion_tokens=1, extra=None):
    body = {
        "id": "cmpl-test", "object": "chat.completion",
        "model": MODEL_ID,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    if extra:
        body.update(extra)
    return body


class _StubUpstreamHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: A003
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/health"):
            self._json(200, {})
        elif self.path.endswith("/models"):
            self._json(200, {"data": [{"id": self.server.model_id}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        srv = self.server
        body = self._read_body()
        with srv.lock:
            srv.requests.append(body)
            script = srv.script.pop(0) if srv.script else {"status": 200, "json": completion_body()}
        if script.get("delay"):
            time.sleep(script["delay"])
        if script.get("stream") is not None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for chunk in script["stream"]:
                data = json.dumps({"choices": [{"delta": {"content": chunk}}]}).encode()
                self.wfile.write(b"data: " + data + b"\n\n")
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        status = script.get("status", 200)
        if "json" in script:
            self._json(status, script["json"])
        else:
            data = script.get("body", "").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class StubUpstream:
    """Serve the scriptable upstream on an ephemeral loopback port."""

    def __init__(self, model_id=MODEL_ID):
        self.model_id = model_id
        self.requests: list[dict] = []
        self.script: list[dict] = []
        self.lock = threading.Lock()
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstreamHandler)
        srv.model_id = model_id
        srv.requests = self.requests
        srv.script = self.script
        srv.lock = self.lock
        self.port = srv.server_address[1]
        self._thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self._thread.start()
        self._srv = srv

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()

    def last_request(self):
        with self.lock:
            return self.requests[-1] if self.requests else {}


# --------------------------------------------------------------------------
# Fake lifecycle
# --------------------------------------------------------------------------

class FakeLifecycle:
    """State-machine only: READY/DOWN on demand, never runs a script."""

    def __init__(self, state=State.READY, profile="balanced", acquire_time=0.0,
                 acquire_error=None):
        self._state = state
        self._profile = profile
        self._acquire_time = acquire_time
        self._acquire_error = acquire_error
        self.acquire_calls: list[str] = []
        self.in_flight = 0
        self.begin_use_calls = 0
        self.event_lines: list[str] = []

    def _flip(self, state, detail="", error=""):
        self._state = state
        self._detail = detail
        self._error = error

    def status(self):
        return LifecycleStatus(
            state=self._state, since=time.time(), last_used=time.time(),
            waiters=0, detail=getattr(self, "_detail", ""),
            last_error=getattr(self, "_error", ""),
            phase=PHASE_SERVING if self._state is State.READY else PHASE_IDLE,
            phase_since=time.time(), idle_ttl=0,
            in_flight=self.in_flight, profile=self._profile,
        )

    @property
    def current_profile(self):
        return self._profile

    def acquire(self, profile_name=None, timeout=None):
        self.acquire_calls.append(profile_name or "")
        if self._acquire_time:
            time.sleep(self._acquire_time)
        if self._acquire_error:
            raise AcquisitionError(self._acquire_error)
        self._state = State.READY
        return self._profile

    def begin_use(self):
        self.in_flight += 1
        self.begin_use_calls += 1

    def end_use(self):
        self.in_flight = max(0, self.in_flight - 1)

    def release(self, *, force=False):
        self._state = State.DOWN

    def _event(self, source, line):
        self.event_lines.append(f"{source}: {line}")

    def events(self, after=0, limit=200):
        return {"seq": len(self.event_lines), "events": [], "active_job": None, "history": []}


class FakeHealth:
    def snapshot(self):
        return {
            "head": {"healthy": True, "serves_model": True, "model_id": MODEL_ID, "detail": ""},
            "worker": {"ssh_reachable": True, "container_running": True,
                       "container": "dsv41-exl3-worker",
                       "fabric": {"192.168.100.11": True}, "detail": ""},
            "mem": {"node1_gib": 100.0, "node2_gib": 100.0},
            "checked": time.time(),
        }

    def worker_ok(self):
        return True

    def head_status(self):
        return TierStatus(AliasState.READY, "fake", usable=True)


# --------------------------------------------------------------------------
# The orchestrator itself (real Handler, real Scheduler)
# --------------------------------------------------------------------------

class OrchestratorHarness(unittest.TestCase):
    """Base class: orchestrator + stub upstream + fakes, on ephemeral ports."""

    def setUp(self):
        import tempfile
        from tests.registry_fixtures import write_fixture_registry
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

        self.upstream = StubUpstream()
        self.addCleanup(self.upstream.close)

        self.registry_path = write_fixture_registry(self.dir / "registry.json")
        self.cfg = Config(
            hosts=("127.0.0.1",),
            port=0,
            gxmax_base=self.upstream.base,
            gxmax_model_id=MODEL_ID,
            gateway_base="http://127.0.0.1:4000/v1",
            registry_path=self.registry_path,
            scheduler_dir=self.dir / "sched",
            upstream_timeout=15,
            gxmax_max_output=32_768,
        )
        # Config is a frozen dataclass; the Handler reads orchestrator_key()
        # from the environment.
        import os
        self._old_key = os.environ.get("GX_ORCHESTRATOR_API_KEY")
        os.environ["GX_ORCHESTRATOR_API_KEY"] = API_KEY
        self.addCleanup(self._restore_key)

        self.lifecycle = FakeLifecycle()
        self.scheduler = Scheduler(
            queue_path=self.dir / "sched" / "queue.json",
            history_path=self.dir / "sched" / "history.jsonl",
            capacity=2,
        )
        self.addCleanup(self.scheduler.shutdown)
        self.journal = RoutingJournal(self.dir / "routing.jsonl")
        self.metrics = TextMetrics()

        self._handler_cls = type("H", (Handler,), {
            "cfg": self.cfg,
            "registry": self._load_registry(),
            "lifecycle": self.lifecycle,
            "health": FakeHealth(),
            "scheduler": self.scheduler,
            "journal": self.journal,
            "metrics": self.metrics,
        })
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler_cls)
        srv.daemon_threads = True
        self.port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)

    def _restore_key(self):
        import os
        if self._old_key is None:
            os.environ.pop("GX_ORCHESTRATOR_API_KEY", None)
        else:
            os.environ["GX_ORCHESTRATOR_API_KEY"] = self._old_key

    def _load_registry(self):
        from gx_orchestrator.profiles import load_registry
        return load_registry(self.registry_path)

    # ------------------------------------------------------------- client
    def post(self, path, payload, headers=None, timeout=15, raw=False):
        import urllib.request, urllib.error
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {API_KEY}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), (body if raw else json.loads(body))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, dict(exc.headers), (body if raw else json.loads(body))

    def get(self, path, headers=None, timeout=10, auth=True, raw=False):
        import urllib.request, urllib.error
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if auth:
            req.add_header("Authorization", f"Bearer {API_KEY}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                return resp.status, dict(resp.headers), (body if raw else json.loads(body))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return exc.code, dict(exc.headers), (body if raw else json.loads(body))

    def chat(self, payload, headers=None, **kw):
        return self.post("/v1/chat/completions", payload, headers=headers, **kw)

    def wait_for_journal(self, request_id, event="completed", timeout=5.0):
        """The response reaches the client BEFORE the server thread writes
        the trailing journal record; poll until it is durable."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            records = [r for r in self.journal.find(limit=100)
                       if r.get("request_id") == request_id and r.get("event") == event]
            if records:
                return records[0]
            time.sleep(0.02)
        raise AssertionError(f"journal never saw {event!r} for {request_id!r}")
