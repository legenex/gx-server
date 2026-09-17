"""python -m gx_live_engine -- the MiniCPM-o 4.5 engine process (container entrypoint)."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from . import __version__
from .model import ModelRuntime
from .server import State, build

log = logging.getLogger("gx_live_engine")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}')
    key = os.environ.get("GX_LIVE_ENGINE_KEY", "")
    if len(key) < 32:
        log.error("GX_LIVE_ENGINE_KEY is missing or too short")
        return 78
    host = os.environ.get("GX_LIVE_ENGINE_HOST", "127.0.0.1")
    port = int(os.environ.get("GX_LIVE_ENGINE_PORT", "18851"))
    runtime = ModelRuntime(os.environ.get("GX_LIVE_MODEL_DIR", "/models/model"))
    state = State(runtime, key)
    server = build(host, port, state)

    def load() -> None:
        try:
            runtime.load()
        except Exception as exc:  # noqa: BLE001
            log.exception("model load failed")
            state.error = f"{type(exc).__name__}"
        finally:
            state.loading = False

    threading.Thread(target=load, name="model-load", daemon=True).start()

    def stop(signum, _frame):  # noqa: ANN001
        log.info("signal %s: stopping", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("gx-live engine %s listening on %s:%s", __version__, host, port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
