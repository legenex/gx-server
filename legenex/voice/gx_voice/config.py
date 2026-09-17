"""Environment-driven configuration, validated once at startup."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from .errors import VoiceError


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise VoiceError(f"{name} must be an integer, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise VoiceError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise VoiceError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise VoiceError(f"{name}={value} outside [{lo}, {hi}]")
    return value


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclasses.dataclass(frozen=True)
class Variant:
    """One Qwen3-TTS checkpoint the router can pick."""

    name: str          # custom | design | base
    repo: str
    revision: str
    directory: str     # folder name under models_dir
    role: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


VARIANTS: dict[str, Variant] = {
    "custom": Variant("custom", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                      "0c0e3051f131929182e2c023b9537f8b1c68adfe", "Qwen3-TTS-12Hz-1.7B-CustomVoice",
                      "preset voices with style and emotion instructions"),
    "design": Variant("design", "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                      "5ecdb67327fd37bb2e042aab12ff7391903235d3", "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                      "a new voice from a text description"),
    "base": Variant("base", "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                    "fd4b254389122332181a7c3db7f27e918eec64e3", "Qwen3-TTS-12Hz-1.7B-Base",
                    "voice cloning from reference audio (saved cloned and designed voices)"),
}
TOKENIZER = {"repo": "Qwen/Qwen3-TTS-Tokenizer-12Hz", "revision": "7dd38ad4e9bad454aae9cd937d0cd577604fe229",
             "sha256": "836b7b357f5ea43e889936a3709af68dfe3751881acefe4ecf0dbd30ba571258",
             "note": "bundled byte-identical as speech_tokenizer/ in all three variants"}
RUNTIME = {"repo": "https://github.com/QwenLM/Qwen3-TTS", "ref": "022e286b98fbec7e1e916cb940cdf532cd9f488e",
           "package": "qwen-tts 0.1.1", "torch": "2.14.0+cu130", "transformers": "4.57.3", "attention": "sdpa"}


@dataclasses.dataclass(frozen=True)
class Config:
    binds: tuple[str, ...]
    port: int
    api_key: str
    data_root: Path
    models_dir: Path
    state_dir: Path
    guard_dir: Path
    orchestrator_dir: Path
    node: str
    image: str
    engine_container: str
    engine_port: int
    engine_key_file: Path
    engine_estimate_gib: float
    engine_memory_cap: str
    engine_max_resident: int
    engine_start_timeout: int
    idle_unload_s: int
    resource_wait_s: int
    resource_retry_s: int
    utterance_timeout_s: int
    speech_timeout_s: int
    max_upload_bytes: int
    max_queue: int
    max_job_chars: int
    peer_health_urls: tuple[str, ...]
    evict_idle_peers: bool
    media_router_container: str
    music_url: str
    music_key_file: Path
    profile_file: Path
    gxmax_hold_file: Path
    maintenance_hold_file: Path
    pins_file: Path
    gxmax_hold_ttl_s: int
    gxmax_rank_container: str
    gxmax_deadman_pidfile: Path
    control_plane_container: str
    reserve_gib: float
    keep_engine_on_exit: bool

    @property
    def db_path(self) -> Path:
        return self.data_root / "db" / "gx-voice.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def references_dir(self) -> Path:
        return self.data_root / "references"

    @property
    def prompts_dir(self) -> Path:
        return self.data_root / "prompts"


#: Where the engine container sees the data root and the model folders.
ENGINE_WORK = "/work/data"
ENGINE_MODELS = "/models"


def read_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VoiceError(f"cannot read API key file {path}: {exc.strerror}") from exc
    if len(key) < 32:
        raise VoiceError(f"API key in {path} is shorter than 32 characters; refusing to run")
    return key


def load() -> Config:
    secrets = Path(_env("GX_VOICE_SECRETS_DIR", "/srv/projects/gx-cluster/secrets/gx-voice"))
    key_file = Path(_env("GX_VOICE_API_KEY_FILE", str(secrets / "api-key")))
    here = Path(__file__).resolve().parent
    binds = tuple(b for b in _env("GX_VOICE_BINDS", "127.0.0.1,192.168.100.11").split(",") if b)
    if not binds:
        raise VoiceError("GX_VOICE_BINDS is empty")
    if any(b in {"0.0.0.0", "::"} for b in binds):
        raise VoiceError("GX_VOICE_BINDS must not contain a wildcard address")
    peers = tuple(u.rstrip("/") for u in _env(
        "GX_VOICE_PEER_HEALTH", "http://192.168.100.11:18800,http://192.168.100.11:18820").split(",") if u)
    return Config(
        binds=binds,
        port=_int("GX_VOICE_PORT", 18830, 1024, 65535),
        api_key=read_key(key_file),
        data_root=Path(_env("GX_VOICE_DATA_ROOT", "/srv/models/voice-data")),
        models_dir=Path(_env("GX_VOICE_MODELS_DIR", "/srv/models/voice")),
        state_dir=Path(_env("GX_VOICE_STATE_DIR", "/srv/projects/gx-cluster/state/gx-voice")),
        guard_dir=Path(_env("GX_GUARD_STATE_DIR", "/srv/projects/gx-cluster/state/guard")),
        orchestrator_dir=Path(_env("GX_VOICE_ORCHESTRATOR_DIR", str(here.parent.parent / "orchestrator"))),
        node=_env("GX_VOICE_NODE", "node2"),
        image=_env("GX_VOICE_IMAGE", "gx-voice-engine:qwen3tts-022e286-t214"),
        engine_container=_env("GX_VOICE_ENGINE_CONTAINER", "gx-voice-engine"),
        engine_port=_int("GX_VOICE_ENGINE_PORT", 18831, 1024, 65535),
        engine_key_file=secrets / "engine-key",
        # Measured peak growth with one resident variant plus generation
        # (coordination/build-v3/voi.md §2); it can be raised, not lowered below 4.
        engine_estimate_gib=_float("GX_VOICE_ENGINE_ESTIMATE_GIB", 12.0, 4.0, 60.0),
        engine_memory_cap=_env("GX_VOICE_ENGINE_MEMORY_CAP", "32g"),
        engine_max_resident=_int("GX_VOICE_MAX_RESIDENT", 1, 1, 3),
        engine_start_timeout=_int("GX_VOICE_ENGINE_START_TIMEOUT", 600, 60, 3600),
        idle_unload_s=_int("GX_VOICE_IDLE_UNLOAD_S", 600, 0, 86400),
        resource_wait_s=_int("GX_VOICE_RESOURCE_WAIT_S", 1800, 0, 86400),
        resource_retry_s=_int("GX_VOICE_RESOURCE_RETRY_S", 15, 2, 600),
        utterance_timeout_s=_int("GX_VOICE_UTTERANCE_TIMEOUT_S", 600, 30, 3600),
        speech_timeout_s=_int("GX_VOICE_SPEECH_TIMEOUT_S", 900, 30, 3600),
        max_upload_bytes=_int("GX_VOICE_MAX_UPLOAD_MB", 32, 1, 256) * 1024 * 1024,
        max_queue=_int("GX_VOICE_MAX_QUEUE", 64, 1, 1000),
        max_job_chars=_int("GX_VOICE_MAX_JOB_CHARS", 20000, 100, 100000),
        peer_health_urls=peers,
        # D-038 sanctioned paths only: idle ComfyUI weights through the media
        # router's free_node, an idle gx-music engine through its if_idle unload.
        evict_idle_peers=_bool("GX_VOICE_EVICT_IDLE_PEERS", True),
        media_router_container=_env("GX_VOICE_MEDIA_ROUTER_CONTAINER", "gx-media-router"),
        music_url=_env("GX_VOICE_MUSIC_URL", "http://192.168.100.11:18820").rstrip("/"),
        music_key_file=Path(_env("GX_VOICE_MUSIC_KEY_FILE", "/srv/projects/gx-cluster/secrets/gx-music/api-key")),
        profile_file=Path(_env("GX_VOICE_PROFILE_FILE", "/srv/projects/gx-cluster/state/guard/profile.json")),
        gxmax_hold_file=Path(_env("GX_VOICE_GXMAX_HOLD", "/srv/projects/gx-cluster/state/guard/node2.gxmax-hold")),
        maintenance_hold_file=Path(_env("GX_VOICE_MAINTENANCE_HOLD",
                                        "/srv/projects/gx-cluster/state/guard/node2.maintenance-hold")),
        pins_file=Path(_env("GX_VOICE_PINS_FILE", "/srv/projects/gx-cluster/state/guard/pins.json")),
        gxmax_hold_ttl_s=_int("GX_VOICE_GXMAX_HOLD_TTL_S", 1200, 60, 86400),
        gxmax_rank_container=_env("GX_VOICE_GXMAX_RANK", "gx-max-rank1"),
        gxmax_deadman_pidfile=Path(_env("GX_VOICE_GXMAX_DEADMAN_PID",
                                        str(Path.home() / ".gx-guard" / "rank1-deadman.pid"))),
        control_plane_container=_env("GX_VOICE_CONTROL_PLANE_CONTAINER", "gx-llama-swap-node02"),
        # The locked normal-operation reserve: it may be raised, never lowered below 30 GiB.
        reserve_gib=_float("GX_GUARD_RESERVE_GIB", 30.0, 30.0, 120.0),
        keep_engine_on_exit=_bool("GX_VOICE_KEEP_ENGINE_ON_EXIT", False),
    )
