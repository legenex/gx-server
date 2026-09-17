"""Environment-driven configuration. Validated once at startup."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from .errors import MusicError


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise MusicError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise MusicError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise MusicError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise MusicError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class ModelIdentity:
    """What is actually loaded. Revisions are pinned at download time."""

    dit_name: str
    dit_repo: str
    dit_revision: str
    lm_name: str
    lm_repo: str
    lm_revision: str
    shared_repo: str
    shared_revision: str
    runtime_repo: str
    runtime_ref: str
    image: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Config:
    binds: tuple[str, ...]
    port: int
    api_key: str
    data_root: Path
    checkpoints_dir: Path
    state_dir: Path
    guard_dir: Path
    orchestrator_dir: Path
    node: str
    model: ModelIdentity
    engine_container: str
    engine_port: int
    engine_key_file: Path
    engine_estimate_gib: float
    engine_memory_cap: str
    engine_lm_backend: str
    engine_start_timeout: int
    idle_unload_s: int
    resource_wait_s: int
    resource_retry_s: int
    generation_timeout_s: int
    max_upload_bytes: int
    max_queue: int
    max_duration_s: int
    evict_comfy: bool
    evict_reason: bool
    media_router_container: str
    media_router_url: str
    gxmax_hold_file: Path
    maintenance_hold_file: Path
    pins_file: Path
    gxmax_hold_ttl_s: int
    gxmax_rank_container: str
    gxmax_deadman_pidfile: Path
    control_plane_container: str
    reserve_gib: float
    #: python inside the engine image, used by the CPU-only analysis helper
    analysis_python: str = "/app/.venv/bin/python"

    @property
    def db_path(self) -> Path:
        return self.data_root / "db" / "gx-music.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_root / "uploads"

    @property
    def engine_tmp_host(self) -> Path:
        """Host side of the engine's TMPDIR (mounted at /work/tmp)."""
        return self.data_root


ENGINE_TMP = "/work/tmp"


def _read_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise MusicError(f"cannot read API key file {path}: {exc.strerror}") from exc
    if len(key) < 32:
        raise MusicError(f"API key in {path} is shorter than 32 characters; refusing to run")
    return key


