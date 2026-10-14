"""Download only pinned public speech assets; verify published Kokoro SHA256s."""

import hashlib
import sys
import urllib.request
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/"
for name, expected in {
    "kokoro-v1.0.onnx": "beb0d1848dee9a49da392cc3df26958d46cfa35d321edf434f52949153f0df3a",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}.items():
    target = root / name
    if not target.exists():
        urllib.request.urlretrieve(base + name, target)
    digest = hashlib.file_digest(target.open("rb"), "sha256").hexdigest()
    if digest != expected:
        raise SystemExit("Model checksum mismatch: " + name)
    print(name, "sha256 verified")
from huggingface_hub import snapshot_download

snapshot_download(
    "Systran/faster-whisper-base.en",
    revision="3d3d5dee26484f91867d81cb899cfcf72b96be6c",
    local_dir=root / "whisper-base.en",
    allow_patterns=[
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.txt",
        "preprocessor_config.json",
    ],
)
print("Whisper immutable revision downloaded")
