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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .classifier import route
from .config import CONFIG, Config
from .health import AliasState, TierHealth, TierStatus
from .lifecycle import AcquisitionError, GxMaxLifecycle, LifecycleStatus, State
from .tiers import TIERS, ROUTABLE, Tier
from .upstream import UpstreamError, post_json, stream_post

log = logging.getLogger("gx.server")

ROUTING_LOG = "gx.routing"

#: How lifecycle.State maps onto the shared AliasState vocabulary (see
#: health.py). RELEASING has no exact match in that six-word vocabulary; it is
#: reported as LOADING (a transition in progress) rather than inventing a
#: seventh state just for gx-max.
_MAX_STATE_MAP: dict[State, AliasState] = {
    State.DOWN: AliasState.STOPPED,
    State.ACQUIRING: AliasState.QUEUED,
    State.READY: AliasState.READY,
    State.RELEASING: AliasState.LOADING,
}


def _gx_max_admission_blocked() -> str:
    """Return a reason string if gx-max could not be admitted from here, else "".

    gx-max is the one tier whose availability is not only a question of
    whether its process is running -- it is also whether it is allowed to
    start. Reporting `usable: true` for a tier that would be refused is a fake
    healthy state, which this project forbids (D-024).

    Since D-025 gx-max uses the cluster-takeover policy, not
    estimate+reserve. This probe checks the preconditions that DRAINING
    CANNOT FIX on node 1 -- /swapfile-sglang active, swap headroom for the
    load transient, no pre-existing memory pressure. It deliberately does not
    demand the drained-state MemAvailable: while gx-mini/gx-fast are loaded
    the node is not drained, and gx-max-start.sh drains before it judges
    that. The full two-node check runs, under both locks, at launch.

    Read-only, takes no lock, and a probe failure returns "" rather than
    inventing a fault.
    """
    try:
        from . import resource_guard as rg

        facts = rg.read_node_facts()
        result = rg.compute_takeover_admission(
            "node1",
            facts,
            other_exclusive_residents=[],
            policy=rg.TakeoverPolicy(clean_start_min_avail_gib=0.0),
        )
        return "" if result.allowed else f"admission_refused: {result.reason}"
    except Exception:  # noqa: BLE001 - never let a probe failure fake a fault
        log.debug("gx-max admission probe failed; reporting the lifecycle state as-is",
                  exc_info=True)
        return ""


