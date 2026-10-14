#!/usr/bin/python3
"""Synthetic native-UDS TTS/STT probe; reads only the existing service key."""

import http.client
import io
import json
import socket
import time
import uuid
import wave
from pathlib import Path

SOCKET = "/run/user/1000/legenex-team-speech-stage/backend.sock"
KEY = Path("/home/legenex-02/.config/legenex-team-speech/api-key").read_text().strip()


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(100)
        self.sock.connect(SOCKET)


def call(path, body=None, content_type=None, authenticated=True):
    client = UnixHTTP("localhost", timeout=100)
    headers = {"Authorization": "Bearer " + KEY} if authenticated else {}
    if content_type:
        headers["Content-Type"] = content_type
    started = time.monotonic()
    try:
        client.request("POST" if body is not None else "GET", path, body, headers)
        response = client.getresponse()
        return response.status, response.read(), round(time.monotonic() - started, 2)
    finally:
        client.close()


assert call("/health", authenticated=False)[0] == 200
assert call("/voices", authenticated=False)[0] == 401
payload = json.dumps(
    {
        "input": "This is a synthetic bounded native speech acceptance test.",
        "voice": "af_heart",
    }
).encode()
assert (
    call("/v1/audio/speech", payload, "application/json", authenticated=False)[0] == 401
)
status, audio, tts_seconds = call("/v1/audio/speech", payload, "application/json")
assert status == 200 and audio[:4] == b"RIFF", status
with wave.open(io.BytesIO(audio)) as recording:
    duration = recording.getnframes() / recording.getframerate()
assert duration > 1
boundary = "stage" + uuid.uuid4().hex
body = (
    (
        "--" + boundary + '\r\nContent-Disposition: form-data; name="file"; '
        'filename="synthetic.wav"\r\nContent-Type: audio/wav\r\n\r\n'
    ).encode()
    + audio
    + ("\r\n--" + boundary + "--\r\n").encode()
)
status, raw, stt_seconds = call(
    "/v1/audio/transcriptions", body, "multipart/form-data; boundary=" + boundary
)
assert status == 200, status
transcription = json.loads(raw)["text"]
assert "synthetic" in transcription.lower() and "native" in transcription.lower()
report = {
    "transport": "private UDS",
    "tts_seconds": tts_seconds,
    "stt_seconds": stt_seconds,
    "audio_bytes": len(audio),
    "audio_seconds": duration,
    "transcription": transcription,
    "anonymous_inference_denied": True,
    "ok": True,
}
print(json.dumps(report))
