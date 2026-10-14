#!/usr/bin/python3
"""Package reviewed committed runtime files on GX01; never deploy a GX checkout."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

assert len(sys.argv) == 1, "No package arguments are accepted"
source = Path(__file__).resolve().parent
repo = source.parents[2]
subprocess.run(
    ["git", "-C", str(repo), "diff", "--quiet", "HEAD", "--", "legenex/team-speech"],
    check=True,
)
assert not subprocess.check_output(
    [
        "git",
        "-C",
        str(repo),
        "ls-files",
        "--others",
        "--exclude-standard",
        "legenex/team-speech",
    ]
).strip(), "Commit runtime source first"
commit = subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
).strip()
output = Path(tempfile.mkdtemp(prefix="legenex-speech-native-"))
files = {
    "server.py": source.parent / "server.py",
    "requirements.lock": source.parent / "requirements.lock",
    "landrun-abi7": Path.home()
    / ".cache/legenex-team-speech/landrun-62823c0-abi7/landrun-abi7",
}
for name in [
    "launch.py",
    "check_inference.py",
    "check_isolation.py",
    "install.py",
    "gx-team-speech-native@.service",
    "upstream.json",
    "LANDRUN-LICENSE",
    "THIRD-PARTY-NOTICES",
    "landrun-abi7.patch",
]:
    files[name] = source / name
checksums = {}
for name, original in files.items():
    shutil.copy2(original, output / name)
    checksums[name] = hashlib.sha256((output / name).read_bytes()).hexdigest()
(output / "release.json").write_text(
    json.dumps({"source_commit": commit, "files": checksums}, indent=2) + "\n"
)
print(output)
