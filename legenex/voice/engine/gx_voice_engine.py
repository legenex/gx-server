"""gx-voice engine: a loopback HTTP server that owns the Qwen3-TTS model objects.

Runs inside the ``gx-voice-engine`` container, started and stopped by the
gx-voice supervisor (legenex/voice/gx_voice). It is bound to 127.0.0.1 and
authenticated with a random per-host key, and it only ever reads and writes
under ``GX_VOICE_WORK`` (the supervisor's data root).

    GET  /health                      process, CUDA and variant state
    POST /variants/load   {variant}   make a variant resident (LRU, max GX_VOICE_MAX_RESIDENT)
    POST /variants/unload {variant}
    POST /synthesize      {...}       one utterance -> 24 kHz mono PCM16 WAV under GX_VOICE_WORK

Variants (the router in the supervisor picks one per utterance):
    custom -> Qwen3-TTS-12Hz-1.7B-CustomVoice  (preset speaker + instruction)
    design -> Qwen3-TTS-12Hz-1.7B-VoiceDesign  (voice from a text description)
    base   -> Qwen3-TTS-12Hz-1.7B-Base         (clone from reference audio)

Model weights are loaded once per variant. A voice is never a model copy: a
cloned or designed voice is its reference clip plus a cached clone prompt
(speaker embedding and reference codec codes, a few hundred kB) under
``GX_VOICE_WORK/prompts/<ref-id>/``. The 12 Hz speech tokenizer is byte-identical in
all three repositories (same sha256); when more than one variant is resident
they share one tokenizer instance.
"""

from __future__ import annotations

import collections
import gc
import hmac
import json
import logging
import os
import random
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel, VoiceClonePromptItem

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s gx_voice_engine: %(message)s")
log = logging.getLogger("gx_voice_engine")

VARIANT_TYPES = {"custom": "custom_voice", "design": "voice_design", "base": "base"}
DEFAULT_DIRS = {
    "custom": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "design": "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "base": "Qwen3-TTS-12Hz-1.7B-Base",
}
SAMPLING_KEYS = {"do_sample": bool, "temperature": float, "top_p": float, "top_k": int,
                 "repetition_penalty": float, "subtalker_temperature": float,
                 "subtalker_top_p": float, "subtalker_top_k": int}
WORK = Path(os.environ.get("GX_VOICE_WORK", "/work/data")).resolve()
MODELS = Path(os.environ.get("GX_VOICE_MODELS", "/models"))
MAX_RESIDENT = max(1, min(3, int(os.environ.get("GX_VOICE_MAX_RESIDENT", "1"))))
ATTN = os.environ.get("GX_VOICE_ATTN", "sdpa")
MAX_BODY = 256 * 1024
REF_ID = re.compile(r"ref-[0-9a-f]{32}")


