"""Entry point: python -m gx_media_router"""

from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from .comfy import ComfyClient
from .config import Config
from .server import build_server
from .service import MediaService
from .workflows import WorkflowRegistry

log = logging.getLogger("gx-media")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    cfg = Config.from_env()
    if not cfg.api_key:
        log.warning(
            "GX_MEDIA_API_KEY is not set: authentication is DISABLED. "
            "Do not run this way on a routable address."
        )
    workflows = WorkflowRegistry(cfg.workflow_dir)
    service = MediaService(cfg, ComfyClient(cfg.comfy_url, connect_timeout=cfg.comfy_connect_timeout), workflows)
    server = build_server(cfg, service)

    def shutdown(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %s received, shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.info(
        "gx-media-router listening on %s:%s -> comfyui %s | workflows: %s | auth: %s",
        cfg.bind_host, cfg.bind_port, cfg.comfy_url,
        ", ".join(workflows.names()), "on" if cfg.api_key else "OFF",
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
