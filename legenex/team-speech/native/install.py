#!/usr/bin/python3
"""Stage a fixed, checksum-verified native release. Never activate production."""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

assert len(sys.argv) == 1 and os.getuid() == 1000 and socket.gethostname() == "gx10-02"
package = Path(__file__).resolve().parent
manifest = json.loads((package / "release.json").read_text())
commit = manifest["source_commit"]
assert re.fullmatch("[a-f0-9]{40}", commit)
allowed = {
    "server.py",
    "requirements.lock",
    "landrun-abi7",
    "launch.py",
    "check_inference.py",
    "check_isolation.py",
    "install.py",
    "gx-team-speech-native@.service",
    "upstream.json",
    "LANDRUN-LICENSE",
    "THIRD-PARTY-NOTICES",
    "landrun-abi7.patch",
}
assert set(manifest["files"]) == allowed
for name, digest in manifest["files"].items():
    p = package / name
    assert not p.is_symlink() and re.fullmatch("[a-f0-9]{64}", digest)
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest
release = Path.home() / ".local/lib/legenex-team-speech/releases" / commit
assert not release.exists(), (
    "Immutable release already exists; inspect it before continuing"
)
release.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
partial = Path(tempfile.mkdtemp(prefix=".partial-" + commit + "-", dir=release.parent))
try:
    for name in allowed | {"release.json"}:
        shutil.copy2(package / name, partial / name)
    subprocess.run(
        ["/usr/bin/python3", "-m", "venv", str(partial / "venv")], check=True
    )
    with (partial / "pip-install.log").open("w") as log:
        subprocess.run(
            [
                str(partial / "venv/bin/python"),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(partial / "requirements.lock"),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
        subprocess.run(
            [str(partial / "venv/bin/python"), "-m", "pip", "check"],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    # Repair venv entrypoints/activation text before the atomic directory rename.
    # Runtime executes python -m uvicorn, which also resolves its final venv prefix.
    for script in [*(partial / "venv/bin").iterdir(), partial / "venv/pyvenv.cfg"]:
        if script.is_file() and not script.is_symlink():
            try:
                text = script.read_text()
            except UnicodeDecodeError:
                continue
            script.write_text(text.replace(str(partial), str(release)))
    os.rename(partial, release)
finally:
    if partial.exists():
        shutil.rmtree(partial)
unit = Path.home() / ".config/systemd/user/gx-team-speech-native@.service"
unit.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(release / unit.name, unit)
dropin = unit.parent / "gx-team-speech-native@stage.service.d/release.conf"
dropin.parent.mkdir(exist_ok=True)
dropin.write_text(
    "[Service]\nExecStart=\nExecStart=/usr/bin/python3 "
    + str(release / "launch.py")
    + " stage\nWorkingDirectory="
    + str(release)
    + "\n"
)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
print(json.dumps({"staged_release": commit, "production_activated": False}))