def _max_tier_status(
    st: LifecycleStatus,
    node2_status: TierStatus,
    *,
    admission_blocked: "Callable[[], str] | None" = None,
) -> TierStatus:
    """Derive gx-max's TierStatus from its lifecycle state.

    This does NOT change gx-max's lifecycle semantics (DOWN stays a valid
    resting state, never a fault) -- it only reports that state through the
    same vocabulary as the other tiers, and folds in node 2 reachability so a
    human reading `gx status` sees "would need node 2, which is offline"
    instead of a bare false. `usable` is preserved EXACTLY from the original
    logic: READY, DOWN and ACQUIRING are all routable (gx-auto/direct gx-max
    may attempt acquisition from any of them); RELEASING is not.
    """
    alias_state = _MAX_STATE_MAP[st.state]
    usable = st.state in (State.READY, State.DOWN, State.ACQUIRING)

    if st.state is State.DOWN and node2_status.state is AliasState.UNAVAILABLE:
        # gx-max needs BOTH nodes (ARCHITECTURE.md L-2/L-6). DOWN is still a
        # valid resting state, but a human asking why it would fail right now
        # deserves the real reason, not a bare boolean. Checked BEFORE
        # admission: if node 2 is confirmed offline, that is the more
        # actionable answer, and the admission arithmetic is moot anyway.
        return TierStatus(alias_state, "node2_unavailable", usable=usable)

    if st.state is State.DOWN:
        # A DOWN gx-max still has to pass the admission guard to become READY.
        # If it cannot, say so, and do not call it usable.
        blocked = (admission_blocked or _gx_max_admission_blocked)()
        if blocked:
            return TierStatus(AliasState.UNAVAILABLE, blocked, usable=False)
    if st.last_error:
        return TierStatus(alias_state, st.last_error, usable=usable)
    return TierStatus(alias_state, st.detail or alias_state.value, usable=usable)


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
            tiers = self.health.snapshot()
            max_status = _max_tier_status(st, tiers[Tier.REASON])
            tiers_out = {t.value: v.as_dict() for t, v in tiers.items()}
            tiers_out[Tier.MAX.value] = max_status.as_dict()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "gx_max": st.as_dict(),
                    "tiers": tiers_out,
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

        if path == "/lifecycle/gx-max/events":
            # Read-only: recent lifecycle script output, the job in progress
            # and finished jobs. Changes nothing, starts nothing.
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                after = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                self._send_error_json(400, "after and limit must be integers", "invalid_request")
                return
            self._send_json(200, self.lifecycle.events(after=after, limit=limit))
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
        tiers = self.health.snapshot()
        lc_status = self.lifecycle.status()
        max_status = _max_tier_status(lc_status, tiers[Tier.REASON])

        # route() takes a plain tier->bool map; richer state (why a tier is
        # down, whether it is loading vs. genuinely failed) lives in `tiers`
        # and `max_status` for /health/detailed and `gx status`, not here.
        avail = {t: v.usable for t, v in tiers.items()}
        avail[Tier.MAX] = max_status.usable
        gxmax_busy = lc_status.state is State.ACQUIRING
        decision = route(payload, available=avail, busy={Tier.MAX: gxmax_busy})

        # Every routing decision is logged, as required by the spec.
        logging.getLogger(ROUTING_LOG).info(json.dumps(decision.as_log_dict()))

        if decision.tier is Tier.MAX:
            # gx-auto may USE gx-max when it is already up. It must never
            # ACQUIRE it (human requirement, 2026-09-16).
            #
            # Acquiring gx-max is not a cheap operation that happens to fail:
            # gx-max-start.sh drains gx-mini, gx-fast and llama-swap on BOTH
            # nodes before it does anything else, because gx-max takes over the
            # whole cluster. Letting an ordinary gx-auto request trigger that is
            # wrong even when it succeeds, and measurably disruptive when it
            # does not -- observed live on 2026-09-16: a single gx-auto prompt
            # containing the word "exhaustive" tore down node 1's resident
            # models and both llama-swaps, was refused by the admission guard,
            # and spent ~12 s putting everything back.
            #
            # Taking over both nodes is a deliberate, operator-initiated act. It
            # stays on the DIRECT gx-max path (_serve_gx_max) and the explicit
            # /lifecycle/gx-max/acquire endpoint, where a human asked for it.
            if lc_status.state is State.READY:
                self._proxy(
                    f"{self.cfg.gxmax_base.rstrip('/')}/chat/completions",
                    {**payload, "model": self.cfg.gxmax_model_id},
                    routed_as=Tier.MAX,
                )
                self.lifecycle.mark_used()
                return
            log.info(
                "gx-auto selected gx-max but it is %s; routing to the best available "
                "tier instead (gx-auto never acquires gx-max)",
                lc_status.state.value,
            )
            fallback = route(payload, available=avail, busy={Tier.MAX: True})
            logging.getLogger(ROUTING_LOG).info(
                json.dumps({
                    **fallback.as_log_dict(),
                    "note": f"gx-max not running ({lc_status.state.value}); "
                            "gx-auto does not acquire it",
                })
            )
            decision = fallback

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
        events_log=cfg.log_dir / "gx-max-lifecycle.log",
        history_path=cfg.state_dir / "orchestrator" / "gx-max-history.json",
    )
    handler = type(
        "BoundHandler",
        (Handler,),
        {"cfg": cfg, "lifecycle": lifecycle, "health": TierHealth(cfg)},
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
