"""Environment-driven configuration, validated once at start-up."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from .errors import LiveError

REPO = Path(__file__).resolve().parents[3]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise LiveError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise LiveError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise LiveError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise LiveError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class ModelIdentity:
    alias: str
    repository: str
    revision: str
    runtime: str
    image: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Config:
    binds: tuple[str, ...]
    port: int
    api_key: str
    node: str
    model: ModelIdentity
    model_dir: Path
    state_dir: Path
    guard_dir: Path
    orchestrator_dir: Path
    common_dir: Path
    engine_dir: Path
    engine_container: str
    engine_port: int
    engine_key_file: Path
    engine_estimate_gib: float
    engine_memory_cap: str
    engine_start_timeout: int
    idle_unload_s: int
    resource_wait_s: int
    resource_retry_s: int
    session_max_s: int
    attach_timeout_s: int
    reconnect_grace_s: int
    tool_timeout_s: int
    delegate_timeout_s: int
    max_pending_tools: int
    gxmax_hold_file: Path
    maintenance_hold_file: Path
    pins_file: Path
    gxmax_hold_ttl_s: int
    gxmax_rank_container: str
    gxmax_deadman_pidfile: Path
    control_plane_container: str
    reserve_gib: float
    metrics_file: str | None
    log_dir: Path

    @property
    def workload(self) -> str:
        """Residency-ledger key: the container name (plt.md section 5.1)."""
        return self.engine_container


def _read_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise LiveError(f"cannot read API key file {path}: {exc.strerror}") from exc
    if len(key) < 32:
        raise LiveError(f"API key in {path} is shorter than 32 characters; refusing to run")
    return key


def load() -> Config:
    secrets = Path(_env("GX_LIVE_SECRETS_DIR", "/srv/projects/gx-cluster/secrets/gx-live"))
    key_file = Path(_env("GX_LIVE_API_KEY_FILE", str(secrets / "api-key")))
    binds = tuple(b.strip() for b in _env("GX_LIVE_BINDS", "127.0.0.1,192.168.100.11").split(",") if b.strip())
    if not binds:
        raise LiveError("GX_LIVE_BINDS is empty")
    if any(b in {"0.0.0.0", "::"} for b in binds):
        raise LiveError("GX_LIVE_BINDS must not contain a wildcard address")
    model = ModelIdentity(
        alias="gx-live",
        repository=_env("GX_LIVE_MODEL_REPO", "openbmb/MiniCPM-o-4_5"),
        revision=_env("GX_LIVE_MODEL_REVISION", "503e754207c94da6bb26850b4469f367c9ea3582"),
        runtime="transformers 4.51.0 remote code (MiniCPMO), torch 2.14.0+cu130, SDPA, bfloat16",
        image=_env("GX_LIVE_IMAGE", "gx-live-engine:minicpmo45-503e754-t214"),
    )
    return Config(
        binds=binds,
        port=_int("GX_LIVE_PORT", 18850, 1024, 65535),
        api_key=_read_key(key_file),
        node=_env("GX_LIVE_NODE", "node2"),
        model=model,
        model_dir=Path(_env("GX_LIVE_MODEL_DIR", "/srv/models/live/MiniCPM-o-4_5")),
        state_dir=Path(_env("GX_LIVE_STATE_DIR", "/srv/projects/gx-cluster/state/gx-live")),
        guard_dir=Path(_env("GX_GUARD_STATE_DIR", "/srv/projects/gx-cluster/state/guard")),
        orchestrator_dir=Path(_env("GX_LIVE_ORCHESTRATOR_DIR", str(REPO / "legenex" / "orchestrator"))),
        common_dir=Path(_env("GX_LIVE_COMMON_DIR", str(REPO / "legenex" / "common"))),
        engine_dir=Path(_env("GX_LIVE_ENGINE_DIR", str(REPO / "legenex" / "live" / "engine"))),
        engine_container=_env("GX_LIVE_ENGINE_CONTAINER", "gx-live-engine"),
        engine_port=_int("GX_LIVE_ENGINE_PORT", 18851, 1024, 65535),
        engine_key_file=secrets / "engine-key",
        # Measured 2026-09-17 (coordination/build-v3/liv.md FOOTPRINT): the
        # admission estimate covers the load transient plus a live session.
        engine_estimate_gib=_float("GX_LIVE_ENGINE_ESTIMATE_GIB", 34.0, 8.0, 110.0),
        engine_memory_cap=_env("GX_LIVE_ENGINE_MEMORY_CAP", "48g"),
        engine_start_timeout=_int("GX_LIVE_ENGINE_START_TIMEOUT", 900, 60, 3600),
        idle_unload_s=_int("GX_LIVE_IDLE_UNLOAD_S", 600, 0, 86400),
        resource_wait_s=_int("GX_LIVE_RESOURCE_WAIT_S", 1800, 0, 86400),
        resource_retry_s=_int("GX_LIVE_RESOURCE_RETRY_S", 15, 2, 600),
        session_max_s=_int("GX_LIVE_SESSION_MAX_S", 4 * 3600, 60, 4 * 3600),
        attach_timeout_s=_int("GX_LIVE_ATTACH_TIMEOUT_S", 180, 10, 3600),
        reconnect_grace_s=_int("GX_LIVE_RECONNECT_GRACE_S", 60, 5, 3600),
        tool_timeout_s=_int("GX_LIVE_TOOL_TIMEOUT_S", 60, 5, 600),
        delegate_timeout_s=_int("GX_LIVE_DELEGATE_TIMEOUT_S", 660, 30, 3600),
        max_pending_tools=_int("GX_LIVE_MAX_PENDING_TOOLS", 4, 1, 32),
        gxmax_hold_file=Path(_env("GX_LIVE_GXMAX_HOLD", "/srv/projects/gx-cluster/state/guard/node2.gxmax-hold")),
        maintenance_hold_file=Path(_env("GX_LIVE_MAINTENANCE_HOLD",
                                        "/srv/projects/gx-cluster/state/guard/node2.maintenance-hold")),
        pins_file=Path(_env("GX_LIVE_PINS_FILE", "/srv/projects/gx-cluster/state/guard/pins.json")),
        gxmax_hold_ttl_s=_int("GX_LIVE_GXMAX_HOLD_TTL_S", 1200, 60, 86400),
        gxmax_rank_container=_env("GX_LIVE_GXMAX_RANK", "gx-max-rank1"),
        gxmax_deadman_pidfile=Path(_env("GX_LIVE_GXMAX_DEADMAN_PID",
                                        str(Path.home() / ".gx-guard" / "rank1-deadman.pid"))),
        control_plane_container=_env("GX_LIVE_CONTROL_PLANE_CONTAINER", "gx-llama-swap-node02"),
        reserve_gib=_float("GX_GUARD_RESERVE_GIB", 30.0, 30.0, 120.0),
        metrics_file=_env("GX_METRICS_FILE", "/srv/logs/gx-live/metrics.jsonl") or None,
        log_dir=Path(_env("GX_LIVE_LOG_DIR", "/srv/logs/gx-live")),
    )
