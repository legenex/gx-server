"""Hermetic gx-media-router for the Wan LoRA tests (D-040).

Runs the REAL router code (legenex/media/router: LoRA catalogue, workflow
generator, HTTP API, video worker) on 127.0.0.1 with a fake ComfyUI and a
temporary LoRA root full of synthetic safetensors headers. Requests the real
router should not answer in tests (image generation) are forwarded to a
fallback stub, so existing fixtures keep their behaviour.

    stub = WanRouterStub(key, fallback_url=None)
    stub.url  -> give this to the Control Center as media_base
"""

from __future__ import annotations

import http.client
import http.server
import json
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "legenex" / "media" / "router"))

from gx_media_router.comfy import Artefact, Result, upstream_error  # noqa: E402
from gx_media_router.config import Config  # noqa: E402
from gx_media_router.server import build_server  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

WORKFLOWS = REPO / "legenex" / "media" / "workflows"
#: an MP4 the Control Center accepts (ftyp box) with enough bytes to be a file
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 2048
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _wan_shapes(dim: int = 5120, blocks: int = 4) -> dict:
    shapes = {}
    for b in range(blocks):
        for part in ("self_attn.q", "cross_attn.k", "ffn.0"):
            shapes[f"diffusion_model.blocks.{b}.{part}.lora_down.weight"] = [32, dim]
            shapes[f"diffusion_model.blocks.{b}.{part}.lora_up.weight"] = [dim, 32]
    return shapes


def write_lora(path: Path, shapes: dict, metadata: dict | None = None) -> None:
    header: dict = {k: {"dtype": "F16", "shape": v, "data_offsets": [0, 0]} for k, v in shapes.items()}
    if metadata:
        header["__metadata__"] = metadata
    raw = json.dumps(header).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\x00" * 8)


#: relative path -> (shapes, metadata); names mirror real-world conventions
LORA_FILES: dict[str, tuple[dict, dict | None]] = {
    "wan22/paired/CinematicGlow_high_noise.safetensors": (_wan_shapes(), {"ss_output_name": "CinematicGlow"}),
    "wan22/paired/CinematicGlow_low_noise.safetensors": (_wan_shapes(), None),
    "DetailBoost-HN.safetensors": (_wan_shapes(), None),
    "DetailBoost-LN.safetensors": (_wan_shapes(), None),
    "wan22/high_noise/SoloMotion.safetensors": (_wan_shapes(), None),
    "wan22/general/FilmGrain.safetensors": (_wan_shapes(), None),
    "orphan_high_noise.safetensors": (_wan_shapes(), None),
    "stray_low_noise.safetensors": (_wan_shapes(), None),
    "mystery_style.safetensors": ({"model.layers.0.mlp.lora_A.weight": [8, 4096]}, None),
    "qwen_portrait.safetensors": ({"diffusion_model.transformer_blocks.0.img_mlp.lora_A.weight": [8, 3072]},
                                  None),
    "wan5b_style_high_noise.safetensors": (_wan_shapes(dim=3072), None),
}


