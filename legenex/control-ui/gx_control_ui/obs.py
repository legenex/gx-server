"""Control Center access to the shared metrics helper (legenex/common/gxcommon).

``metric(event, **fields)`` writes one JSON line to stdout (the service log)
and to ``$GX_METRICS_DIR/gx-control-ui.jsonl`` (the Playground Logs page reads
it back per user). Offline test runs write to stdout only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

_COMMON = Path(__file__).resolve().parents[2] / "common"
if _COMMON.is_dir() and str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from gxcommon.metrics import Metrics, clean_fields, read_metrics  # noqa: E402

__all__ = ["METRICS", "clean_fields", "metric", "read_metrics", "metrics_files"]


def _metrics_dir() -> Path:
    return Path(os.environ.get("GX_METRICS_DIR", "/srv/logs/gx-metrics"))


METRICS = Metrics(
    "gx-control-ui", node="gx10-01",
    file=None if os.environ.get("GX_UI_OFFLINE") == "1" else _metrics_dir() / "gx-control-ui.jsonl",
)


def metric(event: str, /, **fields: Any) -> None:
    METRICS.emit(event, **fields)


def metrics_files(directory: Path | None = None) -> list[Path]:
    d = directory or _metrics_dir()
    try:
        return sorted(p for p in d.glob("*.jsonl") if p.is_file())
    except OSError:
        return []