class EngineFault(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _under_work(rel: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\x00" in rel:
        raise EngineFault("invalid path")
    path = (WORK / rel).resolve()
    if WORK not in path.parents:
        raise EngineFault("path escapes the data root")
    return path


def _seed_all(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed % (2 ** 32))
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def _tokenizer_digest(model_dir: Path) -> str | None:
    """sha256 of speech_tokenizer/model.safetensors from the hf-verify manifest."""
    try:
        manifest = json.loads((model_dir / ".gx-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for f in manifest.get("files", []):
        if f.get("path") == "speech_tokenizer/model.safetensors":
            return f.get("sha256")
    return None


class Engine:
    def __init__(self) -> None:
        self.gpu = threading.Lock()
        self.models: collections.OrderedDict[str, Qwen3TTSModel] = collections.OrderedDict()
        self.loading: str | None = None
        self.last_error: str | None = None
        self.prompts: collections.OrderedDict[str, VoiceClonePromptItem] = collections.OrderedDict()
        self.started = time.time()
        self.stats = {"utterances": 0, "audio_s": 0.0, "generate_s": 0.0, "loads": {}, "evictions": 0}
        self.dirs = {}
        for name, default in DEFAULT_DIRS.items():
            self.dirs[name] = MODELS / os.environ.get(f"GX_VOICE_DIR_{name.upper()}", default)

    # ------------------------------------------------------------ state
    def health(self) -> dict:
        cuda = torch.cuda.is_available()
        mem = {}
        if cuda:
            mem = {"allocated_gib": round(torch.cuda.memory_allocated() / 2 ** 30, 2),
                   "reserved_gib": round(torch.cuda.memory_reserved() / 2 ** 30, 2)}
        return {"ready": cuda, "loaded": list(self.models), "loading": self.loading,
                "max_resident": MAX_RESIDENT, "busy": self.gpu.locked(), "cuda": cuda,
                "device": torch.cuda.get_device_name(0) if cuda else None,
                "torch": torch.__version__, "attn": ATTN, "gpu_memory": mem,
                "prompts_cached": len(self.prompts), "last_error": self.last_error,
                "uptime_s": round(time.time() - self.started, 1), "stats": self.stats}

    def _evict(self, keep: str | None = None) -> None:
        while len(self.models) >= MAX_RESIDENT:
            name = next((n for n in self.models if n != keep), None)
            if name is None:
                return
            self._drop(name)

    def _drop(self, name: str) -> None:
        self.models.pop(name, None)
        if name == "base":
            self.prompts.clear()
        gc.collect()
        torch.cuda.empty_cache()
        self.stats["evictions"] += 1
        log.info("variant %s unloaded", name)

    def load(self, name: str) -> float:
        """Make `name` resident; returns the seconds spent (0 when it already was)."""
        if name not in VARIANT_TYPES:
            raise EngineFault(f"unknown variant {name!r}")
        if name in self.models:
            self.models.move_to_end(name)
            return 0.0
        self._evict(keep=name)
        path = self.dirs[name]
        if not (path / "config.json").is_file():
            raise EngineFault(f"the {name} model files are not installed", 503, "model_missing")
        self.loading = name
        t0 = time.perf_counter()
        try:
            model = Qwen3TTSModel.from_pretrained(str(path), device_map="cuda:0", dtype=torch.bfloat16,
                                                  attn_implementation=ATTN)
        finally:
            self.loading = None
        if model.model.tts_model_type != VARIANT_TYPES[name]:
            raise EngineFault(f"{path.name} is not a {VARIANT_TYPES[name]} model", 500, "model_mismatch")
        digest = _tokenizer_digest(path)
        for other, m in self.models.items():
            if digest and digest == _tokenizer_digest(self.dirs[other]):
                model.model.speech_tokenizer = m.model.speech_tokenizer
                gc.collect()
                torch.cuda.empty_cache()
                log.info("variant %s shares the speech tokenizer of %s", name, other)
                break
        self.models[name] = model
        seconds = round(time.perf_counter() - t0, 2)
        self.stats["loads"][name] = seconds
        log.info("variant %s loaded in %.1fs from %s", name, seconds, path)
        return seconds

    # ----------------------------------------------------------- prompts
    def _prompt(self, model: Qwen3TTSModel, ref: dict) -> VoiceClonePromptItem:
        key = ref.get("cache_key")
        if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise EngineFault("reference.cache_key must be a sha256 hex digest")
        ref_id = ref.get("ref_id")
        if not isinstance(ref_id, str) or not REF_ID.fullmatch(ref_id):
            raise EngineFault("reference.ref_id is invalid")
        if key in self.prompts:
            self.prompts.move_to_end(key)
            return self.prompts[key]
        cache = WORK / "prompts" / ref_id / f"{key}.pt"
        device = model.device
        item = None
        if cache.is_file():
            try:
                d = torch.load(cache, map_location=device, weights_only=True)
                item = VoiceClonePromptItem(ref_code=d["ref_code"], ref_spk_embedding=d["ref_spk_embedding"],
                                            x_vector_only_mode=bool(d["x_vector_only_mode"]),
                                            icl_mode=bool(d["icl_mode"]), ref_text=d.get("ref_text") or None)
            except Exception:  # noqa: BLE001 - a bad cache is rebuilt, never trusted
                log.warning("discarding unreadable prompt cache %s", cache.name)
                item = None
        if item is None:
            audio = _under_work(ref.get("path", ""))
            if not audio.is_file():
                raise EngineFault("the reference audio is missing", 404, "reference_missing")
            text = ref.get("text") or None
            xvec = bool(ref.get("x_vector_only")) or not text
            item = model.create_voice_clone_prompt(ref_audio=str(audio), ref_text=text, x_vector_only_mode=xvec)[0]
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".part")
            torch.save({"ref_code": None if item.ref_code is None else item.ref_code.detach().cpu(),
                        "ref_spk_embedding": item.ref_spk_embedding.detach().cpu(),
                        "x_vector_only_mode": item.x_vector_only_mode, "icl_mode": item.icl_mode,
                        "ref_text": item.ref_text or ""}, tmp)
            os.replace(tmp, cache)
        self.prompts[key] = item
        while len(self.prompts) > 32:
            self.prompts.popitem(last=False)
        return item

    # -------------------------------------------------------- synthesize
    def synthesize(self, req: dict) -> dict:
        variant = req.get("variant")
        text = req.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise EngineFault("text must be 1-4000 characters")
        language = req.get("language") or "Auto"
        seed = req.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2 ** 31:
            raise EngineFault("seed must be an integer")
        max_new = req.get("max_new_tokens", 2048)
        if not isinstance(max_new, int) or not 16 <= max_new <= 8192:
            raise EngineFault("max_new_tokens must be 16-8192")
        kwargs: dict = {"max_new_tokens": max_new}
        for k, typ in SAMPLING_KEYS.items():
            if req.get("sampling", {}).get(k) is not None:
                kwargs[k] = typ(req["sampling"][k])
        out = _under_work(req.get("out", ""))
        with self.gpu:
            load_s = self.load(variant)
            model = self.models[variant]
            _seed_all(seed)
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            try:
                if variant == "custom":
                    speaker = req.get("speaker")
                    if not isinstance(speaker, str) or not speaker:
                        raise EngineFault("speaker is required for the custom variant")
                    wavs, sr = model.generate_custom_voice(text=text, speaker=speaker, language=language,
                                                           instruct=req.get("instruct") or None, **kwargs)
                elif variant == "design":
                    instruct = req.get("instruct") or ""
                    if not instruct.strip():
                        raise EngineFault("a voice description is required for the design variant")
                    wavs, sr = model.generate_voice_design(text=text, instruct=instruct, language=language, **kwargs)
                else:
                    ref = req.get("reference")
                    if not isinstance(ref, dict):
                        raise EngineFault("reference is required for the base variant")
                    prompt = self._prompt(model, ref)
                    wavs, sr = model.generate_voice_clone(text=text, language=language,
                                                          voice_clone_prompt=[prompt], **kwargs)
                torch.cuda.synchronize()
            except ValueError as exc:
                raise EngineFault(str(exc)[:300]) from exc
            gen_s = time.perf_counter() - t0
        wav = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
        if not np.isfinite(wav).all():
            raise EngineFault("the model produced invalid samples", 500, "invalid_audio")
        wav = np.clip(wav, -1.0, 1.0)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        sf.write(str(tmp), wav, int(sr), subtype="PCM_16", format="WAV")
        os.replace(tmp, out)
        seconds = len(wav) / float(sr)
        self.stats["utterances"] += 1
        self.stats["audio_s"] = round(self.stats["audio_s"] + seconds, 2)
        self.stats["generate_s"] = round(self.stats["generate_s"] + gen_s, 2)
        return {"sample_rate": int(sr), "frames": int(len(wav)), "seconds": round(seconds, 3),
                "generate_s": round(gen_s, 3), "variant_load_s": load_s,
                "rtf": round(gen_s / seconds, 3) if seconds else None, "variant": variant}


ENGINE: Engine


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-voice-engine"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    key = ""

    def log_message(self, fmt, *args):  # noqa: A003, ANN001
        return

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > MAX_BODY:
            raise EngineFault("body too large", 413, "too_large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise EngineFault("body must be a JSON object")
        return data

    def _handle(self) -> None:
        try:
            token = (self.headers.get("Authorization") or "")[7:]
            if not hmac.compare_digest(token.encode(), self.key.encode()):
                raise EngineFault("unauthorized", 401, "unauthorized")
            path, method = self.path.split("?")[0], self.command
            if method == "GET" and path == "/health":
                return self._json(200, ENGINE.health())
            if method != "POST":
                raise EngineFault("not found", 404, "not_found")
            body = self._body()
            if path == "/variants/load":
                with ENGINE.gpu:
                    seconds = ENGINE.load(str(body.get("variant")))
                return self._json(200, {"loaded": list(ENGINE.models), "seconds": seconds})
            if path == "/variants/unload":
                with ENGINE.gpu:
                    if body.get("variant") in ENGINE.models:
                        ENGINE._drop(body["variant"])  # noqa: SLF001
                return self._json(200, {"loaded": list(ENGINE.models)})
            if path == "/synthesize":
                return self._json(200, ENGINE.synthesize(body))
            raise EngineFault("not found", 404, "not_found")
        except EngineFault as exc:
            self._json(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except torch.cuda.OutOfMemoryError:
            ENGINE.last_error = "out of memory"
            log.error("CUDA out of memory:\n%s", traceback.format_exc())
            gc.collect()
            torch.cuda.empty_cache()
            self._json(507, {"error": {"code": "out_of_memory", "message": "the GPU ran out of memory"}})
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": {"code": "invalid_request", "message": str(exc)[:300]}})
        except Exception as exc:  # noqa: BLE001
            ENGINE.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            log.error("unhandled error:\n%s", traceback.format_exc())
            self._json(500, {"error": {"code": "engine_error", "message": ENGINE.last_error}})

    do_GET = do_POST = _handle  # noqa: N815


def main() -> int:
    global ENGINE  # noqa: PLW0603
    key = os.environ.get("GX_VOICE_ENGINE_KEY", "")
    if len(key) < 32:
        log.error("GX_VOICE_ENGINE_KEY missing or too short")
        return 78
    if not torch.cuda.is_available():
        log.error("CUDA is not available")
        return 70
    WORK.mkdir(parents=True, exist_ok=True)
    ENGINE = Engine()
    preload = [v for v in os.environ.get("GX_VOICE_PRELOAD", "").split(",") if v]
    for v in preload[:MAX_RESIDENT]:
        with ENGINE.gpu:
            ENGINE.load(v)
    Handler.key = key
    host = os.environ.get("GX_VOICE_ENGINE_HOST", "127.0.0.1")
    port = int(os.environ.get("GX_VOICE_ENGINE_PORT", "18831"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log.info("listening on %s:%s (max resident variants %s, attention %s, torch %s)",
             host, port, MAX_RESIDENT, ATTN, torch.__version__)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
