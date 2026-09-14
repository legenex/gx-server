"""The gx-cluster orchestrator HTTP service.

Two responsibilities, deliberately kept separate (see ARCHITECTURE.md):

  * ROUTING  -- the `gx-auto` alias picks a tier and forwards to the LiteLLM
    gateway. Routing never starts or stops processes.
  * LIFECYCLE -- the `gx-max` alias guarantees the two-node engine is up before
    a request is served, acquiring both nodes if needed. It NEVER falls back to
    a different model.

Served on 127.0.0.1:18900 by default: this is an internal control surface and
must not be exposed unauthenticated.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .classifier import route
from .config import CONFIG, Config
from .lifecycle import AcquisitionError, GxMaxLifecycle, State
from .tiers import TIERS, ROUTABLE, Tier
from .upstream import UpstreamError, post_json, probe, stream_post

log = logging.getLogger("gx.server")

ROUTING_LOG = "gx.routing"


class TierHealth:
    """Cached availability probe for the routable tiers.

    gx-max's availability comes from the lifecycle state machine, not a probe:
    "down" is a normal resting state for it, not a fault.
    """

    def __init__(self, cfg: Config, lifecycle: GxMaxLifecycle, ttl: float = 15.0) -> None:
        self._cfg = cfg
        self._lifecycle = lifecycle
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[Tier, bool] = {}
        self._checked = 0.0

    def snapshot(self) -> dict[Tier, bool]:
        with self._lock:
            if time.time() - self._checked < self._ttl and self._cache:
                return dict(self._cache)
        avail: dict[Tier, bool] = {}
        key = self._cfg.gateway_key()
        gateway_up = probe(
            f"{self._cfg.gateway_base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"} if key else {},
        )
        for tier in ROUTABLE:
            if tier is Tier.MAX:
                # Reachable if the engine is up OR we are able to bring it up.
                avail[tier] = self._lifecycle.status().state in (
                    State.READY,
                    State.DOWN,
                    State.ACQUIRING,
                )
            else:
                avail[tier] = gateway_up
        with self._lock:
            self._cache = avail
            self._checked = time.time()
        return dict(avail)


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-orchestrator/1.0"
    protocol_version = "HTTP/1.1"

    cfg: Config
    lifecycle: GxMaxLifecycle
    health: TierHealth

    # ---------------------------------------------------------------- helpers
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, code: str = "orchestrator_error") -> None:
        self._send_json(status, {"error": {"message": message, "type": code, "code": code}})

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _auth_headers(self) -> dict[str, str]:
        key = self.cfg.gateway_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/health", "/healthz", "/"):
            self._send_json(200, {"status": "ok", "service": "gx-orchestrator"})
            return

        if path == "/health/detailed":
            st = self.lifecycle.status()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "gx_max": st.as_dict(),
                    "tiers": {t.value: v for t, v in self.health.snapshot().items()},
                    "gateway": self.cfg.gateway_base,
                },
            )
            return

        if path == "/v1/models":
            now = int(time.time())
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": alias.value,
                            "object": "model",
                            "created": now,
                            "owned_by": "gx-cluster",
                        }
                        for alias in (Tier.AUTO, *ROUTABLE)
                    ],
                },
            )
            return

        if path == "/lifecycle/gx-max/status":
            self._send_json(200, self.lifecycle.status().as_dict())
            return

        self._send_error_json(404, f"no such path: {path}", "not_found")

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._send_error_json(400, f"invalid JSON body: {exc}", "invalid_request")
            return

        if path == "/lifecycle/gx-max/acquire":
            try:
                self.lifecycle.acquire(timeout=payload.get("timeout"))
                self._send_json(200, {"status": "ready", **self.lifecycle.status().as_dict()})
            except AcquisitionError as exc:
                self._send_error_json(503, str(exc), "gx_max_unavailable")
            return

        if path == "/lifecycle/gx-max/release":
            self.lifecycle.release(
                force=bool(payload.get("force")),
                restore=payload.get("restore", True),
            )
            self._send_json(200, {"status": "released", **self.lifecycle.status().as_dict()})
            return

        if path in ("/v1/chat/completions", "/v1/completions"):
            self._handle_inference(path, payload)
            return

        self._send_error_json(404, f"no such path: {path}", "not_found")

    # ------------------------------------------------------- inference routing
    def _handle_inference(self, path: str, payload: dict[str, Any]) -> None:
        requested = str(payload.get("model") or "").strip()

        if requested == Tier.AUTO.value:
            self._serve_auto(path, payload)
        elif requested == Tier.MAX.value:
            self._serve_gx_max(path, payload, direct=True)
        else:
            self._send_error_json(
                400,
                f"orchestrator handles only '{Tier.AUTO.value}' and '{Tier.MAX.value}'; "
                f"got '{requested}'. Send other aliases to the LiteLLM gateway directly.",
                "invalid_model",
            )

    def _serve_auto(self, path: str, payload: dict[str, Any]) -> None:
        avail = self.health.snapshot()
        gxmax_busy = self.lifecycle.status().state is State.ACQUIRING
        decision = route(payload, available=avail, busy={Tier.MAX: gxmax_busy})

        # Every routing decision is logged, as required by the spec.
        logging.getLogger(ROUTING_LOG).info(json.dumps(decision.as_log_dict()))

        if decision.tier is Tier.MAX:
            # gx-auto chose gx-max. Acquisition is allowed, but if it fails we
            # degrade (unlike a DIRECT gx-max request, which must not).
            try:
                self.lifecycle.acquire()
            except AcquisitionError as exc:
                log.warning("gx-auto wanted gx-max but acquisition failed: %s", exc)
                fallback = route(payload, available=avail, busy={Tier.MAX: True})
                logging.getLogger(ROUTING_LOG).info(
                    json.dumps({**fallback.as_log_dict(), "note": "gx-max acquisition failed"})
                )
                decision = fallback
            else:
                self._proxy(
                    f"{self.cfg.gxmax_base.rstrip('/')}/chat/completions",
                    {**payload, "model": self.cfg.gxmax_model_id},
                    routed_as=Tier.MAX,
                )
                self.lifecycle.mark_used()
                return

        upstream = f"{self.cfg.gateway_base.rstrip('/')}{path[len('/v1'):]}"
        self._proxy(
            f"{self.cfg.gateway_base.rstrip('/')}/chat/completions"
            if path == "/v1/chat/completions"
            else upstream,
            {**payload, "model": decision.tier.value},
            routed_as=decision.tier,
            headers=self._auth_headers(),
        )

    def _serve_gx_max(self, path: str, payload: dict[str, Any], *, direct: bool) -> None:
        """Serve a DIRECT gx-max request. Never downgrades -- fails loudly instead."""
        try:
            self.lifecycle.acquire()
        except AcquisitionError as exc:
            # Explicit, per the locked architecture: a direct gx-max request may
            # not be silently served by a smaller model.
            self._send_error_json(
                503,
                f"gx-max could not be brought up and will NOT be substituted with "
                f"another model: {exc}",
                "gx_max_unavailable",
            )
            return

        self._proxy(
            f"{self.cfg.gxmax_base.rstrip('/')}/chat/completions",
            {**payload, "model": self.cfg.gxmax_model_id},
            routed_as=Tier.MAX,
        )
        self.lifecycle.mark_used()

    # ------------------------------------------------------------------ proxy
    def _proxy(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        routed_as: Tier,
        headers: dict[str, str] | None = None,
    ) -> None:
        streaming = bool(payload.get("stream"))
        try:
            if streaming:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-GX-Routed-To", routed_as.value)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in stream_post(
                    url, payload, headers=headers, timeout=self.cfg.upstream_timeout
                ):
                    self.wfile.write(f"{len(chunk):X}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                return

            resp = post_json(url, payload, headers=headers, timeout=self.cfg.upstream_timeout)
            body = resp.body
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-GX-Routed-To", routed_as.value)
            self.end_headers()
            self.wfile.write(body)
        except UpstreamError as exc:
            log.error("upstream %s failed: %s", url, exc)
            self._send_error_json(exc.status, exc.body, "upstream_error")
        except Exception as exc:  # noqa: BLE001
            log.exception("proxy to %s failed", url)
            self._send_error_json(502, f"proxy failure: {exc!r}", "bad_gateway")


def build_servers(cfg: Config | None = None) -> tuple[list[ThreadingHTTPServer], GxMaxLifecycle]:
    """Build one HTTP server per configured bind address.

    A single lifecycle manager is SHARED across all binds: gx-max acquisition
    must be serialised globally, not per listening socket.
    """
    cfg = cfg or CONFIG
    lifecycle = GxMaxLifecycle(
        cfg.lifecycle_dir,
        health_url=f"{cfg.gxmax_base.rstrip('/').removesuffix('/v1')}/health",
        idle_ttl=cfg.gxmax_idle_ttl,
        acquire_timeout=cfg.gxmax_acquire_timeout,
    )
    handler = type(
        "BoundHandler",
        (Handler,),
        {"cfg": cfg, "lifecycle": lifecycle, "health": TierHealth(cfg, lifecycle)},
    )
    servers: list[ThreadingHTTPServer] = []
    for host in cfg.hosts:
        try:
            httpd = ThreadingHTTPServer((host, cfg.port), handler)
        except OSError as exc:
            # A missing docker bridge must not stop the orchestrator booting.
            log.warning("cannot bind %s:%s (%s); skipping this address", host, cfg.port, exc)
            continue
        httpd.daemon_threads = True
        servers.append(httpd)
    if not servers:
        raise RuntimeError(f"could not bind any of {cfg.hosts!r} on port {cfg.port}")
    return servers, lifecycle


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    cfg = CONFIG
    servers, lifecycle = build_servers(cfg)
    for srv in servers:
        log.info("gx-orchestrator listening on http://%s:%s", *srv.server_address[:2])
    log.info("  gateway=%s  gx-max=%s  idle_ttl=%ss", cfg.gateway_base, cfg.gxmax_base, cfg.gxmax_idle_ttl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{s.server_address[0]}") for s in servers]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        lifecycle.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
