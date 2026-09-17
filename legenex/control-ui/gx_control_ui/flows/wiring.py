"""Builds ``app.flows`` from the Control Center's existing service objects."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .. import netguard
from .ffmpeg import FFmpegRunner
from .services import GatewayLLM, SecretStore, Services
from .store import FlowStore


def build_flows(app: Any) -> Any:
    from . import FlowService

    cfg = app.cfg

    def model_identity(alias: str) -> str:
        """Changes when the registry entry of an alias changes (cache invalidation)."""
        try:
            spec = (app.manager.registry().get("aliases") or {}).get(alias) or {}
        except Exception:  # noqa: BLE001 - identity is best effort; the cache key still has the node config
            return ""
        comps = [{k: c.get(k) for k in ("repository", "revision", "file")} for c in spec.get("components") or []]
        return hashlib.sha256(json.dumps(comps, sort_keys=True).encode()).hexdigest()[:16]

    def metric(event: str, **fields: Any) -> None:
        try:
            from ..obs import metric as emit
        except Exception:  # noqa: BLE001 - metrics are optional
            return
        emit(event, **fields)

    def voice() -> Any:
        return getattr(app, "voice", None)

    def voice_available() -> str | None:
        if getattr(app, "voice", None) is None:
            return "gx-voice is not installed on this Control Center yet"
        return None

    services = Services(
        library=app.library,
        store=FlowStore(app.library.connect),
        llm=GatewayLLM(cfg.litellm_base, app.cluster.litellm_headers, app.cluster.gxmax_state),
        ffmpeg=FFmpegRunner(app.library.root / "tmp", enabled=not cfg.offline),
        secrets=SecretStore(cfg.secret_dir.parent / "flows" / "http-secrets.json"),
        media=app.media,
        music=app.music,
        voice=voice,
        fetch=netguard.fetch,
        explain=lambda alias: app.resources.explain(alias),
        image_catalog=getattr(app, "image_catalog", None),
        wan=getattr(app, "wan", None),
        model_identity=model_identity,
        metric=metric,
    )
    return FlowService(services, audit=app.actions.audit, voice_available=voice_available)
