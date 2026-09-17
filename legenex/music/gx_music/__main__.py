"""python3 -m gx_music  — run the supervisor."""

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
from .service import MusicService
from .store import Store


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"), "level": record.levelname,
               "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def _logging() -> None:
    log_dir = Path(os.environ.get("GX_MUSIC_LOG_DIR", "/srv/logs/gx-music"))
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_dir / "gx-music.log", maxBytes=20 << 20, backupCount=5)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(sh)
    acc = logging.getLogger("gx_music.access")
    acc.propagate = False
    ah = logging.handlers.RotatingFileHandler(log_dir / "access.log", maxBytes=20 << 20, backupCount=3)
    ah.setFormatter(logging.Formatter("%(message)s"))
    acc.addHandler(ah)


def main() -> int:
    _logging()
    log = logging.getLogger("gx_music")
    try:
        cfg = config.load()
    except Exception as exc:  # noqa: BLE001
        log.error("configuration error: %s", exc)
        return 78
    store = Store(cfg.db_path)
    engine = EngineController(cfg)
    service = MusicService(cfg, store, engine)
    servers = build_servers(service, cfg.api_key, cfg.binds, cfg.port)
    service.start()

    def _term(signum, _frame):  # noqa: ANN001
        log.info("signal %s: stopping", signum)
        service.stop()
        if os.environ.get("GX_MUSIC_KEEP_ENGINE_ON_EXIT", "0") != "1" and engine.docker.exists(cfg.engine_container):
            # Never leave an unowned model resident on node 2.
            engine.unload("supervisor stopping")
        for s in servers:
            s.shutdown()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    log.info("gx-music %s listening on %s port %s (model %s @ %s)", __version__, ",".join(cfg.binds),
             cfg.port, cfg.model.dit_name, cfg.model.dit_revision[:12])
    serve_forever(servers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
