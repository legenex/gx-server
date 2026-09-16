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
        )