class FakeComfy:
    """ComfyUI stand-in: records graphs, finishes after ``delay`` seconds.
    A prompt containing ``e2e-oom`` fails like a CUDA OOM; ``e2e-reject``
    is refused at submit like a node_errors response."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.submitted: list[dict] = []
        self.delay = 0.3
        self._prompts: dict[str, dict] = {}
        self._lock = threading.Lock()

    def node_input_options(self, node_class, input_name):
        return list(self.names)

    def _text(self, graph: dict) -> str:
        return " ".join(str(n.get("inputs", {}).get("text", "")) for n in graph.values())

    def submit(self, graph, client_id):
        if "e2e-reject" in self._text(graph):
            raise upstream_error('ComfyUI rejected the graph: {"1000": {"errors": [{"details": '
                                 '"lora_name not in list"}]}}', "lora_not_visible")
        with self._lock:
            self.submitted.append(graph)
            pid = f"e2e-prompt-{len(self.submitted):04d}"
            self._prompts[pid] = graph
        return pid

    def wait(self, prompt_id, *, timeout, poll_interval=1.0, cancelled=None, thumbnail_node=None):
        time.sleep(self.delay)
        graph = self._prompts[prompt_id]
        if "e2e-oom" in self._text(graph):
            raise upstream_error("ComfyUI execution failed: node 12 (KSamplerAdvanced): "
                                 "torch.OutOfMemoryError: CUDA out of memory", "out_of_memory")
        return Result(prompt_id, (Artefact("wan_00001_.mp4", "gx-video", "output", "images"),
                                  Artefact("wan_00001_.png", "gx-video", "output", "images", thumbnail=True)),
                      self.delay)

    def fetch(self, artefact, *, timeout=120.0):
        return MP4 if artefact.filename.endswith(".mp4") else PNG

    def system_stats(self):
        return {"system": {"comfyui_version": "e2e", "pytorch_version": "2.14"},
                "devices": [{"name": "GB10", "vram_free": 1}]}

    def queue_depth(self):
        return 0

    def free(self, *, unload_models=True, free_memory=True):
        return None


class _Split(http.server.BaseHTTPRequestHandler):
    """Forwards Wan paths to the real router, everything else to the fallback."""

    router_port: int
    fallback: str | None
    WAN_PREFIXES = ("/v1/loras", "/v1/videos", "/v1/workflows", "/health")

    def log_message(self, *args):  # noqa: A003
        pass

    def _forward(self) -> None:
        path = self.path.split("?")[0]
        target = None
        if path.startswith(self.WAN_PREFIXES):
            target = ("127.0.0.1", self.router_port)
        elif self.fallback:
            u = urllib.parse.urlsplit(self.fallback)
            target = (u.hostname or "127.0.0.1", u.port or 80)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        if target is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        conn = http.client.HTTPConnection(*target, timeout=60)
        headers = {k: v for k, v in self.headers.items() if k.lower() in (
            "authorization", "content-type", "x-api-key", "x-gx-status-style")}
        conn.request(self.command, self.path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        self.send_response(res.status)
        for k, v in res.getheaders():
            if k.lower() in ("content-type", "content-disposition", "location", "retry-after"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _forward  # noqa: N815


class WanRouterStub:
    def __init__(self, key: str, fallback_url: str | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.video_root = base / "video-loras"
        self.shared_root = base / "shared-loras"
        self.shared_root.mkdir()
        for rel, (shapes, meta) in LORA_FILES.items():
            write_lora(self.video_root / rel, shapes, meta)
        (self.video_root / "corrupt_style.safetensors").write_bytes(b"\x00" * 64)
        inputs = base / "comfy-input"
        inputs.mkdir()
        names = sorted(LORA_FILES) + ["corrupt_style.safetensors"]
        self.comfy = FakeComfy(names)
        roots = (f"shared={self.shared_root}=/srv/models/shared/loras;"
                 f"video={self.video_root}=/srv/models/video/loras")
        cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key=key, input_dir=inputs, lora_roots=roots,
                     workflow_dir=WORKFLOWS)
        self.service = MediaService(cfg, self.comfy, WorkflowRegistry(WORKFLOWS))
        self.service.rescan_loras()
        self.router = build_server(cfg, self.service)
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        handler = type("Split", (_Split,), {"router_port": self.router.server_address[1],
                                            "fallback": fallback_url})
        self.front = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.front.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.front.server_address[1]}"

    def add_file(self, rel: str, shapes: dict | None = None) -> None:
        write_lora(self.video_root / rel, shapes or _wan_shapes())
        self.comfy.names.append(rel)

    def remove_file(self, rel: str) -> None:
        (self.video_root / rel).unlink()
        if rel in self.comfy.names:
            self.comfy.names.remove(rel)

    def close(self) -> None:
        for srv in (self.front, self.router):
            srv.shutdown()
            srv.server_close()
        self._tmp.cleanup()