def load() -> Config:
    secrets = Path(_env("GX_MUSIC_SECRETS_DIR", "/srv/projects/gx-cluster/secrets/gx-music"))
    key_file = Path(_env("GX_MUSIC_API_KEY_FILE", str(secrets / "api-key")))
    here = Path(__file__).resolve().parent
    binds = tuple(b for b in _env("GX_MUSIC_BINDS", "127.0.0.1,192.168.100.11").split(",") if b)
    if not binds:
        raise MusicError("GX_MUSIC_BINDS is empty")
    if any(b in {"0.0.0.0", "::"} for b in binds):
        raise MusicError("GX_MUSIC_BINDS must not contain a wildcard address")
    model = ModelIdentity(
        dit_name=_env("GX_MUSIC_DIT", "acestep-v15-xl-turbo"),
        dit_repo=_env("GX_MUSIC_DIT_REPO", "ACE-Step/acestep-v15-xl-turbo"),
        dit_revision=_env("GX_MUSIC_DIT_REVISION", "d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee"),
        lm_name=_env("GX_MUSIC_LM", "acestep-5Hz-lm-4B"),
        lm_repo=_env("GX_MUSIC_LM_REPO", "ACE-Step/acestep-5Hz-lm-4B"),
        lm_revision=_env("GX_MUSIC_LM_REVISION", "0a3ec94b557aea7d508da38b31cfe7341f6ff737"),
        shared_repo=_env("GX_MUSIC_SHARED_REPO", "ACE-Step/Ace-Step1.5"),
        shared_revision=_env("GX_MUSIC_SHARED_REVISION", "19671f406d603126926c1b7e2adc169acbcade22"),
        runtime_repo="https://github.com/ace-step/ACE-Step-1.5",
        runtime_ref=_env("GX_MUSIC_RUNTIME_REF", "ca1e85fe9430179831e6bc6be790c332190a3866"),
        image=_env("GX_MUSIC_IMAGE", "gx-music-engine:acestep15-ca1e85f-t214"),
    )
    lm_backend = _env("GX_MUSIC_LM_BACKEND", "vllm")
    if lm_backend not in {"vllm", "pt"}:
        raise MusicError("GX_MUSIC_LM_BACKEND must be vllm or pt")
    return Config(
        binds=binds,
        port=_int("GX_MUSIC_PORT", 18820, 1024, 65535),
        api_key=_read_key(key_file),
        data_root=Path(_env("GX_MUSIC_DATA_ROOT", "/srv/models/music-data")),
        checkpoints_dir=Path(_env("GX_MUSIC_CHECKPOINTS", "/srv/models/music/acestep/checkpoints")),
        state_dir=Path(_env("GX_MUSIC_STATE_DIR", "/srv/projects/gx-cluster/state/gx-music")),
        guard_dir=Path(_env("GX_GUARD_STATE_DIR", "/srv/projects/gx-cluster/state/guard")),
        orchestrator_dir=Path(_env("GX_MUSIC_ORCHESTRATOR_DIR", str(here.parent.parent / "orchestrator"))),
        node=_env("GX_MUSIC_NODE", "node2"),
        model=model,
        engine_container=_env("GX_MUSIC_ENGINE_CONTAINER", "gx-music"),
        engine_port=_int("GX_MUSIC_ENGINE_PORT", 18811, 1024, 65535),
        engine_key_file=secrets / "engine-key",
        engine_estimate_gib=_float("GX_MUSIC_ENGINE_ESTIMATE_GIB", 32.0, 4.0, 110.0),
        engine_memory_cap=_env("GX_MUSIC_ENGINE_MEMORY_CAP", "56g"),
        engine_lm_backend=lm_backend,
        engine_start_timeout=_int("GX_MUSIC_ENGINE_START_TIMEOUT", 900, 60, 7200),
        idle_unload_s=_int("GX_MUSIC_IDLE_UNLOAD_S", 600, 0, 86400),
        resource_wait_s=_int("GX_MUSIC_RESOURCE_WAIT_S", 1800, 0, 86400),
        resource_retry_s=_int("GX_MUSIC_RESOURCE_RETRY_S", 20, 2, 600),
        generation_timeout_s=_int("GX_MUSIC_GENERATION_TIMEOUT_S", 1800, 60, 14400),
        max_upload_bytes=_int("GX_MUSIC_MAX_UPLOAD_MB", 64, 1, 512) * 1024 * 1024,
        max_queue=_int("GX_MUSIC_MAX_QUEUE", 32, 1, 1000),
        max_duration_s=_int("GX_MUSIC_MAX_DURATION_S", 600, 10, 600),
        evict_comfy=_bool("GX_MUSIC_EVICT_COMFY_WEIGHTS", True),
        evict_reason=_bool("GX_MUSIC_EVICT_REASON", False),
        media_router_container=_env("GX_MUSIC_MEDIA_ROUTER_CONTAINER", "gx-media-router"),
        # D-038: the router's open /health publishes the growth of its running
        # job that MemAvailable does not show yet; admission subtracts it.
        media_router_url=_env("GX_MUSIC_MEDIA_ROUTER_URL", "http://192.168.100.11:18800").rstrip("/"),
        gxmax_hold_file=Path(_env("GX_MUSIC_GXMAX_HOLD", "/srv/projects/gx-cluster/state/guard/node2.gxmax-hold")),
        maintenance_hold_file=Path(_env("GX_MUSIC_MAINTENANCE_HOLD",
                                        "/srv/projects/gx-cluster/state/guard/node2.maintenance-hold")),
        pins_file=Path(_env("GX_MUSIC_PINS_FILE", "/srv/projects/gx-cluster/state/guard/pins.json")),
        gxmax_hold_ttl_s=_int("GX_MUSIC_GXMAX_HOLD_TTL_S", 1200, 60, 86400),
        gxmax_rank_container=_env("GX_MUSIC_GXMAX_RANK", "gx-max-rank1"),
        gxmax_deadman_pidfile=Path(_env("GX_MUSIC_GXMAX_DEADMAN_PID",
                                        str(Path.home() / ".gx-guard" / "rank1-deadman.pid"))),
        control_plane_container=_env("GX_MUSIC_CONTROL_PLANE_CONTAINER", "gx-llama-swap-node02"),
        # The locked normal-operation reserve: it may be raised, never lowered below 30 GiB.
        reserve_gib=_float("GX_GUARD_RESERVE_GIB", 30.0, 30.0, 120.0),
        analysis_python=_env("GX_MUSIC_ANALYSIS_PYTHON", "/app/.venv/bin/python"),
    )
