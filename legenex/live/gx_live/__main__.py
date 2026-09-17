"""python3 -m gx_live -- run the gx-live supervisor (gx10-02)."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

from . import __version__, config


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def _logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / "gx-live.log", maxBytes=20 << 20, backupCount=5)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)
    acc = logging.getLogger("gx_live.access")
    acc.propagate = False
    ah = logging.handlers.RotatingFileHandler(log_dir / "access.log", maxBytes=20 << 20, backupCount=3)
    ah.setFormatter(logging.Formatter("%(message)s"))
    acc.addHandler(ah)


def main() -> int:
    log_dir = Path(os.environ.get("GX_LIVE_LOG_DIR", "/srv/logs/gx-live"))
    _logging(log_dir)
    log = logging.getLogger("gx_live")
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001
        log.error("configuration error: %s", exc)
        return 78
    if str(cfg.common_dir) not in sys.path:
        sys.path.insert(0, str(cfg.common_dir))
    from gxcommon import rtws  # noqa: PLC0415
    from gxcommon.metrics import Metrics  # noqa: PLC0415

    from .engine import EngineController  # noqa: PLC0415
    from .server import build_servers, serve_forever  # noqa: PLC0415
    from .service import LiveService  # noqa: PLC0415

    metrics = Metrics("gx-live", node="gx10-02", file=cfg.metrics_file)
    engine = EngineController(cfg, metrics=metrics)
    service = LiveService(cfg, engine, metrics=metrics)
    servers = build_servers(service, cfg.api_key, cfg.binds, cfg.port, rtws)
    service.start()

    def _term(signum, _frame):  # noqa: ANN001
        log.info("signal %s: stopping", signum)
        service.stop()
        if os.environ.get("GX_LIVE_KEEP_ENGINE_ON_EXIT", "0") != "1" and engine.docker.exists(cfg.engine_container):
            engine.unload("shutdown: supervisor stopping")
        for s in servers:
            s.shutdown()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    log.info("gx-live %s listening on %s port %s (model %s @ %s)", __version__, ",".join(cfg.binds), cfg.port,
             cfg.model.repository, cfg.model.revision[:12])
    serve_forever(servers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
