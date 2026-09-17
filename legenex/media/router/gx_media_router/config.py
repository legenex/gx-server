"""Runtime configuration, resolved once from the environment at start-up."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:  # pragma: no cover - start-up failure path
        raise SystemExit(f"{name}: not an integer: {raw!r}") from exc
    if not lo <= value <= hi:
        raise SystemExit(f"{name}: {value} out of range [{lo}, {hi}]")
    return value


def _float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:  # pragma: no cover - start-up failure path
        raise SystemExit(f"{name}: not a number: {raw!r}") from exc
    if not lo <= value <= hi:
        raise SystemExit(f"{name}: {value} out of range [{lo}, {hi}]")
    return value


@dataclass(frozen=True)
class Config:
    """Immutable service configuration."""

    bind_host: str = "0.0.0.0"
    bind_port: int = 18800

    # Upstream ComfyUI. Loopback/compose-internal only -- never a routable name.
    comfy_url: str = "http://127.0.0.1:8188"
    comfy_connect_timeout: float = 10.0

    # Shared secret. Empty string disables authentication (development only).
    api_key: str = ""

    workflow_dir: Path = field(default_factory=lambda: Path("/opt/router/workflows"))

    # A generation holds the single global mutex; these bound how long a caller
    # waits for the mutex and how long one generation may run.
    queue_wait_seconds: int = 900
    image_timeout_seconds: int = 1800
    video_timeout_seconds: int = 3600

    # Bounds enforced on every request. Deliberately conservative: this is a
    # 121 GiB UNIFIED memory node, so an oversized latent starves the host too.
    max_body_bytes: int = 65536
    max_prompt_chars: int = 4000
    max_images_per_request: int = 4
    min_dimension: int = 256
    max_dimension: int = 2048
    dimension_multiple: int = 16
    max_video_frames: int = 161
    max_jobs_retained: int = 200

    default_image_size: str = "1328x1328"

    # Source media for edits / image-to-video / video-to-video (D-031).
    #: ComfyUI's input directory, bind-mounted read-write into the router.
    input_dir: Path = field(default_factory=lambda: Path("/srv/comfy-input"))
    input_ttl_seconds: int = 24 * 3600
    max_upload_body_bytes: int = 160 * 1024 * 1024
    max_image_upload_bytes: int = 25 * 1024 * 1024
    max_video_upload_bytes: int = 150 * 1024 * 1024
    max_source_side: int = 4096
    max_source_pixels: int = 16_777_216
    #: Qwen-Image-Edit works at about one megapixel.
    edit_target_pixels: int = 1024 * 1024
    max_edit_seconds: float = 10.0
    #: Free ComfyUI's model cache when the next job needs different weights.
    free_on_model_switch: bool = True
    #: Free ComfyUI's model cache after this many idle seconds (0 disables).
    idle_free_seconds: int = 600
    #: Memory admission (D-038). gx10-02 is shared with gx-reason and gx-music,
    #: and NORMAL single-node operation must keep at least ``reserve_gib``
    #: MemAvailable (the locked 30 GiB rule). A job is admitted only when
    #:
    #:     MemAvailable - other tenants' pending growth - this job's growth >= reserve
    #:
    #: ``footprint_*`` is the measured growth of a COLD job (weights not loaded).
    #: MEASURED 2026-09-17 on an idle node 2 (114 GiB available): image
    #: generate/edit took MemAvailable down to 57.5 GiB (~57 used), t2v/i2v to
    #: 42.3 GiB (~72 used), the keyframe video edit to 7.2 GiB (~107 used).
    #: A warm job grows by (footprint - what the resident weights already hold),
    #: never by less than ``warm_growth_floor_gib``; when the held amount is not
    #: known the full footprint is assumed.
    #: Empty ``meminfo_path`` disables admission (unit tests only); a configured
    #: but unreadable path REFUSES work.
    meminfo_path: str = ""
    reserve_gib: float = 30.0
    footprint_image_gib: float = 57.0
    footprint_video_gib: float = 72.0
    footprint_keyframe_gib: float = 107.0
    warm_growth_floor_gib: float = 8.0
    #: The most MemAvailable gx10-02 ever reports with nothing on-demand loaded
    #: (measured 113-117 GiB). A job whose growth plus the reserve exceeds this
    #: can never run and is refused at submit time instead of waiting.
    node_capacity_gib: float = 117.0
    #: gx10-02's guard directory (read-only mount): holds, pins, profile and the
    #: residency ledger (D-036). Empty disables the cluster policy (tests, dev).
    guard_dir: str = ""
    #: The gx-music supervisor on the same node (D-038). Its open /health reports
    #: the engine state and memory that is not materialised yet; with a key the
    #: router may ask it to unload an IDLE, unpinned engine to make room.
    music_url: str = ""
    music_key_file: str = ""
    evict_idle_music: bool = True
    #: A video that does not fit waits (status queued, phase "waiting") this long
    #: before it fails with the reason; the check repeats every retry seconds.
    resource_wait_seconds: int = 1800
    resource_retry_seconds: float = 15.0
    #: How long an eviction may take to show up as released memory.
    eviction_settle_seconds: float = 60.0

    @property
    def pin_reserve_gib(self) -> float:
        """A pin keeps weights past the idle timer only above the reserve."""
        return self.reserve_gib

    @classmethod
    def from_env(cls) -> "Config":
        # NOTE: there is deliberately no "magic string" (e.g. "not-required",
        # "none", "disabled") that disables authentication here. Sample env
        # files elsewhere in this repo have historically used exactly such
        # placeholder values (see legenex/gateway/.env.sample), and treating
        # them as "auth off" would let a copy-pasted placeholder silently
        # defeat both this check and the docker-compose `:?` guard that is
        # supposed to refuse to start unauthenticated. The ONLY way to
        # disable authentication is to leave GX_MEDIA_API_KEY unset or empty,
        # which is already logged loudly at start-up (see __main__.py).
        api_key = os.environ.get("GX_MEDIA_API_KEY", "").strip()
        return cls(
            bind_host=os.environ.get("GX_MEDIA_BIND", "0.0.0.0"),
            bind_port=_int("GX_MEDIA_PORT", 18800, 1, 65535),
            comfy_url=os.environ.get("GX_COMFY_URL", "http://127.0.0.1:8188").rstrip("/"),
            api_key=api_key,
            workflow_dir=Path(os.environ.get("GX_MEDIA_WORKFLOW_DIR", "/opt/router/workflows")),
            queue_wait_seconds=_int("GX_MEDIA_QUEUE_WAIT", 900, 1, 7200),
            image_timeout_seconds=_int("GX_MEDIA_IMAGE_TIMEOUT", 1800, 30, 7200),
            video_timeout_seconds=_int("GX_MEDIA_VIDEO_TIMEOUT", 3600, 30, 28800),
            default_image_size=os.environ.get("GX_MEDIA_DEFAULT_SIZE", "1328x1328"),
            input_dir=Path(os.environ.get("GX_MEDIA_INPUT_DIR", "/srv/comfy-input")),
            max_video_upload_bytes=_int("GX_MEDIA_MAX_VIDEO_UPLOAD", 150 * 1024 * 1024, 1024, 1 << 31),
            free_on_model_switch=os.environ.get("GX_MEDIA_FREE_ON_SWITCH", "1") != "0",
            idle_free_seconds=_int("GX_MEDIA_IDLE_FREE", 600, 0, 86400),
            meminfo_path=os.environ.get("GX_MEDIA_MEMINFO", "/proc/meminfo"),
            # The reserve can be raised, never lowered below the locked 30 GiB.
            reserve_gib=_float("GX_MEDIA_RESERVE_GIB", 30.0, 30.0, 100.0),
            footprint_image_gib=_float("GX_MEDIA_FOOTPRINT_IMAGE_GIB", 57.0, 57.0, 121.0),
            footprint_video_gib=_float("GX_MEDIA_FOOTPRINT_VIDEO_GIB", 72.0, 72.0, 121.0),
            footprint_keyframe_gib=_float("GX_MEDIA_FOOTPRINT_KEYFRAME_GIB", 107.0, 107.0, 121.0),
            warm_growth_floor_gib=_float("GX_MEDIA_WARM_GROWTH_FLOOR_GIB", 8.0, 8.0, 121.0),
            node_capacity_gib=_float("GX_MEDIA_NODE_CAPACITY_GIB", 117.0, 60.0, 121.0),
            guard_dir=os.environ.get("GX_MEDIA_GUARD_DIR", ""),
            music_url=os.environ.get("GX_MEDIA_MUSIC_URL", "").rstrip("/"),
            music_key_file=os.environ.get("GX_MEDIA_MUSIC_KEY_FILE", ""),
            evict_idle_music=os.environ.get("GX_MEDIA_EVICT_IDLE_MUSIC", "1") != "0",
            resource_wait_seconds=_int("GX_MEDIA_RESOURCE_WAIT", 1800, 30, 86400),
            resource_retry_seconds=float(_int("GX_MEDIA_RESOURCE_RETRY", 15, 2, 600)),
        )
