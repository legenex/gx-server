"""python3 -m gx_call  -- run the supervisor."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from . import __version__, config
from .engine import EngineController
from .server import build_servers, serve_forever
from .service import CallService


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def _logging() -> None:
    log_dir = Path(os.environ.get("GX_CALL_LOG_DIR", "/srv/logs/gx-call"))
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / "gx-call.log", maxBytes=20 << 20, backupCount=5)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)
    acc = logging.getLogger("gx_call.access")
    acc.propagate = False
    ah = logging.handlers.RotatingFileHandler(log_dir / "access.log", maxBytes=20 << 20, backupCount=3)
    ah.setFormatter(logging.Formatter("%(message)s"))
    acc.addHandler(ah)


def _metrics(cfg: config.Config):  # noqa: ANN202
    if str(cfg.common_dir) not in sys.path:
        sys.path.insert(0, str(cfg.common_dir))
    try:
        from gxcommon.metrics import Metrics  # noqa: PLC0415
        from gxcommon.node2_tenants import PeerTenants  # noqa: PLC0415
    except ImportError:
        logging.getLogger("gx_call").warning("legenex/common not importable; metrics and peer accounting off")
        return None, None
    return (Metrics("gx-call", node="gx10-02", file=cfg.metrics_file or None),
            PeerTenants.from_env(exclude="gx-call"))


def main() -> int:
    _logging()
    log = logging.getLogger("gx_call")
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001
        log.error("configuration error: %s", exc)
        return 78
    metrics, peers = _metrics(cfg)
    engine = EngineController(cfg, peers=peers, metrics=metrics)
    service = CallService(cfg, engine, metrics=metrics)
    servers = build_servers(service, cfg.api_key, cfg.binds, cfg.port)
    service.start()

    def _term(signum, _frame):  # noqa: ANN001
        log.info("signal %s: stopping", signum)
        service.stop()
        if os.environ.get("GX_CALL_KEEP_ENGINE_ON_EXIT", "0") != "1" and engine.docker.exists(cfg.engine_container):
            engine.unload("supervisor stopping", kind="shutdown")
        for s in servers:
            s.shutdown()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    log.info("gx-call %s listening on %s port %s (model %s @ %s)", __version__, ",".join(cfg.binds), cfg.port,
             cfg.model.repo, cfg.model.revision[:12])
    serve_forever(servers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
