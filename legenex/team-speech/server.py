"""Private CPU speech backend for the authenticated Legenex team voice studio.

One bounded inference slot; no CUDA, no model routing changes, no public bind.
Kokoro ONNX and Whisper run on GX10-02. User authentication and ownership are
enforced by the studio; this separate hop requires its own service credential.
"""

from __future__ import annotations

import hmac
import io
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
MODELS = Path(os.environ.get("SPEECH_MODELS", "/models"))
KEY = (
    Path(os.environ.get("SPEECH_KEY_FILE", "/run/secrets/speech-key"))
    .read_text()
    .strip()
)
if len(KEY) < 32:
    raise RuntimeError("speech service credential is missing or too short")
SLOT = threading.Lock()
BUSY_SINCE = None
MAINTENANCE = False
TTS = None
STT = None
VOICES = ["af_heart", "af_bella", "am_adam", "am_michael", "bf_emma", "bm_george"]


def authorize(request: Request) -> None:
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, "Bearer " + KEY):
        raise HTTPException(401, "Service authentication required")


@app.middleware("http")
async def bounded_ingress(request: Request, call_next):
    # Authenticate before FastAPI parses/spools a multipart upload. Unknown
    # body lengths are refused rather than allowing an unbounded temporary file.
    if request.url.path != "/health":
        try:
            authorize(request)
        except HTTPException:
            from fastapi.responses import JSONResponse

            return JSONResponse(
                {"detail": "Service authentication required"}, status_code=401
            )
    if request.method == "POST":
        from fastapi.responses import JSONResponse

        try:
            length = int(request.headers["content-length"])
        except (KeyError, ValueError):
            return JSONResponse(
                {"detail": "A bounded Content-Length is required"}, status_code=411
            )
        limit = (
            21 * 1024 * 1024
            if request.url.path.endswith("/transcriptions")
            else 32 * 1024
        )
        if length < 0 or length > limit:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    return await call_next(request)


def start_deadline():
    global BUSY_SINCE
    BUSY_SINCE = time.monotonic()
    # A client timeout cannot interrupt native inference. Kill this isolated
    # worker at the hard deadline; its supervisor restarts it and the studio records
    # the interrupted job as failed. Jobs are never automatically replayed.
    timer = threading.Timer(300, os._exit, args=(70,))
    timer.daemon = True
    timer.start()
    return timer


@app.get("/health")
def health():
    ready = all(
        (MODELS / path).is_file()
        for path in [
            "kokoro-v1.0.onnx",
            "voices-v1.0.bin",
            "whisper-base.en/model.bin",
            "whisper-base.en/vocabulary.txt",
            "whisper-base.en/config.json",
            "whisper-base.en/tokenizer.json",
        ]
    )
    return {
        "ok": True,
        "models_ready": ready,
        "runtime": "cpu",
        "busy": SLOT.locked() and not MAINTENANCE,
        "maintenance": MAINTENANCE,
        "tts_loaded": TTS is not None,
        "stt_loaded": STT is not None,
        "busy_seconds": round(time.monotonic() - BUSY_SINCE, 1) if BUSY_SINCE else 0,
    }


@app.post("/maintenance", dependencies=[Depends(authorize)])
def enter_maintenance():
    """Atomically drain admission; only a process restart clears this barrier."""
    global MAINTENANCE
    if MAINTENANCE:
        return {"ok": True, "maintenance": True, "busy": False}
    if not SLOT.acquire(blocking=False):
        return JSONResponse(
            {"ok": False, "maintenance": False, "busy": True}, status_code=409
        )
    # Deliberately retain SLOT. Inference returns 429 until this process exits.
    # There is no timer or HTTP endpoint that reopens admission after an ack.
    MAINTENANCE = True
    return {"ok": True, "maintenance": True, "busy": False}


class Speech(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: str = Field(min_length=1, max_length=6000)
    voice: str = "af_heart"
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@app.get("/voices", dependencies=[Depends(authorize)])
def voices():
    return {"voices": VOICES, "cloning_supported": False}


@app.post("/v1/audio/speech", dependencies=[Depends(authorize)])
def speech(body: Speech):
    global TTS
    if body.voice not in VOICES:
        raise HTTPException(400, "Unsupported preset voice")
    if not SLOT.acquire(blocking=False):
        raise HTTPException(429, "Speech worker busy; retry later")
    timer = start_deadline()
    try:
        import soundfile as sf

        if TTS is None:
            from kokoro_onnx import Kokoro

            TTS = Kokoro(
                str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin")
            )
        samples, rate = TTS.create(
            body.input,
            voice=body.voice,
            speed=body.speed,
            lang="en-gb" if body.voice.startswith("b") else "en-us",
        )
        output = io.BytesIO()
        sf.write(output, samples, rate, format="WAV", subtype="PCM_16")
        return Response(
            output.getvalue(),
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )
    except Exception as exc:  # noqa: BLE001 - native inference failures must not disclose user input
        # Inputs and service credentials never enter exception responses.
        raise HTTPException(
            503, "Speech inference failed: " + type(exc).__name__
        ) from None
    finally:
        timer.cancel()
        global BUSY_SINCE
        BUSY_SINCE = None
        SLOT.release()


@app.post("/v1/audio/transcriptions", dependencies=[Depends(authorize)])
def transcription(file: Annotated[UploadFile, File()]):
    global STT
    if not SLOT.acquire(blocking=False):
        raise HTTPException(429, "Speech worker busy; retry later")
    timer = start_deadline()
    try:
        import soundfile as sf

        raw = file.file.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise HTTPException(413, "Audio exceeds 20 MB")
        try:
            metadata = sf.info(io.BytesIO(raw))
            if (
                metadata.format != "WAV"
                or metadata.duration > 600
                or metadata.channels > 2
            ):
                raise ValueError("invalid audio")
        except (ValueError, RuntimeError):
            raise HTTPException(400, "Use a WAV recording under ten minutes") from None
        if STT is None:
            from faster_whisper import WhisperModel

            STT = WhisperModel(
                str(MODELS / "whisper-base.en"),
                device="cpu",
                compute_type="int8",
                cpu_threads=2,
                num_workers=1,
                local_files_only=True,
            )
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio:
            audio.write(raw)
            audio.flush()
            segments, info = STT.transcribe(audio.name, beam_size=3, language="en")
            text = " ".join(segment.text.strip() for segment in segments)
        return {"text": text, "language": info.language, "model": "whisper-base.en"}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - native inference failures must not disclose user audio
        raise HTTPException(
            503, "Transcription failed: " + type(exc).__name__
        ) from None
    finally:
        timer.cancel()
        global BUSY_SINCE
        BUSY_SINCE = None
        SLOT.release()
