"""Environment-driven configuration for the gx-call supervisor. Validated once."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from .errors import CallError


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise CallError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise CallError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise CallError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise CallError(f"{name}={value} outside [{lo}, {hi}]")
    return value


@dataclasses.dataclass(frozen=True)
class ModelIdentity:
    repo: str
    revision: str
    backbone_repo: str
    backbone_revision: str
    runtime_repo: str
    runtime_ref: str
    image: str
    voice: str

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
    hf_cache_dir: Path
    state_dir: Path
    data_dir: Path
    guard_dir: Path
    orchestrator_dir: Path
    common_dir: Path
    engine_container: str
    engine_port: int
    engine_key_file: Path
    engine_estimate_gib: float
    engine_memory_cap: str
    engine_start_timeout: int
    engine_chunk_s: float
    idle_unload_s: int
    resource_wait_s: int
    resource_retry_s: int
    max_session_s: int
    session_ttl_s: int
    join_window_s: int
    rejoin_window_s: int
    tool_timeout_s: float
    max_sessions_pending: int
    gxmax_hold_file: Path
    maintenance_hold_file: Path
    pins_file: Path
    gxmax_hold_ttl_s: int
    gxmax_rank_container: str
    gxmax_deadman_pidfile: Path
    control_plane_container: str
    reserve_gib: float
    metrics_file: str

    @property
    def recordings_dir(self) -> Path:
        return self.data_dir / "recordings"


def _read_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise CallError(f"cannot read API key file {path}: {exc.strerror}") from exc
    if len(key) < 32:
        raise CallError(f"API key in {path} is shorter than 32 characters; refusing to run")
    return key


def load() -> Config:
    secrets = Path(_env("GX_CALL_SECRETS_DIR", "/srv/projects/gx-cluster/secrets/gx-call"))
    here = Path(__file__).resolve().parent
    binds = tuple(b for b in _env("GX_CALL_BINDS", "127.0.0.1,192.168.100.11").split(",") if b)
    if not binds:
        raise CallError("GX_CALL_BINDS is empty")
    if any(b in {"0.0.0.0", "::"} for b in binds):
        raise CallError("GX_CALL_BINDS must not contain a wildcard address")
    model = ModelIdentity(
        repo=_env("GX_CALL_MODEL_REPO", "nvidia/NVIDIA-NemotronLabs-VoiceChat-11B"),
        revision=_env("GX_CALL_MODEL_REVISION", "a4c40ca5b4fe77db13e9840ca4a2b91becf030c8"),
        backbone_repo="nvidia/NVIDIA-Nemotron-Nano-9B-v2",
        backbone_revision=_env("GX_CALL_BACKBONE_REVISION", "6533e8de2c68e4536bf7c411d7a3ce5734111476"),
        runtime_repo="https://github.com/NVIDIA-NeMo/Speech (branch nemotron-labs-voicechat)",
        runtime_ref=_env("GX_CALL_RUNTIME_REF", "097dfe9e2f55baf653b83035868bdc89849f1b47"),
        image=_env("GX_CALL_IMAGE", "gx-call-engine:voicechat-097dfe9-t214"),
        voice=_env("GX_CALL_VOICE", "Aria"),
    )
    return Config(
        binds=binds,
        port=_int("GX_CALL_PORT", 18840, 1024, 65535),
        api_key=_read_key(Path(_env("GX_CALL_API_KEY_FILE", str(secrets / "api-key")))),
        node=_env("GX_CALL_NODE", "node2"),
        model=model,
        model_dir=Path(_env("GX_CALL_MODEL_DIR", "/srv/models/voicechat/NVIDIA-NemotronLabs-VoiceChat-11B")),
        hf_cache_dir=Path(_env("GX_CALL_HF_CACHE", "/srv/models/voicechat/hf-cache")),
        state_dir=Path(_env("GX_CALL_STATE_DIR", "/srv/projects/gx-cluster/state/gx-call")),
        data_dir=Path(_env("GX_CALL_DATA_DIR", "/srv/models/voicechat/call-data")),
        guard_dir=Path(_env("GX_GUARD_STATE_DIR", "/srv/projects/gx-cluster/state/guard")),
        orchestrator_dir=Path(_env("GX_CALL_ORCHESTRATOR_DIR", str(here.parent.parent / "orchestrator"))),
        common_dir=Path(_env("GX_CALL_COMMON_DIR", str(here.parent.parent / "common"))),
        engine_container=_env("GX_CALL_ENGINE_CONTAINER", "gx-call-engine"),
        engine_port=_int("GX_CALL_ENGINE_PORT", 18841, 1024, 65535),
        engine_key_file=secrets / "engine-key",
        engine_estimate_gib=_float("GX_CALL_ENGINE_ESTIMATE_GIB", 48.0, 8.0, 110.0),
        engine_memory_cap=_env("GX_CALL_ENGINE_MEMORY_CAP", "72g"),
        engine_start_timeout=_int("GX_CALL_ENGINE_START_TIMEOUT", 1200, 60, 7200),
        engine_chunk_s=_float("GX_CALL_CHUNK_S", 0.08, 0.08, 0.48),
        idle_unload_s=_int("GX_CALL_IDLE_UNLOAD_S", 900, 0, 86400),
        resource_wait_s=_int("GX_CALL_RESOURCE_WAIT_S", 600, 0, 86400),
        resource_retry_s=_int("GX_CALL_RESOURCE_RETRY_S", 15, 2, 600),
        max_session_s=_int("GX_CALL_MAX_SESSION_S", 1800, 60, 14400),
        session_ttl_s=_int("GX_CALL_SESSION_TTL_S", 3600, 60, 14400),
        join_window_s=_int("GX_CALL_JOIN_WINDOW_S", 900, 30, 3600),
        rejoin_window_s=_int("GX_CALL_REJOIN_WINDOW_S", 60, 0, 600),
        tool_timeout_s=_float("GX_CALL_TOOL_TIMEOUT_S", 10.0, 1.0, 60.0),
        max_sessions_pending=_int("GX_CALL_MAX_PENDING", 8, 1, 100),
        gxmax_hold_file=Path(_env("GX_CALL_GXMAX_HOLD", "/srv/projects/gx-cluster/state/guard/node2.gxmax-hold")),
        maintenance_hold_file=Path(_env("GX_CALL_MAINTENANCE_HOLD",
                                        "/srv/projects/gx-cluster/state/guard/node2.maintenance-hold")),
        pins_file=Path(_env("GX_CALL_PINS_FILE", "/srv/projects/gx-cluster/state/guard/pins.json")),
        gxmax_hold_ttl_s=_int("GX_CALL_GXMAX_HOLD_TTL_S", 1200, 60, 86400),
        gxmax_rank_container=_env("GX_CALL_GXMAX_RANK", "gx-max-rank1"),
        gxmax_deadman_pidfile=Path(_env("GX_CALL_GXMAX_DEADMAN_PID",
                                        str(Path.home() / ".gx-guard" / "rank1-deadman.pid"))),
        control_plane_container=_env("GX_CALL_CONTROL_PLANE_CONTAINER", "gx-llama-swap-node02"),
        reserve_gib=_float("GX_GUARD_RESERVE_GIB", 30.0, 30.0, 120.0),
        metrics_file=_env("GX_METRICS_FILE", "/srv/logs/gx-call/metrics.jsonl"),
    )
